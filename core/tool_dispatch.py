"""
Tool dispatcher for JarvisLive: maps a Gemini Live function-call to the
matching actions/* handler and returns a FunctionResponse.

Split out of main.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior change from the move itself -- this was previously a JarvisLive
method in main.py. `execute_tool` takes the JarvisLive instance as `self`,
exactly as when it was a bound method; JarvisLive keeps a thin wrapper
method of the same name (minus the module qualifier) so every call site
elsewhere is unchanged.
"""
from __future__ import annotations

import asyncio
import threading
import time
import traceback

from google.genai import types

from core import latency, tool_registry
from core.assistant_state import get_state, record_activity
from memory.memory_manager import update_memory
from memory.pattern_learning import log_tool_call

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.scheduled_send    import schedule_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen, capture_screen_full
from actions.screen_watch      import screen_watch, get_latest_frame as _get_watched_frame
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.computer_agent    import computer_agent
from actions.game_updater      import game_updater
from actions.system_monitor    import get_system_status
from actions.system_scan       import system_scan
from actions.system_diagnostics import system_diagnostics
from actions.process_hunter    import find_suspicious_processes
from actions.hacker_terminal   import run_terminal_command
from actions.digital_ghost     import digital_ghost
from actions.jarvis_control    import jarvis_control
from actions.window_control    import window_manager, ui_automation, launch_and_verify
from actions.self_extend       import add_capability

# core/tool_registry.py's GATED_LEVELS is a deliberate, project-wide no-op
# (see its own docstring) -- nothing there is gated today. hacker_terminal
# and add_capability used to be force-gated locally here regardless of that
# shared switch (arbitrary shell / self-written code being the two things
# with the widest blast radius in the app) -- explicitly turned off at the
# user's request (2026-09-03): they want every action, including these,
# to run on the first request with no confirmation step. Left as a plain
# `set`, not removed outright, so core/custom_tools.py's register_tool()
# still has somewhere to add a future self-authored tool's own name if its
# generated risk assessment calls for staying gated (a user asking JARVIS
# to write itself a new DANGEROUS tool is a separate decision from this
# one, made per-tool by Claude at generation time -- not overridden here).
_LOCALLY_GATED_TOOLS: set[str] = set()

# Handlers for self-authored tools (actions/self_extend.py's add_capability
# writes these; core/custom_tools.py registers them here both live and on
# every startup reload). Unlike every built-in tool above, these aren't
# known at import time -- dispatched generically below instead of getting
# their own elif branch.
_CUSTOM_TOOL_HANDLERS: dict = {}


async def execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        # Peek, don't pop: actions/computer_settings.py already has its own
        # independent confirmed=yes check for restart/shutdown (predates this
        # registry) -- popping the key here would make it invisible to that
        # handler, so a confirmed retry would pass THIS gate but then hit
        # computer_settings.py's own check with an empty confirmed value and
        # ask again, forever. Leaving it in `args` makes computer_settings.py's
        # check a harmless redundant one instead of a broken dead end. Every
        # other handler already ignores parameters it doesn't recognize.
        confirmed = bool(args.get("confirmed", False))
        _t_start = time.monotonic()

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")
        log_tool_call(name, args)
        capability = {
            "browser_control": "browser", "file_controller": "files", "file_processor": "files",
            "send_message": "messaging", "send_screenshot": "messaging",
            "computer_agent": "screen",
        }.get(name)
        if name == "screen_process":
            capability = "camera" if args.get("angle", "screen").lower() == "camera" else "screen"
        if capability and not get_state()["privacy"].get(capability, True):
            result = f"The {capability} permission is blocked in JARVIS privacy settings."
            record_activity(name, result, "blocked")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(id=fc.id, name=name, response={"result": result})
        record_activity(name, str(args), "started")

        # Risk gate: SENSITIVE/DANGEROUS actions (per core/tool_registry.py's
        # classification, itself grounded in AUDIT.md's Stage-1 findings) do
        # not run on the first recognized word -- the caller must re-invoke
        # the exact same tool with confirmed=true after the user has
        # explicitly agreed. This runs before the save_memory short-circuit
        # too (save_memory is NORMAL/ungated, so it's a no-op for that path).
        risk = tool_registry.resolve_risk(name, args)
        if (risk in tool_registry.GATED_LEVELS or name in _LOCALLY_GATED_TOOLS) and not confirmed:
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            latency.record(name, (time.monotonic() - _t_start) * 1000)
            prompt = tool_registry.confirmation_prompt(name, args, risk)
            print(f"[JARVIS] 🛑 {name} gated ({risk.name}) — awaiting confirmation")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": prompt}
            )

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            latency.record(name, (time.monotonic() - _t_start) * 1000)
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: launch_and_verify(args.get("app_name", "")))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                platform    = (args.get("platform") or "").strip().lower()
                is_telegram = platform in ("telegram", "tg")
                if args.get("date") and args.get("time"):
                    r = await loop.run_in_executor(None, lambda: schedule_message(parameters=args, player=self.ui))
                elif is_telegram and args.get("voice") and self._telegram and self._telegram.client:
                    r = await self._telegram.send_voice_to(args.get("receiver", ""), args.get("message_text", ""))
                elif is_telegram and self._telegram and self._telegram.client:
                    r = await self._telegram.send_to(args.get("receiver", ""), args.get("message_text", ""))
                else:
                    r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "send_screenshot":
                if not (self._telegram and self._telegram_reply_target is not None):
                    result = "I can only send a screenshot to a Telegram chat — this request didn't come from Telegram."
                else:
                    target = self._telegram_reply_target
                    img_b = await loop.run_in_executor(None, capture_screen_full)
                    await self._telegram.send_photo(target, img_b, filename="screenshot.png")
                    result = "Screenshot sent to the chat."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        watched = _get_watched_frame()
                        if watched is not None:
                            img_b, mime_t = watched
                            print(f"[Vision] 🖥️  Screen (from watch cache): {len(img_b):,} bytes")
                        else:
                            img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                            print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                        # Request came in via Telegram → also deliver the actual
                        # screenshot file to that chat, not just a spoken description.
                        if self._telegram and self._telegram_reply_target is not None:
                            asyncio.create_task(
                                self._telegram.send_photo(self._telegram_reply_target, img_b)
                            )
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE natural sentence in the user's language "
                        f"(e.g. 'Looking at your {_stall} now' / "
                        f"'{'Kameraya' if _stall == 'camera' else 'Ekrana'} bakıyorum'). "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "screen_watch":
                r = await loop.run_in_executor(None, lambda: screen_watch(parameters=args))
                result = r or "Done."

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "add_capability":
                r = await loop.run_in_executor(None, lambda: add_capability(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "computer_agent":
                r = await loop.run_in_executor(None, lambda: computer_agent(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "window_manager":
                r = await loop.run_in_executor(None, lambda: window_manager(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "ui_automation":
                r = await loop.run_in_executor(None, lambda: ui_automation(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "system_scan":
                r = await loop.run_in_executor(
                    None,
                    lambda: system_scan(parameters=args, player=self.ui, on_status=self._on_system_scan_status)
                )
                result = r or "Microsoft Defender scan started."

            elif name == "system_diagnostics":
                r = await loop.run_in_executor(None, lambda: system_diagnostics(parameters=args, player=self.ui))
                result = r or "Diagnostics complete."

            elif name == "process_hunter":
                r = await loop.run_in_executor(None, lambda: find_suspicious_processes(parameters=args, player=self.ui))
                result = r or "Scan complete."

            elif name == "hacker_terminal":
                r = await loop.run_in_executor(None, lambda: run_terminal_command(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "digital_ghost":
                r = await loop.run_in_executor(None, lambda: digital_ghost(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "jarvis_control":
                r = await loop.run_in_executor(None, lambda: jarvis_control(parameters=args, player=self.ui))
                result = r or "JARVIS settings updated."
                if str(args.get("action", "")).lower() in {"power_on", "power_off"}:
                    self.ui.set_power_mode(get_state()["power_mode"]["enabled"])
                self.ui.show_content("JARVIS control", result)

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye.")
                def _shutdown():
                    import time, os
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            elif name in _CUSTOM_TOOL_HANDLERS:
                handler = _CUSTOM_TOOL_HANDLERS[name]
                r = await loop.run_in_executor(None, lambda: handler(parameters=args, player=self.ui))
                result = r or "Done."

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)
            # screen_process sets _vision_busy=True *before* capturing (see
            # above) so a concurrent duplicate call gets rejected by the
            # cooldown check. If the capture itself then raises (no camera,
            # screenshot permission error, ...), nothing else ever clears
            # that flag -- _pending_vision never gets set, so _receive_audio's
            # turn_complete handler (the normal place _vision_busy resets)
            # never runs. Without this, one failed vision call permanently
            # wedges screen_process for the rest of the session: every next
            # attempt is rejected with "still processing the previous
            # request" until a full Gemini reconnect happens to reset it.
            if name == "screen_process":
                self._vision_busy = False

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        latency.record(name, (time.monotonic() - _t_start) * 1000)
        record_activity(name, str(result), "done")
        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )
