import {
  TIP,
  basePlot,
  mount,
  fmt,
  isThumbnail,
  categoryName,
  seriesColor,
  truncate,
  bucketToDate,
  stepDate,
  fmtBucket,
  xLabel,
} from "./common.js";

const ROW_HEIGHT = 24;
const MAX_LABEL = 28;

// rows: key, key_is_other, series, series_is_other, value, plus key_label and
// series_label for foreign keys. With meta.timeUnit the keys are time buckets
// and the bars stand upright in time order; otherwise they lie flat, most
// common key first.
export function render(el, ctx) {
  const { Plot, data, meta, theme } = ctx;
  const thumbnail = isThumbnail(ctx);
  const unit = meta.timeUnit;
  const temporal = !!unit;
  const grouped = meta.layout === "grouped" && !temporal;

  const totals = new Map();
  const others = new Set();
  const keys = [];
  const items = [];
  for (const d of data) {
    const series = categoryName(d.series, d.series_is_other, d.series_label);
    if (d.series_is_other) others.add(series);
    totals.set(series, (totals.get(series) || 0) + Math.abs(d.value || 0));
    const item = { series, value: d.value };
    if (temporal) {
      item.date = bucketToDate(d.key, unit);
      if (!item.date) continue;
      item.end = stepDate(item.date, unit);
      item.key = fmtBucket(item.date, unit);
    } else {
      item.key = categoryName(d.key, d.key_is_other, d.key_label);
      if (!keys.includes(item.key)) keys.push(item.key);
    }
    items.push(item);
  }
  // Largest series first, Other last; segments are drawn in that order
  const domain = [...totals.keys()].sort(
    (a, b) => others.has(a) - others.has(b) || totals.get(b) - totals.get(a)
  );
  let slot = 0;
  const range = domain.map((name) => (others.has(name) ? theme.other : seriesColor(theme, slot++)));
  items.sort((a, b) => domain.indexOf(a.series) - domain.indexOf(b.series));

  const visibleKeys = thumbnail ? keys.slice(0, 8) : keys;
  const shown = temporal ? items : items.filter((d) => visibleKeys.includes(d.key));
  const tip = thumbnail
    ? {}
    : { title: (d) => `${d.key}\n${fmt(d.value)}  ${d.series}`, tip: TIP };
  const options = { color: { domain, range, legend: !thumbnail } };

  if (temporal) {
    Object.assign(options, {
      marginLeft: thumbnail ? 6 : 56,
      x: { type: "utc", label: null },
      y: { label: meta.y, grid: !thumbnail, nice: true },
      marks: [
        Plot.rectY(shown, {
          x1: "date",
          x2: "end",
          y: "value",
          fill: "series",
          insetLeft: 0.5,
          insetRight: 0.5,
          insetTop: 0.5,
          ...tip,
        }),
        Plot.ruleY([0], { stroke: theme.muted }),
      ],
    });
  } else {
    const longest = Math.max(0, ...visibleKeys.map((key) => Math.min(key.length, MAX_LABEL)));
    const perKey = grouped ? Math.max(ROW_HEIGHT, domain.length * 9 + 8) : ROW_HEIGHT;
    Object.assign(options, {
      height: thumbnail ? ctx.height : Math.max(120, visibleKeys.length * perKey + 56),
      marginLeft: thumbnail ? 6 : Math.min(220, 16 + longest * 6.6),
      marginRight: thumbnail ? 6 : 24,
      x: { ...xLabel(meta.y), grid: !thumbnail, nice: true },
    });
    const format = (key) => truncate(key, MAX_LABEL);
    if (grouped) {
      Object.assign(options, {
        fy: { domain: visibleKeys, label: null, tickFormat: format, padding: 0.15 },
        y: { axis: null, domain },
        marks: [
          Plot.barX(shown, { fy: "key", y: "series", x: "value", fill: "series", rx2: 2, ...tip }),
          Plot.ruleX([0], { stroke: theme.muted }),
        ],
      });
    } else {
      Object.assign(options, {
        y: { domain: visibleKeys, label: null, tickFormat: format, tickSize: 0 },
        marks: [
          // The 2px gap between segments does the separating, not a stroke
          Plot.barX(shown, {
            y: "key",
            x: "value",
            fill: "series",
            insetLeft: 1,
            insetRight: 1,
            insetTop: thumbnail ? 1 : 3,
            insetBottom: thumbnail ? 1 : 3,
            ...tip,
          }),
          Plot.ruleX([0], { stroke: theme.muted }),
        ],
      });
    }
  }
  mount(el, ctx, Plot.plot(basePlot(ctx, options)));
}
