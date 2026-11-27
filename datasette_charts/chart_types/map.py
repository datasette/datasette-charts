from __future__ import annotations

from ..sources import quote
from ..sqlgen import aggregate_over, aggregate_title, merge_params, numeric_filter
from .base import (
    MEASURE_AGGREGATES,
    BuiltinChartType,
    Candidate,
    Channel,
    CompiledQuery,
    null_penalty,
)

# At or under this many rows a map draws every point
RAW_POINT_LIMIT = 2500
MAX_CELLS = 3000
# The grid is sized so the data's bounding box holds about this many cells
TARGET_GRID_CELLS = 40000
CELL_SIZES = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10)


def choose_cell(profile, latitude: str, longitude: str) -> float:
    "Grid cell size in degrees, chosen from the extent of the coordinates"
    extents = []
    for name, limit in ((latitude, 90), (longitude, 180)):
        column = profile.column(name) if profile else None
        numeric = (column.numeric if column else None) or {}
        lo = max(-limit, numeric.get("min", -limit))
        hi = min(limit, numeric.get("max", limit))
        extents.append(max(hi - lo, 0.001))
    for cell in CELL_SIZES:
        if (extents[0] / cell) * (extents[1] / cell) <= TARGET_GRID_CELLS:
            return cell
    return CELL_SIZES[-1]


class Map(BuiltinChartType):
    name = "map"
    label = "Map"
    description = (
        "Latitude and longitude on a map: points for small sources,"
        " grid cells for large ones"
    )
    icon = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        ' stroke-linejoin="round" aria-hidden="true">'
        '<path d="M12 21s-6-5.5-6-10a6 6 0 1 1 12 0c0 4.500-6 10-6 10z"/>'
        '<circle cx="12" cy="11" r="2"/></svg>'
    )
    js_file = "map.js"
    channels = [
        Channel(name="latitude", label="Latitude", roles=("latitude",)),
        Channel(name="longitude", label="Longitude", roles=("longitude",)),
        Channel(
            name="size",
            label="Size by",
            roles=("measure",),
            required=False,
            aggregates=MEASURE_AGGREGATES,
        ),
    ]
    max_rows = MAX_CELLS
    # A map has no x or y axis, so only free-floating notes
    annotation_types = ("text",)

    def _raw(self, profile) -> bool:
        return (
            profile is not None
            and profile.row_count is not None
            and profile.row_count <= RAW_POINT_LIMIT
        )

    def propose(self, profile) -> list:
        candidates = []
        for pair in profile.pairs:
            if pair.get("kind") != "latlon":
                continue
            latitude = profile.column(pair["latitude"])
            longitude = profile.column(pair["longitude"])
            null_rate = max(latitude.null_rate, longitude.null_rate)
            if self._raw(profile):
                how = "one point per row"
            else:
                how = "counted in {}° grid cells".format(
                    choose_cell(profile, latitude.name, longitude.name)
                )
            candidates.append(
                Candidate(
                    spec={
                        "v": 1,
                        "type": self.name,
                        "encoding": {
                            "latitude": {"column": latitude.name},
                            "longitude": {"column": longitude.name},
                        },
                        "options": {},
                    },
                    # A map is rarely the wrong suggestion when one exists
                    score=round(0.92 - null_penalty(null_rate, 0.4), 4),
                    reason=f"{latitude.name} and {longitude.name} are coordinates, {how}",
                    signals={"null_rate": null_rate},
                )
            )
        return candidates

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        latitude = encoding["latitude"]["column"]
        longitude = encoding["longitude"]["column"]
        size = encoding.get("size")
        if size and not size.get("aggregate"):
            size = {**size, "aggregate": "sum"}
        size_column = f", {quote(size['column'])} as v" if size else ""
        points = (
            f"select {quote(latitude)} as lat, {quote(longitude)} as lon{size_column}"
            " from _chart_source\n"
            f"  where {numeric_filter(latitude)} and {numeric_filter(longitude)}\n"
            f"  and {quote(latitude)} between -90 and 90"
            f" and {quote(longitude)} between -180 and 180"
        )
        meta = {"latitude": latitude, "longitude": longitude}
        if size:
            meta["size"] = aggregate_title(size)
        if self._raw(profile):
            meta["mode"] = "points"
            columns = "lat, lon" + (", v as size" if size else "")
            return CompiledQuery(
                sql=f"{source.with_clause()}\nselect {columns} from (\n  {points}\n)",
                params=merge_params(source, {}),
                max_rows=RAW_POINT_LIMIT,
                meta=meta,
            )
        cell = choose_cell(profile, latitude, longitude)
        meta.update({"mode": "grid", "cell": cell})
        size_sql = f", {aggregate_over(size)} as size" if size else ""
        sql = (
            f"{source.with_clause()}\n"
            "select round(lat / :chart_cell) * :chart_cell as lat,"
            " round(lon / :chart_cell) * :chart_cell as lon,"
            f" count(*) as n{size_sql}\n"
            f"from (\n  {points}\n)\n"
            f"group by 1, 2\norder by n desc\nlimit {MAX_CELLS + 1}"
        )
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, {"chart_cell": cell}),
            max_rows=MAX_CELLS,
            meta=meta,
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        title = "Map of {} and {}".format(
            encoding["latitude"]["column"], encoding["longitude"]["column"]
        )
        if encoding.get("size"):
            title += ", sized by {}".format(aggregate_title(encoding["size"]))
        return title
