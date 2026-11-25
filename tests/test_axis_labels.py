"""
fitBandLabels() in renderers/common.js decides how category labels under
upright bars are written. It is a pure function, run here in Node when Node
is installed.
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
    / "common.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

YEARS = [str(year) for year in range(2006, 2027)]


def run(expression: str):
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


def fit(labels, step, ordered):
    return run(
        "m.fitBandLabels({}, {{step: {}, fontFamily: 'sans-serif', ordered: {}}})".format(
            json.dumps(labels), step, json.dumps(ordered)
        )
    )


def test_labels_that_fit_are_left_alone():
    assert fit(YEARS, 60, True) == {
        "fontSize": 12,
        "angle": 0,
        "every": 1,
        "marginBottom": 44,
        "overhang": 0,
    }


def test_turned_at_12px_when_that_is_enough():
    result = fit(YEARS, 30, True)
    assert (result["fontSize"], result["angle"], result["every"]) == (12, 40, 1)


def test_smaller_then_steeper_before_thinning():
    assert [
        (r["fontSize"], r["angle"], r["every"])
        for r in (fit(YEARS, step, True) for step in (25, 20, 15))
    ] == [(10, 40, 1), (9, 60, 1), (9, 90, 1)]


def test_only_ordered_values_are_thinned():
    years = fit(YEARS, 10, True)
    assert years["every"] == 2
    names = fit(["Solar", "Hydro", "Wind", "Gas", "Coal", "Other"], 10, False)
    assert (names["fontSize"], names["angle"], names["every"]) == (9, 90, 1)


def test_ordered_values():
    assert run("m.orderedValues([2006, 2007, 2008])") is True
    assert run("m.orderedValues(['2006', '2007', '2008'])") is True
    assert run("m.orderedValues(['2006-01', '2006-02', '2006-03'])") is True
    assert run("m.orderedValues([2008, 2006, 2007])") is False
    assert run("m.orderedValues(['Solar', 'Hydro', 'Wind'])") is False
