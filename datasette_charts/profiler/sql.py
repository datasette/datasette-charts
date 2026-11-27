"""
SQL generation for the profiler (spec sections 7.4 and 7.5).

Every query has the shape::

    with _chart_source as (...), _chart_profiled as (...) select ... from _chart_profiled

``_chart_profiled`` is the whole source, or a sample of it.
"""

from __future__ import annotations

import sqlite3

from ..sources import Source, quote, table_ref
from .roles import UNIX_MS_WINDOW, UNIX_SECONDS_WINDOW, YEAR_WINDOW

# SQLite 3.35 added the MATERIALIZED and NOT MATERIALIZED hints
SUPPORTS_MATERIALIZED_HINT = sqlite3.sqlite_version_info >= (3, 35, 0)

ISO_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]"
ISO_DATETIME_GLOB = ISO_DATE_GLOB + "[T ][0-2][0-9]:[0-5][0-9]*"

# SQLite allows 2,000 result columns by default, and a column needs up to 22
ONE_SCAN_COLUMNS_PER_QUERY = 80

QUANTILES = (("p01", 0.01), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p99", 0.99))


def _hint(materialized: bool) -> str:
    if not SUPPORTS_MATERIALIZED_HINT:
        return ""
    return "materialized " if materialized else "not materialized "


def full_cte(materialized: bool) -> str:
    return f"_chart_profiled as {_hint(materialized)}(select * from _chart_source)"


def first_n_cte() -> str:
    "Cheap, but biased toward whatever order the source produces"
    return (
        f"_chart_profiled as {_hint(True)}(select * from _chart_source limit :sample_n)"
    )


def rowid_probe_cte(table: str) -> str:
    """
    Evenly spaced rowid probes: deterministic and spanning the whole table.

    Each probe takes the first row at or after its rowid, so tables with
    gaps (deleted rows, or integer primary keys that are not sequential)
    still give a full-sized sample. Each probe is one index lookup.
    """
    t = table_ref(table)
    return (
        f"_chart_profiled as {_hint(True)}(\n"
        f"  select * from {t} where rowid in (\n"
        "    with recursive _chart_probes(i) as (\n"
        "      select 0 union all select i + 1 from _chart_probes where i + 1 < :sample_n\n"
        "    )\n"
        f"    select (select rowid from {t}\n"
        "            where rowid >= :sample_lo + cast(i * :sample_step as integer)\n"
        "            order by rowid limit 1)\n"
        "    from _chart_probes\n"
        "  )\n"
        ")"
    )


def rowid_stride_cte(table: str) -> str:
    """
    Every Nth row in rowid order. Unlike probes this is not thrown off by
    clustered rowids, but it has to walk every rowid, so it is only used
    when the table is small enough for that to be cheap.
    """
    t = table_ref(table)
    return (
        f"_chart_profiled as {_hint(True)}(\n"
        f"  select * from {t} where rowid in (\n"
        "    select rowid from (\n"
        f"      select rowid, row_number() over (order by rowid) as rn from {t}\n"
        "    ) where (rn - 1) % :sample_every = 0\n"
        "  )\n"
        ")"
    )


def with_profiled(source: Source, profiled_cte: str) -> str:
    return f"{source.with_clause()},\n{profiled_cte}"


def one_scan_expressions(
    index: int, column: str, affinity: str | None = None, coordinate: bool = False
) -> list:
    """
    One group of expressions per column; aliases use the column's index.

    A column with TEXT affinity can only store text, blobs and nulls, so
    its numeric facts are skipped. ``coordinate`` adds the range tests for
    columns whose names suggest a latitude or longitude.
    """
    q = quote(column)
    numeric = f"typeof({q}) in ('integer', 'real')"
    text = f"typeof({q}) = 'text'"
    a = f"c{index}_"
    expressions = [
        f"count({q}) as {a}non_null",
        f"sum({text}) as {a}n_text",
        f"sum(typeof({q}) = 'blob') as {a}n_blob",
        f"min(case when {text} then length({q}) end) as {a}len_min",
        f"max(case when {text} then length({q}) end) as {a}len_max",
        f"avg(case when {text} then length({q}) end) as {a}len_mean",
        f"sum({q} is not null and {q} != '') as {a}n_present",
        (
            f"sum({text} and {q} glob '{ISO_DATE_GLOB}'"
            f" and julianday({q}) is not null) as {a}n_iso_date"
        ),
        (
            f"sum({text} and {q} glob '{ISO_DATETIME_GLOB}'"
            f" and julianday({q}) is not null) as {a}n_iso_datetime"
        ),
        f"sum({text} and ({q} like 'http://%' or {q} like 'https://%')) as {a}n_url",
        f"sum({text} and ({q} glob '{{*}}' or {q} glob '[[]*]')) as {a}n_json",
    ]
    if affinity != "text":
        expressions += [
            f"sum(typeof({q}) = 'integer') as {a}n_integer",
            f"sum(typeof({q}) = 'real') as {a}n_real",
            f"min(case when {numeric} then {q} end) as {a}min",
            f"max(case when {numeric} then {q} end) as {a}max",
            f"avg(case when {numeric} then {q} end) as {a}mean",
            f"avg(case when {numeric} then cast({q} as real) * {q} end) as {a}mean_sq",
            (
                f"sum({numeric} and {q} between {UNIX_SECONDS_WINDOW[0]}"
                f" and {UNIX_SECONDS_WINDOW[1]}) as {a}n_unix_seconds"
            ),
            (
                f"sum({numeric} and {q} between {UNIX_MS_WINDOW[0]}"
                f" and {UNIX_MS_WINDOW[1]}) as {a}n_unix_ms"
            ),
            (
                f"sum(typeof({q}) = 'integer' and {q} between {YEAR_WINDOW[0]}"
                f" and {YEAR_WINDOW[1]}) as {a}n_year"
            ),
        ]
        if coordinate:
            expressions += [
                f"sum({numeric} and {q} between -90 and 90) as {a}n_lat",
                f"sum({numeric} and {q} between -180 and 180) as {a}n_lon",
            ]
    return expressions


def one_scan_sql(source: Source, profiled_cte: str, columns: list) -> str:
    "columns is a list of (index, name, affinity, coordinate) tuples"
    expressions = ["count(*) as n_rows"]
    for index, name, affinity, coordinate in columns:
        expressions.extend(one_scan_expressions(index, name, affinity, coordinate))
    return "{}\nselect\n  {}\nfrom _chart_profiled".format(
        with_profiled(source, profiled_cte), ",\n  ".join(expressions)
    )


def distinct_select(index: int, column: str) -> str:
    "Stops as soon as the cap is passed"
    q = quote(column)
    return (
        f"select {index} as col, (select count(*) from ("
        f"select distinct {q} from _chart_profiled where {q} is not null"
        " limit :distinct_cap + 1)) as n"
    )


def top_values_select(index: int, column: str) -> str:
    q = quote(column)
    return (
        f"select {index} as col, value, n from ("
        f"select {q} as value, count(*) as n from _chart_profiled"
        f" where {q} is not null group by 1 order by n desc, 1 limit :top_values)"
    )


def quantiles_select(index: int, column: str) -> str:
    q = quote(column)
    picks = ", ".join(
        f"max(case when rn = min(n, cast(n * {p} as integer) + 1) then x end) as {name}"
        for name, p in QUANTILES
    )
    return (
        f"select {index} as col, {picks} from ("
        f"select {q} as x, row_number() over (order by {q}) as rn,"
        f" count(*) over () as n from _chart_profiled"
        f" where typeof({q}) in ('integer', 'real'))"
    )


def text_temporal_range_select(index: int, column: str) -> str:
    "Goes through julianday() so values with different offsets compare correctly"
    q = quote(column)
    return (
        f"select {index} as col,"
        f" datetime(min(julianday({q}))) as earliest,"
        f" datetime(max(julianday({q}))) as latest,"
        f" sum({q} glob '*[0-9]Z' or {q} glob '*[+-][0-9][0-9]:[0-9][0-9]')"
        " as n_with_offset"
        f" from _chart_profiled where typeof({q}) = 'text'"
        f" and {q} glob '{ISO_DATE_GLOB}*' and julianday({q}) is not null"
    )


def numeric_temporal_range_select(index: int, column: str, window: tuple) -> str:
    q = quote(column)
    return (
        f"select {index} as col, min({q}) as earliest, max({q}) as latest,"
        " 0 as n_with_offset"
        f" from _chart_profiled where typeof({q}) in ('integer', 'real')"
        f" and {q} between {window[0]} and {window[1]}"
    )
