from __future__ import annotations

PLUGIN_NAME = "datasette-charts"

# Every number here is a starting point for experiments (spec section 16)
DEFAULTS = {
    "max_suggestions": 8,
    # Rows in the sample used when a source is over inline_row_limit
    "sample_rows": 10000,
    # At or below this many rows the whole source is profiled inline...
    "inline_row_limit": 50000,
    # ...provided rows x columns is also at or below this. The profiling scan
    # costs about the same for every cell, so a wide table has to be sampled
    # sooner than a narrow one to fit the SQL time limit. The sample shrinks
    # the same way, down to a floor of 1,000 rows.
    "inline_cell_limit": 1000000,
    # Share of present values that must look like dates
    "temporal_min_match_rate": 0.95,
    # Above this many rows, previews load on demand, not eagerly
    "thumbnail_row_limit": 100000,
    "thumbnail_concurrency": 2,
    # Global cap on the rows any chart query can return
    "hard_max_rows": 5000,
    # Distinct counts are exact up to this cap
    "distinct_cap": 1000,
    "top_values": 20,
    # Total time one inline profile may take. None means twice
    # sql_time_limit_ms, and at least five seconds.
    "profile_budget_ms": None,
    # Optional shorter limit for chart queries; capped at sql_time_limit_ms
    "chart_time_limit_ms": None,
    # Profiles are kept in memory for this long. 0 turns that off.
    "profile_cache_seconds": 300,
    # Load suggestions as soon as a table or query page opens
    "auto_suggest": True,
}

# Accepted so a config written against the full spec does not fail,
# but not used yet: these belong to the background tier and stored profiles
NOT_YET_USED = {
    "background_time_limit_ms",
    "chunk_rows",
    "stale_after_hours",
    "stale_row_change",
}


def get_config(datasette) -> dict:
    config = dict(DEFAULTS)
    plugin_config = datasette.plugin_config(PLUGIN_NAME) or {}
    for key, value in plugin_config.items():
        if key in DEFAULTS:
            config[key] = value
    if config["profile_budget_ms"] is None:
        config["profile_budget_ms"] = max(5000, 2 * datasette.sql_time_limit_ms)
    return config


def temporal_overrides(datasette, database: str, table: str) -> dict:
    "Table-level ``temporal`` overrides: {column: kind or False}"
    table_config = (
        datasette.plugin_config(
            PLUGIN_NAME, database=database, table=table, fallback=False
        )
        or {}
    )
    overrides = table_config.get("temporal")
    return overrides if isinstance(overrides, dict) else {}
