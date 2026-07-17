"""P7 — A fetch pin is an identity, not a comment. See PRINCIPLES.md.

Uses file:// URLs (urllib handles them; same trick as test_tofu.py's e2e).
The TOFU test drives a pipeline *script* via runpy because the TOFU pass
patches the FetchJob's call-site file — which must never be this test file.
"""

from __future__ import annotations

import os
import re
import runpy

import pytest

from principles_helpers import fetch_job, new_graph, principle, requires_core, run_graph

_WRONG_PIN = "ab" * 32  # syntactically valid blake3 hex that matches nothing


def _src(tmp_path, name, payload: bytes):
    f = tmp_path / name
    f.write_bytes(payload)
    return f


def _real_digest(path) -> str:
    from ppg3 import _core

    return _core.blake3_file(str(path))


@requires_core
@principle("P7.1")
def test_changing_the_pin_invalidates(tmp_path):
    src = _src(tmp_path, "data.bin", b"payload one")
    url = src.resolve().as_uri()

    g1 = new_graph(tmp_path)
    fetch_job("in/data.bin", url=url, blake3=_real_digest(src))
    r1 = run_graph(g1)
    assert r1.failed == {}
    assert len(r1.built) == 1

    # A different pin is a different identity: the old entry no longer
    # satisfies this job. It must refetch and then fail verification —
    # never silently hit the stale entry.
    g2 = new_graph(tmp_path)
    fetch_job("in/data.bin", url=url, blake3=_WRONG_PIN)
    with pytest.raises(Exception) as ei:
        run_graph(g2)
    assert _WRONG_PIN in str(ei.value)


@requires_core
@principle("P7.2")
def test_removing_the_pin_refetches_and_repins(tmp_path, capsys):
    src = _src(tmp_path, "data.bin", b"first content")
    url = src.resolve().as_uri()
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    script = tmp_path / "pipeline.py"
    script_text = (
        "import ppg3\n"
        "from ppg3.tools import PyEnv\n"
        f"g = ppg3.new(stores=[ppg3.Store('main', {str(store_dir)!r})], "
        f"default_python=PyEnv.current(), project_dir={str(tmp_path / '.ppg3')!r}, "
        "frozen=False)\n"
        f"job = ppg3.FetchJob(view='in/data.bin', url={url!r})\n"
        "r = ppg3.run(g, project_id='p7')\n"
    )
    script.write_text(script_text)

    ns1 = runpy.run_path(str(script), run_name="__main__")
    capsys.readouterr()
    assert ns1["r"].failed == {}
    assert (tmp_path / "outputs" / "in" / "data.bin").read_bytes() == b"first content"

    # The user deletes the pin (back to blake3=None) *and* the upstream
    # content changed. An unpinned fetch means "trust the next fetch", so
    # this must fetch the new bytes and re-pin them — a silent hit on the
    # previously pinned entry resurrects data the user explicitly unpinned.
    script.write_text(script_text)  # undo the TOFU patch: blake3=None again
    src.write_bytes(b"second content")
    ns2 = runpy.run_path(str(script), run_name="__main__")
    capsys.readouterr()
    assert ns2["r"].failed == {}
    assert (tmp_path / "outputs" / "in" / "data.bin").read_bytes() == b"second content"


@requires_core
@principle("P7.3")
def test_changing_the_url_refetches_even_with_matching_content(tmp_path):
    payload = b"identical payload"
    src_a = _src(tmp_path, "a.bin", payload)
    src_b = _src(tmp_path, "b.bin", payload)
    pin = _real_digest(src_a)

    g1 = new_graph(tmp_path)
    fetch_job("in/data.bin", url=src_a.resolve().as_uri(), blake3=pin)
    r1 = run_graph(g1)
    assert r1.failed == {}
    assert len(r1.built) == 1

    # New URL: refetch. Most of the time a changed URL with an unchanged
    # pin means the user forgot to update the pin — downloading again and
    # verifying is the cheap, safe answer.
    g2 = new_graph(tmp_path)
    fetch_job("in/data.bin", url=src_b.resolve().as_uri(), blake3=pin)
    r2 = run_graph(g2)
    assert r2.failed == {}
    assert len(r2.built) == 1, "URL change did not refetch"


@requires_core
@principle("P7.4", "P9.3")
def test_verification_failure_names_url_hashes_and_rejected_bytes(tmp_path):
    payload = b"the actual bytes"
    src = _src(tmp_path, "data.bin", payload)
    url = src.resolve().as_uri()

    g = new_graph(tmp_path)
    fetch_job("in/data.bin", url=url, blake3=_WRONG_PIN)
    with pytest.raises(Exception) as ei:
        run_graph(g)
    msg = str(ei.value)

    assert url in msg, "the failing URL is the first thing the user needs"
    assert _WRONG_PIN in msg, "expected hash missing from the report"
    assert _real_digest(src) in msg, "actual hash missing from the report"

    # The rejected download must be kept and its path reported — comparing
    # what arrived against what was expected is the user's next action.
    retained = [
        tok.rstrip(".,:;)")
        for tok in re.findall(r"/\S+", msg)
        if os.path.isfile(tok.rstrip(".,:;)"))
        and open(tok.rstrip(".,:;)"), "rb").read() == payload
    ]
    assert retained, (
        f"the report references no retained copy of the rejected download "
        f"(got: {msg!r})"
    )
