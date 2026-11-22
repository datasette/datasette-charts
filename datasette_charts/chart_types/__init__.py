"The built-in chart types. They register through the same hook third parties use."

from .bar import Bar
from .base import (
    BuiltinChartType,
    Candidate,
    Channel,
    ChartType,
    CompiledQuery,
)
from .histogram import Histogram
from .line import Line
from .map import Map
from .scatter import Scatter
from .stacked_bar import StackedBar

BUILTIN_CHART_TYPES = [Histogram, Bar, Line, Scatter, Map, StackedBar]

__all__ = [
    "BUILTIN_CHART_TYPES",
    "BuiltinChartType",
    "Candidate",
    "Channel",
    "ChartType",
    "CompiledQuery",
]
