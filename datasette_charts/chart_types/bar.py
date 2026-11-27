from __future__ import annotations

from ..sources import quote, table_ref
from ..sqlgen import (
    ADDITIVE_AGGREGATES,
    aggregate_over,
    aggregate_title,
    measure_sql,
    merge_params,
    plural,
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
    measure_quality,
    top_coverage,
)

DEFAULT_LIMIT = 20
# A measure is only proposed against a category with at most this many values
MAX_MEASURE_CATEGORY_VALUES = 500
ORDER_BY = {
    "-value": "value desc, key",
    "value": "value, key",
    "key": "key",
    "-key": "key desc",
}


def label_sql(profile, column: str, key_sql: str) -> str | None:
    "A subquery that looks up the label for a foreign key value"
    column_profile = profile.column(column) if profile else None
    fk = column_profile.foreign_key if column_profile else None
    if not fk or not fk.get("label_column"):
        return None
    return "(select {} from {} where {} = {})".format(
        quote(fk["label_column"]), table_ref(fk["table"]), quote(fk["column"]), key_sql
    )


class Bar(BuiltinChartType):
    name = "bar"
    label = "Bar chart"
    description = "A count or an aggregate for each value of a category"
    icon = (
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<rect x="3" y="4" width="16" height="3.5"/><rect x="3" y="10" width="11" height="3.5"/>'
        '<rect x="3" y="16" width="6" height="3.5"/></svg>'
    )
    js_file = "bar.js"
    channels = [
        Channel(
            name="x",
            label="Category",
            roles=("category", "boolean", "foreign_key"),
            allows_limit=True,
            allows_sort=True,
        ),
        Channel(
            name="y",
            label="Value",
            roles=("measure",),
            aggregates=ALL_AGGREGATES,
            column_required=False,
        ),
    ]
    max_rows = 101
    options = {
        "orientation": {
            "label": "Bars",
            "values": ["horizontal", "vertical"],
            "default": "horizontal",
        }
    }
    annotation_axes = {"x": "category", "y": "number"}

    def propose(self, profile) -> list:
        candidates = []
        for column in category_columns(profile):
            score, signals = category_quality(column)
            if not score:
                continue
            distinct = column.distinct_count
            n, share = top_coverage(column, 3)
            if distinct <= 3:
                reason = f"{column.name} has {plural(distinct, 'value')}"
            else:
                reason = "{} has {} values; the top {} cover {}% of rows".format(
                    column.name, f"{distinct:,}", n, round(share * 100)
                )
            if column.null_rate >= 0.2:
                reason += f" ({round(column.null_rate * 100)}% are null)"
            candidates.append(
                Candidate(
                    spec=self._spec(column, {"aggregate": "count"}),
                    score=score,
                    reason=reason,
                    signals=signals,
                )
            )

        # A measure for each value of the best categories. How evenly rows
        # are spread across a category does not matter for these.
        scored = []
        for column in category_columns(profile):
            if 2 <= (column.distinct_count or 0) <= MAX_MEASURE_CATEGORY_VALUES:
                scored.append((category_quality(column, for_counts=False)[0], column))
        scored.sort(key=lambda pair: (-pair[0], pair[1].position))
        for category_score, category in scored[:2]:
            for measure in best_measures(profile, 2):
                aggregate = default_aggregate(measure)
                y = {"column": measure.name, "aggregate": aggregate}
                score = (
                    0.03
                    + 0.5 * min(category_score, 0.7)
                    + 0.5 * measure_quality(measure)
                )
                distinct = category.distinct_count
                if distinct > DEFAULT_LIMIT:
                    which = "the top {} of {} {} values".format(
                        DEFAULT_LIMIT, f"{distinct:,}", category.name
                    )
                else:
                    which = "each of the {} values of {}".format(
                        distinct, category.name
                    )
                candidates.append(
                    Candidate(
                        spec=self._spec(category, y),
                        score=round(score, 4),
                        reason=f"{aggregate_title(y)} for {which}",
                        signals={
                            "category_score": category_score,
                            "measure_quality": measure_quality(measure),
                        },
                    )
                )
        return candidates

    def _spec(self, column, y: dict) -> dict:
        x = {"column": column.name, "limit": DEFAULT_LIMIT, "other": True}
        # Numbers and booleans have a natural order; other categories go by size
        if column.type_counts.get("text", 0) == 0:
            x["sort"] = "key"
        return {"v": 1, "type": self.name, "encoding": {"x": x, "y": y}, "options": {}}

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        x = encoding["x"]
        y = encoding.get("y") or {"aggregate": "count"}
        aggregate = y.get("aggregate") or "count"
        additive = aggregate in ADDITIVE_AGGREGATES
        limit = int(x.get("limit") or DEFAULT_LIMIT)
        other = bool(x.get("other", True)) and additive
        order = ORDER_BY[x.get("sort") or "-value"]
        key = quote(x["column"])

        # Ranked by value where values add up, otherwise by how common each is
        rank = "value" if additive else "n"
        ctes = (
            f"{source.with_clause()},\n"
            f"_chart_base as (select {key} as k, {measure_sql(y)} as v from _chart_source),\n"
            f"_chart_grouped as (\n  select k as key, {aggregate_over(y)} as value,"
            " count(*) as n\n  from _chart_base group by 1\n),\n"
            "_chart_ranked as (\n  select key, value,"
            f" row_number() over (order by {rank} desc, key) as rn\n"
            "  from _chart_grouped where value is not null\n)"
        )
        if other:
            # is_other is separate so a real NULL category stays distinguishable
            inner = (
                "select case when rn <= :chart_limit then key end as key,\n"
                "  max(rn > :chart_limit) as is_other, sum(value) as value\n"
                "from _chart_ranked group by min(rn, :chart_limit + 1)"
            )
        else:
            inner = (
                "select key, 0 as is_other, value from _chart_ranked"
                " where rn <= :chart_limit"
            )
        label = label_sql(profile, x["column"], "t.key")
        columns = "key, is_other, value" + (f", {label} as label" if label else "")
        sql = (
            f"{ctes}\nselect {columns} from (\n{inner}\n) t\norder by is_other, {order}"
        )
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, {"chart_limit": limit}),
            max_rows=limit + 1,
            meta={"x": x["column"], "y": aggregate_title(y), "other": other},
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        return "{} by {}".format(
            aggregate_title(encoding.get("y")), encoding["x"]["column"]
        )
