// datasette-charts: <datasette-chart>, one chart on its own.
//
// Other plugins put this element on their pages with the HTML from
// datasette_charts.api.embed_html(). Its config attribute holds the source,
// the spec and the endpoint URLs. The rows are fetched as whoever is looking
// at the page, so the data endpoint's permission checks apply to them.

import { copyDataButtons, drawChart, errorMessage, getJSON, h, truncationNote } from "./draw.js";

// One request for the chart types, however many charts a page shows
const typeRequests = new Map();
function loadTypes(url) {
  if (!typeRequests.has(url)) typeRequests.set(url, getJSON(url));
  return typeRequests.get(url);
}

class DatasetteChart extends HTMLElement {
  connectedCallback() {
    if (this.started) return;
    this.started = true;
    try {
      this.config = JSON.parse(this.getAttribute("config"));
    } catch (e) {
      this.textContent = "This chart could not be drawn";
      return;
    }
    this.titleEl = h("p", { class: "dc-title" });
    this.chartEl = h("div", { class: "dc-chart dc-loading", role: "img" });
    this.noteEl = h("p", { class: "dc-note", "aria-live": "polite" });
    this.linksEl = h("div", { class: "dc-links" });
    this.replaceChildren(this.titleEl, this.chartEl, this.noteEl, this.linksEl);
    this.draw();

    // Redraw when the space for the chart changes, not on every pixel
    let timer = null;
    new ResizeObserver(() => {
      const width = this.chartEl.clientWidth;
      if (!this.drawnWidth || width === this.drawnWidth) return;
      clearTimeout(timer);
      timer = setTimeout(() => this.draw(), 200);
    }).observe(this.chartEl);
  }

  async draw() {
    const { source, spec, urls } = this.config;
    const requestId = (this.requestId = (this.requestId || 0) + 1);
    const types = await loadTypes(urls.types);
    let result = types;
    // While it redraws the previous chart stays, dimmed: no layout jump
    this.chartEl.classList.add("dc-loading");
    const holder = h("div", {});
    if (types.ok) {
      this.chartEl.append(holder);
      holder.style.cssText = "position:absolute;visibility:hidden;width:100%";
      result = await drawChart(holder, {
        urls,
        types: Object.fromEntries(types.types.map((type) => [type.name, type])),
        source,
        spec,
        measureFrom: this.chartEl,
      });
    }
    if (requestId !== this.requestId) return;
    this.chartEl.classList.remove("dc-loading");
    this.drawnWidth = this.chartEl.clientWidth;
    if (!result.ok) {
      const errors = result.errors && result.errors.length ? result.errors : [errorMessage(result)];
      this.chartEl.replaceChildren();
      this.chartEl.removeAttribute("aria-label");
      this.titleEl.textContent = "";
      this.noteEl.replaceChildren(h("strong", {}, "This chart cannot be drawn. "), h("span", {}, errors.join("; ") + "."));
      this.noteEl.classList.add("dc-error");
      this.linksEl.replaceChildren();
      return;
    }
    holder.style.cssText = "";
    this.chartEl.replaceChildren(holder);
    this.chartEl.setAttribute("aria-label", result.title);
    this.titleEl.textContent = result.title;
    this.noteEl.classList.remove("dc-error");
    this.noteEl.textContent = truncationNote(result);
    const links = [];
    if (this.config.chart_url) links.push(h("a", { href: this.config.chart_url }, "Open in the chart builder"));
    if (result.sql_url) links.push(h("a", { href: result.sql_url }, "This chart's SQL"));
    links.push(...copyDataButtons(result));
    this.linksEl.replaceChildren(h("div", { class: "dc-link-row" }, links));
    this.dispatchEvent(new CustomEvent("datasette-chart-drawn", { bubbles: true }));
  }
}

if (!customElements.get("datasette-chart")) customElements.define("datasette-chart", DatasetteChart);
