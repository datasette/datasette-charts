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
// `marks(plot)` returns where the data is drawn, in pixels, as
// {rects: [{left, top, right, bottom}], lines: [[{X, Y}, ...]]}, so that
// callout labels can keep off it.
//
// Point callouts are laid out in one of three ways, chosen per chart each
// time it is drawn (see chooseCalloutLayout):
//   1. "inline": each label beside its point, when every label fits there
//      clear of the data, the other labels and the plot's edges, and the
//      labels read in the same order as their points
//   2. "lanes": otherwise every label goes in a band of lanes outside the
//      plot (above it, or to the right of a chart whose x axis runs down the
//      side), in the order of their points, with a leader to each point
//   3. "key": when lanes would not fit either, numbered markers at the points
//      and the texts in a numbered list under the chart
// A callout with an explicit anchor, or marker: false, always stays beside
// its point.

const FONT_SIZE = 12;
const LINE_HEIGHT = 14.5;
const GAP = 6;
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

// --- Callout layout: pure functions, no DOM ---------------------------------------

export const LANE_LIMITS = {
  maxLanes: 3, // more lanes than this and it is a numbered key instead
  bandShare: 0.5, // the band may add at most this share of the plot's height
  sideShare: 0.4, // a band at the side may take at most this share of the width
  minLength: 160, // a plot shorter than this along its axis gets a key
  gap: 6, // between labels in a lane, and between lanes
};

// The width callout text wraps to: about 12 to 17 characters a line,
// narrower on a narrow chart
export function calloutWrapWidth(plotWidth) {
  return Math.round(Math.max(78, Math.min(108, plotWidth * 0.16)));
}

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

// Puts labels in lanes, keeping each lane in the order of the labels'
// points. `items` are {pos, size, cross}: where the point is along the band,
// how much of the band the label takes, and how thick it is across it.
// Lane 0 is next to the plot. A label goes in the first lane where it fits
// after the lane's last label, at most `maxShift` of its size from centred
// on its point, with a leader that need not cross a label in a lane nearer
// the plot (it bends to reach the point), and without covering the leader of
// a label in a lane further out.
// Returns [{lane, from, leader}] in the order given, or null if any label
// does not fit in `maxLanes` lanes.
export function assignLanes(items, { start, end, gap = LANE_LIMITS.gap, maxLanes = LANE_LIMITS.maxLanes, maxShift = 0.75 }) {
  const order = items.map((item, i) => i).sort((a, b) => items[a].pos - items[b].pos || a - b);
  const lanes = [];
  const placed = new Array(items.length);
  for (const i of order) {
    const { pos, size } = items[i];
    if (size > end - start) return null;
    const desired = clamp(pos - size / 2, start, end - size);
    let done = false;
    for (let lane = 0; lane < maxLanes && !done; lane++) {
      lanes[lane] = lanes[lane] || { end: -Infinity, boxes: [] };
      const earliest = lanes[lane].end + gap;
      const blocked = lanes
        .slice(0, lane)
        .flatMap((other) => other.boxes.map((box) => [box.from - 3, box.to + 3]));
      // Centred on the point if it can be; otherwise moved along, by up to
      // maxShift of its size, to clear the lane's last label or to give its
      // leader a way past the labels in nearer lanes
      const shifts = [0, 0.25, 0.5, 0.75, -0.25, -0.5].map((k) => k * size).filter((k) => Math.abs(k) <= size * maxShift);
      for (const shift of shifts) {
        const from = Math.max(clamp(desired + shift, start, end - size), earliest);
        if (from + size > end || Math.abs(from - desired) > size * maxShift) continue;
        // The leader leaves the label as near its point as it can, where it
        // does not have to pass through a label in a nearer lane
        const leader = freeSpot(clamp(pos, from + 4, from + size - 4), from + 4, from + size - 4, blocked);
        if (leader === null) continue;
        const coversFurther = placed.some(
          (other) => other && other.lane > lane && other.leader > from - 3 && other.leader < from + size + 3
        );
        if (coversFurther) continue;
        lanes[lane].boxes.push({ from, to: from + size });
        lanes[lane].end = from + size;
        placed[i] = { lane, from, leader };
        done = true;
        break;
      }
    }
    if (!done) return null;
  }
  return placed;
}

// The point in [low, high] nearest `want` that is outside every blocked
// interval, or null if there is none
function freeSpot(want, low, high, blocked) {
  const isFree = (x) => blocked.every(([a, b]) => x <= a || x >= b);
  if (isFree(want)) return want;
  const candidates = blocked
    .flatMap(([a, b]) => [a, b])
    .filter((x) => x >= low && x <= high && isFree(x));
  if (!candidates.length) return null;
  return candidates.reduce((best, x) => (Math.abs(x - want) < Math.abs(best - want) ? x : best));
}

// How thick each lane is, and the whole band with its gaps
export function laneBand(items, placed, gap = LANE_LIMITS.gap) {
  const thickness = [];
  placed.forEach((spot, i) => {
    thickness[spot.lane] = Math.max(thickness[spot.lane] || 0, items[i].cross);
  });
  const band = thickness.reduce((total, t) => total + t + gap, 0) + gap;
  return { thickness, band };
}

// Picks the tier for a chart's callouts.
//   inline: did every callout fit beside its point, in order (from the
//           in-place attempt)?
//   items, start, end: the lane items and the band's extent along its axis
//   plotLength: the plot's length along the band
//   crossLimit: the most the band may add across it, in pixels
// Returns {tier: "inline"} or {tier: "lanes", placed, thickness, band} or
// {tier: "key"}.
export function chooseCalloutLayout({ inline, items, start, end, plotLength, crossLimit, limits = LANE_LIMITS }) {
  if (inline) return { tier: "inline" };
  if (plotLength < limits.minLength) return { tier: "key" };
  const placed = assignLanes(items, { start, end, gap: limits.gap, maxLanes: limits.maxLanes });
  if (!placed) return { tier: "key" };
  const { thickness, band } = laneBand(items, placed, limits.gap);
  if (band > crossLimit) return { tier: "key" };
  return { tier: "lanes", placed, thickness, band };
}

// Whether labels read in the same order as their points: sorted by their
// points, the label centres never go backwards
export function inOrder(pairs) {
  const sorted = [...pairs].sort((a, b) => a.pos - b.pos);
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].pos - sorted[i - 1].pos > 0.5 && sorted[i].center < sorted[i - 1].center - 1) return false;
  }
  return true;
}

// A box against a polyline: does any segment pass through it?
export function boxHitsLine(box, points) {
  const inside = (p) => p.X >= box.left && p.X <= box.right && p.Y >= box.top && p.Y <= box.bottom;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    if (inside(a)) return true;
    if (i === 0) continue;
    const b = points[i - 1];
    if (Math.max(a.X, b.X) < box.left || Math.min(a.X, b.X) > box.right) continue;
    if (Math.max(a.Y, b.Y) < box.top || Math.min(a.Y, b.Y) > box.bottom) continue;
    // Where the segment is at the box's left and right edges
    const span = a.X - b.X;
    if (Math.abs(span) < 1e-9) return true;
    const yAt = (x) => b.Y + ((x - b.X) / span) * (a.Y - b.Y);
    const x1 = clamp(box.left, Math.min(a.X, b.X), Math.max(a.X, b.X));
    const x2 = clamp(box.right, Math.min(a.X, b.X), Math.max(a.X, b.X));
    const y1 = yAt(x1);
    const y2 = yAt(x2);
    if (Math.max(y1, y2) >= box.top && Math.min(y1, y2) <= box.bottom) return true;
  }
  return false;
}

// --- Text ---------------------------------------------------------------------

let measurer = null;
function textWidth(text, font) {
  if (!measurer) measurer = document.createElement("canvas").getContext("2d");
  measurer.font = font;
  return measurer.measureText(text).width;
}

// Greedy word wrap at word boundaries. Line breaks in the text are kept.
// Text is never cut short: a word longer than the width gets a line of its
// own, and the caller decides whether the result is legible.
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
  return lines;
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

// Draws the spec's annotations into a plot. `make(margins)` draws the plot
// with Plot.plot(), with any of {marginTop, marginRight, height} overriding
// the renderer's own: labels moved out of the plot need room around it, so
// the plot can be drawn again with more. A plot made already can be passed
// instead, and is then never drawn again. Returns the plot to mount.
// Thumbnails are left alone.
export function annotate(el, ctx, make, axes) {
  const build = typeof make === "function" ? make : () => make;
  const annotations = Array.isArray(ctx.spec.annotations) ? ctx.spec.annotations : [];
  for (const name of ["annotationNotes", "annotationText", "annotationKey", "annotationCaptions"]) {
    ctx[name] = ctx[name] || [];
  }
  let plot = build({});
  if (!annotations.length || ctx.size === "thumbnail") return plot;
  const canGrow = typeof make === "function";
  const svg0 = mainSvg(plot);
  if (!svg0) return plot;
  const frame0 = frameOf(plot, svg0, ctx);
  const base = {
    top: frame0.top,
    height: +svg0.getAttribute("height") || ctx.height,
    right: (+svg0.getAttribute("width") || ctx.width) - frame0.right,
  };
  let reserved = { top: 0, side: 0 };
  let hint = null;
  // Each pass lays the labels out on the plot as drawn. A pass whose labels
  // want a different amount of room around the plot has the plot drawn
  // again with that room, and lays them out again. The last pass allowed
  // draws whatever it gets, with no more room.
  const MAX_PASSES = 4;
  for (let pass = 1; pass <= MAX_PASSES; pass++) {
    const last = pass === MAX_PASSES;
    const plan = drawAll(el, ctx, plot, axes, { reserved, hint, canGrow: canGrow && !last });
    if (!plan || !canGrow || last) break;
    const wanted = { top: plan.top || 0, side: plan.side || 0 };
    const needsMore = wanted.top > reserved.top || wanted.side > reserved.side;
    // Room kept for lanes that are no longer wanted is given back
    const tooMuch = plan.tier !== "lanes" && (reserved.top || reserved.side);
    if (!needsMore && !tooMuch) break;
    reserved = needsMore ? { top: Math.max(wanted.top, reserved.top), side: Math.max(wanted.side, reserved.side) } : wanted;
    hint = plan;
    const margins = {};
    if (reserved.top) Object.assign(margins, { marginTop: base.top + reserved.top, height: base.height + reserved.top });
    if (reserved.side) margins.marginRight = base.right + reserved.side;
    plot = build(margins);
  }
  return plot;
}

// One pass of drawing: the lines, bands and markers, then every label laid
// out together. Returns the plan: {tier, top, side}, the room the labels
// want above and beside the plot.
function drawAll(el, ctx, plot, axes, { reserved, hint, canGrow }) {
  const annotations = ctx.spec.annotations;
  for (const name of ["annotationNotes", "annotationText", "annotationKey", "annotationCaptions"]) {
    ctx[name].length = 0;
  }
  const svg = mainSvg(plot);
  if (!svg) return null;
  const { theme } = ctx;
  const frame = frameOf(plot, svg, ctx);
  const svgWidth = +svg.getAttribute("width") || ctx.width;
  const family = getComputedStyle(el).fontFamily || "system-ui, sans-serif";
  const font = `${FONT_SIZE}px ${family}`;
  const halo = theme.surface;

  const behind = svgEl("g", { class: "dc-annotations-behind", "aria-hidden": "true", "pointer-events": "none" });
  const front = svgEl("g", { class: "dc-annotations", "pointer-events": "none" });
  const texts = svgEl("g", { class: "dc-annotation-labels" });
  const items = []; // every label to place
  const dots = []; // callout markers, which labels keep off too
  const skip = (index, why) => ctx.annotationNotes.push(`Annotation ${index + 1} is not shown: ${why}`);
  // Spec x down the side: a flat bar chart
  const flat = axes.x && axes.x.screen !== "x";

  const plotWidth = frame.right - frame.left;
  const calloutWidth = calloutWrapWidth(plotWidth);
  const box = (left, top, w, h) => ({ left, top, right: left + w, bottom: top + h });

  annotations.forEach((annotation, index) => {
    if (!annotation || typeof annotation !== "object") return;
    const text = typeof annotation.text === "string" ? annotation.text.trim() : "";
    const [hx0, vy0] = ANCHORS[annotation.anchor] || [null, null];
    const anchored = hx0 !== null;
    const kind = annotation.type;
    if (text) ctx.annotationText.push(text);

    if (kind === "line") {
      const channel = "x" in annotation ? "x" : "y";
      const axis = axes[channel];
      if (!axis) return skip(index, `this chart has no ${channel} axis`);
      const mode = annotation.position || (axis.kind === "band" ? "center" : "before");
      const at = locate(plot, axis, annotation[channel], mode);
      if (at === null) return skip(index, `${describeValue(annotation[channel])} is not on the ${channel} axis`);
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
        const room = (side) => (side > 0 ? frame.right - at : at - frame.left) - GAP - 4;
        const maxWidth = Math.max(60, Math.min(130, Math.max(room(1), room(-1))));
        // The side asked for first, then the other side; top, then bottom
        const sides = anchored ? [[hx0 || 1, vy0 ?? -1]] : [];
        sides.push([1, -1], [-1, -1], [1, 1], [-1, 1], [1, 0], [-1, 0]);
        items.push({
          index,
          text,
          color,
          maxWidth,
          maxLines: 4,
          anchored,
          along: "h",
          pos: at,
          target: { X: at, Y: frame.top },
          badge: { X: at, Y: frame.top + 10 },
          candidates: (w, h) =>
            sides.map(([hx, vy]) => ({
              box: box(
                hx > 0 ? at + GAP : at - GAP - w,
                vy < 0 ? frame.top + 4 : vy > 0 ? frame.bottom - 4 - h : (frame.top + frame.bottom - h) / 2,
                w,
                h
              ),
              align: alignFor(hx),
            })),
        });
      } else {
        const maxWidth = Math.max(60, Math.min(220, plotWidth * 0.55));
        const sides = anchored ? [[hx0, vy0 === 1 ? 1 : -1]] : [];
        sides.push([1, -1], [1, 1], [-1, -1], [-1, 1], [0, -1], [0, 1]);
        items.push({
          index,
          text,
          color,
          maxWidth,
          maxLines: 3,
          anchored,
          along: "v",
          pos: at,
          target: { X: frame.right, Y: at },
          badge: { X: frame.right - 10, Y: at },
          candidates: (w, h) =>
            sides.map(([hx, vy]) => ({
              box: box(
                hx > 0 ? frame.right - 4 - w : hx < 0 ? frame.left + 4 : (frame.left + frame.right - w) / 2,
                vy < 0 ? at - 4 - h : at + 4,
                w,
                h
              ),
              align: alignFor(hx),
            })),
        });
      }
      return;
    }

    if (kind === "range") {
      const channel = "x" in annotation ? "x" : "y";
      const axis = axes[channel];
      if (!axis) return skip(index, `this chart has no ${channel} axis`);
      const exact = axis.kind === "number";
      let a = locate(plot, axis, annotation[channel], exact ? null : "before");
      let b = locate(plot, axis, annotation[`${channel}2`], exact ? null : "after");
      if (a !== null && b !== null && b < a && axis.kind === "band") {
        a = locate(plot, axis, annotation[`${channel}2`], "before");
        b = locate(plot, axis, annotation[channel], "after");
      }
      if (a === null || b === null) {
        const missing = a === null ? annotation[channel] : annotation[`${channel}2`];
        return skip(index, `${describeValue(missing)} is not on the ${channel} axis`);
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
      const center = vertical ? (low + high) / 2 : (low + high) / 2;
      const maxWidth = vertical ? Math.max(60, Math.min(220, high - low - 10)) : Math.max(60, Math.min(220, plotWidth - 10));
      const sides = anchored ? [[hx0, vy0 ?? -1]] : [];
      if (vertical) sides.push([0, -1], [-1, -1], [1, -1], [0, 1]);
      else sides.push([1, -1], [-1, -1], [1, 1], [-1, 1]);
      items.push({
        index,
        text,
        color: annotation.color ? color : theme.ink2,
        maxWidth,
        maxLines: 3,
        anchored,
        along: vertical ? "h" : "v",
        pos: center,
        target: vertical ? { X: center, Y: frame.top } : { X: frame.right, Y: center },
        badge: vertical ? { X: center, Y: frame.top + 10 } : { X: frame.right - 10, Y: center },
        candidates: (w, h) =>
          sides.map(([hx, vy]) => ({
            box: box(
              hx < 0 ? rect.left + 5 : hx > 0 ? rect.right - 5 - w : (rect.left + rect.right - w) / 2,
              vy < 0 ? rect.top + 4 : vy > 0 ? rect.bottom - 4 - h : (rect.top + rect.bottom - h) / 2,
              w,
              h
            ),
            align: alignFor(hx),
          })),
      });
      return;
    }

    if (kind === "point") {
      if (!axes.x || !axes.y) return skip(index, "this chart has no x and y axes");
      const at = pointAt(plot, axes, annotation);
      if (!at) {
        const series = annotation.series ? ` in ${describeValue(annotation.series)}` : "";
        return skip(index, `there is no point at ${describeValue(annotation.x)}${series}`);
      }
      const color = colorFor(theme, annotation.color, theme.ink);
      const marker = annotation.marker !== false;
      if (marker) dots.push({ at, color });
      const distance = marker ? POINT_OFFSET : 0;
      const beside = ([sx, sy], w, h, d) => {
        const cx = at.X + sx * (d * 0.7 + (sx ? w / 2 : 0));
        const cy = at.Y + sy * (d + (sy ? h / 2 : 0));
        return box(cx - w / 2, cy - h / 2, w, h);
      };
      const sides = anchored
        ? [[hx0, vy0]]
        : [[0, -1], [1, -1], [-1, -1], [1, 0], [-1, 0], [0, 1], [1, 1], [-1, 1]];
      items.push({
        index,
        text,
        color,
        maxWidth: calloutWidth,
        maxLines: 4,
        anchored: anchored || !marker,
        callout: true,
        marker,
        at,
        along: flat ? "v" : "h",
        pos: flat ? at.Y : at.X,
        target: at,
        badge: at,
        candidates: (w, h) =>
          [distance, distance + 16, distance + 32].flatMap((d) =>
            sides.map(([sx, sy]) => ({ box: beside([sx, sy], w, h, d), align: alignFor(sx) }))
          ),
      });
      return;
    }

    if (kind === "text") {
      if (typeof annotation.left !== "number" || typeof annotation.top !== "number" || !text) return;
      const X = frame.left + annotation.left * (frame.right - frame.left);
      const Y = frame.top + annotation.top * (frame.bottom - frame.top);
      const hx = hx0 ?? (annotation.left < 0.34 ? 1 : annotation.left > 0.66 ? -1 : 0);
      const vy = vy0 ?? (annotation.top < 0.5 ? 1 : -1);
      const room = hx > 0 ? frame.right - X : hx < 0 ? X - frame.left : plotWidth / 2;
      items.push({
        index,
        text,
        color: colorFor(theme, annotation.color, theme.ink2),
        maxWidth: Math.max(60, Math.min(280, room - 4)),
        maxLines: 3,
        note: true,
        candidates: (w, h) => [
          { box: box(hx > 0 ? X : hx < 0 ? X - w : X - w / 2, vy > 0 ? Y : vy < 0 ? Y - h : Y - h / 2, w, h), align: alignFor(hx) },
        ],
      });
    }
  });

  // --- Where the data is, which no label may cover
  const marks = (axes.marks && axes.marks(plot)) || { rects: [], lines: [] };
  const grow = (b, n) => ({ left: b.left - n, top: b.top - n, right: b.right + n, bottom: b.bottom + n });
  const dotBoxes = dots.map(({ at }) => ({ left: at.X - 5, right: at.X + 5, top: at.Y - 5, bottom: at.Y + 5 }));
  const hitsData = (b) =>
    marks.rects.some((rect) => overlaps(grow(b, 2), rect)) ||
    marks.lines.some((line) => boxHitsLine(grow(b, 3), line)) ||
    dotBoxes.some((dot) => overlaps(b, dot));
  const insideFrame = (b) =>
    b.left >= frame.left + 2 && b.right <= frame.right - 2 && b.top >= frame.top + 2 && b.bottom <= frame.bottom - 2;

  // Wraps a label to a width; null if that is not legible
  const shape = (item, width, maxLines) => {
    const lines = wrap(item.text, width, font);
    const w = Math.max(...lines.map((line) => textWidth(line, font)));
    if (lines.length > maxLines || w > width * 1.25) return null;
    return { lines, w, h: lines.length * LINE_HEIGHT };
  };
  // The first place beside what it labels that is clear of the data, the
  // frame's edges and the labels in `taken`
  const fitInline = (item, taken) => {
    const fitted = shape(item, item.maxWidth, item.maxLines);
    if (!fitted) return null;
    for (const candidate of item.candidates(fitted.w, fitted.h)) {
      const b = candidate.box;
      if (!insideFrame(b) || hitsData(b) || taken.some((other) => overlaps(grow(b, 2), other))) continue;
      return { ...fitted, box: b, align: candidate.align };
    }
    return null;
  };

  // --- Tier 1: everything beside what it labels
  const placements = new Map(); // item -> {how, ...}
  const taken = [];
  let allInline = true;
  const ordered = [...items].sort((a, b) => (b.anchored ? 1 : 0) - (a.anchored ? 1 : 0) || (a.pos ?? 0) - (b.pos ?? 0));
  for (const item of ordered) {
    const spot = fitInline(item, taken);
    if (spot) {
      placements.set(item, { how: "inline", ...spot });
      taken.push(spot.box);
    } else {
      allInline = false;
    }
  }
  const calloutsInOrder = (axis) =>
    inOrder(
      items
        .filter((item) => item.callout && !item.anchored && item.along === axis && placements.has(item))
        .map((item) => {
          const b = placements.get(item).box;
          return { pos: item.pos, center: axis === "h" ? (b.left + b.right) / 2 : (b.top + b.bottom) / 2 };
        })
    );
  let tier = allInline && calloutsInOrder("h") && calloutsInOrder("v") ? "inline" : hint ? hint.tier : "lanes";
  if (tier === "lanes" && !canGrow) tier = "key";

  const plan = { tier, top: 0, side: 0 };
  if (tier !== "inline") {
    // Anchored labels keep their places, as their authors asked, and so do
    // labels on lines across the plot that fit clear of everything. Notes
    // become captions under the chart. The rest move out of the plot.
    // An anchored label with no clear room where it was asked to go moves
    // out like the rest: nothing is drawn over the data
    const keep = new Set();
    const placed = [];
    placements.clear();
    for (const item of items.filter((item) => item.anchored && !item.note)) {
      const spot = fitInline(item, placed);
      if (!spot) continue;
      placements.set(item, { how: "inline", ...spot });
      placed.push(spot.box);
      keep.add(item);
    }
    for (const item of items.filter((item) => !keep.has(item) && !item.note && !item.callout && item.along === "v")) {
      const spot = fitInline(item, placed);
      if (spot) {
        placements.set(item, { how: "inline", ...spot });
        placed.push(spot.box);
        keep.add(item);
      }
    }
    for (const item of items.filter((item) => item.note)) placements.set(item, { how: "caption" });
    const moving = items.filter((item) => !item.note && !keep.has(item));
    const top = moving.filter((item) => item.along === "h");
    const side = moving.filter((item) => item.along === "v");

    let lanesOk = tier === "lanes";
    const laneOut = (group, axis) => {
      if (!group.length) return { ok: true, band: 0 };
      // Narrow first; longer text may take a wider label, up to twice as wide
      const width = axis === "h" ? calloutWidth : Math.min(calloutWidth, 130);
      const shapes = group.map((item) =>
        [1, 1.5, 2].map((k) => shape(item, width * k, 4)).find(Boolean) || null
      );
      if (shapes.some((s) => !s)) return { ok: false };
      const laneItems = group.map((item, i) =>
        axis === "h"
          ? { pos: item.pos, size: shapes[i].w + 8, cross: shapes[i].h + 2 }
          : { pos: item.pos, size: shapes[i].h + 4, cross: shapes[i].w + 8 }
      );
      const chosen = chooseCalloutLayout({
        inline: false,
        items: laneItems,
        start: axis === "h" ? 2 : frame.top,
        end: axis === "h" ? svgWidth - 2 : frame.bottom,
        plotLength: axis === "h" ? plotWidth : frame.bottom - frame.top,
        crossLimit: axis === "h" ? LANE_LIMITS.bandShare * (frame.bottom - frame.top) : LANE_LIMITS.sideShare * svgWidth,
        limits: axis === "h" ? LANE_LIMITS : { ...LANE_LIMITS, maxLanes: 2 },
      });
      if (chosen.tier !== "lanes") return { ok: false };
      group.forEach((item, i) => placements.set(item, { how: "lane", axis, ...shapes[i], spot: chosen.placed[i], thickness: chosen.thickness }));
      return { ok: true, band: chosen.band };
    };
    if (lanesOk) {
      const t = laneOut(top, "h");
      const s = t.ok ? laneOut(side, "v") : { ok: false };
      lanesOk = t.ok && s.ok;
      if (lanesOk) {
        plan.top = t.band;
        plan.side = s.band;
      }
    }
    if (!lanesOk) {
      plan.tier = "key";
      plan.top = 0;
      plan.side = 0;
      for (const item of moving) placements.set(item, { how: "key" });
    }
  }
  ctx.calloutTier = plan.tier;
  svg.setAttribute("data-callout-layout", plan.tier);
  // This pass does not have the room the lanes want: the plot is drawn again
  if (plan.tier === "lanes" && (plan.top > reserved.top || plan.side > reserved.side)) return plan;

  // --- Drawing
  const subdued = { stroke: theme.muted, "stroke-width": 1, "stroke-opacity": 0.8, fill: "none", class: "dc-annotation-leader" };
  const polyline = (points) =>
    front.append(svgEl("polyline", { points: points.map((p) => p.join(",")).join(" "), ...subdued }));
  for (const { at, color } of dots) {
    front.append(svgEl("circle", { cx: at.X, cy: at.Y, r: 3.5, fill: color, stroke: halo, "stroke-width": 1.5 }));
  }
  const keyed = [];
  for (const item of items) {
    const where = placements.get(item);
    if (!where) continue;
    if (where.how === "inline") {
      if (item.callout && item.marker) {
        // A leader from the point to the nearest edge of the label
        const b = where.box;
        const ex = clamp(item.at.X, b.left, b.right);
        const ey = clamp(item.at.Y, b.top, b.bottom);
        const dx = ex - item.at.X;
        const dy = ey - item.at.Y;
        const length = Math.hypot(dx, dy);
        if (length > 8) {
          polyline([
            [item.at.X + (dx / length) * 5, item.at.Y + (dy / length) * 5],
            [ex - (dx / length) * 2, ey - (dy / length) * 2],
          ]);
        }
      }
      drawText(texts, where.lines, where.box, where.align, item.color, halo);
    } else if (where.how === "lane" && where.axis === "h") {
      const gap = LANE_LIMITS.gap;
      const bandBottom = frame.top - 2;
      let y = bandBottom - gap;
      const laneBottom = where.thickness.map((t) => {
        const bottom = y;
        y -= t + gap;
        return bottom;
      });
      const b = { left: where.spot.from + 4, right: where.spot.from + 4 + where.w, bottom: laneBottom[where.spot.lane] - 1 };
      b.top = b.bottom - where.h;
      const leader = where.spot.leader;
      const end = item.callout ? item.target.Y - 5 : item.target.Y;
      polyline(
        Math.abs(leader - item.target.X) < 0.5
          ? [[leader, b.bottom + 2], [item.target.X, end]]
          : [[leader, b.bottom + 2], [leader, bandBottom - 2], [item.target.X, bandBottom + 6], [item.target.X, end]]
      );
      drawText(texts, where.lines, b, "middle", item.color, halo);
    } else if (where.how === "lane") {
      const gap = LANE_LIMITS.gap;
      const bandLeft = svgWidth - (reserved.side || plan.side);
      let x = bandLeft + gap;
      const laneLeft = where.thickness.map((t) => {
        const left = x;
        x += t + gap;
        return left;
      });
      const b = { left: laneLeft[where.spot.lane] + 4, top: where.spot.from + 2 };
      b.right = b.left + where.w;
      b.bottom = b.top + where.h;
      const leader = where.spot.leader;
      const end = item.callout ? item.target.X + 5 : item.target.X;
      polyline(
        Math.abs(leader - item.target.Y) < 0.5
          ? [[b.left - 4, leader], [end, item.target.Y]]
          : [[b.left - 4, leader], [bandLeft + 2, leader], [bandLeft - 6, item.target.Y], [end, item.target.Y]]
      );
      drawText(texts, where.lines, b, "start", item.color, halo);
    } else if (where.how === "key") {
      keyed.push(item);
    } else if (where.how === "caption") {
      ctx.annotationCaptions.push(item.text);
    }
  }
  if (keyed.length) {
    // Numbered badges, numbered across and then down, and the texts in a key
    keyed.sort((a, b) => a.badge.X - b.badge.X || a.badge.Y - b.badge.Y);
    const badges = [];
    keyed.forEach((item, i) => {
      let { X, Y } = item.badge;
      // Badges that would sit on each other step down, or left at the side
      const labelBoxes = [...placements.values()].filter((where) => where.how === "inline").map((where) => where.box);
      for (let tries = 0; tries < 12; tries++) {
        const clash =
          badges.find((other) => Math.hypot(other.X - X, other.Y - Y) < 17) ||
          labelBoxes.find((b) => overlaps({ left: X - 9, right: X + 9, top: Y - 9, bottom: Y + 9 }, b));
        if (!clash) break;
        if (item.along === "v" && !item.callout) X -= 18;
        else Y += 18;
      }
      badges.push({ X, Y });
      const group = svgEl("g", { class: "dc-annotation-marker" });
      group.append(svgEl("circle", { cx: X, cy: Y, r: 8, fill: item.color, stroke: halo, "stroke-width": 1.5 }));
      const label = svgEl("text", { x: X, y: Y + 3.6, "text-anchor": "middle", "font-size": 10, "font-weight": 700, fill: halo });
      label.textContent = String(i + 1);
      group.append(label);
      texts.append(group);
      ctx.annotationKey.push({ number: i + 1, text: item.text, color: item.color, surface: halo });
    });
  }

  front.append(texts);
  const style = svg.querySelector(":scope > style");
  if (behind.childNodes.length) {
    if (style) style.after(behind);
    else svg.prepend(behind);
  }
  const tip = svg.querySelector(':scope > g[aria-label="tip"]');
  if (tip) svg.insertBefore(front, tip);
  else svg.append(front);
  return plan;
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
