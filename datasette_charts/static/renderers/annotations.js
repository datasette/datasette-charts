// Annotations: notes drawn over a chart, from the spec's `annotations` list.
//
// A renderer draws its plot with Plot, then calls annotate() with a
// description of its axes. The annotations are drawn into the plot's own SVG
// using the scales Plot worked out, so they line up exactly with the marks.
// All text goes in as text nodes. An annotation whose value is not on the
// chart is skipped, and a note saying so is left in ctx.annotationNotes.
//
// `axes` describes the spec's x and y channels, which are not always the
// screen's x and y: a horizontal bar chart draws its x channel down the side.
//   {x: {screen: "x" | "y" | "fy", kind: "band" | "time" | "number", match},
//    y: {screen, kind}, lookup}
// `match(value)` returns the band a category value names, or null.
// `lookup(annotation)` returns {x, y}, the data point a point annotation
// with no y refers to, with x as a value of the x channel's scale.

const FONT_SIZE = 12;
const LINE_HEIGHT = 14.5;
const GAP = 6;
const MAX_LINES = 5;
const POINT_OFFSET = 26;

const ANCHORS = {
  "top-left": [-1, -1],
  top: [0, -1],
  "top-right": [1, -1],
  left: [-1, 0],
  center: [0, 0],
  right: [1, 0],
  "bottom-left": [-1, 1],
  bottom: [0, 1],
  "bottom-right": [1, 1],
};
const DASHES = { dashed: "5 4", dotted: "1 3.5", solid: null };
const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

// --- Values -------------------------------------------------------------------

const MOMENT =
  /^(-?\d{4})(?:-Q([1-4])|-(\d\d)(?:-(\d\d)(?:[T ](\d\d)(?::(\d\d)(?::(\d\d)(?:\.\d+)?)?)?Z?)?)?)?$/;

// A moment written as precisely as needed, as the period it names:
// "2009" is all of 2009, "2009-06" all of June 2009. A whole number is a year.
export function parseMoment(value) {
  if (typeof value === "number" && Number.isInteger(value)) value = String(value).padStart(4, "0");
  if (typeof value !== "string") return null;
  const m = MOMENT.exec(value.trim());
  if (!m) return null;
  const year = +m[1];
  const utc = (...parts) => {
    const date = new Date(Date.UTC(2000, 0, 1));
    date.setUTCFullYear(parts[0], parts[1] || 0, parts[2] || 1);
    date.setUTCHours(parts[3] || 0, parts[4] || 0, parts[5] || 0, 0);
    return date;
  };
  let start, end;
  if (m[2]) {
    start = utc(year, (m[2] - 1) * 3);
    end = utc(year, m[2] * 3);
  } else if (m[7] !== undefined) {
    start = utc(year, m[3] - 1, +m[4], +m[5], +m[6], +m[7]);
    end = new Date(start.getTime() + 1000);
  } else if (m[6] !== undefined) {
    start = utc(year, m[3] - 1, +m[4], +m[5], +m[6]);
    end = new Date(start.getTime() + 60000);
  } else if (m[5] !== undefined) {
    start = utc(year, m[3] - 1, +m[4], +m[5]);
    end = new Date(start.getTime() + 3600000);
  } else if (m[4] !== undefined) {
    start = utc(year, m[3] - 1, +m[4]);
    end = utc(year, m[3] - 1, +m[4] + 1);
  } else if (m[3] !== undefined) {
    start = utc(year, m[3] - 1);
    end = utc(year, +m[3]);
  } else {
    start = utc(year);
    end = utc(year + 1);
  }
  return { start, end };
}

function colorFor(theme, name, fallback) {
  const named = {
    blue: theme.series[0],
    orange: theme.series[1],
    green: theme.series[2],
    yellow: theme.series[3],
    pink: theme.series[4],
    purple: theme.series[6],
    red: theme.series[7],
    gray: theme.muted,
  };
  if (named[name]) return named[name];
  if (typeof name === "string" && HEX.test(name)) return name;
  return fallback;
}

// Short descriptions, for notes and the annotation list
export function describeValue(value) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

// --- Text ---------------------------------------------------------------------

let measurer = null;
function textWidth(text, font) {
  if (!measurer) measurer = document.createElement("canvas").getContext("2d");
  measurer.font = font;
  return measurer.measureText(text).width;
}

// Greedy word wrap. Line breaks in the text are kept.
export function wrap(text, maxWidth, font) {
  const lines = [];
  for (const paragraph of String(text).split(/\r?\n/)) {
    let line = "";
    for (const word of paragraph.split(/\s+/).filter(Boolean)) {
      const candidate = line ? `${line} ${word}` : word;
      if (line && textWidth(candidate, font) > maxWidth) {
        lines.push(line);
        line = word;
      } else {
        line = candidate;
      }
    }
    lines.push(line);
  }
  if (lines.length > MAX_LINES) {
    lines.length = MAX_LINES;
    lines[MAX_LINES - 1] += "…";
  }
  // A single word longer than the space is cut short rather than overflowing
  return lines.map((line) => {
    if (textWidth(line, font) <= maxWidth * 1.15) return line;
    let cut = line;
    while (cut.length > 1 && textWidth(cut + "…", font) > maxWidth) cut = cut.slice(0, -1);
    return cut + "…";
  });
}

// --- Drawing ------------------------------------------------------------------

const SVG = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) el.setAttribute(key, String(value));
  }
  return el;
}

// The plot's own <svg>: with a legend, Plot wraps it in a <figure>
function mainSvg(plot) {
  if (plot.tagName.toLowerCase() === "svg") return plot;
  const svgs = plot.querySelectorAll(":scope > svg");
  return svgs.length ? svgs[svgs.length - 1] : null;
}

function scaleOf(plot, name) {
  try {
    return plot.scale(name) || null;
  } catch (e) {
    return null;
  }
}

function extent(range) {
  return [Math.min(...range), Math.max(...range)];
}

// The plot area, inside the margins
function frameOf(plot, svg, ctx) {
  const width = +svg.getAttribute("width") || ctx.width;
  const height = +svg.getAttribute("height") || ctx.height;
  const frame = { left: 0, right: width, top: 0, bottom: height };
  const sx = scaleOf(plot, "x");
  const sy = scaleOf(plot, "y");
  const fy = scaleOf(plot, "fy");
  if (sx && sx.range) [frame.left, frame.right] = extent(sx.range);
  const vertical = fy || sy;
  if (vertical && vertical.range) [frame.top, frame.bottom] = extent(vertical.range);
  if (!sx && !sy) {
    // A map: the whole drawing, less a little room at the edges
    Object.assign(frame, { left: 8, right: width - 8, top: 8, bottom: height - 8 });
  }
  return frame;
}

const within = (value, domain) => {
  const [low, high] = extent(domain.map(Number));
  const span = high - low || 1;
  return +value >= low - span * 1e-9 && +value <= high + span * 1e-9;
};

// The pixel on the screen axis where a channel value falls. `mode` is
// before, center or after: where on a band or time period to go.
function locate(plot, axis, value, mode) {
  const scale = scaleOf(plot, axis.screen);
  if (!scale) return null;
  if (axis.kind === "band") {
    const name = axis.match(value);
    if (name === null || name === undefined) return null;
    const start = scale.apply(name);
    if (start === undefined || Number.isNaN(start)) return null;
    const bandwidth = scale.bandwidth || 0;
    const gap = ((scale.step || bandwidth) - bandwidth) / 2;
    if (mode === "before") return start - gap;
    if (mode === "after") return start + bandwidth + gap;
    return start + bandwidth / 2;
  }
  if (axis.kind === "time") {
    const period = parseMoment(value);
    if (!period) return null;
    const moment =
      mode === "after"
        ? period.end
        : mode === "center"
          ? new Date((period.start.getTime() + period.end.getTime()) / 2)
          : period.start;
    if (!within(moment, scale.domain)) return null;
    return scale.apply(moment);
  }
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (scale.type === "log" && value <= 0) return null;
  if (!within(value, scale.domain)) return null;
  return scale.apply(value);
}

// Where a point annotation is, in screen coordinates. With no y, the
// renderer looks up the data point at x.
function pointAt(plot, axes, annotation) {
  let xPixel;
  let yValue = annotation.y;
  if (yValue === undefined) {
    const found = axes.lookup ? axes.lookup(annotation) : null;
    const scale = scaleOf(plot, axes.x.screen);
    if (!found || !scale) return null;
    yValue = found.y;
    xPixel = scale.apply(found.x);
    if (axes.x.kind === "band") xPixel += (scale.bandwidth || 0) / 2;
  } else {
    xPixel = locate(plot, axes.x, annotation.x, axes.x.kind === "time" ? "before" : "center");
  }
  const yPixel = locate(plot, axes.y, yValue, "center");
  if (xPixel === null || xPixel === undefined || Number.isNaN(xPixel) || yPixel === null) return null;
  return axes.x.screen === "x" ? { X: xPixel, Y: yPixel } : { X: yPixel, Y: xPixel };
}

const overlaps = (a, b) =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

class Layout {
  constructor(frame, font) {
    this.frame = frame;
    this.font = font;
    this.boxes = [];
  }

  // A box for `lines` with its reference edges where `place` says, kept
  // inside the frame and clear of the labels already placed
  // `push` is which way to move to get clear: 1 down, -1 up
  // `place` can be a list of places to try in turn: the first that is clear
  // of earlier labels is used.
  fit(lines, place, push) {
    const frame = this.frame;
    const width = Math.max(...lines.map((line) => textWidth(line, this.font)));
    const height = lines.length * LINE_HEIGHT;
    const boxAt = (where) => {
      let { left, top } = where(width, height);
      left = Math.max(frame.left + 2, Math.min(left, frame.right - 2 - width));
      top = Math.max(frame.top + 2, Math.min(top, frame.bottom - 2 - height));
      return { left, top, right: left + width, bottom: top + height };
    };
    const places = Array.isArray(place) ? place : [place];
    let box = boxAt(places[0]);
    for (const where of places.slice(1)) {
      if (!this.boxes.some((other) => overlaps(box, other))) break;
      const next = boxAt(where);
      if (!this.boxes.some((other) => overlaps(next, other))) box = next;
    }
    // Step clear of earlier labels, away from what this one labels
    for (let tries = 0; tries < 8; tries++) {
      const hit = this.boxes.find((other) => overlaps(box, other));
      if (!hit) break;
      const shift = push > 0 ? hit.bottom + 3 - box.top : hit.top - 3 - box.bottom;
      box = { ...box, top: box.top + shift, bottom: box.bottom + shift };
    }
    this.boxes.push(box);
    return box;
  }
}

// The text sits on a chip of the chart's background, so it stays readable
// where it crosses bars, lines and gridlines
function drawText(group, lines, box, align, fill, surface) {
  const x = align === "end" ? box.right : align === "middle" ? (box.left + box.right) / 2 : box.left;
  group.append(
    svgEl("rect", {
      x: box.left - 3,
      y: box.top - 1,
      width: box.right - box.left + 6,
      height: box.bottom - box.top + 2,
      rx: 3,
      fill: surface,
      "fill-opacity": 0.9,
      class: "dc-annotation-chip",
    })
  );
  const text = svgEl("text", {
    x,
    y: box.top,
    "text-anchor": align,
    fill,
    "font-size": FONT_SIZE,
    class: "dc-annotation-text",
  });
  lines.forEach((line, i) => {
    const tspan = svgEl("tspan", { x, dy: i === 0 ? FONT_SIZE * 0.95 : LINE_HEIGHT });
    tspan.textContent = line;
    text.append(tspan);
  });
  group.append(text);
}

const alignFor = (hx) => (hx > 0 ? "start" : hx < 0 ? "end" : "middle");

// Draws the spec's annotations into a plot made by Plot.plot(). Returns the
// plot. Thumbnails are left alone.
export function annotate(el, ctx, plot, axes) {
  const annotations = Array.isArray(ctx.spec.annotations) ? ctx.spec.annotations : [];
  ctx.annotationNotes = ctx.annotationNotes || [];
  ctx.annotationText = ctx.annotationText || [];
  if (!annotations.length || ctx.size === "thumbnail") return plot;
  const svg = mainSvg(plot);
  if (!svg) return plot;
  const { theme } = ctx;
  const frame = frameOf(plot, svg, ctx);
  const family = getComputedStyle(el).fontFamily || "system-ui, sans-serif";
  const font = `${FONT_SIZE}px ${family}`;
  const layout = new Layout(frame, font);
  const halo = theme.surface;

  const behind = svgEl("g", { class: "dc-annotations-behind", "aria-hidden": "true", "pointer-events": "none" });
  const front = svgEl("g", { class: "dc-annotations", "pointer-events": "none" });
  // Labels go in a group of their own, above every line and marker
  const texts = svgEl("g", { class: "dc-annotation-labels" });
  const labels = [];
  const skip = (index, annotation, why) => {
    ctx.annotationNotes.push(`Annotation ${index + 1} is not shown: ${why}`);
  };

  annotations.forEach((annotation, index) => {
    if (!annotation || typeof annotation !== "object") return;
    const text = typeof annotation.text === "string" ? annotation.text.trim() : "";
    const [hx0, vy0] = ANCHORS[annotation.anchor] || [null, null];
    const kind = annotation.type;
    if (text) ctx.annotationText.push(text);

    if (kind === "line") {
      const channel = "x" in annotation ? "x" : "y";
      const axis = axes[channel];
      if (!axis) return skip(index, annotation, `this chart has no ${channel} axis`);
      const mode = annotation.position || (axis.kind === "band" ? "center" : "before");
      const at = locate(plot, axis, annotation[channel], mode);
      if (at === null) {
        return skip(index, annotation, `${describeValue(annotation[channel])} is not on the ${channel} axis`);
      }
      const color = colorFor(theme, annotation.color, theme.ink2);
      const vertical = axis.screen === "x";
      const stroke = annotation.stroke || "dashed";
      front.append(
        svgEl("line", {
          x1: vertical ? at : frame.left,
          x2: vertical ? at : frame.right,
          y1: vertical ? frame.top : at,
          y2: vertical ? frame.bottom : at,
          stroke: color,
          "stroke-width": 1.5,
          "stroke-dasharray": DASHES[stroke] || null,
          "stroke-linecap": stroke === "dotted" ? "round" : null,
        })
      );
      if (!text) return;
      if (vertical) {
        let hx = hx0 ?? 1;
        const vy = vy0 ?? -1;
        const room = (side) => (side > 0 ? frame.right - at : at - frame.left) - GAP - 4;
        // Not enough room on the side asked for: use the side with more
        if (hx !== 0 && room(hx) < 90 && room(-hx) > room(hx)) hx = -hx;
        const maxWidth = Math.max(40, Math.min(120, hx === 0 ? 120 : room(hx)));
        labels.push(() => {
          const lines = wrap(text, maxWidth, font);
          const box = layout.fit(
            lines,
            (w, h) => ({
              left: hx > 0 ? at + GAP : hx < 0 ? at - GAP - w : at - w / 2,
              top: vy < 0 ? frame.top + 4 : vy > 0 ? frame.bottom - 4 - h : (frame.top + frame.bottom - h) / 2,
            }),
            vy > 0 ? -1 : 1
          );
          drawText(texts, lines, box, alignFor(hx), color, halo);
        });
      } else {
        const hx = hx0 ?? 1;
        const vy = vy0 === 1 ? 1 : -1;
        const maxWidth = Math.max(60, Math.min(260, (frame.right - frame.left) * 0.6));
        labels.push(() => {
          const lines = wrap(text, maxWidth, font);
          const box = layout.fit(
            lines,
            (w, h) => ({
              left: hx > 0 ? frame.right - 4 - w : hx < 0 ? frame.left + 4 : (frame.left + frame.right - w) / 2,
              top: vy < 0 ? at - 4 - h : at + 4,
            }),
            vy < 0 ? -1 : 1
          );
          drawText(texts, lines, box, alignFor(hx), color, halo);
        });
      }
      return;
    }

    if (kind === "range") {
      const channel = "x" in annotation ? "x" : "y";
      const axis = axes[channel];
      if (!axis) return skip(index, annotation, `this chart has no ${channel} axis`);
      const exact = axis.kind === "number";
      let a = locate(plot, axis, annotation[channel], exact ? null : "before");
      let b = locate(plot, axis, annotation[`${channel}2`], exact ? null : "after");
      // A range that runs backwards is drawn the right way round
      if (a !== null && b !== null && b < a && axis.kind === "band") {
        a = locate(plot, axis, annotation[`${channel}2`], "before");
        b = locate(plot, axis, annotation[channel], "after");
      }
      if (a === null || b === null) {
        const missing = a === null ? annotation[channel] : annotation[`${channel}2`];
        return skip(index, annotation, `${describeValue(missing)} is not on the ${channel} axis`);
      }
      const [low, high] = [Math.min(a, b), Math.max(a, b)];
      const vertical = axis.screen === "x";
      const rect = vertical
        ? { left: low, right: high, top: frame.top, bottom: frame.bottom }
        : { left: frame.left, right: frame.right, top: low, bottom: high };
      const color = colorFor(theme, annotation.color, theme.muted);
      behind.append(
        svgEl("rect", {
          x: rect.left,
          y: rect.top,
          width: Math.max(1, rect.right - rect.left),
          height: Math.max(1, rect.bottom - rect.top),
          fill: color,
          "fill-opacity": theme.mode === "dark" ? 0.22 : 0.14,
        })
      );
      if (!text) return;
      // Bands across a flat bar chart are labelled at the right, where bars
      // sorted largest first leave room
      const hx = hx0 ?? (vertical ? 0 : 1);
      const vy = vy0 ?? -1;
      const inner = rect.right - rect.left - 10;
      const maxWidth = Math.max(70, Math.min(220, vertical ? inner : frame.right - frame.left - 10));
      labels.push(() => {
        const lines = wrap(text, maxWidth, font);
        const box = layout.fit(
          lines,
          (w, h) => ({
            left: hx < 0 ? rect.left + 5 : hx > 0 ? rect.right - 5 - w : (rect.left + rect.right - w) / 2,
            top: vy < 0 ? rect.top + 4 : vy > 0 ? rect.bottom - 4 - h : (rect.top + rect.bottom - h) / 2,
          }),
          vy > 0 ? -1 : 1
        );
        drawText(texts, lines, box, alignFor(hx), annotation.color ? color : theme.ink2, halo);
      });
      return;
    }

    if (kind === "point") {
      if (!axes.x || !axes.y) return skip(index, annotation, "this chart has no x and y axes");
      const at = pointAt(plot, axes, annotation);
      if (!at) {
        const series = annotation.series ? ` in ${describeValue(annotation.series)}` : "";
        return skip(index, annotation, `there is no point at ${describeValue(annotation.x)}${series}`);
      }
      const color = colorFor(theme, annotation.color, theme.ink);
      const marker = annotation.marker !== false;
      // Default: above, or below when that is the only room
      let hx = hx0 ?? 0;
      let vy = vy0 ?? -1;
      if (vy0 === null && at.Y - frame.top < 40) vy = 1;
      if (hx0 === null && vy === 0) hx = 1;
      const offset = marker ? POINT_OFFSET : 0;
      labels.push(() => {
        const lines = wrap(text, 160, font);
        const around = ([sx, sy]) => (w, h) => {
          const cx = at.X + sx * (offset * 0.7 + (sx ? w / 2 : 0));
          const cy = at.Y + sy * (offset + (sy ? h / 2 : 0));
          return { left: cx - w / 2, top: cy - h / 2 };
        };
        // Where it was asked to go, then the other sides of the point
        const sides = [[hx, vy], [-1, -1], [1, -1], [-1, 1], [1, 1], [-1, 0], [1, 0], [0, -1], [0, 1]];
        const box = layout.fit(lines, sides.map(around), vy > 0 ? 1 : -1);
        if (marker) {
          // A leader from the point to the nearest edge of the label
          const ex = Math.max(box.left, Math.min(at.X, box.right));
          const ey = Math.max(box.top, Math.min(at.Y, box.bottom));
          const dx = ex - at.X;
          const dy = ey - at.Y;
          const length = Math.hypot(dx, dy);
          if (length > 8) {
            front.append(
              svgEl("line", {
                x1: at.X + (dx / length) * 5,
                y1: at.Y + (dy / length) * 5,
                x2: ex - (dx / length) * 2,
                y2: ey - (dy / length) * 2,
                stroke: color,
                "stroke-width": 1,
              })
            );
          }
          front.append(
            svgEl("circle", { cx: at.X, cy: at.Y, r: 3.5, fill: color, stroke: halo, "stroke-width": 1.5 })
          );
        }
        drawText(texts, lines, box, alignFor(hx), color, halo);
      });
      return;
    }

    if (kind === "text") {
      if (typeof annotation.left !== "number" || typeof annotation.top !== "number") return;
      const X = frame.left + annotation.left * (frame.right - frame.left);
      const Y = frame.top + annotation.top * (frame.bottom - frame.top);
      // By default the text grows away from the nearest edges
      const hx = hx0 ?? (annotation.left < 0.34 ? 1 : annotation.left > 0.66 ? -1 : 0);
      const vy = vy0 ?? (annotation.top < 0.5 ? 1 : -1);
      const room = hx > 0 ? frame.right - X : hx < 0 ? X - frame.left : (frame.right - frame.left) / 2;
      const maxWidth = Math.max(60, Math.min(280, room - 4));
      const color = colorFor(theme, annotation.color, theme.ink2);
      labels.push(() => {
        const lines = wrap(text, maxWidth, font);
        const box = layout.fit(
          lines,
          (w, h) => ({
            left: hx > 0 ? X : hx < 0 ? X - w : X - w / 2,
            top: vy > 0 ? Y : vy < 0 ? Y - h : Y - h / 2,
          }),
          vy > 0 ? 1 : -1
        );
        drawText(texts, lines, box, alignFor(hx), color, halo);
      });
    }
  });

  // Labels go last, so lines and markers never cover them
  for (const draw of labels) draw();
  front.append(texts);
  const style = svg.querySelector(":scope > style");
  if (behind.childNodes.length) {
    if (style) style.after(behind);
    else svg.prepend(behind);
  }
  if (front.childNodes.length) {
    // Below the tooltips, which have to stay on top
    const tip = svg.querySelector(':scope > g[aria-label="tip"]');
    if (tip) svg.insertBefore(front, tip);
    else svg.append(front);
  }
  return plot;
}

// The band a category annotation names: by its label, or its raw key
export function bandMatcher(names, keys) {
  return (value) => {
    const text = String(value);
    let index = names.indexOf(text);
    if (index < 0 && keys) index = keys.findIndex((key) => key !== null && key !== undefined && String(key) === text);
    return index < 0 ? null : names[index];
  };
}
