# /// script
# requires-python = ">=3.11"
# dependencies = ["jinja2", "markdown", "pyyaml"]
# ///
"""
Build the Datasette Charts website into _site/.

    uv run site/build.py            # build the site
    uv run site/build.py shots      # write site/shots.yml for shot-scraper multi

The documentation page is the README, and docs/plugins.md is a page under
it. The gallery comes from site/gallery.yml and the changelog is
fetched from GitHub Releases each time the site is built.
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

import jinja2
import markdown
import yaml

SITE = pathlib.Path(__file__).parent
ROOT = SITE.parent
DOCS = ROOT / "docs"
OUT = ROOT / "_site"
REPO = "datasette/datasette-charts"
GITHUB = f"https://github.com/{REPO}"
LITE = "https://lite.datasette.io/?install=datasette-charts"
# The port shot-scraper runs Datasette on while it takes the screenshots
SHOTS_PORT = 8571

# From the README's chart types table, in the order the gallery uses
TYPE_LABELS = {
    "line": "Line chart",
    "bar": "Bar chart",
    "histogram": "Histogram",
    "scatter": "Scatter plot",
    "map": "Map",
    "stacked_bar": "Stacked bar chart",
    "calendar": "Calendar heatmap",
    "box": "Box plot",
    "intervals": "Active over time",
}


# Gallery


def chart_path(chart):
    "The path and query string of the page a chart is on, the same locally and in Datasette Lite"
    database = urllib.parse.quote(chart["database"])
    if "sql" in chart:
        return f"/{database}/-/query?" + urllib.parse.urlencode({"sql": chart["sql"]})
    path = f"/{database}/{urllib.parse.quote(chart['table'])}"
    if chart.get("filters"):
        path += "?" + urllib.parse.urlencode([tuple(pair) for pair in chart["filters"]])
    return path


def chart_fragment(chart):
    spec = json.dumps(chart["spec"], separators=(",", ":"))
    return "chart=" + urllib.parse.quote(spec, safe="")


def lite_url(chart, databases):
    "A link that opens the chart in Datasette Lite, which keeps the page's path in the fragment"
    url = LITE
    extra = databases[chart["database"]].get("lite")
    if extra:
        url += "&" + extra
    return f"{url}#{chart_path(chart)}%23{chart_fragment(chart)}"


def load_gallery():
    gallery = yaml.safe_load((SITE / "gallery.yml").read_text())
    databases = gallery["databases"]
    seen = set()
    for section in gallery["sections"]:
        # A section is for one chart type, or has an id of its own
        section.setdefault("id", section.get("type"))
        for chart in section["charts"]:
            if chart["id"] in seen:
                sys.exit(f"gallery.yml: duplicate id {chart['id']}")
            seen.add(chart["id"])
            if "type" in section and chart["spec"]["type"] != section["type"]:
                sys.exit(f"gallery.yml: {chart['id']} is not a {section['type']} chart")
            chart["lite_url"] = lite_url(chart, databases)
            chart["spec_json"] = json.dumps(chart["spec"], indent=2)
            chart["image"] = f"screenshots/{chart['id']}.webp"
            chart["size"] = webp_size(SITE / chart["image"])
            if "sql" in chart:
                chart["source"] = "SQL query"
            elif chart.get("filters"):
                chart["source"] = "Filtered table"
            else:
                chart["source"] = "Table"
    return gallery


def webp_size(path):
    "The width and height of a WebP file, or None if it has not been made yet"
    if not path.exists():
        return None
    data = path.read_bytes()[:30]
    kind = data[12:16]
    if kind == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
    elif kind == b"VP8L":
        bits = struct.unpack("<I", data[21:25])[0]
        width, height = 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    else:
        width, height = (n & 0x3FFF for n in struct.unpack("<HH", data[26:30]))
    return {"width": width, "height": height}


# Screenshots

# Shared by both scripts. A chart is settled once it has been drawn, at full
# opacity, for a second and a half: charts are drawn again, and fade in, when
# the space around them changes.
SETTLE_JS = """
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (test) => { for (let i = 0; i < 150 && !test(); i++) await pause(100); };
  const drawn = () => {
    const chart = document.querySelector(".dc-view:not([hidden]) .dc-chart:not(.dc-loading)");
    return chart && chart.querySelector("svg") && getComputedStyle(chart).opacity === "1";
  };
  const settled = async () => {
    for (let i = 0, steady = 0; i < 300 && steady < 15; i++) {
      steady = drawn() ? steady + 1 : 0;
      await pause(100);
    }
  };
"""

# Waits for the chart, hides everything around it and waits for it to be
# drawn again at its new width
CHART_ONLY_JS = (
    "new Promise(async (done) => {"
    + SETTLE_JS
    + """
  await until(drawn);
  const style = document.createElement("style");
  style.textContent = ".dc-builder, .dc-close, .dc-links { display: none !important }" +
    " .dc-view { border: 0 !important; margin: 0 !important; background: #fff !important }";
  document.head.append(style);
  window.dispatchEvent(new Event("resize"));
  await pause(500);
  await settled();
  done();
})
""".rstrip()
)

# The whole panel, with a suggestion opened by clicking its card
PANEL_JS = (
    "new Promise(async (done) => {"
    + SETTLE_JS
    + """
  await until(() => document.querySelectorAll(".dc-card").length > 3);
  document.querySelectorAll(".dc-card")[3].click();
  await pause(500);
  await settled();
  done();
})
""".rstrip()
)


def shots_config(gallery):
    files = [db["file"] for db in gallery["databases"].values()]
    local = f"http://localhost:{SHOTS_PORT}"
    shots = [
        # Datasette itself, not "uv run datasette": shot-scraper stops the
        # process it started, and uv would leave Datasette running behind it
        {"server": [".venv/bin/datasette", *files, "-p", SHOTS_PORT]},
        {
            "url": f"{local}/content/stats",
            "output": "site/screenshots/panel.webp",
            "width": 1180,
            # Taller than the panel. Playwright resizes the window to fit a
            # taller element, and the chart is caught fading in as it redraws
            "height": 1300,
            "selector": ".datasette-charts",
            "padding": 12,
            "quality": 85,
            "javascript": PANEL_JS,
        },
    ]
    for section in gallery["sections"]:
        for chart in section["charts"]:
            shots.append(
                {
                    "url": f"{local}{chart_path(chart)}#{chart_fragment(chart)}",
                    "output": f"site/{chart['image']}",
                    "width": 760,
                    "height": 1300,
                    "selector": ".dc-view",
                    "quality": 85,
                    "javascript": CHART_ONLY_JS,
                }
            )
    return shots


# Documentation


def site_links(html, depth):
    """
    Links written to work on GitHub, rewritten for the site. A page `depth`
    folders below the documentation page links to docs/name.md as name/, to
    the README as the documentation page, and to other files in the
    repository on GitHub.
    """
    up = "../" * depth

    def rewrite(match):
        attr, target = match.group(1), match.group(2)
        if re.match(r"^[a-z]+:|^#|^images/", target):
            return match.group(0)
        path, _, anchor = target.partition("#")
        anchor = f"#{anchor}" if anchor else ""
        # From the README in the root of the repository, or from docs/
        resolved = os.path.normpath(path if depth == 0 else os.path.join("docs", path))
        if resolved == "README.md":
            href = (up or "./") + anchor
        elif resolved.startswith("docs/") and resolved.endswith(".md"):
            href = f"{up}{resolved.removeprefix('docs/').removesuffix('.md')}/{anchor}"
        else:
            kind = "tree" if (ROOT / resolved).is_dir() else "blob"
            href = f"{GITHUB}/{kind}/main/{resolved}{anchor}"
        return f'{attr}="{href}"'

    html = re.sub(r'(href)="([^"]+)"', rewrite, html)
    # Images in docs/images/ are copied to the site's docs/images/
    return html.replace('src="images/', f'src="{up}images/')


def docs_page(name):
    """
    A page from docs/: its title, its first paragraph, its HTML and a table
    of contents. Its own list of contents is left out, since the page has
    one beside it.
    """
    text = (DOCS / f"{name}.md").read_text()
    title, body = text.split("\n", 1)
    body = re.sub(r"\n## Contents\n.*?(?=\n## )", "\n", body, flags=re.S)
    summary = next(
        part for part in body.strip().split("\n\n") if not part.startswith("#")
    )
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "sane_lists"],
        extension_configs={
            "toc": {
                "permalink": "#",
                "permalink_title": "Link to this section",
                "toc_depth": "2-3",
            }
        },
    )
    content = md.convert(body)
    content = content.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>"
    )
    return {
        "name": name,
        "title": title.removeprefix("# ").strip(),
        "summary": re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", summary).replace("`", ""),
        "html": site_links(content, 1),
        "toc": md.toc_tokens,
    }


def docs_from_readme():
    "The README without its title, badges and screenshot, as HTML and a table of contents"
    text = (ROOT / "README.md").read_text()
    intro, rest = text.split("\n## ", 1)
    summary = [
        line
        for line in intro.splitlines()
        if line and not line.startswith(("#", "[!["))
    ]
    sections = [
        s
        for s in ("## " + rest).split("\n## ")
        if not s.removeprefix("## ").startswith("Screenshot")
    ]
    body = "\n".join(summary) + "\n\n" + "\n## ".join(sections)
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "sane_lists"],
        extension_configs={"toc": {"permalink": False, "toc_depth": "2-3"}},
    )
    content = md.convert(body)
    # Wide tables scroll on their own instead of widening the page
    content = content.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>"
    )
    return {
        "summary": " ".join(summary),
        "html": site_links(content, 0),
        "toc": md.toc_tokens,
    }


# Changelog


def fetch_releases():
    "Every GitHub release, newest first. The build fails if GitHub cannot be reached."
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases?per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "datasette-charts-site",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            releases = json.load(response)
    except (urllib.error.URLError, TimeoutError) as error:
        # Stop, so that a deploy never replaces the changelog with an empty page
        sys.exit(
            f"Could not fetch releases from GitHub: {error}\nUse --offline to build without the changelog."
        )
    releases = [r for r in releases if not r["draft"]]
    releases.sort(key=lambda r: r["published_at"], reverse=True)
    for release in releases:
        body = (release["body"] or "").replace("\r\n", "\n")
        # #4 is an issue or pull request, as it is on GitHub
        body = re.sub(r"(?<![\w&/\[])#(\d+)\b", rf"[#\1]({GITHUB}/issues/\1)", body)
        release["html"] = markdown.markdown(
            body, extensions=["fenced_code", "tables", "sane_lists"]
        )
        published = datetime.datetime.fromisoformat(
            release["published_at"].replace("Z", "+00:00")
        )
        release["date"] = ordinal_date(published)
        release["date_iso"] = published.date().isoformat()
    return releases


def ordinal_date(date):
    day = date.day
    suffix = (
        "th"
        if 11 <= day % 100 <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    )
    return f"{day}{suffix} {date:%B %Y}"


# Build


def build(offline=False):
    gallery = load_gallery()
    missing = [
        chart["image"]
        for section in gallery["sections"]
        for chart in section["charts"]
        if not chart["size"]
    ]
    if missing:
        print(
            "Missing screenshots (see site/README.md): " + ", ".join(missing),
            file=sys.stderr,
        )
    docs = docs_from_readme()
    plugins = docs_page("plugins")
    releases = None if offline else fetch_releases()

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(SITE / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    context = {
        "github": GITHUB,
        "lite": LITE + "#/content/stats",
        "gallery": gallery,
        "type_labels": TYPE_LABELS,
        "docs": docs,
        "plugins": plugins,
        "releases": releases,
        "panel_size": webp_size(SITE / "screenshots" / "panel.webp"),
        # The chart open in the home page's screenshot is the gallery's first
        "panel_demo": gallery["sections"][0]["charts"][0]["lite_url"],
    }

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    shutil.copytree(SITE / "static", OUT / "static")
    shutil.copytree(SITE / "screenshots", OUT / "screenshots")
    shutil.copytree(DOCS / "images", OUT / "docs" / "images")
    # Nothing here is Jekyll, and GitHub Pages should not treat it as such
    (OUT / ".nojekyll").write_text("")

    pages = {
        "index.html": "index.html",
        "gallery/index.html": "gallery.html",
        "docs/index.html": "docs.html",
        "docs/plugins/index.html": "plugins.html",
        "changelog/index.html": "changelog.html",
    }
    for path, template in pages.items():
        target = OUT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Relative links, so the site works at a domain root or under /datasette-charts/
        root = "../" * path.count("/")
        target.write_text(
            env.get_template(template).render(root=root, page=template, **context)
        )
        print(f"Wrote {target.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "command", nargs="?", default="build", choices=["build", "shots"]
    )
    parser.add_argument(
        "--offline", action="store_true", help="Do not fetch the changelog from GitHub"
    )
    args = parser.parse_args()
    if args.command == "shots":
        target = SITE / "shots.yml"
        header = "# Written by: uv run site/build.py shots\n# Do not edit this file. Edit site/gallery.yml instead.\n"
        target.write_text(
            header
            + yaml.safe_dump(shots_config(load_gallery()), sort_keys=False, width=1000)
        )
        print(f"Wrote {target.relative_to(ROOT)}")
    else:
        build(offline=args.offline)


if __name__ == "__main__":
    main()
