"""
calendar_heatmap: one cell per day, like a contributions calendar. Weeks are
columns and weekdays rows, with one block per year. The ``years`` option
counts back from the year of the latest date in the data, not from today, so
a table that stopped in 2008 still gets a full calendar.

Days are bucketed with the same time helpers as the line chart, so every
temporal kind that can be bucketed by day works. A ``year`` column cannot
make a calendar.
"""

from __future__ import annotations

from ..profiler.roles import name_tokens
from ..sources import SOURCE_CTE
from ..sqlgen import (
    aggregate_over,
    aggregate_title,
    measure_sql,
    merge_params,
    parse_iso,
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
    default_aggregate,
    null_penalty,
)
from .dimensions import temporal_dimension
from .line import WEAK_TIME_NAME_TOKENS, per_time_category

YEARS = ("1", "2", "3", "5", "10")
DEFAULT_YEARS = "5"
# A calendar is proposed for a column spanning at least this many days
MIN_SPAN_DAYS = 90
# Under this share of days with any rows, the calendar is mostly empty
MIN_FILL = 0.05
# Birthdays and the like: a calendar of the most recent years shows nothing
LIFE_DATE_NAME_TOKENS = {"birthday", "birth", "born", "dob", "death", "died"}
# Taken off each calendar after the best one: a second calendar of another
# date column is rarely worth a place of its own
LATER_CALENDAR_PENALTY = 0.15


def span_days(temporal) -> float | None:
    "Days from the earliest value to the latest"
    start, end = parse_iso(temporal.min), parse_iso(temporal.max)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 86400)


def years_needed(temporal) -> int:
    "Calendar years from the earliest value to the latest, inclusive"
    start, end = parse_iso(temporal.min), parse_iso(temporal.max)
    if start is None or end is None:
        return int(DEFAULT_YEARS)
    return end.year - start.year + 1


def fit_years(temporal) -> str:
    """
    The smallest ``years`` option that shows every year the column covers,
    or the largest option when it covers more than that
    """
    needed = years_needed(temporal)
    for option in YEARS:
        if int(option) >= needed:
            return option
    return YEARS[-1]


class CalendarHeatmap(BuiltinChartType):
    name = "calendar_heatmap"
    label = "Calendar heatmap"
    description = (
        "A count or an aggregate for each day, as a calendar with one block per year"
    )
    icon = (
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<rect x="3" y="4" width="4" height="4" opacity=".35"/>'
        '<rect x="8" y="4" width="4" height="4"/>'
        '<rect x="13" y="4" width="4" height="4" opacity=".6"/>'
        '<rect x="18" y="4" width="3" height="4" opacity=".35"/>'
        '<rect x="3" y="9" width="4" height="4" opacity=".6"/>'
        '<rect x="8" y="9" width="4" height="4" opacity=".35"/>'
        '<rect x="13" y="9" width="4" height="4"/>'
        '<rect x="18" y="9" width="3" height="4" opacity=".6"/>'
        '<rect x="3" y="14" width="4" height="4"/>'
        '<rect x="8" y="14" width="4" height="4" opacity=".6"/>'
        '<rect x="13" y="14" width="4" height="4" opacity=".35"/>'
        '<rect x="18" y="14" width="3" height="4"/></svg>'
    )
    js_file = "calendar_heatmap.js"
    channels = [
        Channel(name="x", label="Day", roles=("temporal",)),
        Channel(
            name="y",
            label="Value",
            roles=("measure",),
            aggregates=ALL_AGGREGATES,
            required=False,
            column_required=False,
        ),
    ]
    options = {
        "years": {
            "label": "Years",
            "values": list(YEARS),
            "default": DEFAULT_YEARS,
        },
    }
    max_rows = int(YEARS[-1]) * 366
    # Weeks wrap from one column to the next, so there is no straight time
    # axis to draw a line, a range or a callout on: free-floating notes only
    annotation_types = ("text",)

    def propose(self, profile) -> list:
        categories = [c for c in category_columns(profile) if c.distinct_count]
        measures = best_measures(profile, 1)
        found = []
        for column in profile.columns:
            temporal = column.temporal
            if column.role != "temporal" or temporal is None:
                continue
            if temporal.kind == "year":
                continue
            tokens = set(name_tokens(column.name))
            if tokens & LIFE_DATE_NAME_TOKENS:
                continue
            days = span_days(temporal)
            if days is None or days < MIN_SPAN_DAYS:
                continue
            rows = column.non_null
            if profile.row_count and profile.rows_profiled:
                # A sample's counts, scaled up to the whole source
                rows = round(rows * profile.row_count / profile.rows_profiled)
            # The share of days with any rows: the distinct days when the
            # profile knows them, otherwise rows per day as an upper bound
            distinct = column.distinct_count
            fill = min(1.0, (distinct if distinct else rows) / max(days, 1))
            if fill < MIN_FILL:
                # The same few dates over and over: a mostly empty calendar
                continue
            score = 0.64
            if fill >= 0.5:
                score += 0.06
            elif fill >= 0.15:
                score += 0.03
            if tokens & WEAK_TIME_NAME_TOKENS:
                score -= 0.06
            score -= null_penalty(column.null_rate, 0.4)
            y = {"aggregate": "count"}
            # One row per entity per day, as in (package, date, downloads):
            # every day has about as many rows, so count nothing and show
            # the measure instead
            panel = (
                len(profile.primary_keys) > 1 and column.name in profile.primary_keys
            ) or per_time_category(profile, column, categories) is not None
            if panel:
                if not measures:
                    continue
                y = {
                    "column": measures[0].name,
                    "aggregate": default_aggregate(measures[0]),
                }
                score -= 0.06
            years_option = fit_years(temporal)
            years = int(years_option)
            last = parse_iso(temporal.max).year
            first = max(parse_iso(temporal.min).year, last - years + 1)
            if first == last:
                which = f"one cell per day of {last}"
            elif years_needed(temporal) > years:
                which = (
                    f"one cell per day for the most recent {years} years,"
                    f" {first} to {last}"
                )
            else:
                which = f"one cell per day, {first} to {last}"
            reason = "{} spans {}; {}".format(
                column.name, span_description(temporal.min, temporal.max), which
            )
            if panel:
                reason = f"{aggregate_title(y)} for each day: {reason}"
            found.append(
                Candidate(
                    spec={
                        "v": 1,
                        "type": self.name,
                        "encoding": {"x": {"column": column.name}, "y": y},
                        "options": {"years": years_option},
                    },
                    score=score,
                    reason=reason,
                    signals={
                        "span_days": round(days),
                        "years": years,
                        "fill": round(fill, 3),
                        "panel": panel,
                    },
                )
            )
        # The best date column gets the calendar; others score less
        found.sort(key=lambda candidate: -candidate.score)
        for i, candidate in enumerate(found):
            candidate.score = round(
                candidate.score - (LATER_CALENDAR_PENALTY if i else 0), 4
            )
        return found

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        x = encoding["x"]
        y = encoding.get("y") or {"aggregate": "count"}
        dimension = temporal_dimension(
            {"column": x["column"], "timeUnit": "day"},
            profile,
            what="A calendar heatmap needs dates",
        )
        where = f" where {dimension['filter']}" if dimension["filter"] else ""
        years = int((spec.get("options") or {}).get("years") or DEFAULT_YEARS)
        sql = (
            f"{source.with_clause()},\n"
            f"_chart_base as (select {dimension['bucket']} as d,"
            f" {measure_sql(y)} as v from {SOURCE_CTE}{where}),\n"
            "_chart_days as (\n"
            f"  select d, {aggregate_over(y)} as value from _chart_base"
            " where d is not null group by 1\n"
            ")\n"
            "select d as day, value from _chart_days\n"
            "where value is not null and d >= (\n"
            "  select date(max(d), 'start of year', :chart_years_back)"
            " from _chart_days\n"
            ")\n"
            "order by d"
        )
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, {"chart_years_back": f"-{years - 1} years"}),
            max_rows=years * 366,
            meta={
                "x": x["column"],
                "y": aggregate_title(y),
                "temporalKind": dimension["kind"],
                "years": years,
            },
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        what = aggregate_title(encoding.get("y"))
        return "{} per day of {}".format(what, encoding["x"]["column"])
