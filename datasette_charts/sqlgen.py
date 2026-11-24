"""
SQL building blocks shared by chart types: aggregates, time buckets and bins.

Chart types do not build date SQL themselves (spec section 7.5).
"""

from __future__ import annotations

import datetime
import math

from .profiler.roles import UNIX_MS_WINDOW, UNIX_SECONDS_WINDOW, YEAR_WINDOW
from .profiler.sql import SUPPORTS_MATERIALIZED_HINT
from .sources import Source, quote

AGGREGATES = ("count", "sum", "avg", "min", "max", "count_distinct")
# Aggregates whose per-group results can be added up into an "Other" bucket
ADDITIVE_AGGREGATES = ("count", "sum")
TIME_UNITS = ("decade", "year", "quarter", "month", "week", "day", "hour", "minute")
SORTS = ("-value", "value", "key", "-key")

# Approximate seconds per unit, for estimating how many buckets a span gives
UNIT_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2629800,
    "quarter": 7889400,
    "year": 31557600,
    "decade": 315576000,
}
# The time unit is chosen so the bucket count stays at or under this
TARGET_TIME_BUCKETS = 100
# Hard bound on time buckets returned by any chart
MAX_TIME_BUCKETS = 500
# Hard bound on histogram bins, whatever a spec asks for
MAX_BINS = 200

AGGREGATE_LABELS = {
    "count": "Count",
    "sum": "Total",
    "avg": "Average",
    "min": "Minimum",
    "max": "Maximum",
    "count_distinct": "Distinct",
}


class SpecError(ValueError):
    "A spec that passed validation but cannot be compiled against this source"


def numeric_filter(column: str) -> str:
    return f"typeof({quote(column)}) in ('integer', 'real')"


def measure_sql(encoding: dict | None) -> str:
    "The value a channel aggregates: its column, or NULL for a bare count"
    column = (encoding or {}).get("column")
    return quote(column) if column is not None else "null"


def aggregate_over(encoding: dict | None, value: str = "v") -> str:
    """
    SQL for a channel like {'aggregate': 'sum', 'column': 'price'}, applied
    to ``value``: the alias the column was given by measure_sql().

    Chart queries first project source columns onto their own aliases and
    only then aggregate, so a source column that happens to be called
    "value" or "bucket" cannot be confused with a chart's own names.
    """
    encoding = encoding or {}
    aggregate = encoding.get("aggregate") or "count"
    if aggregate not in AGGREGATES:
        raise SpecError(f"Unknown aggregate: {aggregate}")
    if encoding.get("column") is None:
        if aggregate != "count":
            raise SpecError(f"{aggregate} needs a column")
        return "count(*)"
    if aggregate == "count":
        return f"count({value})"
    if aggregate == "count_distinct":
        return f"count(distinct {value})"
    # Only real numbers: text in a mostly-numeric column would poison a sum
    return f"{aggregate}(case when typeof({value}) in ('integer', 'real') then {value} end)"


def rank_over(encoding: dict | None, value: str = "v") -> str:
    """
    What "top N" means for a channel: the largest totals where values add
    up (a count or a sum), otherwise the most common values.
    """
    aggregate = (encoding or {}).get("aggregate") or "count"
    if aggregate in ADDITIVE_AGGREGATES:
        return aggregate_over(encoding, value)
    return "count(*)"


def not_materialized() -> str:
    "Hint for a CTE that is read twice but is cheaper to run twice than to store"
    return "not materialized " if SUPPORTS_MATERIALIZED_HINT else ""


def aggregate_title(encoding: dict | None) -> str:
    "'Rows', 'Total price', 'Average price'"
    encoding = encoding or {}
    aggregate = encoding.get("aggregate") or "count"
    column = encoding.get("column")
    if column is None:
        return "Rows"
    if aggregate == "count":
        return f"Count of {column}"
    if aggregate == "count_distinct":
        return f"Distinct {column}"
    return f"{AGGREGATE_LABELS[aggregate]} {column}"


# --- Time -------------------------------------------------------------------


def _time_args(column: str, kind: str) -> str:
    q = quote(column)
    if kind == "unix_seconds":
        return f"{q}, 'unixepoch'"
    if kind == "unix_ms":
        return f"{q} / 1000, 'unixepoch'"
    return q


def time_units_for(kind: str) -> tuple:
    "The time units that make sense for a temporal kind"
    if kind == "year":
        return ("decade", "year")
    if kind == "iso_date":
        return ("decade", "year", "quarter", "month", "week", "day")
    return TIME_UNITS


def time_bucket_sql(column: str, kind: str, unit: str) -> str:
    """
    A bucketing expression for a column, its temporal kind and a time unit.

    Buckets are labels that sort in time order: 2020 (the first year, for
    decades), 2026, 2026-Q4, 2026-11, 2026-11-16 (the Monday, for weeks),
    2026-11-22, 2026-11-22T10 and 2026-11-22T10:30. A value SQLite cannot
    parse gives a NULL bucket.
    """
    if unit not in TIME_UNITS:
        raise SpecError(f"Unknown time unit: {unit}")
    if unit not in time_units_for(kind):
        raise SpecError(f"A {kind} column cannot be bucketed by {unit}")
    if kind == "year":
        year = f"cast({quote(column)} as integer)"
        return f"({year} / 10) * 10" if unit == "decade" else year
    args = _time_args(column, kind)
    if unit == "decade":
        return f"(cast(strftime('%Y', {args}) as integer) / 10) * 10"
    if unit == "year":
        return f"strftime('%Y', {args})"
    if unit == "quarter":
        return (
            f"strftime('%Y', {args}) || '-Q' || "
            f"((cast(strftime('%m', {args}) as integer) + 2) / 3)"
        )
    if unit == "month":
        return f"strftime('%Y-%m', {args})"
    if unit == "week":
        # The Monday on or before the date
        return f"date({args}, 'weekday 0', '-6 days')"
    if unit == "day":
        return f"date({args})"
    if unit == "hour":
        return f"strftime('%Y-%m-%dT%H', {args})"
    return f"strftime('%Y-%m-%dT%H:%M', {args})"


def temporal_filter_sql(column: str, kind: str, origin: str = "detected") -> str | None:
    """
    A predicate that keeps numeric timestamps inside the detection window,
    so a stray 0 does not stretch the axis back to 1970. Configured columns
    are trusted as they are.
    """
    if origin == "config":
        return None
    q = quote(column)
    window = {
        "unix_seconds": UNIX_SECONDS_WINDOW,
        "unix_ms": UNIX_MS_WINDOW,
        "year": YEAR_WINDOW,
    }.get(kind)
    if window is None:
        return None
    return f"{numeric_filter(column)} and {q} between {window[0]} and {window[1]}"


def parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.datetime.strptime(text[:length], fmt)
        except ValueError:
            continue
    return None


def estimate_buckets(earliest: str | None, latest: str | None, unit: str) -> int | None:
    start, end = parse_iso(earliest), parse_iso(latest)
    if start is None or end is None:
        return None
    seconds = max(0.0, (end - start).total_seconds())
    return int(seconds // UNIT_SECONDS[unit]) + 1


def choose_time_unit(temporal, target: int = TARGET_TIME_BUCKETS) -> tuple:
    """
    The finest unit that keeps the bucket count at or under the target.
    Returns (unit, estimated_buckets), or (None, None) without a range.
    """
    units = time_units_for(temporal.kind)
    coarsest_first = [u for u in TIME_UNITS if u in units]
    best = None
    for unit in reversed(coarsest_first):  # finest first
        n = estimate_buckets(temporal.min, temporal.max, unit)
        if n is None:
            return None, None
        best = (unit, n)
        if n <= target:
            return best
    return best  # the coarsest unit, even though it is over the target


def per_time_unit(unit: str, column: str) -> str:
    "'per month of created_at', or just 'per decade' for a column called decade"
    if column.lower() == unit:
        return f"per {unit}"
    return f"per {unit} of {column}"


def span_description(earliest: str | None, latest: str | None) -> str:
    "'3 years', '5 months', '12 days'"
    start, end = parse_iso(earliest), parse_iso(latest)
    if start is None or end is None:
        return "an unknown span"
    seconds = max(0.0, (end - start).total_seconds())
    for unit in ("year", "month", "day", "hour", "minute"):
        n = seconds / UNIT_SECONDS[unit]
        if n >= 1.5:
            return f"{round(n):,} {unit}s"
        if n >= 1:
            return f"1 {unit}"
    return "under a minute"


# --- Bins -------------------------------------------------------------------


def _nice_step(raw: float, integer: bool) -> float:
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    exponent = math.floor(math.log10(raw))
    base = 10.0**exponent
    for multiple in (1, 2, 2.5, 5, 10):
        step = multiple * base
        if integer and step != int(step):
            continue
        if step >= raw * (1 - 1e-9):
            break
    if integer:
        step = max(1.0, step)
    return step


def nice_bins(lo: float, hi: float, maxbins: int = 30, integer: bool = False) -> dict:
    "Round-number bins covering lo to hi: {'start':, 'step':, 'count':}"
    maxbins = max(1, min(int(maxbins), MAX_BINS))
    if hi <= lo:
        step = 1.0
        start = math.floor(lo)
        return {"start": _tidy(start), "step": _tidy(step), "count": 1}
    step = _nice_step((hi - lo) / maxbins, integer)
    start = math.floor(lo / step) * step
    count = int(math.floor((hi - start) / step + 1e-9)) + 1
    while count > maxbins + 1:
        # Rounding start down can add a bin: widen the step until it fits
        step = _nice_step(step * 1.5, integer)
        start = math.floor(lo / step) * step
        count = int(math.floor((hi - start) / step + 1e-9)) + 1
    return {"start": _tidy(start), "step": _tidy(step), "count": count}


def _tidy(value: float):
    "Drop float noise, and use an int where the value is one"
    value = round(value, 10)
    return int(value) if value == int(value) else value


def robust_range(numeric: dict) -> tuple:
    """
    The range to bin over: min to max, unless a long tail would squash
    everything into the first bin, in which case the 1st or 99th percentile
    stands in and the tail lands in an open-ended bin.

    Returns (lo, hi, clipped_low, clipped_high).
    """
    lo, hi = numeric["min"], numeric["max"]
    quantiles = numeric.get("quantiles")
    if not quantiles:
        return lo, hi, False, False
    p01, p99 = quantiles["p01"], quantiles["p99"]
    core = p99 - p01
    if core <= 0:
        return lo, hi, False, False
    clipped_low = (p01 - lo) > 1.5 * core
    clipped_high = (hi - p99) > 1.5 * core
    return (
        (p01 if clipped_low else lo),
        (p99 if clipped_high else hi),
        clipped_low,
        clipped_high,
    )


def resolve_bin(
    bin_spec: dict | None, column_profile, default_maxbins: int = 30
) -> dict:
    "Turn {'maxbins': n} into explicit start, step and count using the profile"
    bin_spec = bin_spec or {}
    if "start" in bin_spec and "step" in bin_spec:
        count = bin_spec.get("count")
        return {
            "start": bin_spec["start"],
            "step": bin_spec["step"],
            "count": min(int(count), MAX_BINS) if count else MAX_BINS,
        }
    numeric = getattr(column_profile, "numeric", None)
    if not numeric or numeric.get("min") is None:
        raise SpecError(
            f"{column_profile.name if column_profile else 'column'} has no numeric values to bin"
        )
    lo, hi, _, _ = robust_range(numeric)
    counts = column_profile.type_counts
    integer = counts.get("real", 0) == 0
    return nice_bins(lo, hi, bin_spec.get("maxbins") or default_maxbins, integer)


# Histograms switch to log-scale bins when the 99th percentile is at least
# this many times the median
LOG_SCALE_MIN_RATIO = 20
MAX_LOG_EDGES = 60


def wants_log_scale(numeric: dict) -> bool:
    "Is this distribution so skewed that linear bins would hide its shape?"
    quantiles = numeric.get("quantiles")
    if not quantiles or numeric.get("min") is None or numeric["min"] < 0:
        return False
    typical = quantiles["p50"] if quantiles["p50"] > 0 else quantiles["p75"]
    return typical > 0 and quantiles["p99"] / typical >= LOG_SCALE_MIN_RATIO


def log_edges(numeric: dict | None) -> list | None:
    """
    Bin edges on a 1-2-5 series (1, 2, 5, 10, 20, 50, ...) covering the
    positive values of a column, or None if it has negative values.

    Values under the first edge, which includes zero, fall in their own bin.
    """
    if not numeric or numeric.get("min") is None or numeric["min"] < 0:
        return None
    hi = numeric["max"]
    if hi <= 0:
        return None
    quantiles = numeric.get("quantiles") or {}
    candidates = [numeric["min"]] + [
        quantiles.get(q) for q in ("p01", "p25", "p50", "p75", "p99")
    ]
    low = next((v for v in candidates if v is not None and v > 0), hi / 1000)
    low = min(low, hi)
    first = math.floor(math.log10(low))
    last = math.ceil(math.log10(hi)) + 1
    multiples = (1, 2, 5) if (last - first) * 3 <= MAX_LOG_EDGES else (1,)
    series = [_tidy(m * 10.0**k) for k in range(first, last + 1) for m in multiples]
    below = [edge for edge in series if edge <= low]
    above = [edge for edge in series if edge > hi]
    start = below[-1] if below else series[0]
    end = above[0] if above else series[-1]
    edges = [edge for edge in series if start <= edge <= end]
    return edges if len(edges) >= 2 else None


def log_bucket_sql(value_sql: str, edges: list) -> str:
    "The bin number for a value: -1 below the first edge, len(edges) - 1 above the last"
    cases = " ".join(
        f"when {value_sql} < {float(edge)!r} then {i - 1}"
        for i, edge in enumerate(edges)
    )
    return f"case {cases} else {len(edges) - 1} end"


def bucket_sql(value_sql: str, prefix: str) -> str:
    """
    The bin number for a value: -1 below the range and ``count`` above it,
    so the result stays bounded if the data has grown past the profiled range.
    """
    return (
        f"case when {value_sql} < :{prefix}_start then -1 else "
        f"min(cast(({value_sql} - :{prefix}_start) / :{prefix}_step as integer), "
        f":{prefix}_count) end"
    )


def bin_params(prefix: str, resolved: dict) -> dict:
    return {
        f"{prefix}_start": resolved["start"],
        f"{prefix}_step": resolved["step"],
        f"{prefix}_count": resolved["count"],
    }


# --- Params -----------------------------------------------------------------


def merge_params(source: Source, chart_params: dict) -> dict:
    "Chart parameters all start chart_; a clash with the query's own is an error"
    params = dict(source.params or {})
    for key, value in chart_params.items():
        if key in params:
            raise SpecError(f"The query already uses a parameter called :{key}")
        params[key] = value
    return params


# --- Formatting for reasons -------------------------------------------------


def fmt_number(value, plain: bool = False) -> str:
    "plain=True leaves out thousands separators, for years"
    if value is None:
        return "?"
    whole = isinstance(value, int) or (
        isinstance(value, float) and value == int(value) and abs(value) < 1e15
    )
    if whole:
        return str(int(value)) if plain else f"{int(value):,}"
    if abs(value) >= 1000:
        return f"{value:.0f}" if plain else f"{value:,.0f}"
    return f"{value:.3g}"


def plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"
