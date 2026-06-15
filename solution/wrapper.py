"""Mitigation and observability layer for Observathon.

The simulator calls mitigate() around the opaque agent. Legal moves used here:
prompt routing, retry, cache, input-note sanitization, output PII redaction,
structured telemetry, and arithmetic verification from the public tool trace.
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
    r"(?is)(ghi\s*chu|note|notes?|instruction|system|developer|admin)\s*[:：].*?(?=(?:\bship\b|\bgiao\b|\btong\b|\btinh\b|$))"
)
_QTY_RE = re.compile(r"(?i)\b(?:mua|dat|order)\s+(\d+)\b")
_CONTACT_TAIL_RE = re.compile(r"(?i)\s*\(?\s*(?:lien he|contact)[^)]*\)?\s*$")


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
    return re.sub(r"\s+", " ", cleaned).strip()


def _cache_key(question, config):
    model = str((config or {}).get("model", ""))
    normalized = re.sub(r"\s+", " ", (question or "").casefold()).strip()
    payload = json.dumps({"model": model, "q": normalized}, ensure_ascii=False, sort_keys=True)
    return "obs:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _log(event, data):
    if logger:
        logger.log_event(event, data)


def _requested_qty(question):
    match = _QTY_RE.search(question or "")
    return int(match.group(1)) if match else None


def _trace_obs(result, tool):
    for step in result.get("trace", []) or []:
        if step.get("tool") == tool:
            return step.get("observation") or {}
    return {}


def _clean_answer(answer):
    answer = answer or ""
    answer = re.sub(r"\s+\([^\)]*\[REDACTED[^\)]*\)\s*$", "", answer).strip()
    answer = _CONTACT_TAIL_RE.sub("", answer).strip()
    return answer


def _needs_shipping(question):
    q = (question or "").casefold()
    return "ship" in q or "giao" in q


def _deterministic_answer(question, result):
    stock = _trace_obs(result, "check_stock")
    if not stock:
        return _clean_answer(result.get("answer") or "")

    qty = _requested_qty(question)
    item = str(stock.get("item") or "San pham")
    unit_price = int(stock.get("unit_price_vnd") or 0)

    if not stock.get("found") or not stock.get("in_stock"):
        return f"{item} hien khong co san de dat mua."

    if qty is None:
        return f"{item} con hang. Gia: {unit_price} VND"

    available = stock.get("quantity")
    if isinstance(available, int) and qty > available:
        return f"{item} hien chi con {available}, khong du so luong {qty}."

    shipping = _trace_obs(result, "calc_shipping")
    if _needs_shipping(question) and "cost_vnd" not in shipping:
        return "Khong ho tro giao hang den dia diem nay."

    discount = _trace_obs(result, "get_discount")
    percent = int(discount.get("percent") or 0)
    shipping_cost = int(shipping.get("cost_vnd") or 0)
    total = unit_price * qty * (100 - percent) // 100 + shipping_cost
    return f"Tong cong: {total} VND"


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

    retry_conf = conf.get("retry") or {}
    attempts = int(retry_conf.get("max_attempts", 2) or 2)
    backoff_ms = int(retry_conf.get("backoff_ms", 250) or 0)
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
                "meta": {
                    "wrapper_exception": type(exc).__name__,
                    "wrapper_exception_message": str(exc)[:300],
                },
            }

        meta = result.get("meta", {}) or {}
        usage = meta.get("usage", {}) or {}
        answer = _deterministic_answer(clean_question, result)
        answer, pii_count = redact(answer)
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
            "wrapper_exception": meta.get("wrapper_exception"),
            "wrapper_exception_message": meta.get("wrapper_exception_message"),
            "trace_preview": result.get("trace", [])[:4] if os.getenv("OBS_DEBUG_TRACE") == "1" else None,
        })

        if result.get("status") == "ok" and result.get("answer"):
            break
        if attempt < attempts and backoff_ms:
            time.sleep(backoff_ms / 1000.0)

    if conf.get("cache", {}).get("enabled", True) and cache_lock and last and last.get("status") == "ok":
        with cache_lock:
            cache[key] = dict(last)

    return last
