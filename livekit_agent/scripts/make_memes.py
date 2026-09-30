"""Fills data/memes/ with a starter pack of meme sounds that are ours to use.

    python scripts/make_memes.py               # synthesized classics (free, offline)
    python scripts/make_memes.py --elevenlabs  # + laughs/effects and voiced catchphrases via ElevenLabs

The classics (rimshot, sad trombone, airhorn, ...) are synthesized from
scratch with numpy -- no clips from films or songs. --elevenlabs uses the
ElevenLabs sound-generation API (your ELEVENLABS_API_KEY, a few cents of
credits) for things synthesis can't fake, like real laughter. Existing files
are never overwritten, so re-running only fills in what's missing.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

SR = 44100
rng = np.random.default_rng(7)


# ---------------------------------------------------------------------------
# Synthesis helpers
# ---------------------------------------------------------------------------

def t(sec: float) -> np.ndarray:
    return np.arange(int(sec * SR)) / SR


def env(n: int, attack=0.01, release=0.1) -> np.ndarray:
    e = np.ones(n)
    a, r = int(attack * SR), int(release * SR)
    if a:
        e[:a] = np.linspace(0, 1, a)
    if r:
        e[-r:] *= np.linspace(1, 0, r)
    return e


def decay(n: int, tau: float) -> np.ndarray:
    return np.exp(-np.arange(n) / (tau * SR))


def brass(freq, sec, vibrato=0.0, harmonics=8, bright=1.0) -> np.ndarray:
    x = t(sec)
    f = freq * (1 + vibrato * np.sin(2 * np.pi * 5.5 * x) * np.clip(x / 0.3, 0, 1))
    phase = 2 * np.pi * np.cumsum(f) / SR
    out = sum(np.sin(k * phase) / k ** (1.6 / bright) for k in range(1, harmonics + 1))
    return out


def noise(sec) -> np.ndarray:
    return rng.standard_normal(int(sec * SR))


def highpass(x, alpha=0.9) -> np.ndarray:
    y = np.zeros_like(x)
    for i in range(1, len(x)):
        y[i] = alpha * (y[i - 1] + x[i] - x[i - 1])
    return y


def lowpass(x, alpha=0.1) -> np.ndarray:
    y = np.zeros_like(x)
    for i in range(1, len(x)):
        y[i] = y[i - 1] + alpha * (x[i] - y[i - 1])
    return y


def drum(freq=180, sec=0.35, drop=2.5) -> np.ndarray:
    x = t(sec)
    f = freq * (1 + drop * np.exp(-x / 0.02))
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * decay(len(x), 0.08)
    click = lowpass(noise(sec), 0.3) * decay(len(x), 0.01)
    return body + 0.5 * click


def cymbal(sec=1.2) -> np.ndarray:
    return highpass(noise(sec), 0.97) * decay(int(sec * SR), 0.35)


def seq(*parts: tuple[float, np.ndarray]) -> np.ndarray:
    """(start_seconds, samples) pairs mixed onto one track."""
    total = max(int(s * SR) + len(p) for s, p in parts)
    out = np.zeros(total)
    for s, p in parts:
        i = int(s * SR)
        out[i:i + len(p)] += p
    return out


# ---------------------------------------------------------------------------
# The pack
# ---------------------------------------------------------------------------

def ba_dum_tss():
    return seq((0.0, drum(200)), (0.18, drum(150)), (0.42, 0.6 * cymbal(1.3)), (0.42, 0.5 * drum(300, 0.2)))


def sad_trombone():
    notes = [(0.0, 293.7, 0.45), (0.5, 277.2, 0.45), (1.0, 261.6, 0.45), (1.5, 246.9, 1.4)]
    parts = []
    for start, f, d in notes:
        tone = brass(f, d, vibrato=0.018 if d > 1 else 0.0, bright=0.8)
        # "wah" -- the mute opening: amplitude + brightness swell per note
        tone *= env(len(tone), 0.08, 0.2) * (0.6 + 0.4 * np.sin(np.linspace(0, np.pi, len(tone))))
        parts.append((start, tone))
    return lowpass(seq(*parts), 0.25)


def airhorn():
    def blast(sec):
        tone = brass(466, sec, harmonics=12, bright=1.8) + 0.7 * brass(587, sec, harmonics=12, bright=1.8)
        return np.tanh(2.5 * tone) * env(len(tone), 0.01, 0.05)
    return seq((0.0, blast(0.35)), (0.42, blast(0.18)), (0.66, blast(0.18)), (0.9, blast(1.0)))


def bonk():
    x = t(0.4)
    f = 520 * np.exp(-x / 0.05) + 170
    wood = np.sin(2 * np.pi * np.cumsum(f) / SR) * decay(len(x), 0.07)
    ring = 0.4 * np.sin(2 * np.pi * 820 * x) * decay(len(x), 0.04)
    return wood + ring + 0.6 * lowpass(noise(0.4), 0.5) * decay(len(x), 0.004)


def fail_buzzer():
    x = t(0.9)
    sq = np.sign(np.sin(2 * np.pi * 110 * x)) + np.sign(np.sin(2 * np.pi * 116.5 * x))
    return lowpass(sq, 0.2) * env(len(x), 0.01, 0.1)


def dramatic():
    def hit(freqs, sec):
        tone = sum(brass(f, sec, bright=1.2) for f in freqs)
        return tone * env(len(tone), 0.02, sec * 0.6)
    return seq((0.0, hit([146.8, 220.0, 293.7], 0.35)), (0.45, hit([138.6, 207.7, 277.2], 0.35)),
               (0.9, hit([130.8, 196.0, 246.9, 65.4], 2.2)), (0.9, 0.8 * drum(60, 1.5, 1.0)))


def victory():
    notes = [(0.0, 523.3, 0.14), (0.15, 659.3, 0.14), (0.3, 784.0, 0.14), (0.45, 1046.5, 0.7)]
    parts = [(s, brass(f, d, harmonics=6, bright=1.4) * env(int(d * SR), 0.01, 0.1)) for s, f, d in notes]
    return seq(*parts, (0.45, 0.4 * cymbal(1.0)))


def crickets():
    parts = []
    for k in range(6):
        start = k * 0.55 + rng.uniform(0, 0.08)
        x = t(0.16)
        chirp = np.sin(2 * np.pi * 4600 * x) * (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 45 * x)))
        parts.append((start, chirp * env(len(x), 0.005, 0.03) * 0.5))
    return seq(*parts)


def ding():
    x = t(1.6)
    partials = [(1.0, 1.0), (2.76, 0.5), (5.4, 0.25), (8.9, 0.12)]
    return sum(a * np.sin(2 * np.pi * 1318 * r * x) * decay(len(x), 0.6 / r) for r, a in partials)


def whoosh():
    sec = 0.9
    n = noise(sec)
    sweep = np.zeros_like(n)
    alphas = np.linspace(0.02, 0.5, len(n))
    for i in range(1, len(n)):
        sweep[i] = sweep[i - 1] + alphas[i] * (n[i] - sweep[i - 1])
    sweep *= np.sin(np.linspace(0, np.pi, len(n))) ** 2
    sparkle = sum(np.sin(2 * np.pi * f * t(sec)) * decay(int(sec * SR), 0.15)
                  for f in (2093, 2637, 3136)) * 0.15
    return seq((0.0, sweep), (0.55, sparkle[: int(0.35 * SR)]))


SYNTH = {
    "ба дум тсс": (ba_dum_tss, "после шутки или тупого каламбура — своего или владельца"),
    "грустный тромбон": (sad_trombone, "когда владелец облажался, что-то не получилось, провал"),
    "эйрхорн": (airhorn, "эпичный момент, владелец что-то затащил, победа в игре, «ну ты монстр»"),
    "бонк": (bonk, "владелец сморозил глупость или пошлость — «бонк, иди в хорни-тюрьму»"),
    "неправильно": (fail_buzzer, "неправильный ответ, плохая идея, «нет, так не пойдёт»"),
    "драма": (dramatic, "внезапный поворот, драматичный момент, шокирующая новость"),
    "победа": (victory, "задача выполнена, что-то удалось, маленький триумф"),
    "сверчки": (crickets, "неловкая тишина, несмешная шутка владельца, никто не ответил"),
    "дзынь": (ding, "правильно, идея засчитана, «в точку»"),
    "вжух": (whoosh, "магия, сделал что-то мгновенно, «вжух — и готово»"),
}

# ElevenLabs sound-generation prompts: name -> (prompt, seconds, hint, is_laugh)
ELEVEN = {
    "смех/ржач": ("a man bursting into loud genuine belly laughter, can't stop laughing", 3.0, "", True),
    "смех/смешок": ("a man chuckling briefly, short amused laugh through the nose", 1.5, "", True),
    "смех/злодейский": ("deep male evil villain laugh, muahaha", 3.0, "", True),
    "смех/истерика": ("a man laughing hysterically, wheezing, out of breath from laughing", 4.0, "", True),
    "смех/саркастичный": ("a single sarcastic fake laugh, male voice, ha. ha. ha.", 2.0, "", True),
    "бум": ("deep cinematic bass boom hit with long reverb, meme style dramatic boom", 2.0,
            "после внезапного факта или жёсткого подкола — эффект «бум»", False),
    "скретч": ("vinyl record scratch stop, sudden awkward pause", 1.0,
               "стоп, что? — когда владелец сказал что-то неожиданное или странное", False),
    "аплодисменты": ("small crowd clapping and cheering enthusiastically", 3.0,
                     "владелец реально молодец, заслужил овации (или саркастично)", False),
}


# Catchphrase memes voiced by Jarvis's own ElevenLabs voice (our recording,
# not a clip lifted from a video), optionally with a synthesized hit:
# name -> (text, fx, hint, (stability, style))
#   fx: None | "boom" (bass hit right after the line) | "hit" (hit under the line's start)
VOICE = {
    "дисциплина": ("ДИСЦИПЛИНА!", "boom",
                   "владелец ленится, прокрастинирует, ноет или просит «ну давай потом» — жёсткое «дисциплина!»",
                   (0.2, 0.9)),
    "брух": ("Bruuuh...", None, "владелец сказал или сделал что-то настолько тупое, что слов нет", (0.3, 0.7)),
    "эмоциональный урон": ("Emotional damage!", "boom",
                           "сразу после особенно жёсткого подкола или неприятной правды для владельца",
                           (0.25, 0.9)),
    "окак": ("Окак.", None, "неожиданный странный факт или поворот — короткое удивлённое «окак»", (0.4, 0.5)),
    "фиаско": ("Это фиаско, братан.", "hit", "полный провал владельца, всё пошло не так", (0.3, 0.8)),
}


def boom_hit(sec: float = 1.6) -> np.ndarray:
    """Sub-bass drop + distorted thud: the meme "boom" (made here, not sampled)."""
    x = t(sec)
    f = 38 + 90 * np.exp(-x / 0.06)
    sub = np.sin(2 * np.pi * np.cumsum(f) / SR) * decay(len(x), 0.45)
    thud = np.tanh(3 * drum(70, sec, 3.0)) * 0.7
    return np.tanh(1.6 * (sub + thud)) + 0.15 * lowpass(noise(sec), 0.05) * decay(len(x), 0.3)


def eleven_tts_pcm(text: str, stability: float, style: float) -> np.ndarray:
    """Jarvis's configured ElevenLabs voice saying `text`, as float samples at SR."""
    import httpx

    import io

    import av  # PyAV ships with livekit-agents

    voice = config.ELEVENLABS_VOICE_ID or "pNInz6obpgDQGcFmaJgB"   # "Adam" if none configured
    # mp3, decoded here: raw pcm_44100 output is Pro-tier only (403 otherwise).
    resp = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": stability, "similarity_boost": 0.8, "style": style,
                                 "use_speaker_boost": True}},
        timeout=90,
    )
    resp.raise_for_status()
    chunks = []
    with av.open(io.BytesIO(resp.content)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
        for frame in container.decode(container.streams.audio[0]):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    pcm = np.concatenate(chunks).astype(np.float64) / 32768
    # trim leading/trailing silence so the hit lands right on the word
    loud = np.flatnonzero(np.abs(pcm) > 0.02)
    return pcm[max(0, loud[0] - 400): loud[-1] + 2000] if loud.size else pcm


def voice_meme(line: np.ndarray, fx: str | None) -> np.ndarray:
    line = line / (np.abs(line).max() or 1.0)
    if fx == "boom":
        return seq((0.0, line), (len(line) / SR - 0.05, 0.9 * boom_hit()))
    if fx == "hit":
        return seq((0.0, 0.6 * boom_hit(1.2)), (0.02, line))
    return line


def write_wav(path: Path, samples: np.ndarray) -> None:
    samples = samples / (np.abs(samples).max() or 1.0) * 0.9
    pcm = (samples * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def eleven_generate(prompt: str, seconds: float) -> bytes:
    import httpx

    resp = httpx.post(
        "https://api.elevenlabs.io/v1/sound-generation",
        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
        json={"text": prompt, "duration_seconds": seconds, "prompt_influence": 0.6},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.content


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elevenlabs", action="store_true", help="also generate laughs/effects via ElevenLabs")
    args = ap.parse_args()

    out = config.MEMES_DIR
    (out / "смех").mkdir(parents=True, exist_ok=True)
    hints_file = out / "memes.json"
    try:
        hints = json.loads(hints_file.read_text(encoding="utf-8"))
    except Exception:
        hints = {}
    hints.pop("_пример", None)

    for name, (fn, hint) in SYNTH.items():
        path = out / f"{name.replace(' ', '_')}.wav"
        if not path.exists():
            write_wav(path, fn())
            print("synth  ", path.name)
        hints.setdefault(name, hint)

    if args.elevenlabs:
        if not config.ELEVENLABS_API_KEY:
            print("ELEVENLABS_API_KEY is empty -- skipping generated sounds")
        for name, (prompt, sec, hint, is_laugh) in ELEVEN.items():
            path = out / f"{name.replace(' ', '_')}.mp3"
            if not path.exists() and config.ELEVENLABS_API_KEY:
                try:
                    path.write_bytes(eleven_generate(prompt, sec))
                    print("eleven ", name)
                except Exception as e:
                    print("FAILED ", name, "--", e)
                    continue
            if not is_laugh and path.exists():
                hints.setdefault(name, hint)
        for name, (text, fx, hint, (stability, style)) in VOICE.items():
            path = out / f"{name.replace(' ', '_')}.wav"
            if not path.exists() and config.ELEVENLABS_API_KEY:
                try:
                    write_wav(path, voice_meme(eleven_tts_pcm(text, stability, style), fx))
                    print("voice  ", name)
                except Exception as e:
                    print("FAILED ", name, "--", e)
                    continue
            if path.exists():
                hints.setdefault(name, hint)

    hints_file.write_text(json.dumps(hints, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
