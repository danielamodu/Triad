"""Stage 1 signal tests: expansion events and funding z-scores."""
from src.signals import event_signal
from src.signals.event_signal import expansion_event
from src.signals.sentiment_signal import _z_to_score, funding_zscore


def _hour(ts, o, h, lo, c, qvol):
    return [ts, o, h, lo, c, o * 10, qvol]


def _calm(n=50, vol=1000.0):
    return [_hour(i, 100.0, 100.5, 99.5, 100.1, vol) for i in range(n)]


def test_expansion_calm_is_neutral():
    out = expansion_event(_calm())
    assert out["signal"] == "NEUTRAL" and out["confidence"] == 0.5


def test_expansion_spike_bullish():
    rows = _calm() + [_hour(51, 100.0, 104.0, 99.0, 103.5, 5000.0)]
    out = expansion_event(rows)
    assert out["signal"] == "BULLISH"
    assert out["confidence"] > 0.5
    assert out["range_mult"] >= 1.5 and out["vol_mult"] >= 1.5


def test_expansion_spike_bearish():
    rows = _calm() + [_hour(51, 100.0, 101.0, 96.0, 96.5, 5000.0)]
    out = expansion_event(rows)
    assert out["signal"] == "BEARISH"


def test_expansion_range_without_volume_is_neutral():
    rows = _calm() + [_hour(51, 100.0, 104.0, 99.0, 103.5, 1000.0)]
    assert expansion_event(rows)["signal"] == "NEUTRAL"


def test_expansion_thin_history_neutral():
    assert expansion_event(_calm(10))["signal"] == "NEUTRAL"
    assert expansion_event([])["signal"] == "NEUTRAL"


def test_funding_zscore_crowded_longs():
    trail = [0.00005, 0.00007] * 45  # mean 6e-5, std 1e-5
    z, n = funding_zscore([0.0002] + trail)
    assert z > 3.0 and n == 91
    assert _z_to_score(z) < 0.1  # strongly bearish tilt


def test_funding_zscore_flat_is_zero():
    z, n = funding_zscore([0.00006] * 91)
    assert z == 0.0 and _z_to_score(z) == 0.5


def test_funding_zscore_thin():
    z, n = funding_zscore([0.0001] * 5)
    assert z == 0.0 and n == 5


RSS_DOC = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
           b'<title>CoinDesk</title>'
           b'<item><title>Bitcoin ETF approval rally continues</title></item>'
           b'<item><title>Ethereum upgrade scheduled next week</title></item>'
           b'</channel></rss>')


def test_rss_titles_skips_channel_title():
    assert event_signal._rss_titles(RSS_DOC) == [
        "Bitcoin ETF approval rally continues",
        "Ethereum upgrade scheduled next week"]
    assert event_signal._rss_titles(b"") == []
    assert event_signal._rss_titles(b"<not xml") == []


def test_get_event_prefers_rss_headlines(monkeypatch):
    monkeypatch.setattr(event_signal, "_fetch_rss",
                        lambda: "bitcoin ETF approval rally bull run")
    monkeypatch.setattr(event_signal, "_fetch_raw", lambda: "")

    def boom(*a, **kw):
        raise AssertionError("candles must not be fetched")
    monkeypatch.setattr(event_signal.cli, "candles", boom)
    out = event_signal.get_event()
    assert out["signal"] == "BULLISH"
    assert "rss" in out["reason"]


def test_get_event_falls_through_to_expansion(monkeypatch):
    monkeypatch.setattr(event_signal, "_fetch_rss", lambda: "")
    monkeypatch.setattr(event_signal, "_fetch_raw", lambda: "")
    monkeypatch.setattr(event_signal.cli, "candles",
                        lambda *a, **kw: _calm())
    assert event_signal.get_event()["signal"] == "NEUTRAL"


def test_classify_scales_conviction_with_evidence():
    assert event_signal._classify("") == ("NEUTRAL", 0.5)
    # Lone hit: a nudge, never full conviction.
    assert event_signal._classify("etf approval") == ("BULLISH", 0.6)
    assert event_signal._classify("hack") == ("BEARISH", 0.4)
    # Sustained drumbeat: full conviction either way.
    assert event_signal._classify("rally " * 6) == ("BULLISH", 1.0)
    assert event_signal._classify("crash " * 6) == ("BEARISH", 0.0)
