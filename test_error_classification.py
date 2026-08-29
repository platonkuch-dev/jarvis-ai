"""
Smoke test for the reconnect-loop error classification in main.py's run()
(_flatten_exceptions / _is_audio_device_error).

Why this exists: asyncio.TaskGroup wraps whatever its child tasks raise in
an (Base)ExceptionGroup, and str(group) is always the generic "unhandled
errors in a TaskGroup (N sub-exceptions)" — never the real error message.
Every keyword-based check in run() (network vs. invalid API key vs. audio
device) used to match against that generic string, so none of them ever
fired for errors raised from inside the TaskGroup body (which is nearly
every real crash: network drops, Gemini 1011s, PortAudioError). This test
pins down that unwrapping actually recovers the real exception, without
needing a live mic, network, or API key.

Run directly:  python test_error_classification.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import sys

import sounddevice as sd

from main import _flatten_exceptions, _is_audio_device_error

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures += 1


# 1. A plain exception (raised before the TaskGroup is entered, e.g. an
#    auth failure during connect()) must flatten to itself.
plain = OSError("getaddrinfo failed")
check("plain exception flattens to itself", _flatten_exceptions(plain) == [plain])

# 2. An ExceptionGroup — what asyncio.TaskGroup actually raises when a
#    child task fails — must flatten to the real leaf exception, not the
#    group wrapper, and str(group) must NOT be what's used for classification.
inner = OSError("getaddrinfo failed: no such host")
group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
leaves = _flatten_exceptions(group)
check("group unwraps to the real leaf exception", leaves == [inner])
check(
    "group's own str() does not leak the real message (regression guard "
    "for the bug this file exists to catch)",
    "getaddrinfo" not in str(group),
)

# 3. Nested groups (TaskGroup inside a TaskGroup, or multiple failing
#    children) must unwrap fully, not just one level.
nested = ExceptionGroup("outer", [ExceptionGroup("inner", [inner]), ValueError("x")])
nested_leaves = _flatten_exceptions(nested)
check(
    "nested groups fully unwrap to all real leaves",
    len(nested_leaves) == 2 and inner in nested_leaves,
)

# 4. A real sounddevice.PortAudioError (the actual "no driver installed" /
#    device-unplugged crash from the logs) must be recognized as an audio
#    device error, so run() gives a clear message instead of endlessly
#    reconnecting to Gemini for a problem Gemini can't fix.
audio_exc = sd.PortAudioError("Unanticipated host error [PaErrorCode -9999]")
check("PortAudioError is recognized as an audio device error", _is_audio_device_error(audio_exc))

# 5. A network error must NOT be misclassified as an audio error.
check("a plain OSError is not misclassified as an audio device error", not _is_audio_device_error(inner))

# 6. The actual crash shape from jarvis_fastpath.err.log: PortAudioError
#    raised inside _play_audio, wrapped by the TaskGroup.
audio_group = ExceptionGroup("unhandled errors in a TaskGroup", [audio_exc])
audio_group_leaves = _flatten_exceptions(audio_group)
check(
    "TaskGroup-wrapped PortAudioError is still classified as an audio "
    "error after unwrapping (this is the exact production crash shape)",
    any(_is_audio_device_error(x) for x in audio_group_leaves),
)

print()
if failures:
    print(f"{failures} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
sys.exit(0)
