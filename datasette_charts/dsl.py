"""
Validation for the chart DSL (spec section 9).

Specs arrive from the browser, so they are untrusted input. The DSL contains
no SQL: every column is checked against the source's actual columns, and
aggregates, time units and numbers are checked against fixed lists and ranges.
"""

from __future__ import annotations

import math

from .sqlgen import AGGREGATES, MAX_BINS, SORTS, TIME_UNITS

DSL_VERSION = 1
SPEC_KEYS = {"v", "type", "encoding", "options"}
CHANNEL_KEYS = {"column", "aggregate", "timeUnit", "bin", "limit", "other", "sort"}
MAX_LIMIT = 100


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def validate_spec(spec, chart_types: dict, columns: list) -> list:
    "Returns a list of error messages; an empty list means the spec is valid"
    if not isinstance(spec, dict):
        return ["The spec must be a JSON object"]
    errors = []
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        errors.append("Unknown spec keys: {}".format(", ".join(sorted(unknown))))
    if spec.get("v") != DSL_VERSION or isinstance(spec.get("v"), bool):
        errors.append(f"v must be {DSL_VERSION}")
    type_name = spec.get("type")
    chart_type = chart_types.get(type_name) if isinstance(type_name, str) else None
    if chart_type is None:
        errors.append(f"Unknown chart type: {type_name!r}")
        return errors
    if not isinstance(spec.get("encoding"), dict):
        errors.append("encoding must be an object")
        return errors
    if "options" in spec and not isinstance(spec["options"], dict):
        errors.append("options must be an object")
        return errors
    errors.extend(chart_type.validate(spec, columns))
    return errors


def validate_channels(chart_type, spec: dict, columns: list) -> list:
    "Checks a spec's encoding against a chart type's channel declarations"
    errors = []
    encoding = spec["encoding"]
    declared = {channel.name: channel for channel in chart_type.channels}
    for name in encoding:
        if name not in declared:
            errors.append(f"{chart_type.name} has no {name} channel")
    for name, channel in declared.items():
        if name not in encoding or encoding[name] is None:
            if channel.required:
                errors.append(f"{name} is required")
            continue
        errors.extend(_validate_channel(name, channel, encoding[name], columns))
    return errors


def _validate_channel(name: str, channel, value, columns: list) -> list:
    if not isinstance(value, dict):
        return [f"{name} must be an object"]
    errors = []
    unknown = set(value) - CHANNEL_KEYS
    if unknown:
        errors.append("{}: unknown keys: {}".format(name, ", ".join(sorted(unknown))))

    column = value.get("column")
    if column is None:
        if channel.column_required:
            errors.append(f"{name} needs a column")
    elif not isinstance(column, str):
        errors.append(f"{name}: column must be a string")
    elif column not in columns:
        errors.append(f"{name}: no such column: {column}")

    if "aggregate" in value:
        aggregate = value["aggregate"]
        if not channel.aggregates:
            errors.append(f"{name} does not take an aggregate")
        elif aggregate not in AGGREGATES or aggregate not in channel.aggregates:
            errors.append(
                f"{name}: aggregate must be one of {', '.join(channel.aggregates)}"
            )
        elif aggregate != "count" and column is None:
            errors.append(f"{name}: {aggregate} needs a column")
    elif channel.aggregates and column is not None and not channel.column_required:
        errors.append(f"{name} needs an aggregate")

    if "timeUnit" in value:
        if not channel.allows_time_unit:
            errors.append(f"{name} does not take a timeUnit")
        elif value["timeUnit"] not in TIME_UNITS:
            errors.append(f"{name}: timeUnit must be one of {', '.join(TIME_UNITS)}")

    if "bin" in value:
        if not channel.binnable:
            errors.append(f"{name} cannot be binned")
        else:
            errors.extend(_validate_bin(name, value["bin"]))

    if "limit" in value:
        if not channel.allows_limit:
            errors.append(f"{name} does not take a limit")
        elif not _is_int(value["limit"]) or not 1 <= value["limit"] <= MAX_LIMIT:
            errors.append(f"{name}: limit must be a whole number from 1 to {MAX_LIMIT}")
    if "other" in value:
        if not channel.allows_limit:
            errors.append(f"{name} does not take other")
        elif not isinstance(value["other"], bool):
            errors.append(f"{name}: other must be true or false")
    if "sort" in value:
        if not channel.allows_sort:
            errors.append(f"{name} does not take a sort")
        elif value["sort"] not in SORTS:
            errors.append(f"{name}: sort must be one of {', '.join(SORTS)}")
    return errors


def _validate_bin(name: str, bin_spec) -> list:
    if not isinstance(bin_spec, dict):
        return [f"{name}: bin must be an object"]
    unknown = set(bin_spec) - {"maxbins", "start", "step", "count"}
    if unknown:
        return ["{}: unknown bin keys: {}".format(name, ", ".join(sorted(unknown)))]
    errors = []
    if "maxbins" in bin_spec:
        if not _is_int(bin_spec["maxbins"]) or not 1 <= bin_spec["maxbins"] <= MAX_BINS:
            errors.append(
                f"{name}: maxbins must be a whole number from 1 to {MAX_BINS}"
            )
    if ("start" in bin_spec) != ("step" in bin_spec):
        errors.append(f"{name}: a bin needs both start and step")
    elif "start" in bin_spec:
        if not _is_number(bin_spec["start"]):
            errors.append(f"{name}: bin start must be a number")
        if not _is_number(bin_spec["step"]) or bin_spec["step"] <= 0:
            errors.append(f"{name}: bin step must be a number above zero")
    elif "maxbins" not in bin_spec and bin_spec:
        errors.append(f"{name}: a bin needs maxbins, or start and step")
    if "count" in bin_spec:
        if not _is_int(bin_spec["count"]) or not 1 <= bin_spec["count"] <= MAX_BINS:
            errors.append(
                f"{name}: bin count must be a whole number from 1 to {MAX_BINS}"
            )
    return errors


def validate_options(chart_type, spec: dict) -> list:
    "Checks options against the chart type's ``options`` declaration"
    errors = []
    declared = chart_type.options or {}
    for key, value in (spec.get("options") or {}).items():
        option = declared.get(key)
        if option is None:
            errors.append(f"{chart_type.name} has no {key} option")
        elif value not in option["values"]:
            errors.append(
                "{} must be one of {}".format(
                    key, ", ".join(map(str, option["values"]))
                )
            )
    return errors


def spec_columns(spec: dict) -> list:
    "The columns a spec refers to, in channel order"
    columns = []
    for value in (spec.get("encoding") or {}).values():
        if isinstance(value, dict) and isinstance(value.get("column"), str):
            if value["column"] not in columns:
                columns.append(value["column"])
    return columns
