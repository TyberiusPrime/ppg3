"""``python -m ppg3 <subcommand>`` entry point (PPG3_DESIGN.md §6.7).

Two subcommands exist so far — ``watch`` and ``webwatch`` (the same loop
with a read-only HTTP status frontend attached, see ``webwatch.py``);
session-mode template persistence (the other half of §6.7) is out of
scope for this pass (see STATUS.md). The Rust ``ppg3`` CLI binary
(``cli/``, CONTRACT.md) is a separate, unrelated entry point for
store-level operations (``gc``, ``verify``, ...); these coordinator-side
commands are Python-only and do not shell out to it.

Usage::

    python -m ppg3 watch    <pipeline.py> [--interval SECONDS] [args...]
    python -m ppg3 webwatch <pipeline.py> [--interval SECONDS]
                            [--host HOST] [--port PORT] [args...]

Both commands' own arguments are parsed by hand (see :func:`_extract_flags`)
rather than via ``argparse``'s ``REMAINDER``: the required CLI shape puts
the pipeline script *before* our own flags (``watch script.py --interval
0.1``), and ``argparse.REMAINDER`` greedily swallows every token —
including a later ``--interval`` — once it starts consuming at the first
positional, which would silently ignore the flag. Instead: scan the whole
argv for our flags (``--flag VALUE`` / ``--flag=VALUE``) wherever they
appear and strip them out; the first remaining token is the script path,
everything else (in original relative order) is forwarded to the script
unchanged as its own ``sys.argv[1:]``.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_USAGE = (
    "usage: python -m ppg3 watch <pipeline.py> [--interval SECONDS] [args...]\n"
    "       python -m ppg3 webwatch <pipeline.py> [--interval SECONDS] "
    "[--host HOST] [--port PORT] [args...]\n"
)


class _UsageError(SystemExit):
    def __init__(self, message: str):
        super().__init__(f"python -m ppg3: {message}")


def _extract_flags(
    argv: Sequence[str],
    spec: Dict[str, Callable[[str], Any]],
    command: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """Strip ``--flag VALUE`` / ``--flag=VALUE`` occurrences of the flags in
    ``spec`` (name -> value converter) out of ``argv``, wherever they
    appear. Returns (parsed values keyed by flag name, everything else in
    original relative order)."""
    values: Dict[str, Any] = {}
    remaining: List[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        name, eq, inline = tok.partition("=")
        if name in spec:
            if eq:
                raw = inline
                i += 1
            else:
                if i + 1 >= n:
                    raise _UsageError(f"{command}: {name} requires a value")
                raw = argv[i + 1]
                i += 2
            convert = spec[name]
            try:
                values[name] = convert(raw)
            except ValueError:
                raise _UsageError(
                    f"{command}: {name}: invalid {convert.__name__} {raw!r}"
                )
            continue
        remaining.append(tok)
        i += 1
    return values, remaining


def _split_script(remaining: List[str], command: str) -> Tuple[str, List[str]]:
    if not remaining:
        raise _UsageError(f"{command}: missing pipeline script path")
    return remaining[0], remaining[1:]


def _parse_watch_argv(argv: Sequence[str]) -> Tuple[str, List[str], float]:
    values, remaining = _extract_flags(argv, {"--interval": float}, "watch")
    script, script_args = _split_script(remaining, "watch")
    return script, script_args, values.get("--interval", 0.5)


def _parse_webwatch_argv(
    argv: Sequence[str],
) -> Tuple[str, List[str], float, str, int]:
    values, remaining = _extract_flags(
        argv,
        {"--interval": float, "--host": str, "--port": int},
        "webwatch",
    )
    script, script_args = _split_script(remaining, "webwatch")
    # Imported here, not at module top: `watch --help` and argv-parsing
    # tests should not pay for (or depend on) the webwatch module.
    from .webwatch import DEFAULT_HOST, DEFAULT_PORT

    return (
        script,
        script_args,
        values.get("--interval", 0.5),
        values.get("--host", DEFAULT_HOST),
        values.get("--port", DEFAULT_PORT),
    )


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(_USAGE)
        return 0 if argv and argv[0] in ("-h", "--help") else 2

    command, rest = argv[0], argv[1:]
    if command == "watch":
        from .watch import run_watch

        script, script_args, interval = _parse_watch_argv(rest)
        return run_watch(script, script_args, interval=interval)
    if command == "webwatch":
        from .webwatch import run_webwatch

        script, script_args, interval, host, port = _parse_webwatch_argv(rest)
        return run_webwatch(
            script, script_args, interval=interval, host=host, port=port
        )
    sys.stderr.write(
        f"python -m ppg3: unknown command {command!r} "
        "(only 'watch' and 'webwatch' exist)\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
