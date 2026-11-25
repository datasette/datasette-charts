"""
The callout layout rules in renderers/annotations.js are pure functions, so
they are tested here by running them in Node, when Node is installed. No
JavaScript test framework is needed for that.
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
    / "annotations.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def run(expression: str):
    "Evaluates a JavaScript expression with the module imported as `m`"
    script = (
        f"const m = await import({json.dumps(MODULE.as_uri())});\n"
        f"console.log(JSON.stringify({expression}));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def items(*specs):
    return [{"pos": pos, "size": size, "cross": 30} for pos, size in specs]


def test_labels_that_fit_share_one_lane_in_order():
    placed = run(
        "m.assignLanes({}, {{start: 0, end: 600}})".format(
            json.dumps(items((100, 80), (300, 80), (500, 80)))
        )
    )
    assert [spot["lane"] for spot in placed] == [0, 0, 0]
    # Centred on their points
    assert [spot["from"] for spot in placed] == [60, 260, 460]


def test_crowded_labels_take_more_lanes_and_never_swap():
    given = items((100, 100), (130, 100), (160, 100), (400, 100))
    placed = run("m.assignLanes({}, {{start: 0, end: 600}})".format(json.dumps(given)))
    assert placed is not None
    lanes = {}
    for spot, item in zip(placed, given):
        lanes.setdefault(spot["lane"], []).append((item["pos"], spot["from"]))
    assert len(lanes) > 1
    for row in lanes.values():
        row.sort()
        froms = [start for _, start in row]
        assert froms == sorted(froms)
        # And the labels in a lane do not overlap
        assert all(b - a >= 100 for a, b in zip(froms, froms[1:]))


def test_labels_stay_inside_the_band_but_leaders_reach_their_points():
    placed = run(
        "m.assignLanes({}, {{start: 0, end: 300}})".format(
            json.dumps(items((5, 80), (295, 80)))
        )
    )
    assert placed[0]["from"] == 0
    assert placed[1]["from"] == 220
    # The leader leaves the label as near its point as it can
    assert placed[0]["leader"] == 5
    assert placed[1]["leader"] == 295


def test_too_many_labels_do_not_fit():
    crowded = items(*[(150 + i, 120) for i in range(4)])
    assert (
        run(
            "m.assignLanes({}, {{start: 0, end: 300, maxLanes: 3}})".format(
                json.dumps(crowded)
            )
        )
        is None
    )


def test_choose_callout_layout_tiers():
    common = {"start": 0, "end": 600, "plotLength": 600, "crossLimit": 200}
    spread = items((100, 80), (300, 80), (500, 80))
    assert run(
        "m.chooseCalloutLayout({})".format(
            json.dumps({**common, "inline": True, "items": spread})
        )
    ) == {"tier": "inline"}
    lanes = run(
        "m.chooseCalloutLayout({})".format(
            json.dumps({**common, "inline": False, "items": spread})
        )
    )
    assert lanes["tier"] == "lanes"
    assert lanes["thickness"] == [30]
    assert lanes["band"] == 30 + 6 + 6
    # A band taller than allowed, too many lanes, or too short a plot: a key
    assert run(
        "m.chooseCalloutLayout({})".format(
            json.dumps({**common, "inline": False, "items": spread, "crossLimit": 20})
        )
    ) == {"tier": "key"}
    crowded = items(*[(300 + i, 150) for i in range(5)])
    assert run(
        "m.chooseCalloutLayout({})".format(
            json.dumps({**common, "inline": False, "items": crowded})
        )
    ) == {"tier": "key"}
    assert run(
        "m.chooseCalloutLayout({})".format(
            json.dumps({**common, "inline": False, "items": spread, "plotLength": 100})
        )
    ) == {"tier": "key"}


def test_in_order_and_box_hits_line():
    assert (
        run(
            "m.inOrder([{pos: 1, center: 10}, {pos: 2, center: 30}, {pos: 3, center: 20}])"
        )
        is False
    )
    assert run("m.inOrder([{pos: 1, center: 10}, {pos: 2, center: 30}])") is True
    box = {"left": 10, "right": 20, "top": 10, "bottom": 20}
    assert run(
        "m.boxHitsLine({}, [{{X: 0, Y: 0}}, {{X: 30, Y: 30}}])".format(json.dumps(box))
    )
    assert not run(
        "m.boxHitsLine({}, [{{X: 0, Y: 40}}, {{X: 30, Y: 40}}])".format(json.dumps(box))
    )


def test_wrap_width_narrows_with_the_chart():
    assert run(
        "[m.calloutWrapWidth(250), m.calloutWrapWidth(600), m.calloutWrapWidth(1200)]"
    ) == [
        78,
        96,
        108,
    ]
