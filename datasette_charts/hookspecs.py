from pluggy import HookspecMarker

hookspec = HookspecMarker("datasette")


@hookspec
def register_chart_types(datasette):
    "Return a list of ChartType subclasses"
