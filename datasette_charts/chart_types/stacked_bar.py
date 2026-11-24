from __future__ import annotations

from ..sources import quote
from ..sqlgen import (
    MAX_TIME_BUCKETS,
    aggregate_over,
    aggregate_title,
    measure_sql,
    merge_params,
    not_materialized,
    per_time_unit,
    rank_over,
)
from .bar import label_sql
from .base import (
    ALL_AGGREGATES,
    BuiltinChartType,
    Candidate,
    Channel,
    CompiledQuery,
    category_columns,
    category_quality,
)
from .dimensions import is_temporal, temporal_dimension

DEFAULT_X_LIMIT = 12
DEFAULT_SERIES_LIMIT = 6


class StackedBar(BuiltinChartType):
    name = "stacked_bar"
    label = "Stacked bar chart"
    description = "A count or an aggregate for each pair of values of two categories"
    icon = (
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<rect x="4" y="12" width="4" height="9"/><rect x="4" y="7" width="4" height="4" opacity=".5"/>'
        '<rect x="10" y="9" width="4" height="12"/><rect x="10" y="3" width="4" height="5" opacity=".5"/>'
        '<rect x="16" y="15" width="4" height="6"/><rect x="16" y="11" width="4" height="3" opacity=".5"/></svg>'
    )
    js_file = "stacked_bar.js"
    channels = [
        Channel(
            name="x",
            label="Category",
            roles=("category", "boolean", "foreign_key", "temporal"),
            allows_limit=True,
            allows_time_unit=True,
        ),
        Channel(
            name="series",
            label="Stack by",
            roles=("category", "boolean", "foreign_key"),
            allows_limit=True,
        ),
        Channel(
            name="y",
            label="Value",
            roles=("measure",),
            aggregates=ALL_AGGREGATES,
            column_required=False,
        ),
    ]
    options = {
        "layout": {
            "label": "Layout",
            "values": ["stacked", "grouped"],
            "default": "stacked",
        }
    }
    max_rows = 1001

    def propose(self, profile) -> list:
        scored = []
        for column in category_columns(profile):
            quality, _ = category_quality(column)
            # Two-way charts need both categories to carry some information
            if quality >= 0.45 and 2 <= column.distinct_count <= 60:
                scored.append((quality, column))
        scored.sort(key=lambda pair: (-pair[0], pair[1].position))
        scored = scored[:4]
        pairs = []
        for i, (quality_a, a) in enumerate(scored):
            for quality_b, b in scored[i + 1 :]:
                # The one with fewer values makes the more readable stack
                x, series = (a, b) if a.distinct_count >= b.distinct_count else (b, a)
                if series.distinct_count > 30:
                    continue
                score = 0.5 * (quality_a + quality_b) - 0.16
                pairs.append((round(score, 4), x, series))
        pairs.sort(key=lambda t: (-t[0], t[1].position, t[2].position))
        candidates = []
        for score, x, series in pairs[:2]:
            candidates.append(
                Candidate(
                    spec={
                        "v": 1,
                        "type": self.name,
                        "encoding": {
                            "x": {
                                "column": x.name,
                                "limit": DEFAULT_X_LIMIT,
                                "other": True,
                            },
                            "series": {
                                "column": series.name,
                                "limit": DEFAULT_SERIES_LIMIT,
                                "other": True,
                            },
                            "y": {"aggregate": "count"},
                        },
                        "options": {"layout": "stacked"},
                    },
                    score=score,
                    reason="How the {} values of {} break down across {}".format(
                        f"{series.distinct_count:,}", series.name, x.name
                    ),
                    signals={
                        "x_distinct": x.distinct_count,
                        "series_distinct": series.distinct_count,
                    },
                )
            )
        return candidates

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        x = encoding["x"]
        series = encoding["series"]
        y = encoding.get("y") or {"aggregate": "count"}
        series_limit = int(series.get("limit") or DEFAULT_SERIES_LIMIT)
        series_other = bool(series.get("other", True))
        params = {"chart_series_limit": series_limit}
        meta = {
            "x": x["column"],
            "series": series["column"],
            "y": aggregate_title(y),
            "layout": (spec.get("options") or {}).get("layout", "stacked"),
        }

        temporal = is_temporal(x, profile)
        where = ""
        if temporal:
            dimension = temporal_dimension(x, profile)
            key = dimension["bucket"]
            if dimension["filter"]:
                where = f" where {dimension['filter']}"
            meta["timeUnit"] = dimension["unit"]
            # Time buckets are all kept, in time order
            x_rank = "select k, 0 as rn from base where k is not null group by 1"
            x_keep = 1
            x_position = "base.k"
            x_bound = MAX_TIME_BUCKETS
            x_other = False
        else:
            key = quote(x["column"])
            x_limit = int(x.get("limit") or DEFAULT_X_LIMIT)
            x_other = bool(x.get("other", True))
            params["chart_x_limit"] = x_limit
            x_rank = (
                f"select k, row_number() over (order by {rank_over(y)} desc, k) as rn"
                " from base group by 1"
            )
            x_keep = ":chart_x_limit"
            x_position = "min(xr.rn, :chart_x_limit + 1)"
            x_bound = x_limit + 1

        keep = []
        if not temporal and not x_other:
            keep.append("xr.rn <= :chart_x_limit")
        if not series_other:
            keep.append("sr.rn <= :chart_series_limit")
        keep = "  where {}\n".format(" and ".join(keep)) if keep else ""

        labels = ""
        if not temporal:
            key_label = label_sql(profile, x["column"], "t.key")
            labels += f", {key_label} as key_label" if key_label else ""
        series_label = label_sql(profile, series["column"], "t.series")
        labels += f", {series_label} as series_label" if series_label else ""

        max_rows = x_bound * (series_limit + 1)
        sql = (
            f"{source.with_clause()},\n"
            f"base as {not_materialized()}(select {key} as k,"
            f" {quote(series['column'])} as s, {measure_sql(y)} as v"
            f" from source{where}),\n"
            f"x_rank as ({x_rank}),\n"
            "series_rank as (\n"
            f"  select s, row_number() over (order by {rank_over(y)} desc, s) as rn\n"
            "  from base group by 1\n"
            ")\n"
            f"select key, key_is_other, series, series_is_other, value{labels} from (\n"
            f"  select case when xr.rn <= {x_keep} then xr.k end as key,\n"
            f"    max(xr.rn > {x_keep}) as key_is_other,\n"
            "    case when sr.rn <= :chart_series_limit then sr.s end as series,\n"
            "    max(sr.rn > :chart_series_limit) as series_is_other,\n"
            f"    {aggregate_over(y, 'base.v')} as value,\n"
            f"    {x_position} as x_position,\n"
            "    min(sr.rn, :chart_series_limit + 1) as series_position\n"
            "  from base\n"
            "  join x_rank xr on xr.k is base.k\n"
            "  join series_rank sr on sr.s is base.s\n"
            f"{keep}"
            "  group by x_position, series_position\n"
            ") t where value is not null\n"
            "order by x_position, series_position\n"
            f"limit {max_rows + 1}"
        )
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, params),
            max_rows=max_rows,
            meta=meta,
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        x = encoding["x"]
        if x.get("timeUnit"):
            return "{} {} by {}".format(
                aggregate_title(encoding.get("y")),
                per_time_unit(x["timeUnit"], x["column"]),
                encoding["series"]["column"],
            )
        return "{} by {} and {}".format(
            aggregate_title(encoding.get("y")),
            x["column"],
            encoding["series"]["column"],
        )
