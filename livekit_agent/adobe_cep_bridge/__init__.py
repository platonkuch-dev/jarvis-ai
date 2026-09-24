"""CEP bridge: lets Python drive Premiere Pro / After Effects through their
own ExtendScript engine (exact, non-visual control) instead of clicking
through the UI.

- installer.py: installs a minimal CEP extension + enables PlayerDebugMode
  (Adobe's supported way to load an unsigned/dev extension).
- client.py: talks to that extension's Chrome-DevTools-Protocol debug port
  to run ExtendScript and get its result back.

Ports (see extension/.debug): Premiere Pro = 9231, After Effects = 9232.
"""

from __future__ import annotations

PREMIERE_PORT = 9231
AFTER_EFFECTS_PORT = 9232

from . import client, installer  # noqa: E402,F401
