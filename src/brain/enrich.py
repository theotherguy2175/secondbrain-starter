"""Enrichment: turn a raw item into search-friendly metadata.

Per the "cheap model writes the summaries" idea, each item gets:
  * summary   - a short description of what it is
  * questions - questions someone would type to find it (rephrased a few ways)
  * atoms     - exact errors, fixes, commands, decisions and their reasons

Three enrichers:
  heuristic (default) - no model, no network; extracts what it can.
  anthropic           - Messages API with a cheap model.
  openai              - any OpenAI-compatible endpoint (Ollama, vLLM, llama.cpp,
                        LM Studio) so sensitive data can stay on your network.

LLM enrichment sends item text to that provider. It is opt-in
(BRAIN_ENRICH=anthropic|openai) and can be limited to scopes with
BRAIN_ENRICH_SCOPES (default: all scopes). Items in other scopes fall back to
the heuristic enricher.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Protocol

MAX_CHARS = 12000

PROMPT = """You are indexing a personal knowledge base entry so it can be found later \
by keyword search. Read the entry and reply with ONLY a JSON object:
{{
  "summary": "1-3 sentences: what this is and why it matters",
  "questions": ["3-5 questions someone might type to find this, phrased differently"],
  "atoms": ["exact error messages, exact fixes, commands that worked, config values, \
and decisions with the reason behind them; one short line each; verbatim where possible"]
}}
Use only facts present in the entry. Empty lists are fine.

Title: {title}
Kind: {kind}

{body}"""


class Enricher(Protocol):
    def enrich(self, item: dict[str, Any]) -> dict[str, str]: ...


def _lines(values: Any) -> str:
    if isinstance(values, str):
        return values.strip()
    if isinstance(values, (list, tuple)):
        return "\n".join(str(v).strip() for v in values if str(v).strip())
    return ""


def parse_llm_json(text: str) -> dict[str, str]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in model reply")
    data = json.loads(m.group(0))
    return {
        "summary": str(data.get("summary", "")).strip(),
        "questions": _lines(data.get("questions")),
        "atoms": _lines(data.get("atoms")),
    }


class ClaudeCliEnricher:
    """Enrich via the Claude Code CLI (`claude -p`) on your own subscription: no API key.

    Runs tool-free, one turn, from a dedicated cwd so each call stays cheap and its own
    session logs are easy to exclude from ingestion. Set BRAIN_ENRICH_BIN to the CLI path
    and (optionally) BRAIN_ENRICH_CONFIG_DIR to a separate Claude config dir.
    """

    CWD = os.path.expanduser("~/.brain/enrich-cwd")

    def __init__(self, binary: str | None = None, model: str = "sonnet", timeout: int = 300):
        self.binary = os.path.expanduser(binary or os.environ.get("BRAIN_ENRICH_BIN", "claude"))
        self.model, self.timeout = model, timeout

    def enrich(self, item: dict[str, Any]) -> dict[str, str]:
        import subprocess
        os.makedirs(self.CWD, exist_ok=True)
        home = os.path.expanduser("~")
        prompt = PROMPT.format(title=item.get("title", ""), kind=item.get("kind", ""),
                               body=(item.get("body") or "")[:MAX_CHARS])
        env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR", "SHELL", "TERM")
               if k in os.environ}
        env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        if os.environ.get("BRAIN_ENRICH_CONFIG_DIR"):
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(os.environ["BRAIN_ENRICH_CONFIG_DIR"])
        excludes = [f"{home}/.claude/CLAUDE.md", f"{home}/AGENTS.md"]
        cmd = [self.binary, "-p", prompt, "--model", self.model, "--output-format", "json", "--tools", "",
               "--strict-mcp-config", "--disable-slash-commands",
               "--system-prompt", "You index knowledge-base entries. Reply with only the requested JSON.",
               "--settings", json.dumps({"claudeMdExcludes": excludes})]
        p = subprocess.run(cmd, cwd=self.CWD, env=env, capture_output=True, text=True, timeout=self.timeout)
        if p.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({p.returncode}): {p.stderr[:200]}")
        reply = json.loads(p.stdout)
        if reply.get("is_error"):
            raise RuntimeError(f"claude CLI error: {str(reply.get('result'))[:200]}")
        return parse_llm_json(str(reply.get("result", "")))


class HeuristicEnricher:
    """No-model fallback: lead text as summary, error/command lines as atoms."""

    ATOM_PATTERNS = re.compile(
        r"(error|exception|traceback|failed|fatal|denied|refused|timeout|"
        r"fix(ed)?:|solution|workaround|decid(ed|ing)|because|^\s*[$#] |^\s*(kubectl|docker|git|ssh|"
        r"systemctl|helm|apt|pip|npm|curl|sqlite3?) )",
        re.IGNORECASE,
    )

    def enrich(self, item: dict[str, Any]) -> dict[str, str]:
        body = item.get("body", "")
        text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
        text = " ".join(text.split())
        summary = text[:300].rstrip()
        if len(text) > 300:
            summary += "…"
        if not summary:
            summary = item.get("title", "")
        atoms: list[str] = []
        for block in re.findall(r"```[a-zA-Z]*\n(.*?)```", body, flags=re.DOTALL):
            first = next((ln.strip() for ln in block.splitlines() if ln.strip()), "")
            if first:
                atoms.append(first[:200])
        for line in body.splitlines():
            s = line.strip()
            if 8 < len(s) < 240 and self.ATOM_PATTERNS.search(s) and s not in atoms:
                atoms.append(s)
            if len(atoms) >= 12:
                break
        title = item.get("title", "")
        questions = f"{title}?" if title else ""
        return {"summary": summary, "questions": questions, "atoms": "\n".join(atoms[:12])}


class _HTTPEnricher:
    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    def _prompt(self, item: dict[str, Any]) -> str:
        return PROMPT.format(
            title=item.get("title", ""),
            kind=item.get("kind", ""),
            body=item.get("body", "")[:MAX_CHARS],
        )

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())


class AnthropicEnricher(_HTTPEnricher):
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com"):
        super().__init__()
        self.api_key, self.model, self.base_url = api_key, model, base_url.rstrip("/")

    def enrich(self, item: dict[str, Any]) -> dict[str, str]:
        data = self._post(
            f"{self.base_url}/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": 800,
             "messages": [{"role": "user", "content": self._prompt(item)}]},
        )
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return parse_llm_json(text)


class OpenAICompatEnricher(_HTTPEnricher):
    def __init__(self, base_url: str, model: str, api_key: str = ""):
        super().__init__()
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key

    def enrich(self, item: dict[str, Any]) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._post(
            f"{self.base_url}/chat/completions", headers,
            {"model": self.model, "max_tokens": 800, "temperature": 0,
             "messages": [{"role": "user", "content": self._prompt(item)}]},
        )
        return parse_llm_json(data["choices"][0]["message"]["content"])


class ScopedEnricher:
    """Use the LLM enricher only for allowed scopes; heuristic for the rest.
    Any LLM failure falls back to the heuristic so ingestion never stalls."""

    def __init__(self, primary: Enricher, allowed_scopes: set[str] | None):
        self.primary, self.allowed = primary, allowed_scopes
        self.fallback = HeuristicEnricher()
        self.last_error: str | None = None

    def enrich(self, item: dict[str, Any]) -> dict[str, str]:
        if self.allowed is None or item.get("scope") in self.allowed:
            try:
                out = self.primary.enrich(item)
                if out.get("summary"):
                    self.last_error = None
                    return out
            except Exception as e:  # network, bad JSON, auth...
                self.last_error = f"{type(e).__name__}: {e}"
        return self.fallback.enrich(item)


def get_enricher(env: dict[str, str] | None = None) -> Enricher:
    env = dict(os.environ) if env is None else env
    mode = env.get("BRAIN_ENRICH", "heuristic").lower()
    scopes = {s.strip() for s in env.get("BRAIN_ENRICH_SCOPES", "").split(",") if s.strip()} or None
    if mode == "anthropic":
        key = env.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("BRAIN_ENRICH=anthropic needs ANTHROPIC_API_KEY")
        model = env.get("BRAIN_ENRICH_MODEL", "claude-haiku-5-5")
        return ScopedEnricher(AnthropicEnricher(key, model, env.get("BRAIN_ENRICH_BASE_URL",
                                                                    "https://api.anthropic.com")), scopes)
    if mode == "claude-cli":
        return ClaudeCliEnricher(model=env.get("BRAIN_ENRICH_MODEL", "sonnet"))
    if mode == "openai":
        base = env.get("BRAIN_ENRICH_BASE_URL", "")
        model = env.get("BRAIN_ENRICH_MODEL", "")
        if not base or not model:
            raise RuntimeError("BRAIN_ENRICH=openai needs BRAIN_ENRICH_BASE_URL and BRAIN_ENRICH_MODEL")
        return ScopedEnricher(OpenAICompatEnricher(base, model, env.get("BRAIN_ENRICH_API_KEY", "")), scopes)
    return HeuristicEnricher()


def enrich_pending(store, enricher: Enricher, limit: int = 20) -> int:
    n = 0
    for item in store.needing_enrichment(limit):
        out = enricher.enrich(item)
        store.set_enrichment(item["id"], out["summary"], out["questions"], out["atoms"])
        n += 1
    return n
