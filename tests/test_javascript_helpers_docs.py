"""
docs/javascript-helpers.md documents ctx.helpers for authors of renderers.
These tests keep it honest.

Every helper, every ctx field and every theme token must be documented, each
helper under a heading of its own.

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

import json
import pathlib
import re
import shutil
import subprocess

import pytest
from test_renderer_helpers import CTX_FIELDS, HELPERS, THEME_TOKENS

ROOT = pathlib.Path(__file__).parent.parent
DOC = ROOT / "docs" / "javascript-helpers.md"
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
    start = text.index(heading + "\n")
    rest = text[start + len(heading) :]
    end = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


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


@pytest.mark.parametrize("name", HELPERS)
def test_every_helper_has_a_heading(name):
    level3 = [text for level, text in headings(doc_text()) if level == 3]
    matching = [
        text for text in level3 if re.match(rf"^`{re.escape(name)}(\(.*\))?`$", text)
    ]
    assert len(matching) == 1, f"no ### heading for {name}"


def test_no_heading_for_a_helper_that_does_not_exist():
    documented = {
        match.group(1)
        for level, text in headings(doc_text())
        if level == 3 and (match := re.match(r"^`(\w+)\(", text))
    }
    assert documented <= set(HELPERS)


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
    assert len(renderers) >= 2
    for i, lines in enumerate(renderers):
        module = tmp_path / f"renderer{i}.mjs"
        module.write_text("\n".join(lines))
        subprocess.run(
            ["node", "--check", str(module)], check=True, capture_output=True
        )
