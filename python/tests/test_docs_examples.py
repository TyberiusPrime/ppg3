"""Executable documentation: every ``ppg3-example``-marked ```python block
in ``docs/content/docs/api/*.md`` is extracted verbatim and run as a
standalone script in a scratch directory, so the API-by-example pages
cannot drift from the implementation.

Marker grammar, in the markdown source, immediately before a fence::

    <!-- ppg3-example: some-name requires=core -->
    ```python
    ...a complete script...
    ```

``requires=`` is a comma-separated capability list (``core``: the compiled
``ppg3._core`` extension; ``nix``: a working ``nix`` binary) turned into
skips, mirroring the rest of the suite. The reserved name ``fragment``
marks a block as an intentionally non-runnable excerpt (a signature sketch,
a Nix-pinning illustration).

``test_every_python_block_is_marked`` closes the loop: an unmarked
```python fence in those pages is a test failure, so a newly documented
example is *forced* to either run under this harness or be explicitly
declared a fragment.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HAVE_CORE, PYTHON_DIR

DOCS_API_DIR = PYTHON_DIR.parent / "docs" / "content" / "docs" / "api"

_MARKER_RE = re.compile(
    r"<!--\s*ppg3-example:\s*(?P<spec>[^>]+?)\s*-->\s*\n```python\n"
    r"(?P<code>.*?)\n```",
    re.DOTALL,
)
_FENCE_RE = re.compile(r"^```python$", re.MULTILINE)

_KNOWN_REQUIRES = {"core", "nix"}


class Example:
    def __init__(self, page: str, name: str, requires: frozenset, code: str):
        self.page = page
        self.name = name
        self.requires = requires
        self.code = code


def _parse_spec(spec: str, page: str):
    parts = spec.split()
    name = parts[0]
    requires = set()
    for extra in parts[1:]:
        key, _, value = extra.partition("=")
        if key != "requires" or not value:
            raise ValueError(f"{page}: bad ppg3-example attribute {extra!r}")
        requires.update(value.split(","))
    unknown = requires - _KNOWN_REQUIRES
    if unknown:
        raise ValueError(
            f"{page}: unknown requires= capabilities {sorted(unknown)}; "
            f"known: {sorted(_KNOWN_REQUIRES)}"
        )
    return name, frozenset(requires)


def _collect():
    examples = []
    seen = {}
    for md in sorted(DOCS_API_DIR.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        for m in _MARKER_RE.finditer(text):
            name, requires = _parse_spec(m.group("spec"), md.name)
            if name == "fragment":
                continue
            if name in seen:
                raise ValueError(
                    f"duplicate ppg3-example name {name!r} "
                    f"({seen[name]} and {md.name})"
                )
            seen[name] = md.name
            examples.append(Example(md.name, name, requires, m.group("code")))
    return examples


EXAMPLES = _collect() if DOCS_API_DIR.is_dir() else []

requires_docs = pytest.mark.skipif(
    not DOCS_API_DIR.is_dir(), reason="docs/content/docs/api not present"
)


def _skip_unmet(requires: frozenset) -> None:
    if "core" in requires and not HAVE_CORE:
        pytest.skip("ppg3._core extension not built")
    if "nix" in requires and shutil.which("nix") is None:
        pytest.skip("nix not on PATH")


@requires_docs
@pytest.mark.parametrize(
    "example", EXAMPLES, ids=[e.name for e in EXAMPLES]
)
def test_docs_example_runs(example, tmp_path):
    _skip_unmet(example.requires)
    script = tmp_path / "example.py"
    script.write_text(example.code, encoding="utf-8")
    env = dict(os.environ)
    # Make the checkout importable even when ppg3 isn't installed into the
    # interpreter (no-core examples); sandboxed shims use `python -I` and
    # need a real install, but those examples are gated requires=core.
    env["PYTHONPATH"] = str(PYTHON_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"docs example {example.name!r} ({example.page}) exited "
        f"{proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )


@requires_docs
def test_every_python_block_is_marked():
    """Every ```python fence in the API pages must carry a ppg3-example
    marker (a runnable example or an explicit `fragment`) — new snippets
    can't silently opt out of the harness."""
    problems = []
    for md in sorted(DOCS_API_DIR.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        marked_starts = {
            m.start("code") for m in _MARKER_RE.finditer(text)
        }
        for fence in _FENCE_RE.finditer(text):
            code_start = fence.end() + 1
            if code_start not in marked_starts:
                line = text.count("\n", 0, fence.start()) + 1
                problems.append(f"{md.name}:{line}")
    assert not problems, (
        "```python blocks without a `<!-- ppg3-example: ... -->` marker "
        f"(add one, or mark as fragment): {problems}"
    )


@requires_docs
def test_examples_were_collected():
    """Guard against a silent regex/layout regression collecting nothing."""
    assert len(EXAMPLES) >= 20
    core_gated = [e for e in EXAMPLES if "core" in e.requires]
    plain = [e for e in EXAMPLES if not e.requires]
    assert core_gated and plain
