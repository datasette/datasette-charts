// datasette-charts: the annotations part of the chart builder.
//
// A list of the chart's annotations, with buttons to add, edit and remove
// them, and a dialog for editing one with a live preview of the chart. The
// preview is drawn from the rows the chart already has: annotations do not
// change the rows, so nothing is fetched while you type.
//
// The dialog is Datasette's own <datasette-modal>.

import { h, renderChart } from "./draw.js";
import { categoryName } from "./renderers/common.js";
import { parseMoment } from "./renderers/annotations.js";

const KINDS = [
  { value: "line-x", type: "line", axis: "x" },
  { value: "line-y", type: "line", axis: "y" },
  { value: "range-x", type: "range", axis: "x" },
  { value: "range-y", type: "range", axis: "y" },
  { value: "point", type: "point" },
  { value: "text", type: "text" },
];
const ANCHORS = [
  ["", "Automatic"],
  ["top-left", "Top left"],
  ["top", "Top"],
  ["top-right", "Top right"],
  ["left", "Left"],
  ["center", "Centre"],
  ["right", "Right"],
  ["bottom-left", "Bottom left"],
  ["bottom", "Bottom"],
  ["bottom-right", "Bottom right"],
];
const COLORS = ["", "blue", "orange", "green", "yellow", "pink", "red", "purple", "gray"];
const MAX_TEXT = 200;

let dialogCount = 0;

const clone = (value) => JSON.parse(JSON.stringify(value));
const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function rowsOf(result) {
  if (!result || !result.rows) return [];
  return result.rows.map((row) => Object.fromEntries(result.columns.map((name, i) => [name, row[i]])));
}

const unique = (values) => [...new Set(values.filter((value) => value !== null && value !== undefined))];

// --- What the chart's axes are ---------------------------------------------------

// "band", "time" or "number" for a spec channel, given what the chart drew
export function axisKind(type, spec, channel, result) {
  const declared = ((type.annotations || {}).axes || {})[channel];
  if (!declared) return null;
  if (declared === "category") return result && result.meta && result.meta.timeUnit ? "time" : "band";
  return declared;
}

// Which way a spec channel runs on the screen
function screenAxis(spec, channel, result) {
  const flat =
    (spec.type === "bar" && (spec.options || {}).orientation !== "vertical") ||
    (spec.type === "stacked_bar" && !(result && result.meta && result.meta.timeUnit));
  if (!flat) return channel;
  return channel === "x" ? "y" : "x";
}

// The x values the chart shows: categories or time buckets
function axisValues(spec, result) {
  const rows = rowsOf(result);
  if (spec.type === "bar") return unique(rows.filter((d) => !d.is_other).map((d) => categoryName(d.key, false, d.label)));
  if (spec.type === "stacked_bar") {
    return unique(rows.filter((d) => !d.key_is_other).map((d) => categoryName(d.key, false, d.key_label)));
  }
  if (spec.type === "line") return unique(rows.map((d) => String(d.bucket)));
  return [];
}

function seriesValues(spec, result) {
  const rows = rowsOf(result);
  if (spec.type === "line" && result && result.columns.includes("series")) {
    return unique(rows.filter((d) => !d.is_other).map((d) => categoryName(d.series, false, d.series_label)));
  }
  if (spec.type === "stacked_bar") {
    return unique(rows.filter((d) => !d.series_is_other).map((d) => categoryName(d.series, false, d.series_label)));
  }
  return [];
}

function channelName(spec, channel, result) {
  const encoding = (spec.encoding || {})[channel] || {};
  if (encoding.column) return encoding.column;
  if (channel === "y" && result && result.meta && typeof result.meta.y === "string") return result.meta.y;
  return channel;
}

// --- Describing an annotation -------------------------------------------------------

const show = (value) => (typeof value === "string" ? value : JSON.stringify(value));

export function summarize(annotation, spec, result) {
  const where = (channel) => `${channelName(spec, channel, result)} ${show(annotation[channel])}`;
  if (annotation.type === "line") {
    const channel = "x" in annotation ? "x" : "y";
    const position = { before: "before", after: "after" }[annotation.position] || "at";
    return `Line ${position} ${where(channel)}`;
  }
  if (annotation.type === "range") {
    const channel = "x" in annotation ? "x" : "y";
    return `Shaded ${where(channel)} to ${show(annotation[`${channel}2`])}`;
  }
  if (annotation.type === "point") {
    const series = annotation.series ? `, ${annotation.series}` : "";
    return `Callout at ${where("x")}${series}`;
  }
  if (annotation.type === "text") {
    return `Text note at ${Math.round(annotation.left * 100)}% across, ${Math.round(annotation.top * 100)}% down`;
  }
  return "Annotation";
}

// --- The list in the builder ------------------------------------------------------

// The builder's annotations section. `panel` provides current, types,
// lastResult, urls and setAnnotations(list, focusIndex).
export function annotationSection(panel) {
  const spec = panel.current.spec;
  const type = panel.types[spec.type];
  const allowed = ((type && type.annotations) || {}).types || [];
  if (!allowed.length) return null;
  const annotations = Array.isArray(spec.annotations) ? spec.annotations : [];
  const result = panel.lastResult;
  const items = annotations.map((annotation, index) => {
    const summary = summarize(annotation, spec, result);
    const text = typeof annotation.text === "string" && annotation.text.trim() ? annotation.text : "";
    const name = `annotation ${index + 1}: ${summary}`;
    return h(
      "li",
      { class: "dc-annotation-item" },
      h(
        "span",
        { class: "dc-annotation-summary" },
        h("span", { class: "dc-annotation-kind" }, summary),
        text ? h("span", { class: "dc-annotation-text" }, text) : null
      ),
      h(
        "span",
        { class: "dc-annotation-actions" },
        h(
          "button",
          {
            type: "button",
            class: "dc-link",
            "data-annotation-edit": index,
            "aria-label": `Edit ${name}`,
            onclick: () => openAnnotationDialog(panel, index),
          },
          "Edit"
        ),
        h(
          "button",
          {
            type: "button",
            class: "dc-link",
            "aria-label": `Remove ${name}`,
            onclick: () => {
              const next = annotations.filter((_, i) => i !== index);
              panel.setAnnotations(next, next.length ? Math.min(index, next.length - 1) : "add");
            },
          },
          "Remove"
        )
      )
    );
  });
  const atLimit = annotations.length >= 20;
  return h(
    "fieldset",
    { class: "dc-channel dc-annotations-field" },
    h("legend", { class: "dc-legend" }, "Annotations"),
    items.length
      ? h("ul", { class: "dc-annotation-list" }, items)
      : h("p", { class: "dc-annotation-empty" }, allowed.length === 1 ? "Add a text note to this chart." : "Add reference lines, shaded ranges and notes to this chart."),
    h(
      "button",
      {
        type: "button",
        class: "dc-link dc-annotation-add",
        "data-annotation-add": "",
        disabled: atLimit,
        onclick: () => openAnnotationDialog(panel, null),
      },
      "Add annotation"
    )
  );
}

// --- The dialog -------------------------------------------------------------------

// The dialog goes in Datasette's <datasette-modal>, which every Datasette
// page loads: it traps focus, closes on Escape and gives focus back after
function modalFor(dialog) {
  const modal = document.createElement("datasette-modal");
  dialog.classList.add("datasette-modal");
  modal.append(dialog);
  document.body.append(modal);
  return {
    show: (initialFocus) => modal.show({ initialFocus }),
    close: () => modal.close(),
    remove: () => modal.remove(),
    cancel: () => modal.requestClose("cancel"),
  };
}

// Opens the editor for annotation `index`, or for a new one if it is null
export async function openAnnotationDialog(panel, index) {
  // modal.js is a deferred script: it may still be loading
  await customElements.whenDefined("datasette-modal");
  const spec = panel.current.spec;
  const type = panel.types[spec.type];
  const support = type.annotations || {};
  const allowed = support.types || [];
  const result = panel.lastResult;
  const annotations = Array.isArray(spec.annotations) ? clone(spec.annotations) : [];
  const existing = index === null ? null : annotations[index];
  const id = `dc-annotation-dialog-${++dialogCount}`;

  const kindOf = (annotation) => {
    if (!annotation) return null;
    if (annotation.type === "line" || annotation.type === "range") return `${annotation.type}-${"x" in annotation ? "x" : "y"}`;
    return annotation.type;
  };
  const axes = support.axes || {};
  const kinds = KINDS.filter(
    (kind) => allowed.includes(kind.type) && (!kind.axis || axes[kind.axis])
  );
  const kindLabel = (kind) => {
    const direction = (channel) => (screenAxis(spec, channel, result) === "x" ? "Vertical" : "Horizontal");
    const name = (channel) => channelName(spec, channel, result);
    switch (kind.value) {
      case "line-x":
        return `${direction("x")} line at a ${name("x")}`;
      case "line-y":
        return `${direction("y")} line at a ${name("y")}`;
      case "range-x":
        return `Shaded range of ${name("x")} values`;
      case "range-y":
        return `Shaded range of ${name("y")} values`;
      case "point":
        return "Callout on a data point";
      default:
        return "Text note";
    }
  };
  const xKind = axisKind(type, spec, "x", result);
  const values = axisValues(spec, result);
  const series = support.series ? seriesValues(spec, result) : [];

  // --- Controls ---
  const field = (label, control, hint) => {
    const input = control.id ? control : control.querySelector("input, select, textarea");
    const hintId = hint ? `${input.id}-hint` : null;
    if (hintId) input.setAttribute("aria-describedby", hintId);
    return h(
      "div",
      { class: "dc-field" },
      h("label", { for: input.id }, label),
      control,
      hint ? h("span", { class: "dc-hint", id: hintId }, hint) : null
    );
  };
  const control = (name, tag, attrs, ...children) => h(tag, { id: `${id}-${name}`, name, ...attrs }, ...children);
  const select = (name, options, value) =>
    control(
      name,
      "select",
      {},
      options.map(([optionValue, label]) => h("option", { value: optionValue, selected: optionValue === value }, label))
    );

  // An x value: a choice of the chart's categories, a time with the chart's
  // buckets suggested, or a number
  const xControl = (name, value) => {
    const current = value === undefined || value === null ? "" : String(value);
    if (xKind === "band" && values.length) {
      const options = values.map((v) => [v, v]);
      if (current && !values.includes(current)) options.unshift([current, `${current} (not on this chart)`]);
      if (!current) options.unshift(["", "Choose…"]);
      return select(name, options, current);
    }
    if (xKind === "number") return control(name, "input", { type: "number", step: "any", value: current });
    const list = `${id}-${name}-values`;
    return h(
      "span",
      { class: "dc-combo" },
      control(name, "input", { type: "text", list, value: current, autocomplete: "off", spellcheck: "false" }),
      h("datalist", { id: list }, values.map((v) => h("option", { value: v })))
    );
  };
  const xHint = xKind === "time" ? "A year, month or date, such as 2009, 2009-06 or 2009-06-15" : null;
  const yName = channelName(spec, "y", result);
  const xName = channelName(spec, "x", result);
  const numberValue = (value) => (typeof value === "number" ? String(value) : "");

  const initial = existing || {};
  const kindSelect = select(
    "kind",
    kinds.map((kind) => [kind.value, kindLabel(kind)]),
    kindOf(existing) || kinds[0].value
  );
  const positionLabels =
    xKind === "time"
      ? [
          ["before", "At its start"],
          ["center", "In its middle"],
          ["after", "At its end"],
        ]
      : [
          ["center", "Through the middle of it"],
          ["before", "Before it, between it and the one before"],
          ["after", "After it, between it and the next"],
        ];
  const fields = {
    x: field(`${capitalize(xName)}`, xControl("x", initial.x), xHint),
    position: field("Place the line", select("position", positionLabels, initial.position || positionLabels[0][0])),
    x2: field(`To ${xName}`, xControl("x2", initial.x2), xHint),
    y: field(capitalize(yName), control("y", "input", { type: "number", step: "any", value: numberValue(initial.y) })),
    y2: field(`To ${yName}`, control("y2", "input", { type: "number", step: "any", value: numberValue(initial.y2) })),
    series: field(
      "Series",
      select(
        "series",
        [["", "The largest, or the whole stack"], ...series.map((s) => [s, s])],
        initial.series || ""
      )
    ),
    left: field(
      "Across, %",
      control("left", "input", {
        type: "number",
        min: 0,
        max: 100,
        step: 1,
        value: typeof initial.left === "number" ? String(Math.round(initial.left * 100)) : "5",
      }),
      "From the left of the plot"
    ),
    top: field(
      "Down, %",
      control("top", "input", {
        type: "number",
        min: 0,
        max: 100,
        step: 1,
        value: typeof initial.top === "number" ? String(Math.round(initial.top * 100)) : "5",
      }),
      "From the top of the plot"
    ),
    text: field(
      "Text",
      control("text", "textarea", { rows: 3, maxlength: MAX_TEXT }, initial.text || ""),
      "Long text wraps. A new line starts a new line."
    ),
    anchor: field("Label position", select("anchor", ANCHORS, initial.anchor || "")),
    color: field(
      "Color",
      select(
        "color",
        COLORS.map((c) => [c, c ? capitalize(c) : "Default"]),
        COLORS.includes(initial.color) ? initial.color || "" : ""
      )
    ),
    stroke: field(
      "Line style",
      select(
        "stroke",
        [
          ["dashed", "Dashed"],
          ["solid", "Solid"],
          ["dotted", "Dotted"],
        ],
        initial.stroke || "dashed"
      )
    ),
    marker: h(
      "label",
      { class: "dc-check" },
      control("marker", "input", { type: "checkbox", checked: initial.marker !== false }),
      "Mark the point, with a line to the text"
    ),
  };
  // A hex color from the spec is kept, and offered as it is
  if (initial.color && !COLORS.includes(initial.color)) {
    const colorSelect = fields.color.querySelector("select");
    colorSelect.prepend(h("option", { value: initial.color, selected: true }, initial.color));
  }
  const yHint = h("span", { class: "dc-hint" });
  const error = h("p", { class: "dc-dialog-error", role: "alert" });
  const form = h(
    "form",
    { class: "dc-annotation-form", novalidate: true, onsubmit: (event) => event.preventDefault() },
    field("Kind", kindSelect),
    Object.values(fields),
    error
  );
  const preview = h("div", { class: "dc-chart dc-annotation-preview", role: "img" });
  const previewNote = h("p", { class: "dc-note", "aria-live": "polite" });
  const save = h("button", { type: "submit", class: "modal-btn modal-btn-primary", form: `${id}-form` }, existing ? "Save" : "Add");
  form.id = `${id}-form`;
  const cancel = h("button", { type: "button", class: "modal-btn modal-btn-ghost" }, "Cancel");
  const titleId = `${id}-title`;
  const dialog = h(
    "dialog",
    { class: "dc-annotation-dialog", "aria-labelledby": titleId },
    h(
      "div",
      { class: "modal-header" },
      h("h2", { class: "modal-title", id: titleId }, existing ? "Edit annotation" : "Add an annotation"),
      panel.current.title ? h("span", { class: "modal-meta" }, panel.current.title) : null
    ),
    h(
      "div",
      { class: "modal-body dc-annotation-body" },
      form,
      h(
        "div",
        { class: "dc-annotation-preview-col" },
        h("p", { class: "dc-preview-label", "aria-hidden": "true" }, "Preview"),
        result ? preview : h("p", { class: "dc-note" }, "The chart has to draw before it can be previewed."),
        previewNote
      )
    ),
    h("div", { class: "modal-footer" }, cancel, save)
  );
  const modal = modalFor(dialog);

  // --- Reading the form ---
  const value = (name) => form.elements[name] && form.elements[name].value.trim();
  const parseX = (raw) => {
    if (raw === "" || raw === undefined) return undefined;
    if (xKind === "number") return Number(raw);
    // A category that is a number in the data stays as typed: names match
    return raw;
  };
  const parseNumber = (raw) => (raw === "" || raw === undefined ? undefined : Number(raw));

  // Returns [annotation, problems]
  function read() {
    const kind = KINDS.find((k) => k.value === value("kind"));
    const annotation = { type: kind.type };
    const problems = [];
    const text = form.elements.text.value.replace(/\s+$/, "");
    if (text) annotation.text = text;
    const need = (name, label, parsed, check) => {
      if (parsed === undefined || (typeof parsed === "number" && !Number.isFinite(parsed))) {
        problems.push([name, `${label} is needed`]);
        return;
      }
      if (check && !check(parsed)) {
        problems.push([name, `${label}: ${check.message}`]);
        return;
      }
      annotation[name] = parsed;
    };
    const isTime = (v) => !!parseMoment(v);
    isTime.message = "write a year, month or date, such as 2009, 2009-06 or 2009-06-15";
    const xCheck = xKind === "time" ? isTime : null;
    if (kind.value === "line-x") {
      need("x", capitalize(xName), parseX(value("x")), xCheck);
      annotation.position = value("position");
      if (annotation.position === (xKind === "time" ? "before" : "center")) delete annotation.position;
    } else if (kind.value === "line-y") {
      need("y", capitalize(yName), parseNumber(value("y")));
    } else if (kind.value === "range-x") {
      need("x", capitalize(xName), parseX(value("x")), xCheck);
      need("x2", `To ${xName}`, parseX(value("x2")), xCheck);
    } else if (kind.value === "range-y") {
      need("y", capitalize(yName), parseNumber(value("y")));
      need("y2", `To ${yName}`, parseNumber(value("y2")));
    } else if (kind.value === "point") {
      need("x", capitalize(xName), parseX(value("x")), xCheck);
      const y = parseNumber(value("y"));
      if (y !== undefined || support.lookup === false) need("y", capitalize(yName), y);
      if (value("series")) annotation.series = value("series");
      if (!form.elements.marker.checked) annotation.marker = false;
      if (!text) problems.push(["text", "Text is needed"]);
    } else if (kind.value === "text") {
      const percent = (v) => v >= 0 && v <= 100;
      percent.message = "use a number from 0 to 100";
      need("left", "Across", parseNumber(value("left")), percent);
      need("top", "Down", parseNumber(value("top")), percent);
      if ("left" in annotation) annotation.left = Math.round(annotation.left) / 100;
      if ("top" in annotation) annotation.top = Math.round(annotation.top) / 100;
      if (!text) problems.push(["text", "Text is needed"]);
    }
    if (kind.type === "line" && value("stroke") !== "dashed") annotation.stroke = value("stroke");
    if (value("anchor")) annotation.anchor = value("anchor");
    if (value("color")) annotation.color = value("color");
    if (text.length > MAX_TEXT) problems.push(["text", `Text can be at most ${MAX_TEXT} characters`]);
    return [annotation, problems];
  }

  // Shows the fields this kind of annotation takes
  function arrange() {
    const kind = value("kind");
    const shown = {
      x: ["line-x", "range-x", "point"],
      position: ["line-x"],
      x2: ["range-x"],
      y: ["line-y", "range-y", "point"],
      y2: ["range-y"],
      series: series.length ? ["point"] : [],
      left: ["text"],
      top: ["text"],
      stroke: ["line-x", "line-y"],
      marker: ["point"],
    };
    for (const [name, kinds] of Object.entries(shown)) fields[name].hidden = !kinds.includes(kind);
    // Positions only mean something on a category or time axis
    if (xKind === "number") fields.position.hidden = true;
    const yLabel = fields.y.querySelector("label");
    yLabel.textContent =
      kind === "point" && support.lookup !== false ? `${capitalize(yName)} (optional)` : capitalize(yName);
    if (kind === "point" && support.lookup !== false) {
      yHint.textContent = "Leave empty to use the chart's own value";
      fields.y.append(yHint);
    } else {
      yHint.remove();
    }
    fields.x.querySelector("label").textContent = kind === "range-x" ? `From ${xName}` : capitalize(xName);
    fields.y.querySelector("label").textContent =
      kind === "range-y" ? `From ${yName}` : fields.y.querySelector("label").textContent;
    fields.text.querySelector("label").textContent = ["point", "text"].includes(kind) ? "Text" : "Label (optional)";
  }

  // --- Preview ---
  let frame = null;
  let drawing = Promise.resolve();
  function schedulePreview() {
    if (!result) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      drawing = drawing.then(drawPreview);
    });
  }
  async function drawPreview() {
    const [annotation, problems] = read();
    const list = clone(annotations);
    // An incomplete annotation is left out of the preview, not shown wrong
    const usable = !problems.some(([name]) => name !== "text") && (annotation.text || !["point", "text"].includes(annotation.type));
    if (usable) {
      if (index === null) list.push(annotation);
      else list[index] = annotation;
    } else if (index !== null) {
      list.splice(index, 1);
    }
    const previewSpec = { ...clone(spec), annotations: list };
    const holder = h("div", {});
    preview.append(holder);
    holder.style.cssText = "position:absolute;visibility:hidden;width:100%";
    const drawn = await renderChart(holder, {
      urls: panel.urls,
      types: panel.types,
      spec: previewSpec,
      result,
      measureFrom: preview,
    });
    holder.style.cssText = "";
    preview.replaceChildren(holder);
    const position = index === null ? list.length - 1 : index;
    const mine = (drawn.annotationNotes || []).filter((note) => note.startsWith(`Annotation ${position + 1} `));
    previewNote.textContent = mine.length
      ? mine.map((note) => note.replace(/^Annotation \d+ is/, "This annotation is") + ".").join(" ")
      : usable
        ? ""
        : "Fill in the highlighted fields to see this annotation.";
    preview.setAttribute("aria-label", `Preview: ${summarize(annotation, spec, result)}`);
  }

  function showProblems(problems) {
    for (const el of form.querySelectorAll("[aria-invalid]")) el.removeAttribute("aria-invalid");
    if (!problems.length) {
      error.textContent = "";
      return;
    }
    error.textContent = problems.map(([, message]) => message).join(". ") + ".";
    for (const [name] of problems) {
      const el = form.elements[name];
      if (el) el.setAttribute("aria-invalid", "true");
    }
    const first = form.elements[problems[0][0]];
    if (first) first.focus();
  }

  form.addEventListener("input", schedulePreview);
  form.addEventListener("change", (event) => {
    if (event.target.name === "kind") arrange();
    schedulePreview();
  });
  cancel.addEventListener("click", () => modal.cancel());
  form.addEventListener("submit", () => {
    const [annotation, problems] = read();
    if (problems.length) {
      showProblems(problems);
      return;
    }
    if (index === null) annotations.push(annotation);
    else annotations[index] = annotation;
    modal.close();
    panel.setAnnotations(annotations, index === null ? annotations.length - 1 : index);
  });
  dialog.addEventListener("close", () => setTimeout(() => modal.remove(), 0));

  arrange();
  modal.show(existing ? form.elements.text : kindSelect);
  // The preview measures the dialog, so it is drawn once that is open
  schedulePreview();
}
