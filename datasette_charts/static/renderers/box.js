import { TIP, basePlot, mount, fmt, isThumbnail, categoryName, uniqueNames, truncate, xLabel, plural } from "./common.js";
import { annotate, bandMatcher } from "./annotations.js";

const ROW_HEIGHT = 28;
const MAX_LABEL = 28;

// rows: key, is_other, count, min, q1, median, q3, max, mean (and label for
// a foreign key), one per box, in the order the server sorted them. The
// quartiles were worked out in SQL; this draws them, lying flat like the
// bar chart, with whiskers out to the minimum and maximum.
export function render(el, ctx) {
  const { Plot, data, meta, theme } = ctx;
  const thumbnail = isThumbnail(ctx);
  let rows = data.filter((d) => d.median !== null && d.median !== undefined);
  if (thumbnail) rows = rows.slice(0, 8);
  const log = meta.scale === "log";
  if (log) rows = rows.filter((d) => d.min > 0);
  const names = uniqueNames(rows.map((d) => categoryName(d.key, d.is_other, d.label)));
  const boxes = rows.map((d, i) => ({ ...d, name: names[i], other: !!d.is_other }));
  const color = (d) => (d.other ? theme.other : theme.series[0]);
  const title = (d) =>
    [
      `${d.name}: ${plural(d.count, "value")}`,
      `Maximum ${fmt(d.max)}`,
      `Upper quartile ${fmt(d.q3)}`,
      `Median ${fmt(d.median)}`,
      `Lower quartile ${fmt(d.q1)}`,
      `Minimum ${fmt(d.min)}`,
      `Mean ${fmt(d.mean)}`,
    ].join("\n");

  const longest = Math.max(0, ...names.map((name) => Math.min(name.length, MAX_LABEL)));
  const marginLeft = thumbnail ? 6 : Math.min(220, 16 + longest * 6.6);
  const height = thumbnail ? ctx.height : Math.max(120, boxes.length * ROW_HEIGHT + 60);
  const band = thumbnail ? 1.5 : 5; // how much of each row the box leaves empty
  const marks = [
    // Whiskers: minimum to maximum
    Plot.ruleY(boxes, { y: "name", x1: "min", x2: "max", stroke: color, strokeOpacity: 0.8 }),
    Plot.tickX(boxes, { y: "name", x: "min", stroke: color, insetTop: band + 3, insetBottom: band + 3 }),
    Plot.tickX(boxes, { y: "name", x: "max", stroke: color, insetTop: band + 3, insetBottom: band + 3 }),
    // The box: lower to upper quartile
    Plot.barX(boxes, {
      y: "name",
      x1: "q1",
      x2: "q3",
      fill: color,
      fillOpacity: theme.mode === "dark" ? 0.45 : 0.3,
      stroke: color,
      insetTop: band,
      insetBottom: band,
      rx: thumbnail ? 0 : 2,
    }),
    // The median
    Plot.tickX(boxes, {
      y: "name",
      x: "median",
      stroke: theme.ink,
      strokeWidth: thumbnail ? 1.5 : 2.5,
      insetTop: band,
      insetBottom: band,
    }),
  ];
  if (!thumbnail) {
    marks.push(Plot.tip(boxes, Plot.pointerY({ y: "name", x: "q3", title, anchor: "left", ...TIP })));
  }
  const options = basePlot(ctx, {
    height,
    marginLeft,
    marginRight: thumbnail ? 6 : 24,
    x: {
      ...xLabel(log ? `${meta.y} (log scale)` : meta.y),
      type: log ? "log" : "linear",
      grid: !thumbnail,
      nice: !log,
      ...(log ? {} : { zero: false }),
    },
    y: {
      label: null,
      domain: names,
      tickFormat: (name) => truncate(name, MAX_LABEL),
      tickSize: 0,
    },
    marks,
  });
  const match = bandMatcher(
    names,
    rows.map((d) => (d.is_other ? null : d.key))
  );
  // A callout at a category points at its box's median
  const lookup = (annotation) => {
    const name = match(annotation.x);
    const box = boxes.find((d) => d.name === name);
    return box ? { x: name, y: box.median } : null;
  };
  const plot = annotate(el, ctx, (margins) => Plot.plot({ ...options, ...margins }), {
    // x is the category, down the side; y is the value, across
    x: { screen: "y", kind: "band", match },
    y: { screen: "x", kind: "number" },
    lookup,
    marks: (drawn) => {
      const sx = drawn.scale("x");
      const sy = drawn.scale("y");
      return {
        rects: boxes.map((d) => {
          const top = sy.apply(d.name);
          return { left: sx.apply(d.min), right: sx.apply(d.max), top, bottom: top + sy.bandwidth };
        }),
        lines: [],
      };
    },
  });
  mount(el, ctx, plot);
}
