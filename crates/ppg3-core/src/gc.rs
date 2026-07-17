//! Mark/sweep GC (WP1, PPG3_DESIGN.md §11/§11.2, CONTRACT.md "GC").
//!
//! Exclusive `gc.lock` for the whole run (publish only ever takes it
//! shared, so publish and GC can never interleave between an entry's
//! rename and its `inputs/<ik>` symlink creation — PPG3_DESIGN.md §11).
//!
//! ## Levels (§11.2)
//!
//! GC is driven by a single ordered [`GcLevel`], each level a superset of
//! the one below. Everything the level dial controls is derived from it in
//! [`run`]; `max_size`/`min_age_ms`/`dry_run` are orthogonal knobs the
//! caller layers on top.
//!
//! - **debris** (all levels): stale `staging/` build dirs (owner pid dead on
//!   this host, or idle past a generous cross-host window), stale
//!   `leases/`/`intents/` files, and `staging/violations/` records.
//! - **orphan logs** (all levels): `logs/<ik>/` with no live `inputs/<ik>`
//!   entry behind it, guarded by mtime staleness so a running build's log
//!   dir is never swept. A sweep that removes an entry also removes that
//!   entry's `logs/<ik>/` directly (the entry was complete → no guard).
//! - **evict-marked + dangling** (`minimal`+): `retain=Evict` unrooted
//!   entries and dangling `inputs/` links.
//! - **unrooted normal entries**: `default` only under `max_size` (LRU by
//!   `.atime`); `aggressive`/`reset` sweep every unrooted entry.
//! - **pins**: cleared by `reset`.
//!
//! Generation dropping is *not* here — it is the project-level phase 1
//! (`views::remove_old_generations`) the CLI runs before calling `gc` at the
//! same level (§11.2a).

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::error::Error;
use crate::lease::{self, INTENT_STALE_AFTER, LEASE_STALE_AFTER};
use crate::store::{self, GcLockGuard, Store};

/// Orphan-log directories and cross-host staging dirs are only reclaimed once
/// idle this long — long enough that a running build (which is not otherwise
/// GC-protected before it publishes) is never clobbered.
const STAGING_CROSS_HOST_STALE_AFTER: Duration = Duration::from_secs(24 * 60 * 60);
/// `staging/violations/` forensic records past this age are debris.
const VIOLATION_STALE_AFTER: Duration = Duration::from_secs(24 * 60 * 60);

/// The GC policy dial (§11.2). Each variant is a superset of the ones above
/// it. `PartialOrd`/`Ord` follow declaration order, so `level >= GcLevel::X`
/// reads as "at least as aggressive as X".
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Default, Serialize, Deserialize)]
pub enum GcLevel {
    /// Debris + orphan logs only. Post-mortem cleanup of a bad run; touches
    /// no live content.
    FailedOnly,
    /// `failed-only` + evict-marked entries + dangling input links.
    Minimal,
    /// `minimal` + budget-driven LRU sweep of unrooted normal entries (only
    /// with `max_size`). The constructive-trace default: an unrooted entry is
    /// cache, kept until space pressure.
    #[default]
    Default,
    /// `default` but sweep **every** unrooted normal entry, budget or not.
    Aggressive,
    /// `aggressive` + clear all pins. Combined with the project-level "drop
    /// all but the current generation" phase, leaves the store holding
    /// exactly the current output tree's closure.
    Reset,
}

#[derive(Debug, Clone, Default)]
pub struct GcPolicy {
    pub level: GcLevel,
    /// Byte budget for the `default`-level LRU sweep (and log budget
    /// eviction). Ignored by `aggressive`/`reset`, which sweep unconditionally.
    pub max_size: Option<u64>,
    /// Grace window (ms) for the `default`-level budget sweep: unrooted
    /// entries touched more recently than this are kept even when over budget,
    /// until nothing older is left to reclaim. `None` = no grace.
    pub min_age_ms: Option<i64>,
    /// Allow the `default`-level budget path to evict `logs/` before entries.
    pub evict_logs: bool,
    pub dry_run: bool,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct GcReport {
    pub removed_entries: Vec<String>,
    pub removed_logs: Vec<String>,
    pub removed_dangling_inputs: Vec<String>,
    /// Stale `staging/<host>-<pid>-<rand>/` build dirs reclaimed.
    pub removed_staging: Vec<String>,
    /// Stale `leases/*.json` files reclaimed.
    pub removed_leases: Vec<String>,
    /// Stale `intents/*.json` files reclaimed.
    pub removed_intents: Vec<String>,
    /// `staging/violations/*` records reclaimed.
    pub removed_violations: Vec<String>,
    /// Pins cleared (`reset` only).
    pub removed_pins: Vec<String>,
    pub bytes_freed: u64,
    pub remaining_size: u64,
    pub dry_run: bool,
}

fn now() -> SystemTime {
    SystemTime::now()
}

/// True if `path`'s mtime is older than `dur`. Missing/unreadable → treated
/// as *not* stale (conservative: never sweep something we can't stat).
fn idle_past(path: &Path, dur: Duration) -> bool {
    match std::fs::metadata(path).and_then(|m| m.modified()) {
        Ok(mtime) => now().duration_since(mtime).unwrap_or_default() > dur,
        Err(_) => false,
    }
}

fn this_hostname() -> String {
    std::env::var("HOSTNAME")
        .or_else(|_| std::env::var("HOST"))
        .unwrap_or_else(|_| "host".to_string())
}

/// Parse a staging dir name `<host>-<pid>-<rand>` from the right (`rand` and
/// `pid` never contain `-`; the host may). Returns `(host, pid)`.
fn parse_staging_name(name: &str) -> Option<(String, u32)> {
    let mut parts = name.rsplitn(3, '-');
    let _rand = parts.next()?;
    let pid: u32 = parts.next()?.parse().ok()?;
    let host = parts.next()?.to_string();
    Some((host, pid))
}

/// Linux pid-liveness via `/proc/<pid>`. `true` = a process with this pid
/// currently exists (so the staging dir might be a live build → keep).
fn pid_alive(pid: u32) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}

pub fn run(store: &Store, policy: &GcPolicy) -> Result<GcReport, Error> {
    let _lock = GcLockGuard::lock_exclusive(&store.gc_lock_path())?;
    let mut report = GcReport {
        dry_run: policy.dry_run,
        ..Default::default()
    };

    // --- Phase A: debris (all levels) -----------------------------------
    clean_stale_staging(store, policy, &mut report)?;
    clean_stale_lease_files(store, policy, &mut report)?;
    clean_stale_intent_files(store, policy, &mut report)?;
    clean_violations(store, policy, &mut report)?;

    // `reset` clears pins before we compute roots, so pinned-but-otherwise-
    // unreferenced entries fall to the unrooted sweep below.
    if policy.level >= GcLevel::Reset {
        clear_pins(store, policy, &mut report)?;
    }

    // --- Roots ----------------------------------------------------------
    let mut rooted: HashSet<String> = store::collect_symlinked_ohs(&store.roots_dir())?;
    rooted.extend(store::collect_symlinked_ohs(&store.pins_dir())?);
    rooted.extend(lease::live_protected_ohs(&store.leases_dir())?);

    // --- Phase B: dangling inputs (minimal+) ----------------------------
    if policy.level >= GcLevel::Minimal {
        for name in store::list_dir_names(&store.inputs_dir())? {
            let link_path = store.inputs_dir().join(&name);
            let meta = match std::fs::symlink_metadata(&link_path) {
                Ok(m) => m,
                Err(_) => continue,
            };
            if !meta.file_type().is_symlink() {
                continue;
            }
            let target = std::fs::read_link(&link_path).map_err(|e| Error::io(&link_path, e))?;
            let resolved = link_path.parent().expect("inputs dir").join(&target);
            if !resolved.exists() {
                report.removed_dangling_inputs.push(name);
                if !policy.dry_run {
                    std::fs::remove_file(&link_path).map_err(|e| Error::io(&link_path, e))?;
                }
            }
        }
    }

    // --- Phase C/D: entry sweeps ----------------------------------------
    let all_ohs = store::list_dir_names(&store.entries_dir())?;
    let mut candidates: Vec<String> = all_ohs
        .into_iter()
        .filter(|oh| !rooted.contains(oh))
        .collect();

    // C. evict-marked (minimal+): always swept, independent of budget.
    if policy.level >= GcLevel::Minimal {
        let mut evict_marked: Vec<String> = candidates
            .iter()
            .filter(|oh| store::entry_has_evict_marker(&store.entry_dir(oh)))
            .cloned()
            .collect();
        evict_marked.sort();
        for oh in &evict_marked {
            report.bytes_freed += store::dir_size(&store.entry_dir(oh));
            report.removed_entries.push(oh.clone());
            if !policy.dry_run {
                remove_entry_and_logs(store, oh, &mut report)?;
            }
        }
        let evict_set: HashSet<&String> = evict_marked.iter().collect();
        candidates.retain(|oh| !evict_set.contains(oh));
    }

    // D. unrooted normal entries.
    match policy.level {
        GcLevel::FailedOnly | GcLevel::Minimal => {}
        GcLevel::Default => {
            let total_size = total_store_size(store).saturating_sub(report.bytes_freed);
            default_budget_sweep(store, policy, &candidates, total_size, &mut report)?;
        }
        GcLevel::Aggressive | GcLevel::Reset => {
            candidates.sort();
            for oh in &candidates {
                report.bytes_freed += store::dir_size(&store.entry_dir(oh));
                report.removed_entries.push(oh.clone());
                if !policy.dry_run {
                    remove_entry_and_logs(store, oh, &mut report)?;
                }
            }
        }
    }

    // --- Phase E: orphan logs (all levels) ------------------------------
    sweep_orphan_logs(store, policy, &mut report)?;

    // `bytes_freed` counts everything reclaimed (entries, logs, staging); so
    // `remaining` is the current bulk minus that in dry-run (nothing actually
    // removed yet), or simply the current bulk after a real run.
    report.remaining_size = bulk_store_size(store).saturating_sub(if policy.dry_run {
        report.bytes_freed
    } else {
        0
    });
    Ok(report)
}

/// The `default`-level budget sweep: evict logs first (if allowed), then LRU
/// unrooted entries, honoring the `min_age_ms` grace, until under `max_size`.
/// A no-op without `max_size` (constructive-trace default: unrooted == cache).
fn default_budget_sweep(
    store: &Store,
    policy: &GcPolicy,
    candidates: &[String],
    mut total_size: u64,
    report: &mut GcReport,
) -> Result<(), Error> {
    let Some(budget) = policy.max_size else {
        return Ok(());
    };

    if total_size > budget && policy.evict_logs {
        let mut log_dirs = collect_log_leaf_dirs(store)?;
        log_dirs.sort_by_key(|(_, mtime)| *mtime);
        for (dir, _mtime) in log_dirs {
            if total_size <= budget {
                break;
            }
            let size = store::dir_size(&dir);
            total_size = total_size.saturating_sub(size);
            report.bytes_freed += size;
            report.removed_logs.push(log_rel(store, &dir));
            if !policy.dry_run {
                std::fs::remove_dir_all(&dir).map_err(|e| Error::io(&dir, e))?;
            }
        }
    }

    if total_size > budget {
        let now_ms = now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as i64;
        let mut lru: Vec<(String, i64)> = candidates
            .iter()
            .map(|oh| (oh.clone(), store::entry_atime_ms(&store.entry_dir(oh))))
            .collect();
        lru.sort_by_key(|(_, atime)| *atime);
        for (oh, atime) in lru {
            if total_size <= budget {
                break;
            }
            // Grace: keep entries touched within min_age_ms — unless there is
            // nothing older to reclaim, in which case budget wins.
            if let Some(min_age) = policy.min_age_ms {
                if now_ms.saturating_sub(atime) < min_age {
                    continue;
                }
            }
            let size = store::dir_size(&store.entry_dir(&oh));
            total_size = total_size.saturating_sub(size);
            report.bytes_freed += size;
            report.removed_entries.push(oh.clone());
            if !policy.dry_run {
                remove_entry_and_logs(store, &oh, report)?;
            }
        }
    }
    Ok(())
}

/// Remove an entry and, since a published entry's build is by definition
/// complete, its `logs/<ik>/` directory too (read `ik` from the manifest
/// before the entry is gone).
fn remove_entry_and_logs(store: &Store, oh: &str, report: &mut GcReport) -> Result<(), Error> {
    if let Some(ik) = entry_input_key(store, oh) {
        let log_dir = store.logs_dir().join(&ik);
        if log_dir.is_dir() {
            report.removed_logs.push(ik);
            std::fs::remove_dir_all(&log_dir).map_err(|e| Error::io(&log_dir, e))?;
        }
    }
    remove_entry(store, oh)
}

fn entry_input_key(store: &Store, oh: &str) -> Option<String> {
    let manifest_path = store.entry_dir(oh).join("manifest.json");
    let bytes = std::fs::read(&manifest_path).ok()?;
    let v: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    v.get("input_key")?.as_str().map(|s| s.to_string())
}

fn remove_entry(store: &Store, oh: &str) -> Result<(), Error> {
    let entry_dir = store.entry_dir(oh);
    // Payload was made read-only at publish time (§4); chmod +w before
    // deleting (CONTRACT.md: "deleting read-only entries requires chmod +w
    // first").
    store::chmod_writable_recursive(&entry_dir)?;
    std::fs::remove_dir_all(&entry_dir).map_err(|e| Error::io(&entry_dir, e))
}

/// Remove any `logs/<ik>/` with no live entry behind it (failed/abandoned
/// builds, or entries a prior GC removed), guarded by mtime staleness so a
/// build that is still running — its log dir created, entry not yet published
/// — is never clobbered. Runs at every level; the entry sweeps above already
/// removed the logs of entries *they* deleted, so this catches the rest.
fn sweep_orphan_logs(store: &Store, policy: &GcPolicy, report: &mut GcReport) -> Result<(), Error> {
    let logs_dir = store.logs_dir();
    for ik in store::list_dir_names(&logs_dir)? {
        let ik_log_dir = logs_dir.join(&ik);
        if !ik_log_dir.is_dir() {
            continue;
        }
        let link_path = store.inputs_dir().join(&ik);
        let has_live_entry = match std::fs::read_link(&link_path) {
            Ok(target) => link_path.parent().expect("inputs dir").join(&target).exists(),
            Err(_) => false,
        };
        if has_live_entry {
            continue;
        }
        if !idle_past(&ik_log_dir, LEASE_STALE_AFTER) {
            continue; // a build may still be writing here
        }
        report.removed_logs.push(ik.clone());
        report.bytes_freed += store::dir_size(&ik_log_dir);
        if !policy.dry_run {
            std::fs::remove_dir_all(&ik_log_dir).map_err(|e| Error::io(&ik_log_dir, e))?;
        }
    }
    Ok(())
}

fn clean_stale_staging(
    store: &Store,
    policy: &GcPolicy,
    report: &mut GcReport,
) -> Result<(), Error> {
    let staging_dir = store.staging_dir();
    let host = this_hostname();
    for name in store::list_dir_names(&staging_dir)? {
        if name == "violations" {
            continue; // handled separately
        }
        let dir = staging_dir.join(&name);
        if !dir.is_dir() {
            continue;
        }
        let stale = match parse_staging_name(&name) {
            // Same host: exact pid liveness. A dead pid → the build that owned
            // this dir is gone. A live pid → keep (conservative; pid reuse can
            // only ever make us keep, never falsely delete).
            Some((h, pid)) if h == host => !pid_alive(pid),
            // Cross-host or unparseable: liveness unknowable → idle window.
            _ => idle_past(&dir, STAGING_CROSS_HOST_STALE_AFTER),
        };
        if stale {
            report.removed_staging.push(name);
            report.bytes_freed += store::dir_size(&dir);
            if !policy.dry_run {
                store::chmod_writable_recursive(&dir).ok();
                std::fs::remove_dir_all(&dir).map_err(|e| Error::io(&dir, e))?;
            }
        }
    }
    Ok(())
}

fn clean_stale_lease_files(
    store: &Store,
    policy: &GcPolicy,
    report: &mut GcReport,
) -> Result<(), Error> {
    clean_stale_json(&store.leases_dir(), LEASE_STALE_AFTER, policy, &mut report.removed_leases)
}

fn clean_stale_intent_files(
    store: &Store,
    policy: &GcPolicy,
    report: &mut GcReport,
) -> Result<(), Error> {
    clean_stale_json(
        &store.intents_dir(),
        INTENT_STALE_AFTER,
        policy,
        &mut report.removed_intents,
    )
}

fn clean_stale_json(
    dir: &Path,
    stale_after: Duration,
    policy: &GcPolicy,
    out: &mut Vec<String>,
) -> Result<(), Error> {
    for name in store::list_dir_names(dir)? {
        let path = dir.join(&name);
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        if idle_past(&path, stale_after) {
            out.push(name);
            if !policy.dry_run {
                std::fs::remove_file(&path).map_err(|e| Error::io(&path, e))?;
            }
        }
    }
    Ok(())
}

fn clean_violations(store: &Store, policy: &GcPolicy, report: &mut GcReport) -> Result<(), Error> {
    let violations_dir = store.staging_dir().join("violations");
    // `reset`/`aggressive` clear all records; lighter levels only the stale
    // ones (forensics of a run you may still be investigating stay put).
    let clear_all = policy.level >= GcLevel::Aggressive;
    for name in store::list_dir_names(&violations_dir)? {
        let path = violations_dir.join(&name);
        if clear_all || idle_past(&path, VIOLATION_STALE_AFTER) {
            report.removed_violations.push(name);
            if !policy.dry_run {
                if path.is_dir() {
                    std::fs::remove_dir_all(&path).map_err(|e| Error::io(&path, e))?;
                } else {
                    std::fs::remove_file(&path).map_err(|e| Error::io(&path, e))?;
                }
            }
        }
    }
    Ok(())
}

fn clear_pins(store: &Store, policy: &GcPolicy, report: &mut GcReport) -> Result<(), Error> {
    let pins_dir = store.pins_dir();
    for name in store::list_dir_names(&pins_dir)? {
        report.removed_pins.push(name.clone());
        if !policy.dry_run {
            let path = pins_dir.join(&name);
            std::fs::remove_file(&path).map_err(|e| Error::io(&path, e))?;
        }
    }
    Ok(())
}

/// Entries + logs — the denominator for the `default`-level `max_size`
/// budget (transient debris like `staging/` is deliberately excluded so the
/// budget bounds *retained* content, not build scratch).
fn total_store_size(store: &Store) -> u64 {
    store::dir_size(&store.entries_dir()) + store::dir_size(&store.logs_dir())
}

/// Everything GC can reclaim (entries + logs + staging) — the basis for
/// `GcReport.remaining_size`, so the reported figure reflects staging cleanup
/// too, not just the budgeted content.
fn bulk_store_size(store: &Store) -> u64 {
    total_store_size(store) + store::dir_size(&store.staging_dir())
}

fn log_rel(store: &Store, dir: &Path) -> String {
    dir.strip_prefix(store.logs_dir())
        .unwrap_or(dir)
        .to_string_lossy()
        .to_string()
}

fn collect_log_leaf_dirs(store: &Store) -> Result<Vec<(PathBuf, SystemTime)>, Error> {
    let mut out = Vec::new();
    let logs_dir = store.logs_dir();
    for ik in store::list_dir_names(&logs_dir)? {
        let ik_dir = logs_dir.join(&ik);
        for ts_host in store::list_dir_names(&ik_dir)? {
            let leaf = ik_dir.join(&ts_host);
            let mtime = std::fs::metadata(&leaf)
                .and_then(|m| m.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            out.push((leaf, mtime));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::BuiltInfo;
    use crate::store::PublishOutcome;

    fn sample_built(retain_evict: bool) -> BuiltInfo {
        BuiltInfo {
            start_ms: 0,
            end_ms: 1,
            host: "test".to_string(),
            sandboxed: false,
            ppg3_version: "0.1.0".to_string(),
            retain_evict,
        }
    }

    fn publish(store: &Store, ik: &str, bytes: &[u8], retain_evict: bool) -> PublishOutcome {
        let staging = store.open_staging().unwrap();
        std::fs::write(staging.path().join("out.txt"), bytes).unwrap();
        store
            .publish(
                staging,
                ik,
                &serde_json::json!({"k": ik}),
                sample_built(retain_evict),
                None,
            )
            .unwrap()
    }

    fn default_policy(max_size: Option<u64>) -> GcPolicy {
        GcPolicy {
            max_size,
            ..Default::default()
        }
    }

    #[test]
    fn unrooted_entry_is_swept_under_budget() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let o = publish(&store, &"1".repeat(64), b"x", false);
        let report = store.gc(&default_policy(Some(0))).unwrap();
        assert!(report.removed_entries.contains(&o.oh().to_string()));
        assert!(!store.entry_dir(o.oh()).exists());
    }

    #[test]
    fn default_without_budget_keeps_unrooted_entry() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let o = publish(&store, &"1".repeat(64), b"cache-me", false);
        // Constructive-trace default: unrooted == cache, kept without pressure.
        let report = store.gc(&default_policy(None)).unwrap();
        assert!(store.entry_dir(o.oh()).exists());
        assert!(report.removed_entries.is_empty());
    }

    #[test]
    fn aggressive_sweeps_unrooted_without_budget() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let o = publish(&store, &"1".repeat(64), b"garbage", false);
        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Aggressive,
                ..Default::default()
            })
            .unwrap();
        assert!(report.removed_entries.contains(&o.oh().to_string()));
        assert!(!store.entry_dir(o.oh()).exists());
    }

    #[test]
    fn rooted_pinned_leased_entries_are_kept_even_aggressive() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let rooted = publish(&store, &"1".repeat(64), b"rooted", false);
        let pinned = publish(&store, &"2".repeat(64), b"pinned", false);
        let leased = publish(&store, &"3".repeat(64), b"leased", false);
        let unrooted = publish(&store, &"4".repeat(64), b"gone", false);

        store.add_root("proj", 1, &[rooted.oh().to_string()]).unwrap();
        store.pin("release", pinned.oh()).unwrap();
        let lease = store.lease("run1").unwrap();
        lease.protect(leased.oh()).unwrap();

        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Aggressive,
                ..Default::default()
            })
            .unwrap();

        assert!(store.entry_dir(rooted.oh()).exists());
        assert!(store.entry_dir(pinned.oh()).exists(), "pins survive aggressive");
        assert!(store.entry_dir(leased.oh()).exists());
        assert!(!store.entry_dir(unrooted.oh()).exists());
        assert!(report.removed_entries.contains(&unrooted.oh().to_string()));

        drop(lease);
    }

    #[test]
    fn reset_clears_pins_and_sweeps_the_formerly_pinned_entry() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let pinned = publish(&store, &"2".repeat(64), b"pinned", false);
        store.pin("release", pinned.oh()).unwrap();

        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Reset,
                ..Default::default()
            })
            .unwrap();

        assert!(report.removed_pins.contains(&"release".to_string()));
        assert!(!store.pins_dir().join("release").exists());
        assert!(
            !store.entry_dir(pinned.oh()).exists(),
            "reset drops pins then sweeps the now-unrooted entry"
        );
    }

    #[test]
    fn evict_marked_needs_minimal_not_failed_only() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let evictable = publish(&store, &"1".repeat(64), b"temp", true);

        // failed-only leaves even evict-marked entries alone.
        let report = store
            .gc(&GcPolicy {
                level: GcLevel::FailedOnly,
                ..Default::default()
            })
            .unwrap();
        assert!(store.entry_dir(evictable.oh()).exists());
        assert!(report.removed_entries.is_empty());

        // minimal sweeps them, no budget needed.
        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Minimal,
                ..Default::default()
            })
            .unwrap();
        assert!(!store.entry_dir(evictable.oh()).exists());
        assert!(report.removed_entries.contains(&evictable.oh().to_string()));
    }

    #[test]
    fn dangling_input_symlink_is_removed_at_minimal() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let o = publish(&store, &"1".repeat(64), b"x", false);
        store::chmod_writable_recursive(&store.entry_dir(o.oh())).unwrap();
        std::fs::remove_dir_all(store.entry_dir(o.oh())).unwrap();

        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Minimal,
                ..Default::default()
            })
            .unwrap();
        assert!(report.removed_dangling_inputs.contains(&"1".repeat(64)));
        assert!(!store.inputs_dir().join("1".repeat(64)).exists());
    }

    #[test]
    fn sweeping_an_entry_removes_its_logs() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let ik = "1".repeat(64);
        let o = publish(&store, &ik, b"x", false);
        // Simulate the build having written a log for this ik.
        let log_leaf = store.log_dir_for(&ik).unwrap();
        std::fs::write(log_leaf.join("stdout.txt"), b"hi").unwrap();
        assert!(store.logs_dir().join(&ik).is_dir());

        store
            .gc(&GcPolicy {
                level: GcLevel::Aggressive,
                ..Default::default()
            })
            .unwrap();
        assert!(!store.entry_dir(o.oh()).exists());
        assert!(
            !store.logs_dir().join(&ik).exists(),
            "an entry's logs go with the entry"
        );
    }

    #[test]
    fn orphan_logs_of_failed_builds_are_swept_when_stale() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        // A log dir for an ik that never produced an entry (failed build).
        let ik = "f".repeat(64);
        let log_leaf = store.log_dir_for(&ik).unwrap();
        std::fs::write(log_leaf.join("failure.log"), b"boom").unwrap();
        // Age the log dir past the staleness window.
        let old = SystemTime::now() - Duration::from_secs(3600);
        set_mtime(&store.logs_dir().join(&ik), old);

        let report = store
            .gc(&GcPolicy {
                level: GcLevel::FailedOnly,
                ..Default::default()
            })
            .unwrap();
        assert!(report.removed_logs.contains(&ik));
        assert!(!store.logs_dir().join(&ik).exists());
    }

    #[test]
    fn fresh_orphan_logs_are_kept_build_may_be_running() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let ik = "f".repeat(64);
        let log_leaf = store.log_dir_for(&ik).unwrap();
        std::fs::write(log_leaf.join("stdout.txt"), b"...").unwrap();
        // No aging: the log dir is fresh — an in-progress build could own it.
        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Aggressive,
                ..Default::default()
            })
            .unwrap();
        assert!(!report.removed_logs.contains(&ik));
        assert!(store.logs_dir().join(&ik).is_dir());
    }

    #[test]
    fn stale_lease_and_intent_files_are_reclaimed() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let lease = store.lease("crashed").unwrap();
        let lease_path = lease.path().to_path_buf();
        std::mem::forget(lease); // simulate a crashed run: file left behind
        let intent = store.write_intent(&"a".repeat(64)).unwrap();
        let intent_path = intent.path().to_path_buf();

        let old = SystemTime::now() - Duration::from_secs(3600);
        set_mtime(&lease_path, old);
        set_mtime(&intent_path, old);

        let report = store.gc(&default_policy(None)).unwrap();
        assert!(!report.removed_leases.is_empty());
        assert!(!report.removed_intents.is_empty());
        assert!(!lease_path.exists());
        assert!(!intent_path.exists());
    }

    #[test]
    fn fresh_lease_is_not_reclaimed() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let lease = store.lease("live").unwrap();
        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Reset,
                ..Default::default()
            })
            .unwrap();
        assert!(report.removed_leases.is_empty());
        assert!(lease.path().exists());
        drop(lease);
    }

    #[test]
    fn stale_cross_host_staging_is_reclaimed() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        // A staging dir from another host — pid liveness unknowable, so only
        // the idle window applies.
        let staging = store.staging_dir().join("otherhost-99999-deadbeef");
        std::fs::create_dir_all(staging.join("data")).unwrap();
        std::fs::write(staging.join("data").join("partial"), b"x").unwrap();
        let old = SystemTime::now() - Duration::from_secs(48 * 60 * 60);
        set_mtime(&staging, old);

        let report = store.gc(&default_policy(None)).unwrap();
        assert!(report
            .removed_staging
            .contains(&"otherhost-99999-deadbeef".to_string()));
        assert!(!staging.exists());
    }

    #[test]
    fn same_host_dead_pid_staging_is_reclaimed() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let host = this_hostname();
        // pid 2^31-ish: never a live process.
        let name = format!("{host}-2147480000-cafef00d");
        let staging = store.staging_dir().join(&name);
        std::fs::create_dir_all(staging.join("data")).unwrap();
        let report = store.gc(&default_policy(None)).unwrap();
        assert!(report.removed_staging.contains(&name));
        assert!(!staging.exists());
    }

    #[test]
    fn live_open_staging_is_not_reclaimed() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        // A staging dir named with *our* pid → looks alive → kept.
        let staging = store.open_staging().unwrap();
        let name = staging
            .root()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .to_string();
        let report = store.gc(&default_policy(None)).unwrap();
        assert!(!report.removed_staging.contains(&name));
        assert!(staging.root().is_dir());
    }

    #[test]
    fn dry_run_reports_without_deleting() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let o = publish(&store, &"1".repeat(64), b"x", false);
        let report = store
            .gc(&GcPolicy {
                level: GcLevel::Aggressive,
                dry_run: true,
                ..Default::default()
            })
            .unwrap();
        assert!(report.removed_entries.contains(&o.oh().to_string()));
        assert!(
            store.entry_dir(o.oh()).exists(),
            "dry_run must not delete anything"
        );
    }

    #[test]
    fn logs_evicted_before_entries_under_budget() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let rooted = publish(&store, &"1".repeat(64), b"keep-this-content-rooted", false);
        store.add_root("proj", 1, &[rooted.oh().to_string()]).unwrap();

        // Log for the *rooted* ik, so it is not an orphan (won't be swept by
        // the orphan pass) — only budget eviction can remove it.
        let log_dir = store.log_dir_for(&"1".repeat(64)).unwrap();
        std::fs::write(log_dir.join("stdout.txt"), vec![0u8; 4096]).unwrap();

        let entries_size = store::dir_size(&store.entries_dir());
        let total = entries_size + store::dir_size(&store.logs_dir());
        let budget = entries_size;
        assert!(total > budget);

        let report = store
            .gc(&GcPolicy {
                max_size: Some(budget),
                evict_logs: true,
                ..Default::default()
            })
            .unwrap();

        assert!(store.entry_dir(rooted.oh()).exists(), "rooted entry survives");
        assert!(!log_dir.exists(), "logs evicted to make budget");
        assert!(!report.removed_logs.is_empty());
        assert!(report.removed_entries.is_empty());
    }

    #[test]
    fn lru_order_respected_under_budget() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let old = publish(&store, &"1".repeat(64), b"old-entry-content", false);
        std::thread::sleep(std::time::Duration::from_millis(5));
        let newer = publish(&store, &"2".repeat(64), b"newer-entry-content", false);
        std::thread::sleep(std::time::Duration::from_millis(5));
        store.lookup(&"2".repeat(64)).unwrap();

        let one_entry_size = store::dir_size(&store.entry_dir(newer.oh()));
        let report = store.gc(&default_policy(Some(one_entry_size))).unwrap();

        assert!(!store.entry_dir(old.oh()).exists(), "older evicted first");
        assert!(store.entry_dir(newer.oh()).exists(), "recently-hit survives");
        assert!(report.removed_entries.contains(&old.oh().to_string()));
    }

    #[test]
    fn min_age_grace_protects_recent_entries_from_budget_sweep() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open("s", dir.path(), false).unwrap();
        let o = publish(&store, &"1".repeat(64), b"recent", false);
        // Over budget, but a huge grace window keeps the just-built entry.
        let report = store
            .gc(&GcPolicy {
                max_size: Some(0),
                min_age_ms: Some(1_000_000),
                ..Default::default()
            })
            .unwrap();
        assert!(store.entry_dir(o.oh()).exists());
        assert!(report.removed_entries.is_empty());
    }

    fn set_mtime(path: &Path, time: SystemTime) {
        let ft = std::fs::FileTimes::new().set_modified(time);
        // Directories can't be opened write-only; open read for the handle.
        let file = std::fs::File::open(path).unwrap();
        file.set_times(ft).unwrap();
    }
}
