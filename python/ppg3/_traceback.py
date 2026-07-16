"""Rich, self-contained traceback formatting for ppg3 job failures.

Adapted from ppg2's ``test/ppg_traceback.py`` (itself borrowed from
``rich``, Copyright 2020 Will McGugan, Florian Finkernagel), with the sole
behavioural change that it is **stdlib-only**: the ``rich.markup.escape``
dependency is dropped (job workers — ``ppg3._shim`` / ``ppg3._template`` —
must import with *zero* third-party dependencies, since they run inside a
job's declared ``PyEnv`` which may have nothing else installed).

What it adds over ``traceback.print_exc()``:

- per-frame **source context** (± a few lines, with the failing line marked
  ``>``), resolved through :mod:`linecache` so source-mode job callbacks —
  shipped under a synthetic ``<ppg3-source:NAME>`` filename and *registered*
  in ``linecache`` by ``_shim`` — render their real code, not a bare line
  number;
- per-frame **local variables** (``str()``'d, truncated), the "variables and
  all" that make a failed build job actually diagnosable;
- ``__cause__``/``__context__`` chain walking.

The single public entry point, :func:`format_exc`, is deliberately
*robust*: any failure while capturing or formatting falls back to the
stdlib ``traceback.format_exc()`` so a job never dies with a traceback about
the traceback formatter.
"""

from __future__ import annotations

import linecache
import textwrap
import traceback as _stdlib_traceback
from dataclasses import dataclass, field
from traceback import walk_tb
from types import TracebackType
from typing import Dict, List, Optional, Type

# How many source lines of context to show on either side of the failing
# line in each frame.
_CONTEXT_LINES = 3
# Per-local value truncation, matching ppg2.
_MAX_LOCAL_LEN = 1000


@dataclass
class Frame:
    filename: str
    lineno: int
    name: str
    locals: Dict[str, str]
    source: str


@dataclass
class Stack:
    exc_type: str
    exc_value: str
    # How this exception relates to the one raised *after* it (i.e. the stack
    # printed just before it, since output is outermost-cause-first):
    # "cause" = explicit `raise ... from ...`, "context" = implicit "during
    # handling of the above", None = the outermost exception (no successor).
    relation_to_next: Optional[str] = None
    frames: List[Frame] = field(default_factory=list)


def _read_source(filename: str) -> str:
    """Full source text for ``filename``, via :mod:`linecache` first (so
    ``<ppg3-source:...>`` synthetic modules registered by ``_shim`` resolve),
    then a direct read for real files not cached. Returns ``""`` if neither
    works — the formatter degrades to "# no source available"."""
    lines = linecache.getlines(filename)
    if lines:
        return "".join(lines)
    if filename and not filename.startswith("<"):
        try:
            with open(filename, "rb") as op:
                return op.read().decode("utf-8", errors="replace")
        except Exception:
            pass
    return ""


class Trace:
    """Captured (never-formatted-yet) view of an exception chain — a list of
    :class:`Stack`, innermost cause last, each with its frames + locals +
    source already snapshotted (so it survives the interpreter moving on)."""

    def __init__(
        self,
        exc_type: Type[BaseException],
        exc_value: BaseException,
        tb: Optional[TracebackType],
    ):
        stacks: List[Stack] = []
        # Relation of the exception we are *about* to append to the one just
        # appended (its later successor): set when we follow a cause/context
        # edge, consumed when the earlier exception's Stack is created.
        pending_relation: Optional[str] = None

        while True:
            stack = Stack(
                exc_type=str(getattr(exc_type, "__name__", exc_type)),
                exc_value=_safe_str(exc_value),
                relation_to_next=pending_relation,
            )
            stacks.append(stack)

            for frame_summary, line_no in walk_tb(tb):
                filename = frame_summary.f_code.co_filename
                source = _read_source(filename)
                my_locals: Dict[str, str] = {}
                for key, value in frame_summary.f_locals.items():
                    my_locals[key] = _safe_str(value)
                stack.frames.append(
                    Frame(
                        filename=filename,
                        lineno=line_no,
                        name=frame_summary.f_code.co_name,
                        locals=my_locals,
                        source=source,
                    )
                )

            # Follow the explicit `raise ... from ...` cause first, then the
            # implicit "during handling of the above" context. The edge's
            # relation is stamped onto the *earlier* exception's Stack (built
            # next iteration) via `pending_relation`, because output is
            # outermost-cause-first and the separator is printed just before
            # the later exception.
            cause = getattr(exc_value, "__cause__", None)
            if cause is not None and getattr(cause, "__traceback__", None):
                exc_type, exc_value, tb = cause.__class__, cause, cause.__traceback__
                pending_relation = "cause"
                continue
            context = getattr(exc_value, "__context__", None)
            if (
                context is not None
                and not getattr(exc_value, "__suppress_context__", False)
                and getattr(context, "__traceback__", None)
            ):
                exc_type, exc_value, tb = (
                    context.__class__,
                    context,
                    context.__traceback__,
                )
                pending_relation = "context"
                continue
            break

        # Outermost cause first when printed.
        self.stacks = stacks[::-1]

    def format(self, include_locals: bool = True) -> str:
        out: List[str] = []
        for i, stack in enumerate(self.stacks):
            if i > 0:
                # Separator text is driven by the *previous* printed stack's
                # edge to this one (output is outermost-cause-first).
                relation = self.stacks[i - 1].relation_to_next
                out.append("")
                out.append(
                    "The above exception was the direct cause of the following:"
                    if relation == "cause"
                    else "During handling of the above exception, another occurred:"
                )

            exc_value = stack.exc_value
            if len(exc_value) > _MAX_LOCAL_LEN:
                exc_value = exc_value[:_MAX_LOCAL_LEN] + "…"
            out.append(f"Exception: {stack.exc_type} {exc_value}")
            out.append("Traceback (most recent call last):")

            for frame in stack.frames:
                out.append(f"  {frame.filename}:{frame.lineno}, in {frame.name}")
                if frame.source:
                    code = frame.source.split("\n")
                    center = frame.lineno - 1  # 0-based index of failing line
                    lo = max(0, center - _CONTEXT_LINES)
                    hi = min(len(code), center + _CONTEXT_LINES + 1)
                    for ii in range(lo, hi):
                        marker = "> " if ii == center else "  "
                        out.append(f"\t{marker}{ii + 1} {code[ii]}")
                else:
                    out.append("\t# no source available")
                if include_locals and frame.locals:
                    _render_locals(out, frame.locals)

        # Conventional trailing exception line for the *final* (outermost-
        # raised) exception, mirroring stdlib tracebacks. Keeps the exception
        # visible even when only the tail of a long traceback is surfaced
        # (e.g. the scheduler's 20-line `stderr_tail`).
        if self.stacks:
            final = self.stacks[-1]
            value = final.exc_value
            if len(value) > _MAX_LOCAL_LEN:
                value = value[:_MAX_LOCAL_LEN] + "…"
            out.append(f"{final.exc_type}: {value}")
        return "\n".join(out) + "\n"


def _render_locals(out: List[str], scope: Dict[str, str]) -> None:
    out.append("\tLocals:")
    items = sorted(scope.items())
    longest = max((len(k) for k, _ in items), default=0)
    for key, value in items:
        v = value
        if len(v) > _MAX_LOCAL_LEN:
            v = v[:_MAX_LOCAL_LEN] + "…"
        v = textwrap.indent(v, "\t   " + " " * longest).lstrip()
        out.append(f"\t{key.rjust(longest)} = {v}")
    out.append("")


def _safe_str(value) -> str:
    try:
        return str(value)
    except Exception as e:  # a __str__ that itself raises must not kill us
        return f"<unprintable {type(value).__name__}: {e}>"


def format_exc(include_locals: bool = True) -> str:
    """Format the exception currently being handled (``sys.exc_info()``) as a
    rich traceback string. Never raises: on any internal failure it falls
    back to the stdlib ``traceback.format_exc()`` so a job's real error is
    always reported, even if this formatter cannot."""
    import sys

    exc_type, exc_value, tb = sys.exc_info()
    if exc_value is None:
        return _stdlib_traceback.format_exc()
    try:
        return Trace(exc_type, exc_value, tb).format(include_locals=include_locals)
    except Exception:
        try:
            return _stdlib_traceback.format_exc()
        except Exception:
            return f"{exc_type}: {exc_value}\n(traceback formatting failed)\n"
