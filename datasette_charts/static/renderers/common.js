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
  if (unit === "year") return new Date(Date.UTC(parseInt(text, 10), 0, 1));
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
  if (unit === "year") next.setUTCFullYear(next.getUTCFullYear() + 1);
  else if (unit === "quarter") next.setUTCMonth(next.getUTCMonth() + 3);
  else if (unit === "month") next.setUTCMonth(next.getUTCMonth() + 1);
  else if (unit === "week") next.setUTCDate(next.getUTCDate() + 7);
  else if (unit === "day") next.setUTCDate(next.getUTCDate() + 1);
  else if (unit === "hour") next.setUTCHours(next.getUTCHours() + 1);
  else next.setUTCMinutes(next.getUTCMinutes() + 1);
  return next;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function fmtBucket(date, unit) {
  const y = date.getUTCFullYear();
  const m = date.getUTCMonth();
  const d = date.getUTCDate();
  const pad = (n) => String(n).padStart(2, "0");
  if (unit === "year") return String(y);
  if (unit === "quarter") return `Q${Math.floor(m / 3) + 1} ${y}`;
  if (unit === "month") return `${MONTHS[m]} ${y}`;
  if (unit === "week") return `Week of ${d} ${MONTHS[m]} ${y}`;
  if (unit === "day") return `${d} ${MONTHS[m]} ${y}`;
  if (unit === "hour") return `${d} ${MONTHS[m]} ${y}, ${pad(date.getUTCHours())}:00`;
  return `${d} ${MONTHS[m]} ${y}, ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

// Every bucket from the first to the last, so a gap can be shown as a gap.
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
  return out;
}
