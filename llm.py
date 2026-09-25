"""LLM backends: LLM_URL → settings API key → polaris :8080 → thin llama-server spawn → ollama → canned."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import requests

from replies import reply

DEFAULT_MODEL = "/mnt/backup/llm-models/Qwen3-4B-Q4_K_M.gguf"
GGUF_DIRS = [
    Path("/mnt/backup/llm-models"),
    Path.home() / "llama.cpp" / "models",
    Path.home() / "models",
]
SYSTEM = (
    "You are Laya, a concise local voice assistant. "
    "Answer in one short spoken sentence. No markdown, no lists, no reasoning."
)


def _pick_content(payload: dict) -> str:
    """Handle OpenAI chat payloads incl. Qwen3 reasoning_content (content may lag)."""
    try:
        msg = payload["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if content:
            return content
        # Reasoning model: fall back to trailing non-reasoning tail if any
        reasoning = (msg.get("reasoning_content") or "").strip()
        if reasoning:
            # Last line often has the answer once tokens ran out mid-think;
            # with enough max_tokens, content fills — prefer empty→cant over dump.
            return ""
        return ""
    except Exception:
        return ""


def _find_model() -> Path | None:
    env = os.environ.get("LLM_MODEL")
    if env and Path(env).exists():
        return Path(env)
    if Path(DEFAULT_MODEL).exists():
        return Path(DEFAULT_MODEL)
    for d in GGUF_DIRS:
        if not d.is_dir():
            continue
        # Prefer smallest real chat model (skip image/tts talkers/mmproj)
        cands = sorted(
            (
                p
                for p in d.glob("*.gguf")
                if not any(s in p.name for s in ("talker", "mmproj", "flux-", "tokenizer"))
            ),
            key=lambda p: p.stat().st_size,
        )
        if cands:
            return cands[0]
    return None


def _find_llama_server() -> Path | None:
    env = os.environ.get("LLAMA_SERVER_BIN")
    if env and Path(env).exists():
        return Path(env)
    for p in (
        Path.home() / "llama.cpp" / "build" / "bin" / "llama-server",
        Path("/home/touhid/llama.cpp/build/bin/llama-server"),
        Path(shutil.which("llama-server") or ""),
    ):
        if p and p.exists():
            return p
    return None


def _healthy(url: str, timeout: float = 0.4) -> bool:
    try:
        base = url.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        # llama-server /health, ollama /api/tags
        if "/api" in base or "11434" in base:
            r = requests.get(base + "/api/tags", timeout=timeout)
        else:
            r = requests.get(base + "/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _settings_llm() -> dict:
    """LLM-related keys from the settings file (empty when untouched)."""
    try:
        from settings import load_settings

        s = load_settings()
        return {
            "provider": s.get("llm_provider", "local"),
            "url": (s.get("llm_api_url") or "").strip(),
            "key": (s.get("llm_api_key") or "").strip(),
            "model": (s.get("llm_api_model") or "gpt-4o-mini").strip(),
        }
    except Exception:
        return {}


class LLM:
    def __init__(self):
        self.mode = "none"
        self.base: str | None = None  # OpenAI-style base incl. /v1
        self.api_key = ""
        self.api_model = ""
        self._proc: subprocess.Popen | None = None
        self._messages: list[dict] = []

    def reset(self):
        """Drop the cached backend so the next ask() re-resolves it."""
        self.base = None
        self.mode = "none"
        self.api_key = ""
        self.api_model = ""

    # --- lifecycle ---------------------------------------------------------
    def ensure(self) -> bool:
        if self.mode == "api" and self.base:
            return True  # key-based cloud endpoint: no local health probe
        if self.base and _healthy(self.base):
            return True

        # 1. explicit LLM_URL
        env = os.environ.get("LLM_URL", "").strip()
        if env:
            self.base, self.mode = env, "env"
            self.api_key, self.api_model = "", ""
            return True

        # 2. API key from the Settings page (OpenAI-compatible endpoint)
        cfg = _settings_llm()
        if cfg.get("provider") == "api" and cfg.get("key") and cfg.get("url"):
            base = cfg["url"].rstrip("/")
            if base.endswith("/chat/completions"):
                base = base[: -len("/chat/completions")]
            self.base = base
            self.api_key = cfg["key"]
            self.api_model = cfg["model"] or "gpt-4o-mini"
            self.mode = "api"
            return True

        # 2. existing llama-server (Polaris :8080 or our :8081)
        for port in (8080, 8081):
            cand = f"http://127.0.0.1:{port}/v1"
            if _healthy(cand):
                self.base, self.mode = cand, "llama-existing"
                return True

        # 3. own thin spawn — only if explicitly enabled (safe default: off)
        if os.environ.get("LLM_AUTOSTART", "0") not in ("0", "false"):
            if self._spawn():
                return True

        # 4. ollama
        oll = "http://127.0.0.1:11434"
        try:
            if requests.get(oll + "/api/tags", timeout=0.4).status_code == 200:
                self.base = oll + "/api"
                self.mode = "ollama"
                return True
        except Exception:
            pass

        self.base = None
        self.mode = "none"
        return False

    def _spawn(self) -> bool:
        if self._proc and self._proc.poll() is None:
            self.base = "http://127.0.0.1:8081/v1"
            self.mode = "llama-own"
            return True
        binary = _find_llama_server()
        model = _find_model()
        if not binary or not model:
            return False
        # Port 8081 avoids clobbering Polaris on 8080
        cmd = [
            str(binary),
            "-m", str(model),
            "--host", "127.0.0.1",
            "--port", "8081",
            "-c", "4096",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False
        # Wait for health (up to ~90s for 4B load)
        for _ in range(180):
            if _healthy("http://127.0.0.1:8081/v1", timeout=0.5):
                self.base = "http://127.0.0.1:8081/v1"
                self.mode = "llama-own"
                return True
            if self._proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def shutdown(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None
        self.base = None
        self.mode = "none"

    # --- chat --------------------------------------------------------------
    def ask(self, user_text: str, max_tokens: int = 256) -> str:
        if not self.ensure() or not self.base:
            return reply("cant_answer")

        if self.mode == "ollama":
            return self._ask_ollama(user_text, max_tokens)
        return self._ask_openai(user_text, max_tokens)

    def _ask_openai(self, user_text: str, max_tokens: int) -> str:
        base = self.base
        if not base:
            return reply("cant_answer")
        self._messages.append({"role": "user", "content": user_text})
        self._messages = self._messages[-8:]
        headers = (
            {"Authorization": f"Bearer {self.api_key}"}
            if self.mode == "api" and self.api_key
            else None
        )
        body = {
            "messages": [{"role": "system", "content": SYSTEM}] + self._messages,
            "max_tokens": max_tokens,
            "temperature": 0.4,
            "stream": False,
        }
        if self.mode == "api" and self.api_model:
            body["model"] = self.api_model
        try:
            r = requests.post(
                base.rstrip("/") + "/chat/completions",
                json=body,
                headers=headers,
                timeout=60,
            )
            r.raise_for_status()
            content = _pick_content(r.json())
            if not content:
                # One retry with explicit no-think hint for reasoning models
                self._messages.append({"role": "user", "content": "Answer directly without reasoning."})
                body["messages"] = [{"role": "system", "content": SYSTEM}] + self._messages
                body["temperature"] = 0.2
                r = requests.post(
                    base.rstrip("/") + "/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=60,
                )
                r.raise_for_status()
                content = _pick_content(r.json())
            self._messages.append({"role": "assistant", "content": content or reply("cant_answer")})
            return content or reply("cant_answer")
        except Exception:
            return reply("error")

    def _ask_ollama(self, user_text: str, max_tokens: int) -> str:
        base = self.base
        if not base:
            return reply("cant_answer")
        self._messages.append({"role": "user", "content": user_text})
        self._messages = self._messages[-8:]
        try:
            r = requests.post(
                base.rstrip("/") + "/chat",
                json={
                    "model": os.environ.get("OLLAMA_MODEL", "qwen3:4b"),
                    "messages": [{"role": "system", "content": SYSTEM}] + self._messages,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
                timeout=60,
            )
            r.raise_for_status()
            content = (r.json().get("message") or {}).get("content", "").strip()
            self._messages.append({"role": "assistant", "content": content})
            return content or reply("error")
        except Exception:
            return reply("error")


if __name__ == "__main__":
    llm = LLM()
    ok = llm.ensure()
    print(f"llm.ensure → {ok} mode={llm.mode} base={llm.base}")
    if ok:
        print("ask:", llm.ask("Say hello in five words."))
    else:
        print("ask (canned):", llm.ask("hello"))
    llm.shutdown()
    print("llm.demo OK")
