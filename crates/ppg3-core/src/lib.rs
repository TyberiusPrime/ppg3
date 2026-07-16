//! ppg3-core: store, keys, scheduler, executors, views.
//!
//! Boundary rules (PPG3_DESIGN.md §8.1): everything crossing into this crate
//! is canonical JSON or raw bytes; no Python semantics live here.

pub mod canon;
pub mod error;
pub mod executor;
pub mod explain;
pub mod forkserver;
pub mod gc;
pub mod hash;
pub mod lease;
pub mod manifest;
pub mod resources;
pub mod sandbox;
pub mod scheduler;
pub mod store;
pub mod storeset;
pub mod views;

pub use error::Error;

/// Crate-wide result type. The error is an [`error_stack::Report`] so *every*
/// error — not just executor failures — accretes a `file:line` context chain
/// (each `?` boundary adds a frame) and an optional backtrace, all the way
/// out to `scheduler::run` / the PyO3 boundary, which render it. Construct
/// errors with `Report::new(Error::…)` (or let `?` lift a plain [`Error`] via
/// its blanket `From`); add context with `.attach(…)` / `.change_context(…)`.
pub type Result<T> = std::result::Result<T, error_stack::Report<Error>>;

pub const STORE_LAYOUT_VERSION: &str = "v1";
pub const KEY_VERSION: u64 = 1;
pub const MANIFEST_VERSION: u64 = 1;
