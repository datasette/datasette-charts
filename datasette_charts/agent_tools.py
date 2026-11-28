"""
Tools for datasette-agent: the agent can ask what is worth charting, and draw
a chart in the chat.

Only imported when datasette-agent is installed. The tools speak the same
chart DSL as the Charts panel, so the model never writes SQL or JavaScript to
draw a chart, and a chart it draws opens in the panel's builder.
"""

from __future__ import annotations

import json
import urllib.parse

from datasette.utils import MultiParams, tilde_decode
from datasette_agent.tools import AgentTool

from . import annotations as annotations_module
from . import api, dsl, engine
from .filters import filters_from_args
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

Annotations mark events, thresholds and periods on a chart. Add them as "annotations": [...] in the spec. Each has a type:
- {"type": "line", "x": <x value>, "text": "..."}: a dashed line across the chart at an x value. "position" places it on a category or time period: "before" or "after" a category puts the line between it and its neighbour (default "center"); on a time axis it is the start (default), "center" or end of the period the value names, so "x": 2009, "position": "after" is the end of 2009.
- {"type": "line", "y": <number>, "text": "Target"}: a line at a y value, such as a threshold.
- {"type": "range", "x": <from>, "x2": <to>, "text": "Recession"}, or "y" and "y2": a shaded band. Categories and periods at either end are covered whole.
- {"type": "point", "x": <x value>, "text": "..."}: a callout on the data point at x, with a dot and a line to the text. Leave out "y" to use the chart's own value there; give "series" to pick a series. Scatter charts need "y". Labels are laid out for you: beside what they label when there is room, otherwise in order in a band above the chart, otherwise as numbered markers with the texts listed below. Leave out "anchor" unless a label must stay in one place.
- {"type": "text", "left": 0.02, "top": 0.05, "text": "..."}: a note placed by fractions of the plot area, from its left and top edges.
x and y mean the spec's own x and y channels: on a horizontal bar chart x is the category axis down the side. Write x values the way the chart shows them: a category value, a year (2009), "2009-06" or "2009-06-15" for times, or a number.
Keep each text to a few words and use at most about 3 annotations: put explanations and caveats in your reply, not in a text note.
Optional: "anchor" says where the text sits from what it labels (top-left, top, top-right, left, center, right, bottom-left, bottom, bottom-right); "color" is blue, orange, green, yellow, pink, red, purple, gray or a hex color; lines take "stroke": dashed (default), solid or dotted; points take "marker": false for text with no dot. Text wraps by itself, up to 200 characters, and "\n" starts a new line. At most 20.
Chart types and the annotations they take: __ANNOTATIONS__

Example, a vertical bar chart by year with a line between 2009 and 2010: {"v": 1, "type": "bar", "encoding": {"x": {"column": "year", "sort": "key", "limit": 50, "other": false}, "y": {"column": "delays", "aggregate": "sum"}}, "options": {"orientation": "vertical"}, "annotations": [{"type": "line", "x": 2009, "position": "after", "text": "Tarmac delay rule enacted"}]}

To add, change or remove annotations on a chart that has been drawn, use annotate_chart.

If the spec does not fit the source this returns the errors, so you can correct it and call again. Call suggest_charts first if you do not know the columns."""

ANNOTATE_DESCRIPTION = """Add, change or remove annotations on a chart that has already been drawn, and draw it again in the chat. Annotations are described in the chart tool.

chart_path is the chart_path from an earlier chart or annotate_chart result, or the address of a chart page the user has open (one with #chart= in it). Then give any of:
- add: annotations to add at the end
- update: changes to existing annotations, as [{"index": 0, "text": "New text"}]. Only the fields given change, and a field set to null is removed
- remove: the indexes of annotations to remove, such as [1]
- annotations: a whole new list, replacing every annotation. [] removes them all

Indexes count from 0 in the list as it was before this call. Returns what chart returns, with annotations: the chart's annotations now, in order, and a new chart_path."""


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
        .replace("__ANNOTATIONS__", annotations_module.describe(chart_types))
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


def _load_spec(spec):
    # Models sometimes send a nested object as a string of JSON
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            raise api.ChartsError("spec must be a JSON object")
    if isinstance(spec, dict):
        spec = {"v": dsl.DSL_VERSION, **spec}
    return spec


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
    try:
        spec = _load_spec(spec)
        source = await api.resolve(
            datasette,
            actor,
            _descriptor(database, table, sql, query_name, filters, params),
        )
        return json.dumps(await _draw(datasette, actor, source, spec))
    except api.EXPECTED_ERRORS as e:
        return json.dumps(api.error_body(e))


async def _draw(datasette, actor, source, spec) -> dict:
    "The chart tool's output for a resolved source and a spec"
    data = await api.chart_data(datasette, actor, source, spec)
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
    warnings = annotation_warnings(spec, data)
    if warnings:
        output["annotation_warnings"] = warnings
    return output


def _shown(value) -> str:
    "A value as a chart's axis shows it: 2009.0 is 2009, as in JavaScript"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def annotation_warnings(spec: dict, data: dict) -> list:
    """
    Annotations that name a category the chart does not show are valid, but
    are not drawn. Say so, with the values that are there, so the model can
    put them right.
    """
    annotations = spec.get("annotations") or []
    columns = data["columns"]
    if not annotations or (data.get("meta") or {}).get("timeUnit"):
        return []
    if spec.get("type") in ("bar", "box_plot"):
        key, label, is_other = "key", "label", "is_other"
    elif spec.get("type") == "stacked_bar":
        key, label, is_other = "key", "key_label", "key_is_other"
    else:
        return []
    shown = []
    names = set()
    for row in data["rows"]:
        record = dict(zip(columns, row))
        if record.get(is_other):
            continue
        for value in (record.get(key), record.get(label)):
            if value is not None:
                names.add(_shown(value))
        name = record.get(label) if record.get(label) is not None else record.get(key)
        shown.append("(null)" if name is None else _shown(name))
    warnings = []
    for index, annotation in enumerate(annotations):
        for field in ("x", "x2"):
            if field in annotation and _shown(annotation[field]) not in names:
                values = ", ".join(shown[:30]) + (", ..." if len(shown) > 30 else "")
                warnings.append(
                    f"annotations[{index}]: {field} {annotation[field]!r} is not one "
                    f"of the chart's x values, so it is not drawn. They are: {values}"
                )
    return warnings


async def _parse_chart_path(datasette, chart_path) -> tuple:
    "The source descriptor and spec in a link to a chart"
    if not isinstance(chart_path, str) or api.FRAGMENT not in chart_path:
        raise api.ChartsError(
            "chart_path must be a link to a chart, with #chart= in it, as chart returns"
        )
    address, _, fragment = chart_path.strip().partition(api.FRAGMENT)
    try:
        spec = json.loads(urllib.parse.unquote(fragment))
    except ValueError:
        raise api.ChartsError("The chart in chart_path is not valid JSON")
    parts = urllib.parse.urlsplit(address)
    path = parts.path
    base_url = datasette.setting("base_url") or "/"
    if base_url != "/" and path.startswith(base_url):
        path = "/" + path[len(base_url) :]
    segments = [segment for segment in path.split("/") if segment]
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    args = MultiParams(urllib.parse.parse_qs(parts.query, keep_blank_values=True))
    if not segments:
        raise api.ChartsError("chart_path does not name a database")
    try:
        db = datasette.get_database(
            route=tilde_decode(urllib.parse.unquote(segments[0]))
        )
    except KeyError:
        raise api.ChartsError("Database not found", 404)
    descriptor = {"database": db.name}
    rest = segments[1:]
    if rest in ([], ["-", "query"]) and args.get("sql"):
        params = {key: value for key, value in pairs if key != "sql"}
        descriptor.update(kind="query", sql=args.get("sql"), params=params)
    elif len(rest) == 1:
        name = tilde_decode(urllib.parse.unquote(rest[0]))
        if await datasette.get_query(db.name, name) is not None:
            descriptor.update(kind="stored_query", query_name=name, params=dict(pairs))
        else:
            filters = [list(pair) for pair in filters_from_args(args)]
            descriptor.update(kind="table", table=name)
            if filters:
                descriptor["filters"] = filters
    else:
        raise api.ChartsError("chart_path is not a link to a chart of a table or query")
    return descriptor, spec


def _annotation_list(value, name) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise api.ChartsError(f"{name} must be a list")
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise api.ChartsError(f"{name} must be a list")
    return value


def apply_annotation_changes(
    current, add=None, update=None, remove=None, annotations=None
):
    "The annotations after an annotate_chart call's changes"
    if annotations is not None:
        result = _annotation_list(annotations, "annotations")
        if add or update or remove:
            raise api.ChartsError(
                "Give annotations to replace them all, or add, update and remove - not both"
            )
        return result
    result = [dict(item) if isinstance(item, dict) else item for item in current]
    count = len(result)

    def check_index(index, name):
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < count
        ):
            raise api.ChartsError(
                f"{name}: {index!r} is not the index of an annotation. "
                + (
                    f"This chart has {count}, numbered 0 to {count - 1}"
                    if count
                    else "This chart has none"
                )
            )
        return index

    for change in _annotation_list(update, "update") if update else []:
        if not isinstance(change, dict) or "index" not in change:
            raise api.ChartsError(
                'Each update needs an index, as {"index": 0, "text": "..."}'
            )
        index = check_index(change["index"], "update")
        for key, value in change.items():
            if key == "index":
                continue
            if value is None:
                result[index].pop(key, None)
            else:
                result[index][key] = value
    if remove:
        if not isinstance(remove, list):
            remove = [remove]
        indexes = {check_index(index, "remove") for index in remove}
        result = [item for i, item in enumerate(result) if i not in indexes]
    for item in _annotation_list(add, "add") if add else []:
        result.append(item)
    return result


async def annotate_chart(
    datasette,
    actor,
    chart_path=None,
    add=None,
    update=None,
    remove=None,
    annotations=None,
):
    try:
        descriptor, spec = await _parse_chart_path(datasette, chart_path)
        spec = _load_spec(spec)
        if not isinstance(spec, dict):
            raise api.ChartsError("The chart in chart_path is not a spec")
        if add is None and update is None and remove is None and annotations is None:
            raise api.ChartsError("Give add, update, remove or annotations")
        current = spec.get("annotations") or []
        if not isinstance(current, list):
            current = []
        changed = apply_annotation_changes(current, add, update, remove, annotations)
        spec = {key: value for key, value in spec.items() if key != "annotations"}
        if changed:
            spec["annotations"] = changed
        source = await api.resolve(datasette, actor, descriptor)
        output = await _draw(datasette, actor, source, spec)
    except api.EXPECTED_ERRORS as e:
        return json.dumps(api.error_body(e))
    output["annotations"] = [
        {"index": index, **annotation} for index, annotation in enumerate(changed)
    ]
    return json.dumps(output)


ANNOTATIONS_SCHEMA = {
    "type": "array",
    "items": {"type": "object"},
}


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
                        "description": (
                            "The chart to draw, in the format described above, "
                            "with any annotations"
                        ),
                    },
                },
                "required": ["database", "spec"],
            },
            fn=chart,
        ),
        AgentTool(
            name="annotate_chart",
            description=ANNOTATE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "chart_path": {
                        "type": "string",
                        "description": "A link to the chart, with #chart= in it",
                    },
                    "add": {
                        **ANNOTATIONS_SCHEMA,
                        "description": "Annotations to add",
                    },
                    "update": {
                        **ANNOTATIONS_SCHEMA,
                        "description": 'Changes, as [{"index": 0, "text": "..."}]',
                    },
                    "remove": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Indexes of annotations to remove",
                    },
                    "annotations": {
                        **ANNOTATIONS_SCHEMA,
                        "description": "A new list that replaces every annotation",
                    },
                },
                "required": ["chart_path"],
            },
            fn=annotate_chart,
        ),
    ]
