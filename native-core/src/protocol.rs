//! The IPC wire format: line-delimited JSON-RPC-style messages over stdio.
//!
//! One JSON object per line, both directions:
//!
//!   request:  {"id": "...", "method": "window.list", "params": {}}
//!   response: {"id": "...", "success": true, "result": {...}}
//!   error:    {"id": "...", "success": false,
//!              "error": {"code": "...", "message": "...", "source": "...",
//!                        "details": {...}, "recoverable": true}}
//!
//! Mirrored in TypeScript by agent-ts/src/rustBridge.ts and documented in
//! shared/schemas/ipc-protocol.md — all three must stay in sync.

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Deserialize)]
pub struct Request {
    pub id: String,
    pub method: String,
    #[serde(default = "default_params")]
    pub params: Value,
}

fn default_params() -> Value {
    Value::Object(serde_json::Map::new())
}

#[derive(Debug, Serialize)]
pub struct Response {
    pub id: String,
    pub success: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorInfo>,
}

#[derive(Debug, Serialize)]
pub struct ErrorInfo {
    pub code: String,
    pub message: String,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
    pub recoverable: bool,
}

impl Response {
    pub fn ok(id: String, result: Value) -> Self {
        Response { id, success: true, result: Some(result), error: None }
    }

    pub fn err(id: String, code: &str, message: impl Into<String>, recoverable: bool) -> Self {
        Response {
            id,
            success: false,
            result: None,
            error: Some(ErrorInfo {
                code: code.to_string(),
                message: message.into(),
                source: "native-core".to_string(),
                details: None,
                recoverable,
            }),
        }
    }
}

/// Application-level error used throughout native-core. Every fallible
/// operation returns `AppError` instead of a bare String, so callers never
/// have to guess whether a failure is safe to retry (recoverable) or not.
#[derive(Debug)]
pub struct AppError {
    pub code: &'static str,
    pub message: String,
    pub recoverable: bool,
}

impl AppError {
    pub fn new(code: &'static str, message: impl Into<String>, recoverable: bool) -> Self {
        AppError { code, message: message.into(), recoverable }
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        AppError::new("NOT_FOUND", message, true)
    }

    pub fn invalid_params(message: impl Into<String>) -> Self {
        AppError::new("INVALID_PARAMS", message, true)
    }

    pub fn os_error(message: impl Into<String>) -> Self {
        AppError::new("OS_ERROR", message, true)
    }

    pub fn refused(message: impl Into<String>) -> Self {
        AppError::new("REFUSED", message, false)
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

pub type AppResult<T> = Result<T, AppError>;
