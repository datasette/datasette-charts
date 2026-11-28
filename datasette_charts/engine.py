"""
The suggestion engine: propose, then rank (spec section 10).

Scores follow a shared rubric so they are roughly comparable across chart
types:

    0.9 and up   almost certainly something the user wants to see first
    0.6 to 0.9   a solid, informative chart
    0.3 to 0.6   plausible but unremarkable
    under 0.3    do not propose

The verify stage from the spec is not built yet: candidates go straight
from propose to rank.
"""

from __future__ import annotations

from datasette.plugins import pm

from . import dsl
from .chart_types.base import Candidate, ChartType

# --- Central ranking knobs, tunable in one place ------------------------------

# Per-chart-type weight, multiplied into every score. Unlisted types get 1.0.
TYPE_WEIGHTS = {
    "map": 1.0,
    "line": 1.0,
    "bar": 1.0,
    "histogram": 1.0,
    "stacked_bar": 1.0,
    "scatter": 1.0,
    "calendar_heatmap": 1.0,
    "box": 1.0,
    "intervals": 1.0,
}
# Candidates scoring under this are dropped before ranking
MIN_SCORE = 0.3
# Taken off for each suggestion of the same chart type already chosen
SAME_TYPE_PENALTY = 0.06
# Taken off in proportion to how many of a candidate's columns are already used
COLUMN_OVERLAP_PENALTY = 0.1
# Added when a candidate brings in a column no chosen suggestion has shown
NEW_COLUMN_BONUS = 0.04
# The greedy pass stops once the best remaining candidate is under this
MIN_ADJUSTED_SCORE = 0.2


def get_chart_types(datasette) -> dict:
    "Registered chart types as {name: instance}, in registration order"
    cached = getattr(datasette, "_datasette_charts_types", None)
    if cached is not None:
        return cached
    chart_types = {}
    for classes in pm.hook.register_chart_types(datasette=datasette):
        for cls in classes or []:
            instance = cls() if isinstance(cls, type) else cls
            if not isinstance(instance, ChartType) or not instance.name:
                continue
            chart_types.setdefault(instance.name, instance)
    datasette._datasette_charts_types = chart_types
    return chart_types


def propose(profile, chart_types: dict) -> list:
    "Every candidate from every chart type, as (chart_type, candidate) pairs"
    proposed = []
    columns = profile.column_names
    for chart_type in chart_types.values():
        for candidate in chart_type.propose(profile) or []:
            if candidate.score < MIN_SCORE:
                continue
            # A chart type that proposes an invalid spec has a bug: drop it
            if dsl.validate_spec(candidate.spec, chart_types, columns):
                continue
            proposed.append((chart_type, candidate))
    return proposed


def canonical_columns(profile) -> dict:
    """
    Maps each column to the first column with identical facts.

    Two columns that hold the same data, such as stargazers_count and
    watchers_count, would otherwise each get their own near-identical charts.
    """
    first_seen = {}
    canonical = {}
    for column in profile.columns:
        if column.role in ("constant", "unknown", "blob") or not column.non_null:
            canonical[column.name] = column.name
            continue
        numeric = column.numeric or {}
        temporal = column.temporal
        if temporal:
            # The same moments can be written two ways, so compare the range
            facts = (temporal.min, temporal.max)
        elif numeric:
            facts = (numeric.get("min"), numeric.get("max"), numeric.get("mean"))
        else:
            facts = (column.text, (column.top_values or [])[:5])
        fingerprint = repr((column.role, column.non_null, column.distinct, facts))
        canonical[column.name] = first_seen.setdefault(fingerprint, column.name)
    return canonical


def rank(proposed: list, profile, max_suggestions: int) -> list:
    """
    Greedy selection across chart types with a diversity pass.

    Deterministic: ties break on chart type name, then the positions of the
    columns used, then their names. No randomness, no wall-clock inputs.
    """
    positions = {column.name: column.position for column in profile.columns}
    canonical = canonical_columns(profile)
    remaining = []
    for chart_type, candidate in proposed:
        spec_columns = dsl.spec_columns(candidate.spec)
        columns = [canonical.get(name, name) for name in spec_columns]
        weighted = candidate.score * TYPE_WEIGHTS.get(chart_type.name, 1.0)
        tie_break = (
            chart_type.name,
            tuple(positions.get(name, 0) for name in spec_columns),
            tuple(spec_columns),
            repr(sorted(candidate.spec["encoding"].items(), key=lambda kv: kv[0])),
        )
        remaining.append(
            {
                "chart_type": chart_type,
                "candidate": candidate,
                "columns": columns,
                "weighted": weighted,
                "tie_break": tie_break,
            }
        )

    chosen = []
    type_counts = {}
    used_columns = set()
    seen = set()  # (chart type, columns): near-duplicate suppression
    chosen_column_sets = []
    while remaining and len(chosen) < max_suggestions:
        best = None
        for item in remaining:
            columns = item["columns"]
            same_type = type_counts.get(item["chart_type"].name, 0)
            # The largest share of this candidate's columns that any one
            # chosen suggestion already shows. A new combination of columns
            # that have each been seen separately is only partly penalized.
            overlap = max(
                (
                    len([c for c in columns if c in other]) / len(columns)
                    for other in chosen_column_sets
                    if columns
                ),
                default=0.0,
            )
            brings_new = any(c not in used_columns for c in columns)
            adjusted = (
                item["weighted"]
                - SAME_TYPE_PENALTY * same_type
                - COLUMN_OVERLAP_PENALTY * overlap
                + (NEW_COLUMN_BONUS if brings_new and chosen else 0.0)
            )
            item["adjusted"] = adjusted
            item["adjustments"] = {
                "same_type": same_type,
                "column_overlap": round(overlap, 4),
                "new_column": brings_new,
            }
            key = (-adjusted, item["tie_break"])
            if best is None or key < best[0]:
                best = (key, item)
        item = best[1]
        remaining.remove(item)
        if item["adjusted"] < MIN_ADJUSTED_SCORE:
            break
        # The same columns in the same chart type with, say, a different
        # aggregate: the higher-scoring one has already been chosen
        duplicate_key = (item["chart_type"].name, frozenset(item["columns"]))
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        chosen.append(item)
        type_counts[item["chart_type"].name] = (
            type_counts.get(item["chart_type"].name, 0) + 1
        )
        used_columns.update(item["columns"])
        chosen_column_sets.append(set(item["columns"]))
    return chosen


def suggest(profile, chart_types: dict, config: dict, debug: bool = False) -> list:
    "The ranked suggestions for a profile, as JSON-ready dictionaries"
    eager = (
        profile.row_count is not None
        and profile.row_count <= config["thumbnail_row_limit"]
    )
    suggestions = []
    for item in rank(propose(profile, chart_types), profile, config["max_suggestions"]):
        candidate: Candidate = item["candidate"]
        suggestion = {
            "spec": candidate.spec,
            "title": item["chart_type"].title(candidate.spec),
            "reason": candidate.reason,
            "score": round(item["adjusted"], 3),
            "basis": profile.basis,
            "preview": "eager" if eager else "on_demand",
        }
        if debug:
            suggestion["signals"] = {
                "raw_score": candidate.score,
                **item["adjustments"],
                **candidate.signals,
            }
        suggestions.append(suggestion)
    return suggestions
