"""
Every renderer is handed ``ctx.helpers``: the public part of what the
built-in renderers draw with, so that a chart type from another plugin can
look like them and draw annotations without knowing where their files are.

These tests pin that interface. A helper can be added (add it to HELPERS
here, and document it in docs/plugins.md, which
test_plugin_docs.py checks); renaming or removing one breaks
other plugins.

Run in node: a renderer written to a temporary file stands in for one from
another plugin, and records what it was given.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).parent.parent
STATIC = ROOT / "datasette_charts" / "static"

# Each helper, and the module the built-in renderers import it from
HELPERS = {
    "TIP": "common",
    "basePlot": "common",
    "mount": "common",
    "isThumbnail": "common",
    "xLabel": "common",
    "valueAxisMargin": "common",
    "fmt": "common",
    "fmtCompact": "common",
    "truncate": "common",
    "categoryName": "common",
    "uniqueNames": "common",
    "seriesColor": "common",
    "textWidth": "common",
    "fitBandLabels": "common",
    "orderedValues": "common",
    "bucketToDate": "common",
    "stepDate": "common",
    "fmtBucket": "common",
    "allBuckets": "common",
    "annotate": "annotations",
    "bandMatcher": "annotations",
    "parseMoment": "annotations",
}
# The ctx fields a renderer can rely on
CTX_FIELDS = [
    "spec",
    "rows",
    "columns",
    "data",
    "meta",
    "size",
    "width",
    "height",
    "Plot",
    "d3",
    "theme",
    "helpers",
]
THEME_TOKENS = [
    "mode",
    "surface",
    "ink",
    "ink2",
    "muted",
    "grid",
    "series",
    "other",
    "sequential",
    "land",
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def draw_with(renderer_path):
    "Draws a chart whose type's renderer is the module at renderer_path"
    script = f"""
    // Just enough of a browser for renderChart
    globalThis.window = {{ Plot: {{}}, d3: {{}} }};
    globalThis.getComputedStyle = () => ({{ backgroundColor: "" }});
    const draw = await import({json.dumps((STATIC / "draw.js").as_uri())});
    const el = {{ clientWidth: 600, append() {{}} }};
    const result = await draw.renderChart(el, {{
      urls: {{}},
      types: {{ other: {{ js_url: {json.dumps(renderer_path.as_uri())} }} }},
      spec: {{ v: 1, type: "other", encoding: {{}} }},
      result: {{ rows: [], columns: [] }},
    }});
    console.log(JSON.stringify({{ ok: result.ok !== false, seen: globalThis.seen }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def seen(tmp_path_factory):
    "What a renderer from another plugin is handed"
    common = (STATIC / "renderers" / "common.js").as_uri()
    annotations = (STATIC / "renderers" / "annotations.js").as_uri()
    renderer = tmp_path_factory.mktemp("other") / "other.js"
    renderer.write_text(f"""
    import * as common from {json.dumps(common)};
    import * as annotations from {json.dumps(annotations)};
    const modules = {{ common, annotations }};
    export function render(el, ctx) {{
      const {{ helpers }} = ctx;
      globalThis.seen = {{
        ctx: Object.keys(ctx),
        frozen: Object.isFrozen(helpers),
        helpers: Object.fromEntries(
          Object.keys(helpers).map((name) => [name, typeof helpers[name]])
        ),
        // The very functions the built-in renderers import, not copies
        same: Object.fromEntries(
          Object.entries({json.dumps(HELPERS)}).map(([name, module]) => [
            name,
            typeof helpers[name] === "function"
              ? helpers[name] === modules[module][name]
              : JSON.stringify(helpers[name]) === JSON.stringify(modules[module][name]),
          ])
        ),
        tipFrozen: Object.isFrozen(helpers.TIP),
        theme: Object.keys(ctx.theme),
      }};
    }}
    """)
    drawn = draw_with(renderer)
    assert drawn["ok"]
    return drawn["seen"]


def test_helpers_are_exactly_the_public_set(seen):
    assert sorted(seen["helpers"]) == sorted(HELPERS)
    expected = {name: "function" for name in HELPERS}
    expected["TIP"] = "object"
    assert seen["helpers"] == expected


def test_helpers_are_the_built_in_renderers_own(seen):
    assert seen["same"] == {name: True for name in HELPERS}


def test_helpers_cannot_be_changed_by_a_renderer(seen):
    assert seen["frozen"]
    assert seen["tipFrozen"]


def test_ctx_fields_and_theme_tokens(seen):
    assert set(CTX_FIELDS) <= set(seen["ctx"])
    assert sorted(seen["theme"]) == sorted(THEME_TOKENS)
