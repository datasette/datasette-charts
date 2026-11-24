// datasette-charts: what the Charts panel and <datasette-chart> share.
// Fetching a chart's rows, drawing them, and copying the rows behind it.

import { themeFor } from "./renderers/common.js";

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
  const type = types[spec.type];
  try {
    const [{ Plot, d3 }, renderer] = await Promise.all([loadPlot(urls), import(type.js_url)]);
    const thumbnail = size === "thumbnail";
    const box = measureFrom || el;
    const width = thumbnail ? THUMBNAIL.width : Math.max(320, Math.floor(box.clientWidth));
    await renderer.render(el, {
      spec,
      rows: result.rows,
      columns: result.columns,
      data: result.rows.map((row) => Object.fromEntries(result.columns.map((name, i) => [name, row[i]]))),
      meta: result.meta || {},
      size,
      width,
      height: thumbnail ? THUMBNAIL.height : FULL_HEIGHT,
      Plot,
      d3,
      theme: themeFor(box),
      urls,
      loadScript,
    });
  } catch (e) {
    console.error("datasette-charts could not draw a chart", e);
    return { ok: false, error: "This chart could not be drawn" };
  }
  return result;
}

export function truncationNote(result) {
  if (!result.truncated) return "";
  return `Only the first ${count(result.rows.length, "row")} of this chart's data ${result.rows.length === 1 ? "is" : "are"} shown.`;
}

// A button that copies text to the clipboard, says whether it worked, then
// goes back to its own label
export function copyButton(label, done, getText, failed = "Could not copy") {
  let timer = null;
  return h(
    "button",
    {
      type: "button",
      class: "dc-link",
      onclick: async (event) => {
        const button = event.currentTarget;
        let ok = true;
        try {
          await navigator.clipboard.writeText(getText());
        } catch (e) {
          ok = false;
        }
        button.textContent = ok ? done : failed;
        clearTimeout(timer);
        timer = setTimeout(() => (button.textContent = label), ok ? 2000 : 5000);
      },
    },
    label
  );
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
