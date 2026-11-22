from __future__ import annotations

from datasette import hookimpl
from datasette.plugins import pm

from . import hookspecs

pm.add_hookspecs(hookspecs)

from .chart_types import (  # noqa: E402
    BUILTIN_CHART_TYPES,
    Candidate,
    Channel,
    ChartType,
    CompiledQuery,
)
from .sources import Source  # noqa: E402

__all__ = ["Candidate", "Channel", "ChartType", "CompiledQuery", "Source"]


@hookimpl
def register_chart_types(datasette):
    # The built-in chart types use the same hook third parties get
    return BUILTIN_CHART_TYPES
