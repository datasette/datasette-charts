"Turning a channel's encoding into SQL for one axis of a chart"

from __future__ import annotations

from ..sqlgen import (
    SpecError,
    choose_time_unit,
    temporal_filter_sql,
    time_bucket_sql,
    time_units_for,
)


def temporal_dimension(encoding: dict, profile) -> dict:
    """
    For a channel with a temporal column, returns the bucketing expression,
    an optional filter, and the time unit and kind that were used.

    Without a timeUnit in the encoding, one is chosen from the column's range.
    """
    name = encoding["column"]
    column = profile.column(name) if profile else None
    if column is None:
        raise SpecError(f"No such column: {name}")
    origin = "detected"
    if column.temporal:
        kind = column.temporal.kind
        origin = column.temporal.origin
    elif column.temporal_candidate:
        # Matched on values but lacked a hint: the manual builder may use it
        kind = column.temporal_candidate["kind"]
    else:
        raise SpecError(
            f"{name} does not hold dates or times in a format SQLite can read"
        )
    unit = encoding.get("timeUnit")
    if unit is None:
        unit = choose_time_unit(column.temporal)[0] if column.temporal else None
        unit = unit or time_units_for(kind)[min(2, len(time_units_for(kind)) - 1)]
    return {
        "bucket": time_bucket_sql(name, kind, unit),
        "filter": temporal_filter_sql(name, kind, origin),
        "unit": unit,
        "kind": kind,
    }


def is_temporal(encoding: dict, profile) -> bool:
    "Should this channel be treated as a time axis?"
    if encoding.get("timeUnit"):
        return True
    column = profile.column(encoding["column"]) if profile else None
    return bool(
        column
        and column.role == "temporal"
        and "category" not in column.secondary_roles
    )
