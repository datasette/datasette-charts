from __future__ import annotations

import json

from datasette import hookimpl
from markupsafe import Markup, escape

NEEDS_NEWER_DATASETTE = (
    "datasette-charts needs a Datasette newer than 1.0a43 (commit 56e02c1 or"
    " later). Until that is released, run it against Datasette's main branch:"
    " see the datasette-charts README."
)

try:
    from datasette.plugins import pm
    from datasette.resources import DatabaseResource
    from datasette.utils import named_parameters
except ImportError as e:  # a much older Datasette
    raise ImportError(NEEDS_NEWER_DATASETTE) from e

from . import hookspecs  # noqa: E402

pm.add_hookspecs(hookspecs)

from . import views  # noqa: E402
from .chart_types import (  # noqa: E402
    BUILTIN_CHART_TYPES,
    Candidate,
    Channel,
    ChartType,
    CompiledQuery,
)
from .config import PLUGIN_NAME, get_config  # noqa: E402
from .filters import filters_from_args  # noqa: E402
from .sources import SOURCE_CTE, Source  # noqa: E402

__all__ = [
    "SOURCE_CTE",
    "Candidate",
    "Channel",
    "ChartType",
    "CompiledQuery",
    "Source",
]

PANEL_ID = "datasette-charts"
CHART_VIEWS = ("table", "query")


@hookimpl
def startup(datasette):
    # Fail at startup with a clear message, not on the first chart request
    if not hasattr(datasette, "capped_time_limit_ms"):
        raise RuntimeError(NEEDS_NEWER_DATASETTE)


@hookimpl
def register_chart_types(datasette):
    # The built-in chart types use the same hook third parties get
    return BUILTIN_CHART_TYPES


@hookimpl
def register_routes():
    return views.ROUTES


@hookimpl
def register_agent_tools(datasette):
    # A datasette-agent hook, so this is only called when that is installed
    from .agent_tools import get_agent_tools

    return get_agent_tools(datasette)


@hookimpl
def register_commands(cli):
    from .cli import register

    register(cli)


@hookimpl
def extra_js_urls(view_name, datasette):
    if view_name in CHART_VIEWS:
        return [
            {"url": datasette.static("charts.js", plugin=PLUGIN_NAME), "module": True}
        ]
    return []


@hookimpl
def extra_css_urls(view_name, datasette):
    if view_name in CHART_VIEWS:
        return [datasette.static("charts.css", plugin=PLUGIN_NAME)]
    return []


def _panel(datasette, database: str, source: dict, suggest_url: str, **extra) -> Markup:
    """
    A container element and a small JSON bootstrap. The plugin's JavaScript
    takes over from there, so nothing depends on core's undocumented globals.
    """
    config = get_config(datasette)
    database_url = str(datasette.urls.database(database))

    def static(path):
        return str(datasette.static(path, plugin=PLUGIN_NAME))

    bootstrap = {
        "source": source,
        "urls": {
            "types": str(datasette.urls.path("/-/charts/types.json")),
            "suggest": suggest_url,
            "data": f"{database_url}/-/charts/data.json",
            "profile": f"{database_url}/-/charts/profile.json",
            "d3": static("vendor/d3.min.js"),
            "plot": static("vendor/plot.umd.min.js"),
            "topojson": static("vendor/topojson-client.min.js"),
            "land": static("vendor/land-110m.json"),
        },
        "thumbnail_concurrency": config["thumbnail_concurrency"],
        "auto_suggest": bool(config["auto_suggest"]),
        **extra,
    }
    return Markup(
        '<div class="datasette-charts" id="{}" data-bootstrap="{}"></div>'.format(
            PANEL_ID, escape(json.dumps(bootstrap))
        )
    )


@hookimpl
def top_table(datasette, request, database, table):
    source = {"database": database, "kind": "table", "table": table}
    # Charts cover the rows the page shows, so they get the page's filters
    filters = filters_from_args(request.args)
    if filters:
        source["filters"] = [list(pair) for pair in filters]
    return _panel(
        datasette,
        database,
        source,
        "{}/-/charts/suggest.json".format(datasette.urls.table(database, table)),
    )


def _is_chart_sql(sql: str) -> bool:
    """
    The SQL behind "This chart's SQL" uses chart_ parameters.
    Charting it again would clash with the parameters the new chart adds,
    so pages showing it get no charts.
    """
    return any(name.startswith("chart_") for name in named_parameters(sql))


@hookimpl
def top_query(datasette, request, database, sql):
    if not sql or not sql.strip() or _is_chart_sql(sql):
        return None

    async def inner():
        # Charting an arbitrary query needs execute-sql
        if not await datasette.allowed(
            action="execute-sql",
            resource=DatabaseResource(database),
            actor=request.actor,
        ):
            return None
        params = {
            name: request.args.get(name)
            for name in named_parameters(sql)
            if name in request.args
        }
        source = {"database": database, "kind": "query", "sql": sql}
        if params:
            source["params"] = params
        return _panel(
            datasette,
            database,
            source,
            "{}/-/charts/suggest.json".format(datasette.urls.database(database)),
        )

    return inner


@hookimpl
def top_stored_query(datasette, request, database, query_name):
    if not query_name:
        return None

    async def inner():
        query = await datasette.get_query(database, query_name)
        if query is None or query.is_write or _is_chart_sql(query.sql):
            return None
        params = {
            name: request.args.get(name)
            for name in named_parameters(query.sql)
            if name in request.args
        }
        source = {
            "database": database,
            "kind": "stored_query",
            "query_name": query_name,
        }
        if params:
            source["params"] = params
        return _panel(
            datasette,
            database,
            source,
            "{}/-/charts/suggest.json".format(datasette.urls.database(database)),
        )

    return inner

