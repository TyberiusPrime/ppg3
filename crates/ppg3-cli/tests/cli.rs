//! CLI integration tests (WP5, CONTRACT.md "CLI") — drives the built
//! `ppg3` binary via `assert_cmd`/`predicates` against real stores and
//! projects built directly through `ppg3-core` (no fakes: same publish
//! protocol, same `write_generation` the Python side will eventually call).

use std::path::{Path, PathBuf};

use assert_cmd::Command;
use predicates::prelude::*;

use ppg3_core::manifest::BuiltInfo;
use ppg3_core::store::Store;
use ppg3_core::storeset::StoreSet;
use ppg3_core::views::{self, VcsInfo, ViewEntry, ViewSpec};

fn built() -> BuiltInfo {
    BuiltInfo {
        start_ms: 0,
        end_ms: 1,
        host: "test-host".to_string(),
        sandboxed: false,
        ppg3_version: "0.1.0".to_string(),
        retain_evict: false,
    }
}

fn publish(
    store: &Store,
    ik: &str,
    key_doc: &serde_json::Value,
    files: &[(&str, &[u8])],
) -> String {
    let staging = store.open_staging().unwrap();
    for (name, content) in files {
        std::fs::write(staging.path().join(name), content).unwrap();
    }
    let outcome = store.publish(staging, ik, key_doc, built(), None).unwrap();
    outcome.oh().to_string()
}

fn simple_doc(recipe: &str) -> serde_json::Value {
    serde_json::json!({
        "ppg3_key_version": 1, "job_recipe": recipe,
        "inputs": {}, "tools": {}, "env": {},
        "runtime": {"python_env": "x", "preload": [], "shim": "1"},
        "outputs_declared": ["out.txt"],
    })
}

/// Set up `<tmp>/proj/.ppg3/config.json` pointing at a single store rooted
/// at `<tmp>/store`. Returns (project root dir, .ppg3 dir, store).
fn setup_project(tmp: &Path) -> (PathBuf, PathBuf, Store) {
    let store_dir = tmp.join("store");
    let store = Store::open("main", &store_dir, false).unwrap();

    let project_root = tmp.join("proj");
    let project_dir = project_root.join(".ppg3");
    std::fs::create_dir_all(&project_dir).unwrap();

    let config = serde_json::json!({
        "stores": [
            {"name": "main", "path": store_dir.to_string_lossy(), "readonly": false}
        ]
    });
    std::fs::write(
        project_dir.join("config.json"),
        serde_json::to_vec_pretty(&config).unwrap(),
    )
    .unwrap();

    (project_root, project_dir, store)
}

fn one_entry_spec(view_path: &str, oh: String) -> ViewSpec {
    ViewSpec {
        entries: vec![ViewEntry {
            view_rel_path: view_path.to_string(),
            oh,
            path_within_entry: "out.txt".to_string(),
            store_index: 0,
        }],
    }
}

#[test]
fn generations_list_human_and_json() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, store) = setup_project(tmp.path());
    let oh = publish(
        &store,
        &"1".repeat(64),
        &simple_doc("r1"),
        &[("out.txt", b"v1")],
    );
    let stores = StoreSet::new(vec![store]);
    let n = views::write_generation(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh),
        false,
    )
    .unwrap();

    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["generations", "list"])
        .assert()
        .success()
        .stdout(predicate::str::contains(n.to_string()));

    let output = Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["--json", "generations", "list"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let arr = parsed.as_array().unwrap();
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["n"].as_u64().unwrap(), n);
    assert!(arr[0]["current"].as_bool().unwrap());
    assert_eq!(arr[0]["n_entries"].as_u64().unwrap(), 1);
}

#[test]
fn generations_command_finds_ppg3_walking_up_from_subdirectory() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, store) = setup_project(tmp.path());
    let oh = publish(
        &store,
        &"2".repeat(64),
        &simple_doc("r1"),
        &[("out.txt", b"v1")],
    );
    let stores = StoreSet::new(vec![store]);
    views::write_generation(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh),
        false,
    )
    .unwrap();

    let subdir = project_root.join("nested/deeper");
    std::fs::create_dir_all(&subdir).unwrap();

    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&subdir)
        .args(["generations", "list"])
        .assert()
        .success()
        .stdout(predicate::str::contains("1"));
}

#[test]
fn jj_add_ignores_is_idempotent_and_preserves_existing() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, _project_dir, _store) = setup_project(tmp.path());

    // Pre-existing .gitignore with an unrelated entry must be preserved.
    let gitignore = project_root.join(".gitignore");
    std::fs::write(&gitignore, "*.pyc\n").unwrap();

    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .arg("jj-add-ignores")
        .assert()
        .success()
        .stdout(predicate::str::contains("/.ppg3/"));

    let after = std::fs::read_to_string(&gitignore).unwrap();
    assert!(after.contains("*.pyc\n"), "existing entry lost");
    assert!(after.contains("/.ppg3/"));
    assert!(after.contains("/outputs"));

    // Second run adds nothing (idempotent) and says so.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .arg("jj-add-ignores")
        .assert()
        .success()
        .stdout(predicate::str::contains("already ignores"));

    // Exactly one occurrence of each entry — no duplication.
    let final_text = std::fs::read_to_string(&gitignore).unwrap();
    assert_eq!(final_text.matches("/.ppg3/").count(), 1);
    assert_eq!(final_text.matches("/outputs").count(), 1);
}

#[test]
fn materialize_exports_a_real_file_tree() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, store) = setup_project(tmp.path());
    let oh = publish(
        &store,
        &"9".repeat(64),
        &simple_doc("r1"),
        &[("out.txt", b"hello materialize")],
    );
    let stores = StoreSet::new(vec![store]);
    views::write_generation(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh),
        false,
    )
    .unwrap();

    let dest = tmp.path().join("exported");
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .arg("materialize")
        .arg(&dest)
        .assert()
        .success()
        .stdout(predicate::str::contains("materialized generation 1"));

    let out_file = dest.join("out.txt");
    assert_eq!(
        std::fs::read_to_string(&out_file).unwrap(),
        "hello materialize"
    );
    assert!(
        !std::fs::symlink_metadata(&out_file)
            .unwrap()
            .file_type()
            .is_symlink(),
        "materialized output must be a real file, not a symlink"
    );

    // A second run at the same dest is refused (operational error, code 1).
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .arg("materialize")
        .arg(&dest)
        .assert()
        .failure()
        .code(1);
}

#[test]
fn explain_reports_first_appearance_then_diff() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, store) = setup_project(tmp.path());
    let oh1 = publish(
        &store,
        &"3".repeat(64),
        &simple_doc("recipe-a"),
        &[("out.txt", b"v1")],
    );
    let stores = StoreSet::new(vec![store]);
    views::write_generation(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh1),
        false,
    )
    .unwrap();

    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["explain", "out.txt"])
        .assert()
        .success()
        .stdout(predicate::str::contains("first appearance"));

    // A second generation with a changed recipe should produce a real diff.
    let oh2 = publish(
        &stores.stores[0],
        &"4".repeat(64),
        &simple_doc("recipe-b"),
        &[("out.txt", b"v2")],
    );
    views::write_generation(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh2),
        false,
    )
    .unwrap();

    let output = Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["--json", "explain", "outputs/out.txt"]) // exercise the outputs/ prefix stripping
        .output()
        .unwrap();
    assert!(output.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(parsed["kind"], "Diff");
    assert_eq!(parsed["diff"]["recipe_changed"], true);
}

#[test]
fn rollback_default_goes_to_previous_generation() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, store) = setup_project(tmp.path());
    let oh1 = publish(
        &store,
        &"5".repeat(64),
        &simple_doc("r1"),
        &[("out.txt", b"one")],
    );
    let stores = StoreSet::new(vec![store]);
    let n1 = views::write_generation(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh1),
        false,
    )
    .unwrap();
    let oh2 = publish(
        &stores.stores[0],
        &"6".repeat(64),
        &simple_doc("r2"),
        &[("out.txt", b"two")],
    );
    views::write_generation(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh2),
        false,
    )
    .unwrap();

    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .arg("rollback")
        .assert()
        .success()
        .stdout(predicate::str::contains(n1.to_string()))
        // "how to get back": names the exact command to return to gen 2.
        .stdout(predicate::str::contains("ppg3 rollback 2"));

    assert_eq!(
        std::fs::read_to_string(project_dir.join("views/current/out.txt")).unwrap(),
        "one"
    );

    // Explicit generation argument works too.
    let cur = views::current_generation_number(&project_dir)
        .unwrap()
        .unwrap();
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["rollback", &cur.to_string()])
        .assert()
        .success();
}

#[test]
fn generations_rm_and_keep() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, store) = setup_project(tmp.path());
    let stores = StoreSet::new(vec![store]);
    let mut gens = Vec::new();
    for i in 0..4u8 {
        let oh = publish(
            &stores.stores[0],
            &format!("{i}").repeat(64),
            &simple_doc("r"),
            &[("out.txt", format!("v{i}").as_bytes())],
        );
        gens.push(
            views::write_generation(
                &project_dir,
                "proj",
                &stores,
                &one_entry_spec("out.txt", oh),
                false,
            )
            .unwrap(),
        );
    }

    // rm the oldest, non-current generation.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["generations", "rm", &gens[0].to_string()])
        .assert()
        .success();
    assert!(!project_dir.join("views").join(gens[0].to_string()).exists());

    // rm the current generation must fail with exit code 1.
    let cur = views::current_generation_number(&project_dir)
        .unwrap()
        .unwrap();
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["generations", "rm", &cur.to_string()])
        .assert()
        .failure()
        .code(1);

    // keep 1 -> drops everything except current.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["generations", "keep", "1"])
        .assert()
        .success();
    let remaining = views::list_generations(&project_dir).unwrap();
    assert_eq!(remaining.len(), 1);
    assert!(remaining[0].current);
}

#[test]
fn diff_entries_cli_reports_changed_file() {
    let tmp = tempfile::tempdir().unwrap();
    let store_dir = tmp.path().join("store");
    let store = Store::open("main", &store_dir, false).unwrap();
    let doc = serde_json::json!({"ppg3_key_version": 1});
    let oh1 = publish(&store, &"7".repeat(64), &doc, &[("a.txt", b"hello world")]);
    let oh2 = publish(&store, &"8".repeat(64), &doc, &[("a.txt", b"hello WORLD")]);

    Command::cargo_bin("ppg3")
        .unwrap()
        .args(["diff-entries", &oh1, &oh2, "--store"])
        .arg(&store_dir)
        .assert()
        .success()
        .stdout(predicate::str::contains("a.txt"));

    let output = Command::cargo_bin("ppg3")
        .unwrap()
        .args(["--json", "diff-entries", &oh1, &oh2, "--store"])
        .arg(&store_dir)
        .output()
        .unwrap();
    assert!(output.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(parsed["changed"][0]["path"], "a.txt");
    assert_eq!(parsed["changed"][0]["first_diff_offset"], 6);
}

#[test]
fn diff_entries_diff_flag_shows_line_level_content() {
    let tmp = tempfile::tempdir().unwrap();
    let store_dir = tmp.path().join("store");
    let store = Store::open("main", &store_dir, false).unwrap();
    let doc = serde_json::json!({"ppg3_key_version": 1});
    let oh1 = publish(
        &store,
        &"7".repeat(64),
        &doc,
        &[("out.txt", b"alpha\nbeta\ngamma\n"), ("gone.txt", b"old\n")],
    );
    let oh2 = publish(
        &store,
        &"8".repeat(64),
        &doc,
        &[
            ("out.txt", b"alpha\nBETA\ngamma\n"),
            ("added.txt", b"fresh\n"),
        ],
    );

    let out = Command::cargo_bin("ppg3")
        .unwrap()
        .args(["diff-entries", &oh1, &oh2, "--diff", "--store"])
        .arg(&store_dir)
        .output()
        .unwrap();
    assert!(out.status.success());
    let text = String::from_utf8(out.stdout).unwrap();
    // Changed file: the single altered line shows as -/+ around context.
    assert!(text.contains("-beta"), "missing removed line:\n{text}");
    assert!(text.contains("+BETA"), "missing added line:\n{text}");
    assert!(text.contains(" alpha"), "missing context line:\n{text}");
    // Added and removed whole files show their content one-sided.
    assert!(text.contains("+fresh"), "missing added-file body:\n{text}");
    assert!(
        text.contains("  - gone.txt"),
        "missing removed-file entry:\n{text}"
    );
    assert!(text.contains("-old"), "missing removed-file body:\n{text}");

    // Without --diff the content lines are absent (summary only).
    let plain = Command::cargo_bin("ppg3")
        .unwrap()
        .args(["diff-entries", &oh1, &oh2, "--store"])
        .arg(&store_dir)
        .output()
        .unwrap();
    let plain_text = String::from_utf8(plain.stdout).unwrap();
    assert!(
        !plain_text.contains("+BETA"),
        "content leaked without --diff"
    );
}

#[test]
fn store_verify_succeeds_then_fails_after_corruption() {
    let tmp = tempfile::tempdir().unwrap();
    let store_dir = tmp.path().join("store");
    let store = Store::open("main", &store_dir, false).unwrap();
    let doc = serde_json::json!({"ppg3_key_version": 1});
    let oh = publish(
        &store,
        &"9".repeat(64),
        &doc,
        &[("a.txt", b"pristine content")],
    );

    Command::cargo_bin("ppg3")
        .unwrap()
        .args(["store", "verify", "--entry", &oh, "--store"])
        .arg(&store_dir)
        .assert()
        .success()
        .stdout(predicate::str::contains("OK"));

    // Corrupt the published (read-only) file: chmod +w, then edit it in
    // place, so verify_entry's rehash no longer matches the manifest.
    let data_file = store.data_dir(&oh).join("a.txt");
    let mut perms = std::fs::metadata(&data_file).unwrap().permissions();
    std::os::unix::fs::PermissionsExt::set_mode(&mut perms, 0o644);
    std::fs::set_permissions(&data_file, perms).unwrap();
    std::fs::write(&data_file, b"TAMPERED CONTENT").unwrap();

    Command::cargo_bin("ppg3")
        .unwrap()
        .args(["store", "verify", "--entry", &oh, "--store"])
        .arg(&store_dir)
        .assert()
        .failure()
        .code(1)
        .stdout(predicate::str::contains("FAIL"))
        // P9.4: a failing entry is named by its on-disk path, never only
        // its hash — the entry dir is what the user opens next.
        .stdout(predicate::str::contains(
            store.entry_dir(&oh).display().to_string(),
        ));
}

#[test]
fn store_verify_sample_and_entry_are_mutually_exclusive_usage_error() {
    let tmp = tempfile::tempdir().unwrap();
    let store_dir = tmp.path().join("store");
    Store::open("main", &store_dir, false).unwrap();

    Command::cargo_bin("ppg3")
        .unwrap()
        .args([
            "store",
            "verify",
            "--sample",
            "10",
            "--entry",
            &"a".repeat(64),
            "--store",
        ])
        .arg(&store_dir)
        .assert()
        .failure()
        .code(2);
}

#[test]
fn store_gc_end_to_end_sweeps_unrooted_unpinned_entry() {
    let tmp = tempfile::tempdir().unwrap();
    let store_dir = tmp.path().join("store");
    let store = Store::open("main", &store_dir, false).unwrap();
    let doc = serde_json::json!({"ppg3_key_version": 1});

    let oh_rooted = publish(&store, &"a".repeat(64), &doc, &[("a.txt", b"rooted")]);
    let oh_pinned = publish(&store, &"b".repeat(64), &doc, &[("b.txt", b"pinned")]);
    let oh_orphan = publish(&store, &"c".repeat(64), &doc, &[("c.txt", b"orphan")]);

    // Root entry 1 via a real generation.
    let project_dir = tmp.path().join("proj/.ppg3");
    let stores = StoreSet::new(vec![store]);
    let spec = ViewSpec {
        entries: vec![ViewEntry {
            view_rel_path: "out.txt".to_string(),
            oh: oh_rooted.clone(),
            path_within_entry: "a.txt".to_string(),
            store_index: 0,
        }],
    };
    views::write_generation(&project_dir, "proj", &stores, &spec, false).unwrap();

    // Pin entry 2.
    stores.stores[0].pin("keepme", &oh_pinned).unwrap();

    // `gc()` only budget-sweeps unrooted/unpinned entries when `max_size`
    // is set and currently exceeded (WP1's gc.rs module doc / STATUS.md
    // deviation); pass a tiny budget so the lone orphan entry gets swept.
    // Rooted/pinned entries are excluded from the sweep candidate set
    // entirely, independent of budget, so they survive regardless.
    let output = Command::cargo_bin("ppg3")
        .unwrap()
        .args(["--json", "store", "gc", "--max-size", "1", "--store"])
        .arg(&store_dir)
        .output()
        .unwrap();
    assert!(output.status.success(), "gc failed: {output:?}");
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let removed: Vec<String> = parsed["removed_entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert_eq!(removed, vec![oh_orphan.clone()]);

    assert!(!store_dir.join("v1/entries").join(&oh_orphan).exists());
    assert!(store_dir.join("v1/entries").join(&oh_rooted).exists());
    assert!(store_dir.join("v1/entries").join(&oh_pinned).exists());
}

#[test]
fn project_gc_two_phases_drop_oplog_generation_then_sweep_freed_entry() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, store) = setup_project(tmp.path());

    let oh_old = publish(
        &store,
        &"1".repeat(64),
        &simple_doc("r1"),
        &[("out.txt", b"old-oplog")],
    );
    let oh_cur = publish(
        &store,
        &"2".repeat(64),
        &simple_doc("r2"),
        &[("out.txt", b"current")],
    );
    let stores = StoreSet::new(vec![store]);

    // Old generation from a dirty jj working copy (op-log bucket)...
    let vcs_dirty = VcsInfo {
        backend: "jj".to_string(),
        commit_id: "c".repeat(40),
        change_id: "oldchangeid".to_string(),
        op_id: "o".repeat(64),
        committed: false,
        parent_commit_id: None,
    };
    let n_old = views::write_generation_with_vcs(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh_old.clone()),
        false,
        Some(vcs_dirty),
    )
    .unwrap();
    // ...then a committed current generation.
    let vcs_clean = VcsInfo {
        backend: "jj".to_string(),
        commit_id: "d".repeat(40),
        change_id: "curchangeid".to_string(),
        op_id: "p".repeat(64),
        committed: true,
        parent_commit_id: Some("e".repeat(40)),
    };
    let n_cur = views::write_generation_with_vcs(
        &project_dir,
        "proj",
        &stores,
        &one_entry_spec("out.txt", oh_cur.clone()),
        false,
        Some(vcs_clean),
    )
    .unwrap();

    // The human `generations list` shows the committed/op-log distinction.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["generations", "list"])
        .assert()
        .success()
        .stdout(predicate::str::contains("op-log"))
        .stdout(predicate::str::contains("committed"))
        .stdout(predicate::str::contains("oldchangeid"));

    // Dry run first: reports the drop + would-be sweep, changes nothing.
    let output = Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args([
            "--json",
            "gc",
            "--keep",
            "5",
            "--keep-oplog",
            "0",
            "--max-size",
            "1",
            "--dry-run",
        ])
        .output()
        .unwrap();
    assert!(output.status.success(), "gc --dry-run failed: {output:?}");
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        parsed["generations"]["dropped_oplog"],
        serde_json::json!([n_old])
    );
    assert!(views::list_generations(&project_dir)
        .unwrap()
        .iter()
        .any(|g| g.n == n_old));

    // Real run: phase 1 drops the op-log generation, phase 2 sweeps the
    // entry its root had been keeping alive.
    let output = Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args([
            "--json",
            "gc",
            "--keep",
            "5",
            "--keep-oplog",
            "0",
            "--max-size",
            "1",
        ])
        .output()
        .unwrap();
    assert!(output.status.success(), "gc failed: {output:?}");
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        parsed["generations"]["dropped_oplog"],
        serde_json::json!([n_old])
    );
    assert_eq!(
        parsed["generations"]["dropped_committed"],
        serde_json::json!([])
    );
    assert_eq!(parsed["generations"]["kept"], serde_json::json!([n_cur]));
    let removed: Vec<String> = parsed["stores"]["main"]["removed_entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert_eq!(removed, vec![oh_old.clone()]);

    let store_dir = tmp.path().join("store");
    assert!(!store_dir.join("v1/entries").join(&oh_old).exists());
    assert!(store_dir.join("v1/entries").join(&oh_cur).exists());
    assert!(!project_dir.join("views").join(n_old.to_string()).exists());
    assert!(project_dir.join("views").join(n_cur.to_string()).exists());
}

#[test]
fn unknown_subcommand_is_a_usage_error() {
    Command::cargo_bin("ppg3")
        .unwrap()
        .arg("not-a-real-command")
        .assert()
        .failure()
        .code(2);
}

#[test]
fn why_answers_from_last_run_json() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, project_dir, _store) = setup_project(tmp.path());

    let last_run = serde_json::json!({
        "schema": 1,
        "created_at_ms": 1,
        "project_id": "p",
        "generation": null,
        "partial_dir": null,
        "jobs": [
            {
                "label": "ok.txt",
                "kind": "command",
                "defsite": "/proj/pipeline.py:10",
                "outputs": {"ok": "ok.txt"},
                "status": "built",
                "entry": "/store/v1/entries/abc/data"
            },
            {
                "label": "bad.txt",
                "kind": "command",
                "defsite": "/proj/pipeline.py:20",
                "outputs": {"bad": "bad.txt"},
                "status": "failed",
                "reason": "job \"x\" exited with code 7",
                "exit_code": 7,
                "failure_log": "/store/v1/logs/ik/failure.log"
            },
            {
                "label": "results/leaf.txt",
                "kind": "command",
                "defsite": "/proj/pipeline.py:30",
                "outputs": {"leaf": "results/leaf.txt"},
                "status": "not_run",
                "upstream": "bad.txt",
                "reason": "did not run — upstream job bad.txt failed"
            }
        ]
    });
    std::fs::write(
        project_dir.join("last_run.json"),
        serde_json::to_vec_pretty(&last_run).unwrap(),
    )
    .unwrap();

    // "why is this file missing" — a cascaded job resolves to its root
    // cause, with both definition sites and the root's log path.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["why", "outputs/results/leaf.txt"])
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "did not run — upstream job bad.txt failed",
        ))
        .stdout(predicate::str::contains("/proj/pipeline.py:30"))
        .stdout(predicate::str::contains("root cause: bad.txt"))
        .stdout(predicate::str::contains("/proj/pipeline.py:20"))
        .stdout(predicate::str::contains("/store/v1/logs/ik/failure.log"))
        .stdout(predicate::str::contains("in output tree: no"));

    // Suffix match: the bare filename finds results/leaf.txt.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["why", "leaf.txt"])
        .assert()
        .success()
        .stdout(predicate::str::contains("results/leaf.txt"));

    // A failed job's own story names its reason/log.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["why", "bad.txt"])
        .assert()
        .success()
        .stdout(predicate::str::contains("FAILED"))
        .stdout(predicate::str::contains("exited with code 7"));

    // No path: one status line per job.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["why"])
        .assert()
        .success()
        .stdout(predicate::str::contains("built"))
        .stdout(predicate::str::contains(
            "did not run (upstream bad.txt failed)",
        ));

    // Unknown path: says so, exit 1, offers close matches.
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["why", "nope/leaf.txt"])
        .assert()
        .code(1)
        .stdout(predicate::str::contains("no job in the last run publishes"))
        .stdout(predicate::str::contains("results/leaf.txt"));
}

#[test]
fn why_without_last_run_points_at_running_the_pipeline() {
    let tmp = tempfile::tempdir().unwrap();
    let (project_root, _project_dir, _store) = setup_project(tmp.path());
    Command::cargo_bin("ppg3")
        .unwrap()
        .current_dir(&project_root)
        .args(["why", "x.txt"])
        .assert()
        .code(1)
        .stderr(predicate::str::contains("no last-run record"))
        .stderr(predicate::str::contains("ppg3.run()"));
}
