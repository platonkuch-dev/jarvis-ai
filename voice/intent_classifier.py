"""
MiniLM fuzzy-intent tier — sits between the exact/regex Fast Router
(fast_router.py) and the Smart Path (Gemini Live), completing the priority
ladder the original spec asked for: exact command > regex > classifier >
LLM. Catches paraphrases the regex router is deliberately too literal to
match (e.g. "подними звук" instead of "громче").

Deliberately scoped to ZERO-ARGUMENT intents only (mute, volume up/down,
screenshot, minimize, maximize, close-active-window) — these are the cases
where a wrong guess is cheap to have made (idempotent, no destructive slot
value). Parameterized intents (open/close a NAMED app, a specific volume
number) are NOT attempted here: extracting the right slot from a paraphrase
without real NLU is a guess, and fast_router.py's own regex already covers
most real phrasings of those. This mirrors fast_router.py's stated
philosophy: a wrong guess executed at low latency is worse than a slightly
slower correct one.

Safety note from real testing against this exact model
(paraphrase-multilingual-MiniLM-L12-v2): raw cosine similarity against a
small set of per-intent example phrases is NOT reliable on its own — e.g.
"закрой хром" (a parameterized close-app command that must never be
auto-executed here) scored 0.847 against the "mute" examples, well above
what would look like a safe threshold. Two things fixed it in testing:

  1. An explicit "other" decoy bucket of adversarial phrases (parameterized
     commands, small talk) that competes directly for argmax — this is far
     more reliable than tuning an absolute threshold, because general-
     purpose sentence embeddings run high on short Russian phrases
     regardless of actual semantic match.
  2. Requiring BOTH a minimum absolute confidence AND a minimum margin over
     the best-scoring example from a different intent (with "other" counted
     as a competing intent). Confidence alone let "подними звук" (raise
     volume) top-match "mute" at 0.973 before the decoy bucket existed;
     margin alone let genuine matches through with too little confidence.

With the current example set, this combination produced zero dangerous
misclassifications across ~18 adversarial test phrases (mute/volume
confusions, parameterized app commands, off-topic chit-chat) — see the
verification note in voice/README.md for the actual test transcript.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from core import latency

_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_CONFIDENCE_THRESHOLD = 0.80
_MARGIN_THRESHOLD = 0.03

# A handful of representative Russian phrasings per intent — not exhaustive,
# just enough anchor points for cosine similarity to generalize from. The
# "other" bucket is a decoy: matching it (or losing to it) means "not
# confident this is one of our zero-arg intents" — never dispatched.
_INTENT_EXAMPLES: dict[str, list[str]] = {
    "mute": [
        "выключи звук", "заглуши", "без звука", "убери звук совсем",
        "отключи звук полностью", "выруби звук", "вырубай звук",
        "убери звук совсем на нуль", "выключи громкость совсем",
    ],
    "volume_up": [
        "громче", "прибавь громкость", "увеличь громкость",
        "сделай погромче", "добавь звука", "подними звук",
        "прибавь звук", "сделай звук громче", "увеличь звук",
    ],
    "volume_down": [
        "тише", "убавь громкость", "уменьши громкость",
        "сделай потише", "снизь громкость", "опусти звук",
        "убавь звук", "сделай звук тише",
    ],
    "screenshot": [
        "сделай скриншот", "скриншот", "сфотографируй экран",
        "сохрани экран", "сними экран",
    ],
    "minimize": [
        "сверни окно", "минимизируй", "убери окно", "сверни это",
        "скрой окно",
    ],
    "maximize": [
        "разверни окно", "максимизируй", "на весь экран",
        "разверни на весь экран",
    ],
    "close_active": [
        "закрой окно", "закрой программу", "закрой это", "закрой приложение",
    ],
    "other": [
        "закрой хром", "закрой телеграм", "закрой дискорд", "открой хром",
        "открой телеграм", "открой блокнот", "запусти стим",
        "переключись на браузер", "какая погода", "расскажи анекдот",
        "найди файл", "который час", "включи музыку", "что нового",
    ],
}

# Real, dispatchable intents — "other" is a decoy and never returned as a match.
DISPATCHABLE_INTENTS = frozenset(k for k in _INTENT_EXAMPLES if k != "other")


@dataclass
class IntentResult:
    matched: bool
    intent: str = ""
    confidence: float = 0.0


class IntentClassifier:
    """Loaded once, kept resident — same 'never reload per command' rule
    the rest of voice/ follows."""

    def __init__(self):
        self._model = None
        self._example_embeddings: np.ndarray | None = None
        self._example_intents: list[str] = []

    def load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer
        import torch
        t0 = time.monotonic()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = SentenceTransformer(_MODEL_NAME, device=device)

        examples, intents = [], []
        for intent, phrases in _INTENT_EXAMPLES.items():
            for phrase in phrases:
                examples.append(phrase)
                intents.append(intent)
        self._example_intents = intents
        embeddings = self._model.encode(examples, normalize_embeddings=True)
        self._example_embeddings = np.asarray(embeddings, dtype=np.float32)
        latency.record("intent_classifier_load", (time.monotonic() - t0) * 1000)

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def classify(self, text: str) -> IntentResult:
        """Never raises. Returns matched=False for anything not confidently
        a zero-arg intent — including anything that ties with (or loses to)
        the "other" decoy bucket."""
        if self._model is None or self._example_embeddings is None or not text.strip():
            return IntentResult(matched=False)

        t0 = time.monotonic()
        query = self._model.encode([text], normalize_embeddings=True)[0]
        sims = self._example_embeddings @ query  # both normalized -> cosine sim
        latency.record("intent_classifier", (time.monotonic() - t0) * 1000)

        order = np.argsort(-sims)
        top_idx = int(order[0])
        top_intent = self._example_intents[top_idx]
        top_conf = float(sims[top_idx])

        if top_intent not in DISPATCHABLE_INTENTS:
            return IntentResult(matched=False, confidence=top_conf)

        # margin against the best-scoring example from ANY different intent
        # (including "other") — this is what actually catches the dangerous
        # high-absolute-confidence-but-wrong cases found in testing.
        runner_up_idx = next(i for i in order if self._example_intents[int(i)] != top_intent)
        margin = top_conf - float(sims[runner_up_idx])

        if top_conf < _CONFIDENCE_THRESHOLD or margin < _MARGIN_THRESHOLD:
            return IntentResult(matched=False, intent=top_intent, confidence=top_conf)

        return IntentResult(matched=True, intent=top_intent, confidence=top_conf)
