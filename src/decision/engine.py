"""Decision engine: AI (Groq LLM) primary, backup-rules fallback.

decide(signals, positions=None, memory=None) returns
{decision, confidence, reasoning, scores, engine_used} where engine_used
is "groq" or "weighted_fallback". Any Groq failure (no key, timeout,
API error, bad JSON) falls back to weighted_decision() — the loop never
blocks on the LLM.

`memory` is a list of recent decision/outcome dicts (last 3 are
injected into the Groq prompt as "Recent decisions: {memory}").
"""
import json

import config

W_PRICE = 0.5
W_EVENT = 0.3
W_SENTIMENT = 0.2

LONG_T = 0.30
HEDGE_T = -0.30
EXIT_T = -0.60

VALID_DECISIONS = ("LONG_RTOKEN", "HEDGE_CRYPTO", "HOLD", "EXIT")
VALID_SIGNALS = ("price_divergence", "event", "sentiment")

PROMPT = (
    "You are Triad, an autonomous cross-asset trading agent managing "
    "rToken (RAAPLUSDT) and crypto (BTCUSDT) positions simultaneously.\n"
    "\n"
    "Current market signals:\n"
    "Price Divergence: {price_signal}\n"
    "Event Signal: {event_signal}\n"
    "Sentiment Signal: {sentiment_signal}\n"
    "\n"
    "Current positions: {positions}\n"
    "Risk parameters: Max position $1000, Max drawdown 5%\n"
    "\n"
    "Recent decisions: {memory}\n"
    "(last 3 decisions + outcomes; use for context across ticks — "
    "avoid repeating a failed decision, respect running outcomes)\n"
    "\n"
    "Decision definitions (the rToken basket is the divergence SIGNAL; "
    "every trade is EXECUTED in BTC on the spot market):\n"
    "LONG_RTOKEN = rTokens outperforming (bullish divergence) -> BUY BTC.\n"
    "HEDGE_CRYPTO = crypto outperforming (bearish divergence) -> SELL BTC.\n"
    "EXIT = flatten every open BTC leg (cover shorts, sell longs).\n"
    "HOLD = do nothing.\n"
    "Note: the rToken basket only signals direction; BTC is the leg that "
    "fills, so both bullish and bearish calls trade for real.\n"
    "\n"
    "Analyze the three signals and return ONLY a JSON object:\n"
    "{\n"
    '"decision": "LONG_RTOKEN" | "HEDGE_CRYPTO" | "HOLD" | "EXIT",\n'
    '"confidence": 0.0-1.0,\n'
    '"reasoning": "one sentence explaining the decision",\n'
    '"dominant_signal": "price_divergence" | "event" | "sentiment"\n'
    "}\n"
    "No other text. JSON only."
)


def _price_component(price_signal: dict) -> tuple:
    # Symmetric in the gap: a +-5pp divergence scores +-1.0 either way,
    # so LONG (+0.30) and HEDGE (-0.30) are equally reachable on price
    # alone (+-3pp). (A previous 0.5x dampening on crypto-outperformance
    # capped the negative vote at -0.25, making HEDGE unreachable without
    # a fully bearish event+sentiment: positions opened but price action
    # alone could never close them. Found by backtest, 2026-09-15.)
    if (price_signal or {}).get("signal") == "DIVERGENCE_DETECTED":
        gap = float(price_signal.get("divergence_score", 0.0))
        direction = price_signal.get("direction", "FLAT")
        mag = max(-1.0, min(1.0, gap / 0.05))
        return mag, "divergence " + format(gap, "+.3%") + " (" + direction + ")"
    return 0.0, "no divergence (stable)"


def _event_component(event_signal: dict) -> tuple:
    sig = str((event_signal or {}).get("signal", "NEUTRAL")).upper()
    try:
        conf = float((event_signal or {}).get("confidence", 0.5) or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    span = max(0.0, min(1.0, (conf - 0.5) * 2))
    if sig == "BULLISH":
        return span, "event BULLISH (" + format(conf, ".2f") + ")"
    if sig == "BEARISH":
        return -span, "event BEARISH (" + format(conf, ".2f") + ")"
    return 0.0, "event NEUTRAL"


def _sentiment_component(sentiment_signal: dict) -> tuple:
    try:
        score = float((sentiment_signal or {}).get("score", 0.5) or 0.5)
    except (TypeError, ValueError):
        score = 0.5
    score = max(0.0, min(1.0, score))
    label = str((sentiment_signal or {}).get("sentiment", "neutral"))
    return (score - 0.5) * 2, "sentiment " + label + " (" + format(score, ".2f") + ")"


def rule_scores(price_signal: dict, event_signal: dict,
                sentiment_signal: dict) -> dict:
    p, _ = _price_component(price_signal)
    e, _ = _event_component(event_signal)
    s, _ = _sentiment_component(sentiment_signal)
    final = round(W_PRICE * p + W_EVENT * e + W_SENTIMENT * s, 4)
    return {"price": round(p, 4), "event": round(e, 4),
            "sentiment": round(s, 4), "final": final}


def weighted_decision(price_signal: dict, event_signal: dict,
                      sentiment_signal: dict) -> dict:
    """Fallback scorer. Returns {decision, confidence, reasoning, scores}."""
    p, p_why = _price_component(price_signal or {})
    e, e_why = _event_component(event_signal or {})
    s, s_why = _sentiment_component(sentiment_signal or {})
    scores = rule_scores(price_signal, event_signal, sentiment_signal)
    final = scores["final"]

    event_sig = str((event_signal or {}).get("signal", "NEUTRAL")).upper()
    if final >= LONG_T:
        decision = "LONG_RTOKEN"
    elif final <= EXIT_T and event_sig == "BEARISH":
        decision = "EXIT"
    elif final <= HEDGE_T:
        decision = "HEDGE_CRYPTO"
    else:
        decision = "HOLD"

    confidence = round(min(1.0, abs(final) * 2), 3)
    reasoning = ("vote " + format(final, "+.3f") + " from price "
                 + format(p, "+.2f") + "*0.5 [" + p_why + "], event "
                 + format(e, "+.2f") + "*0.3 [" + e_why + "], sentiment "
                 + format(s, "+.2f") + "*0.2 [" + s_why + "] -> "
                 + decision)
    return {"decision": decision, "confidence": confidence,
            "reasoning": reasoning, "scores": scores}


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # drop opening fence (```json or ```)
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_groq_json(text: str) -> dict:
    """Parse + validate a Groq verdict. Raises ValueError on any problem."""
    data = json.loads(_strip_fences(text))
    if not isinstance(data, dict):
        raise ValueError("Groq response is not a JSON object")
    decision = str(data.get("decision", "")).upper()
    if decision not in VALID_DECISIONS:
        raise ValueError("bad decision: %r" % (data.get("decision"),))
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        raise ValueError("bad confidence: %r" % (data.get("confidence"),))
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(data.get("reasoning", "") or "")[:300]
    dominant = str(data.get("dominant_signal", "") or "")
    if dominant not in VALID_SIGNALS:
        dominant = "price_divergence"
    return {"decision": decision, "confidence": round(confidence, 3),
            "reasoning": reasoning, "dominant_signal": dominant}


def build_prompt(price_signal: dict, event_signal: dict,
                 sentiment_signal: dict, positions: dict,
                 memory=None) -> str:
    """Render the Groq prompt (pure; also used for the audit trace)."""
    # NOTE: plain .replace(), not .format() — the prompt contains
    # literal JSON braces that .format() would treat as fields.
    if isinstance(memory, list):
        mem_ctx = memory[-3:]
    elif isinstance(memory, dict):
        mem_ctx = memory
    elif memory:
        mem_ctx = memory
    else:
        mem_ctx = []
    return (PROMPT
            .replace("{price_signal}", json.dumps(
                price_signal or {}, separators=(",", ":")))
            .replace("{event_signal}", json.dumps(
                event_signal or {}, separators=(",", ":")))
            .replace("{sentiment_signal}", json.dumps(
                sentiment_signal or {}, separators=(",", ":")))
            .replace("{positions}", json.dumps(
                positions or {}, separators=(",", ":")))
            .replace("{memory}", json.dumps(
                mem_ctx, separators=(",", ":"))))


def _retry_after_seconds(exc) -> float:
    """Seconds to wait from a rate-limit (429) error, or -1.0 if `exc`
    is not a 429. Reads the Retry-After header when present, else a
    default backoff. Never raises."""
    try:
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__
        msg = str(exc).lower()
        is_429 = (status == 429 or name == "RateLimitError"
                  or "429" in msg or "rate limit" in msg
                  or "too many requests" in msg)
        if not is_429:
            return -1.0
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        try:
            raw = headers.get("retry-after", headers.get("Retry-After"))
        except Exception:
            raw = None
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        return float(config.GROQ_RETRY_BASE_SEC)
    except Exception:
        return -1.0


def _create_with_retry(client, prompt):
    """Call Groq once, with a single short retry on a 429.

    Honors Retry-After but never sleeps past GROQ_RETRY_CAP_SEC — a
    server-mandated cooldown longer than that (typically a daily-quota
    429) re-raises so the caller falls back for this tick instead of
    stalling the loop. Non-429 errors re-raise immediately.
    """
    import time
    attempts = max(0, int(config.GROQ_MAX_RETRIES)) + 1
    for attempt in range(attempts):
        try:
            return client.chat.completions.create(
                model=config.GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1024,
            )
        except Exception as exc:
            wait = _retry_after_seconds(exc)
            if (wait < 0 or attempt >= attempts - 1
                    or wait > config.GROQ_RETRY_CAP_SEC):
                raise
            time.sleep(wait)
    raise RuntimeError("unreachable: retry loop exited")  # pragma: no cover


def groq_decide(price_signal: dict, event_signal: dict,
                sentiment_signal: dict, positions: dict,
                memory=None) -> dict:
    """Call Groq. Raises on missing key, timeout, API error, bad JSON."""
    import time

    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError("groq package missing: pip install groq") from exc

    prompt = build_prompt(price_signal, event_signal, sentiment_signal,
                          positions, memory)
    client = Groq(api_key=config.GROQ_API_KEY,
                  timeout=config.GROQ_TIMEOUT_SEC)
    # NOTE: max_tokens (set in _create_with_retry) must leave headroom
    # for this model's hidden reasoning tokens — too small a budget
    # yields empty content. _create_with_retry adds one short 429 retry.
    started = time.monotonic()
    resp = _create_with_retry(client, prompt)
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    choice = resp.choices[0]
    text = choice.message.content or ""
    if not text.strip():
        raise ValueError(
            "empty Groq content (finish_reason=%s)"
            % getattr(choice, "finish_reason", "?"))
    verdict = parse_groq_json(text)
    verdict["scores"] = rule_scores(price_signal, event_signal,
                                    sentiment_signal)
    verdict["engine_used"] = "groq"
    verdict["latency_ms"] = latency_ms
    # Underscore keys feed the audit trace; main.py pops them before the
    # trade log so prompts don't bloat it.
    verdict["_prompt"] = prompt
    verdict["_raw_response"] = text[:2000]
    return verdict


def decide(signals: dict, positions: dict = None, memory=None,
           force_fallback: bool = False,
           fallback_reason: str = "groq cooldown (drift breaker)") -> dict:
    """Primary Groq path with weighted fallback.

    Returns {decision, confidence, reasoning, scores, engine_used,
    fallback_decision, fallback_agree}. The fallback is always computed
    (cheap) so callers can audit Groq against it. force_fallback skips
    the LLM entirely; fallback_reason records why (drift-breaker cooldown
    by default, or e.g. a calm tick the caller chose not to spend a call
    on). `positions` is optional so existing callers (main.py) keep
    working. `memory` is an optional list of recent decision/outcome
    dicts; the last 3 are passed to the Groq prompt for cross-tick context.
    """
    signals = signals or {}
    price = signals.get("price", {})
    event = signals.get("event", {})
    sentiment = signals.get("sentiment", {})
    fallback = weighted_decision(price, event, sentiment)
    if force_fallback:
        fallback["engine_used"] = "weighted_fallback"
        fallback["fallback_reason"] = fallback_reason
        fallback["fallback_decision"] = fallback["decision"]
        fallback["fallback_agree"] = True
        return fallback
    try:
        verdict = groq_decide(price, event, sentiment, positions or {},
                              memory or [])
        verdict["fallback_decision"] = fallback["decision"]
        verdict["fallback_agree"] = (verdict.get("decision")
                                     == fallback["decision"])
        return verdict
    except Exception as exc:
        fallback["engine_used"] = "weighted_fallback"
        fallback["fallback_reason"] = str(exc)[:160]
        fallback["fallback_decision"] = fallback["decision"]
        fallback["fallback_agree"] = True
        return fallback
