//! Method dispatch: maps a JSON-RPC `method` name + `params` to a system.rs
//! handler and back into a `serde_json::Value` result. This is the ONLY
//! place that decides what a caller (agent-ts) is allowed to invoke — the
//! agent never gets raw process/window handles to call arbitrary Win32
//! APIs with, only these named, parameter-validated operations.

use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};

use crate::protocol::{AppError, AppResult};
use crate::system::{process, uia, window};

fn parse<T: for<'de> Deserialize<'de>>(params: &Value) -> AppResult<T> {
    serde_json::from_value(params.clone()).map_err(|e| AppError::invalid_params(format!("Invalid params: {e}")))
}

fn window_json(w: &window::WindowInfo) -> Value {
    serde_json::to_value(w).unwrap_or(Value::Null)
}

pub fn dispatch(method: &str, params: &Value) -> AppResult<Value> {
    match method {
        "system.ping" => Ok(json!({ "pong": true, "version": env!("CARGO_PKG_VERSION") })),

        // ── process ──────────────────────────────────────────────────
        "process.list" => {
            #[derive(Deserialize, Default)]
            struct P { #[serde(default)] name_filter: String }
            let p: P = parse(params).unwrap_or_default();
            let procs = process::list_processes(Some(&p.name_filter))?;
            Ok(json!({ "processes": procs }))
        }

        "process.kill" => {
            #[derive(Deserialize)]
            struct P { pid: u32, #[serde(default)] force: bool }
            let p: P = parse(params)?;
            let _ = p.force; // Windows terminate/kill are the same primitive — see process::kill_process doc.
            let message = process::kill_process(p.pid)?;
            Ok(json!({ "message": message }))
        }

        "process.launch" => {
            #[derive(Deserialize)]
            struct P { app_name: String, #[serde(default)] timeout_ms: Option<u64> }
            let p: P = parse(params)?;
            let timeout = Duration::from_millis(p.timeout_ms.unwrap_or(8000));
            let result = process::launch_or_focus(&p.app_name, timeout)?;
            Ok(serde_json::to_value(result).unwrap())
        }

        // ── window ───────────────────────────────────────────────────
        "window.list" => {
            #[derive(Deserialize, Default)]
            struct P { #[serde(default)] query: String }
            let p: P = parse(params).unwrap_or_default();
            let windows = if p.query.trim().is_empty() { window::list_windows() } else { window::find_windows(&p.query) };
            Ok(json!({ "windows": windows }))
        }

        "window.get_active" => {
            Ok(json!({ "window": window::get_active_window() }))
        }

        "window.focus" => {
            #[derive(Deserialize)]
            struct P { query: String }
            let p: P = parse(params)?;
            let w = window::resolve_one(&p.query)?;
            let ok = window::focus_window(w.hwnd);
            Ok(json!({ "success": ok, "window": window_json(&w) }))
        }

        "window.minimize" | "window.maximize" | "window.restore" => {
            #[derive(Deserialize)]
            struct P { query: String }
            let p: P = parse(params)?;
            let w = window::resolve_one(&p.query)?;
            let ok = match method {
                "window.minimize" => window::minimize_window(w.hwnd),
                "window.maximize" => window::maximize_window(w.hwnd),
                _ => window::restore_window(w.hwnd),
            };
            Ok(json!({ "success": ok, "window": window_json(&w) }))
        }

        "window.close" => {
            #[derive(Deserialize)]
            struct P { query: String, #[serde(default)] force: bool }
            let p: P = parse(params)?;
            let w = window::resolve_one(&p.query)?;
            window::close_window(w.hwnd);
            let gone = window::wait_for_window_gone(w.hwnd, Duration::from_secs(3));
            if !gone && p.force {
                let message = process::kill_process(w.pid)?;
                return Ok(json!({ "success": true, "method": "force_kill", "message": message }));
            }
            Ok(json!({
                "success": true,
                "method": if gone { "graceful" } else { "pending" },
                "message": if gone { format!("Closed {}.", w.title) } else { format!("Asked {} to close — it may be prompting to save.", w.title) }
            }))
        }

        "window.move" => {
            #[derive(Deserialize)]
            struct P { query: String, x: Option<i32>, y: Option<i32> }
            let p: P = parse(params)?;
            let w = window::resolve_one(&p.query)?;
            window::move_resize_window(w.hwnd, p.x, p.y, None, None)?;
            Ok(json!({ "success": true, "window": window_json(&w) }))
        }

        "window.resize" => {
            #[derive(Deserialize)]
            struct P { query: String, width: Option<i32>, height: Option<i32> }
            let p: P = parse(params)?;
            let w = window::resolve_one(&p.query)?;
            window::move_resize_window(w.hwnd, None, None, p.width, p.height)?;
            Ok(json!({ "success": true, "window": window_json(&w) }))
        }

        "window.wait_for" => {
            #[derive(Deserialize)]
            struct P { query: String, #[serde(default)] timeout_ms: Option<u64> }
            let p: P = parse(params)?;
            let timeout = Duration::from_millis(p.timeout_ms.unwrap_or(10_000));
            match window::wait_for_window(&p.query, timeout) {
                Some(w) => Ok(json!({ "success": true, "window": window_json(&w) })),
                None => Err(AppError::not_found(format!("No window for '{}' appeared within {:.0}s.", p.query, timeout.as_secs_f32()))),
            }
        }

        // ── UI Automation ────────────────────────────────────────────
        "ui.find_element" | "ui.click_element" | "ui.type_into_element" | "ui.read_element"
        | "ui.wait_for_element" | "ui.clear_element" => {
            dispatch_ui(method, params)
        }

        "ui.get_tree" => {
            #[derive(Deserialize)]
            struct P {
                query: String,
                #[serde(default = "default_depth")] max_depth: u32,
                #[serde(default = "default_max_elements")] max_elements: usize,
            }
            let p: P = parse(params)?;
            let w = window::resolve_one(&p.query)?;
            let automation = uia::automation()?;
            let root = uia::element_from_hwnd(&automation, w.hwnd)?;
            let tree = uia::get_ui_tree(&automation, &root, p.max_depth, p.max_elements)?;
            Ok(json!({ "window": window_json(&w), "tree": tree }))
        }

        _ => Err(AppError::new("UNKNOWN_METHOD", format!("Unknown method '{method}'."), false)),
    }
}

fn default_depth() -> u32 { 6 }
fn default_max_elements() -> usize { 150 }

#[derive(Deserialize)]
struct UiParams {
    query: String,
    #[serde(default)]
    element_query: String,
    #[serde(default)]
    control_type: Option<String>,
    #[serde(default = "default_depth")]
    max_depth: u32,
    #[serde(default)]
    index: usize,
    #[serde(default)]
    text: Option<String>,
    #[serde(default = "default_true")]
    clear_first: bool,
    #[serde(default)]
    double: bool,
    #[serde(default)]
    right: bool,
    #[serde(default)]
    timeout_ms: Option<u64>,
}

fn default_true() -> bool { true }

fn dispatch_ui(method: &str, params: &Value) -> AppResult<Value> {
    let p: UiParams = parse(params)?;
    let w = window::resolve_one(&p.query)?;
    let automation = uia::automation()?;
    let root = uia::element_from_hwnd(&automation, w.hwnd)?;
    let ct = p.control_type.as_deref();

    match method {
        "ui.find_element" => {
            let el = uia::find_one(&automation, &root, &p.element_query, ct, p.max_depth, p.index)?;
            Ok(json!({ "element": uia::describe(&el) }))
        }
        "ui.click_element" => {
            let el = uia::find_one(&automation, &root, &p.element_query, ct, p.max_depth, p.index)?;
            uia::click_element(&el, p.double, p.right)?;
            Ok(json!({ "success": true, "element": uia::describe(&el) }))
        }
        "ui.type_into_element" => {
            let text = p.text.ok_or_else(|| AppError::invalid_params("Missing 'text'."))?;
            let el = uia::find_one(&automation, &root, &p.element_query, ct, p.max_depth, p.index)?;
            uia::type_into_element(&el, &text, p.clear_first)?;
            Ok(json!({ "success": true, "element": uia::describe(&el) }))
        }
        "ui.read_element" => {
            let el = uia::find_one(&automation, &root, &p.element_query, ct, p.max_depth, p.index)?;
            let info = uia::read_element(&el);
            let text = uia::element_text(&el);
            Ok(json!({ "element": info, "text": text }))
        }
        "ui.clear_element" => {
            let el = uia::find_one(&automation, &root, &p.element_query, ct, p.max_depth, p.index)?;
            uia::clear_element(&el)?;
            Ok(json!({ "success": true, "element": uia::describe(&el) }))
        }
        "ui.wait_for_element" => {
            let timeout = Duration::from_millis(p.timeout_ms.unwrap_or(8000));
            let el = uia::wait_for_element(&automation, &root, &p.element_query, ct, p.max_depth, timeout)?;
            Ok(json!({ "element": uia::describe(&el) }))
        }
        _ => unreachable!(),
    }
}
