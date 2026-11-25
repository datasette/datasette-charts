import {
  TIP,
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
import { annotate, bandMatcher } from "./annotations.js";

const ROW_HEIGHT = 24; // bars stay well under 24px thick
const MAX_LABEL = 28;

// rows: key, is_other, value, and label when the category is a foreign key.
// Bars in the order the server sorted them, with Other last: lying flat by
// default, or standing upright with the vertical orientation option.
export function render(el, ctx) {
  const { Plot, data, meta, theme, spec } = ctx;
  const thumbnail = isThumbnail(ctx);
  const vertical = (spec.options || {}).orientation === "vertical";
  let rows = data;
  if (thumbnail) rows = rows.slice(0, vertical ? 16 : 8);
  const names = uniqueNames(rows.map((d) => categoryName(d.key, d.is_other, d.label)));
  const bars = rows.map((d, i) => ({ name: names[i], value: d.value, other: !!d.is_other }));
  const negative = bars.some((d) => d.value < 0);
  const fill = (d) => (d.other ? theme.other : theme.series[0]);
  const tip = thumbnail ? {} : { title: (d) => `${d.name}\n${fmt(d.value)}`, tip: TIP };
  const match = bandMatcher(
    names,
    rows.map((d) => (d.is_other ? null : d.key))
  );
  const lookup = (annotation) => {
    const name = match(annotation.x);
    const bar = bars.find((d) => d.name === name);
    return bar && bar.value !== null ? { x: name, y: bar.value } : null;
  };

  let plot;
  if (vertical) {
    const plotWidth = ctx.width - 56 - 16;
    const step = plotWidth / Math.max(1, bars.length);
    const longest = Math.max(0, ...names.map((name) => Math.min(name.length, MAX_LABEL)));
    // Labels that would run into each other are turned
    const rotate = !thumbnail && longest * 6.6 > step - 4;
    const marks = [
      Plot.barY(bars, {
        x: "name",
        y: "value",
        fill,
        ry1: thumbnail ? 1 : 2,
        insetLeft: thumbnail ? 0.5 : 1,
        insetRight: thumbnail ? 0.5 : 1,
        ...tip,
      }),
      Plot.ruleY([0], { stroke: theme.muted }),
    ];
    if (!thumbnail && !negative && step >= 30) {
      marks.push(
        Plot.text(bars, {
          x: "name",
          y: "value",
          text: (d) => fmtCompact(d.value),
          dy: -7,
          fill: theme.ink2,
        })
      );
    }
    plot = Plot.plot(
      basePlot(ctx, {
        marginLeft: thumbnail ? 6 : 56,
        marginRight: thumbnail ? 6 : 16,
        marginTop: thumbnail ? 6 : 20,
        marginBottom: thumbnail ? 6 : rotate ? Math.min(140, 20 + longest * 5) : 44,
        x: {
          label: null,
          domain: names,
          tickFormat: (name) => truncate(name, MAX_LABEL),
          tickSize: 0,
          tickRotate: rotate ? -40 : 0,
          padding: 0.15,
        },
        y: { label: meta.y, grid: !thumbnail, nice: true },
        marks,
      })
    );
    annotate(el, ctx, plot, {
      x: { screen: "x", kind: "band", match },
      y: { screen: "y", kind: "number" },
      lookup,
    });
  } else {
    const longest = Math.max(0, ...names.map((name) => Math.min(name.length, MAX_LABEL)));
    const marginLeft = thumbnail ? 6 : Math.min(220, 16 + longest * 6.6);
    const height = thumbnail ? ctx.height : Math.max(120, bars.length * ROW_HEIGHT + 56);
    const marks = [
      Plot.barX(bars, {
        y: "name",
        x: "value",
        fill,
        rx2: thumbnail ? 2 : 4,
        insetTop: thumbnail ? 1 : 3,
        insetBottom: thumbnail ? 1 : 3,
        ...tip,
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
    plot = Plot.plot(
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
    annotate(el, ctx, plot, {
      x: { screen: "y", kind: "band", match },
      y: { screen: "x", kind: "number" },
      lookup,
    });
  }
  mount(el, ctx, plot);
}
