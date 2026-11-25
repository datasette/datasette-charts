"""
Annotations: notes drawn on top of a chart, such as a dashed line where a
rule changed or a shaded range for a recession.

They live in the spec as an ``annotations`` list. They change nothing about
the SQL a chart compiles to: the renderer draws them over the rows it gets.
Like the rest of the spec they are untrusted input, so every key and value
is checked here against fixed lists and ranges. The error messages are
written to be read by whoever wrote the spec, which is often a model.

    {"type": "line", "x": 2009, "position": "after", "text": "Rule enacted"}
    {"type": "line", "y": 100, "text": "Target", "stroke": "solid"}
    {"type": "range", "x": "2008-01", "x2": "2009-06", "text": "Recession"}
    {"type": "point", "x": "2020", "text": "Lockdown"}
    {"type": "text", "left": 0.02, "top": 0.05, "text": "Source: FAA"}

``x`` and ``y`` always mean the spec's x and y channels. On a horizontal bar
chart x is the category axis, drawn down the left-hand side.
"""

from __future__ import annotations

import math
import re

TYPES = ("line", "range", "point", "text")
MAX_ANNOTATIONS = 20
MAX_TEXT = 200
POSITIONS = ("before", "center", "after")
STROKES = ("dashed", "solid", "dotted")
ANCHORS = (
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
)
# Named colors follow the chart's palette, light or dark
COLORS = (
    "default",
    "blue",
    "orange",
    "green",
    "yellow",
    "pink",
    "red",
    "purple",
    "gray",
)
HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# A moment, written as precisely as needed: 2009, 2009-Q3, 2009-06,
# 2009-06-15, 2009-06-15T10, 2009-06-15 10:30 and 2009-06-15T10:30:00
MOMENT = re.compile(
    r"^-?\d{4}(?:-Q[1-4]|-\d\d(?:-\d\d(?:[T ]\d\d(?::\d\d(?::\d\d(?:\.\d+)?)?)?Z?)?)?)?$"
)

# The keys each type takes, besides type itself
COMMON_KEYS = {"type", "text", "color", "anchor"}
TYPE_KEYS = {
    "line": {"x", "y", "position", "stroke"},
    "range": {"x", "x2", "y", "y2"},
    "point": {"x", "y", "series", "marker"},
    "text": {"left", "top"},
}
# What each kind of axis accepts, for the messages
AXIS_VALUES = {
    "number": "a number",
    "category": "a category value: a string or a number",
    "time": 'a date or time such as 2009, "2009-06" or "2009-06-15"',
}


def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _value_ok(kind: str, value) -> bool:
    if kind == "number":
        return _is_number(value)
    if kind == "time":
        if isinstance(value, int) and not isinstance(value, bool):
            return 1 <= value <= 9999  # a year
        return isinstance(value, str) and bool(MOMENT.match(value.strip()))
    # A category, or a time bucket on an axis that can be either
    return (isinstance(value, str) and value != "") or _is_number(value)


def validate_annotations(chart_type, annotations) -> list:
    "Returns a list of error messages for a spec's ``annotations``"
    allowed = tuple(getattr(chart_type, "annotation_types", ()) or ())
    if not isinstance(annotations, list):
        return ["annotations must be a list"]
    if not annotations:
        return []
    if not allowed:
        return [f"{chart_type.name} charts do not take annotations"]
    if len(annotations) > MAX_ANNOTATIONS:
        return [f"annotations: at most {MAX_ANNOTATIONS} are allowed"]
    errors = []
    for index, annotation in enumerate(annotations):
        errors.extend(
            f"annotations[{index}]: {message}"
            for message in _validate_one(chart_type, allowed, annotation)
        )
    return errors


def _validate_one(chart_type, allowed: tuple, annotation) -> list:
    if not isinstance(annotation, dict):
        return ["must be an object"]
    kind = annotation.get("type")
    if kind not in TYPES:
        return ["type must be one of {}".format(", ".join(TYPES))]
    if kind not in allowed:
        return [
            "{} charts take {} annotations, not {}".format(
                chart_type.name, " or ".join(allowed), kind
            )
        ]
    errors = []
    unknown = set(annotation) - COMMON_KEYS - TYPE_KEYS[kind]
    if unknown:
        errors.append(
            "a {} annotation has no {} - it takes {}".format(
                kind,
                ", ".join(sorted(unknown)),
                ", ".join(sorted((COMMON_KEYS | TYPE_KEYS[kind]) - {"type"})),
            )
        )
    axes = getattr(chart_type, "annotation_axes", None) or {}
    series_allowed = getattr(chart_type, "annotation_series", False)

    def check_value(key, channel=None):
        channel = channel or key[0]
        axis = axes.get(channel)
        if axis is None:
            errors.append(f"{chart_type.name} charts have no {channel} axis")
        elif not _value_ok(axis, annotation[key]):
            errors.append(f"{key} must be {AXIS_VALUES[axis]}")

    if "text" in annotation:
        text = annotation["text"]
        if not isinstance(text, str):
            errors.append("text must be a string")
        elif len(text) > MAX_TEXT:
            errors.append(f"text must be at most {MAX_TEXT} characters")
    if kind in ("point", "text") and not (
        isinstance(annotation.get("text"), str) and annotation["text"].strip()
    ):
        errors.append(f"a {kind} annotation needs text")
    if "color" in annotation:
        color = annotation["color"]
        if not isinstance(color, str) or not (
            color in COLORS or HEX_COLOR.match(color)
        ):
            errors.append(
                "color must be one of {}, or a hex color like #d03b3b".format(
                    ", ".join(COLORS)
                )
            )
    if "anchor" in annotation and annotation["anchor"] not in ANCHORS:
        errors.append("anchor must be one of {}".format(", ".join(ANCHORS)))

    if kind == "line":
        has_x, has_y = "x" in annotation, "y" in annotation
        if has_x == has_y:
            errors.append(
                "a line needs x (a vertical line on most charts) or y (a "
                "horizontal line), not both"
            )
        elif has_x:
            check_value("x")
        else:
            check_value("y")
        if "position" in annotation:
            if annotation["position"] not in POSITIONS:
                errors.append("position must be one of {}".format(", ".join(POSITIONS)))
            elif not has_x or axes.get("x") == "number":
                errors.append(
                    "position only applies to a line at an x category or time"
                )
        if "stroke" in annotation and annotation["stroke"] not in STROKES:
            errors.append("stroke must be one of {}".format(", ".join(STROKES)))

    elif kind == "range":
        pairs = [
            axis
            for axis in ("x", "y")
            if axis in annotation or f"{axis}2" in annotation
        ]
        if len(pairs) != 1:
            errors.append("a range needs x and x2, or y and y2")
        else:
            axis = pairs[0]
            if axis not in annotation or f"{axis}2" not in annotation:
                errors.append(f"a range needs both {axis} and {axis}2")
            else:
                check_value(axis)
                check_value(f"{axis}2", axis)

    elif kind == "point":
        if "x" not in annotation:
            errors.append("a point needs x")
        else:
            check_value("x")
        if "y" in annotation:
            check_value("y")
        elif not getattr(chart_type, "annotation_lookup", True):
            errors.append(f"a point on a {chart_type.name} chart needs y")
        if "series" in annotation:
            if not series_allowed:
                errors.append(f"{chart_type.name} charts have no series")
            elif not isinstance(annotation["series"], str) or not annotation["series"]:
                errors.append("series must be a series name")
        if "marker" in annotation and not isinstance(annotation["marker"], bool):
            errors.append("marker must be true or false")

    elif kind == "text":
        for key in ("left", "top"):
            if key not in annotation:
                errors.append(
                    f"a text annotation needs {key}: a fraction of the plot from 0 to 1"
                )
            elif not _is_number(annotation[key]) or not 0 <= annotation[key] <= 1:
                errors.append(f"{key} must be a number from 0 to 1")
    return errors


def describe(chart_types: dict) -> str:
    "Which chart types take which annotations, for the agent's tool description"
    lines = []
    for chart_type in chart_types.values():
        allowed = getattr(chart_type, "annotation_types", ()) or ()
        if allowed:
            lines.append("{}: {}".format(chart_type.name, ", ".join(allowed)))
    return "; ".join(lines)
