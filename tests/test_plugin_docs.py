"""
docs/plugins.md is for authors of chart type plugins: a tutorial, then a
reference for the Python classes, the profile and the JavaScript helpers.
These tests keep it honest.

Every public name in datasette_charts, every ChartType attribute and method,
and every JavaScript helper must have a heading of its own. Every ctx field
and theme token, and every field of the dataclasses, must be documented.

The tutorial's finished plugin is written out from the page and its own tests
are run. Every line of the tutorial's steps must be in the finished plugin,
and every line of the finished plugin in one of the steps.

The examples are checked. In a ```javascript block, a line such as

    fmt(1284.567) // => "1,284.57"

says what an expression returns. A "// =>" line with no code before it is
about the line above it, and a value that does not close its brackets
carries on over the "//" lines after it:

    basePlot(ctx)
    // => {
    //   width: 640,
    //   ...
    // }

Every block with a "// =>" line is run in Node, its other lines included, in
a scope of its own. The helpers are in scope by name, and so is `ctx`: a
full-size chart 640 pixels wide, with the light theme. Values are compared
as they would be written in JavaScript, ignoring spaces outside strings,
with dates written as new Date("...").

Results from a browser, such as text widths, are written as plain comments,
which are not checked here.
"""

import ast
import dataclasses
import inspect
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from test_renderer_helpers import CTX_FIELDS, HELPERS, THEME_TOKENS

import datasette_charts
from datasette_charts import Candidate, Channel, ChartType, CompiledQuery
from datasette_charts.profiler.model import ColumnProfile, Profile, TemporalInfo

ROOT = pathlib.Path(__file__).parent.parent
DOC = ROOT / "docs" / "plugins.md"
STATIC = ROOT / "datasette_charts" / "static"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def doc_text():
    return DOC.read_text()


def headings(text):
    "(level, text) for each heading outside code blocks"
    found = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and (match := re.match(r"^(#{1,6}) (.+)$", line)):
            found.append((len(match.group(1)), match.group(2).strip()))
    return found


def section(text, heading):
    "The text under a heading, up to the next heading at the same level or above"
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = lines.index(heading)
    end = len(lines)
    fenced = False
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(rf"^#{{1,{level}}} ", lines[i]):
            end = i
            break
    return "\n".join(lines[start + 1 : end]) + "\n"


def code_blocks(text, language):
    "(first line number, [lines]) for each fenced block in a language"
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i] == "```" + language:
            start = i + 1
            i += 1
            while lines[i] != "```":
                i += 1
            blocks.append((start + 1, lines[start:i]))
        i += 1
    return blocks


# Documented, each helper under its own heading


JS_START = "## What a renderer is handed"


def javascript_part():
    text = doc_text()
    return text[text.index(JS_START) :]


def python_part():
    text = doc_text()
    return text[: text.index(JS_START)]


def h3_named(text, name):
    "The ### headings for a name, as `name` or `name(args)`"
    return [
        heading
        for level, heading in headings(text)
        if level == 3 and re.match(rf"^`{re.escape(name)}(\(.*\))?`$", heading)
    ]


@pytest.mark.parametrize("name", HELPERS)
def test_every_helper_has_a_heading(name):
    assert len(h3_named(javascript_part(), name)) == 1, f"no ### heading for {name}"


def test_no_heading_for_a_helper_that_does_not_exist():
    documented = {
        match.group(1)
        for level, text in headings(javascript_part())
        if level == 3 and (match := re.match(r"^`(\w+)\(", text))
    }
    assert documented <= set(HELPERS)


# The Python reference


@pytest.mark.parametrize("name", datasette_charts.__all__)
def test_every_public_name_has_a_heading(name):
    # ChartType has a section of its own, the rest a ### heading
    sections = [
        text
        for level, text in headings(python_part())
        if level == 2 and f"`{name}`" in text
    ]
    found = len(h3_named(python_part(), name)) + len(sections)
    assert found == 1, f"no heading for {name}"


CHART_TYPE_MEMBERS = [name for name in vars(ChartType) if not name.startswith("_")]


@pytest.mark.parametrize("name", CHART_TYPE_MEMBERS)
def test_every_chart_type_attribute_and_method_has_a_heading(name):
    found = h3_named(section(doc_text(), "## The `ChartType` class"), name)
    assert len(found) == 1, f"no ### heading for ChartType.{name}"
    member = vars(ChartType)[name]
    if callable(member):
        # The heading shows the method's real arguments
        arguments = list(inspect.signature(member).parameters)[1:]
        assert found[0] == "`{}({})`".format(name, ", ".join(arguments))
    else:
        assert found[0] == f"`{name}`"


def table_names(text):
    "The names in the first column of every table in some text"
    names = set()
    for line in text.splitlines():
        if line.startswith("| ") and not line.startswith("|---"):
            names.update(re.findall(r"`(\w+)`", line.split("|")[1]))
    return names


@pytest.mark.parametrize("cls", [Channel, Candidate, CompiledQuery])
def test_every_dataclass_field_is_documented(cls):
    documented = table_names(section(doc_text(), f"### `{cls.__name__}`"))
    fields = {field.name for field in dataclasses.fields(cls)}
    assert fields == documented


@pytest.mark.parametrize("cls", [Profile, ColumnProfile, TemporalInfo])
def test_documented_profile_attributes_exist(cls):
    documented = table_names(section(doc_text(), f"### `{cls.__name__}`"))
    assert documented
    for name in documented:
        assert hasattr(cls, name) or name in {
            f.name for f in dataclasses.fields(cls)
        }, name


def test_documented_source_attributes_exist():
    documented = table_names(section(doc_text(), "### `Source`"))
    fields = {field.name for field in dataclasses.fields(datasette_charts.Source)}
    for name in documented:
        assert hasattr(datasette_charts.Source, name) or name in fields, name


def test_every_role_is_documented():
    from datasette_charts.profiler.model import ROLES

    assert table_names(section(doc_text(), "### Column roles")) == set(ROLES)


@pytest.mark.parametrize("field", CTX_FIELDS)
def test_every_ctx_field_is_documented(field):
    # In the first column of the table, which has `width`, `height` together
    assert re.search(
        rf"^\| [^|]*`{field}`", section(doc_text(), "### `ctx`"), re.MULTILINE
    )


@pytest.mark.parametrize("token", THEME_TOKENS)
def test_every_theme_token_is_documented(token):
    assert f"| `{token}` |" in section(doc_text(), "### `ctx.theme`")


# Links and images, on GitHub and on charts.datasette.io


def github_slug(heading):
    text = re.sub(r"<[^>]+>", "", heading).replace("`", "").lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def markdown_slug(heading):
    "What the site's build, Python-Markdown's toc extension, makes of a heading"
    text = re.sub(r"<[^>]+>", "", heading).replace("`", "")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def test_headings_link_the_same_on_github_and_the_site():
    slugs = [github_slug(text) for _, text in headings(doc_text())]
    assert len(slugs) == len(set(slugs)), "two headings with the same anchor"
    for _, text in headings(doc_text()):
        assert github_slug(text) == markdown_slug(text), text


def test_links_within_the_page_work():
    anchors = {github_slug(text) for _, text in headings(doc_text())}
    for target in re.findall(r"\]\(#([^)]+)\)", doc_text()):
        assert target in anchors, target


def test_links_to_the_repository_work():
    readme_anchors = {
        github_slug(text) for _, text in headings((ROOT / "README.md").read_text())
    }
    for target in re.findall(r"\]\((\.\./[^)#]*)(?:#([^)]+))?\)", doc_text()):
        path, anchor = target
        assert (DOC.parent / path).exists(), path
        if anchor:
            assert path == "../README.md"
            assert anchor in readme_anchors, anchor


def test_images_exist():
    images = re.findall(r'<img src="([^"]+)"', doc_text())
    assert images
    for src in images:
        assert (DOC.parent / src).exists(), src


# The examples


def balanced(text):
    depth = 0
    for char in re.sub(r'"(?:[^"\\]|\\.)*"', '""', text):
        depth += char in "[{("
        depth -= char in "]})"
    return depth == 0


def parse_examples():
    "The JavaScript to run for each checked block, and each example in it"
    programs = []
    examples = []
    for start, lines in code_blocks(doc_text(), "javascript"):
        if not any("// =>" in line for line in lines):
            continue
        statements = []  # ("code", line) or ("check", example index)
        i = 0
        while i < len(lines):
            line = lines[i]
            match = re.match(r"^(.*?)\s*// => (.*)$", line)
            if not match:
                statements.append(("code", line))
                i += 1
                continue
            code, expected = match.group(1), match.group(2)
            number = start + i
            if not code.strip():
                # About the line above
                kind, code = statements.pop()
                assert kind == "code" and code.strip(), (
                    f"line {number}: nothing to check"
                )
            while (
                not balanced(expected)
                and i + 1 < len(lines)
                and lines[i + 1].lstrip().startswith("//")
            ):
                i += 1
                expected += " " + lines[i].strip()[2:]
            statements.append(("check", len(examples)))
            examples.append(
                {
                    "line": number,
                    "code": code.strip(),
                    "expected": expected,
                    "block": "\n".join(lines),
                }
            )
            i += 1
        programs.append(statements)
    return programs, examples


PROGRAMS, EXAMPLES = parse_examples()

RUNNER = """
globalThis.getComputedStyle = () => ({ backgroundColor: "rgb(255, 255, 255)" });
const { HELPERS } = await import(%(draw)s);
const { themeFor } = await import(%(common)s);
const ctx = {
  spec: { v: 1, type: "example", encoding: {}, annotations: [] },
  rows: [],
  columns: [],
  data: [],
  meta: {},
  size: "full",
  width: 640,
  height: 340,
  theme: themeFor({ nodeType: 1 }),
  helpers: HELPERS,
};
const { %(names)s } = HELPERS;
const show = (value) => {
  if (value === undefined) return "undefined";
  if (value === null) return "null";
  if (value instanceof Date) return `new Date(${JSON.stringify(value.toISOString())})`;
  if (Array.isArray(value)) return `[${value.map(show).join(", ")}]`;
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "function") return "function";
  if (typeof value === "object") {
    const key = (name) => (/^[A-Za-z_$][\\w$]*$/.test(name) ? name : JSON.stringify(name));
    return `{ ${Object.entries(value).map(([name, item]) => `${key(name)}: ${show(item)}`).join(", ")} }`;
  }
  return String(value);
};
const results = {};
%(blocks)s
console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def results():
    blocks = []
    for statements in PROGRAMS:
        body = []
        for kind, value in statements:
            if kind == "code":
                body.append(value)
            else:
                body.append(f"results[{value}] = show(({EXAMPLES[value]['code']}));")
        blocks.append(
            "try {\n"
            + "\n".join(body)
            + "\n} catch (error) {\n"
            + "  console.error(error);\n"
            + "}"
        )
    script = RUNNER % {
        "draw": json.dumps((STATIC / "draw.js").as_uri()),
        "common": json.dumps((STATIC / "renderers" / "common.js").as_uri()),
        "names": ", ".join(HELPERS),
        "blocks": "\n".join(blocks),
    }
    run = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return {int(index): shown for index, shown in json.loads(run.stdout).items()}


def canonical(text):
    "The text without spaces, except inside strings"
    parts = re.split(r'("(?:[^"\\]|\\.)*")', text.strip())
    return "".join(
        part if part.startswith('"') else re.sub(r"\s+", "", part) for part in parts
    )


def test_there_are_examples_for_most_helpers():
    used = {
        name
        for name in HELPERS
        for example in EXAMPLES
        if re.search(rf"\b{name}\b", example["block"])
    }
    browser_only = {
        "mount",
        "textWidth",
        "fitBandLabels",
        "valueAxisMargin",
        "annotate",
    }
    assert set(HELPERS) - browser_only <= used


@needs_node
@pytest.mark.parametrize(
    "index",
    range(len(EXAMPLES)),
    ids=[f"line {e['line']}: {e['code']}" for e in EXAMPLES],
)
def test_example(results, index):
    example = EXAMPLES[index]
    assert index in results, f"line {example['line']}: {example['code']} did not run"
    assert canonical(results[index]) == canonical(example["expected"])


@needs_node
def test_renderers_on_the_page_are_valid_javascript(tmp_path):
    renderers = [
        lines
        for _, lines in code_blocks(doc_text(), "javascript")
        if any("export function render" in line for line in lines)
    ]
    assert renderers
    for i, lines in enumerate(renderers):
        module = tmp_path / f"renderer{i}.mjs"
        module.write_text("\n".join(lines))
        subprocess.run(
            ["node", "--check", str(module)], check=True, capture_output=True
        )


# The tutorial


def tutorial():
    "The tutorial's code blocks: the steps, the finished files and the tests"
    text = section(doc_text(), "## Tutorial: a lollipop chart plugin")
    steps, finished = text.split("### The finished plugin")
    python_steps = [lines for _, lines in code_blocks(steps, "python")]
    tests = [lines for lines in python_steps if lines[0] == "import json"]
    assert len(tests) == 1
    python_steps.remove(tests[0])
    init, js = [lines for _, lines in code_blocks(finished, "python")] + [
        lines for _, lines in code_blocks(finished, "javascript")
    ]
    return {
        "python_steps": python_steps,
        "javascript_steps": [lines for _, lines in code_blocks(steps, "javascript")],
        "init": init,
        "js": js,
        "tests": tests[0],
    }


def test_tutorial_python_is_valid():
    for _, lines in code_blocks(doc_text(), "python"):
        code = "\n".join(lines)
        if code.startswith(" "):
            # Methods, shown with the class's indentation
            code = "class Example:\n" + code
        ast.parse(code)


# The renderer at step 4, before step 7 replaces its last two lines
REPLACED = {"  const plot = Plot.plot(options);"}


@pytest.mark.parametrize("language", ["python", "javascript"])
def test_tutorial_steps_add_up_to_the_finished_plugin(language):
    blocks = tutorial()
    finished = blocks["init" if language == "python" else "js"]
    steps = [line for lines in blocks[f"{language}_steps"] for line in lines]
    finished_lines = {line for line in finished if line.strip()}
    step_lines = {line for line in steps if line.strip()}
    # Methods are shown on their own, with the class's indentation
    assert step_lines - REPLACED <= finished_lines, step_lines - finished_lines
    assert finished_lines <= step_lines, finished_lines - step_lines


@needs_node
def test_tutorial_renderer_is_valid_javascript(tmp_path):
    module = tmp_path / "lollipop.mjs"
    module.write_text("\n".join(tutorial()["js"]))
    subprocess.run(["node", "--check", str(module)], check=True, capture_output=True)


CONFTEST = """
from datasette.plugins import pm

import datasette_lollipop

pm.register(datasette_lollipop, name="datasette-lollipop")
"""


def test_tutorial_plugin_passes_its_own_tests(tmp_path):
    blocks = tutorial()
    package = tmp_path / "datasette_lollipop"
    (package / "static").mkdir(parents=True)
    (package / "__init__.py").write_text("\n".join(blocks["init"]) + "\n")
    (package / "static" / "lollipop.js").write_text("\n".join(blocks["js"]) + "\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lollipop.py").write_text(
        "\n".join(blocks["tests"]) + "\n"
    )
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = strict\n")
    run = subprocess.run(
        # test_plugin_is_installed needs the package to be installed
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
        + ["--deselect", "tests/test_lollipop.py::test_plugin_is_installed"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "2 passed" in run.stdout
