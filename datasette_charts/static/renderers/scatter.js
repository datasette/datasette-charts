import { basePlot, mount, fmt, isThumbnail, looksLikeYears, xLabel } from "./common.js";

// meta.mode is "points" (rows: x, y) for small sources, or "bins"
// (rows: bx, by, n, with meta.x_bin and meta.y_bin) drawn as a heatmap.
export function render(el, ctx) {
  const { Plot, data, meta, theme } = ctx;
  const thumbnail = isThumbnail(ctx);
  const plainTicks = (name) => (looksLikeYears(name) ? (d) => String(d) : undefined);
  const options = {
    marginLeft: thumbnail ? 6 : 56,
    x: { ...xLabel(meta.x), grid: !thumbnail, nice: true, tickFormat: plainTicks(meta.x) },
    y: { label: meta.y, grid: !thumbnail, nice: true, tickFormat: plainTicks(meta.y) },
  };

  if (meta.mode === "points") {
    options.marks = [
      Plot.dot(data, {
        x: "x",
        y: "y",
        r: thumbnail ? 1.5 : 3,
        fill: theme.series[0],
        fillOpacity: 0.55,
        stroke: theme.surface,
        strokeWidth: thumbnail ? 0 : 0.75,
        // The pointer only has to be closest to a point, not on it
        ...(thumbnail
          ? {}
          : { title: (d) => `${meta.x}: ${fmt(d.x)}\n${meta.y}: ${fmt(d.y)}`, tip: true }),
      }),
    ];
  } else {
    const xb = meta.x_bin;
    const yb = meta.y_bin;
    const tidy = (value) => Number(value.toPrecision(12));
    const cells = data.map((d) => ({
      x1: tidy(xb.start + d.bx * xb.step),
      x2: tidy(xb.start + (d.bx + 1) * xb.step),
      y1: tidy(yb.start + d.by * yb.step),
      y2: tidy(yb.start + (d.by + 1) * yb.step),
      n: d.n,
    }));
    const counts = cells.map((d) => d.n);
    const skewed = counts.length && Math.max(...counts) / Math.max(1, Math.min(...counts)) > 20;
    // One hue, light to dark
    options.color = {
      type: skewed ? "log" : "linear",
      range: theme.sequential,
      interpolate: "lab",
      legend: !thumbnail,
      label: "Rows",
    };
    options.marks = [
      Plot.rect(cells, {
        x1: "x1",
        x2: "x2",
        y1: "y1",
        y2: "y2",
        fill: "n",
        inset: thumbnail ? 0 : 0.5,
        ...(thumbnail
          ? {}
          : {
              title: (d) =>
                `${fmt(d.n)} ${d.n === 1 ? "row" : "rows"}\n` +
                `${meta.x}: ${fmt(d.x1)} to ${fmt(d.x2)}\n${meta.y}: ${fmt(d.y1)} to ${fmt(d.y2)}`,
              tip: true,
            }),
      }),
    ];
  }
  mount(el, ctx, Plot.plot(basePlot(ctx, options)));
}
