"""Tests for src/risk/positions.py + restart recovery in main.

The bug this guards: OPEN_POSITIONS used to vanish on restart, so
reconcile re-adopted the bot's own legs as wallet inventory at the
current mark — disarming brackets and losing the true entry price.
"""
import json
import os

import config
import main
from src.risk import positions as position_book


def _bot_leg(symbol="RAAPLUSDT", side="long", entry=100.0, current=100.0,
             size=1000.0):
    return {"symbol": symbol, "side": side, "size_usd": size,
            "entry_price": entry, "current_price": current, "pnl": 0.0,
            "usd": size}


def test_missing_file_is_empty_and_ok(tmp_path):
    target = os.path.join(str(tmp_path), "positions.json")
    book, ok = position_book.load_positions(target)
    assert ok is True and book == {}


def test_corrupt_file_reports_not_ok(tmp_path):
    target = os.path.join(str(tmp_path), "positions.json")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    book, ok = position_book.load_positions(target)
    assert ok is False and book == {}


def test_wrong_version_reports_not_ok(tmp_path):
    target = os.path.join(str(tmp_path), "positions.json")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write('{"version": 999, "positions": {}}')
    _, ok = position_book.load_positions(target)
    assert ok is False


def test_roundtrip_preserves_bot_legs(tmp_path):
    target = os.path.join(str(tmp_path), "positions.json")
    book = {"RAAPLUSDT": _bot_leg(entry=300.0, current=306.0),
            "BTCUSDT": _bot_leg(symbol="BTCUSDT", side="short",
                                entry=90000.0, current=89000.0, size=500.0)}
    assert position_book.save_positions(book, target) is True
    reloaded, ok = position_book.load_positions(target)
    assert ok is True
    assert reloaded["RAAPLUSDT"]["entry_price"] == 300.0
    assert reloaded["RAAPLUSDT"]["side"] == "long"
    assert reloaded["BTCUSDT"]["side"] == "short"
    assert reloaded["BTCUSDT"]["size_usd"] == 500.0


def test_save_filters_reconciled_and_load_drops_it(tmp_path):
    target = os.path.join(str(tmp_path), "positions.json")
    adopted = _bot_leg(symbol="BTCUSDT", entry=90000.0)
    adopted["reconciled"] = True
    book = {"RAAPLUSDT": _bot_leg(), "BTCUSDT": adopted}
    assert position_book.save_positions(book, target) is True
    # Only the bot leg is written; the adopted leg is rebuilt at boot.
    reloaded, ok = position_book.load_positions(target)
    assert ok is True and list(reloaded) == ["RAAPLUSDT"]
    # Even if an adopted leg somehow lands on disk, load never trusts it.
    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "positions": {
            "BTCUSDT": {"symbol": "BTCUSDT", "side": "long",
                        "size_usd": 500.0, "entry_price": 90000.0,
                        "reconciled": True}}}, fh)
    reloaded, ok = position_book.load_positions(target)
    assert ok is True and reloaded == {}


def test_coerce_drops_malformed_legs(tmp_path):
    target = os.path.join(str(tmp_path), "positions.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "positions": {
            "GOOD": {"symbol": "GOOD", "side": "long", "size_usd": 100.0,
                     "entry_price": 10.0},
            "NOSIZE": {"symbol": "NOSIZE", "side": "long", "size_usd": 0.0,
                       "entry_price": 10.0},
            "NOENTRY": {"symbol": "NOENTRY", "side": "long",
                        "size_usd": 100.0, "entry_price": 0.0},
            "JUNK": "not a dict"}}, fh)
    book, ok = position_book.load_positions(target)
    assert ok is True and list(book) == ["GOOD"]


def test_restore_populates_open_positions(tmp_path, monkeypatch):
    target = os.path.join(str(tmp_path), "positions.json")
    position_book.save_positions({"RAAPLUSDT": _bot_leg(entry=300.0)}, target)
    monkeypatch.setattr(config, "POSITIONS_FILE", target)
    main.OPEN_POSITIONS.clear()
    try:
        assert main.restore_positions() == 1
        assert main.OPEN_POSITIONS["RAAPLUSDT"]["entry_price"] == 300.0
    finally:
        main.OPEN_POSITIONS.clear()


def _boot_with_book(monkeypatch, tmp_path, book, snapshot):
    """Persist `book`, then run reconcile against `snapshot`."""
    pos_target = os.path.join(str(tmp_path), "positions.json")
    position_book.save_positions(book, pos_target)
    monkeypatch.setattr(config, "POSITIONS_FILE", pos_target)
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    log_target = os.path.join(str(tmp_path), "trades.jsonl")
    with open(log_target, "w", encoding="utf-8") as fh:
        fh.write("")
    monkeypatch.setattr(config, "LOG_FILE", log_target)
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "STABLE", "direction": "FLAT",
                                 "divergence_score": 0.0, "rtoken_change": 0.0,
                                 "crypto_change": 0.0, "rtoken_last": "306",
                                 "crypto_last": "90000"})
    monkeypatch.setattr(main, "get_positions", lambda **kw: dict(snapshot))
    monkeypatch.setattr(main.cli, "open_orders", lambda *a, **kw: [])
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    report = main.reconcile_startup(False)
    return report


def test_restored_bot_leg_is_not_readopted_and_bracket_survives(
        tmp_path, monkeypatch):
    # Bot bought RAAPL at 300 last run; broker now shows both that leg
    # and a separate BTC bag. RAAPL must keep its 300 entry and stay a
    # bot leg (brackets armed); only the BTC bag is adopted.
    book = {"RAAPLUSDT": _bot_leg(entry=300.0, current=300.0)}
    snapshot = {"RAAPLUSDT": {"usd": 1000.0}, "BTCUSDT": {"usd": 500.0},
                "__ok": True}
    report = _boot_with_book(monkeypatch, tmp_path, book, snapshot)
    try:
        raapl = main.OPEN_POSITIONS["RAAPLUSDT"]
        assert raapl["entry_price"] == 300.0  # not reset to the 306 mark
        assert raapl.get("reconciled") is not True  # still a bot leg
        btc = main.OPEN_POSITIONS["BTCUSDT"]
        assert btc.get("reconciled") is True  # genuinely external
        assert [a["symbol"] for a in report["adopted"]] == ["BTCUSDT"]
        # Bracket survives the restart: drive RAAPL 3% below entry.
        main.OPEN_POSITIONS["RAAPLUSDT"]["current_price"] = 291.0
        assert main._bracket_breach() == ("RAAPLUSDT", "stop")
    finally:
        main.OPEN_POSITIONS.clear()


def test_unreadable_book_falls_back_to_broker_rebuild(tmp_path, monkeypatch):
    pos_target = os.path.join(str(tmp_path), "positions.json")
    with open(pos_target, "w", encoding="utf-8") as fh:
        fh.write("corrupt{{{")
    monkeypatch.setattr(config, "POSITIONS_FILE", pos_target)
    main.OPEN_POSITIONS.clear()
    try:
        assert main.restore_positions() == 0  # warns, restores nothing
    finally:
        main.OPEN_POSITIONS.clear()
