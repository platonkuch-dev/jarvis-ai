"""Talks to a loaded CEP extension's debug port using the Chrome DevTools
Protocol (the same mechanism Chrome's own remote-debugging inspector uses --
CEP's extension frame is just an embedded CEF/Chromium page). Once connected,
`window.__adobe_cep__.evalScript()` is CEP's native bridge from that page's
JS into the host application's own ExtendScript engine -- this is how
Premiere Pro / After Effects get scripted from outside the app.

Requires installer.ensure_installed() to have run at least once, and the
target app to have loaded the extension (panel is AutoVisible, so this
happens automatically -- but only picked up on app (re)start after a fresh
install).
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import websockets
import websockets.exceptions


class BridgeConnectionError(RuntimeError):
    """The CEP debug port isn't reachable, the extension panel hasn't been
    loaded by the host app yet, or the app stopped responding -- distinct
    from an ExtendScript-side scripting error (bad comp/layer name, bad
    expression, ...), so callers can show a "is AE running?" hint only when
    it's actually a connectivity problem instead of on every failure."""


async def wait_for_port(port: int, timeout: float = 25.0) -> bool:
    """Polls the debug port's discovery endpoint until it responds with at
    least one debuggable target, or times out. Use after launching/focusing
    the host app, since the extension needs a moment to load."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as http:
        while time.monotonic() < deadline:
            try:
                r = await http.get(f"http://127.0.0.1:{port}/json", timeout=2.0)
                if r.status_code == 200 and r.json():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


async def _debug_target_ws_url(port: int) -> str:
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"http://127.0.0.1:{port}/json", timeout=5.0)
            resp.raise_for_status()
            targets = resp.json()
    except httpx.HTTPError as exc:
        raise BridgeConnectionError(
            f"Не удалось достучаться до отладочного порта CEP {port}: {exc}"
        ) from exc
    if not targets:
        raise BridgeConnectionError(
            "Нет доступных целей отладки CEP -- расширение Jarvis Bridge ещё не "
            "загружено этим приложением (перезапустите его после установки моста)."
        )
    target = next((t for t in targets if "index.html" in t.get("url", "")), targets[0])
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BridgeConnectionError("У цели отладки CEP нет webSocketDebuggerUrl.")
    return ws_url


async def eval_script(port: int, jsx_code: str, timeout: float = 20.0) -> str:
    """Runs `jsx_code` as ExtendScript inside the host app connected to
    `port` and returns its result as a string (whatever the last expression
    in the script evaluates to, ExtendScript-side -- have the script
    JSON.stringify(...) its own return value if you need structured data
    back). Raises BridgeConnectionError if the app/panel isn't reachable, or
    RuntimeError with the ExtendScript error text if the script itself
    failed."""
    ws_url = await _debug_target_ws_url(port)

    # window.__adobe_cep__.evalScript(script, callback) is fire-and-forget
    # from CEP's side -- wrapping it in a Promise lets Runtime.evaluate's
    # awaitPromise actually wait for and return the callback's value.
    escaped = json.dumps(jsx_code)
    expression = (
        "new Promise(function(resolve) {"
        f"  try {{ window.__adobe_cep__.evalScript({escaped}, resolve); }}"
        "  catch (e) { resolve('JARVIS_BRIDGE_ERROR: ' + e); }"
        "});"
    )

    request_id = 1
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            await ws.send(json.dumps({
                "id": request_id,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
            }))
            # The debug socket can carry unrelated CDP traffic (console
            # events, target-info notifications) ahead of our actual reply
            # -- taking whatever recv() returns first (as this used to)
            # silently parsed the wrong message on occasion (verified live:
            # an early event has no "result" key, so this read back an empty
            # value as if the script had succeeded with nothing). Loop until
            # a message carrying our request id shows up, honoring the
            # overall timeout across every skipped event, not just the last.
            deadline = time.monotonic() + timeout
            msg = None
            while msg is None or msg.get("id") != request_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeConnectionError(
                        f"Тайм-аут ({timeout:.0f}с) ожидания ответа от After Effects -- "
                        "скрипт выполняется слишком долго или завис на модальном диалоге."
                    )
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
    except asyncio.TimeoutError as exc:
        raise BridgeConnectionError(
            f"Тайм-аут ({timeout:.0f}с) ожидания ответа от After Effects -- "
            "скрипт выполняется слишком долго или завис на модальном диалоге."
        ) from exc
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        raise BridgeConnectionError(f"Соединение с After Effects оборвалось: {exc}") from exc

    result = msg.get("result", {})
    if "exceptionDetails" in result:
        raise RuntimeError(f"Ошибка CDP: {result['exceptionDetails']}")
    value = result.get("result", {}).get("value")
    if isinstance(value, str) and value.startswith("JARVIS_BRIDGE_ERROR:"):
        raise RuntimeError(value)
    if isinstance(value, str) and value.startswith("EvalScript error"):
        raise RuntimeError(f"Ошибка ExtendScript: {value}")
    return value if value is not None else ""
