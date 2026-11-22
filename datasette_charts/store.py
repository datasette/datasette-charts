"""
A small in-memory memo of profiles.

Suggestions and every chart drawn from them need the same profile, so
without this a page with eight thumbnails would profile its source nine
times. This is not the stored-profile design from the spec (section 7.6):
nothing is written to the internal database, nothing survives a restart,
and entries simply expire.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

from .config import get_config
from .profiler import Profile, profile_source
from .sources import Source

MAX_ENTRIES = 200


def _state(datasette) -> dict:
    state = getattr(datasette, "_datasette_charts_store", None)
    if state is None:
        state = {"profiles": OrderedDict(), "in_flight": {}}
        datasette._datasette_charts_store = state
    return state


async def get_profile(
    datasette, source: Source, config: dict | None = None, refresh: bool = False
) -> Profile:
    config = config or get_config(datasette)
    ttl = config["profile_cache_seconds"]
    state = _state(datasette)
    key = (source.database, source.kind, source.key)

    if ttl and not refresh:
        entry = state["profiles"].get(key)
        if entry is not None:
            stored_at, profile = entry
            if time.monotonic() - stored_at <= ttl and await _columns_match(
                datasette, source, profile
            ):
                return profile
            state["profiles"].pop(key, None)

    # Requests for the same source that arrive together share one profile run
    in_flight = state["in_flight"].get(key)
    if in_flight is not None and not refresh:
        return await asyncio.shield(in_flight)

    task = asyncio.ensure_future(profile_source(datasette, source, config))
    state["in_flight"][key] = task
    try:
        profile = await asyncio.shield(task)
    finally:
        if state["in_flight"].get(key) is task:
            del state["in_flight"][key]
    if ttl:
        profiles = state["profiles"]
        profiles[key] = (time.monotonic(), profile)
        profiles.move_to_end(key)
        while len(profiles) > MAX_ENTRIES:
            profiles.popitem(last=False)
    return profile


async def _columns_match(datasette, source: Source, profile: Profile) -> bool:
    "A table whose columns have changed needs profiling again"
    if source.kind != "table":
        return True
    db = datasette.get_database(source.database)
    try:
        details = await db.table_column_details(source.table)
    except Exception:
        return False
    current = [column.name for column in details if not column.hidden]
    return current == profile.column_names
