"""
Tools for datasette-agent: the agent can ask what is worth charting, and draw
a chart in the chat.

Only imported when datasette-agent is installed. The tools speak the same
chart DSL as the Charts panel, so the model never writes SQL or JavaScript to
draw a chart, and a chart it draws opens in the panel's builder.
"""

from __future__ import annotations

import json

from datasette_agent.tools import AgentTool

from . import api, dsl, engine
from .sqlgen import SORTS, TIME_UNITS

# The chart is drawn from every row either way. This caps the copy of its
# rows that goes back to the model.
MODEL_ROWS = 100

SOURCE_PROPERTIES = {
    "database": {"type": "string", "description": "Database name"},
    "table": {
        "type": "string",
        "description": "The table or view to chart. Give this, sql or query_name.",
    },
    "filters": {
        "type": "object",
        "description": (
            "With table: only chart the rows matching these Datasette table "
            'filters, as query string keys and values - for example {"state": '
            '"CA", "price__gt": "5"}'
        ),
    },
    "sql": {
        "type": "string",
        "description": (
            "A read-only SQL query to chart, instead of a table. Return the "
            "rows to chart from, not an aggregate of them."
        ),
    },
    "query_name": {
        "type": "string",
        "description": "The name of a stored query to chart, instead of a table",
    },
    "params": {
        "type": "object",
        "description": "Values for the :named parameters in sql or the stored query",
    },
}

SUGGEST_DESCRIPTION = """Find out what is worth charting in a table, view, stored query or SQL query.

Returns the role of each column (measure, category, temporal and so on) and a ranked list of suggested charts. Each suggestion has a title, the reason it was suggested and a spec. Pass a spec to the chart tool, unchanged or adjusted, to draw it.

Call this before chart when you do not already know which columns suit which kind of chart."""

CHART_DESCRIPTION = """Draw a chart for the user from a table, view, stored query or SQL query. The chart appears in the chat as soon as this returns - do not describe how to make it, and do not repeat its data as a table.

You get back the rows the chart plots so you can comment on them, and chart_path: a link to the chart on its own page with a builder for changing it. To open that page next to the chat, pass chart_path to show_in_panel.

The chart does its own grouping, binning and aggregation in SQL over every row of the source, so chart a table directly where you can. Chart a SQL query when the rows need joining or filtering first.

spec describes the chart:

{"v": 1, "type": "<chart type>", "encoding": {"<channel>": {...}, ...}, "options": {...}}

Each channel is an object. Depending on the channel it takes:
- "column": a column of the source
- "aggregate": how to combine values. {"aggregate": "count"} with no column counts rows
- "timeUnit": the bucket for a date column - __TIME_UNITS__. Leave it out to have one chosen
- "bin": {"maxbins": n} for a numeric column. Leave it out to have bins chosen
- "limit" and "other": keep the top n values, and whether to group the rest as Other
- "sort": __SORTS__

Chart types and their channels:

__CHART_TYPES__

Example: {"v": 1, "type": "line", "encoding": {"x": {"column": "created_at", "timeUnit": "month"}, "y": {"column": "price", "aggregate": "sum"}, "series": {"column": "status", "limit": 5, "other": true}}}

If the spec does not fit the source this returns the errors, so you can correct it and call again. Call suggest_charts first if you do not know the columns."""


def _describe_channel(channel) -> str:
    parts = ["required" if channel.required else "optional"]
    if channel.roles:
        parts.append("a {} column".format(" or ".join(channel.roles)))
    if channel.aggregates:
        aggregates = "aggregate " + "|".join(channel.aggregates)
        if not channel.column_required:
            aggregates += ", or count with no column"
        parts.append(aggregates)
    for allowed, label in (
        (channel.allows_time_unit, "timeUnit"),
        (channel.binnable, "bin"),
        (channel.allows_limit, "limit, other"),
        (channel.allows_sort, "sort"),
    ):
        if allowed:
            parts.append(label)
    return "  - {}: {}".format(channel.name, "; ".join(parts))


def describe_chart_types(chart_types: dict) -> str:
    "The registered chart types, in words a model can write specs from"
    lines = []
    for chart_type in chart_types.values():
        lines.append(
            "- {}: {}".format(chart_type.name, " ".join(chart_type.description.split()))
        )
        lines.extend(_describe_channel(channel) for channel in chart_type.channels)
        for name, option in (chart_type.options or {}).items():
            lines.append(
                "  - option {}: {} (default {})".format(
                    name, "|".join(map(str, option["values"])), option["default"]
                )
            )
    return "\n".join(lines)


def chart_description(chart_types: dict) -> str:
    return (
        CHART_DESCRIPTION.replace("__TIME_UNITS__", ", ".join(TIME_UNITS))
        .replace("__SORTS__", ", ".join(SORTS))
        .replace("__CHART_TYPES__", describe_chart_types(chart_types))
    )


def _error(message, **extra) -> str:
    return json.dumps({"ok": False, "error": message, **extra})


def _descriptor(database, table, sql, query_name, filters, params) -> dict:
    "The source descriptor for a tool call's flat arguments"
    given = [
        name
        for name, value in (("table", table), ("sql", sql), ("query_name", query_name))
        if value
    ]
    if len(given) != 1:
        raise api.ChartsError("Give exactly one of table, sql or query_name")
    descriptor = {"database": database}
    if table:
        descriptor.update(kind="table", table=table)
        if filters:
            descriptor["filters"] = _filter_pairs(filters)
        return descriptor
    if filters:
        raise api.ChartsError("filters only apply to a table")
    if sql:
        descriptor.update(kind="query", sql=sql)
    else:
        descriptor.update(kind="stored_query", query_name=query_name)
    if params:
        descriptor["params"] = _params(params)
    return descriptor


def _filter_pairs(filters) -> list:
    "Filters as [key, value] pairs, from an object or from pairs"
    if isinstance(filters, dict):
        pairs = []
        for key, value in filters.items():
            # A key that repeats in a query string arrives as a list
            for item in value if isinstance(value, list) else [value]:
                pairs.append([key, item])
    else:
        pairs = filters
    cleaned = []
    for pair in pairs if isinstance(pairs, list) else [None]:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise api.ChartsError("filters must be an object of keys and values")
        key, value = pair
        if isinstance(value, bool):
            value = int(value)
        cleaned.append([str(key), "" if value is None else str(value)])
    return cleaned


def _params(params) -> dict:
    # sql_query takes parameters as [{"name":, "value":}], so models send that
    if isinstance(params, list):
        try:
            return {item["name"]: item.get("value") for item in params}
        except (TypeError, KeyError, AttributeError):
            raise api.ChartsError("params must be an object of names and values")
    return params


async def suggest_charts(
    datasette,
    actor,
    database=None,
    table=None,
    sql=None,
    query_name=None,
    filters=None,
    params=None,
):
    try:
        source = await api.resolve(
            datasette,
            actor,
            _descriptor(database, table, sql, query_name, filters, params),
        )
        result = await api.suggest(datasette, actor, source)
    except api.EXPECTED_ERRORS as e:
        return json.dumps(api.error_body(e))
    profile = result["profile"]
    output = {
        "ok": True,
        "row_count": profile["row_count"],
        "columns": result["columns"],
        "suggestions": [
            {
                "title": suggestion["title"],
                "reason": suggestion["reason"],
                "spec": suggestion["spec"],
            }
            for suggestion in result["suggestions"]
        ],
    }
    if profile["basis"] == "sample":
        output["note"] = (
            "Suggestions are based on a sample of {} rows; charts use every row"
        ).format(profile["rows_profiled"])
    if not output["suggestions"]:
        output["note"] = (
            "Nothing was suggested for this data. You can still write a spec "
            "for the chart tool from the columns."
        )
    return json.dumps(output)


async def chart(
    datasette,
    actor,
    database=None,
    spec=None,
    table=None,
    sql=None,
    query_name=None,
    filters=None,
    params=None,
):
    # Models sometimes send a nested object as a string of JSON
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            return _error("spec must be a JSON object")
    if isinstance(spec, dict):
        spec = {"v": dsl.DSL_VERSION, **spec}
    try:
        source = await api.resolve(
            datasette,
            actor,
            _descriptor(database, table, sql, query_name, filters, params),
        )
        data = await api.chart_data(datasette, actor, source, spec)
    except api.EXPECTED_ERRORS as e:
        return json.dumps(api.error_body(e))
    rows = data["rows"]
    output = {
        "ok": True,
        "title": data["title"],
        "columns": data["columns"],
        "rows": rows[:MODEL_ROWS],
        "chart_path": api.chart_path(datasette, source, spec),
        "message": "The chart is now showing in the chat",
        "_html": str(api.embed_html(datasette, source, spec)),
    }
    if len(rows) > MODEL_ROWS:
        output["rows_note"] = (
            "The chart plots {} rows. Only the first {} are included here."
        ).format(len(rows), MODEL_ROWS)
    if data["truncated"]:
        output["truncated"] = True
    if data.get("meta"):
        # What "leave it out to have one chosen" resolved to
        output["resolved"] = data["meta"]
    return json.dumps(output)


def get_agent_tools(datasette) -> list:
    source_schema = {
        "type": "object",
        "properties": SOURCE_PROPERTIES,
        "required": ["database"],
    }
    return [
        AgentTool(
            name="suggest_charts",
            description=SUGGEST_DESCRIPTION,
            input_schema=source_schema,
            fn=suggest_charts,
        ),
        AgentTool(
            name="chart",
            description=chart_description(engine.get_chart_types(datasette)),
            input_schema={
                "type": "object",
                "properties": {
                    **SOURCE_PROPERTIES,
                    "spec": {
                        "type": "object",
                        "description": "The chart to draw, in the format described above",
                    },
                },
                "required": ["database", "spec"],
            },
            fn=chart,
        ),
    ]
