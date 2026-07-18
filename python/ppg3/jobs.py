"""Job classes + ``Graph`` (PRINCIPLES.md P1/P2/P10; PPG3_DESIGN.md §7,
CONTRACT.md "Scheduler" for the ``JobDef`` JSON shape).

Identity is content, not names (P1): a job's id is derived from its
*definition fingerprint* (recipe, inputs, outputs, env, runtime, publish
targets) — users never name jobs, two identical definitions merge into one
job, and the only definition-time conflict is two *different* jobs claiming
the same publish destination (P2), reported with both definition sites.

Publishing is spelled ``outputs=`` (P10): a dict mapping output *names* (how
the job itself refers to its files, and how they are laid out inside the
store entry — see P1.4) to destinations in the output tree. ``outputs`` is
optional — a job without it is an internal job that simply doesn't appear in
the output tree — and a ``None`` destination declares an output *name*
(written by the job, part of its key, consumable by children) without
publishing it (P10.2's internal jobs).

Each job's ``.job_def(graph)`` method assembles the CONTRACT.md ``JobDef``
dict (destined for ``ppg3._core.run(jobs_json, ...)``); nothing here ever
imports the compiled extension directly — leaf-file/tool hashing needs real
I/O (stat-cache, optionally ``nix build``) but no Rust.

Rust-serde shape note: plain serde-default *externally tagged* JSON (unit
variants as bare strings, e.g. ``"InProcess"``/``"Default"``; struct/tuple
variants as ``{"VariantName": {...}}``). The wire field ``view`` carries the
publish map (kept under its old wire name; the Rust side uses it only as
informational manifest metadata since P1.4 moved entry layout onto output
names).
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import sysconfig
import uuid
import warnings
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from . import canon, recipe
from .localscope import DefinitionError
from .statcache import StatCache
from .tools import PyEnv, ToolSpec
from .transport import Source, select_transport

SHIM_VERSION = "1"

_current_graph: Optional["Graph"] = None


# --------------------------------------------------------------------------
# Leaf input / parameter / resource / retention / store types
# --------------------------------------------------------------------------


class File:
    """A leaf file input, hashed via the stat-cache at run-lowering time."""

    def __init__(self, path: Union[str, "os.PathLike"]):
        self.path = str(path)

    def __repr__(self) -> str:
        return f"File({self.path!r})"


class Params:
    """A leaf parameter input: canonicalized per §5's closed type set."""

    def __init__(self, value: Any):
        self.value = value

    def canonical(self) -> Any:
        return canon.canonicalize_value(self.value, "$.params")

    def __repr__(self) -> str:
        return f"Params({self.value!r})"


class Resources:
    """Named resource-pool requirements, e.g. ``Resources(cores=4)``."""

    def __init__(self, **pools: int):
        self.pools: Dict[str, int] = {k: int(v) for k, v in pools.items()}

    def __repr__(self) -> str:
        return f"Resources({self.pools!r})"


class Retain:
    """``Retain.Default`` | ``Retain.Evict`` | ``Retain.Pin(name)`` (§7.3)."""

    class _Sentinel:
        def __init__(self, tag: str):
            self.tag = tag

        def __repr__(self) -> str:
            return f"Retain.{self.tag}"

    class Pin:
        def __init__(self, name: str):
            self.name = name

        def __repr__(self) -> str:
            return f"Retain.Pin({self.name!r})"


Retain.Default = Retain._Sentinel("Default")  # type: ignore[attr-defined]
Retain.Evict = Retain._Sentinel("Evict")  # type: ignore[attr-defined]


def _resolve_retain(retain: Any) -> Any:
    """Default + validate a constructor's ``retain=`` at definition time —
    a bad value is a definition error, not something to surface only once
    ``job_def()`` lowers it at run time."""
    retain = retain if retain is not None else Retain.Default
    _retain_json(retain)
    return retain


def _retain_json(retain: Any) -> Any:
    if retain is Retain.Default:
        return "Default"
    if retain is Retain.Evict:
        return "Evict"
    if isinstance(retain, Retain.Pin):
        return {"Pin": retain.name}
    raise DefinitionError(
        f"invalid retain= value {retain!r}; expected ppg3.Retain.Default, "
        "ppg3.Retain.Evict, or ppg3.Retain.Pin(name)"
    )


class Store:
    """A store entry for ``ppg3.new(stores=[...])`` (§4.1)."""

    def __init__(self, name: str, path: Union[str, "os.PathLike"], readonly: bool = False):
        self.name = name
        self.path = str(path)
        self.readonly = readonly

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "path": self.path, "readonly": self.readonly}

    def __repr__(self) -> str:
        return f"Store({self.name!r}, {self.path!r}, readonly={self.readonly!r})"


# --------------------------------------------------------------------------
# CommandJob argv placeholders
# --------------------------------------------------------------------------


class In:
    def __init__(self, name: str):
        self.name = name

    def _wire(self) -> str:
        return f"{{in:{self.name}}}"

    def __repr__(self) -> str:
        return f"In({self.name!r})"


class Out:
    def __init__(self, name: Optional[str] = None):
        self.name = name

    def _wire(self) -> str:
        return "{out}" if self.name is None else f"{{out:{self.name}}}"

    def __repr__(self) -> str:
        return f"Out({self.name!r})"


class Tool:
    def __init__(self, name: str):
        self.name = name

    def _wire(self) -> str:
        return f"{{tool:{self.name}}}"

    def __repr__(self) -> str:
        return f"Tool({self.name!r})"


def serialize_argv(argv: Sequence[Any]) -> list:
    """Lower a ``CommandJob`` argv template (mixed str/In/Out/Tool) to the
    wire-form list of strings (§6.2: placeholders, never real paths)."""
    out = []
    for i, item in enumerate(argv):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (In, Out, Tool)):
            out.append(item._wire())
        else:
            raise DefinitionError(
                f"$.argv[{i}]: CommandJob argv entries must be str/In/Out/Tool, "
                f"got {type(item).__name__} ({item!r})"
            )
    return out


# --------------------------------------------------------------------------
# Definition sites (P1.5/P9.1: how humans find their jobs)
# --------------------------------------------------------------------------

# Absolute directory of the `ppg3` package itself — call-site walks skip
# frames inside it.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_STDLIB_DIR = sysconfig.get_paths().get("stdlib", "") or "\x00never"


def _is_user_frame(filename: str) -> bool:
    if filename.startswith("<"):  # <frozen ...>, <string>, REPL
        return False
    if filename == _PKG_DIR or filename.startswith(_PKG_DIR + os.sep):
        return False
    if filename.startswith(_STDLIB_DIR):
        return False
    if f"{os.sep}site-packages{os.sep}" in filename or f"{os.sep}dist-packages{os.sep}" in filename:
        return False
    return True


def _user_call_chain(limit: int = 2) -> List[Tuple[str, int]]:
    """Up to ``limit`` user-code ``(file, line)`` frames, innermost first,
    walking out from the job constructor: the definition site, plus the
    caller of a wrapper function if there is one. This is how every report
    refers back to a job (P1.5) — the id itself is never shown alone."""
    frames: List[Tuple[str, int]] = []
    frame = inspect.currentframe()
    try:
        if frame is None:  # pragma: no cover
            return frames
        frame = frame.f_back
        while frame is not None and len(frames) < limit:
            filename = os.path.abspath(frame.f_code.co_filename)
            if _is_user_frame(filename) and os.path.isfile(filename):
                frames.append((filename, frame.f_lineno))
            frame = frame.f_back
        return frames
    finally:
        del frame


def _record_call_site() -> Optional[Tuple[str, int]]:
    """Best-effort ``(file, lineno)`` of the first stack frame outside the
    ``ppg3`` package (the §7.6 TOFU patch target and the jj source file)."""
    chain = _user_call_chain(limit=1)
    return chain[0] if chain else None


def _format_defsite(chain: Sequence[Tuple[str, int]]) -> str:
    if not chain:
        return "<unknown definition site>"
    return " ← ".join(f"{f}:{l}" for f, l in chain)


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------


class Graph:
    def __init__(
        self,
        stores: Sequence[Store],
        default_python: Optional[PyEnv],
        project_dir: Union[str, "os.PathLike"],
        parallelism: Dict[str, int],
        frozen: bool,
        paranoid: bool,
        forkserver: bool = True,
        jj: bool = False,
        sandbox: str = "auto",
    ):
        self.stores = list(stores)
        self.default_python = default_python
        self.project_dir = str(project_dir)
        self.parallelism = dict(parallelism)
        self.frozen = frozen
        self.paranoid = paranoid
        # jj (jujutsu) support (see jj.py): when enabled, run() hard-errors
        # on job sources not tracked in the enclosing jj workspace and
        # captures commit/change/op-log ids into the generation meta.
        self.jj = jj
        # PRINCIPLES.md P6: "require" (error at run start if enforcement is
        # unavailable) | "auto" (best available, warn once per run when
        # downgraded) | "off" (explicit, silent).
        if sandbox not in ("require", "auto", "off"):
            raise DefinitionError(
                f"ppg3.new(sandbox={sandbox!r}): expected \"require\", "
                "\"auto\", or \"off\""
            )
        self.sandbox = sandbox
        # §6.4: warm template processes for python FileJob/DataJob/FetchJob
        # dispatch. `False` disables it (`run.py` then passes an empty
        # `template_argv` to `ppg3._core.run`).
        self.forkserver = forkserver
        self.jobs: Dict[str, "Job"] = {}
        self.data_job_ids: Set[str] = set()
        # P2: destination -> claiming job. The ONLY definition-time
        # uniqueness constraint in the system.
        self.claims: Dict[str, "Job"] = {}
        self._statcache: Optional[StatCache] = None
        # §6.7 watch mode: paths that should trigger a re-run of the
        # definition pass on change.
        self._watched_paths: Set[str] = set()
        # jj support: files that *define* jobs.
        self._source_paths: Set[str] = set()

    def record_watched_path(self, path: Union[str, "os.PathLike"]) -> None:
        """Record a path that watch mode (§6.7) should poll for changes."""
        self._watched_paths.add(str(path))

    def record_source_path(self, path: Union[str, "os.PathLike"]) -> None:
        """Record a job-*source* file (for jj tracking enforcement)."""
        self._source_paths.add(str(path))

    def source_paths(self) -> List[str]:
        return sorted(self._source_paths)

    def watched_paths(self) -> List[str]:
        return sorted(self._watched_paths)

    def add(self, job: "Job") -> "Job":
        """Register `job`. Identical redefinition (same fingerprint) merges
        (P2.3); a contested destination is the only conflict (P2.1), and its
        error points at both definition sites."""
        existing = self.jobs.get(job.id)
        if existing is not None:
            # Same fingerprint = the same job, stated twice. The new object
            # shares the id, so anything referencing it lowers to the
            # registered one.
            return existing
        for dest in job.publish.values():
            claimant = self.claims.get(dest)
            if claimant is not None and claimant.id != job.id:
                raise DefinitionError(
                    f"two different jobs both publish {dest!r}:\n"
                    f"  first:  defined at {claimant.defsite}\n"
                    f"  second: defined at {job.defsite}\n"
                    "one destination, one producer — change one of the paths "
                    "(identical jobs would have merged; these differ)"
                )
        for dest in job.publish.values():
            self.claims[dest] = job
        self.jobs[job.id] = job
        return job

    def statcache(self) -> StatCache:
        if self._statcache is None:
            self._statcache = StatCache(os.path.join(self.project_dir, "statcache.sqlite"))
        return self._statcache

    def job_defs(self) -> list:
        """Lower every job to its JobDef dict — what run.py hands to
        ``ppg3._core.run``. Requires real I/O (stat-cache, tool resolution)."""
        return [job.job_def(self) for job in self.jobs.values()]


def _is_ci() -> bool:
    for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE"):
        if os.environ.get(var):
            return True
    return False


def new(
    stores: Optional[Sequence[Store]] = None,
    default_python: Optional[PyEnv] = None,
    project_dir: Union[str, "os.PathLike"] = ".ppg3",
    parallelism: Optional[Dict[str, int]] = None,
    frozen: Optional[bool] = None,
    paranoid: bool = False,
    forkserver: bool = True,
    jj: bool = False,
    sandbox: str = "auto",
) -> Graph:
    """Create (and make current) a new ``Graph``. Module-level "current
    graph" like ppg2 — job constructors look it up implicitly.

    ``sandbox=`` (PRINCIPLES.md P6): ``"require"`` errors at run start when
    real enforcement (bwrap + nix) is unavailable; ``"auto"`` (default) uses
    the best available enforcement and warns exactly once per run when
    running unenforced; ``"off"`` opts out silently.

    ``forkserver=True`` (the default, §6.4): python `FileJob`/`DataJob`/
    `FetchJob` dispatch runs through warm per-``(PyEnv, preload)`` template
    processes instead of a cold ``python -I -m ppg3._shim`` exec per job.

    ``jj=True`` enables jj (jujutsu) support (see :mod:`ppg3.jj`).
    """
    global _current_graph
    if frozen is None:
        try:
            interactive = os.isatty(0)
        except (OSError, ValueError):
            interactive = False
        frozen = (not interactive) or _is_ci()
    if parallelism is None:
        parallelism = {"cores": os.cpu_count() or 1}
    graph = Graph(
        stores=stores or [],
        default_python=default_python,
        project_dir=project_dir,
        parallelism=parallelism,
        frozen=frozen,
        paranoid=paranoid,
        forkserver=forkserver,
        jj=jj,
        sandbox=sandbox,
    )
    _current_graph = graph
    return graph


def _require_current_graph() -> Graph:
    if _current_graph is None:
        raise DefinitionError(
            "no active ppg3 graph — call ppg3.new(...) before defining jobs"
        )
    return _current_graph


def _normalize_outputs(outputs: Any, kind: str) -> Dict[str, Optional[str]]:
    """``outputs=`` (P10): None (internal job) | dict name -> destination.

    A ``None`` destination declares the output *name* (it is written by the
    job, keyed, and consumable — P1.4/P10.2) without publishing it into the
    output tree: an internal output."""
    if outputs is None:
        return {}
    if isinstance(outputs, dict):
        for k, v in outputs.items():
            if (
                not isinstance(k, str)
                or not k
                or not (v is None or (isinstance(v, str) and v))
            ):
                raise DefinitionError(
                    f"{kind}(outputs=...): expected a dict of output-name -> "
                    "output-tree path (non-empty str), or -> None for an "
                    f"internal (unpublished) output; got {k!r}: {v!r}"
                )
        return dict(outputs)
    raise DefinitionError(
        f"{kind}(outputs=...): expected a dict of output-name -> output-tree "
        f"path, or None for an internal job; got {type(outputs).__name__}"
    )


def _apply_below(
    publish: Dict[str, Optional[str]], below: Optional[str], kind: str
) -> Dict[str, Optional[str]]:
    """``below=``: prefix every output destination with a folder — pure
    convenience for "put all this job's outputs below folder X", so a batch
    of destinations doesn't repeat the folder in every path. Publish-layer
    only (P1.4): the entry layout and the job's identity key are untouched,
    exactly as if the caller had written the joined paths by hand."""
    if below is None:
        return publish
    if not isinstance(below, str) or not below:
        raise DefinitionError(
            f"{kind}(below=...): expected a non-empty str — a folder in the "
            "output tree to put this job's outputs under"
        )
    norm = below.strip("/")
    if not norm or any(part in ("", ".", "..") for part in norm.split("/")):
        raise DefinitionError(
            f"{kind}(below={below!r}): must be a relative folder path with "
            "no '.'/'..' components"
        )
    return {
        name: f"{norm}/{dest}" if dest is not None else None
        for name, dest in publish.items()
    }


# --------------------------------------------------------------------------
# Input lowering
# --------------------------------------------------------------------------


class OutputRef:
    """``job["output_name"]`` — a named-subset reference to a parent's output."""

    def __init__(self, job: "Job", name: str):
        self.job = job
        self.name = name

    def __repr__(self) -> str:
        return f"{self.job!r}[{self.name!r}]"


def _lower_input(input_name: str, value: Any, graph: Graph) -> Dict[str, Any]:
    if isinstance(value, OutputRef):
        return {"JobSubset": {"id": value.job.id, "names": [value.name]}}
    if isinstance(value, Job):
        return {"Job": {"id": value.id}}
    if isinstance(value, File):
        # §6.7 watch mode: File inputs are watched paths, recorded here at
        # lowering time (job_defs()/job_def(), called from run()).
        graph.record_watched_path(value.path)
        h = graph.statcache().hash_file(value.path)
        # A File is BOTH a change-detection dependency (its content hash, the
        # only thing that enters the input key) AND a mounted read-only input
        # bound at /ppg/in/<name> (P4.1) — the `source` is carried out-of-band
        # and never keyed. Absolute so the mount resolves regardless of cwd.
        return {"File": {"hash": h, "source": os.path.abspath(value.path)}}
    if isinstance(value, Params):
        h = canon.input_key_local(value.canonical())
        return {"Leaf": {"hash": h}}
    raise DefinitionError(
        f"input {input_name!r}: expected a Job, job[\"output\"], ppg3.File(...), "
        f"or ppg3.Params(...), got {type(value).__name__} ({value!r})"
    )


def _fingerprint_input(value: Any) -> Any:
    """Definition-time identity of one input (no I/O — this must work before
    any store/stat-cache exists, because it feeds the job id)."""
    if isinstance(value, OutputRef):
        return ["subset", value.job.id, value.name]
    if isinstance(value, Job):
        return ["job", value.id]
    if isinstance(value, File):
        return ["file", os.path.abspath(value.path)]
    if isinstance(value, Params):
        return ["params", value.canonical()]
    return ["other", repr(value)]


def _lower_tools(tools: Sequence[ToolSpec]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in tools:
        if t.name in out:
            raise DefinitionError(f"duplicate tool name {t.name!r}")
        out[t.name] = t.resolve().hash
    return out


def _fingerprint_tools(tools: Sequence[ToolSpec]) -> list:
    return sorted([t.name, t.kind, t.ref_or_path] for t in tools)


def _fingerprint_pyenv(python_env: Optional[PyEnv]) -> Any:
    if python_env is None:
        return None
    return [python_env.kind, python_env.flake_ref, list(python_env.preload)]


def _runtime_doc(python_env: Optional[PyEnv]) -> Dict[str, Any]:
    if python_env is None:
        return {"python_env": None, "preload": [], "shim": "0"}
    res = python_env.resolve()
    return {
        "python_env": res.hash,
        "preload": list(python_env.preload),
        "shim": SHIM_VERSION,
    }


# --------------------------------------------------------------------------
# Shim spec delivery (see STATUS.md "shim stdin question")
# --------------------------------------------------------------------------


def _b64_json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, sort_keys=True).encode("utf-8")).decode("ascii")


def _mounted_input_names(inputs: Dict[str, Any]) -> List[str]:
    """Input names backed by a real mounted path (P4: every declared input
    is readable): ``Job``/``JobSubset`` refs and ``File`` refs. A ``Params``
    (`Leaf`) ref has no mount; params arrive via ``io.params``."""
    return sorted(
        n for n, v in inputs.items() if isinstance(v, (Job, OutputRef, File))
    )


def _leaf_params(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """``Params(...)``-typed inputs, canonicalized, keyed by input name —
    what ends up as ``io.params`` inside the shim (§7.1)."""
    return {n: v.canonical() for n, v in inputs.items() if isinstance(v, Params)}


def _shim_argv(
    python_env: PyEnv,
    static_spec: Dict[str, Any],
    mounted_inputs: Sequence[str],
    output_names: Sequence[str],
    tool_names: Sequence[str],
) -> List[str]:
    """Build the ``python -I -m ppg3._shim`` argv (CONTRACT.md addendum,
    "Shim spec delivery"): the static part of the spec travels as one base64
    JSON blob (``--spec-b64``); every real/virtual path travels as its own
    argv token carrying exactly one ``{in:NAME}``/``{out:NAME}``/
    ``{tool:NAME}`` placeholder so the scheduler's `lower_argv` can resolve
    it."""
    argv = [
        python_env.executable_hint(),
        "-I",
        "-m",
        "ppg3._shim",
        "--spec-b64",
        _b64_json(static_spec),
    ]
    for name in mounted_inputs:
        argv += ["--in", name, f"{{in:{name}}}"]
    for name in output_names:
        argv += ["--out", name, f"{{out:{name}}}"]
    for name in tool_names:
        argv += ["--tool", name, f"{{tool:{name}}}"]
    argv += ["--log-dir", "/ppg/log"]
    return argv


# --------------------------------------------------------------------------
# Job base
# --------------------------------------------------------------------------


def _callable_source_file(fn: Callable) -> Optional[str]:
    """Best-effort on-disk source file of a callback, or ``None`` (builtins,
    C extensions, REPL-defined functions)."""
    try:
        path = inspect.getsourcefile(fn)
    except TypeError:
        return None
    if path is None or not os.path.isfile(path):
        return None
    return os.path.abspath(path)


class Job:
    kind = "base"

    def __init__(self, graph: Graph, outputs_spec: Dict[str, Optional[str]]):
        self.graph = graph
        # P10: output name -> destination in the output tree, or None for a
        # declared-but-unpublished (internal) output. May be empty.
        self.outputs_spec = dict(outputs_spec) if outputs_spec else {}
        # The publish map proper — only the outputs that appear in the
        # output tree. Everything view-layer reads this; identity (output
        # *names*, P1.4) reads outputs_spec.
        self.publish = {
            k: v for k, v in self.outputs_spec.items() if v is not None
        }
        # P1.5: how humans find this job.
        self.defsite_chain = _user_call_chain()
        # jj support: the file this job constructor was called from is a
        # job-source file.
        if self.defsite_chain:
            graph.record_source_path(self.defsite_chain[0][0])

    # -- identity (P1) ------------------------------------------------

    def _fingerprint_doc(self) -> Dict[str, Any]:
        """Definition fingerprint: everything that makes this job *this*
        job. Subclasses extend. Two jobs with equal fingerprints are the
        same job (dedup/merge); the id derives from this and nothing else —
        there is no user-assignable name."""
        return {
            "kind": self.kind,
            "publish": dict(sorted(self.outputs_spec.items())),
        }

    def _register(self) -> None:
        """Compute the id from the fingerprint and register with the graph.
        Must be the last statement of every concrete ``__init__``."""
        doc = json.dumps(self._fingerprint_doc(), sort_keys=True, default=repr)
        self.id = "j" + hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]
        self.graph.add(self)

    # -- display (P1.5) -----------------------------------------------

    @property
    def defsite(self) -> str:
        return _format_defsite(self.defsite_chain)

    @property
    def label(self) -> str:
        """The human name of this job: its published destinations, or its
        definition site for internal jobs. Never the internal id."""
        if self.publish:
            return "+".join(sorted(self.publish.values()))
        site = self.defsite_chain[0] if self.defsite_chain else None
        where = f"{os.path.basename(site[0])}:{site[1]}" if site else "?"
        return f"<{self.kind} @ {where}>"

    def __getitem__(self, name: str) -> OutputRef:
        declared = self.output_names()
        if name not in declared:
            raise DefinitionError(
                f"{self!r} (defined at {self.defsite}) has no declared "
                f"output {name!r}; declared outputs: {declared}"
            )
        return OutputRef(self, name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.label!r})"

    def output_names(self) -> List[str]:
        """Declared output names — entry-internal layout (P1.4). Defaults to
        the outputs spec's keys (published or not); subclasses with fixed
        names override."""
        return sorted(self.outputs_spec.keys())

    def job_def(self, graph: Optional[Graph] = None) -> Dict[str, Any]:
        raise NotImplementedError


# --------------------------------------------------------------------------
# FileJob
# --------------------------------------------------------------------------


class FileJob(Job):
    kind = "file"
    # Overridden to True by DataJob — see its class docstring.
    _pickle_output = False

    def __init__(
        self,
        outputs: Optional[Dict[str, Optional[str]]] = None,
        run: Union[Callable, Source, None] = None,
        tools: Sequence[ToolSpec] = (),
        inputs: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
        resources: Optional[Resources] = None,
        retain: Any = None,
        python: Optional[PyEnv] = None,
        store: Optional[str] = None,
        below: Optional[str] = None,
    ):
        """
        outputs = output name -> output-tree destination (optional: omit
                  for an internal job that is not published; a None
                  destination declares the name — written, keyed,
                  consumable — without publishing it)
        run     = what we execute (callable or ppg3.Source)
        tools   = what's in the sandbox
        inputs  = upstream jobs / files / params
        env     = environment variables (declared = keyed + visible, P5.4)
        python  = Python environment
        retain  = Retain.Default | Retain.Evict | Retain.Pin - GC behaviour
        below   = optional folder to put every output destination under
                  (``outputs={"a": "x.txt"}, below="s1"`` publishes s1/x.txt)
        """
        if run is None:
            raise DefinitionError("FileJob requires run= (a callable or ppg3.Source)")
        publish = _apply_below(_normalize_outputs(outputs, "FileJob"), below, "FileJob")
        graph = _require_current_graph()
        python_env = python or graph.default_python
        if python_env is None:
            raise DefinitionError(
                "FileJob requires a PyEnv: pass python=... or set "
                "ppg3.new(default_python=...)"
            )
        super().__init__(graph, publish)
        self.run = run
        if isinstance(run, Source):
            # §6.7 watch mode: Source callback files are watched paths,
            # recorded at definition time. They are also job-source files
            # for jj tracking enforcement.
            graph.record_watched_path(run.path)
            graph.record_source_path(run.path)
            for inc in run.includes:
                graph.record_watched_path(inc)
                graph.record_source_path(inc)
        elif callable(run):
            src = _callable_source_file(run)
            if src is not None:
                graph.record_source_path(src)
        self.tools = list(tools)
        self.inputs = dict(inputs or {})
        self.env = dict(env or {})
        self.resources = resources.pools if isinstance(resources, Resources) else {}
        self.retain = _resolve_retain(retain)
        self.python_env = python_env
        self.store = store
        self._transport = select_transport(run, python_env, paranoid=graph.paranoid)
        self._register()

    def _fingerprint_doc(self) -> Dict[str, Any]:
        doc = super()._fingerprint_doc()
        doc.update(
            {
                "recipe": self._transport["recipe"],
                "inputs": {
                    n: _fingerprint_input(v) for n, v in sorted(self.inputs.items())
                },
                "tools": _fingerprint_tools(self.tools),
                "env": dict(sorted(self.env.items())),
                "resources": dict(sorted(self.resources.items())),
                "retain": repr(self.retain),
                "store": self.store,
                "pyenv": _fingerprint_pyenv(self.python_env),
                "pickle_output": self._pickle_output,
                "output_names": self.output_names(),
            }
        )
        return doc

    def job_def(self, graph: Optional[Graph] = None) -> Dict[str, Any]:
        graph = graph or self.graph
        inputs_json = {
            n: _lower_input(n, v, graph) for n, v in self.inputs.items()
        }
        static_spec = {
            "mode": "callback",
            "transport": self._transport["transport"],
            "pickle_output": self._pickle_output,
            "params": _leaf_params(self.inputs),
        }
        argv = _shim_argv(
            self.python_env,
            static_spec,
            _mounted_input_names(self.inputs),
            self.output_names(),
            [t.name for t in self.tools],
        )
        return {
            "id": self.id,
            "recipe": self._transport["recipe"],
            "inputs": inputs_json,
            "tools": _lower_tools(self.tools),
            "runtime": _runtime_doc(self.python_env),
            "env": dict(self.env),
            "outputs_declared": self.output_names(),
            "resources": dict(self.resources),
            "store_target": self.store,
            "retain": _retain_json(self.retain),
            "exec_template": {
                "Argv": {
                    "argv": argv,
                    "allow_network": False,
                }
            },
            "view": dict(self.publish),
            "fixed_output": None,
            "graph_job": False,
        }


# --------------------------------------------------------------------------
# CommandJob
# --------------------------------------------------------------------------


class CommandJob(Job):
    kind = "command"

    def __init__(
        self,
        outputs: Optional[Dict[str, Optional[str]]] = None,
        argv: Optional[Sequence[Any]] = None,
        tools: Sequence[ToolSpec] = (),
        inputs: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
        resources: Optional[Resources] = None,
        retain: Any = None,
        store: Optional[str] = None,
        below: Optional[str] = None,
    ):
        if argv is None:
            raise DefinitionError("CommandJob requires argv=")
        publish = _apply_below(_normalize_outputs(outputs, "CommandJob"), below, "CommandJob")
        graph = _require_current_graph()
        super().__init__(graph, publish)
        self.argv_template = serialize_argv(argv)
        self.tools = list(tools)
        self.inputs = dict(inputs or {})
        self.env = dict(env or {})
        self.resources = resources.pools if isinstance(resources, Resources) else {}
        self.retain = _resolve_retain(retain)
        self.store = store
        self._recipe = recipe.recipe_hash_command(self.argv_template)
        self._register()

    def _fingerprint_doc(self) -> Dict[str, Any]:
        doc = super()._fingerprint_doc()
        doc.update(
            {
                "recipe": self._recipe,
                "inputs": {
                    n: _fingerprint_input(v) for n, v in sorted(self.inputs.items())
                },
                "tools": _fingerprint_tools(self.tools),
                "env": dict(sorted(self.env.items())),
                "resources": dict(sorted(self.resources.items())),
                "retain": repr(self.retain),
                "store": self.store,
                "output_names": self.output_names(),
            }
        )
        return doc

    def job_def(self, graph: Optional[Graph] = None) -> Dict[str, Any]:
        graph = graph or self.graph
        inputs_json = {
            n: _lower_input(n, v, graph) for n, v in self.inputs.items()
        }
        return {
            "id": self.id,
            "recipe": self._recipe,
            "inputs": inputs_json,
            "tools": _lower_tools(self.tools),
            "runtime": {"python_env": None, "preload": [], "shim": "0"},
            "env": dict(self.env),
            "outputs_declared": self.output_names(),
            "resources": dict(self.resources),
            "store_target": self.store,
            "retain": _retain_json(self.retain),
            "exec_template": {
                "Argv": {"argv": list(self.argv_template), "allow_network": False}
            },
            "view": dict(self.publish),
            "fixed_output": None,
            "graph_job": False,
        }


# --------------------------------------------------------------------------
# DataJob
# --------------------------------------------------------------------------


class DataJob(FileJob):
    """A ``FileJob`` producing a single pickled artifact (§6.3 tier 1).

    The shim pickles the callback's return value to ``data.pickle``;
    consumers declare this job as an input and call ``io.load(name)``.
    ``outputs`` is a single destination path (or None: an internal data
    artifact, consumed by other jobs but not published).
    """

    kind = "data"
    OUTPUT_NAME = "data.pickle"
    _pickle_output = True

    def __init__(
        self,
        outputs: Optional[str] = None,
        run: Union[Callable, Source, None] = None,
        **kwargs,
    ):
        if outputs is None:
            publish: Dict[str, str] = {}
        elif isinstance(outputs, str):
            publish = {self.OUTPUT_NAME: outputs}
        else:
            raise DefinitionError(
                "DataJob(outputs=...) must be a single output-tree path "
                "(str), or None for an internal data artifact"
            )
        super().__init__(outputs=publish, run=run, **kwargs)
        self.graph.data_job_ids.add(self.id)

    def output_names(self) -> List[str]:
        # The pickle artifact exists (and is keyed, P1.4) whether or not it
        # is published.
        return [self.OUTPUT_NAME]


# --------------------------------------------------------------------------
# FetchJob (§7.6; PRINCIPLES.md P7 — the pin is the identity)
# --------------------------------------------------------------------------


class FetchJob(Job):
    kind = "fetch"
    OUTPUT_NAME = "file"

    def __init__(
        self,
        outputs: Optional[str] = None,
        url: Optional[str] = None,
        blake3: Optional[str] = None,
        retain: Any = None,
        store: Optional[str] = None,
        below: Optional[str] = None,
    ):
        if url is None:
            raise DefinitionError("FetchJob requires url=")
        if outputs is None:
            publish: Dict[str, str] = {}
        elif isinstance(outputs, str):
            publish = {self.OUTPUT_NAME: outputs}
        else:
            raise DefinitionError(
                "FetchJob(outputs=...) must be a single output-tree path "
                "(str), or None for an internal fetch"
            )
        publish = _apply_below(publish, below, "FetchJob")
        graph = _require_current_graph()
        if blake3 is None and graph.frozen:
            raise DefinitionError(
                f"FetchJob(outputs={outputs!r}, url={url!r}): blake3=None is "
                "rejected in --frozen mode (the default outside an interactive "
                "terminal, or under CI). TOFU (trust-on-first-use, §7.6) is an "
                "interactive-only escape hatch; --frozen never TOFUs — pin the "
                "hash by hand or run interactively once to let ppg3 patch it "
                "in for you."
            )
        super().__init__(graph, publish)
        self.url = url
        self.blake3 = blake3
        # §7.6 TOFU: recorded unconditionally (cheap), consumed only for
        # jobs actually defined with blake3=None (see tofu.py).
        self._call_site = _record_call_site()
        self.retain = _resolve_retain(retain)
        self.store = store
        python_env = graph.default_python
        if python_env is None:
            raise DefinitionError(
                "FetchJob requires ppg3.new(default_python=...) to run its "
                "fetch shim invocation"
            )
        self.python_env = python_env
        # P7.1: the pin participates in the identity — (url, expected hash)
        # IS the fetch. An unpinned fetch (blake3=None, interactive TOFU)
        # means "trust the next download": it gets a one-shot key (never a
        # memo hit), and the TOFU pass aliases the *pinned* key onto the
        # entry it produces so the patched-source run hits (see tofu.py).
        self._recipe = self._recipe_for_pin(
            blake3 if blake3 is not None else {"tofu_one_shot": uuid.uuid4().hex}
        )
        self._register()

    @staticmethod
    def _recipe_for_pin(pin: Any) -> str:
        return canon.input_key_local(
            canon.canonicalize_value({"kind": "fetch", "blake3": pin}, "$.fetch")
        )

    def _fingerprint_doc(self) -> Dict[str, Any]:
        doc = super()._fingerprint_doc()
        doc.update(
            {
                "url": self.url,
                "blake3": self.blake3,
                "recipe": self._recipe,
                "retain": repr(self.retain),
                "store": self.store,
            }
        )
        return doc

    def output_names(self) -> List[str]:
        return [self.OUTPUT_NAME]

    def job_def(self, graph: Optional[Graph] = None) -> Dict[str, Any]:
        return self._job_def_for(self._recipe, self.blake3)

    def pinned_job_def(self, digest: str) -> Dict[str, Any]:
        """The JobDef this job will lower to once ``blake3=digest`` is
        pinned into the source — what the TOFU pass derives the pinned input
        key from (P7.2), so the very next run hits instead of re-downloading
        what it just fetched."""
        return self._job_def_for(self._recipe_for_pin(digest), digest)

    def _job_def_for(self, recipe_hash: str, fixed_output: Optional[str]) -> Dict[str, Any]:
        static_spec = {"mode": "fetch", "url": self.url, "blake3": fixed_output}
        argv = _shim_argv(self.python_env, static_spec, [], [self.OUTPUT_NAME], [])
        return {
            "id": self.id,
            "recipe": recipe_hash,
            # The URL is provenance, not identity-of-content — but changing
            # it must refetch (P7.3): it rides in the key as a leaf input.
            "inputs": {"url": {"Leaf": {"hash": canon.input_key_local(self.url)}}},
            "tools": {},
            # A fetch's identity is (url, pin) — NOT the local Python
            # environment; the shim that downloads is env-independent.
            "runtime": {"python_env": None, "preload": [], "shim": SHIM_VERSION},
            "env": {},
            "outputs_declared": [self.OUTPUT_NAME],
            "resources": {},
            "store_target": self.store,
            "retain": _retain_json(self.retain),
            "exec_template": {
                "Argv": {
                    "argv": argv,
                    "allow_network": True,
                }
            },
            "view": dict(self.publish),
            "fixed_output": fixed_output,
            "graph_job": False,
        }


# --------------------------------------------------------------------------
# GraphJob
# --------------------------------------------------------------------------


class GraphJob(Job):
    """Dynamic graph expansion (§7.4, ppg2's JobGeneratingJob). Runs
    in-process via ``HostCallbacks.expand_graph_job``; recorded but not
    keyed (its recipe hash appears in the run report only)."""

    kind = "graph"

    def __init__(self, fn: Callable):
        graph = _require_current_graph()
        super().__init__(graph, {})
        self.fn = fn
        src = _callable_source_file(fn)
        if src is not None:
            graph.record_source_path(src)
        self._recipe = recipe.recipe_hash(fn)
        self._register()

    def _fingerprint_doc(self) -> Dict[str, Any]:
        doc = super()._fingerprint_doc()
        doc.update({"recipe": self._recipe})
        return doc

    def job_def(self, graph: Optional[Graph] = None) -> Dict[str, Any]:
        return {
            "id": self.id,
            "recipe": self._recipe,
            "inputs": {},
            "tools": {},
            "runtime": {"python_env": None, "preload": [], "shim": "0"},
            "env": {},
            "outputs_declared": [],
            "resources": {},
            "store_target": None,
            "retain": _retain_json(Retain.Default),
            "exec_template": "InProcess",
            "view": {},
            "fixed_output": None,
            "graph_job": True,
        }


# --------------------------------------------------------------------------
# UnsandboxedJob
# --------------------------------------------------------------------------


class UnsandboxedJob(Job):
    """The explicit escape hatch (§6.3 tier 3): forks from the coordinator,
    sees loader-layer results via COW, but is still publish-time
    determinism-checked. Warned about at definition time."""

    kind = "unsandboxed"

    def __init__(
        self,
        run: Callable,
        outputs: Optional[Union[str, Dict[str, Optional[str]]]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
        resources: Optional[Resources] = None,
        retain: Any = None,
        below: Optional[str] = None,
    ):
        graph = _require_current_graph()
        if isinstance(outputs, str):
            publish = {"out": outputs}
        else:
            publish = _normalize_outputs(outputs, "UnsandboxedJob")
        publish = _apply_below(publish, below, "UnsandboxedJob")
        super().__init__(graph, publish)
        self.run = run
        src = _callable_source_file(run)
        if src is not None:
            graph.record_source_path(src)
        self.inputs = dict(inputs or {})
        self.env = dict(env or {})
        self.resources = resources.pools if isinstance(resources, Resources) else {}
        self.retain = _resolve_retain(retain)
        self._recipe = recipe.recipe_hash(run)
        self._register()
        warnings.warn(
            f"UnsandboxedJob {self.label!r}: runs unsandboxed, forked from "
            "the coordinator (§6.3 tier 3) — marked sandboxed=false in its "
            "manifest and still determinism-checked at publish, but it "
            "inherits ppg2's fork-under-threads hazards. Prefer DataJob "
            "unless you genuinely need COW-shared in-process state.",
            UserWarning,
            stacklevel=2,
        )

    def _fingerprint_doc(self) -> Dict[str, Any]:
        doc = super()._fingerprint_doc()
        doc.update(
            {
                "recipe": self._recipe,
                "inputs": {
                    n: _fingerprint_input(v) for n, v in sorted(self.inputs.items())
                },
                "env": dict(sorted(self.env.items())),
                "resources": dict(sorted(self.resources.items())),
                "retain": repr(self.retain),
            }
        )
        return doc

    def job_def(self, graph: Optional[Graph] = None) -> Dict[str, Any]:
        graph = graph or self.graph
        inputs_json = {
            n: _lower_input(n, v, graph) for n, v in self.inputs.items()
        }
        return {
            "id": self.id,
            "recipe": self._recipe,
            "inputs": inputs_json,
            "tools": {},
            "runtime": {"python_env": None, "preload": [], "shim": "0"},
            "env": dict(self.env),
            "outputs_declared": self.output_names(),
            "resources": dict(self.resources),
            "store_target": None,
            "retain": _retain_json(self.retain),
            "exec_template": "InProcess",
            "view": dict(self.publish),
            "fixed_output": None,
            "graph_job": False,
        }
