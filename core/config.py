"""
core/config.py — single source of truth for tunable constants.

Model names, token budgets, and similar values used to be copy-pasted across
every action module. Import them from here instead of hardcoding the same
string again — one place to bump a model version or retune a budget.
"""

# ── Claude (default text/vision brain — see actions/*.py for the per-provider
#    fallback to Gemini when no Claude key is configured) ───────────────────
CLAUDE_MODEL                = "claude-sonnet-5"
CLAUDE_DEFAULT_MAX_TOKENS   = 8192
CLAUDE_THINKING_BUDGET      = 6000
CLAUDE_THINKING_MAX_TOKENS  = 12000

# ── Gemini (voice engine + fallback brain) ───────────────────────────────────
GEMINI_TEXT_MODEL  = "gemini-2.5-flash"
GEMINI_LITE_MODEL  = "gemini-2.5-flash-lite"
GEMINI_LIVE_MODEL  = "models/gemini-2.5-flash-native-audio-preview-12-2025"
GEMINI_VOICE_NAME  = "Puck"

DEFAULT_PROVIDER = "claude"

# ── Memory ───────────────────────────────────────────────────────────────────
MEMORY_MAX_CHARS     = 2200
MEMORY_MAX_VALUE_LEN = 380

# ── Behavioral pattern learning ──────────────────────────────────────────────
PATTERN_ANALYZE_EVERY = 40
PATTERN_MAX_LOG_LINES = 300

# ── Screen watch ──────────────────────────────────────────────────────────────
SCREEN_WATCH_INTERVAL_SECONDS = 12
