from __future__ import annotations

from ..profiler.roles import has_year_name_hint, name_tokens
from ..sources import quote
from ..sqlgen import (
    SpecError,
    bin_params,
    bucket_sql,
    fmt_number,
    log_bucket_sql,
    log_edges,
    merge_params,
    nice_bins,
    numeric_filter,
    resolve_bin,
    robust_range,
    wants_log_scale,
)
from .base import (
    MEASURE_NAME_TOKENS,
    BuiltinChartType,
    Candidate,
    Channel,
    CompiledQuery,
    null_penalty,
)

DEFAULT_MAXBINS = 30
# A measure with fewer distinct values than this reads better as a bar chart
MIN_DISTINCT = 8


class Histogram(BuiltinChartType):
    name = "histogram"
    label = "Histogram"
    description = "How the values of one numeric column are distributed"
    icon = (
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<rect x="3" y="14" width="3.5" height="7"/><rect x="7.5" y="8" width="3.5" height="13"/>'
        '<rect x="12" y="4" width="3.5" height="17"/><rect x="16.5" y="11" width="3.5" height="10"/></svg>'
    )
    js_file = "histogram.js"
    channels = [
        Channel(name="x", label="Column", roles=("measure",), binnable=True),
    ]
    options = {
        "scale": {"label": "Scale", "values": ["linear", "log"], "default": "linear"}
    }
    max_rows = 202
    # y is the number of rows in each bin
    annotation_axes = {"x": "number", "y": "number"}

    def propose(self, profile) -> list:
        candidates = []
        for column in profile.columns:
            if column.role != "measure":
                continue
            numeric = column.numeric or {}
            if numeric.get("min") is None or numeric["min"] == numeric["max"]:
                continue
            distinct = column.distinct_count
            if distinct is not None and distinct < MIN_DISTINCT:
                continue
            plain = has_year_name_hint(column.name)
            reason = "{} ranges from {} to {}".format(
                column.name,
                fmt_number(numeric["min"], plain),
                fmt_number(numeric["max"], plain),
            )
            quantiles = numeric.get("quantiles")
            if quantiles:
                reason += "; half of its values are between {} and {}".format(
                    fmt_number(quantiles["p25"], plain),
                    fmt_number(quantiles["p75"], plain),
                )

            score = 0.55
            named = bool(MEASURE_NAME_TOKENS.intersection(name_tokens(column.name)))
            if named:
                score += 0.08
            score -= null_penalty(column.null_rate)

            x = {"column": column.name}
            options = {}
            log = wants_log_scale(numeric) and log_edges(numeric)
            if log:
                # Most values are small: linear bins would be one tall bar
                options["scale"] = "log"
                reason += ", so the bins are on a log scale"
                bin_count = len(log) - 1
                long_tail = True
            else:
                lo, hi, clipped_low, clipped_high = robust_range(numeric)
                integer = column.type_counts.get("real", 0) == 0
                # Explicit bins from the profile: one cheap query to draw this
                x["bin"] = nice_bins(lo, hi, DEFAULT_MAXBINS, integer)
                bin_count = x["bin"]["count"]
                long_tail = clipped_low or clipped_high
                if not quantiles:
                    reason += "; {} bins of {}".format(
                        bin_count, fmt_number(x["bin"]["step"])
                    )
            if bin_count < 4:
                continue
            if long_tail:
                # The shape of a long-tailed distribution is worth seeing
                score += 0.03
            candidates.append(
                Candidate(
                    spec={
                        "v": 1,
                        "type": self.name,
                        "encoding": {"x": x},
                        "options": options,
                    },
                    score=round(score, 4),
                    reason=reason,
                    signals={
                        "name_hint": named,
                        "null_rate": column.null_rate,
                        "long_tail": bool(long_tail),
                        "log_scale": bool(log),
                        "bins": bin_count,
                    },
                )
            )
        return candidates

    def compile(self, spec, source, profile) -> CompiledQuery:
        x = spec["encoding"]["x"]
        column = x["column"]
        column_profile = profile.column(column) if profile else None
        points = (
            f"select {quote(column)} as x from _chart_source"
            f" where {numeric_filter(column)}"
        )

        if (spec.get("options") or {}).get("scale") == "log":
            edges = log_edges(column_profile.numeric if column_profile else None)
            if not edges:
                raise SpecError(
                    f"{column} cannot use a log scale: that needs numbers of zero or more"
                )
            sql = (
                f"{source.with_clause()}\n"
                f"select {log_bucket_sql('x', edges)} as bucket, count(*) as n\n"
                f"from ({points})\ngroup by 1\norder by 1"
            )
            return CompiledQuery(
                sql=sql,
                params=merge_params(source, {}),
                # One bin under the first edge and one over the last
                max_rows=len(edges) + 1,
                meta={"scale": "log", "edges": edges, "column": column},
            )

        bins = resolve_bin(x.get("bin"), column_profile, DEFAULT_MAXBINS)
        if bins["step"] <= 0:
            raise SpecError("bin step must be above zero")
        sql = (
            f"{source.with_clause()}\n"
            f"select {bucket_sql('x', 'chart_x')} as bucket, count(*) as n\n"
            f"from ({points})\n"
            "group by 1\norder by 1"
        )
        return CompiledQuery(
            sql=sql,
            params=merge_params(source, bin_params("chart_x", bins)),
            # One bin below the range and one above it
            max_rows=bins["count"] + 2,
            meta={"scale": "linear", "bin": bins, "column": column},
        )

    def title(self, spec) -> str:
        return "Distribution of {}".format(spec["encoding"]["x"]["column"])
