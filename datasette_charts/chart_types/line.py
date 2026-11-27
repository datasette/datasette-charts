from __future__ import annotations

from ..profiler.roles import name_tokens
from ..sources import quote
from ..sqlgen import (
    MAX_TIME_BUCKETS,
    TARGET_TIME_BUCKETS,
    aggregate_over,
    aggregate_title,
    choose_time_unit,
    measure_sql,
    per_time_unit,
    merge_params,
    not_materialized,
    rank_over,
    span_description,
)
from .base import (
    ALL_AGGREGATES,
    BuiltinChartType,
    Candidate,
    Channel,
    CompiledQuery,
    best_measures,
    category_columns,
    category_quality,
    default_aggregate,
    null_penalty,
)
from .bar import label_sql
from .dimensions import temporal_dimension

DEFAULT_SERIES_LIMIT = 5
# Aim for at least this many rows in each time bucket
ROWS_PER_BUCKET = 5
UNIT_ADJECTIVES = {
    "decade": "decade",
    "year": "yearly",
    "quarter": "quarterly",
    "month": "monthly",
    "week": "weekly",
    "day": "daily",
    "hour": "hourly",
    "minute": "per-minute",
}
# Timestamps that say when a row was last touched make a weaker time axis
WEAK_TIME_NAME_TOKENS = {"updated", "modified", "pushed", "synced", "fetched"}


def per_time_category(profile, column, categories):
    """
    The category with exactly one row per time for each of its values, as
    in a summary query with no primary key to say so, or None. Counting rows
    over time then charts nothing but the shape of the summary. (A time that
    is simply unique says nothing: events with timestamps are unique too.)
    """
    times = column.distinct_count
    rows = column.non_null
    if profile.basis != "full" or not times or times < 3:
        return None
    for category in categories:
        values = category.distinct_count
        if values and rows == times * values:
            return category
    return None


class Line(BuiltinChartType):
    name = "line"
    label = "Line chart"
    description = "A count or an aggregate over time, optionally split into series"
    icon = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<polyline points="3,17 8,11 12,14 16,6 21,9"/></svg>'
    )
    js_file = "line.js"
    channels = [
        Channel(name="x", label="Time", roles=("temporal",), allows_time_unit=True),
        Channel(
            name="y",
            label="Value",
            roles=("measure",),
            aggregates=ALL_AGGREGATES,
            column_required=False,
        ),
        Channel(
            name="series",
            label="Split by",
            roles=("category", "boolean", "foreign_key"),
            required=False,
            allows_limit=True,
        ),
    ]
    max_rows = MAX_TIME_BUCKETS * (DEFAULT_SERIES_LIMIT + 1)
    annotation_axes = {"x": "time", "y": "number"}
    annotation_series = True

    def propose(self, profile) -> list:
        candidates = []
        measures = best_measures(profile, 2)
        categories = sorted(
            (
                (category_quality(column)[0], column)
                for column in category_columns(profile)
                if 2 <= (column.distinct_count or 0)
            ),
            key=lambda pair: (-pair[0], pair[1].position),
        )
        for column in profile.columns:
            if column.role != "temporal" or not column.temporal:
                continue
            # One row per entity per date, as in (package, date, downloads):
            # there the measure over time is the chart, not the row count
            per_category = per_time_category(
                profile, column, [c for _, c in categories]
            )
            panel = (
                len(profile.primary_keys) > 1 and column.name in profile.primary_keys
            ) or per_category is not None
            # Few rows spread over many buckets make a spiky line, so the
            # bucket target shrinks with the row count. A panel already has
            # a value for every time, so it keeps the finer buckets.
            target = TARGET_TIME_BUCKETS
            if not panel:
                target = min(target, max(12, column.non_null // ROWS_PER_BUCKET))
            unit, buckets = choose_time_unit(column.temporal, target)
            if unit is None or buckets < 3:
                continue
            base = 0.72
            if buckets >= 12:
                base += 0.06
            elif buckets < 6:
                base -= 0.1
            if buckets > column.non_null:
                # Even the coarsest unit leaves most buckets empty
                base -= 0.12
            tokens = set(name_tokens(column.name))
            if tokens & WEAK_TIME_NAME_TOKENS:
                base -= 0.06
            base -= null_penalty(column.null_rate, 0.4)
            signals = {"buckets": buckets, "unit": unit, "panel": panel}
            span = "{} spans {}; {} buckets give {} points".format(
                column.name,
                span_description(column.temporal.min, column.temporal.max),
                UNIT_ADJECTIVES[unit],
                f"{buckets:,}",
            )
            x = {"column": column.name, "timeUnit": unit}

            candidates.append(
                Candidate(
                    spec=self._spec(x, {"aggregate": "count"}),
                    score=round(base - (0.25 if panel else 0), 4),
                    reason=span,
                    signals=signals,
                )
            )
            for i, measure in enumerate(measures):
                y = {"column": measure.name, "aggregate": default_aggregate(measure)}
                score = base + 0.08 if panel else base - 0.1
                candidates.append(
                    Candidate(
                        spec=self._spec(x, y),
                        score=round(score - 0.06 * i, 4),
                        reason=f"{aggregate_title(y)} over time: {span}",
                        signals=signals,
                    )
                )
            y = {"aggregate": "count"}
            if panel and measures:
                y = {
                    "column": measures[0].name,
                    "aggregate": default_aggregate(measures[0]),
                }
            splits = categories[:2]
            if per_category and all(c is not per_category for _, c in splits):
                splits = [pair for pair in categories if pair[1] is per_category]
                splits += categories[:1]
            for i, (quality, category) in enumerate(splits):
                distinct = category.distinct_count
                series = {
                    "column": category.name,
                    "limit": DEFAULT_SERIES_LIMIT,
                    "other": True,
                }
                score = base - 0.1 - 0.05 * i - (0.04 if distinct > 8 else 0)
                score -= 0.3 * max(0.0, 0.62 - quality)
                if category is per_category and distinct <= DEFAULT_SERIES_LIMIT:
                    # One line per value, each with one point per time: the
                    # lines are the data, where one line would average them
                    score = base + 0.1
                if distinct <= DEFAULT_SERIES_LIMIT:
                    split = f"split by the {distinct} values of {category.name}"
                else:
                    split = "split by the top {} of {} {} values".format(
                        DEFAULT_SERIES_LIMIT, f"{distinct:,}", category.name
                    )
                candidates.append(
                    Candidate(
                        spec=self._spec(x, y, series),
                        score=round(score, 4),
                        reason=f"{aggregate_title(y)} {per_time_unit(unit, column.name)}, {split}",
                        signals={**signals, "series_quality": quality},
                    )
                )
        return candidates

    def _spec(self, x: dict, y: dict, series: dict | None = None) -> dict:
        encoding = {"x": x, "y": y}
        if series:
            encoding["series"] = series
        return {"v": 1, "type": self.name, "encoding": encoding, "options": {}}

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        x = encoding["x"]
        y = encoding.get("y") or {"aggregate": "count"}
        series = encoding.get("series")
        dimension = temporal_dimension(x, profile)
        where = f" where {dimension['filter']}" if dimension["filter"] else ""
        meta = {
            "timeUnit": dimension["unit"],
            "temporalKind": dimension["kind"],
            "x": x["column"],
            "y": aggregate_title(y),
        }
        value = aggregate_over(y)
        if not series:
            sql = (
                f"{source.with_clause()},\n"
                f"chart_base as (select {dimension['bucket']} as b, {measure_sql(y)} as v"
                f" from source{where})\n"
                "select bucket, value from (\n"
                f"  select b as bucket, {value} as value from chart_base group by 1\n"
                ") where bucket is not null and value is not null\n"
                f"order by 1 limit {MAX_TIME_BUCKETS + 1}"
            )
            return CompiledQuery(
                sql=sql,
                params=merge_params(source, {}),
                max_rows=MAX_TIME_BUCKETS,
                meta=meta,
            )

        limit = int(series.get("limit") or DEFAULT_SERIES_LIMIT)
        other = bool(series.get("other", True))
        keep = "" if other else "  where r.rn <= :chart_series_limit\n"
        label = label_sql(profile, series["column"], "t.series")
        label = f", {label} as series_label" if label else ""
        sql = (
            f"{source.with_clause()},\n"
            f"chart_base as {not_materialized()}(select {dimension['bucket']} as b,"
            f" {quote(series['column'])} as s, {measure_sql(y)} as v"
            f" from source{where}),\n"
            "chart_series_rank as (\n"
            f"  select s, row_number() over (order by {rank_over(y)} desc, s) as rn\n"
            "  from chart_base where b is not null group by 1\n"
            ")\n"
            f"select bucket, series, is_other, value{label} from (\n"
            "  select chart_base.b as bucket,\n"
            "    case when r.rn <= :chart_series_limit then r.s end as series,\n"
            "    max(r.rn > :chart_series_limit) as is_other,\n"
            f"    {value} as value,\n"
            "    min(r.rn, :chart_series_limit + 1) as position\n"
            "  from chart_base join chart_series_rank r on r.s is chart_base.s\n"
            f"{keep}"
            "  group by 1, min(r.rn, :chart_series_limit + 1)\n"
            ") t where bucket is not null and value is not null\n"
            "order by bucket, position\n"
            f"limit {MAX_TIME_BUCKETS * (limit + 1) + 1}"
        )
        meta["series"] = series["column"]
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, {"chart_series_limit": limit}),
            max_rows=MAX_TIME_BUCKETS * (limit + 1),
            meta=meta,
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        x = encoding["x"]
        title = aggregate_title(encoding.get("y"))
        if x.get("timeUnit"):
            title += " " + per_time_unit(x["timeUnit"], x["column"])
        else:
            title += " over {}".format(x["column"])
        if encoding.get("series"):
            title += " by {}".format(encoding["series"]["column"])
        return title
