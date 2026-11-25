from __future__ import annotations

from ..sources import quote
from ..sqlgen import (
    bin_params,
    bucket_sql,
    merge_params,
    numeric_filter,
    resolve_bin,
)
from .base import (
    BuiltinChartType,
    Candidate,
    Channel,
    CompiledQuery,
    best_measures,
    measure_quality,
)

# At or under this many rows a scatter plot draws every point
RAW_POINT_LIMIT = 2000
X_BINS = 40
Y_BINS = 24


class Scatter(BuiltinChartType):
    name = "scatter"
    label = "Scatter plot"
    description = (
        "Two numeric columns against each other: points for small sources,"
        " a binned heatmap for large ones"
    )
    icon = (
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<circle cx="6" cy="17" r="2"/><circle cx="10" cy="11" r="2"/><circle cx="14" cy="14" r="2"/>'
        '<circle cx="17" cy="7" r="2"/><circle cx="19" cy="12" r="2"/></svg>'
    )
    js_file = "scatter.js"
    channels = [
        Channel(name="x", label="X axis", roles=("measure",), binnable=True),
        Channel(name="y", label="Y axis", roles=("measure",), binnable=True),
    ]
    max_rows = RAW_POINT_LIMIT
    annotation_axes = {"x": "number", "y": "number"}
    # There is no one y for an x to look up
    annotation_lookup = False

    def _binned(self, profile) -> bool:
        if profile is None or profile.row_count is None:
            return True
        return profile.row_count > RAW_POINT_LIMIT

    def propose(self, profile) -> list:
        measures = best_measures(profile, 4)
        pairs = []
        for i, a in enumerate(measures):
            for b in measures[i + 1 :]:
                # Position order, so the same pair is never proposed both ways
                x, y = sorted((a, b), key=lambda column: column.position)
                quality = 0.5 * (measure_quality(a) + measure_quality(b))
                # Nothing here says the two are related: that needs a verify step
                pairs.append((round(0.42 + 0.3 * (quality - 0.5), 4), x, y))
        pairs.sort(key=lambda t: (-t[0], t[1].position, t[2].position))
        binned = self._binned(profile)
        candidates = []
        for score, x, y in pairs[:3]:
            encoding = {"x": {"column": x.name}, "y": {"column": y.name}}
            if binned:
                # Explicit bins from the profile, so this compiles to one cheap query
                encoding["x"]["bin"] = resolve_bin({"maxbins": X_BINS}, x)
                encoding["y"]["bin"] = resolve_bin({"maxbins": Y_BINS}, y)
                how = "binned into a heatmap"
            else:
                how = "one point per row"
            candidates.append(
                Candidate(
                    spec={
                        "v": 1,
                        "type": self.name,
                        "encoding": encoding,
                        "options": {},
                    },
                    score=score,
                    reason=f"{y.name} against {x.name}, {how}",
                    signals={"binned": binned},
                )
            )
        return candidates

    def compile(self, spec, source, profile) -> CompiledQuery:
        encoding = spec["encoding"]
        x, y = encoding["x"], encoding["y"]
        qx, qy = quote(x["column"]), quote(y["column"])
        points = (
            f"select {qx} as x, {qy} as y from source\n"
            f"  where {numeric_filter(x['column'])} and {numeric_filter(y['column'])}"
        )
        meta = {"x": x["column"], "y": y["column"]}
        binned = self._binned(profile) or "bin" in x or "bin" in y
        if not binned:
            meta["mode"] = "points"
            return CompiledQuery(
                sql=f"{source.with_clause()}\nselect x, y from (\n  {points}\n)",
                params=merge_params(source, {}),
                max_rows=RAW_POINT_LIMIT,
                meta=meta,
            )
        x_bin = resolve_bin(
            x.get("bin") or {"maxbins": X_BINS},
            profile.column(x["column"]) if profile else None,
        )
        y_bin = resolve_bin(
            y.get("bin") or {"maxbins": Y_BINS},
            profile.column(y["column"]) if profile else None,
        )
        meta.update({"mode": "bins", "x_bin": x_bin, "y_bin": y_bin})
        sql = (
            f"{source.with_clause()}\n"
            f"select {bucket_sql('x', 'chart_x')} as bx,"
            f" {bucket_sql('y', 'chart_y')} as by, count(*) as n\n"
            f"from (\n  {points}\n)\ngroup by 1, 2"
        )
        params = {**bin_params("chart_x", x_bin), **bin_params("chart_y", y_bin)}
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, params),
            max_rows=(x_bin["count"] + 2) * (y_bin["count"] + 2),
            meta=meta,
        )

    def title(self, spec) -> str:
        encoding = spec["encoding"]
        return "{} against {}".format(encoding["y"]["column"], encoding["x"]["column"])
