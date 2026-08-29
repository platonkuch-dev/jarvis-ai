//! native-core — JARVIS's Rust system core.
//!
//! Reads one line-delimited JSON request per line from stdin, dispatches it
//! (dispatch.rs), writes one line-delimited JSON response per line to
//! stdout, flushing after every response so the parent process (agent-ts)
//! never blocks waiting on a buffered pipe. Diagnostics/panics go to
//! stderr, never stdout — stdout is reserved for the protocol.

mod dispatch;
mod protocol;
mod system;

use std::io::{self, BufRead, Write};

use protocol::{Request, Response};

fn main() {
    // A panic in one request handler must not take down the whole process —
    // every other in-flight/future request would silently hang forever on
    // the agent-ts side. Convert panics into a proper INTERNAL_ERROR response.
    std::panic::set_hook(Box::new(|info| {
        eprintln!("[native-core] panic: {info}");
    }));

    // Pre-warm UI Automation's COM/IUIAutomation interface in the
    // background — the first real UIAutomation::new() + element resolution
    // in a process measurably costs more than every call after it (COM
    // interface creation), so pay that here instead of on the first actual
    // ui.* request from agent-ts. Runs on its own thread so it never delays
    // "ready" or blocks the stdin loop from accepting requests immediately.
    std::thread::spawn(|| {
        if let Some(active) = system::window::get_active_window() {
            if let Ok(automation) = system::uia::automation() {
                let _ = system::uia::element_from_hwnd(&automation, active.hwnd);
            }
        }
    });

    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = stdout.lock();

    eprintln!("[native-core] ready");

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                eprintln!("[native-core] stdin read error: {e}");
                break;
            }
        };
        // Trim a leading BOM defensively — some client environments (seen
        // from a .NET StreamWriter's first write to a fresh pipe) prepend
        // one on the very first line even when UTF-8-without-BOM was asked
        // for, and a stray BOM would otherwise break every request on that
        // line with an opaque "expected value at line 1 column 1" error.
        let line = line.trim().trim_start_matches('\u{feff}');
        if line.is_empty() {
            continue;
        }

        let response = handle_line(line);
        let serialized = serde_json::to_string(&response).unwrap_or_else(|e| {
            format!(r#"{{"id":"unknown","success":false,"error":{{"code":"SERIALIZE_ERROR","message":"{e}","source":"native-core","recoverable":false}}}}"#)
        });

        if writeln!(out, "{serialized}").is_err() || out.flush().is_err() {
            eprintln!("[native-core] stdout write failed — parent process likely gone, exiting.");
            break;
        }
    }

    eprintln!("[native-core] stdin closed, shutting down");
}

fn handle_line(line: &str) -> Response {
    let request: Request = match serde_json::from_str(line) {
        Ok(r) => r,
        Err(e) => {
            return Response::err("unknown".to_string(), "PARSE_ERROR", format!("Malformed request: {e}"), false);
        }
    };

    let id = request.id.clone();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        dispatch::dispatch(&request.method, &request.params)
    }));

    match result {
        Ok(Ok(value)) => Response::ok(id, value),
        Ok(Err(app_err)) => Response::err(id, app_err.code, app_err.message, app_err.recoverable),
        Err(_) => Response::err(id, "INTERNAL_PANIC", "native-core handler panicked — see stderr.", true),
    }
}
