"The built-in chart types. They register through the same hook third parties use."

from .bar import Bar
from .base import (
    BuiltinChartType,
    Candidate,
    Channel,
    ChartType,
    CompiledQuery,
)
from .box_plot import BoxPlot
from .calendar_heatmap import CalendarHeatmap
from .histogram import Histogram
from .active_count import ActiveCount
from .line import Line
from .map import Map
from .scatter import Scatter
from .stacked_bar import StackedBar

BUILTIN_CHART_TYPES = [
    Histogram,
    Bar,
    Line,
    Scatter,
    Map,
    StackedBar,
    CalendarHeatmap,
    BoxPlot,
    ActiveCount,
]

__all__ = [
    "BUILTIN_CHART_TYPES",
    "BuiltinChartType",
    "Candidate",
    "Channel",
    "ChartType",
    "CompiledQuery",
]
