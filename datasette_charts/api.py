"""
A Python API for other plugins: suggestions, chart data, links and embeds.

The HTTP endpoints take a request. These take an actor instead, so they work
from anywhere that knows who is asking, such as a datasette-agent tool. They
apply the same permission checks as the endpoints.

    source = await resolve(datasette, actor, {"database": "shop", "kind": "table", "table": "orders"})
    data = await chart_data(datasette, actor, source, spec)
    html = embed_html(datasette, source, spec)

``resolve`` and the functions that run queries raise one of
``views.EXPECTED_ERRORS``; ``error_body`` turns that into a JSON-ready
dictionary.
"""

from __future__ import annotations

import json
import urllib.parse

from datasette.utils.asgi import Request
from markupsafe import Markup

from . import views
from .config import PLUGIN_NAME
from .sources import Source
from .views import EXPECTED_ERRORS, ChartsError

__all__ = [
    "EXPECTED_ERRORS",
    "ChartsError",
    "chart_data",
    "chart_path",
    "embed_html",
    "error_body",
    "resolve",
    "suggest",
]

FRAGMENT = "#chart="


def error_body(e: Exception) -> dict:
    "A JSON-ready description of one of EXPECTED_ERRORS"
    return views.error_body(e)[0]


def _request(actor) -> Request:
    # The table filter hooks read the actor from a request, so they get one
    request = Request.fake("/")
    request.scope["actor"] = actor
    return request


def _database(datasette, name):
    try:
        return datasette.get_database(name)
    except KeyError:
        raise ChartsError("Database not found", 404)


async def resolve(datasette, actor, descriptor: dict) -> Source:
    """
    Check an untrusted source descriptor against what the actor may see and
    return the Source it describes. The descriptor is the dictionary the HTTP
    API takes as ``source``, plus a ``database`` key.
    """
    if not isinstance(descriptor, dict):
        raise ChartsError("source must be an object")
    db = _database(datasette, descriptor.get("database"))
    return await views.resolve_source(datasette, _request(actor), db, descriptor)


async def suggest(datasette, actor, source: Source) -> dict:
    "Ranked chart suggestions for a source, as ``suggest.json`` returns them"
    return await views.suggestions_body(datasette, source)


async def chart_data(datasette, actor, source: Source, spec: dict) -> dict:
    """
    The rows for one chart, as ``data.json`` returns them. Raises ChartsError
    with ``errors`` set if the spec is not valid for this source.
    """
    db = _database(datasette, source.database)
    return await views.chart_body(datasette, _request(actor), db, source, spec)


def source_path(datasette, source: Source) -> str:
    "The page that shows a source: its table, stored query or SQL query page"
    if source.kind == "table":
        path = str(datasette.urls.table(source.database, source.table))
        query = list(source.filters or ())
    elif source.kind == "stored_query":
        path = str(datasette.urls.query(source.database, source.query_name))
        query = list((source.params or {}).items())
    else:
        path = "{}/-/query".format(datasette.urls.database(source.database))
        query = [("sql", source.sql), *(source.params or {}).items()]
    query = [(key, "" if value is None else value) for key, value in query]
    return path + ("?" + urllib.parse.urlencode(query) if query else "")


def chart_path(datasette, source: Source, spec: dict) -> str:
    """
    A link to a chart: the source's own page, with the chart selected in the
    Charts panel and the builder open beside it.
    """
    encoded = urllib.parse.quote(json.dumps(spec, separators=(",", ":")), safe="")
    return source_path(datasette, source) + FRAGMENT + encoded


def embed_html(datasette, source: Source, spec: dict) -> Markup:
    """
    HTML that draws one chart anywhere on this Datasette instance: a
    ``<datasette-chart>`` element with the script and stylesheet it needs.

    The element fetches its rows from ``data.json`` as whoever is looking at
    it, so the usual permission checks apply each time it is drawn.
    """

    def static(path):
        return str(datasette.static(path, plugin=PLUGIN_NAME))

    database_url = str(datasette.urls.database(source.database))
    config = {
        "source": source.to_dict(),
        "spec": spec,
        "chart_url": chart_path(datasette, source, spec),
        "urls": {
            "types": str(datasette.urls.path("/-/charts/types.json")),
            "data": f"{database_url}/-/charts/data.json",
            "d3": static("vendor/d3.min.js"),
            "plot": static("vendor/plot.umd.min.js"),
            "topojson": static("vendor/topojson-client.min.js"),
            "land": static("vendor/land-110m.json"),
        },
    }
    return Markup(
        '<link rel="stylesheet" href="{css}">'
        '<script type="module" src="{js}"></script>'
        '<datasette-chart class="datasette-charts" config="{config}"></datasette-chart>'
    ).format(
        css=static("charts.css"),
        js=static("embed.js"),
        config=json.dumps(config),
    )
