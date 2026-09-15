"""Tests for the weighted fallback scorer (deterministic side)."""
from src.decision.engine import rule_scores, weighted_decision


def _price(gap, direction=None):
    direction = direction or ("RTOKEN_OUTPERFORMING" if gap > 0
                              else "CRYPTO_OUTPERFORMING" if gap < 0
                              else "FLAT")
    return {"signal": "DIVERGENCE_DETECTED", "direction": direction,
            "divergence_score": gap, "rtoken_change": gap,
            "crypto_change": 0.0}


NEU = {"signal": "NEUTRAL", "confidence": 0.5}
NEG = {"sentiment": "neutral", "score": 0.5}
BEAR = {"signal": "BEARISH", "confidence": 0.9}


def test_price_vote_is_symmetric():
    assert rule_scores(_price(0.04), NEU, NEG)["final"] == \
        -rule_scores(_price(-0.04), NEU, NEG)["final"]


def test_long_and_hedge_equally_reachable_on_price():
    assert weighted_decision(_price(0.04), NEU, NEG)["decision"] == \
        "LONG_RTOKEN"
    # Regression: the old 0.5x crypto-out dampening capped the negative
    # vote at -0.25, so HEDGE could never fire on price alone.
    assert weighted_decision(_price(-0.04), NEU, NEG)["decision"] == \
        "HEDGE_CRYPTO"


def test_threshold_boundaries():
    assert weighted_decision(_price(0.03), NEU, NEG)["decision"] == \
        "LONG_RTOKEN"  # final exactly +0.30
    assert weighted_decision(_price(0.029), NEU, NEG)["decision"] == "HOLD"
    assert weighted_decision(_price(-0.03), NEU, NEG)["decision"] == \
        "HEDGE_CRYPTO"  # final exactly -0.30


def test_exit_needs_bearish_event():
    assert weighted_decision(_price(-0.10), BEAR, NEG)["decision"] == "EXIT"
    assert weighted_decision(_price(-0.10), NEU, NEG)["decision"] == \
        "HEDGE_CRYPTO"


def test_confidence_scales_with_conviction():
    weak = weighted_decision(_price(0.03), NEU, NEG)["confidence"]
    strong = weighted_decision(_price(0.05), NEU, NEG)["confidence"]
    assert 0.0 <= weak < strong <= 1.0
