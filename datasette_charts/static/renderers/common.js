// Shared by the built-in renderers: colors, sizing and formatting.
//
// A renderer is an ES module that exports
//   render(el, {spec, rows, columns, data, meta, size, width, height, Plot, d3, theme, urls})
// `rows` are the already-aggregated rows from the server; `data` is the same
// rows as objects keyed by column name. Renderers do no data fetching.

// Categorical hues are assigned in this fixed order and never cycled:
// anything past the eighth series is drawn in the neutral "other" color.
const THEMES = {
  light: {
    series: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    // One hue, light to dark: the lightest step means "near zero"
    sequential: ["#cde2fb", "#0d366b"],
    other: "#a8a69f",
    land: "#ecebe6",
    grid: "#e1e0d9",
    muted: "#898781",
    ink: "#0b0b0b",
    ink2: "#52514e",
  },
  dark: {
    series: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"],
    sequential: ["#104281", "#cde2fb"],
    other: "#6f6e69",
    land: "#2c2c2a",
    grid: "#2c2c2a",
    muted: "#898781",
    ink: "#f0efec",
    ink2: "#c3c2b7",
  },
};

function parseColor(value) {
  const match = /rgba?\(([^)]+)\)/.exec(value || "");
  if (!match) return null;
  const parts = match[1].split(/[,\s/]+/).filter(Boolean).map(Number);
  const alpha = parts.length > 3 ? parts[3] : 1;
  return { r: parts[0], g: parts[1], b: parts[2], alpha };
}

// The palette is picked from the surface the chart actually sits on, so it
// follows whatever theme the page uses, not just the OS setting.
// Tooltips: a little more space between lines than Plot gives them
export const TIP = { lineHeight: 1.3 };

export function themeFor(el) {
  let surface = { r: 255, g: 255, b: 255 };
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    const color = parseColor(getComputedStyle(node).backgroundColor);
    if (color && color.alpha > 0.5) {
      surface = color;
      break;
    }
  }
  const luminance = (0.2126 * surface.r + 0.7152 * surface.g + 0.0722 * surface.b) / 255;
  const mode = luminance < 0.45 ? "dark" : "light";
  return {
    mode,
    surface: `rgb(${surface.r}, ${surface.g}, ${surface.b})`,
    ...THEMES[mode],
  };
}

export function seriesColor(theme, index) {
  return index < theme.series.length ? theme.series[index] : theme.other;
}

// Options every plot starts from. Thumbnails omit axes, labels and legends,
// so one renderer serves both sizes.
export function basePlot(ctx, options = {}) {
  const thumbnail = ctx.size === "thumbnail";
  const base = {
    width: ctx.width,
    height: ctx.height,
    style: {
      background: "transparent",
      color: ctx.theme.ink2,
      fontFamily: "inherit",
      fontSize: "12px",
      overflow: "visible",
    },
  };
  if (thumbnail) {
    Object.assign(base, { axis: null, margin: 6 });
  } else {
    base.marginBottom = 44; // room for a centred x-axis label under the ticks
  }
  return { ...base, ...options };
}

// An x-axis label centred under the ticks, where it cannot collide with them
export function xLabel(label) {
  return { label, labelAnchor: "center" };
}

// Draws the plot into the element and sets what Plot reads from CSS
export function mount(el, ctx, plot) {
  // Tooltips fill with this. Plot's own stylesheet sets it on every plot
  // <svg>, so with a legend (a <figure> wrapper) it must be set on the
  // inner svg too, or tips fall back to white whatever the page theme.
  for (const node of [plot, ...plot.querySelectorAll("svg")]) {
    node.style.setProperty("--plot-background", ctx.theme.surface);
  }
  plot.style.maxWidth = "100%";
  el.replaceChildren(plot);
}

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const full = new Intl.NumberFormat("en", { maximumFractionDigits: 2 });
const whole = new Intl.NumberFormat("en", { maximumFractionDigits: 0 });

export function fmt(value) {
  if (value === null || value === undefined) return "";
  if (typeof value !== "number") return String(value);
  return full.format(value);
}

// For value labels beside marks: 1,284 then 12.9K then 4.2M
export function fmtCompact(value) {
  if (typeof value !== "number") return fmt(value);
  const size = Math.abs(value);
  if (size >= 10000) return compact.format(value);
  return size >= 100 ? whole.format(value) : full.format(value);
}

// Years read badly with thousands separators
export function looksLikeYears(name) {
  return /(^|[^a-z])(year|yr)([^a-z]|$)/i.test(String(name || "").replace(/([a-z])([A-Z])/g, "$1_$2"));
}

export function isThumbnail(ctx) {
  return ctx.size === "thumbnail";
}

// The display name for a category value. `label` is a foreign key's label.
export function categoryName(key, isOther, label) {
  if (isOther) return "Other";
  if (label !== null && label !== undefined && label !== "") return String(label);
  if (key === null || key === undefined) return "(null)";
  if (key === "") return "(empty)";
  return String(key);
}

// Band scales need unique names: two keys can share a label
export function uniqueNames(names) {
  const seen = new Map();
  return names.map((name) => {
    const count = seen.get(name) || 0;
    seen.set(name, count + 1);
    return count ? `${name} (${count + 1})` : name;
  });
}

// "1 row", "1,284 rows"
export function plural(n, noun) {
  return `${Number(n).toLocaleString("en")} ${noun}${n === 1 ? "" : "s"}`;
}

export function truncate(text, max) {
  text = String(text);
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

// --- Time buckets -----------------------------------------------------------
// The server labels buckets 2026, 2026-Q4, 2026-11, 2026-11-16 (the Monday of
// a week), 2026-11-22, 2026-11-22T10 and 2026-11-22T10:30.

export function bucketToDate(bucket, unit) {
  const text = String(bucket);
  let match;
  if (unit === "year" || unit === "decade") return new Date(Date.UTC(parseInt(text, 10), 0, 1));
  if (unit === "quarter" && (match = /^(-?\d+)-Q(\d)$/.exec(text))) {
    return new Date(Date.UTC(+match[1], (+match[2] - 1) * 3, 1));
  }
  if (unit === "month" && (match = /^(-?\d+)-(\d\d)$/.exec(text))) {
    return new Date(Date.UTC(+match[1], +match[2] - 1, 1));
  }
  if ((match = /^(-?\d+)-(\d\d)-(\d\d)(?:T(\d\d)(?::(\d\d))?)?$/.exec(text))) {
    return new Date(Date.UTC(+match[1], +match[2] - 1, +match[3], +(match[4] || 0), +(match[5] || 0)));
  }
  return null;
}

export function stepDate(date, unit) {
  const next = new Date(date.getTime());
  if (unit === "decade") next.setUTCFullYear(next.getUTCFullYear() + 10);
  else if (unit === "year") next.setUTCFullYear(next.getUTCFullYear() + 1);
  else if (unit === "quarter") next.setUTCMonth(next.getUTCMonth() + 3);
  else if (unit === "month") next.setUTCMonth(next.getUTCMonth() + 1);
  else if (unit === "week") next.setUTCDate(next.getUTCDate() + 7);
  else if (unit === "day") next.setUTCDate(next.getUTCDate() + 1);
  else if (unit === "hour") next.setUTCHours(next.getUTCHours() + 1);
  else next.setUTCMinutes(next.getUTCMinutes() + 1);
  return next;
}

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function fmtBucket(date, unit) {
  const y = date.getUTCFullYear();
  const m = date.getUTCMonth();
  const d = date.getUTCDate();
  const pad = (n) => String(n).padStart(2, "0");
  if (unit === "decade") return `${y}s`;
  if (unit === "year") return String(y);
  if (unit === "quarter") return `Q${Math.floor(m / 3) + 1} ${y}`;
  if (unit === "month") return `${MONTHS[m]} ${y}`;
  if (unit === "week") return `Week of ${d} ${MONTHS[m]} ${y}`;
  if (unit === "day") return `${d} ${MONTHS[m]} ${y}`;
  if (unit === "hour") return `${d} ${MONTHS[m]} ${y}, ${pad(date.getUTCHours())}:00`;
  return `${d} ${MONTHS[m]} ${y}, ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

// Every bucket from the first to the last, so a gap can be shown as a gap.
// Data that comes at a regular step of several buckets, like one row per
// decade bucketed by year, keeps its own step: only a missing step is a gap.
// Returns null when there would be too many to be worth filling.
export function allBuckets(dates, unit, max = 2000) {
  if (!dates.length) return [];
  const times = dates.map((date) => date.getTime());
  const end = Math.max(...times);
  const out = [];
  for (let date = new Date(Math.min(...times)); date.getTime() <= end; date = stepDate(date, unit)) {
    out.push(date);
    if (out.length > max) return null;
  }
  const present = new Set(times);
  let step = 0;
  out.forEach((date, i) => {
    if (present.has(date.getTime())) step = gcd(step, i);
  });
  return step > 1 ? out.filter((date, i) => i % step === 0) : out;
}

function gcd(a, b) {
  return b ? gcd(b, a % b) : a;
}

// --- Crowded category axes ------------------------------------------------------

let measurer = null;
export function textWidth(text, font) {
  // Away from a browser (in tests), an estimate from the font size
  if (typeof document === "undefined") return String(text).length * parseFloat(font) * 0.55;
  if (!measurer) measurer = document.createElement("canvas").getContext("2d");
  measurer.font = font;
  return measurer.measureText(String(text)).width;
}

// How to write category labels under upright bars so they do not run into
// each other, tried in this order until they fit:
//   level: as they are, flat, when they fit side by side
//   then turned 40 degrees at 12px; smaller, down to 9px; steeper, to 60
//   and then 90 degrees; and last, for ordered values such as years only,
//   every Nth label. Named categories never lose a label.
// Turned labels need their line height, plus a little air, between them
// along the axis.
export const BAND_LABEL_STEPS = [
  [12, 40],
  [11, 40],
  [10, 40],
  [9, 40],
  [9, 60],
  [9, 90],
];

export function fitBandLabels(labels, { step, fontFamily, ordered }) {
  const font = (size) => `${size}px ${fontFamily}`;
  const longestChars = Math.max(0, ...labels.map((label) => label.length));
  // The rule charts already used to stay level, so those look the same
  if (longestChars * 6.6 <= step - 4) return { fontSize: 12, angle: 0, every: 1, marginBottom: 44, overhang: 0 };
  const needs = (size, angle) => (size * 1.2 + 3) / Math.sin((angle * Math.PI) / 180);
  let chosen = null;
  for (let every = 1; every <= labels.length && !chosen; every++) {
    if (every > 1 && !ordered) break;
    const found = BAND_LABEL_STEPS.find(([size, angle]) => step * every >= needs(size, angle));
    if (found) chosen = { size: found[0], angle: found[1], every };
  }
  // Named categories that cannot fit even upright at 9px: upright at 9px
  if (!chosen) chosen = { size: 9, angle: 90, every: 1 };
  const { size, angle, every } = chosen;
  const radians = (angle * Math.PI) / 180;
  const widest = Math.max(0, ...labels.map((label) => textWidth(label, font(size))));
  const first = labels.length ? textWidth(labels[0], font(size)) : 0;
  return {
    fontSize: size,
    angle,
    every,
    // Room under the plot for the turned labels
    marginBottom: Math.min(170, Math.ceil(widest * Math.sin(radians) + size * Math.cos(radians)) + 16),
    // How far the first label reaches left of its bar's middle
    overhang: Math.ceil(first * Math.cos(radians)),
  };
}

// Whether category values are ordered numbers or dates, such as years,
// which can be thinned out without losing track of which bar is which
export function orderedValues(keys) {
  const values = keys.map((key) => {
    if (typeof key === "number") return key;
    const text = String(key ?? "");
    if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
    if (/^\d{4}(-\d\d){0,2}$/.test(text)) return Date.parse(text);
    return NaN;
  });
  if (values.length < 3 || values.some((value) => Number.isNaN(value))) return false;
  const up = values.every((value, i) => i === 0 || value > values[i - 1]);
  const down = values.every((value, i) => i === 0 || value < values[i - 1]);
  return up || down;
}

// A left margin wide enough for the value axis's tick labels, such as
// "1,200,000", which the default 56px would clip. Never less than 56px.
export function valueAxisMargin(values, fontFamily = "system-ui, sans-serif") {
  const finite = values.filter((value) => typeof value === "number" && Number.isFinite(value));
  if (!finite.length) return 56;
  const biggest = Math.max(...finite.map((value) => Math.abs(value)));
  if (!biggest) return 56;
  // Round up to two significant figures, about what the top tick will be
  const scale = 10 ** Math.max(0, Math.floor(Math.log10(biggest)) - 1);
  const top = Math.ceil(biggest / scale) * scale;
  const negative = finite.some((value) => value < 0) ? "−" : "";
  const label = negative + new Intl.NumberFormat("en").format(top);
  return Math.max(56, Math.ceil(textWidth(label, `12px ${fontFamily}`) + 18));
}
