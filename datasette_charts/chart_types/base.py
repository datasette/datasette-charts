"""
The chart type interface (spec section 8).

A chart type is a self-contained bundle of heuristics (``propose``),
SQL generation (``compile``) and a JavaScript renderer (``js_url``).
The DSL is the contract between the Python half and the JavaScript half.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .. import dsl
from ..profiler.roles import name_tokens
from ..sqlgen import AGGREGATES


@dataclass(frozen=True)
class Channel:
    name: str  # "x", "y", "series", ...
    roles: tuple = ()  # roles a column should have to fit here
    required: bool = True
    aggregates: tuple = ()  # allowed aggregates, if any
    binnable: bool = False
    allows_time_unit: bool = False
    # Shown in the manual builder
    label: str | None = None
    # False for a channel that can be a bare {"aggregate": "count"}
    column_required: bool = True
    # Takes "limit" and "other": keep the top N values, fold the rest into Other
    allows_limit: bool = False
    allows_sort: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["roles"] = list(self.roles)
        d["aggregates"] = list(self.aggregates)
        d["label"] = self.label or self.name
        return d


@dataclass
class Candidate:
    spec: dict
    score: float  # 0 to 1: see the rubric in engine.py
    reason: str  # plain English, shown to the user
    signals: dict = field(default_factory=dict)  # the facts behind the score


@dataclass
class CompiledQuery:
    sql: str
    params: dict
    max_rows: int
    # Passed to the renderer: resolved bins, the time unit, the mode chosen
    meta: dict = field(default_factory=dict)


class ChartType:
    name: str = None  # unique, lowercase, e.g. "line"
    label: str = None  # "Line chart"
    description: str = ""
    icon: str = ""  # inline SVG, used when no thumbnail is shown
    js_url: str = ""  # URL of the ES module that exports render()
    channels: list = []
    max_rows: int = 500  # upper bound on rows this chart ever needs
    # {"layout": {"label": "Layout", "values": [...], "default": ...}}
    options: dict = {}
    # The annotation types this chart's renderer draws: any of "line",
    # "range", "point" and "text". None by default, since a renderer has to
    # draw them. See annotations.py.
    annotation_types: tuple = ()
    # What an annotation's x and y are on this chart, by spec channel:
    # "number", "category" or "time"
    annotation_axes: dict = {}
    # Whether a point annotation can name a series
    annotation_series: bool = False
    # Whether a point annotation can leave out y and have it looked up
    annotation_lookup: bool = True

    def channel(self, name: str) -> Channel | None:
        for channel in self.channels:
            if channel.name == name:
                return channel
        return None

    def propose(self, profile) -> list:
        "Pure function of the profile. No database access."
        return []

    async def verify(self, candidate: Candidate, ctx) -> Candidate | None:
        """
        Optional. May run cheap, budgeted queries. Return an adjusted
        candidate, or None to drop it. Not called yet in this version.
        """
        return candidate

    def validate(self, spec: dict, columns: list) -> list:
        "Return a list of error messages"
        return dsl.validate_channels(self, spec, columns) + dsl.validate_options(
            self, spec
        )

    def compile(self, spec: dict, source, profile) -> CompiledQuery:
        """
        Return SQL and params that select from the ``source`` CTE and
        return at most ``max_rows`` rows.
        """
        raise NotImplementedError

    def title(self, spec: dict) -> str:
        "Deterministic title, e.g. 'Rows per month of created_at'"
        return self.label

    def get_js_url(self, datasette) -> str:
        if self.js_url.startswith("/"):
            return datasette.urls.path(self.js_url)
        return self.js_url

    def to_dict(self, datasette) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "js_url": self.get_js_url(datasette),
            "channels": [channel.to_dict() for channel in self.channels],
            "options": self.options,
            "max_rows": self.max_rows,
            "annotations": {
                "types": list(self.annotation_types),
                "axes": dict(self.annotation_axes),
                "series": self.annotation_series,
                "lookup": self.annotation_lookup,
            },
        }


ALL_ANNOTATIONS = ("line", "range", "point", "text")


class BuiltinChartType(ChartType):
    "Built-in renderers are static files shipped with this plugin"

    js_file: str = None
    annotation_types = ALL_ANNOTATIONS

    def get_js_url(self, datasette) -> str:
        return datasette.static(f"renderers/{self.js_file}", plugin="datasette-charts")


# --- Scoring helpers shared by the built-in chart types ----------------------

ALL_AGGREGATES = AGGREGATES
MEASURE_AGGREGATES = ("sum", "avg", "min", "max")

# Names that suggest a quantity worth looking at
MEASURE_NAME_TOKENS = {
    "price", "amount", "total", "count", "size", "score", "cost", "revenue",
    "sales", "qty", "quantity", "downloads", "population", "capacity", "weight",
    "height", "length", "duration", "age", "salary", "income", "rating", "stars",
    "stargazers", "forks", "views", "value", "balance", "mw", "gwh", "kwh",
    "distance", "area", "speed", "temperature", "percent", "pct", "rate",
}  # fmt: skip
# Names that suggest adding values up makes more sense than averaging them
ADDITIVE_NAME_TOKENS = {
    "amount", "total", "count", "revenue", "sales", "qty", "quantity",
    "downloads", "population", "capacity", "views", "mw", "gwh", "kwh",
    "stars", "stargazers", "forks", "cost", "balance", "size",
}  # fmt: skip
CATEGORY_NAME_TOKENS = {
    "type", "status", "category", "kind", "state", "country", "party",
    "gender", "language", "region", "class", "group", "fuel", "source",
    "level", "tier", "department", "genre", "brand", "license",
}  # fmt: skip


def null_penalty(null_rate: float, weight: float = 0.35) -> float:
    """
    High null rates lower a score. A column that is half empty still charts
    well, so nothing is taken off up to 20% and the full weight only applies
    to a column that is almost entirely null.
    """
    return weight * max(0.0, null_rate - 0.2) / 0.8


def measure_quality(column) -> float:
    "How promising a measure column looks, 0 to 1"
    numeric = column.numeric or {}
    if numeric.get("min") is None or numeric["min"] == numeric.get("max"):
        return 0.0
    score = 0.5
    if MEASURE_NAME_TOKENS.intersection(name_tokens(column.name)):
        score += 0.1
    score -= null_penalty(column.null_rate, 0.4)
    return max(0.0, round(score, 4))


def default_aggregate(column) -> str:
    if ADDITIVE_NAME_TOKENS.intersection(name_tokens(column.name)):
        return "sum"
    return "avg"


def best_measures(profile, limit: int) -> list:
    "The most promising measure columns, best first, ties by position"
    scored = [
        (measure_quality(column), column)
        for column in profile.columns
        if column.role == "measure"
    ]
    scored = [(q, c) for q, c in scored if q >= 0.3]
    scored.sort(key=lambda pair: (-pair[0], pair[1].position))
    return [column for _, column in scored[:limit]]


def category_columns(profile) -> list:
    "Columns that work as the category axis of a chart"
    return [
        column
        for column in profile.columns
        if column.role in ("category", "boolean")
        or (column.role == "foreign_key" and "category" in column.secondary_roles)
    ]


def category_quality(column, for_counts: bool = True) -> tuple:
    """
    How promising a category column looks. Returns (score, signals).

    A handful to a few dozen values is ideal. For a chart of row counts, one
    dominant value or near-uniform counts is less informative; pass
    for_counts=False when the chart shows a measure instead.
    """
    distinct = column.distinct_count
    if not distinct:
        return 0.0, {}
    signals = {"distinct": distinct, "null_rate": column.null_rate}
    score = 0.62
    if column.role == "boolean":
        score = 0.46
    if distinct == 1:
        score = 0.3
    elif 3 <= distinct <= 30:
        score += 0.08
    elif distinct > 100:
        score -= 0.12
    top = column.top_values or []
    if top and column.non_null:
        top_share = top[0][1] / column.non_null
        signals["top_share"] = round(top_share, 4)
        if top_share > 0.95:
            score -= 0.25
        elif top_share > 0.8:
            score -= 0.1
        counts = [n for _, n in top]
        # The most common values are all about as common as each other
        if (
            for_counts
            and distinct >= 3
            and (max(counts) - min(counts)) <= 0.2 * max(counts)
        ):
            signals["uniform"] = True
            score -= 0.2
    if CATEGORY_NAME_TOKENS.intersection(name_tokens(column.name)):
        score += 0.04
    score -= null_penalty(column.null_rate, 0.4)
    return max(0.0, round(score, 4)), signals


def top_coverage(column, n: int = 3) -> tuple:
    "(how many top values, the share of non-null rows they cover)"
    top = (column.top_values or [])[:n]
    if not top or not column.non_null:
        return 0, 0.0
    return len(top), sum(count for _, count in top) / column.non_null
