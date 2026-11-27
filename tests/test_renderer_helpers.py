"""
Every renderer is handed the modules the built-in renderers draw with, as
``ctx.helpers``, so that a chart type from another plugin can use the same
theme, formatting and annotation layout without knowing where they are.

Run in node: a renderer written to a temporary file stands in for one from
another plugin, and records what it was given.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).parent.parent / "datasette_charts" / "static"

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


def test_renderers_get_the_built_in_helpers(tmp_path):
    common = (STATIC / "renderers" / "common.js").as_uri()
    annotations = (STATIC / "renderers" / "annotations.js").as_uri()
    renderer = tmp_path / "other.js"
    renderer.write_text(f"""
    import * as common from {json.dumps(common)};
    import * as annotations from {json.dumps(annotations)};
    export function render(el, ctx) {{
      const {{ helpers }} = ctx;
      globalThis.seen = {{
        // The very modules a built-in renderer imports, not copies
        same: helpers.common === common && helpers.annotations === annotations,
        frozen: Object.isFrozen(helpers),
        basePlot: typeof helpers.common.basePlot,
        themeFor: typeof helpers.common.themeFor,
        fmt: typeof helpers.common.fmt,
        annotate: typeof helpers.annotations.annotate,
      }};
    }}
    """)
    drawn = draw_with(renderer)
    assert drawn == {
        "ok": True,
        "seen": {
            "same": True,
            "frozen": True,
            "basePlot": "function",
            "themeFor": "function",
            "fmt": "function",
            "annotate": "function",
        },
    }
