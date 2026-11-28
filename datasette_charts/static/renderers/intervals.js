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
  stepDate,
  valueAxisMargin,
  plural,
} from "./common.js";
import { annotate, parseMoment } from "./annotations.js";

// rows: bucket, value, after - or bucket, series, is_other, value, after
// (and series_label for a foreign key). `value` is how many rows were
// active at some point in the bucket, `after` how many are still active
// once it is over. Buckets with no starts or ends are not sent: the count
// holds at `after` until the next bucket that is, so it is drawn as a step.
export function render(el, ctx) {
  const { Plot, data, meta, theme } = ctx;
  const unit = meta.timeUnit;
  const thumbnail = isThumbnail(ctx);
  const hasSeries = ctx.columns.includes("series");
  const SINGLE = "\u0000";

  // Each series' events, in time order
  const groups = new Map();
  for (const d of data) {
    const date = bucketToDate(d.bucket, unit);
    if (!date) continue;
    const name = hasSeries ? categoryName(d.series, d.is_other, d.series_label) : SINGLE;
    if (!groups.has(name)) groups.set(name, { name, other: !!d.is_other, peak: 0, events: [] });
    const group = groups.get(name);
    group.events.push({ date, value: d.value, after: d.after });
    group.peak = Math.max(group.peak, d.value);
  }
  const series = [...groups.values()].sort((a, b) => a.other - b.other || b.peak - a.peak);

  // The moments where any count changes: every bucket sent, and the bucket
  // after each one, where the rows that ended in it drop out
  const times = new Set();
  for (const group of series) {
    for (const event of group.events) {
      times.add(event.date.getTime());
      times.add(stepDate(event.date, unit).getTime());
    }
  }
  const moments = [...times].sort((a, b) => a - b).map((time) => new Date(time));

  // Each series' count at each of those moments
  const points = [];
  for (const group of series) {
    const at = new Map(group.events.map((event) => [event.date.getTime(), event]));
    let current = 0;
    for (const date of moments) {
      const event = at.get(date.getTime());
      const value = event ? event.value : current;
      if (event) current = event.after;
      points.push({ date, series: group.name, value });
    }
  }
  const totals = moments.map((date, i) => ({
    date,
    value: series.reduce((sum, group, j) => sum + points[j * moments.length + i].value, 0),
    parts: series.map((group, j) => ({ name: group.name, value: points[j * moments.length + i].value })),
  }));

  const stacked = hasSeries && meta.layout !== "lines";
  const strokeWidth = thumbnail ? 1.5 : 2;
  const marks = [Plot.ruleY([0], { stroke: theme.muted })];
  const options = {
    marginLeft: thumbnail ? 6 : valueAxisMargin(totals.map((d) => (stacked ? d.value : Math.max(0, ...d.parts.map((p) => p.value)))), getComputedStyle(el).fontFamily),
    x: { type: "utc", label: null },
    y: { label: meta.y, grid: !thumbnail, nice: true, zero: true },
  };
  if (!hasSeries) {
    const color = theme.series[0];
    marks.push(Plot.areaY(points, { x: "date", y: "value", curve: "step-after", fill: color, fillOpacity: 0.14 }));
    marks.push(Plot.lineY(points, { x: "date", y: "value", curve: "step-after", stroke: color, strokeWidth }));
  } else {
    const domain = series.map((group) => group.name);
    let slot = 0;
    const range = series.map((group) => (group.other ? theme.other : seriesColor(theme, slot++)));
    options.color = { domain, range, legend: !thumbnail };
    if (stacked) {
      marks.push(
        Plot.areaY(points, {
          x: "date",
          y: "value",
          z: "series",
          fill: "series",
          order: domain,
          curve: "step-after",
          fillOpacity: 0.85,
        })
      );
    } else {
      marks.push(
        Plot.lineY(points, { x: "date", y: "value", z: "series", stroke: "series", curve: "step-after", strokeWidth })
      );
    }
  }
  if (!thumbnail) {
    const text = (d) => {
      const lines = [`${fmtBucket(d.date, unit)}: ${plural(d.value, "row")} active`];
      if (hasSeries) for (const part of d.parts) if (part.value) lines.push(`${fmt(part.value)}  ${part.name}`);
      return lines.join("\n");
    };
    const top = (d) => (stacked || !hasSeries ? d.value : Math.max(0, ...d.parts.map((p) => p.value)));
    marks.push(Plot.ruleX(totals, Plot.pointerX({ x: "date", stroke: theme.muted })));
    marks.push(Plot.tip(totals, Plot.pointerX({ x: "date", y: top, title: text, ...TIP })));
  }
  options.marks = marks;

  // The total active in the bucket that holds a moment
  const lookup = (annotation) => {
    const period = parseMoment(annotation.x);
    if (!period) return null;
    const time = period.start.getTime();
    let found = null;
    for (const d of totals) {
      if (d.date.getTime() <= time) found = d;
      else break;
    }
    return found ? { x: period.start, y: stacked || !hasSeries ? found.value : Math.max(0, ...found.parts.map((p) => p.value)) } : null;
  };
  const plot = annotate(el, ctx, (margins) => Plot.plot(basePlot(ctx, { ...options, ...margins })), {
    x: { screen: "x", kind: "time" },
    y: { screen: "y", kind: "number" },
    lookup,
    // Where the data is drawn, so labels keep off it: the whole filled area
    // under the steps (one box per step, down to zero) for a single series
    // or a stack, or each series' own steps when they are drawn as lines
    marks: (drawn) => {
      const sx = drawn.scale("x");
      const sy = drawn.scale("y");
      if (hasSeries && !stacked) {
        const lines = series.map((group, j) => {
          const line = [];
          moments.forEach((date, i) => {
            const Y = sy.apply(points[j * moments.length + i].value);
            if (i) line.push({ X: sx.apply(date), Y: line[line.length - 1].Y });
            line.push({ X: sx.apply(date), Y });
          });
          return line;
        });
        return { rects: [], lines: lines.filter((line) => line.length) };
      }
      const base = sy.apply(0);
      const rects = [];
      totals.forEach((d, i) => {
        const next = totals[i + 1];
        if (!next || !(d.value > 0)) return;
        const top = sy.apply(d.value);
        rects.push({ left: sx.apply(d.date), right: sx.apply(next.date), top: Math.min(top, base), bottom: Math.max(top, base) });
      });
      return { rects, lines: [] };
    },
  });
  mount(el, ctx, plot);
}
