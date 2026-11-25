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
  fitBandLabels,
  orderedValues,
  textWidth,
  valueAxisMargin,
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
    const fontFamily = getComputedStyle(el).fontFamily || "system-ui, sans-serif";
    const plotWidth = ctx.width - valueAxisMargin(bars.map((d) => d.value), fontFamily) - 16;
    const step = plotWidth / Math.max(1, bars.length);
    // Labels that would run into each other are turned, made smaller, or
    // for years and the like, thinned out
    const shownNames = names.map((name) => truncate(name, MAX_LABEL));
    const realKeys = rows.filter((d) => !d.is_other).map((d) => d.key);
    const fit = fitBandLabels(shownNames, {
      step,
      fontFamily,
      ordered: orderedValues(realKeys),
    });
    const keep = new Set(names.filter((name, i) => i % fit.every === 0 || rows[i].is_other));
    const marks = [
      ...(thumbnail
        ? []
        : [
            Plot.axisX({
              label: null,
              tickSize: 0,
              tickRotate: -fit.angle,
              fontSize: fit.fontSize,
              ticks: names.filter((name) => keep.has(name)),
              tickFormat: (name) => truncate(name, MAX_LABEL),
            }),
          ]),
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
    // Values above the bars, smaller or left out when they would run into
    // their neighbours
    const widestValue = (size) =>
      Math.max(0, ...bars.map((d) => textWidth(fmtCompact(d.value), `${size}px ${fontFamily}`)));
    const valueSize = thumbnail || negative ? null : [12, 11, 10].find((size) => widestValue(size) + 4 <= step) || null;
    const showValues = valueSize !== null;
    if (showValues) {
      marks.push(
        Plot.text(bars, {
          x: "name",
          y: "value",
          text: (d) => fmtCompact(d.value),
          dy: -7,
          fontSize: valueSize,
          fill: theme.ink2,
        })
      );
    }
    const options = basePlot(ctx, {
        // Turned labels reach left of the first bar
        marginLeft: thumbnail
          ? 6
          : Math.max(valueAxisMargin(bars.map((d) => d.value), fontFamily), Math.min(160, fit.overhang - step / 2 + 8)),
        marginRight: thumbnail ? 6 : 16,
        marginTop: thumbnail ? 6 : 20,
        marginBottom: thumbnail ? 6 : fit.marginBottom,
        x: {
          label: null,
          domain: names,
          padding: 0.15,
          ...(thumbnail ? {} : { axis: null }),
        },
        y: { label: meta.y, grid: !thumbnail, nice: true },
        marks,
      });
    plot = annotate(el, ctx, (margins) => Plot.plot({ ...options, ...margins }), {
      x: { screen: "x", kind: "band", match },
      y: { screen: "y", kind: "number" },
      lookup,
      // Each bar, and the value written above it
      marks: (drawn) => {
        const sx = drawn.scale("x");
        const sy = drawn.scale("y");
        return {
          rects: bars
            .filter((d) => d.value !== null)
            .map((d) => {
              const left = sx.apply(d.name);
              const [a, b] = [sy.apply(0), sy.apply(d.value)];
              return { left, right: left + sx.bandwidth, top: Math.min(a, b) - (showValues ? 16 : 0), bottom: Math.max(a, b) };
            }),
          lines: [],
        };
      },
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
    const options = basePlot(ctx, {
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
      });
    plot = annotate(el, ctx, (margins) => Plot.plot({ ...options, ...margins }), {
      x: { screen: "y", kind: "band", match },
      y: { screen: "x", kind: "number" },
      lookup,
      // Each bar, and the value written at its tip
      marks: (drawn) => {
        const sx = drawn.scale("x");
        const sy = drawn.scale("y");
        return {
          rects: bars
            .filter((d) => d.value !== null)
            .map((d) => {
              const top = sy.apply(d.name);
              const [a, b] = [sx.apply(0), sx.apply(d.value)];
              return { left: Math.min(a, b), right: Math.max(a, b) + (negative ? 0 : 44), top, bottom: top + sy.bandwidth };
            }),
          lines: [],
        };
      },
    });
  }
  mount(el, ctx, plot);
}
