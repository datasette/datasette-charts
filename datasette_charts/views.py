"HTTP endpoints (spec section 12). All return JSON."

from __future__ import annotations

import copy
import json
import math
import sqlite3
import urllib.parse

from datasette import Response
from datasette.database import QueryInterrupted
from datasette.resources import DatabaseResource, QueryResource, TableResource
from datasette.utils import (
    InvalidSql,
    named_parameters,
    tilde_decode,
    validate_sql_select,
)

from . import dsl, engine
from .config import get_config
from .profiler import ProfileError, ProfileTimeout
from .sources import Source
from .sqlgen import AGGREGATES, SORTS, TIME_UNITS, SpecError
from .store import get_profile

SIZES = ("thumbnail", "full")


class ChartsError(Exception):
    def __init__(self, message, status=400, errors=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.errors = errors


def _error(message, status=400, errors=None) -> Response:
    body = {"ok": False, "error": message}
    if errors:
        body["errors"] = errors
    return Response.json(body, status=status)


def _json_view(fn):
    "Turn the errors these endpoints expect into JSON responses"

    async def view(datasette, request):
        try:
            return await fn(datasette, request)
        except ChartsError as e:
            return _error(e.message, e.status, e.errors)
        except (ProfileTimeout, QueryInterrupted):
            # What the UI should offer next is an open question (spec section 19)
            return _error("timeout", 400)
        except ProfileError as e:
            return _error(str(e), 400)
        except SpecError as e:
            return _error(str(e), 400, [str(e)])
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            return _error(str(e), 400)

    return view


def _get_database(datasette, request):
    route = tilde_decode(request.url_vars["database"])
    try:
        return datasette.get_database(route=route)
    except KeyError:
        raise ChartsError("Database not found", 404)


def _json_arg(request, name: str):
    raw = request.args.get(name)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise ChartsError(f"{name} is not valid JSON")


def _clean_params(params, sql: str) -> dict:
    "Named parameter values as strings; any the query needs but lacks are ''"
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ChartsError("params must be an object")
    cleaned = {}
    for key, value in params.items():
        if not isinstance(key, str) or isinstance(value, (dict, list)):
            raise ChartsError("params must map names to simple values")
        cleaned[key] = value
    for name in named_parameters(sql):
        cleaned.setdefault(name, "")
    return cleaned


async def resolve_source(datasette, request, db, descriptor) -> Source:
    """
    Build a Source from an untrusted descriptor, applying the same permission
    check as the thing it describes (spec section 14).
    """
    if not isinstance(descriptor, dict):
        raise ChartsError("source must be an object")
    kind = descriptor.get("kind")
    actor = request.actor

    if kind == "table":
        table = descriptor.get("table")
        if not isinstance(table, str) or not table:
            raise ChartsError("source needs a table")
        if not (await db.table_exists(table) or await db.view_exists(table)):
            raise ChartsError("Table not found", 404)
        # execute-sql is not required: this works for anyone who can see the table
        if not await datasette.allowed(
            action="view-table", resource=TableResource(db.name, table), actor=actor
        ):
            raise ChartsError("Permission denied", 403)
        return Source(database=db.name, kind="table", table=table)

    if kind == "query":
        sql = descriptor.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ChartsError("source needs sql")
        if not await datasette.allowed(
            action="execute-sql", resource=DatabaseResource(db.name), actor=actor
        ):
            raise ChartsError("Permission denied", 403)
        try:
            validate_sql_select(sql)
        except InvalidSql as e:
            raise ChartsError(str(e))
        return Source(
            database=db.name,
            kind="query",
            sql=sql,
            params=_clean_params(descriptor.get("params"), sql),
        )

    if kind == "stored_query":
        name = descriptor.get("query_name")
        if not isinstance(name, str) or not name:
            raise ChartsError("source needs a query_name")
        query = await datasette.get_query(db.name, name)
        if query is None:
            raise ChartsError("Query not found", 404)
        if not await datasette.allowed(
            action="view-query", resource=QueryResource(db.name, name), actor=actor
        ):
            raise ChartsError("Permission denied", 403)
        if query.is_write:
            raise ChartsError("Queries that write cannot be charted")
        try:
            validate_sql_select(query.sql)
        except InvalidSql as e:
            raise ChartsError(str(e))
        return Source(
            database=db.name,
            kind="stored_query",
            sql=query.sql,
            params=_clean_params(descriptor.get("params"), query.sql),
            query_name=name,
        )

    raise ChartsError("source kind must be table, query or stored_query")


async def _source_from_request(datasette, request, db) -> Source:
    descriptor = _json_arg(request, "source")
    if descriptor is None and request.url_vars.get("table"):
        descriptor = {
            "kind": "table",
            "table": tilde_decode(request.url_vars["table"]),
        }
    if descriptor is None and request.args.get("sql"):
        # ?sql= with named parameters in the query string, as on the query page
        sql = request.args.get("sql")
        params = {
            name: request.args.get(name)
            for name in named_parameters(sql)
            if name in request.args
        }
        descriptor = {"kind": "query", "sql": sql, "params": params}
    if descriptor is None:
        raise ChartsError("A source is required")
    return await resolve_source(datasette, request, db, descriptor)


def _column_summary(column) -> dict:
    "What the manual builder needs to know about a column"
    summary = {
        "name": column.name,
        "role": column.role,
        "secondary_roles": column.secondary_roles,
    }
    if column.temporal:
        summary["temporal"] = column.temporal.kind
    elif column.temporal_candidate:
        summary["temporal_candidate"] = column.temporal_candidate["kind"]
    return summary


@_json_view
async def chart_types_json(datasette, request):
    chart_types = engine.get_chart_types(datasette)
    return Response.json(
        {
            "ok": True,
            "types": [
                chart_type.to_dict(datasette) for chart_type in chart_types.values()
            ],
            "aggregates": list(AGGREGATES),
            "time_units": list(TIME_UNITS),
            "sorts": list(SORTS),
        }
    )


@_json_view
async def suggest_json(datasette, request):
    db = _get_database(datasette, request)
    source = await _source_from_request(datasette, request, db)
    config = get_config(datasette)
    profile = await get_profile(
        datasette, source, config, refresh=bool(request.args.get("_refresh"))
    )
    suggestions = engine.suggest(
        profile,
        engine.get_chart_types(datasette),
        config,
        debug=bool(request.args.get("_debug")),
    )
    return Response.json(
        {
            "ok": True,
            "source": source.to_dict(),
            "profile": profile.summary(),
            "columns": [_column_summary(column) for column in profile.columns],
            "suggestions": suggestions,
        }
    )


@_json_view
async def profile_json(datasette, request):
    db = _get_database(datasette, request)
    # The profile holds data values, so it gets the same check as the source
    source = await _source_from_request(datasette, request, db)
    profile = await get_profile(
        datasette, source, refresh=bool(request.args.get("_refresh"))
    )
    return Response.json(
        {
            "ok": True,
            "source": source.to_dict(),
            "status": "complete",
            "progress": 1,
            "profile": profile.to_dict(),
        }
    )


async def _labels_allowed(datasette, request, db, profile):
    """
    Foreign key labels come from another table. Hide them from an actor who
    cannot view that table, without touching the shared profile.
    """
    blocked = set()
    checked = {}
    for column in profile.columns:
        fk = column.foreign_key
        if not fk or not fk.get("label_column"):
            continue
        if fk["table"] not in checked:
            checked[fk["table"]] = await datasette.allowed(
                action="view-table",
                resource=TableResource(db.name, fk["table"]),
                actor=request.actor,
            )
        if not checked[fk["table"]]:
            blocked.add(column.name)
    if not blocked:
        return profile
    restricted = copy.copy(profile)
    restricted.columns = []
    for column in profile.columns:
        if column.name in blocked:
            column = copy.copy(column)
            column.foreign_key = {**column.foreign_key, "label_column": None}
        restricted.columns.append(column)
    return restricted


def _json_safe(value):
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@_json_view
async def data_json(datasette, request):
    db = _get_database(datasette, request)
    source = await _source_from_request(datasette, request, db)
    spec = _json_arg(request, "spec")
    if spec is None:
        raise ChartsError("A spec is required")
    size = request.args.get("size") or "full"
    if size not in SIZES:
        raise ChartsError("size must be thumbnail or full")
    config = get_config(datasette)
    chart_types = engine.get_chart_types(datasette)

    profile = await get_profile(datasette, source, config)
    errors = dsl.validate_spec(spec, chart_types, profile.column_names)
    if errors:
        raise ChartsError("Invalid spec", 400, errors)
    chart_type = chart_types[spec["type"]]
    profile = await _labels_allowed(datasette, request, db, profile)
    compiled = chart_type.compile(spec, source, profile)

    # The server enforces the bound whatever a chart type does (spec 8.4)
    max_rows = max(1, min(int(compiled.max_rows), int(config["hard_max_rows"])))
    params = dict(compiled.params)
    params["chart_row_cap"] = max_rows + 1
    sql = f"select * from (\n{compiled.sql}\n) limit :chart_row_cap"
    # Never longer than sql_time_limit_ms, and never taken from the request
    limit_ms = datasette.capped_time_limit_ms(
        config["chart_time_limit_ms"] or datasette.sql_time_limit_ms
    )
    results = await db.execute(
        sql, params, custom_time_limit=limit_ms, log_sql_errors=False
    )
    rows = [[_json_safe(value) for value in row] for row in results.rows]
    truncated = len(rows) > max_rows
    body = {
        "ok": True,
        "title": chart_type.title(spec),
        "columns": [column.name for column in results.columns],
        "rows": rows[:max_rows],
        "truncated": truncated,
        "meta": compiled.meta,
        "basis": profile.basis,
    }
    # The compiled SQL is only for actors who could have run it themselves
    if await datasette.allowed(
        action="execute-sql", resource=DatabaseResource(db.name), actor=request.actor
    ):
        body["sql"] = compiled.sql
        body["params"] = compiled.params
        body["sql_url"] = "{}/-/query?{}".format(
            datasette.urls.database(db.name),
            urllib.parse.urlencode({"sql": compiled.sql, **compiled.params}),
        )
    return Response.json(body)


ROUTES = [
    (r"^/-/charts/types\.json$", chart_types_json),
    (r"^/(?P<database>[^/]+)/-/charts/suggest\.json$", suggest_json),
    (r"^/(?P<database>[^/]+)/(?P<table>[^/]+)/-/charts/suggest\.json$", suggest_json),
    (r"^/(?P<database>[^/]+)/-/charts/data\.json$", data_json),
    (r"^/(?P<database>[^/]+)/-/charts/profile\.json$", profile_json),
]
