# Hey Laya

**Local-first voice assistant.** Hold a key or say *“Hey Laya”* — music, volume,
apps, dark mode, timers, and system power, all decided on-device by the
[Laya](https://pypi.org/project/laya/) reasoning model and spoken back with
Kokoro TTS. Zero API keys, zero cloud.

```
hold Right-Alt → “set volume to fifty percent” → [Laya] volume_set 50 → “Volume set.”
say “Hey Laya, open Spotify”                   → [Laya] app → xdg-open spotify → “Opening it.”
say “Hey Laya, what’s the capital of France”   → [Laya] none → local LLM (or canned fallback)
```

| Layer | Choice | Why |
|-------|--------|-----|
| Decision | **Laya** in-process (English checkpoint) | open-weights router; calibrated `answer_confidence` gates every command |
| STT | faster-whisper (`base`, CPU int8) | fast, offline |
| TTS | Kokoro-82M (`af_heart`) | offline, 24 kHz, warm disk cache |
| LLM | llama-server → Ollama → canned | only for open questions; **autostart off by default** |
| Actions | `playerctl` / `wpctl` / `gsettings` / `xdg-open` (Linux), pycaw (Windows) | native tools, no agents, no shell exec |

---

## Install

```bash
git clone https://github.com/touhidsiddiqueeraj-bit/hey-laya.git
cd hey-laya
./install.sh
```

`install.sh` is idempotent. It will:

1. Create `.venv` if missing (Python ≥ 3.10)
2. Install `requirements.txt` (uses `uv` if present, else `pip`)
3. Write `.env` from `.env.example` (first run only)
4. Put **`hey-laya`** on your PATH → `~/.local/bin/hey-laya`
5. Add a desktop entry → `~/.local/share/applications/hey-laya.desktop`
6. Run the fast test suite as a smoke check

Manual alternative:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py --help
```

---

## Use

```bash
hey-laya                     # full voice: PTT (hold Right-Alt) + wake word
hey-laya --ptt-only          # only push-to-talk
hey-laya --wake-only         # only always-on wake word
hey-laya --text "volume up"  # one utterance, print reply (no mic)
hey-laya --no-ui             # headless (no Tk window)
```

| Flag | Meaning |
|------|---------|
| `--text "…"` | Run one utterance through the full pipeline and print the reply |
| `--fixture FILE` | Batch routing smoke test (no audio) |
| `--gate 0.50` | Laya confidence gate — lower ⇒ more LLM fallback |
| `--stt base` | faster-whisper size: `tiny` / `base` / `small` / `medium` |
| `--demo-tts` | Warm the TTS cache and exit |
| `--no-ui` | Headless |
| `--ptt-only` / `--wake-only` | Restrict interaction mode |

### Interaction

- **Push-to-talk:** hold **Right-Alt**, speak, release. Recording stops →
  whisper transcribes → Laya decides → action runs → Kokoro speaks the reply.
- **Wake word:** always listening (~2.5 s buffers). Say **“Hey Laya”** followed
  by the command, or just “Hey Laya” and speak after the prompt.
- **UI:** small always-on-top window. Color bar = state
  (light blue ready · blue listening · purple working · red error).
  Radio buttons switch PTT / wake / both at runtime.

### What it understands (v1)

| Domain | Examples |
|--------|----------|
| Media | “pause the music”, “next track”, “play” |
| Volume | “volume up”, “mute”, “set volume to fifty percent” |
| Apps | “open Spotify”, “launch the terminal”, “focus Slack” |
| Display | “switch to dark mode”, “brightness up” |
| System | “lock the computer” *(logout/shutdown/reboot/sleep confirmed by wording)* |
| Timers | “set a timer for ten minutes” |
| Anything else | falls through to the local LLM, or a canned “can’t answer” reply |

Intent routing is **not** keyword matching — Laya answers structured questions
(see `decide.py`) and `answer_confidence < GATE` always falls back to the LLM.

---

## Configuration (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `GATE` | `0.50` | Laya confidence gate for acting vs asking the LLM |
| `STT_MODEL` | `base` | faster-whisper model size |
| `TTS_VOICE` | `af_heart` | Kokoro voice |
| `KOKORO_PY` | — | Override Kokoro interpreter (defaults to project venv) |
| `MODE` | `both` | `ptt` \| `wake` \| `both` |
| `LLM_URL` | — | Explicit OpenAI-compatible endpoint (skips auto-detect) |
| `LLM_MODEL` | `~/…/Qwen3-4B-Q4_K_M.gguf` | GGUF for llama-server spawn |
| `LLAMA_SERVER_BIN` | — | llama-server binary path (auto-detects if empty) |
| `LLM_AUTOSTART` | **`0`** | `1` spawns llama-server on `:8081` at startup |
| `OLLAMA_MODEL` | `qwen3:4b` | Model name when using Ollama |
| `POLARIS_TTS_URL` | — | Optional external TTS HTTP endpoint |

### LLM chain (first hit wins)

1. `LLM_URL` if set
2. Healthy llama-server on `:8080` (e.g. Polaris) or `:8081`
3. Own thin llama-server spawn on `:8081` — **only if `LLM_AUTOSTART=1`**
4. Ollama on `:11434`
5. Canned reply: *“I can't answer that yet — no local model is running.”*

> **RAM warning.** Laya (torch) uses ~4–6 GB. Adding a 4B llama-server on top
> can OOM an 8–16 GB machine. Autostart defaults to **off** for this reason.
> Start a server manually (or use Polaris) when you need open-ended answers,
> and only set `LLM_AUTOSTART=1` if ≥ 8 GB is free.

---

## Architecture

```
mic ─► audio.Recorder ─► faster-whisper ─► text
                                          │
                                          ▼
                              decide.decide(router, text)
                              ┌────────────────────────────┐
                              │ Laya Router.predict()      │
                              │  task → domain questions   │
                              │  answer_confidence ≥ GATE? │
                              └────────────┬───────────────┘
                          actions          │          needs_llm
                              │            └───────────┤
                              ▼                        ▼
                     actions.run(dict)            llm.ask(text)
                     linux | windows            (chain, canned)
                              │                        │
                              └──────────┬─────────────┘
                                         ▼
                              tts.Speaker (queue)
                              cache → Kokoro → play_wav
```

- **`decide.py`** — the Laya question set (dict of `type` / `instructions` /
  `criteria`, dependency-gated) + `split_actions` mapping answers to action
  dicts. A regex hint forces `open|launch|start|run <known-app>` to the app path.
- **`actions/`** — thin, fail-safe platform adapters. Unknown commands return an
  error reply; nothing here executes arbitrary shell.
- **`tts.py`** — SHA-256 disk cache of every canned line (warmed at startup);
  live lines render via Kokoro in-process. Fail loud — no espeak fallback.
- **`llm.py`** — explicit chain, short timeouts, Qwen3 `reasoning_content`
  handling (`_pick_content`).

### Project layout

```
main.py            entry point: pipeline, PTT/wake loops, CLI, UI wiring
decide.py          Laya QUESTIONS + decide/split_actions   (self-check: python decide.py)
llm.py             backend chain                            (self-check: python llm.py)
tts.py             cache + Kokoro + Polaris                 (self-check: python tts.py)
audio.py           recorder, wake regex, transcribe, play   (self-check: python audio.py)
replies.py         canned spoken strings
timers.py          background countdown                     (self-check: python timers.py)
ui.py              Tkinter status window
actions/           linux.py · windows.py · __init__.py (dispatcher)
fixtures/          routing.txt — batch phrases
test_all.py        full test suite (--full adds live Laya)
install.sh         system installer
```

---

## Test

```bash
source .venv/bin/activate
python test_all.py          # fast: 74 unit checks, no model load
python test_all.py --full   # + live Laya routing fixture (80 checks, ~2 GB RAM)
python main.py --fixture fixtures/routing.txt   # 9-phrase batch through the app
```

CI-friendly: `test_all.py` needs no microphone, no display (UI check degrades
gracefully), and no LLM server.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `hey-laya: command not found` | `export PATH="$HOME/.local/bin:$PATH"` and reopen the shell |
| No reply voice | Check `python tts.py`; set `KOKORO_PY` to a working Kokoro venv; need a default audio sink |
| PTT does nothing | pynput needs X11/Wayland input access; try `--text` first to isolate |
| Wake never fires | Mic default source? `python -c "import sounddevice as sd; print(sd.query_devices())"`; try `--stt tiny` |
| “can't answer” on questions | Expected with `LLM_AUTOSTART=0`. Start llama-server/Ollama or set `LLM_URL` |
| OOM / machine freezes | Keep `LLM_AUTOSTART=0`; use `--stt tiny`; close other heavy apps |
| LLM answers only reasoning | Handled automatically (`_pick_content` + retry); still empty ⇒ raise `max_tokens` in `llm.py` |
| `laya` checkpoint temp warning | Upstream known issue; confidence for 11+ choice labels is uncalibrated — our question sets stay under that |
| Windows: no volume control | `pip install pycaw comtypes` on the Windows Python |

---

## Platform notes

**Linux (primary, tested):** PipeWire/Pulse (`wpctl`/`pactl`), playerctl for
media, `brightnessctl` → `gsettings` → `xrandr` chain for brightness, GNOME/KDE
dark mode via `gsettings`, `loginctl` lock.

**Windows (secondary):** pycaw volume, `os.startfile` apps, SendInput media
keys, registry dark mode. Python 3.11 + `pip install pycaw comtypes`. Right-Alt
PTT works via pynput; wake word needs a default mic.

**Not in v1:** shell exec, file operations, web search, plugins, macOS,
PyInstaller packaging, multi-language Laya.

---

## Development

```bash
source .venv/bin/activate
python test_all.py          # before every commit
python -m pyflakes *.py actions/*.py
```

Each module keeps an assert-based `demo()` under `if __name__ == "__main__"`.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
