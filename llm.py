"""
llm.py — the LLM debate engine, plus provider auto-detection and the grounding
verifier.

Provider priority (override with LLM_PROVIDER):

  1. claude_code — the `claude` CLI on PATH. Shells out with
     `claude -p "<prompt>" --output-format json --model haiku`, stdin closed.
     Uses the user's own Claude subscription: no API key, no per-call billing.
  2. anthropic   — ANTHROPIC_API_KEY via the Messages API.
  3. openai      — OPENAI_API_KEY via Chat Completions.

Anything at all going wrong — no provider, no network, bad JSON, a refusal, a
timeout, an is_error envelope — raises, and app.py falls back to scoring.py.
The dashboard never blocks on this.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import scoring

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are the standing equity panel of a small Indian-markets research desk. "
    "Six seats debate one NSE-listed stock: Bull, Bear, Fundamentals, Technicals, "
    "News, and a Judge who closes the debate.\n\n"
    "The Judge rules TWICE, on two different questions:\n"
    "  • INTRADAY — is there a move to trade inside today's session, closed "
    "before the bell? This rests on the session tape only: VWAP, the opening "
    "range, the gap, time-adjusted RVOL, position in the day's range.\n"
    "  • POSITIONAL — is this worth holding for weeks or months? This rests on "
    "trend, the 52-week position, analyst headroom and news.\n"
    "The two can disagree, and often should: a stock can be extended intraday "
    "but attractive to hold, or ripping today with no lasting case.\n\n"
    "House rules, in order of importance:\n"
    "1. GROUNDING. Every single figure you cite must appear in the evidence JSON "
    "you are given. Never estimate, never annualise, never recall a number from "
    "training data, never invent a ratio. If a value you want is missing or null, "
    "write exactly 'data unavailable' and argue without it.\n"
    "2. This feed contains NO raw fundamental ratios (no P/E, P/B, ROE, margins, "
    "debt). The Fundamentals seat works only from sell-side targets and consensus, "
    "and must say so rather than pretend to a valuation view it cannot have.\n"
    "3. VERDICT BAR. BUY requires genuinely favourable risk/reward WITH "
    "confirmation — momentum and volume both pointing the same way, and real "
    "headroom left to the target. WATCH is for a promising setup that is not yet "
    "confirmed. AVOID is for poor risk/reward. A thin or contradictory evidence "
    "bundle is a WATCH or an AVOID, never a BUY.\n"
    "4. INTRADAY HONESTY. If the evidence says intraday.available is false, the "
    "session has not traded yet and there is NOTHING to read — return verdict "
    "UNAVAILABLE for the intraday track. Never infer today's tape from "
    "yesterday's bars. Never issue an intraday BUY without price above VWAP and "
    "above the opening range high.\n"
    "5. Do not state a holding period. It is computed arithmetically from the "
    "evidence and supplied to you; reference it if useful, never invent one.\n"
    "6. Be concise and specific. No hedging boilerplate, no disclaimers — the "
    "app adds its own.\n\n"
    "Return ONLY a JSON object. No markdown fence, no prose before or after."
)

RESPONSE_SHAPE = """{
  "bull":         {"conviction": 0-100, "point": "<=25 words"},
  "bear":         {"conviction": 0-100, "point": "<=25 words"},
  "fundamentals": {"conviction": 0-100, "point": "<=25 words"},
  "technicals":   {"conviction": 0-100, "point": "<=25 words"},
  "news":         {"conviction": 0-100, "point": "<=25 words"},
  "judge_intraday": {
    "winner": "Bull" | "Bear",
    "verdict": "BUY" | "WATCH" | "AVOID" | "UNAVAILABLE",
    "confidence": 1-10,
    "rationale": "<=2 lines, session tape only",
    "key_catalyst": "the single fact that decided it"
  },
  "judge_positional": {
    "winner": "Bull" | "Bear",
    "verdict": "BUY" | "WATCH" | "AVOID",
    "confidence": 1-10,
    "rationale": "<=2 lines, the multi-week case",
    "key_catalyst": "the single fact that decided it"
  }
}"""


# --------------------------------------------------------------------------
# provider detection
# --------------------------------------------------------------------------

def claude_cli_path():
    """Absolute path to the `claude` CLI, or None."""
    return shutil.which("claude")


def detect_provider(env=None) -> dict:
    """
    Work out which engine we will actually use.

    Returns {"provider", "model", "label", "reason"}; provider is one of
    claude_code | anthropic | openai | deterministic.
    """
    env = env if env is not None else os.environ
    forced = (env.get("LLM_PROVIDER") or "").strip().lower()

    cli = claude_cli_path()
    anthropic_key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    openai_key = (env.get("OPENAI_API_KEY") or "").strip()

    def claude_code():
        model = (env.get("CLAUDE_CLI_MODEL") or "haiku").strip() or "haiku"
        return {"provider": "claude_code", "model": model,
                "label": f"claude cli ({model})",
                "reason": f"claude CLI found at {cli}"}

    def anthropic():
        model = (env.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001").strip()
        return {"provider": "anthropic", "model": model,
                "label": f"anthropic api ({model})", "reason": "ANTHROPIC_API_KEY set"}

    def openai():
        model = (env.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
        return {"provider": "openai", "model": model,
                "label": f"openai api ({model})", "reason": "OPENAI_API_KEY set"}

    if forced == "claude_code":
        if cli:
            return claude_code()
        return _no_llm("LLM_PROVIDER=claude_code but the claude CLI is not on PATH")
    if forced == "anthropic":
        if anthropic_key:
            return anthropic()
        return _no_llm("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty")
    if forced == "openai":
        if openai_key:
            return openai()
        return _no_llm("LLM_PROVIDER=openai but OPENAI_API_KEY is empty")

    if cli:
        return claude_code()
    if anthropic_key:
        return anthropic()
    if openai_key:
        return openai()
    return _no_llm("no claude CLI on PATH and no API key in .env")


def _no_llm(reason):
    return {"provider": "deterministic", "model": None,
            "label": "deterministic", "reason": reason}


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

def build_prompt(evidence: dict) -> str:
    gaps = evidence.get("data_gaps") or []
    gap_line = ", ".join(gaps) if gaps else "none — every field computed"
    trimmed = {k: v for k, v in evidence.items()
               if k not in ("frame", "intraday_frame")}

    # Headlines are the bulkiest part of a live bundle and the tail adds little.
    # Keeping four keeps the prompt (and the latency) down without changing the
    # counts the News seat is allowed to cite, which live in news.total.
    news = trimmed.get("news")
    if isinstance(news, dict) and isinstance(news.get("recent"), list):
        trimmed["news"] = dict(news, recent=news["recent"][:4])

    phase = evidence.get("market") or {}
    intraday = evidence.get("intraday") or {}
    window = scoring.holding_window(evidence)

    if intraday.get("available"):
        session_line = (
            f"The session is LIVE and {phase.get('session_pct')}% elapsed, "
            f"{phase.get('minutes_to_close')} minutes to the close. Intraday figures "
            f"are real but still forming. RVOL has been scaled for time of day "
            f"({evidence.get('technicals', {}).get('rvol_method')})."
        )
    else:
        session_line = (
            f"There is NO live session behind this bundle ({phase.get('label')}): "
            f"{intraday.get('reason')}. The intraday track must return UNAVAILABLE."
        )

    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"=== MARKET CONTEXT ===\n{session_line}\n\n"
        f"=== HOLDING WINDOW (computed, do not restate a different one) ===\n"
        f"{window['label']} — {window['basis']}\n\n"
        f"=== EVIDENCE BUNDLE ({evidence.get('symbol')} — "
        f"{evidence.get('name')}, {evidence.get('cap_segment')} cap) ===\n"
        f"{json.dumps(trimmed, indent=2, default=str)}\n\n"
        f"Fields that could NOT be computed (treat as unknown, never fill in): {gap_line}\n\n"
        f"Hold the debate, then return exactly this JSON shape:\n{RESPONSE_SHAPE}\n"
    )


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

def _timeout(env=None):
    env = env if env is not None else os.environ
    try:
        return max(15, int(float(env.get("LLM_TIMEOUT") or 150)))
    except (TypeError, ValueError):
        return 150


def call_claude_code(prompt: str, model: str, env=None) -> str:
    """
    Shell out to the claude CLI and return the assistant's raw text.

    On Windows the npm shim is a .CMD, which CreateProcess will not launch
    directly in every Python build, so we route those through the comspec.
    """
    cli = claude_cli_path()
    if not cli:
        raise RuntimeError("claude CLI not on PATH")

    # Pass prompt via stdin rather than as a CLI argument to avoid Windows
    # cmd.exe special-character escaping and command-line length limits.
    argv = [cli, "-p", "--output-format", "json", "--model", model]
    if sys.platform == "win32" and cli.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c"] + argv

    # Run from a scratch directory. Launched inside this repo the CLI loads the
    # project as context — slower, billed for a cache the debate never uses,
    # and liable to answer about the codebase instead of the stock.
    with tempfile.TemporaryDirectory(prefix="dalaldesk-") as scratch:
        completed = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_timeout(env),
            check=False,
            cwd=scratch,
        )

    if completed.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[:300]}"
        )

    try:
        envelope = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"claude CLI returned non-JSON: {completed.stdout.strip()[:300]}"
        ) from exc

    if isinstance(envelope, list):                 # defensive: stream-json shape
        envelope = next((e for e in reversed(envelope)
                         if isinstance(e, dict) and "result" in e), {})

    if not isinstance(envelope, dict):
        raise RuntimeError("claude CLI envelope was not an object")
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI reported is_error: {str(envelope.get('result'))[:300]}")

    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("claude CLI envelope had no usable 'result'")
    return result


def call_anthropic(prompt: str, model: str, env=None) -> str:
    import requests

    env = env if env is not None else os.environ
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is empty")

    response = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1200,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=_timeout(env),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"anthropic api {response.status_code}: {response.text[:300]}")

    blocks = response.json().get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text.strip():
        raise RuntimeError("anthropic api returned no text")
    return text


def call_openai(prompt: str, model: str, env=None) -> str:
    import requests

    env = env if env is not None else os.environ
    key = (env.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is empty")

    response = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=_timeout(env),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"openai api {response.status_code}: {response.text[:300]}")

    choices = response.json().get("choices") or []
    text = (choices[0].get("message") or {}).get("content", "") if choices else ""
    if not text.strip():
        raise RuntimeError("openai api returned no text")
    return text


# --------------------------------------------------------------------------
# parsing + grounding verifier
# --------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Numbers that are part of a label rather than a cited measurement. "52-week
# range" and "20-day SMA" name a window; they are not figures the panel is
# claiming, so the verifier must not treat them as ungrounded.
_LABEL_RE = re.compile(
    r"\b\d+\s*-?\s*(?:week|day|month|year|yr|session)s?\b"
    r"|\bq[1-4]\b|\bfy\s*'?\d{2,4}\b|\b\d+\s*/\s*10\b|\bh[12]\b",
    re.IGNORECASE,
)


def extract_json(text: str) -> dict:
    """Pull the first well-formed JSON object out of a model response."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()

    try:
        return json.loads(text)
    except ValueError:
        pass

    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:index + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)

    raise RuntimeError(f"no JSON object found in model output: {text[:200]}")


def collect_evidence_numbers(evidence) -> set:
    """Every number that appears anywhere in the bundle, plus rounded variants."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.add(float(node))
        elif isinstance(node, str):
            for token in _NUMBER_RE.findall(node):
                try:
                    found.add(float(token))
                except ValueError:
                    pass

    walk(evidence)

    derived = set()
    for value in found:
        derived.update({abs(value), round(value), round(value, 1), round(value, 2)})
        if value:
            derived.add(round(abs(value)))
    return {float(v) for v in found | derived}


def verify_grounding(payload: dict, evidence: dict) -> list:
    """
    Flag any number in the model's prose that cannot be traced to the evidence.

    Returns a list of {"field", "value", "text"} — advisory, not fatal: the
    verdict still stands, but the run records that the panel quoted a figure
    nobody gave it.
    """
    allowed = collect_evidence_numbers(evidence)
    flagged = []

    def traceable(value):
        if abs(value) <= 10 and float(value).is_integer():
            return True                      # scores, confidences, small counts
        for known in allowed:
            if abs(known - value) <= 0.05:
                return True
            if known and abs(known - value) / abs(known) <= 0.01:
                return True
        return False

    texts = []
    for seat in ("bull", "bear", "fundamentals", "technicals", "news"):
        node = payload.get(seat)
        if isinstance(node, dict) and node.get("point"):
            texts.append((f"{seat}.point", str(node["point"])))
    for judge_key in ("judge_intraday", "judge_positional", "judge"):
        judge_node = payload.get(judge_key) or {}
        for field in ("rationale", "key_catalyst"):
            if judge_node.get(field):
                texts.append((f"{judge_key}.{field}", str(judge_node[field])))

    for field, text in texts:
        for token in _NUMBER_RE.findall(_LABEL_RE.sub(" ", text)):
            try:
                value = float(token)
            except ValueError:
                continue
            if not traceable(value):
                flagged.append({"field": field, "value": token, "text": text})
    return flagged


def normalise(payload: dict, evidence: dict, engine_label: str) -> dict:
    """Model JSON -> the same shape scoring.evaluate() returns."""
    def seat(name):
        node = payload.get(name)
        if not isinstance(node, dict):
            return {"score": 50, "reasons": ["seat returned nothing usable"]}
        try:
            score = int(round(float(node.get("conviction", 50))))
        except (TypeError, ValueError):
            score = 50
        point = str(node.get("point") or "").strip() or "no point offered"
        return {"score": max(0, min(100, score)), "reasons": [point]}

    scores = {name: seat(name) for name in
              ("bull", "bear", "fundamentals", "technicals", "news")}
    bull_score = scores["bull"]["score"]
    bear_score = scores["bear"]["score"]

    def track(key, allow_unavailable):
        node = payload.get(key)
        if not isinstance(node, dict):
            raise RuntimeError(f"model response had no {key} block")

        verdict = str(node.get("verdict") or "").strip().upper()
        allowed = ("BUY", "WATCH", "AVOID") + (("UNAVAILABLE",) if allow_unavailable else ())
        if verdict not in allowed:
            raise RuntimeError(f"model returned an unknown verdict for {key}: {verdict!r}")

        try:
            confidence = int(round(float(node.get("confidence", 5))))
        except (TypeError, ValueError):
            confidence = 5
        confidence = max(1, min(10, confidence))

        winner = str(node.get("winner") or "").strip().title()
        if winner not in ("Bull", "Bear"):
            winner = "Bull" if bull_score >= bear_score else "Bear"

        return {
            "track": key.replace("judge_", ""),
            "winner": winner,
            "verdict": verdict,
            "confidence": confidence,
            "rationale": str(node.get("rationale") or "").strip() or "no rationale returned",
            "key_catalyst": str(node.get("key_catalyst") or "").strip()
                            or scores[winner.lower()]["reasons"][0],
            "bull_score": bull_score,
            "bear_score": bear_score,
            "net": bull_score - bear_score,
        }

    positional = track("judge_positional", allow_unavailable=False)
    intraday = track("judge_intraday", allow_unavailable=True)

    # ---- horizon is arithmetic, never opinion ----------------------------
    window = scoring.holding_window(evidence)
    positional.update({
        "horizon": window["label"],
        "horizon_days_min": window["days_min"],
        "horizon_days_max": window["days_max"],
        "horizon_basis": window["basis"],
        "levels": scoring._positional_levels(evidence),
    })

    intraday = _gate_intraday(intraday, evidence)

    return {
        "scores": scores,
        "tracks": {"positional": positional, "intraday": intraday},
        "engine": engine_label,
        "ungrounded_numbers": verify_grounding(payload, evidence),
    }


def _gate_intraday(intraday: dict, evidence: dict) -> dict:
    """
    Deterministic guard rails the model cannot argue its way past.

    A language model can be talked into an intraday BUY by a persuasive-looking
    tape. These three conditions are structural, so they are enforced in code
    rather than left to the prompt:

      * no live session  -> UNAVAILABLE, always
      * not above VWAP and the opening range -> cannot be a BUY
      * too little of the session left to work -> cannot be a BUY
    """
    block = evidence.get("intraday") or {}
    phase = evidence.get("market") or {}

    if not block.get("available"):
        return scoring._intraday_unavailable(
            block.get("reason") or "no intraday session data", evidence)

    intraday.setdefault("levels", scoring._intraday_levels(evidence))
    minutes_left = phase.get("minutes_to_close") or 0
    intraday["minutes_to_close"] = minutes_left
    intraday["horizon"] = (f"same session — {minutes_left} min to close"
                           if minutes_left else "same session")
    intraday["horizon_days_min"] = 0
    intraday["horizon_days_max"] = 0
    intraday["horizon_basis"] = "intraday positions are closed before the bell by definition"

    if intraday["verdict"] != "BUY":
        return intraday

    above_vwap = (evidence.get("intraday", {}).get("price_vs_vwap_pct") or 0) > 0
    above_or = block.get("above_opening_range") is True
    rvol = (evidence.get("technicals") or {}).get("rvol")
    rvol_ok = rvol is not None and rvol >= scoring.INTRADAY_MIN_RVOL

    blockers = []
    if not above_vwap:
        blockers.append("price is not above VWAP")
    if not above_or:
        blockers.append("the opening range high is not cleared")
    if not rvol_ok:
        blockers.append(f"RVOL {rvol}x is under {scoring.INTRADAY_MIN_RVOL}x")
    if minutes_left < scoring.INTRADAY_MIN_MINUTES_LEFT:
        blockers.append(f"only {minutes_left} minutes remain in the session")

    if blockers:
        intraday["verdict"] = "WATCH"
        intraday["confidence"] = min(6, intraday["confidence"])
        intraday["rationale"] = (
            f"Panel argued a BUY, held back to WATCH by the desk rules: "
            f"{'; '.join(blockers)}. Original read: {intraday['rationale']}"
        )
        intraday["gated"] = True

    return intraday


# --------------------------------------------------------------------------
# public interface
# --------------------------------------------------------------------------

def evaluate(evidence: dict, provider: dict = None, env=None, log=None) -> dict:
    """
    Run the LLM debate for one stock.

    Same contract as scoring.evaluate(). Falls back to the deterministic panel
    on any failure, and says so in the returned `engine` / `fallback_reason`.
    """
    env = env if env is not None else os.environ
    say = log or (lambda _m: None)
    provider = provider or detect_provider(env)
    name = provider.get("provider")

    if name == "deterministic":
        out = scoring.evaluate(evidence)
        out["fallback_reason"] = provider.get("reason")
        return out

    prompt = build_prompt(evidence)
    try:
        if name == "claude_code":
            raw = call_claude_code(prompt, provider["model"], env)
        elif name == "anthropic":
            raw = call_anthropic(prompt, provider["model"], env)
        elif name == "openai":
            raw = call_openai(prompt, provider["model"], env)
        else:
            raise RuntimeError(f"unknown provider {name!r}")

        result = normalise(extract_json(raw), evidence, provider["label"])
        if result["ungrounded_numbers"]:
            say(f"{evidence.get('symbol')}: verifier flagged "
                f"{len(result['ungrounded_numbers'])} untraceable figure(s)")
        return result

    except subprocess.TimeoutExpired:
        reason = f"{name} timed out after {_timeout(env)}s"
    except Exception as exc:                                       # noqa: BLE001
        reason = f"{name} failed: {type(exc).__name__}: {str(exc)[:200]}"

    say(f"{evidence.get('symbol')}: {reason} — falling back to the rule engine")
    out = scoring.evaluate(evidence)
    out["fallback_reason"] = reason
    return out
