"""
The shared profiler: gathers facts about a source and its columns.

This first version runs tier 0 (schema) and tier 1 inline, under the normal
SQL time limit. A source at or under ``inline_row_limit`` rows is profiled
in full; a larger one is profiled from a sample. The background full tier
and stored profiles from the spec are not built yet.
"""

from __future__ import annotations

import datetime
import math
import sqlite3
import time

from datasette.database import QueryInterrupted

from ..config import get_config, temporal_overrides
from ..sources import Source, quote
from . import sql as psql
from .model import ColumnProfile, Profile, TemporalInfo
from .roles import (
    LONG_TEXT_MEAN_LENGTH,
    UNIX_MS_WINDOW,
    UNIX_SECONDS_WINDOW,
    YEAR_WINDOW,
    affinity_for,
    assign_role,
    detect_pairs,
    is_coordinate_name,
)

__all__ = [
    "profile_source",
    "Profile",
    "ColumnProfile",
    "TemporalInfo",
    "ProfileError",
    "ProfileTimeout",
]

# Rowid probes are used when at least this share of the rowid range is filled
ROWID_DENSE_RATIO = 0.5
# Below that, every Nth rowid is taken instead, for tables up to this size
ROWID_STRIDE_MAX_ROWS = 1_000_000

# However wide the source, a sample is never smaller than this
MIN_SAMPLE_ROWS = 1000

# Selects combined into one query with UNION ALL, so a sample is only
# materialized once per batch
BATCH_SIZE = 10


class ProfileError(Exception):
    "The source could not be profiled, for example because its SQL is invalid"


class ProfileTimeout(ProfileError):
    "The source is too expensive to profile under the time limit"


class _BudgetExceeded(Exception):
    pass


class _Runner:
    "Runs profiling queries under a capped time limit and a total budget"

    def __init__(self, datasette, db, source: Source, budget_ms: int):
        self.datasette = datasette
        self.db = db
        self.source = source
        self.budget_ms = budget_ms
        self.started = time.monotonic()
        self.queries = 0
        self.params = dict(source.params or {})

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    async def execute(self, sql: str, extra_params: dict | None = None):
        remaining = self.budget_ms - self.elapsed_ms()
        if remaining <= 0:
            raise _BudgetExceeded()
        # These limits only ever shorten the default (spec section 15.1)
        limit = self.datasette.capped_time_limit_ms(max(1, remaining))
        params = dict(self.params)
        if extra_params:
            params.update(extra_params)
        self.queries += 1
        return await self.db.execute(
            sql, params, custom_time_limit=limit, log_sql_errors=False
        )

    async def batched(self, prefix: str, selects: list, extra_params=None) -> tuple:
        """
        Runs selects in UNION ALL batches. A batch that times out is retried
        one select at a time. Returns (rows, number_of_selects_that_failed).
        """
        rows = []
        failed = 0
        for start in range(0, len(selects), BATCH_SIZE):
            batch = selects[start : start + BATCH_SIZE]
            try:
                results = await self.execute(
                    prefix + "\n" + "\nunion all\n".join(batch), extra_params
                )
                rows.extend(results.rows)
                continue
            except _BudgetExceeded:
                failed += len(selects) - start
                break
            except QueryInterrupted:
                if len(batch) == 1:
                    failed += 1
                    continue
            for select in batch:
                try:
                    results = await self.execute(prefix + "\n" + select, extra_params)
                    rows.extend(results.rows)
                except QueryInterrupted:
                    failed += 1
                except _BudgetExceeded:
                    failed += 1
        return rows, failed


async def profile_source(
    datasette, source: Source, config: dict | None = None
) -> Profile:
    "Profile a source inline: schema facts, then a full or sampled scan"
    config = config or get_config(datasette)
    db = datasette.get_database(source.database)
    runner = _Runner(datasette, db, source, config["profile_budget_ms"])
    profile = Profile(source=source.to_dict())
    try:
        await _profile(datasette, db, source, config, runner, profile)
    except QueryInterrupted as e:
        raise ProfileTimeout("timeout") from e
    except _BudgetExceeded as e:
        raise ProfileTimeout("timeout") from e
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        raise ProfileError(str(e)) from e
    profile.duration_ms = runner.elapsed_ms()
    profile.queries = runner.queries
    profile.profiled_at = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return profile


async def _profile(datasette, db, source, config, runner, profile) -> None:
    # Tier 0: schema. No table scan.
    columns = await _schema(datasette, db, source, runner, profile)
    profile.columns = columns
    if not columns:
        return

    # How big is it?
    try:
        results = await runner.execute(
            f"{source.with_clause()}\nselect count(*) from source"
        )
        profile.row_count = results.rows[0][0]
        profile.row_count_exact = True
    except QueryInterrupted:
        profile.notes.append("Counting rows timed out")

    # Rowid sampling reads the table directly, so it cannot see a filter
    use_table_tricks = (
        source.kind == "table" and not profile.is_view and not source.where
    )
    cell_limit = int(config["inline_cell_limit"])
    inline = (
        profile.row_count is not None
        and profile.row_count <= config["inline_row_limit"]
        and profile.row_count * len(columns) <= cell_limit
    )
    sample_rows = min(
        int(config["sample_rows"]), max(MIN_SAMPLE_ROWS, cell_limit // len(columns))
    )
    row = None
    if inline:
        profiled_cte = psql.full_cte(materialized=not use_table_tricks)
        sample_params = {}
        try:
            row = await _one_scan(runner, source, profiled_cte, columns, sample_params)
            profile.basis = "full"
        except QueryInterrupted:
            profile.notes.append("A full scan timed out, so a sample was used")
    if row is None:
        profiled_cte, sample_params, method = await _sample_cte(
            db, source, sample_rows, use_table_tricks, profile.row_count
        )
        row = await _one_scan(runner, source, profiled_cte, columns, sample_params)
        profile.basis = "sample"
        profile.sample = {"method": method, "rows": row["n_rows"]}
        if profile.row_count is not None and row["n_rows"] >= profile.row_count:
            # The sample turned out to be the whole source
            profile.basis = "full"
            profile.sample = None

    profile.rows_profiled = row["n_rows"]
    for column in columns:
        _apply_one_scan(column, row, profile.rows_profiled)

    prefix = psql.with_profiled(source, profiled_cte)
    exact = profile.basis == "full"
    await _distinct_and_top_values(
        runner, prefix, sample_params, config, profile, exact
    )

    # Roles
    overrides = {}
    if source.kind == "table":
        overrides = temporal_overrides(datasette, source.database, source.table)
    for column in columns:
        assign_role(
            column,
            min_match_rate=config["temporal_min_match_rate"],
            temporal_override=overrides.get(column.name),
        )
    detect_pairs(profile)

    await _temporal_ranges(runner, prefix, sample_params, profile)
    await _quantiles(runner, prefix, sample_params, profile)


# --- Tier 0 -----------------------------------------------------------------


async def _schema(datasette, db, source, runner, profile) -> list:
    if source.kind != "table":
        results = await runner.execute(
            f"{source.with_clause()}\nselect * from source limit 0"
        )
        return [
            ColumnProfile(name=column.name, position=i)
            for i, column in enumerate(results.columns)
        ]

    table = source.table
    details = await db.table_column_details(table)
    profile.is_view = await db.view_exists(table)
    visible = [column for column in details if not column.hidden]
    pks = [
        name
        for _, name in sorted(
            (column.pk_position, column.name)
            for column in details
            if column.pk_position
        )
    ]
    profile.primary_keys = pks

    foreign_keys = {}
    unique_columns = set()
    column_types = {}
    if not profile.is_view:
        for fk in await db.foreign_keys_for_table(table):
            label_column = None
            try:
                label_column = await db.label_column_for_table(fk["other_table"])
            except Exception:  # a missing table should not stop the profile
                pass
            foreign_keys[fk["column"]] = {
                "table": fk["other_table"],
                "column": fk["other_column"],
                "label_column": label_column,
            }
        profile.foreign_keys = [
            {"column": column, **fk} for column, fk in foreign_keys.items()
        ]
        unique_columns = await db.execute_fn(lambda conn: _unique_columns(conn, table))
    try:
        assigned = await datasette.get_column_types(source.database, table)
        column_types = {
            name: getattr(column_type, "name", None)
            for name, column_type in assigned.items()
        }
    except Exception:
        pass

    columns = []
    for i, column in enumerate(visible):
        is_single_pk = pks == [column.name]
        columns.append(
            ColumnProfile(
                name=column.name,
                position=i,
                declared_type=column.type,
                affinity=affinity_for(column.type),
                is_pk=is_single_pk,
                unique_declared=is_single_pk or column.name in unique_columns,
                foreign_key=foreign_keys.get(column.name),
                column_type=column_types.get(column.name),
            )
        )
    return columns


def _unique_columns(conn, table: str) -> set:
    "Columns covered by a single-column unique index"
    unique = set()
    for index in conn.execute(f"PRAGMA index_list({quote(table)})").fetchall():
        name, is_unique = index[1], index[2]
        if not is_unique:
            continue
        info = conn.execute(f"PRAGMA index_info({quote(name)})").fetchall()
        if len(info) == 1 and info[0][2]:
            unique.add(info[0][2])
    return unique


# --- Sampling ---------------------------------------------------------------


async def _sample_cte(db, source, n, use_table_tricks, row_count) -> tuple:
    if use_table_tricks:
        try:
            # min and max of rowid are both instant
            bounds = await db.execute(
                f"select min(rowid), max(rowid) from {quote(source.table)}",
                log_sql_errors=False,
            )
            lo, hi = bounds.rows[0][0], bounds.rows[0][1]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            lo = hi = None  # a WITHOUT ROWID table
        if isinstance(lo, int) and isinstance(hi, int) and hi > lo:
            span = hi - lo + 1
            sparse = row_count is not None and row_count / span < ROWID_DENSE_RATIO
            if sparse and row_count <= ROWID_STRIDE_MAX_ROWS:
                # Probes land in the gaps of sparse or clustered rowids
                params = {"sample_every": max(1, math.ceil(row_count / n))}
                return psql.rowid_stride_cte(source.table), params, "rowid_stride"
            params = {"sample_n": n, "sample_lo": lo, "sample_step": span / n}
            return psql.rowid_probe_cte(source.table), params, "rowid_probes"
    return psql.first_n_cte(), {"sample_n": n}, "first_n"


# --- The one-scan query -----------------------------------------------------


async def _one_scan(runner, source, profiled_cte, columns, sample_params) -> dict:
    merged = {}
    step = psql.ONE_SCAN_COLUMNS_PER_QUERY
    for start in range(0, len(columns), step):
        chunk = [
            (c.position, c.name, c.affinity, is_coordinate_name(c.name))
            for c in columns[start : start + step]
        ]
        results = await runner.execute(
            psql.one_scan_sql(source, profiled_cte, chunk), sample_params
        )
        row = results.rows[0]
        for i, column in enumerate(results.columns):
            merged[column.name] = row[i]
    return merged


def _number(value):
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    return value


def _apply_one_scan(column: ColumnProfile, row: dict, rows_profiled: int) -> None:
    def get(fact):
        return row.get(f"c{column.position}_{fact}")

    def count(fact):
        return get(fact) or 0

    column.non_null = count("non_null")
    column.present = count("n_present")
    column.null_rate = (
        round(1 - column.non_null / rows_profiled, 4) if rows_profiled else 0.0
    )
    column.type_counts = {
        "integer": count("n_integer"),
        "real": count("n_real"),
        "text": count("n_text"),
        "blob": count("n_blob"),
        "null": rows_profiled - column.non_null,
    }
    if get("min") is not None:
        mean = get("mean")
        mean_sq = get("mean_sq")
        stddev = None
        if mean is not None and mean_sq is not None:
            stddev = _number(math.sqrt(max(0.0, mean_sq - mean * mean)))
        column.numeric = {
            "min": _number(get("min")),
            "max": _number(get("max")),
            "mean": _number(mean),
            "stddev": stddev,
        }
    if get("len_max") is not None:
        column.text = {
            "min_length": get("len_min"),
            "max_length": get("len_max"),
            "mean_length": round(get("len_mean"), 2),
        }
    column.tests = {
        "iso_date": count("n_iso_date"),
        "iso_datetime": count("n_iso_datetime"),
        "unix_seconds": count("n_unix_seconds"),
        "unix_ms": count("n_unix_ms"),
        "year": count("n_year"),
        "url": count("n_url"),
        "json": count("n_json"),
    }
    if get("n_lat") is not None:
        column.tests["latitude"] = count("n_lat")
        column.tests["longitude"] = count("n_lon")


# --- Distinct counts and top values -----------------------------------------


def _json_safe(value):
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return _number(value)


async def _distinct_and_top_values(
    runner, prefix, sample_params, config, profile, exact
):
    cap = int(config["distinct_cap"])
    by_position = {column.position: column for column in profile.columns}

    wanted = []
    for column in profile.columns:
        if not column.non_null:
            column.distinct = {"count": 0, "capped": False}
            continue
        if column.type_counts.get("blob", 0) > column.non_null / 2:
            continue
        if column.text and column.text["mean_length"] > LONG_TEXT_MEAN_LENGTH:
            continue  # free text: not worth counting
        if column.unique_declared and exact:
            column.unique = True
            column.distinct = {
                "count": min(column.non_null, cap),
                "capped": column.non_null > cap,
            }
            continue
        wanted.append(column)

    params = dict(sample_params)
    params["distinct_cap"] = cap
    rows, failed = await runner.batched(
        prefix, [psql.distinct_select(c.position, c.name) for c in wanted], params
    )
    for row in rows:
        column = by_position[row[0]]
        n = row[1] or 0
        column.distinct = {"count": min(n, cap), "capped": n > cap}
        if exact and n <= cap:
            column.unique = n == column.non_null and n > 1

    low = [
        column
        for column in wanted
        if column.distinct is not None
        and not column.distinct["capped"]
        and column.distinct["count"] > 0
    ]
    params = dict(sample_params)
    params["top_values"] = int(config["top_values"])
    rows, failed_top = await runner.batched(
        prefix, [psql.top_values_select(c.position, c.name) for c in low], params
    )
    for column in low:
        column.top_values = []
    for row in rows:
        by_position[row[0]].top_values.append([_json_safe(row[1]), row[2]])
    for column in low:
        # Stable, so SQL's own tie-break order within equal counts is kept
        column.top_values.sort(key=lambda pair: -pair[1])

    if failed or failed_top:
        profile.notes.append(
            f"Ran out of time counting distinct values for {failed + failed_top} columns"
        )


# --- Temporal ranges --------------------------------------------------------


def _unix_to_iso(seconds) -> str | None:
    try:
        moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M:%S")


async def _temporal_ranges(runner, prefix, sample_params, profile) -> None:
    by_position = {column.position: column for column in profile.columns}
    selects = []
    for column in profile.columns:
        if not column.temporal:
            continue
        kind = column.temporal.kind
        if kind in ("iso_date", "iso_datetime"):
            selects.append(
                psql.text_temporal_range_select(column.position, column.name)
            )
        else:
            window = {
                "unix_seconds": UNIX_SECONDS_WINDOW,
                "unix_ms": UNIX_MS_WINDOW,
                "year": YEAR_WINDOW,
            }[kind]
            selects.append(
                psql.numeric_temporal_range_select(column.position, column.name, window)
            )
    if not selects:
        return
    rows, failed = await runner.batched(prefix, selects, sample_params)
    for row in rows:
        column = by_position[row[0]]
        earliest, latest, n_with_offset = row[1], row[2], row[3]
        kind = column.temporal.kind
        if earliest is None or latest is None:
            continue
        if kind == "unix_seconds":
            earliest, latest = _unix_to_iso(earliest), _unix_to_iso(latest)
        elif kind == "unix_ms":
            earliest, latest = _unix_to_iso(earliest / 1000), _unix_to_iso(
                latest / 1000
            )
        elif kind == "year":
            earliest, latest = f"{int(earliest):04d}-01-01", f"{int(latest):04d}-12-31"
        elif kind == "iso_date":
            earliest, latest = earliest[:10], latest[:10]
        column.temporal.min = earliest
        column.temporal.max = latest
        column.temporal.has_offset = bool(n_with_offset)
    if failed:
        profile.notes.append(
            f"Ran out of time finding date ranges for {failed} columns"
        )


# --- Quantiles --------------------------------------------------------------


async def _quantiles(runner, prefix, sample_params, profile) -> None:
    by_position = {column.position: column for column in profile.columns}
    measures = [
        column
        for column in profile.columns
        if column.has_role("measure")
        and column.numeric
        and column.numeric["min"] != column.numeric["max"]
    ]
    if not measures:
        return
    rows, failed = await runner.batched(
        prefix,
        [psql.quantiles_select(c.position, c.name) for c in measures],
        sample_params,
    )
    names = [name for name, _ in psql.QUANTILES]
    for row in rows:
        values = [_number(v) for v in row[1:]]
        if any(v is None for v in values):
            continue
        by_position[row[0]].numeric["quantiles"] = dict(zip(names, values))
    if failed:
        profile.notes.append(f"Ran out of time finding quantiles for {failed} columns")
