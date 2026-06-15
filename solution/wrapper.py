"""YOUR mitigation + observability layer. The simulator calls mitigate() around the
opaque agent (a REAL LLM) for every request. This is the ONLY place observability can
live -- the agent is silent. Legal moves: retry / cache / route / guardrail / sanitize
/ fallback / session-reset / PROMPT ROUTING, plus your own logging/tracing/metrics.
Illegal: hardcoding answers, importing the agent internals, reading instructor files,
network exfiltration.

  call_next(question, config) -> result   # the only way to reach the black box
  context = {"session_id","turn_index","qid","cache": <shared dict>, "cache_lock": <Lock>}
  result  = {"answer","status","steps","trace","meta":{latency_ms,usage,...}}

PROMPT ROUTING: you can override the agent's system prompt PER REQUEST by setting it in
the config you pass to call_next, e.g.:
    conf = dict(config); conf["system_prompt"] = my_better_prompt
    result = call_next(question, conf)
(Or just edit solution/prompt.txt for a single static prompt used on every request.)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

try:
    from telemetry.logger import logger, new_correlation_id, set_correlation_id
    from telemetry.cost import cost_from_usage
    from telemetry.redact import redact
except Exception:
    logger = None

    def new_correlation_id():
        return "req-local"

    def set_correlation_id(_cid):
        return None

    def cost_from_usage(_model, _usage):
        return 0.0

    def redact(text):
        return text, 0


_PROMPT_CACHE = None
_NOTE_RE = re.compile(
    r"(?is)(ghi\s*chu|note|notes?|instruction|system|developer|admin)\s*[:：].*?(?=(?:\bship\b|\bgiao\b|\btong\b|\btổng\b|$))"
)
_PRICE_HINT_RE = re.compile(
    r"(?is)(ignore|bỏ qua|bo qua|hãy|hay|must|system|developer|admin|override|price|giá|gia)\b[^,.!?;]*"
)


def _load_prompt():
    global _PROMPT_CACHE
    if _PROMPT_CACHE is not None:
        return _PROMPT_CACHE
    prompt_path = os.path.join(os.path.dirname(__file__), "prompt.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            _PROMPT_CACHE = f.read().strip()
    except Exception:
        _PROMPT_CACHE = ""
    return _PROMPT_CACHE


def _sanitize_question(question):
    cleaned = _NOTE_RE.sub(" [order note removed] ", question or "")
    cleaned = _PRICE_HINT_RE.sub(" [untrusted note removed] ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _cache_key(question, config):
    model = str((config or {}).get("model", ""))
    normalized = re.sub(r"\s+", " ", (question or "").casefold()).strip()
    payload = json.dumps({"model": model, "q": normalized}, ensure_ascii=False, sort_keys=True)
    return "obs:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _log(event, data):
    if logger:
        logger.log_event(event, data)


def mitigate(call_next, question, config, context):
    set_correlation_id(new_correlation_id())

    conf = dict(config or {})
    prompt = _load_prompt()
    if prompt:
        conf["system_prompt"] = prompt
    conf["temperature"] = min(float(conf.get("temperature", 0.2) or 0.2), 0.2)
    conf["loop_guard"] = True
    conf["redact_pii"] = True

    clean_question = _sanitize_question(question)
    cache = context.get("cache") or {}
    cache_lock = context.get("cache_lock")
    key = _cache_key(clean_question, conf)

    if conf.get("cache", {}).get("enabled", True) and cache_lock:
        with cache_lock:
            cached = cache.get(key)
        if cached:
            _log("CACHE_HIT", {"qid": context.get("qid"), "session": context.get("session_id")})
            return dict(cached)

    attempts = int((conf.get("retry") or {}).get("max_attempts", 2) or 2)
    backoff_ms = int((conf.get("retry") or {}).get("backoff_ms", 250) or 0)
    last = None

    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            result = call_next(clean_question, conf)
        except Exception as exc:
            result = {
                "answer": None,
                "status": "wrapper_error",
                "steps": 0,
                "trace": [],
                "meta": {"wrapper_exception": type(exc).__name__},
            }

        meta = result.get("meta", {}) or {}
        usage = meta.get("usage", {}) or {}
        answer, pii_count = redact(result.get("answer") or "")
        result["answer"] = answer
        last = result

        _log("AGENT_CALL", {
            "qid": context.get("qid"),
            "session": context.get("session_id"),
            "turn": context.get("turn_index"),
            "attempt": attempt,
            "status": result.get("status"),
            "steps": result.get("steps"),
            "wall_ms": int((time.time() - started) * 1000),
            "reported_latency_ms": meta.get("latency_ms"),
            "tokens": usage,
            "cost_usd": cost_from_usage(meta.get("model", conf.get("model", "")), usage),
            "tools_used": meta.get("tools_used", []),
            "pii_redactions": pii_count,
            "sanitized": clean_question != (question or ""),
        })

        if result.get("status") == "ok" and result.get("answer"):
            break
        if attempt < attempts and backoff_ms:
            time.sleep(backoff_ms / 1000.0)

    if conf.get("cache", {}).get("enabled", True) and cache_lock and last and last.get("status") == "ok":
        with cache_lock:
            cache[key] = dict(last)

    return last
