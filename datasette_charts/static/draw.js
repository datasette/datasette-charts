// datasette-charts: what the Charts panel and <datasette-chart> share.
// Fetching a chart's rows, drawing them, and copying the rows behind it.

import * as common from "./renderers/common.js";
import * as annotations from "./renderers/annotations.js";

// The helpers every renderer is handed as `ctx.helpers`: the public part of
// what the built-in renderers draw with, so that a chart type from another
// plugin can look like them and draw annotations. These are the very
// functions the built-in renderers use. Only the names listed here are part
// of the interface: the rest of common.js and annotations.js can change.
// Add to this list freely; renaming or removing a name breaks other plugins.
export const HELPERS = Object.freeze({
  // Setting up and mounting a plot
  TIP: Object.freeze({ ...common.TIP }),
  basePlot: common.basePlot,
  mount: common.mount,
  isThumbnail: common.isThumbnail,
  xLabel: common.xLabel,
  valueAxisMargin: common.valueAxisMargin,
  // Numbers, names and crowded category labels
  fmt: common.fmt,
  fmtCompact: common.fmtCompact,
  truncate: common.truncate,
  categoryName: common.categoryName,
  uniqueNames: common.uniqueNames,
  seriesColor: common.seriesColor,
  textWidth: common.textWidth,
  fitBandLabels: common.fitBandLabels,
  orderedValues: common.orderedValues,
  // Time buckets, as the server labels them
  bucketToDate: common.bucketToDate,
  stepDate: common.stepDate,
  fmtBucket: common.fmtBucket,
  allBuckets: common.allBuckets,
  // Annotations
  annotate: annotations.annotate,
  bandMatcher: annotations.bandMatcher,
  parseMoment: annotations.parseMoment,
});

export const THUMBNAIL = { width: 208, height: 112 };
export const FULL_HEIGHT = 340;

// --- Small helpers ------------------------------------------------------------

// Text always goes in as text nodes: titles, reasons and column names are data
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

const scripts = new Map();
export function loadScript(url) {
  if (!scripts.has(url)) {
    scripts.set(
      url,
      new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = url;
        script.onload = resolve;
        script.onerror = () => reject(new Error(`Could not load ${url}`));
        document.head.append(script);
      })
    );
  }
  return scripts.get(url);
}

// Observable Plot and D3 are only loaded when a chart is about to be drawn
export async function loadPlot(urls) {
  if (!(window.Plot && window.d3)) {
    await loadScript(urls.d3);
    await loadScript(urls.plot);
  }
  return { Plot: window.Plot, d3: window.d3 };
}

export async function getJSON(url, params) {
  const query = params ? "?" + new URLSearchParams(params) : "";
  try {
    const response = await fetch(url + query, { headers: { Accept: "application/json" } });
    return await response.json();
  } catch (e) {
    return { ok: false, error: "The request failed" };
  }
}

export function errorMessage(result) {
  if (result.error === "timeout") return "This took too long to work out.";
  return result.error || "Something went wrong";
}

export const count = (n, noun) => `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;

// --- Drawing ------------------------------------------------------------------

// Fetches the rows for a spec and draws them into el. Resolves to the data
// endpoint's response, or to {ok: false, error} if the chart cannot be drawn.
// `types` is {name: chart type} from types.json. `measureFrom` is the element
// whose width and background the chart should fit, if that is not el.
export async function drawChart(el, { urls, types, source, spec, size = "full", measureFrom }) {
  const result = await getJSON(urls.data, {
    source: JSON.stringify(source),
    spec: JSON.stringify(spec),
    size,
  });
  if (!result.ok) return result;
  return renderChart(el, { urls, types, spec, result, size, measureFrom });
}

// Draws rows already fetched for a spec. Annotations do not change the rows,
// so a spec that differs only in its annotations can be drawn again from the
// same result without asking the server. The result comes back with
// `annotationNotes`, saying which annotations could not be placed, and
// `annotationText`, the text of the ones that were.
export async function renderChart(el, { urls, types, spec, result, size = "full", measureFrom }) {
  const type = types[spec.type];
  const ctx = {
    spec,
    rows: result.rows,
    columns: result.columns,
    data: result.rows.map((row) => Object.fromEntries(result.columns.map((name, i) => [name, row[i]]))),
    meta: result.meta || {},
    size,
    annotationNotes: [],
    annotationText: [],
    annotationKey: [],
    annotationCaptions: [],
  };
  try {
    const [{ Plot, d3 }, renderer] = await Promise.all([loadPlot(urls), import(type.js_url)]);
    const thumbnail = size === "thumbnail";
    const box = measureFrom || el;
    const width = thumbnail ? THUMBNAIL.width : Math.max(320, Math.floor(box.clientWidth));
    await renderer.render(el, {
      ...ctx,
      width,
      height: thumbnail ? THUMBNAIL.height : FULL_HEIGHT,
      Plot,
      d3,
      theme: common.themeFor(box),
      urls,
      helpers: HELPERS,
      loadScript,
      // Renderers add to these
      annotationNotes: ctx.annotationNotes,
      annotationText: ctx.annotationText,
      annotationKey: ctx.annotationKey,
      annotationCaptions: ctx.annotationCaptions,
    });
    // Notes with no clear room on the plot go under it, as captions
    for (const caption of ctx.annotationCaptions) el.append(h("p", { class: "dc-annotation-caption" }, caption));
    // Labels with no room on the plot or in lanes beside it are
    // numbered on the chart, and their texts listed under it
    if (ctx.annotationKey.length) el.append(annotationKeyList(ctx.annotationKey));
  } catch (e) {
    console.error("datasette-charts could not draw a chart", e);
    return { ok: false, error: "This chart could not be drawn" };
  }
  return { ...result, annotationNotes: ctx.annotationNotes, annotationText: ctx.annotationText };
}

// The numbered key for callouts drawn as numbered markers. Real text, so it
// wraps, can be selected and is read out.
function annotationKeyList(key) {
  return h(
    "ol",
    { class: "dc-annotation-key", "aria-label": "Notes on the chart" },
    key.map((item) =>
      h(
        "li",
        { value: item.number },
        h("span", { class: "dc-annotation-key-number", "aria-hidden": "true", style: `background:${item.color};color:${item.surface}` }, String(item.number)),
        h("span", { class: "dc-annotation-key-text" }, item.text)
      )
    )
  );
}

// The text alternative for a drawn chart: its title, then its annotations
export function chartLabel(result, ...extra) {
  const parts = [result.title, ...extra].filter(Boolean);
  if (result.annotationText && result.annotationText.length) {
    parts.push(`Annotations: ${result.annotationText.join("; ")}`);
  }
  return parts.join(". ");
}

export function truncationNote(result) {
  const notes = [];
  if (result.truncated) {
    notes.push(
      `Only the first ${count(result.rows.length, "row")} of this chart's data ${result.rows.length === 1 ? "is" : "are"} shown.`
    );
  }
  for (const note of result.annotationNotes || []) notes.push(`${note}.`);
  return notes.join(" ");
}

// Copies text to the clipboard. The Clipboard API only exists on secure
// pages: over plain HTTP, such as a Datasette reached by a LAN or Tailscale
// address, it is missing, so the older execCommand("copy") is used instead.
// That has to run during the click, before anything is awaited.
function copyWithSelection(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none";
  const focused = document.activeElement;
  document.body.append(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (e) {
    ok = false;
  }
  area.remove();
  if (focused && focused.focus) focused.focus({ preventScroll: true });
  return ok;
}

export async function copyText(text) {
  if (!(navigator.clipboard && window.isSecureContext)) return copyWithSelection(text);
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    return copyWithSelection(text);
  }
}

// A button that copies text to the clipboard, says whether it worked, then
// goes back to its own label. If nothing can be copied, the text is shown
// beside the button, selected, to be copied by hand. `name` is a fuller
// accessible name for a short label, such as "Copy link to this chart" for
// "Copy link".
export function copyButton(label, done, getText, failed = "Could not copy", name = null) {
  let timer = null;
  const button = h(
    "button",
    {
      type: "button",
      class: "dc-link",
      "aria-label": name,
      title: name,
      onclick: async () => {
        const text = getText();
        const ok = await copyText(text);
        // What it says now is what it is called now, for screen readers too
        button.textContent = ok ? done : failed;
        if (name) button.setAttribute("aria-label", ok ? done : failed);
        clearTimeout(timer);
        timer = setTimeout(() => {
          button.textContent = label;
          if (name) button.setAttribute("aria-label", name);
        }, ok ? 2000 : 5000);
        const previous = button.nextElementSibling;
        if (previous && previous.classList.contains("dc-copy-fallback")) previous.remove();
        if (!ok) {
          const multiline = text.includes("\n");
          const field = h(multiline ? "textarea" : "input", {
            class: "dc-copy-fallback",
            readonly: true,
            rows: multiline ? 4 : null,
            "aria-label": `${label}: select and copy this`,
          });
          field.value = text;
          button.after(field);
          field.focus();
          field.select();
        }
      },
    },
    label
  );
  return button;
}

// The absolute address of a chart: its source's page, with the spec, and any
// annotations, in the #chart= fragment. `page` is that page's address, with
// or without a fragment.
export function chartLink(page, spec) {
  const url = new URL(page, location.href);
  url.hash = "chart=" + encodeURIComponent(JSON.stringify(spec));
  return url.href;
}

function delimited(result, separator) {
  const cell =
    separator === "\t"
      ? // Tabs and line breaks would split a TSV cell, so they become spaces
        (value) => String(value).replace(/[\t\r\n]+/g, " ")
      : (value) => {
          const text = String(value);
          return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
        };
  return [result.columns, ...result.rows]
    .map((row) => row.map((value) => (value === null || value === undefined ? "" : cell(value))).join(separator))
    .join("\n");
}

// The aggregated rows behind the chart, ready to paste into a spreadsheet
export function copyDataButtons(result) {
  return [
    copyButton("Copy TSV", "TSV copied", () => delimited(result, "\t")),
    copyButton("Copy CSV", "CSV copied", () => delimited(result, ",")),
  ];
}
