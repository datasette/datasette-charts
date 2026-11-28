"""
box: how a measure is distributed within each of the most common values of
a category, as min, quartiles, median and max.

SQLite has no percentile function, so the quartiles come from window
functions: each value's position within its group, and the group's size.
A quartile at a fractional position is interpolated between its two
neighbours, the same as numpy's default ("linear") and the spreadsheet
QUARTILE.INC function.
"""

from __future__ import annotations

from ..profiler.roles import name_tokens
from ..sources import SOURCE_CTE, quote
from ..sqlgen import SpecError, merge_params, numeric_filter, wants_log_scale
from .bar import label_sql
from .base import (
    MEASURE_NAME_TOKENS,
    BuiltinChartType,
    Candidate,
    Channel,
    CompiledQuery,
    best_measures,
    category_columns,
    category_quality,
    check_limit,
    measure_quality,
)

DEFAULT_LIMIT = 12
MAX_LIMIT = 50
# Each box should summarise at least this many values, on average
MIN_ROWS_PER_BOX = 8
# A category is only proposed with at most this many values
MAX_CATEGORY_VALUES = 500
ORDER_BY = {
    "median": "is_other, median desc, key",
    "count": "is_other, count desc, key",
    "name": "is_other, key",
}


def quantile_sql(q: float) -> str:
    """
    The q quantile of v within a group whose rows are numbered i = 1..n in
    order of v. Position (n - 1) * q + 1, interpolated between neighbours.
    """
    pos = f"(n - 1) * {q}"
    lo = f"cast({pos} as integer) + 1"
    at_lo = f"max(case when i = {lo} then v end)"
    at_hi = f"max(case when i = {lo} + 1 then v end)"
    frac = f"max({pos} - cast({pos} as integer))"
    return f"{at_lo} + {frac} * (coalesce({at_hi}, {at_lo}) - {at_lo})"


class Box(BuiltinChartType):
    name = "box"
    label = "Box plot"
    description = (
        "How a numeric column is distributed within each value of a category:"
        " min, quartiles, median and max"
    )
    icon = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"'
        ' aria-hidden="true"><path d="M3 6h3M15 6h6M3 18h5M17 18h4M3 12h6M14 12h3"/>'
        '<rect x="6" y="4" width="9" height="4"/><rect x="8" y="16" width="9" height="4"/>'
        '<rect x="9" y="10" width="5" height="4"/></svg>'
    )
    js_file = "box.js"
    channels = [
        Channel(
            name="x",
            label="Category",
            roles=("category", "boolean", "foreign_key"),
            allows_limit=True,
        ),
        Channel(name="y", label="Value", roles=("measure",)),
    ]
    options = {
        "order": {
            "label": "Order",
            "values": ["median", "count", "name"],
            "default": "median",
        },
        "scale": {"label": "Scale", "values": ["linear", "log"], "default": "linear"},
    }
    max_rows = MAX_LIMIT + 1
    # The boxes lie flat like the bar chart's: x is the category, down the
    # side. A callout points at a box's median.
    annotation_axes = {"x": "category", "y": "number"}

    def validate(self, spec, columns) -> list:
        errors = super().validate(spec, columns)
        check_limit(errors, spec, "x", MAX_LIMIT)
        return errors

    def propose(self, profile) -> list:
        measures = best_measures(profile, 2)
        if not measures:
            return []
        scored = []
        for column in category_columns(profile):
            distinct = column.distinct_count or 0
            if distinct < 2 or distinct > MAX_CATEGORY_VALUES:
                continue
            boxes = min(distinct, DEFAULT_LIMIT)
            # Too few rows per value and the boxes are mostly whiskers
            if column.non_null / boxes < MIN_ROWS_PER_BOX:
                continue
            scored.append((category_quality(column, for_counts=False)[0], column))
        scored.sort(key=lambda pair: (-pair[0], pair[1].position))
        candidates = []
        for category_score, category in scored[:2]:
            for i, measure in enumerate(measures):
                numeric = measure.numeric or {}
                quantiles = numeric.get("quantiles") or {}
                # Scored a little under a bar chart of the same pair: a box
                # plot says more, but takes more reading
                score = (
                    0.5 * min(category_score, 0.7)
                    + 0.5 * measure_quality(measure)
                    - 0.06
                    - 0.04 * i
                )
                spread = quantiles.get("p75", 0) > quantiles.get("p25", 0)
                if spread:
                    score += 0.03
                named = bool(
                    MEASURE_NAME_TOKENS.intersection(name_tokens(measure.name))
                )
                if not named:
                    # A number with no name to say it is a quantity, such as
                    # a district number, is rarely worth a box plot
                    score -= 0.05
                options = {"order": "median"}
                log = (
                    wants_log_scale(numeric)
                    and numeric.get("min") is not None
                    and numeric["min"] > 0
                )
                if log:
                    options["scale"] = "log"
                distinct = category.distinct_count
                if distinct > DEFAULT_LIMIT:
                    which = "the {} most common of {} {} values".format(
                        DEFAULT_LIMIT, f"{distinct:,}", category.name
                    )
                else:
                    which = f"each of the {distinct} values of {category.name}"
                reason = f"How {measure.name} is spread within {which}"
                if log:
                    reason += ", on a log scale"
                candidates.append(
                    Candidate(
                        spec={
                            "v": 1,
                            "type": self.name,
                            "encoding": {
                                "x": {
                                    "column": category.name,
                                    "limit": DEFAULT_LIMIT,
                                    "other": False,
                                },
                                "y": {"column": measure.name},
                            },
                            "options": options,
                        },
                        score=round(score, 4),
                        reason=reason,
                        signals={
                            "category_score": category_score,
                            "measure_quality": measure_quality(measure),
                            "spread": spread,
                            "name_hint": named,
                            "log_scale": bool(log),
                        },
                    )
                )
        return candidates

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        x = encoding["x"]
        y = encoding["y"]
        options = spec.get("options") or {}
        limit = int(x.get("limit") or DEFAULT_LIMIT)
        other = bool(x.get("other", False))
        order = options.get("order") or "median"
        scale = options.get("scale") or "linear"
        if scale == "log":
            column = profile.column(y["column"]) if profile else None
            numeric = (column.numeric if column else None) or {}
            if numeric.get("min") is not None and numeric["min"] <= 0:
                raise SpecError(
                    f"{y['column']} cannot use a log scale: it has values of zero or less"
                )
        keep = "" if other else "  where g.rn <= :chart_limit\n"
        label = label_sql(profile, x["column"], "t.key")
        label = f", {label} as label" if label else ""
        sql = (
            f"{source.with_clause()},\n"
            f"_chart_base as (select {quote(x['column'])} as k, {quote(y['column'])} as v"
            f" from {SOURCE_CTE} where {numeric_filter(y['column'])}),\n"
            "_chart_groups as (\n"
            "  select k, row_number() over (order by count(*) desc, k) as rn\n"
            "  from _chart_base group by 1\n"
            "),\n"
            "_chart_tagged as (\n"
            "  select min(g.rn, :chart_limit + 1) as position,\n"
            "    case when g.rn <= :chart_limit then g.k end as key,\n"
            "    g.rn > :chart_limit as is_other, b.v as v\n"
            "  from _chart_base b join _chart_groups g on g.k is b.k\n"
            f"{keep}"
            "),\n"
            "_chart_ordered as (\n"
            "  select position, key, is_other, v,\n"
            "    row_number() over (partition by position order by v) as i,\n"
            "    count(*) over (partition by position) as n\n"
            "  from _chart_tagged\n"
            ")\n"
            f"select key, is_other, count, min, q1, median, q3, max, mean{label} from (\n"
            "  select max(key) as key, max(is_other) as is_other, max(n) as count,\n"
            "    min(v) as min,\n"
            f"    {quantile_sql(0.25)} as q1,\n"
            f"    {quantile_sql(0.5)} as median,\n"
            f"    {quantile_sql(0.75)} as q3,\n"
            "    max(v) as max, avg(v) as mean\n"
            "  from _chart_ordered group by position\n"
            ") t\n"
            f"order by {ORDER_BY[order]}"
        )
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, {"chart_limit": limit}),
            max_rows=limit + 1,
            meta={
                "x": x["column"],
                "y": y["column"],
                "scale": scale,
                "order": order,
                "other": other,
            },
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        return "Distribution of {} by {}".format(
            encoding["y"]["column"], encoding["x"]["column"]
        )
