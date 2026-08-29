# native-core IPC protocol

The wire contract between `agent-ts` (TypeScript orchestrator) and
`native-core` (Rust system core). Implemented independently on each side —
`native-core/src/protocol.rs` and `agent-ts/src/protocol.ts` — kept in sync
by hand; `agent-ts/tests/rustBridge.integration.test.ts` and
`native-core/tests/integration.rs` are what actually catch drift, by driving
the real binary from both directions.

## Transport

One JSON object per line (NDJSON), over the child process's stdin/stdout.
`native-core` is spawned once and kept alive for the life of the
orchestrator session — not one process per call. stderr carries diagnostics
only (`[native-core] ...` lines) and is never part of the protocol.

## Request

```json
{ "id": "1", "method": "window.list", "params": { "query": "chrome" } }
```

- `id` — caller-assigned, echoed back verbatim. Used to match responses to
  requests (`agent-ts`'s rustBridge tracks these in a pending-calls map with
  a per-call timeout).
- `method` — one of the methods listed below.
- `params` — method-specific, see `native-core/src/dispatch.rs` for the
  authoritative field list per method (it's the single source of truth for
  request shapes; this file is the human-readable index).

## Response

Success:

```json
{ "id": "1", "success": true, "result": { "windows": [ ... ] } }
```

Error:

```json
{
  "id": "1",
  "success": false,
  "error": {
    "code": "NOT_FOUND",
    "message": "No window found for 'ThisAppDoesNotExist'.",
    "source": "native-core",
    "recoverable": true
  }
}
```

`error.recoverable` tells the caller whether retrying with different
parameters could plausibly succeed (`NOT_FOUND`, `INVALID_PARAMS`) versus a
hard refusal that retrying won't fix (`REFUSED` — e.g. a protected-process
kill attempt).

## Methods

| Method                | Summary                                              |
|------------------------|------------------------------------------------------|
| `system.ping`          | Health check, no side effects.                       |
| `process.list`         | List running processes, optional name filter.        |
| `process.kill`         | Terminate a process by PID (protected names refused). |
| `process.launch`       | Smart launch-or-focus an app by name.                |
| `window.list`          | List/find windows, optional fuzzy query.              |
| `window.get_active`    | The current foreground window.                        |
| `window.focus`         | Bring a window to the foreground.                     |
| `window.minimize`      | Minimize a window.                                     |
| `window.maximize`      | Maximize a window.                                     |
| `window.restore`       | Restore a minimized/maximized window.                  |
| `window.close`         | Ask a window to close (WM_CLOSE); `force` kills the process if it won't. |
| `window.move`          | Move a window to given coordinates.                    |
| `window.resize`        | Resize a window.                                       |
| `window.wait_for`      | Poll for a window to appear (bounded, not a blind sleep). |
| `ui.find_element`      | Find a UI Automation element inside a window.          |
| `ui.click_element`     | Click/double-click/right-click a found element.        |
| `ui.type_into_element` | Type text into a found element.                        |
| `ui.read_element`      | Read a found element's text/value and properties.      |
| `ui.clear_element`     | Clear a found element's content.                       |
| `ui.get_tree`          | Bounded, depth/count-limited accessibility tree.        |
| `ui.wait_for_element`  | Poll for a UI element to appear.                       |

## Error codes

| Code | Meaning |
|---|---|
| `NOT_FOUND` | The named window/process/element doesn't exist right now. |
| `INVALID_PARAMS` | Request params failed to parse/validate. |
| `OS_ERROR` | A Win32/UI Automation call failed. |
| `REFUSED` | Deliberately refused (e.g. protected process). Not recoverable by retrying. |
| `UNKNOWN_METHOD` | No handler for that method name. |
| `PARSE_ERROR` | The request line itself wasn't valid JSON. |
| `INTERNAL_PANIC` | A handler panicked; caught, logged to stderr, process stays alive. |

`agent-ts` additionally synthesizes: `NATIVE_CORE_NOT_BUILT`,
`NATIVE_CORE_SPAWN_FAILED`, `NATIVE_CORE_EXITED`, `NATIVE_CORE_TIMEOUT`,
`NATIVE_CORE_WRITE_FAILED` for bridge-level failures that never reach Rust.
