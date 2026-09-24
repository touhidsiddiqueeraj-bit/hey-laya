# Contributing to Hey Laya

## Setup

```bash
git clone https://github.com/touhidsiddiqueeraj-bit/hey-laya.git
cd hey-laya
./install.sh
source .venv/bin/activate
```

## Before you commit

```bash
python test_all.py          # must be 0 failed (fast)
python test_all.py --full   # include live Laya routing (needs ~2 GB RAM)
python -m pyflakes *.py actions/*.py
```

## Conventions

- **No unrequested abstractions.** Flat modules, stdlib first, shortest diff wins.
- **Every module keeps a self-check**: an assert-based `demo()` reachable via
  `python <module>.py` and from `test_all.py`.
- **No secrets, no cloud keys.** The project is local-only by design.
- **Actions stay thin.** `actions/*.py` wraps native tools and returns bool/state;
  reply strings live in `replies.py`; routing lives in `decide.py`.
- **Fail loud, never silent.** Prefer a spoken error reply over espeak or a blank.
- **RAM is sacred.** Nothing heavy (torch models, llama-server) starts unless the
  user asked for it (`LLM_AUTOSTART`, lazy Router load).

## Adding a command

1. Add/extend a question in `decide.py` `QUESTIONS` (with `instructions` +
   `criteria`, and `depends_on` if conditional).
2. Map answers → action dict in `decide.split_actions`.
3. Handle the action in `actions/linux.py` + `actions/windows.py` and
   `actions/__init__.py`.
4. Add spoken replies to `replies.py` (short, no markdown).
5. Add a phrase to `fixtures/routing.txt` and a unit case to `test_all.py`.

## Reporting bugs

Open an issue with: OS + desktop, `python --version`, the exact command,
and the terminal output. For routing bugs include the phrase and expected
action.
