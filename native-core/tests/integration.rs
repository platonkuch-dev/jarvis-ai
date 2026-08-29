//! End-to-end integration test: spawns the REAL compiled native-core.exe
//! and drives it over stdin/stdout exactly like agent-ts's rustBridge will,
//! against the real live desktop this test runs on (real Notepad process,
//! real Win32 calls, real UI Automation) — not mocks.
//!
//! `serial_test` keeps these single-threaded relative to each other since
//! they share real OS-level state (one Notepad window at a time).

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use serde_json::{json, Value};
use serial_test::serial;

struct Core {
    child: Child,
    stdin: std::process::ChildStdin,
    stdout: BufReader<std::process::ChildStdout>,
}

impl Core {
    fn spawn() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_native-core"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("failed to spawn native-core.exe");
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Core { child, stdin, stdout }
    }

    fn call(&mut self, id: &str, method: &str, params: Value) -> Value {
        let request = json!({ "id": id, "method": method, "params": params });
        writeln!(self.stdin, "{request}").expect("write to native-core stdin failed");
        self.stdin.flush().unwrap();

        let mut line = String::new();
        self.stdout.read_line(&mut line).expect("read from native-core stdout failed");
        serde_json::from_str(&line).unwrap_or_else(|e| panic!("bad JSON response '{line}': {e}"))
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
#[serial]
fn ping_round_trip() {
    let mut core = Core::spawn();
    let resp = core.call("1", "system.ping", json!({}));
    assert_eq!(resp["success"], true);
    assert_eq!(resp["result"]["pong"], true);
}

#[test]
#[serial]
fn malformed_request_returns_structured_error_not_a_crash() {
    let mut core = Core::spawn();
    writeln!(core.stdin, "not valid json at all").unwrap();
    core.stdin.flush().unwrap();
    let mut line = String::new();
    core.stdout.read_line(&mut line).unwrap();
    let resp: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(resp["success"], false);
    assert_eq!(resp["error"]["code"], "PARSE_ERROR");

    // The process must still be alive and answer the next well-formed request —
    // one bad line must not take down the whole IPC loop.
    let resp2 = core.call("2", "system.ping", json!({}));
    assert_eq!(resp2["success"], true);
}

#[test]
#[serial]
fn unknown_method_is_a_structured_error() {
    let mut core = Core::spawn();
    let resp = core.call("1", "no.such.method", json!({}));
    assert_eq!(resp["success"], false);
    assert_eq!(resp["error"]["code"], "UNKNOWN_METHOD");
}

#[test]
#[serial]
fn window_list_returns_real_windows() {
    let mut core = Core::spawn();
    let resp = core.call("1", "window.list", json!({}));
    assert_eq!(resp["success"], true);
    let windows = resp["result"]["windows"].as_array().unwrap();
    assert!(!windows.is_empty(), "expected at least one real window on this desktop");
}

#[test]
#[serial]
fn focus_on_missing_app_is_a_clean_not_found_not_a_silent_fallback() {
    let mut core = Core::spawn();
    let resp = core.call("1", "window.focus", json!({ "query": "ThisAppDefinitelyDoesNotExist_9f8e7d6c" }));
    assert_eq!(resp["success"], false);
    assert_eq!(resp["error"]["code"], "NOT_FOUND");
}

#[test]
#[serial]
fn full_notepad_lifecycle_launch_type_read_close() {
    let mut core = Core::spawn();

    let launch = core.call("1", "process.launch", json!({ "app_name": "Notepad", "timeout_ms": 8000 }));
    assert_eq!(launch["success"], true, "launch call itself failed: {launch}");
    assert_eq!(launch["result"]["success"], true);
    assert!(
        launch["result"]["window"].is_object(),
        "expected a real window to be found and focused after launch: {launch}"
    );

    let typed = core.call("2", "ui.type_into_element", json!({
        "query": "notepad", "element_query": "", "control_type": "Document",
        "text": "integration test marker 42", "clear_first": true
    }));
    assert_eq!(typed["success"], true, "type failed: {typed}");

    let read = core.call("3", "ui.read_element", json!({
        "query": "notepad", "element_query": "", "control_type": "Document"
    }));
    assert_eq!(read["success"], true, "read failed: {read}");
    let text = read["result"]["text"].as_str().unwrap_or("");
    assert!(text.contains("integration test marker 42"), "unexpected document text: {text}");

    let closed = core.call("4", "window.close", json!({ "query": "notepad", "force": true }));
    assert_eq!(closed["success"], true, "close failed: {closed}");

    // Give Windows a beat to actually tear the process down before the next
    // test in this file launches its own Notepad instance.
    std::thread::sleep(Duration::from_millis(500));
}

#[test]
#[serial]
fn already_running_app_is_focused_not_relaunched() {
    let mut core = Core::spawn();
    let first = core.call("1", "process.launch", json!({ "app_name": "Notepad", "timeout_ms": 8000 }));
    assert_eq!(first["result"]["already_running"], false);
    let pid1 = first["result"]["window"]["pid"].as_u64();

    let second = core.call("2", "process.launch", json!({ "app_name": "Notepad", "timeout_ms": 8000 }));
    assert_eq!(second["result"]["already_running"], true, "second launch should recognize the running instance: {second}");
    let pid2 = second["result"]["window"]["pid"].as_u64();
    assert_eq!(pid1, pid2, "should be the exact same process, not a second Notepad");

    let closed = core.call("3", "window.close", json!({ "query": "notepad", "force": true }));
    assert_eq!(closed["success"], true);
    std::thread::sleep(Duration::from_millis(500));
}

#[test]
#[serial]
fn protected_process_cannot_be_killed() {
    let mut core = Core::spawn();
    let procs = core.call("1", "process.list", json!({ "name_filter": "explorer.exe" }));
    let list = procs["result"]["processes"].as_array().unwrap();
    if let Some(explorer) = list.first() {
        let pid = explorer["pid"].as_u64().unwrap();
        let resp = core.call("2", "process.kill", json!({ "pid": pid, "force": true }));
        assert_eq!(resp["success"], false, "explorer.exe must never be killable via this API");
        assert_eq!(resp["error"]["code"], "REFUSED");
    }
}
