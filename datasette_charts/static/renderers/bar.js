import {
  basePlot,
  mount,
  fmt,
  fmtCompact,
  isThumbnail,
  categoryName,
  uniqueNames,
  truncate,
  xLabel,
} from "./common.js";

const ROW_HEIGHT = 24; // bars stay well under 24px thick
const MAX_LABEL = 28;

// rows: key, is_other, value, and label when the category is a foreign key.
// Horizontal bars, in the order the server sorted them, with Other last.
export function render(el, ctx) {
  const { Plot, data, meta, theme } = ctx;
  const thumbnail = isThumbnail(ctx);
  let rows = data;
  if (thumbnail) rows = rows.slice(0, 8);
  const names = uniqueNames(rows.map((d) => categoryName(d.key, d.is_other, d.label)));
  const bars = rows.map((d, i) => ({ name: names[i], value: d.value, other: !!d.is_other }));

  const longest = Math.max(0, ...names.map((name) => Math.min(name.length, MAX_LABEL)));
  const marginLeft = thumbnail ? 6 : Math.min(220, 16 + longest * 6.6);
  const height = thumbnail ? ctx.height : Math.max(120, bars.length * ROW_HEIGHT + 56);
  const negative = bars.some((d) => d.value < 0);

  const marks = [
    Plot.barX(bars, {
      y: "name",
      x: "value",
      fill: (d) => (d.other ? theme.other : theme.series[0]),
      rx2: thumbnail ? 2 : 4,
      insetTop: thumbnail ? 1 : 3,
      insetBottom: thumbnail ? 1 : 3,
      ...(thumbnail ? {} : { title: (d) => `${d.name}\n${fmt(d.value)}`, tip: true }),
    }),
    Plot.ruleX([0], { stroke: theme.muted }),
  ];
  if (!thumbnail && !negative) {
    // The value at the tip of each bar
    marks.push(
      Plot.text(bars, {
        y: "name",
        x: "value",
        text: (d) => fmtCompact(d.value),
        textAnchor: "start",
        dx: 5,
        fill: theme.ink2,
      })
    );
  }

  const plot = Plot.plot(
    basePlot(ctx, {
      height,
      marginLeft,
      marginRight: thumbnail ? 6 : 56,
      x: { ...xLabel(meta.y), grid: !thumbnail, nice: true },
      y: {
        label: null,
        domain: names,
        tickFormat: (name) => truncate(name, MAX_LABEL),
        tickSize: 0,
      },
      marks,
    })
  );
  mount(el, ctx, plot);
}
