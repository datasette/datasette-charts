"""
The boxes a single-series line chart gives the annotation layout for the
shaded area under its line, so labels are kept out of it.

Run against line.js in node, with linear scales: x as given, and y with
zero at 100 and screen y growing downwards, as in an SVG.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

MODULE = (
    pathlib.Path(__file__).parent.parent
    / "datasette_charts"
    / "static"
    / "renderers"
    / "line.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def area_rects(values, step=30):
    "values[i] is drawn at x = i * step; None is a gap in the line"
    points = [{"date": i * step, "value": v} for i, v in enumerate(values)]
    script = (
        f"const m = await import({json.dumps(MODULE.as_uri())});\n"
        "const sx = {apply: (x) => x};\n"
        "const sy = {apply: (v) => 100 - v};\n"
        f"console.log(JSON.stringify(m.areaRects({json.dumps(points)}, sx, sy)));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def line_y(values, x, step=30):
    "Screen y of the straight line between points at x"
    i = int(x // step)
    a, b = values[i], values[min(i + 1, len(values) - 1)]
    t = (x - i * step) / step
    return 100 - (a + (b - a) * t)


def covered(rects, x, y):
    return any(
        r["left"] <= x <= r["right"] and r["top"] <= y <= r["bottom"] for r in rects
    )


def test_boxes_cover_the_whole_area():
    values = [10, 80, 75, 0, 40, 40]
    rects = area_rects(values)
    for x in range(0, 30 * (len(values) - 1)):
        x += 0.5
        top = line_y(values, x)
        for y in range(int(top) + 1, 100):
            assert covered(rects, x, y), (x, y)


def test_boxes_follow_the_slope():
    "No box reaches more than a slice's rise above the line"
    values = [0, 90]
    rects = area_rects(values)
    assert len(rects) == 4
    for r in rects:
        assert r["bottom"] == 100
        # The line at the box's right edge is its highest point
        assert r["top"] == pytest.approx(line_y(values, r["right"] - 1e-9))
    # So the space above the line on the left stays free for labels
    assert not covered(rects, 1, 20)


def test_flat_line_boxes_stop_at_the_line():
    rects = area_rects([50, 50, 50])
    assert all(r["top"] == 50 and r["bottom"] == 100 for r in rects)
    assert not covered(rects, 10, 49)


def test_gaps_and_negative_values():
    # A gap leaves no box either side of it
    rects = area_rects([20, None, 20])
    assert rects == []
    # Below zero the box hangs down from zero
    rects = area_rects([-30, -30])
    assert all(r["top"] == 100 and r["bottom"] == 130 for r in rects)


def test_narrow_gaps_get_one_box_each():
    rects = area_rects([5, 10, 15, 20], step=4)
    assert len(rects) == 3
