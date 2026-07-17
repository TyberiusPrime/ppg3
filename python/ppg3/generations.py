"""Generation pointer-state verification (PRINCIPLES.md P3.4).

A generation has exactly one authoritative form: its ``meta.json`` record.
The symlink tree next to it is *derived* — convenient to browse, but never
a second source of truth. This module makes disagreement between the two
detectable instead of silent: :func:`verify_generation` cross-checks record
against tree in both directions and reports every mismatch as a
human-readable string (empty list = consistent).
"""

from __future__ import annotations

import json
import os
from typing import List, Optional, Union


def _generation_dir(project_dir: str, n: Optional[int]) -> str:
    views = os.path.join(project_dir, "views")
    if n is not None:
        return os.path.join(views, str(n))
    current = os.path.join(views, "current")
    if os.path.lexists(current):
        return os.path.realpath(current)
    raise FileNotFoundError(
        f"no generation to verify: {current} does not exist"
    )


def verify_generation(
    project_dir: Union[str, "os.PathLike"] = ".ppg3",
    n: Optional[int] = None,
) -> List[str]:
    """Cross-check generation ``n`` (default: current) of ``project_dir``:
    every ``meta.json`` entry must have a symlink at its ``view_rel_path``
    resolving into ``entries/<oh>/data/<path_within_entry>`` of some store,
    and the tree must contain nothing the record doesn't claim. Returns a
    list of mismatch descriptions; ``[]`` means record and tree agree."""
    gen_dir = _generation_dir(str(project_dir), n)
    meta_path = os.path.join(gen_dir, "meta.json")
    problems: List[str] = []
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as e:
        return [f"cannot read generation record {meta_path}: {e}"]

    claimed = set()
    for entry in meta.get("entries", []):
        rel = entry.get("view_rel_path", "")
        oh = entry.get("oh", "")
        within = entry.get("path_within_entry", "")
        claimed.add(rel)
        link = os.path.join(gen_dir, rel)
        if not os.path.islink(link):
            problems.append(
                f"{rel}: recorded in {meta_path} but no symlink at {link}"
            )
            continue
        resolved = os.path.realpath(link)
        expected_suffix = os.path.join("entries", oh, "data", within)
        if not resolved.endswith(expected_suffix):
            problems.append(
                f"{rel}: symlink resolves to {resolved}, but the record says "
                f".../{expected_suffix}"
            )
            continue
        if not os.path.exists(resolved):
            problems.append(
                f"{rel}: symlink target {resolved} does not exist "
                "(store entry gone?)"
            )

    for root, _dirs, files in os.walk(gen_dir):
        for fname in files:
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, gen_dir)
            if rel == "meta.json" or rel in claimed:
                continue
            problems.append(
                f"{rel}: present in the tree at {path} but absent from the "
                f"generation record {meta_path}"
            )

    return problems
