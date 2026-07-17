"""P10 — One word per concept. See PRINCIPLES.md.

These are the only tests (besides P1.1) that assert constructor *shape*;
everything else goes through conftest's target-API shims precisely so the
rename asserted here only touches the shims when it lands.
"""

from __future__ import annotations

import inspect

import ppg3
from principles_helpers import new_graph, principle


def _noop(io):
    pass


@principle("P10.1")
def test_publishing_is_spelled_outputs_not_view():
    for ctor in (ppg3.FileJob, ppg3.CommandJob, ppg3.FetchJob):
        params = inspect.signature(ctor).parameters
        assert "outputs" in params and "view" not in params, (
            f"{ctor.__name__}: the output-name → tree-path mapping is "
            "publishing, and it is spelled outputs=; 'view' names nothing "
            "a user ever asked for"
        )


@principle("P10.2")
def test_a_job_without_published_outputs_is_legal(tmp_path):
    new_graph(tmp_path)
    # An internal job: consumers reference it directly; it simply does not
    # appear in the output tree. Nothing about "being a job" requires being
    # published.
    ppg3.FileJob(run=_noop)
