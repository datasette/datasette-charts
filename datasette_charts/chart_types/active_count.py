"""
active_count: how many rows are active over time, for rows with a start and an
end, such as terms of office, subscriptions or tickets. Optionally split
into series by a category (the top K, with the rest as "Other").

The SQL never joins rows to rows. Each row adds +1 at its start bucket and
-1 at its end bucket, the events are summed per bucket, and a running total
over the buckets gives the count. A row counts in each bucket from the one
it starts in up to, but not including, the one it ends in, so a term that
ends on the day the next one starts is not counted twice:

    active(b) = (rows starting at or before b) - (rows ending at or before b)
                + (rows starting and ending in b)

The last term is there so that a row that starts and ends in the same
bucket still counts in that bucket. Each returned bucket has ``value``, the
rows active in it, and ``after``, the rows still active once it is over.
Between two returned buckets the count stays at ``after``, so the renderer
draws a step there.

A row with no end is treated as still active up to the latest start or end
in the source, the last bucket drawn. A row that ends before it starts is
left out.
"""

from __future__ import annotations

from ..profiler.model import TemporalInfo
from ..profiler.roles import name_tokens
from ..sources import SOURCE_CTE, quote
from ..sqlgen import (
    MAX_TIME_BUCKETS,
    SpecError,
    choose_time_unit,
    estimate_buckets,
    merge_params,
    parse_iso,
    span_description,
    time_units_for,
)
from .bar import label_sql
from .base import (
    BuiltinChartType,
    Candidate,
    Channel,
    CompiledQuery,
    category_columns,
    category_quality,
    null_penalty,
    top_coverage,
)
from .dimensions import temporal_dimension

DEFAULT_SERIES_LIMIT = 5
# Step charts read well with more buckets than a line chart uses
TARGET_TIME_BUCKETS = 250
# Words in a column name that say it starts or ends a span, as pairs
START_END_WORDS = (
    ("start", "end"),
    ("start", "finish"),
    ("start", "stop"),
    ("starts", "ends"),
    ("started", "ended"),
    ("started", "finished"),
    ("started", "stopped"),
    ("begin", "end"),
    ("begins", "ends"),
    ("began", "ended"),
    ("beginning", "end"),
    ("from", "to"),
    ("from", "until"),
    ("from", "thru"),
    ("from", "through"),
    ("opened", "closed"),
    ("open", "close"),
    ("open", "closed"),
    ("opening", "closing"),
    ("first", "last"),
    ("since", "until"),
    ("effective", "expiry"),
    ("effective", "expires"),
    ("effective", "expiration"),
    ("checkin", "checkout"),
    ("arrival", "departure"),
    ("arrived", "departed"),
    ("admitted", "discharged"),
    ("hired", "terminated"),
    ("created", "closed"),
    ("created", "resolved"),
    ("created", "deleted"),
)


def is_start_end_pair(start: str, end: str) -> bool:
    """
    Whether two column names look like the start and end of a span:
    start/end, start_date/end_date, valid_from/valid_to, opened_at/closed_at.
    The names must be the same apart from one start word and its end word.
    """
    a, b = name_tokens(start), name_tokens(end)
    if len(a) != len(b) or not a:
        return False
    differ = [i for i in range(len(a)) if a[i] != b[i]]
    if len(differ) != 1:
        return False
    i = differ[0]
    return (a[i], b[i]) in START_END_WORDS


def combined_span(start, end) -> TemporalInfo | None:
    "From the earliest start to the latest start or end"
    s, e = start.temporal, end.temporal
    if s is None or e is None:
        return None
    lows = [v for v in (s.min, e.min) if parse_iso(v)]
    highs = [v for v in (s.max, e.max) if parse_iso(v)]
    if not lows or not highs:
        return None
    return TemporalInfo(
        kind=s.kind if s.kind == e.kind else "iso_datetime",
        match_rate=1.0,
        min=min(lows, key=parse_iso),
        max=max(highs, key=parse_iso),
        has_offset=s.has_offset or e.has_offset,
        origin="detected",
    )


def common_units(profile, start: str, end: str) -> tuple:
    "The time units both columns can be bucketed by"
    kinds = []
    for name in (start, end):
        column = profile.column(name) if profile else None
        if column is None:
            raise SpecError(f"No such column: {name}")
        if column.temporal:
            kinds.append(column.temporal.kind)
        elif column.temporal_candidate:
            kinds.append(column.temporal_candidate["kind"])
        else:
            raise SpecError(
                f"{name} does not hold dates or times in a format SQLite can read"
            )
    units = time_units_for(kinds[0])
    return tuple(u for u in units if u in time_units_for(kinds[1]))


def consistent(start, end) -> bool:
    "Ranges consistent with start <= end: neither end of the range is reversed"
    s, e = start.temporal, end.temporal
    pairs = [(s.min, e.min), (s.max, e.max)]
    for a, b in pairs:
        a, b = parse_iso(a), parse_iso(b)
        if a is None or b is None or a > b:
            return False
    return True


class ActiveCount(BuiltinChartType):
    name = "active_count"
    label = "Active over time"
    description = (
        "How many rows are active at each point in time, from a start and an"
        " end column, optionally split by a category"
    )
    icon = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        ' stroke-linejoin="round" aria-hidden="true">'
        '<polyline points="3,19 3,15 7,15 7,10 11,10 11,6 15,6 15,12 19,12 19,16 21,16"/>'
        "</svg>"
    )
    js_file = "active_count.js"
    channels = [
        Channel(
            name="start", label="Start", roles=("temporal",), allows_time_unit=True
        ),
        Channel(name="end", label="End", roles=("temporal",)),
        Channel(
            name="series",
            label="Split by",
            roles=("category", "boolean", "foreign_key"),
            required=False,
            allows_limit=True,
        ),
    ]
    options = {
        "layout": {
            "label": "Series",
            "values": ["stacked", "lines"],
            "default": "stacked",
        }
    }
    max_rows = MAX_TIME_BUCKETS * (DEFAULT_SERIES_LIMIT + 1)
    annotation_axes = {"x": "time", "y": "number"}

    def validate(self, spec, columns) -> list:
        errors = super().validate(spec, columns)
        encoding = spec.get("encoding") or {}
        start, end = encoding.get("start"), encoding.get("end")
        if (
            isinstance(start, dict)
            and isinstance(end, dict)
            and start.get("column") is not None
            and start.get("column") == end.get("column")
        ):
            errors.append("start and end must be different columns")
        return errors

    def propose(self, profile) -> list:
        temporal = [c for c in profile.columns if c.role == "temporal" and c.temporal]
        candidates = []
        for start in temporal:
            for end in temporal:
                if start is end or not is_start_end_pair(start.name, end.name):
                    continue
                if not consistent(start, end):
                    continue
                span = combined_span(start, end)
                units = common_units(profile, start.name, end.name)
                if span is None or not units:
                    continue
                unit, buckets = choose_time_unit(span, TARGET_TIME_BUCKETS)
                if unit not in units:
                    unit = units[-1]
                    buckets = estimate_buckets(span.min, span.max, unit)
                if not buckets or buckets < 3 or buckets > MAX_TIME_BUCKETS:
                    continue
                score = 0.82 - null_penalty(start.null_rate, 0.4)
                reason = f"Each row runs from {start.name} to {end.name}: rows active in each {unit}, across {span_description(span.min, span.max)}"
                if end.null_rate:
                    reason += f"; rows with no {end.name} count as still active"
                encoding = {
                    "start": {"column": start.name, "timeUnit": unit},
                    "end": {"column": end.name},
                }
                signals = {"unit": unit, "buckets": buckets}
                candidates.append(
                    Candidate(
                        spec=self._spec(encoding),
                        score=round(score, 4),
                        reason=reason,
                        signals=signals,
                    )
                )
                # Split by the category whose top few values cover most rows
                splits = []
                for category in category_columns(profile):
                    distinct = category.distinct_count or 0
                    if distinct < 2:
                        continue
                    _, share = top_coverage(category, DEFAULT_SERIES_LIMIT)
                    if share < 0.6:
                        continue
                    quality, _ = category_quality(category)
                    splits.append((quality, category))
                splits.sort(key=lambda pair: (-pair[0], pair[1].position))
                for quality, category in splits[:1]:
                    series = {
                        "column": category.name,
                        "limit": DEFAULT_SERIES_LIMIT,
                        "other": True,
                    }
                    candidates.append(
                        Candidate(
                            spec=self._spec({**encoding, "series": series}),
                            score=round(score - 0.2 - 0.3 * max(0, 0.62 - quality), 4),
                            reason=f"Rows active in each {unit}, split by {category.name}",
                            signals={**signals, "series_quality": quality},
                        )
                    )
        return candidates

    def _spec(self, encoding: dict) -> dict:
        return {"v": 1, "type": self.name, "encoding": encoding, "options": {}}

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        start = encoding["start"]
        end = encoding["end"]
        series = encoding.get("series")
        units = common_units(profile, start["column"], end["column"])
        unit = start.get("timeUnit")
        span = combined_span(
            profile.column(start["column"]), profile.column(end["column"])
        )
        if unit is None:
            unit = choose_time_unit(span, TARGET_TIME_BUCKETS)[0] if span else None
            if unit not in units:
                unit = units[-1] if units else None
        if unit is None or unit not in units:
            raise SpecError(
                "{} and {} cannot both be bucketed by {}".format(
                    start["column"], end["column"], unit
                )
            )
        if span:
            buckets = estimate_buckets(span.min, span.max, unit)
            if buckets and buckets > MAX_TIME_BUCKETS:
                raise SpecError(
                    "{} by {} gives about {:,} points; at most {} can be shown,"
                    " so use a longer timeUnit".format(
                        start["column"], unit, buckets, MAX_TIME_BUCKETS
                    )
                )
        s = temporal_dimension(
            {"column": start["column"], "timeUnit": unit}, profile, what="start"
        )
        e = temporal_dimension(
            {"column": end["column"], "timeUnit": unit}, profile, what="end"
        )
        # A row that ends before it starts is left out. Values of one kind
        # compare directly, so this holds within a bucket too; values of two
        # kinds are compared by bucket only.
        in_order = "1"
        if s["kind"] == e["kind"]:
            in_order = "coalesce({end} >= {start}, 1)".format(
                start=quote(start["column"]), end=quote(end["column"])
            )
        # A start outside the detection window is dropped; an end outside it
        # is treated like a missing end
        where = f" where {s['filter']}" if s["filter"] else ""
        start_bucket, end_bucket = s["bucket"], e["bucket"]
        if unit == "year":
            # A year column buckets to a number, a date to text: compare as text
            start_bucket = f"cast({start_bucket} as text)"
            end_bucket = f"cast({end_bucket} as text)"
        if e["filter"]:
            end_bucket = f"case when {e['filter']} then {end_bucket} end"
        meta = {
            "timeUnit": unit,
            "start": start["column"],
            "end": end["column"],
            "y": "Rows active",
            "layout": (spec.get("options") or {}).get("layout") or "stacked",
        }
        params = {}
        series_sql = "null"
        if series:
            series_sql = quote(series["column"])
        spans = (
            f"_chart_spans as (\n"
            f"  select {start_bucket} as s, {end_bucket} as e, {series_sql} as g,"
            f" {in_order} as in_order from {SOURCE_CTE}{where}\n"
            "),\n"
            "_chart_clean as (\n"
            "  select s, e, g from _chart_spans\n"
            "  where s is not null and (e is null or (in_order and e >= s))\n"
            ")"
        )
        # +1 where a row starts, -1 where it ends. A row with no end never
        # drops out. "same" counts rows that start and end in one bucket,
        # which are active in it but not after it.
        events = (
            "  select {cols}s as b, 1 as plus, 0 as minus, coalesce(e = s, 0) as same"
            " from {table}\n"
            "  union all\n"
            "  select {cols}e as b, 0 as plus, 1 as minus, 0 as same"
            " from {table} where e is not null\n"
        )
        running = (
            "sum(plus) over w - sum(minus) over w + same as value,\n"
            "    sum(plus) over w - sum(minus) over w as after"
        )
        if not series:
            sql = (
                f"{source.with_clause()},\n{spans},\n"
                "_chart_events as (\n"
                + events.format(cols="", table="_chart_clean")
                + "),\n"
                "_chart_per_bucket as (\n"
                "  select b, sum(plus) as plus, sum(minus) as minus, sum(same) as same\n"
                "  from _chart_events group by b\n"
                ")\n"
                f"select b as bucket,\n    {running}\n"
                "from _chart_per_bucket\n"
                "window w as (order by b rows unbounded preceding)\n"
                f"order by b limit {MAX_TIME_BUCKETS + 1}"
            )
            return CompiledQuery(
                sql=sql,
                params=merge_params(source, params),
                max_rows=MAX_TIME_BUCKETS,
                meta=meta,
            )

        limit = int(series.get("limit") or DEFAULT_SERIES_LIMIT)
        other = bool(series.get("other", True))
        params["chart_series_limit"] = limit
        keep = "" if other else "  where r.rn <= :chart_series_limit\n"
        label = label_sql(profile, series["column"], "t.series")
        label = f", {label} as series_label" if label else ""
        max_rows = MAX_TIME_BUCKETS * (limit + 1)
        sql = (
            f"{source.with_clause()},\n{spans},\n"
            "_chart_series_rank as (\n"
            "  select g, row_number() over (order by count(*) desc, g) as rn\n"
            "  from _chart_clean group by 1\n"
            "),\n"
            "_chart_tagged as (\n"
            "  select c.s, c.e, min(r.rn, :chart_series_limit + 1) as position,\n"
            "    case when r.rn <= :chart_series_limit then r.g end as series,\n"
            "    r.rn > :chart_series_limit as is_other\n"
            "  from _chart_clean c join _chart_series_rank r on r.g is c.g\n"
            f"{keep}"
            "),\n"
            "_chart_events as (\n"
            + events.format(cols="position, series, is_other, ", table="_chart_tagged")
            + "),\n"
            "_chart_per_bucket as (\n"
            "  select position, max(series) as series, max(is_other) as is_other, b,\n"
            "    sum(plus) as plus, sum(minus) as minus, sum(same) as same\n"
            "  from _chart_events group by position, b\n"
            ")\n"
            f"select bucket, series, is_other, value, after{label} from (\n"
            f"  select b as bucket, series, is_other, position,\n    {running}\n"
            "  from _chart_per_bucket\n"
            "  window w as (partition by position order by b rows unbounded preceding)\n"
            ") t\n"
            f"order by bucket, position limit {max_rows + 1}"
        )
        meta["series"] = series["column"]
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, params),
            max_rows=max_rows,
            meta=meta,
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        unit = encoding["start"].get("timeUnit")
        title = "Rows active {}from {} to {}".format(
            f"each {unit} " if unit else "",
            encoding["start"]["column"],
            encoding["end"]["column"],
        )
        if encoding.get("series"):
            title += " by {}".format(encoding["series"]["column"])
        return title
