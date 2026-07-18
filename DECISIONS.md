# ppg3 decision log

Decisions we made and don't want to re-litigate. Append-only; newest last.
Each entry: date, decision, and enough context that a future reader (or agent)
can tell whether the premises still hold. If a premise changes, add a new
entry that supersedes the old one — don't edit history.

## 2026-07-18 — No landlock executor; bwrap stays the only real sandbox

**Decision:** We will not build a landlock-based executor as an alternative to
the bwrap one.

**Context:** Landlock looked attractive because it needs no external binary
and works where user namespaces are blocked (e.g. many CI containers, where
bwrap fails). Kernel ≥ 5.13 has it near-universally, and both a Rust crate
(`landlock`, maintained by the landlock project) and a Python package
(`landlock`, ctypes-based, dev-release only) exist.

**Why not:** Landlock cannot restrict network access to *nothing*. As of
ABI v4+ it can only restrict TCP `bind`/`connect`; UDP, ICMP, raw sockets and
other families remain unrestricted (abstract unix sockets/signals got scoping
in ABI v6, but that doesn't close the gap). A sandbox whose network story is
"TCP mostly blocked, everything else open" is not a sandbox tier we want to
offer — it invites false confidence. bwrap's empty network namespace gives
actual no-network; that's the bar.

**Premise to re-check:** if a future landlock ABI gains full-socket/socket-
creation restriction (enough to express "no network at all"), this decision
should be revisited — the "works without bwrap and without userns" niche is
real.
