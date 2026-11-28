import { TIP, MONTHS, basePlot, mount, fmt, isThumbnail } from "./common.js";
import { annotate } from "./annotations.js";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
// Aggregates where a day with no rows really is zero
const ZERO_WHEN_EMPTY = new Set(["count", "sum", "count_distinct"]);

// The week starts on Monday
const mondayFirst = (weekday) => (weekday + 6) % 7;

function parseDay(text) {
  const match = /^(\d{4})-(\d\d)-(\d\d)$/.exec(String(text));
  return match ? new Date(Date.UTC(+match[1], +match[2] - 1, +match[3])) : null;
}

// Where a day goes: its year's block, the week column (the week holding
// 1 January is column 0) and its weekday row
function place(date) {
  const year = date.getUTCFullYear();
  const jan1 = new Date(Date.UTC(year, 0, 1));
  const offset = mondayFirst(jan1.getUTCDay());
  const dayOfYear = Math.round((date - jan1) / 86400000);
  return { year, week: Math.floor((dayOfYear + offset) / 7), day: mondayFirst(date.getUTCDay()) };
}

// Days with no rows are drawn in this, so the grid stays visible
function emptyCell(theme) {
  return theme.mode === "dark" ? "rgba(255, 255, 255, 0.06)" : "rgba(0, 0, 0, 0.045)";
}

// rows: day, value, for every day with rows in the most recent years.
// Everything was counted in SQL: this only lays the days out, one block per
// year, newest at the top, weeks across and weekdays down.
export function render(el, ctx) {
  const { Plot, data, meta, theme, spec } = ctx;
  const thumbnail = isThumbnail(ctx);
  const aggregate = (spec.encoding.y && spec.encoding.y.aggregate) || "count";
  const fillZero = ZERO_WHEN_EMPTY.has(aggregate);
  const days = [];
  for (const d of data) {
    const date = parseDay(d.day);
    if (date) days.push({ date, value: d.value, ...place(date) });
  }
  const values = new Map(days.map((d) => [d.date.getTime(), d.value]));
  const last = days.length ? days[days.length - 1].date : new Date();
  let years = [...new Set(days.map((d) => d.year))].sort((a, b) => b - a);
  // Every year from the first shown to the last, even one with no rows
  if (years.length) {
    const all = [];
    for (let y = years[0]; y >= years[years.length - 1]; y--) all.push(y);
    years = all;
  }
  if (thumbnail) years = years.slice(0, 3);
  if (!years.length) years = [last.getUTCFullYear()];
  const shown = new Set(years);

  // A cell for every day of every year shown, up to the last day with data
  const cells = [];
  for (const year of years) {
    for (
      let date = new Date(Date.UTC(year, 0, 1));
      date.getUTCFullYear() === year && date <= last;
      date = new Date(date.getTime() + 86400000)
    ) {
      const value = values.get(date.getTime());
      cells.push({ date, value: value === undefined ? (fillZero ? 0 : null) : value, ...place(date) });
    }
  }
  const filled = cells.filter((d) => d.value !== null && (d.value !== 0 || !fillZero) && shown.has(d.year));
  const drawn = new Set(filled);
  const empty = cells.filter((d) => !drawn.has(d));
  const max = Math.max(0, ...filled.map((d) => d.value || 0));
  // A few very busy days would leave every other day pale: a square root
  // scale keeps the rest apart
  const sorted = filled.map((d) => d.value).filter((v) => v > 0).sort((a, b) => a - b);
  const p90 = sorted.length ? sorted[Math.floor(0.9 * (sorted.length - 1))] : 0;
  const skewed = p90 > 0 && max > 3 * p90;
  // Small counts are whole numbers: the legend shows no fractions
  const whole = filled.every((d) => Number.isInteger(d.value));
  const legendTicks = whole && max <= 6 ? { ticks: Math.max(1, max) } : {};

  const marginLeft = thumbnail ? 2 : 84;
  const marginRight = thumbnail ? 2 : 12;
  const cell = thumbnail
    ? Math.min((ctx.width - 4) / 54, (ctx.height - 4) / (years.length * 7 + (years.length - 1) * 1.5))
    : Math.max(8, Math.min(18, Math.floor((ctx.width - marginLeft - marginRight) / 54)));
  const gap = thumbnail ? cell * 1.5 : 26; // between year blocks
  const marginTop = thumbnail ? 2 : 22;
  const marginBottom = thumbnail ? 2 : 8;
  const height = Math.round(marginTop + marginBottom + years.length * 7 * cell + (years.length - 1) * gap);
  const width = Math.round(marginLeft + marginRight + 54 * cell);

  // The week each month starts in, from a year that starts on a Monday
  const monthTicks = MONTHS.map((name, month) => ({ name, week: place(new Date(Date.UTC(2018, month, 1))).week }));
  const title = (d) => {
    const date = d.date;
    return `${WEEKDAY_NAMES[d.day]} ${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()}\n${meta.y}: ${fmt(d.value ?? 0)}`;
  };
  const inset = thumbnail ? 0.25 : 1.2;
  const marks = [
    Plot.cell(empty, { x: "week", y: "day", fy: "year", fill: emptyCell(theme), inset }),
    Plot.cell(filled, {
      x: "week",
      y: "day",
      fy: "year",
      fill: "value",
      inset,
      rx: thumbnail ? 0 : 2,
      ...(thumbnail ? {} : { title, tip: TIP }),
    }),
  ];
  if (!thumbnail) {
    marks.push(
      Plot.text(years, {
        fy: (d) => d,
        text: (d) => String(d),
        frameAnchor: "top-left",
        dx: -marginLeft + 4,
        dy: 1,
        textAnchor: "start",
        fontWeight: 600,
        fill: theme.ink2,
      }),
      Plot.axisY({
        ticks: [0, 2, 4],
        tickFormat: (day) => WEEKDAYS[day],
        tickSize: 0,
        label: null,
        fontSize: 10,
      }),
      Plot.axisX({
        anchor: "top",
        ticks: monthTicks.map((m) => m.week),
        tickFormat: (week) => (monthTicks.find((m) => m.week === week) || {}).name,
        tickSize: 0,
        textAnchor: "start",
        label: null,
        fontSize: 10,
      })
    );
  }
  const options = basePlot(ctx, {
    width,
    height,
    marginLeft,
    marginRight,
    marginTop,
    marginBottom,
    x: { type: "band", domain: [...Array(54).keys()], padding: 0, axis: null },
    y: { type: "band", domain: [...Array(7).keys()], padding: 0, axis: null },
    fy: { domain: years, padding: gap / (7 * cell + gap), axis: null },
    // One hue, light to dark: the lightest step means "near zero"
    color: {
      type: skewed ? "sqrt" : "linear",
      domain: [0, max > 0 ? max : 1],
      range: theme.sequential,
      interpolate: "lab",
      label: meta.y,
      legend: !thumbnail,
      tickFormat: fmt,
      ...legendTicks,
      // Room for the first tick label, which Plot's ramp legend would clip
      marginLeft: 10,
      width: 280,
    },
    marks,
  });
  // Weeks wrap, so there is no straight time axis: only free-floating notes,
  // which keep off every day drawn, empty or not
  const plot = annotate(el, ctx, (margins) => Plot.plot({ ...options, ...margins }), {
    marks: (drawn) => {
      const sx = drawn.scale("x");
      const sy = drawn.scale("y");
      const sfy = drawn.scale("fy");
      // Plot gives y the same range in every facet and moves each facet
      // down to where fy puts it
      const facetTop = Math.min(...sy.range);
      const rects = [];
      for (const d of cells) {
        if (!shown.has(d.year)) continue;
        const left = sx.apply(d.week);
        const top = sy.apply(d.day) + sfy.apply(d.year) - facetTop;
        if (!Number.isFinite(left) || !Number.isFinite(top)) continue;
        rects.push({ left, right: left + sx.bandwidth, top, bottom: top + sy.bandwidth });
      }
      return { rects, lines: [] };
    },
  });
  mount(el, ctx, plot);
}
