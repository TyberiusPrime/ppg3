"""Meta-tests: keep PRINCIPLES.md, invariants.json, and the test files in
sync. These are always plain tests — the schema itself is never xfail."""

from __future__ import annotations

import re

from principles_helpers import HERE, INVARIANTS, MANIFEST, REPO_ROOT, VALID_STATUSES

PRINCIPLES_MD = REPO_ROOT / "PRINCIPLES.md"

# `@principle("P1.1")` / `@principle("P5.2", "P9.3")` occurrences.
_PRINCIPLE_CALL = re.compile(r"@principle\(([^)]*)\)")
_ID = re.compile(r"\"(P\d+\.\d+)\"")


def _ids_used_in_tests():
    used = set()
    for path in sorted(HERE.glob("test_p*.py")):
        for call in _PRINCIPLE_CALL.finditer(path.read_text()):
            used.update(_ID.findall(call.group(1)))
    return used


def test_statuses_are_valid():
    for iid, inv in INVARIANTS.items():
        assert inv["status"] in VALID_STATUSES, (
            f"{iid}: invalid status {inv['status']!r}"
        )


def test_every_invariant_has_at_least_one_test():
    used = _ids_used_in_tests()
    missing = sorted(set(INVARIANTS) - used)
    assert not missing, (
        f"invariants with no test: {missing} — every id in invariants.json "
        "needs at least one @principle(...) test"
    )


def test_every_test_id_exists_in_manifest():
    # The decorator also raises at import time; this catches ids in files
    # that fail to import for other reasons.
    unknown = sorted(_ids_used_in_tests() - set(INVARIANTS))
    assert not unknown, f"tests reference unknown invariant ids: {unknown}"


def test_every_invariant_appears_in_principles_md():
    text = PRINCIPLES_MD.read_text()
    missing = sorted(iid for iid in INVARIANTS if f"**{iid}**" not in text)
    assert not missing, (
        f"invariant ids missing from PRINCIPLES.md: {missing} — the manifest "
        "and the constitution must list the same invariants"
    )


def test_principles_md_ids_all_in_manifest():
    text = PRINCIPLES_MD.read_text()
    documented = set(re.findall(r"\*\*(P\d+\.\d+)\*\*", text))
    unknown = sorted(documented - set(INVARIANTS))
    assert not unknown, (
        f"PRINCIPLES.md documents ids missing from invariants.json: {unknown}"
    )


def test_manifest_principles_are_contiguously_numbered():
    ids = [p["id"] for p in MANIFEST["principles"]]
    assert ids == [f"P{i}" for i in range(1, len(ids) + 1)]
