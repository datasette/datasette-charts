// datasette-charts: the suggestions strip, the chart view and the manual builder.
//
// The page provides a container with a JSON bootstrap (the source descriptor
// and endpoint URLs). Everything else is fetched from the plugin's endpoints.
// Suggestions and the builder both produce specs in the chart DSL, and the
// server only ever compiles specs.

import { annotationSection } from "./annotation-editor.js";
import { parseMoment } from "./renderers/annotations.js";
import { chartLabel, copyButton, copyDataButtons, count, drawChart, errorMessage, getJSON, h, truncationNote } from "./draw.js";

const COLLAPSED_KEY = "datasette-charts:collapsed";
const SELECTED_PREFIX = "datasette-charts:selected:";
const FRAGMENT = "#chart=";

// --- Small helpers ------------------------------------------------------------

// Runs at most `limit` tasks at once: SQL threads are scarce
class Queue {
  constructor(limit) {
    this.limit = Math.max(1, limit || 2);
    this.active = 0;
    this.waiting = [];
  }
  add(task) {
    return new Promise((resolve) => {
      this.waiting.push(() => task().then(resolve, resolve));
      this.next();
    });
  }
  next() {
    while (this.active < this.limit && this.waiting.length) {
      this.active += 1;
      this.waiting.shift()().finally(() => {
        this.active -= 1;
        this.next();
      });
    }
  }
}

const clone = (value) => JSON.parse(JSON.stringify(value));

// Storage can be unavailable or full. Nothing here depends on it working.
function recall(storageName, key) {
  try {
    return window[storageName].getItem(key);
  } catch (e) {
    return null;
  }
}

function remember(storageName, key, value) {
  try {
    if (value === null) window[storageName].removeItem(key);
    else window[storageName].setItem(key, value);
  } catch (e) {
    // Not being able to remember is fine
  }
}

// JSON with object keys in a fixed order, for comparing specs
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

// A suggested spec carries bins worked out from the data it was suggested
// for. Without them the server works bins out again from the current data.
function withoutDerivedBins(spec) {
  const copy = clone(spec);
  for (const channel of Object.values(copy.encoding || {})) {
    if (channel && channel.bin && ("start" in channel.bin || "step" in channel.bin)) delete channel.bin;
  }
  return copy;
}

// --- The panel ----------------------------------------------------------------

class ChartsPanel {
  constructor(root, bootstrap) {
    this.root = root;
    this.source = bootstrap.source;
    this.urls = bootstrap.urls;
    this.bootstrap = bootstrap;
    this.queue = new Queue(bootstrap.thumbnail_concurrency);
    this.types = null; // {name: chart type}
    this.vocabulary = null; // aggregates, time units, sorts
    this.columns = [];
    this.suggestions = [];
    this.loaded = false;
    this.current = null; // {spec, title, reason, card}
    this.cards = [];
    this.requestId = 0;
    this.build();
  }

  build() {
    const stored = recall("localStorage", COLLAPSED_KEY);
    // The panel holds the suggestions. An open chart sits below it, and shows
    // whether or not the suggestions are collapsed. A link to the panel
    // itself opens it. Someone who followed a link to a chart sees that chart
    // without the suggestions above it, on that visit only: the choice they
    // made earlier is still remembered. Reloading the page, or going back to
    // it, is not following a link.
    const navigation = performance.getEntriesByType("navigation")[0];
    const followedChartLink =
      location.hash.startsWith(FRAGMENT) && (!navigation || navigation.type === "navigate");
    const open =
      location.hash === `#${this.root.id}` ||
      (!followedChartLink && (stored === null ? this.bootstrap.auto_suggest : stored !== "1"));
    // Stay open with nothing suggested only if the person asked for the panel
    this.keepOpen = location.hash === `#${this.root.id}`;

    this.status = h("span", { class: "dc-status" });
    this.strip = h("div", { class: "dc-strip" });
    this.view = h("div", { class: "dc-view", hidden: true });
    this.heading = h("span", { class: "dc-heading" }, "Suggested charts");
    const summary = h("summary", {}, this.heading, this.status);
    this.panel = h("details", { class: "dc-panel", open }, summary, this.strip);
    // Only a click or key press on the summary is the person's own choice.
    // The panel also opens and closes itself, and that is not remembered.
    let byUser = false;
    summary.addEventListener("click", () => {
      byUser = true;
    });
    this.panel.addEventListener("toggle", () => {
      if (byUser) {
        byUser = false;
        remember("localStorage", COLLAPSED_KEY, this.panel.open ? "0" : "1");
        if (this.panel.open) this.keepOpen = true;
      }
      if (this.panel.open) this.load();
    });
    this.root.replaceChildren(this.panel, this.view);

    // A link to the panel opens it. A link to
    // a chart on this same page changes only the fragment, so the page
    // does not load again: open that chart here.
    window.addEventListener("hashchange", () => {
      if (location.hash === `#${this.root.id}`) {
        this.keepOpen = true;
        this.panel.open = true;
        this.root.scrollIntoView({ block: "start" });
      } else if (location.hash.startsWith(FRAGMENT)) {
        this.openLinkedChart();
      }
    });
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => this.current && this.drawCurrent(), 200);
    });
    // A chart to show needs the chart types and columns, even with the
    // suggestions collapsed
    if (open || this.linkedSpec() || this.savedSelection()) this.load();
  }

  async load() {
    if (this.loaded) return;
    this.loaded = true;
    this.status.textContent = "Looking for charts…";
    // A plain table has its own suggestions URL. Anything else, including a
    // filtered table, is described by its source.
    const plainTable = this.source.kind === "table" && !this.source.filters;
    const [types, suggested] = await Promise.all([
      getJSON(this.urls.types),
      getJSON(this.urls.suggest, plainTable ? null : { source: JSON.stringify(this.source) }),
    ]);
    if (!types.ok || !suggested.ok) {
      this.status.textContent = `Charts are not available. ${errorMessage(types.ok ? suggested : types)}`;
      return;
    }
    this.types = Object.fromEntries(types.types.map((type) => [type.name, type]));
    this.vocabulary = types;
    this.columns = suggested.columns;
    this.suggestions = suggested.suggestions;
    // With nothing suggested the panel only offers "Build a chart"
    if (!this.suggestions.length) this.heading.textContent = "Charts";
    this.source = suggested.source;
    this.status.textContent = this.statusText(suggested.profile);
    this.status.title = (suggested.profile.notes || []).join("\n");
    this.renderStrip();

    this.openInitialChart();
    // With nothing suggested and no chart showing, the panel gets out of the
    // way. Opening it again offers "Build a chart".
    if (!this.suggestions.length && !this.current && !this.keepOpen) this.panel.open = false;
  }

  // --- Which chart is selected ---
  // A link to a chart wins. Otherwise the chart selected earlier for this
  // table comes back, so it stays selected while the person filters, facets,
  // sorts or pages: each of those loads a new page.

  selectionKey() {
    const source = this.bootstrap.source;
    if (source.kind === "table") return `${SELECTED_PREFIX}${source.database}/table/${source.table}`;
    if (source.kind === "stored_query") return `${SELECTED_PREFIX}${source.database}/query/${source.query_name}`;
    return null; // an arbitrary query can change shape between runs
  }

  filtersKey() {
    return canonical([this.source.filters || [], this.source.params || {}]);
  }

  rememberSelection(spec) {
    const key = this.selectionKey();
    if (!key) return;
    remember("sessionStorage", key, spec ? JSON.stringify({ spec, filters: this.filtersKey() }) : null);
  }

  // The chart selected earlier for this table, {spec, filters}, or null
  savedSelection() {
    const key = this.selectionKey();
    if (!key) return null;
    try {
      const saved = JSON.parse(recall("sessionStorage", key));
      return saved && saved.spec ? saved : null;
    } catch (e) {
      return null; // Nothing usable was remembered
    }
  }

  // The spec in a #chart= fragment, or null
  linkedSpec() {
    if (!location.hash.startsWith(FRAGMENT)) return null;
    try {
      return JSON.parse(decodeURIComponent(location.hash.slice(FRAGMENT.length)));
    } catch (e) {
      return null; // A fragment that is not a spec is ignored
    }
  }

  openLinkedChart() {
    const spec = this.linkedSpec();
    if (!spec) return;
    // Until load() finishes there is nothing to open: it opens the linked
    // chart itself
    this.load();
    if (!this.types) return;
    if (!this.current || canonical(spec) !== canonical(this.current.spec)) this.openInitialChart();
    else this.scrollToView();
  }

  // Someone who followed a link to a chart sees that chart at the top of
  // the window. Only this page scrolls, not a page it is framed in.
  scrollToView() {
    const top = this.view.getBoundingClientRect().top + window.scrollY - 16;
    window.scrollTo({ top: Math.max(0, top) });
  }

  openInitialChart() {
    let spec = null;
    let exact = true;
    const linked = location.hash.startsWith(FRAGMENT);
    if (linked) {
      spec = this.linkedSpec();
    } else {
      const saved = this.savedSelection();
      if (saved) {
        spec = saved.spec;
        // The rows have changed, so bins that suited the old rows are dropped
        exact = saved.filters === this.filtersKey();
        if (!exact) spec = withoutDerivedBins(spec);
      }
    }
    if (!spec || typeof spec !== "object" || !spec.encoding) return;
    // If it is one of this page's suggestions, select that card: it has a
    // reason, and bins that suit the rows now on the page
    const comparable = (candidate) => canonical(exact ? candidate : withoutDerivedBins(candidate));
    const wanted = comparable(spec);
    const index = this.suggestions.findIndex((suggestion) => comparable(suggestion.spec) === wanted);
    if (index >= 0) {
      const suggestion = this.suggestions[index];
      this.open({ ...suggestion, spec: clone(suggestion.spec), card: this.cards[index] });
    } else {
      this.open({ spec, title: "", reason: "" });
    }
    if (linked) this.scrollToView();
  }

  statusText(profile) {
    const parts = [];
    if (!this.suggestions.length) parts.push("No charts suggested for this data.");
    if (profile.basis === "sample") {
      const of = profile.row_count === null ? "" : ` of ${profile.row_count.toLocaleString()}`;
      parts.push(
        `Suggestions are based on a sample of ${profile.rows_profiled.toLocaleString()}${of} rows; charts use every row.`
      );
    }
    if (this.source.filters) {
      const rows = profile.row_count === null ? "the rows" : `the ${count(profile.row_count, "row")}`;
      const match = profile.row_count === 1 ? "matches" : "match";
      parts.push(`Charts cover ${rows} that ${match} the current filters.`);
    }
    return parts.join(" ");
  }

  // --- Suggestions strip ---

  renderStrip() {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          observer.unobserve(entry.target);
          entry.target.loadPreview();
        }
      },
      { root: this.strip, rootMargin: "0px 200px" }
    );
    const cards = this.suggestions.map((suggestion) => {
      const card = this.card(suggestion);
      // Eager previews load as cards scroll into view. On-demand ones show
      // the chart type icon until the card is hovered, focused or tapped.
      if (suggestion.preview === "eager") observer.observe(card);
      else {
        card.addEventListener("pointerenter", card.loadPreview, { once: true });
        card.addEventListener("focus", card.loadPreview, { once: true });
      }
      return card;
    });
    const build = h(
      "button",
      { type: "button", class: "dc-card dc-card-build", onclick: () => this.open(this.blank()) },
      h("span", { class: "dc-thumb dc-thumb-build", "aria-hidden": "true" }, "+"),
      h("span", { class: "dc-card-title" }, "Build a chart"),
      h("span", { class: "dc-card-reason" }, "Pick a chart type and columns yourself")
    );
    this.cards = cards;
    this.strip.replaceChildren(...cards, build);
  }

  card(suggestion) {
    const type = this.types[suggestion.spec.type];
    const thumb = h("span", { class: "dc-thumb", "aria-hidden": "true" });
    thumb.innerHTML = type ? type.icon : ""; // an SVG from the chart type's own code
    const card = h(
      "button",
      {
        type: "button",
        class: "dc-card",
        "aria-pressed": "false",
        onclick: () => this.open({ ...suggestion, spec: clone(suggestion.spec), card }),
      },
      thumb,
      h("span", { class: "dc-card-title" }, suggestion.title),
      h("span", { class: "dc-card-reason" }, suggestion.reason)
    );
    let started = false;
    card.loadPreview = () => {
      if (started) return;
      started = true;
      this.queue.add(async () => {
        const holder = h("span", { class: "dc-thumb-chart" });
        const drawn = await this.draw(holder, suggestion.spec, "thumbnail", thumb);
        // A failed or timed-out thumbnail falls back to the icon
        if (drawn.ok) thumb.replaceChildren(holder);
      });
    };
    return card;
  }

  // --- Drawing ---

  draw(el, spec, size, measureFrom) {
    return drawChart(el, { urls: this.urls, types: this.types, source: this.source, spec, size, measureFrom });
  }

  // --- Chart view ---

  blank() {
    // The first chart type whose required channels can all be filled
    const names = Object.keys(this.types);
    const type =
      names.find((name) =>
        this.types[name].channels.every(
          (channel) => !channel.required || !channel.column_required || this.fitting(channel).length
        )
      ) || names[0];
    return { spec: this.defaultSpec(type, {}), title: "", reason: "" };
  }

  open(chart) {
    for (const card of this.strip.querySelectorAll(".dc-card")) {
      card.setAttribute("aria-pressed", card === chart.card ? "true" : "false");
    }
    this.current = chart;
    this.lastResult = null;
    this.titleEl = h("h3", { class: "dc-title" }, chart.title || "Chart");
    this.reasonEl = h("p", { class: "dc-reason" }, chart.reason || "");
    this.chartEl = h("div", { class: "dc-chart", role: "img" });
    this.noteEl = h("p", { class: "dc-note", "aria-live": "polite" });
    this.linksEl = h("div", { class: "dc-links" });
    this.builderEl = h("form", { class: "dc-builder", "aria-label": "Chart settings", onsubmit: (event) => event.preventDefault() });
    this.builderEl.addEventListener("change", (event) => this.onBuilderChange(event.target));
    this.view.replaceChildren(
      h(
        "div",
        { class: "dc-view-head" },
        this.titleEl,
        h("button", { type: "button", class: "dc-close", onclick: () => this.close() }, "Close")
      ),
      this.reasonEl,
      h(
        "div",
        { class: "dc-body" },
        h("div", { class: "dc-chart-col" }, this.chartEl, this.noteEl, this.linksEl),
        this.builderEl
      )
    );
    this.view.hidden = false;
    this.renderBuilder();
    this.drawCurrent();
  }

  close() {
    this.current = null;
    this.rememberSelection(null);
    this.view.hidden = true;
    this.view.replaceChildren();
    for (const card of this.strip.querySelectorAll(".dc-card")) card.setAttribute("aria-pressed", "false");
    if (location.hash.startsWith(FRAGMENT)) {
      history.replaceState(null, "", location.pathname + location.search);
    }
  }

  async drawCurrent() {
    const chart = this.current;
    const requestId = ++this.requestId;
    // The spec lives in the URL fragment, so a chart can be linked to
    history.replaceState(null, "", FRAGMENT + encodeURIComponent(JSON.stringify(chart.spec)));
    this.rememberSelection(chart.spec);
    // While data reloads the previous chart stays, dimmed: no layout jump
    this.chartEl.classList.add("dc-loading");
    const holder = h("div", {});
    this.chartEl.append(holder);
    holder.style.cssText = "position:absolute;visibility:hidden;width:100%";
    const result = await this.draw(holder, chart.spec, "full", this.chartEl);
    if (requestId !== this.requestId || chart !== this.current) return;
    this.chartEl.classList.remove("dc-loading");
    const hadResult = !!this.lastResult;
    this.lastResult = result.ok ? result : null;
    // The annotation list describes values with the chart's own column names
    if (!hadResult && result.ok && (chart.spec.annotations || []).length) this.renderBuilder();
    if (!result.ok) {
      // The spec cannot be drawn, so the previous chart must not stay up
      this.chartEl.replaceChildren();
      this.chartEl.removeAttribute("aria-label");
      this.titleEl.textContent = "Chart";
      this.reasonEl.textContent = "";
      const errors = result.errors && result.errors.length ? result.errors : [errorMessage(result)];
      this.noteEl.replaceChildren(
        h("strong", {}, "This chart cannot be drawn yet. "),
        h("span", {}, errors.join("; ") + ".")
      );
      this.noteEl.classList.add("dc-error");
      this.linksEl.replaceChildren();
      return;
    }
    holder.style.cssText = "";
    this.chartEl.replaceChildren(holder);
    chart.title = result.title;
    this.titleEl.textContent = result.title;
    this.reasonEl.textContent = chart.reason || "";
    this.chartEl.setAttribute("aria-label", chartLabel(result, chart.reason));
    this.noteEl.classList.remove("dc-error");
    this.noteEl.textContent = truncationNote(result);
    this.renderLinks(result);
    this.noteResolved(result.meta || {});
  }

  renderLinks(result) {
    const links = [];
    if (result.sql_url) links.push(h("a", { href: result.sql_url }, "This chart's SQL"));
    links.push(
      copyButton("Copy link to this chart", "Link copied", () => location.href, "Copy the address from the address bar"),
      ...copyDataButtons(result)
    );
    this.linksEl.replaceChildren(h("div", { class: "dc-link-row" }, links));
  }

  // --- Manual builder ---
  // Generated from the channel declarations: there is no chart-specific UI.
  // It edits the same spec a suggestion produced.

  fits(channel, column) {
    const roles = channel.roles || [];
    if (roles.includes(column.role)) return true;
    if ((column.secondary_roles || []).some((role) => roles.includes(role))) return true;
    return roles.includes("temporal") && !!column.temporal_candidate;
  }

  fitting(channel) {
    return this.columns.filter((column) => this.fits(channel, column));
  }

  defaultEncoding(channel, used) {
    const fitting = this.fitting(channel);
    const column = fitting.find((c) => !used.has(c.name)) || fitting[0];
    if (!channel.column_required) return { aggregate: "count" };
    if (!channel.required) return null;
    const name = column ? column.name : this.columns.length ? this.columns[0].name : null;
    if (name === null) return null;
    used.add(name);
    const encoding = { column: name };
    if (channel.allows_limit) Object.assign(encoding, { limit: 10, other: true });
    return encoding;
  }

  defaultSpec(typeName, previous) {
    const type = this.types[typeName];
    const encoding = {};
    const used = new Set();
    for (const channel of type.channels) {
      // Keep what carries over from the previous chart type: the same
      // channel name, if its column also suits the new chart. Bins do not
      // carry over, since they change how some charts are drawn.
      const before = previous[channel.name];
      const column = before && this.columns.find((c) => c.name === before.column);
      let kept = null;
      if (column && this.fits(channel, column)) {
        kept = { column: column.name };
        if (channel.aggregates.includes(before.aggregate)) kept.aggregate = before.aggregate;
        else if (channel.aggregates.length && !channel.column_required) kept.aggregate = "sum";
        if (channel.allows_time_unit && before.timeUnit) kept.timeUnit = before.timeUnit;
        if (channel.allows_limit) Object.assign(kept, { limit: before.limit || 10, other: before.other !== false });
        used.add(column.name);
      } else if (before && before.column === undefined && !channel.column_required) {
        kept = { aggregate: "count" };
      }
      const chosen = kept || this.defaultEncoding(channel, used);
      if (chosen) encoding[channel.name] = chosen;
    }
    const options = {};
    for (const [name, option] of Object.entries(type.options || {})) options[name] = option.default;
    return { v: 1, type: typeName, encoding, options };
  }

  // Annotations carry over to a new chart type that can draw them, if
  // their values still make sense on its axes
  keptAnnotations(typeName, annotations) {
    const support = (this.types[typeName] || {}).annotations || {};
    const allowed = support.types || [];
    const axes = support.axes || {};
    const fits = (annotation, key) => {
      if (!(key in annotation)) return true;
      const axis = axes[key[0]];
      const value = annotation[key];
      if (!axis) return false;
      if (axis === "number") return typeof value === "number";
      if (axis === "time") return !!parseMoment(value);
      return true;
    };
    return (annotations || []).filter(
      (annotation) =>
        annotation &&
        allowed.includes(annotation.type) &&
        ["x", "x2", "y", "y2"].every((key) => fits(annotation, key)) &&
        (!annotation.series || support.series)
    );
  }

  // Called by the annotation editor with the chart's new annotations.
  // `focus` is the index of the one to put focus on, or "add".
  setAnnotations(annotations, focus) {
    const chart = this.current;
    if (annotations.length) chart.spec.annotations = annotations;
    else delete chart.spec.annotations;
    this.edited();
    this.renderBuilder();
    const target =
      focus === "add" || focus === undefined || focus === null
        ? this.builderEl.querySelector("[data-annotation-add]")
        : this.builderEl.querySelector(`[data-annotation-edit="${focus}"]`);
    if (target) target.focus();
    this.drawCurrent();
  }

  // An edited chart is no longer the suggestion it started as
  edited() {
    this.current.reason = "";
    this.current.card = null;
    for (const card of this.strip.querySelectorAll(".dc-card")) card.setAttribute("aria-pressed", "false");
  }

  renderBuilder() {
    const spec = this.current.spec;
    const type = this.types[spec.type];
    const focused = document.activeElement && this.builderEl.contains(document.activeElement)
      ? document.activeElement.name
      : null;
    const fields = [
      h(
        "label",
        { class: "dc-field" },
        h("span", {}, "Chart type"),
        h(
          "select",
          { name: "type" },
          Object.values(this.types).map((t) => h("option", { value: t.name, selected: t.name === spec.type }, t.label))
        )
      ),
    ];
    if (type) {
      for (const channel of type.channels) fields.push(this.channelFields(channel, spec.encoding[channel.name]));
      for (const [name, option] of Object.entries(type.options || {})) {
        const value = (spec.options || {})[name] ?? option.default;
        fields.push(
          h(
            "label",
            { class: "dc-field" },
            h("span", {}, option.label || name),
            h(
              "select",
              { name: `option.${name}` },
              option.values.map((v) => h("option", { value: v, selected: v === value }, v))
            )
          )
        );
      }
    }
    if (type) {
      const annotations = annotationSection(this);
      if (annotations) fields.push(annotations);
    }
    this.builderEl.replaceChildren(...fields);
    if (focused && this.builderEl.elements[focused]) this.builderEl.elements[focused].focus();
  }

  channelFields(channel, encoding) {
    const key = channel.name;
    const current = encoding ? encoding.column : undefined;
    const option = (column) => h("option", { value: column.name, selected: column.name === current }, column.name);
    const fitting = this.columns.filter((column) => this.fits(channel, column));
    const rest = this.columns.filter((column) => !this.fits(channel, column));
    const choices = [];
    if (!channel.column_required) {
      choices.push(h("option", { value: "", selected: current === undefined }, "Count of rows"));
    } else if (!channel.required) {
      choices.push(h("option", { value: "", selected: current === undefined }, "None"));
    }
    if (fitting.length) choices.push(h("optgroup", { label: "Suggested" }, fitting.map(option)));
    if (rest.length) choices.push(h("optgroup", { label: "Other columns" }, rest.map(option)));

    const rows = [h("label", { class: "dc-field" }, h("span", {}, channel.label), h("select", { name: `${key}.column` }, choices))];
    if (!encoding || current === undefined) return h("fieldset", { class: "dc-channel" }, rows);

    const small = (label, control) => h("label", { class: "dc-field dc-field-small" }, h("span", {}, label), control);
    // A channel that takes both categories and dates is a time axis when its
    // column is temporal: the server applies the same rule
    const column = this.columns.find((c) => c.name === current) || {};
    const temporal = !!column.temporal || !!column.temporal_candidate;
    const timeAxis =
      channel.allows_time_unit &&
      (!!encoding.timeUnit ||
        (column.role === "temporal" && !(column.secondary_roles || []).includes("category")) ||
        !channel.allows_limit);
    if (channel.aggregates.length) {
      const labels = { count: "Count", sum: "Total", avg: "Average", min: "Minimum", max: "Maximum", count_distinct: "Distinct values" };
      const value = encoding.aggregate || (channel.column_required ? "sum" : "count");
      rows.push(
        small(
          "Show",
          h(
            "select",
            { name: `${key}.aggregate` },
            channel.aggregates.map((a) => h("option", { value: a, selected: a === value }, labels[a] || a))
          )
        )
      );
    }
    if (channel.allows_time_unit && (temporal || encoding.timeUnit)) {
      rows.push(
        small(
          "Per",
          h(
            "select",
            { name: `${key}.timeUnit` },
            h("option", { value: "", selected: !encoding.timeUnit }, "Automatic"),
            this.vocabulary.time_units.map((unit) => h("option", { value: unit, selected: unit === encoding.timeUnit }, unit))
          )
        )
      );
    }
    if (channel.binnable) {
      const bin = encoding.bin || {};
      rows.push(
        small(
          "Bins",
          h("input", {
            name: `${key}.bins`,
            type: "number",
            min: 1,
            max: 200,
            placeholder: bin.count ? `${bin.count} (automatic)` : "Automatic",
            value: bin.maxbins || null,
          })
        )
      );
    }
    if (channel.allows_limit && !timeAxis) {
      rows.push(
        small("Keep top", h("input", { name: `${key}.limit`, type: "number", min: 1, max: 100, value: encoding.limit || null, placeholder: "Automatic" })),
        h(
          "label",
          { class: "dc-check" },
          h("input", { name: `${key}.other`, type: "checkbox", checked: encoding.other !== false }),
          "Group the rest as Other"
        )
      );
    }
    if (channel.allows_sort) {
      const labels = { "-value": "Largest first", value: "Smallest first", key: "A to Z", "-key": "Z to A" };
      const value = encoding.sort || "-value";
      rows.push(
        small(
          "Order",
          h(
            "select",
            { name: `${key}.sort` },
            this.vocabulary.sorts.map((s) => h("option", { value: s, selected: s === value }, labels[s] || s))
          )
        )
      );
    }
    return h("fieldset", { class: "dc-channel" }, rows);
  }

  onBuilderChange(control) {
    const chart = this.current;
    const spec = chart.spec;
    const [first, field] = control.name.split(".");
    if (!control.name) return; // not one of the builder's own controls
    if (control.name === "type") {
      chart.spec = this.defaultSpec(control.value, spec.encoding);
      const annotations = this.keptAnnotations(control.value, spec.annotations);
      if (annotations.length) chart.spec.annotations = annotations;
    } else if (first === "option") {
      spec.options = { ...(spec.options || {}), [field]: control.value };
    } else {
      const channel = this.types[spec.type].channels.find((c) => c.name === first);
      const encoding = spec.encoding[first] || {};
      if (field === "column") {
        if (control.value === "") {
          if (channel.column_required) delete spec.encoding[first];
          else spec.encoding[first] = { aggregate: "count" };
        } else {
          // Bins and the time unit belonged to the old column: start again
          const next = { ...encoding, column: control.value };
          delete next.bin;
          delete next.timeUnit;
          if (channel.aggregates.length && !channel.column_required && (!encoding.column || !next.aggregate)) {
            next.aggregate = "sum";
          }
          if (channel.allows_limit && next.limit === undefined) Object.assign(next, { limit: 10, other: true });
          spec.encoding[first] = next;
        }
      } else if (field === "bins") {
        const bins = parseInt(control.value, 10);
        if (bins > 0) encoding.bin = { maxbins: Math.min(bins, 200) };
        else delete encoding.bin;
      } else if (field === "limit") {
        const limit = parseInt(control.value, 10);
        if (limit > 0) encoding.limit = Math.min(limit, 100);
        else delete encoding.limit;
      } else if (field === "other") {
        encoding.other = control.checked;
      } else if (control.value === "") {
        delete encoding[field];
      } else {
        encoding[field] = control.value;
      }
    }
    this.edited();
    this.renderBuilder();
    this.drawCurrent();
  }

  // Show what "Automatic" resolved to
  noteResolved(meta) {
    for (const select of this.builderEl.querySelectorAll('select[name$=".timeUnit"]')) {
      if (meta.timeUnit) select.options[0].textContent = `Automatic (${meta.timeUnit})`;
    }
  }
}

for (const root of document.querySelectorAll(".datasette-charts[data-bootstrap]")) {
  try {
    // A table page says which rows it shows ("4,235 rows where country =
    // CHN") below where top_table puts the panel. The charts are of those
    // rows, so the panel goes after that line.
    const summary = document.querySelector("h3.table-summary");
    if (summary) summary.after(root);
    new ChartsPanel(root, JSON.parse(root.dataset.bootstrap));
  } catch (e) {
    console.error("datasette-charts could not start", e);
  }
}
