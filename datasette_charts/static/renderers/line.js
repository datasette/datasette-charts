import {
  TIP,
  basePlot,
  mount,
  fmt,
  isThumbnail,
  categoryName,
  seriesColor,
  bucketToDate,
  fmtBucket,
  allBuckets,
  stepDate,
  valueAxisMargin,
} from "./common.js";
import { annotate, parseMoment } from "./annotations.js";

// Aggregates where a time bucket with no rows really is zero
const ZERO_WHEN_EMPTY = new Set(["count", "sum", "count_distinct"]);

// rows: bucket, value - or bucket, series, is_other, value (and series_label
// when the series is a foreign key). meta.timeUnit says how to read buckets.
export function render(el, ctx) {
  const { Plot, data, meta, spec, theme } = ctx;
  const unit = meta.timeUnit;
  const thumbnail = isThumbnail(ctx);
  const aggregate = (spec.encoding.y && spec.encoding.y.aggregate) || "count";
  const fillZero = ZERO_WHEN_EMPTY.has(aggregate);
  const hasSeries = ctx.columns.includes("series");
  const SINGLE = "\u0000";

  // Group the rows by series, keyed by bucket time
  const groups = new Map();
  for (const d of data) {
    const date = bucketToDate(d.bucket, unit);
    if (!date) continue;
    const name = hasSeries ? categoryName(d.series, d.is_other, d.series_label) : SINGLE;
    if (!groups.has(name)) groups.set(name, { name, other: !!d.is_other, total: 0, values: new Map() });
    const group = groups.get(name);
    group.values.set(date.getTime(), d.value);
    group.total += Math.abs(d.value || 0);
  }
  // Largest series first, with Other always last
  const series = [...groups.values()].sort((a, b) => a.other - b.other || b.total - a.total);

  // Missing buckets are filled in, so gaps show as zero (or as a break in
  // the line, for averages and the like) and are not skipped over
  const times = new Set();
  for (const group of series) for (const time of group.values.keys()) times.add(time);
  const present = [...times].sort((a, b) => a - b).map((time) => new Date(time));
  const buckets = allBuckets(present, unit) || present;
  const points = [];
  for (const group of series) {
    for (const date of buckets) {
      const value = group.values.get(date.getTime());
      points.push({
        date,
        series: group.name,
        value: value === undefined ? (fillZero ? 0 : null) : value,
      });
    }
  }

  const marks = [];
  const options = {
    marginLeft: thumbnail ? 6 : valueAxisMargin(points.map((d) => d.value), getComputedStyle(el).fontFamily),
    x: { type: "utc", label: null },
    y: { label: meta.y, grid: !thumbnail, nice: true, zero: fillZero },
  };
  const strokeWidth = thumbnail ? 1.5 : 2;
  const withMarkers = !thumbnail && buckets.length <= 12;

  if (!hasSeries) {
    const color = theme.series[0];
    if (fillZero) {
      marks.push(Plot.areaY(points, { x: "date", y: "value", fill: color, fillOpacity: 0.1 }));
      marks.push(Plot.ruleY([0], { stroke: theme.muted }));
    }
    marks.push(Plot.lineY(points, { x: "date", y: "value", stroke: color, strokeWidth }));
    if (withMarkers || buckets.length === 1) {
      marks.push(
        Plot.dot(points, { x: "date", y: "value", fill: color, r: 4, stroke: theme.surface, strokeWidth: 2 })
      );
    }
    if (!thumbnail) {
      const hover = points.filter((d) => d.value !== null);
      marks.push(Plot.ruleX(hover, Plot.pointerX({ x: "date", stroke: theme.muted })));
      marks.push(
        Plot.dot(
          hover,
          Plot.pointerX({ x: "date", y: "value", fill: color, r: 4, stroke: theme.surface, strokeWidth: 2 })
        )
      );
      marks.push(
        Plot.tip(
          hover,
          Plot.pointerX({
            x: "date",
            y: "value",
            title: (d) => `${fmtBucket(d.date, unit)}\n${fmt(d.value)}`,
            ...TIP,
          })
        )
      );
    }
  } else {
    const domain = series.map((group) => group.name);
    let slot = 0;
    const range = series.map((group) => (group.other ? theme.other : seriesColor(theme, slot++)));
    options.color = { domain, range, legend: !thumbnail };
    if (fillZero) marks.push(Plot.ruleY([0], { stroke: theme.muted }));
    marks.push(
      Plot.lineY(points, { x: "date", y: "value", z: "series", stroke: "series", strokeWidth })
    );
    if (withMarkers) {
      marks.push(
        Plot.dot(points, { x: "date", y: "value", fill: "series", r: 4, stroke: theme.surface, strokeWidth: 2 })
      );
    }
    if (!thumbnail) {
      // One tooltip listing every series at that point in time
      const byDate = buckets.map((date) => {
        const here = points.filter((d) => d.date === date && d.value !== null);
        return {
          date,
          top: Math.max(0, ...here.map((d) => d.value)),
          text: [fmtBucket(date, unit), ...here.map((d) => `${fmt(d.value)}  ${d.series}`)].join("\n"),
        };
      });
      marks.push(Plot.ruleX(byDate, Plot.pointerX({ x: "date", stroke: theme.muted })));
      marks.push(Plot.tip(byDate, Plot.pointerX({ x: "date", y: "top", title: (d) => d.text, ...TIP })));
    }
  }

  options.marks = marks;
  const plot = annotate(el, ctx, (margins) => Plot.plot(basePlot(ctx, { ...options, ...margins })), {
    // Each series as drawn, broken where values are missing
    marks: (drawn) => {
      const sx = drawn.scale("x");
      const sy = drawn.scale("y");
      const lines = [];
      for (const group of series) {
        let run = [];
        for (const d of points) {
          if (d.series !== group.name) continue;
          if (d.value === null) {
            if (run.length) lines.push(run);
            run = [];
          } else {
            run.push({ X: sx.apply(d.date), Y: sy.apply(d.value) });
          }
        }
        if (run.length) lines.push(run);
      }
      return { rects: [], lines };
    },
    x: { screen: "x", kind: "time" },
    y: { screen: "y", kind: "number" },
    // The value in the bucket that holds x, of the series named or the largest
    lookup: (annotation) => {
      const period = parseMoment(annotation.x);
      if (!period) return null;
      const name = hasSeries ? (annotation.series ?? (series[0] && series[0].name)) : SINGLE;
      const time = period.start.getTime();
      const point = points.find(
        (d) =>
          d.series === String(name) &&
          d.date.getTime() <= time &&
          time < stepDate(d.date, unit).getTime() &&
          d.value !== null
      );
      return point ? { x: point.date, y: point.value } : null;
    },
  });
  mount(el, ctx, plot);
}
