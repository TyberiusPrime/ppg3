//! Project discovery + `.ppg3/config.json` (WP5 CLI-only addition —
//! documented additively in `../../CONTRACT.md` and `../../STATUS.md`).
//!
//! Nothing in `ppg3-core` needs to know about `config.json`: it exists only
//! so the standalone CLI (which has no Python `ppg3.new(stores=...)` call
//! to hand it a ready-made `StoreSet`) can reconstruct one from disk. The
//! shape is intentionally minimal:
//!
//! ```json
//! {"stores": [{"name": "main", "path": "/abs/path/to/store", "readonly": false}]}
//! ```
//!
//! `run.py` (python side, WP7) is expected to write this file next to the
//! rest of `.ppg3/` when a project is created/configured.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use ppg3_core::store::Store;
use ppg3_core::storeset::StoreSet;

use crate::AppError;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoreConfigEntry {
    pub name: String,
    pub path: PathBuf,
    #[serde(default)]
    pub readonly: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProjectConfig {
    #[serde(default)]
    pub stores: Vec<StoreConfigEntry>,
}

/// Walk up from `start` looking for a `.ppg3` directory.
pub fn find_project_dir(start: &Path) -> Option<PathBuf> {
    let mut cur = Some(start.to_path_buf());
    while let Some(dir) = cur {
        let candidate = dir.join(".ppg3");
        if candidate.is_dir() {
            return Some(candidate);
        }
        cur = dir.parent().map(Path::to_path_buf);
    }
    None
}

/// Resolve the effective project directory (the `.ppg3` dir itself):
/// `--project PATH` overrides discovery. `PATH` may point either at the
/// project root (containing `.ppg3`) or directly at the `.ppg3` directory.
/// Without an override, walk up from the current directory.
pub fn resolve_project_dir(explicit: Option<&Path>) -> Result<PathBuf, AppError> {
    if let Some(p) = explicit {
        let candidate = p.join(".ppg3");
        if candidate.is_dir() {
            return Ok(candidate);
        }
        if p.is_dir() {
            return Ok(p.to_path_buf());
        }
        return Err(AppError::Usage(format!(
            "--project {p:?} is neither a project root (containing .ppg3) nor a .ppg3 directory"
        )));
    }
    let cwd = std::env::current_dir().map_err(|e| AppError::Io(e.to_string()))?;
    find_project_dir(&cwd).ok_or_else(|| {
        AppError::Operational(format!(
            "no .ppg3/ found walking up from {cwd:?}; pass --project to override"
        ))
    })
}

/// Resolve a single store path for the store-level commands (`store gc`,
/// `store nuke`, `store verify`, `diff-entries`). An explicit `--store`
/// always wins; otherwise we walk up from the current directory for a
/// project and, if its `config.json` lists exactly one store, use that.
/// Zero or many configured stores is an error telling the user to pass
/// `--store` — we never guess which of several stores they meant.
pub fn resolve_single_store(explicit: Option<&Path>) -> Result<PathBuf, AppError> {
    if let Some(p) = explicit {
        return Ok(p.to_path_buf());
    }
    let cwd = std::env::current_dir().map_err(|e| AppError::Io(e.to_string()))?;
    let project_dir = find_project_dir(&cwd).ok_or_else(|| {
        AppError::Usage(format!(
            "no --store given and no .ppg3/ found walking up from {cwd:?}; pass --store PATH"
        ))
    })?;
    let config_path = project_dir.join("config.json");
    let bytes = std::fs::read(&config_path)
        .map_err(|e| AppError::Operational(format!("reading {config_path:?}: {e}")))?;
    let config: ProjectConfig = serde_json::from_slice(&bytes)
        .map_err(|e| AppError::Operational(format!("parsing {config_path:?}: {e}")))?;
    match config.stores.as_slice() {
        [only] => Ok(only.path.clone()),
        [] => Err(AppError::Usage(format!(
            "{config_path:?} configures no stores; pass --store PATH"
        ))),
        many => {
            let names: Vec<&str> = many.iter().map(|s| s.name.as_str()).collect();
            Err(AppError::Usage(format!(
                "{} stores configured ({}); pass --store PATH to pick one",
                many.len(),
                names.join(", ")
            )))
        }
    }
}

/// Load `<project_dir>/config.json` and open every configured store.
pub fn load_storeset(project_dir: &Path) -> Result<StoreSet, AppError> {
    let config_path = project_dir.join("config.json");
    let bytes = std::fs::read(&config_path)
        .map_err(|e| AppError::Operational(format!("reading {config_path:?}: {e}")))?;
    let config: ProjectConfig = serde_json::from_slice(&bytes)
        .map_err(|e| AppError::Operational(format!("parsing {config_path:?}: {e}")))?;
    let mut stores = Vec::with_capacity(config.stores.len());
    for sc in &config.stores {
        let store = Store::open(&sc.name, &sc.path, sc.readonly)
            .map_err(|e| AppError::Core(e.to_string()))?;
        stores.push(store);
    }
    Ok(StoreSet::new(stores))
}
