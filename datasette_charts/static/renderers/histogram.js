import { TIP, basePlot, mount, fmt, isThumbnail, looksLikeYears, xLabel } from "./common.js";
import { annotate } from "./annotations.js";

// rows: bucket, n.
//
// Linear scale: meta.bin is {start, step, count}. Bucket -1 holds values
// below the binned range and bucket `count` holds values above it.
//
// Log scale: meta.edges is a list of bin edges. Bucket i runs from edges[i]
// to edges[i + 1]; bucket -1 holds values under the first edge (zero
// included) and the last bucket holds values over the last edge.
//
// Open-ended bins are drawn lighter and labelled as open-ended.
export function render(el, ctx) {
  const { Plot, data, meta, theme } = ctx;
  const thumbnail = isThumbnail(ctx);
  const log = meta.scale === "log";
  const plain = looksLikeYears(meta.column);
  const tidy = (value) => Number(value.toPrecision(12));
  const edge = (value) => (plain ? String(tidy(value)) : fmt(tidy(value)));

  let range;
  if (log) {
    const edges = meta.edges;
    const last = edges.length - 1;
    range = (bucket) => {
      if (bucket < 0) return { x1: edges[0] / (edges[1] / edges[0]), x2: edges[0], open: "below" };
      if (bucket >= last) return { x1: edges[last], x2: edges[last] * (edges[last] / edges[last - 1]), open: "above" };
      return { x1: edges[bucket], x2: edges[bucket + 1] };
    };
  } else {
    const { start, step, count } = meta.bin;
    range = (bucket) => {
      const x1 = tidy(start + bucket * step);
      const open = bucket < 0 ? "below" : bucket >= count ? "above" : undefined;
      return { x1, x2: tidy(x1 + step), open };
    };
  }

  const bins = data.map((d) => {
    const bin = range(d.bucket);
    let label = `${edge(bin.x1)} to ${edge(bin.x2)}`;
    if (bin.open === "below") label = `Under ${edge(bin.x2)}`;
    if (bin.open === "above") label = `${edge(bin.x1)} and over`;
    return { ...bin, n: d.n, label };
  });

  const options = basePlot(ctx, {
      marginLeft: thumbnail ? 6 : 56,
      x: {
        ...xLabel(meta.column),
        type: log ? "log" : "linear",
        tickFormat: log ? (d) => fmt(d) : plain ? (d) => String(d) : undefined,
      },
      y: { label: "Rows", grid: !thumbnail, nice: true },
      marks: [
        Plot.rectY(bins, {
          x1: "x1",
          x2: "x2",
          y: "n",
          fill: theme.series[0],
          fillOpacity: (d) => (d.open ? 0.45 : 1),
          insetLeft: thumbnail ? 0.5 : 1,
          insetRight: thumbnail ? 0.5 : 1,
          ry2: thumbnail ? 0 : 2,
          ...(thumbnail
            ? {}
            : { title: (d) => `${d.label}\n${fmt(d.n)} ${d.n === 1 ? "row" : "rows"}`, tip: TIP }),
        }),
        Plot.ruleY([0], { stroke: theme.muted }),
      ],
    });
  const plot = annotate(el, ctx, (margins) => Plot.plot({ ...options, ...margins }), {
    x: { screen: "x", kind: "number" },
    y: { screen: "y", kind: "number" },
    marks: (drawn) => {
      const sx = drawn.scale("x");
      const sy = drawn.scale("y");
      return {
        rects: bins.map((d) => ({ left: sx.apply(d.x1), right: sx.apply(d.x2), top: sy.apply(d.n), bottom: sy.apply(0) })),
        lines: [],
      };
    },
    // The bin that holds x: the point goes on top of its bar
    lookup: (annotation) => {
      const bin = bins.find((d) => d.x1 <= annotation.x && annotation.x < d.x2);
      return bin ? { x: (bin.x1 + bin.x2) / 2, y: bin.n } : null;
    },
  });
  mount(el, ctx, plot);
}
