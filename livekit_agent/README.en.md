# Jarvis — a voice assistant for Windows

A voice assistant that listens, answers out loud and does things on your PC: opens apps, manages windows and files, drives After Effects, starts Claude Code, sets reminders, messages people on Telegram and answers phone calls. Built on [LiveKit Agents](https://docs.livekit.io/agents/), Claude (Anthropic), Deepgram and Edge TTS / ElevenLabs. The assistant speaks Russian by default (the language is a setting); the panel and most docs are in Russian, this page is the short English version.

## Quick start

1. Install [Python 3.11 or 3.12](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
2. Run **`Start-Panel.bat`**. The first run creates a virtual environment, installs dependencies and opens the control panel in your browser.
3. Enter the two required keys — **Claude** (console.anthropic.com) and **Deepgram** (console.deepgram.com) — and press "Save and check" (the button on the panel). The Claude check sends a one-token request, so it also tells you whether the account has credit.
4. Press "Start Jarvis" (the gold button at the top). A tray icon appears; say hello.

Telegram, phone calls and custom ElevenLabs voices are optional and are set up in the same panel. Step-by-step guides for every key: [docs/SETUP.md](docs/SETUP.md) (Russian).

`INSTALL_JARVIS.bat` additionally adds Windows autostart and a desktop shortcut.

## Control panel

`python panel.py`, or "Открыть панель" in the tray icon menu.

- **Настройка (Setup)** — keys and options, per-service connection tests, Telegram login without a terminal, start/stop.
- **Возможности (Capabilities)** — everything Jarvis can do, with example phrases, marked as working / needs setup on this machine.
- **Голоса (Voices)** — named ElevenLabs voices for "switch the voice to …".
- **Состояние (Status)** — which parts are running.

The panel binds to `127.0.0.1` only, rejects foreign `Host` headers and needs a per-install token. Secrets are never sent back to the browser (only the last four characters).

## What it can do

Apps and windows (Windows UI Automation), screen-driven multi-step tasks (Claude computer use — the most expensive feature), files and system settings (deletes go to the Recycle Bin), After Effects through a scripting bridge (Photoshop and Premiere Pro are experimental), coding with Claude Code in VS Code, reminders/timers/calendar/to-dos, long-term memory and voice scenarios, Telegram (send messages from a dedicated account, chat with Jarvis, health alerts), inbound phone calls via LiveKit, switchable ElevenLabs voices, F10 sleep/wake.

## Security model

- Irreversible actions (sleep, deleting/saving a scenario) need an explicit spoken confirmation, enforced inside the tools.
- System actions are whitelisted; scripts run only from `config.SCRIPT_WHITELIST` (empty by default); there is no arbitrary shell, `eval` or `exec`.
- Telegram: the first person to message Jarvis's account becomes the owner and gets tools and memory; everyone else gets plain conversation with no tools and none of your data.
- Phone: a caller gets the same power as you at the microphone — do not hand the number out.
- Personal data (`data/`, `.env`, Telegram sessions, logs) stays on your machine and is git-ignored. Before publishing your own copy run `python scripts/release_check.py`.

## Cost

You pay the providers directly. Rough guide (check current prices): Claude Haiku for normal conversation costs a fraction of a cent per turn thanks to prompt caching; computer-use steps on Claude Sonnet cost noticeably more; Edge TTS is free; Deepgram, ElevenLabs and LiveKit are billed by usage. A Claude Pro/Max subscription does not pay for API calls — the API has its own balance.
