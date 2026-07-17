//! Shared error type for ppg3-core. WP agents: extend variants as needed,
//! never stringly-type protocol errors (DeterminismViolation, CorruptStore
//! must stay structured).

use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("io error at {path:?}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("canonical json: {0}")]
    Canon(String),
    #[error("corrupt store: {0}")]
    CorruptStore(String),
    // The report carries the ik together with its on-disk memo-link path
    // (PRINCIPLES.md P9.4: no bare hash without an accompanying path), so
    // the headline doesn't repeat it bare.
    #[error("determinism violation (same inputs produced different outputs):\n{report}")]
    DeterminismViolation { ik: String, report: String },
    #[error("store is read-only: {0}")]
    ReadOnlyStore(String),
    #[error("graph error: {0}")]
    Graph(String),
    #[error("job failed: {0}")]
    JobFailed(String),
    #[error("{0}")]
    Other(String),
}

impl Error {
    pub fn io(path: impl Into<PathBuf>, source: std::io::Error) -> Self {
        Error::Io { path: path.into(), source }
    }
}
