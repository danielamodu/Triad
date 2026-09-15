# Triad — cross-asset execution agent (Bitget AI Hackathon S2)

rToken/crypto divergence agent on the Bitget UTA API via the `bgc` CLI.
Heartbeat loop: **3 signals → weighted decision → risk cage → execute → log**.

```
Triad/
├── main.py                        # heartbeat loop (300s, event wake on divergence)
├── config.py                      # env, symbols, risk limits, divergence threshold
├── src/
│   ├── cli.py                     # bgc subprocess wrapper (always --paper-trading)
│   ├── logger.py                  # append_log() -> logs/trades.jsonl
│   ├── signals/
│   │   ├── price_divergence.py    # RAAPLUSDT vs BTCUSDT 24h gap
│   │   ├── event_signal.py        # news keyword scan (NEUTRAL fallback)
│   │   └── sentiment_signal.py    # sentiment scan + funding-rate proxy
│   ├── decision/engine.py         # weighted_decision() 0.5/0.3/0.2
│   ├── risk/cage.py               # validate(): $1000 cap, 5% halt, no doubling
│   └── execution/executor.py      # execute() paper orders + positions()
└── logs/trades.jsonl              # trade log output
```

Pairs: Apple rToken `RAAPLUSDT` vs `BTCUSDT` (both SPOT).

## Setup (Windows PowerShell)

```powershell
$env:BITGET_API_KEY="demo-key"          # demo keys! (Demo mode -> API Management)
$env:BITGET_SECRET_KEY="demo-secret"
$env:BITGET_PASSPHRASE="demo-passphrase"
# or put them in a local .env file (never commit it)
python main.py --once   # single tick (testing)
python main.py          # loop: 300s sleep, instant wake on divergence
```

## Live mode (real money — triple-gated)

Paper is the default. Live requires ALL three, otherwise the run is
refused before any trading:

```powershell
$env:TRIAD_LIVE_OK="1"                  # 2nd key (in addition to --live)
python main.py --live                   # 1st key; 3rd key = no logs/KILL file
```

Every order carries a `triad-` client ID and, once filled, the order
detail is fetched: legs book the broker's `avgPrice` and fee (signal
price is the fallback), and each log entry records `mode`, `fees_usd`,
and intended vs executed size.

## Backtest (offline, deterministic)

```powershell
python -m backtest.harness            # cached candles, current thresholds
python -m backtest.harness --refresh  # refetch 1D candles (RAAPL/BTC, ~90d)
python -m backtest.harness --sweep    # also try alternate sizing cutoffs
```

Replays daily candles through the real scorer, sizing, and cage
(Groq excluded — non-deterministic; event/sentiment fixed neutral).
Fills at close, 0.1%/side. Latest 89-day run: 5 trades, 40% win rate,
profit factor 2.41, +$80 vs +$114 buy-and-hold; high-confidence bucket
3 trades +$101, mid bucket 2 trades −$38. n=5 is far too thin to tune
on — directionally supportive of the 0.6/0.8 cuts, nothing more.

Two real bugs found by the harness and fixed: the fallback scorer
halved crypto-outperformance votes so HEDGE could never fire on price
alone (roach motel), and PnL halts blocked exits, trapping positions.

## Decisions

`LONG_RTOKEN` (vote ≥ +0.30) · `HEDGE_CRYPTO` (≤ −0.30) ·
`EXIT` (≤ −0.60 + BEARISH event) · `HOLD` otherwise.
Weights: price_divergence 0.5, event 0.3, sentiment 0.2.

## Safety model

1. Paper is the default; live needs `--live` + `TRIAD_LIVE_OK=1` + no
   `logs/KILL`, otherwise the run is refused before any trading.
2. Risk cage tracks bot-opened exposure in a persisted ledger
   (`logs/risk_state.json`, atomic writes — survives restarts, unlike the
   append-only trade log): $1,000 max single position, no doubling, halt
   on 5% peak-to-current drawdown or 2% daily loss. Halts block entries
   only — exits always pass so a halt can never trap a position;
   `logs/KILL` manual halt.
3. Fail-closed: an unreadable risk ledger or 3 consecutive failed broker
   snapshots block ALL trading (including HOLD) until resolved.
4. Ticks: 300s calm interval, 60s floor even on divergence wake.
5. Every stage is guarded — a failed signal degrades to neutral, a
   crashed tick is logged, the loop never dies.
6. Logs scrub credential-shaped fields. Each entry records intended
   (`position_size_usd`) vs actually filled (`executed_notional_usd`)
   size plus the current `drawdown_pct`.
7. Pre-trade validation blocks unlisted/offline symbols, sub-minimum
   sizes, insufficient balances, and >1% price drift since the signal —
   before any order is placed.
8. Order settlement: one retry on retryable errors (same `triad-`
   client ID, so no duplicates); partial fills cancel the remainder and
   book what filled; broker-confirmed zero fills report NO_FILL.
9. Boot reconciles from the broker: unknown balances are adopted into
   tracking and seeded into the ledger (never the reverse); resting
   orders are reported, never touched.
10. Ledger gates see equity PnL (realized + unrealized); each entry logs
    `realized_pnl` and `equity_pnl` alongside `running_pnl`.
11. Groq is audited and leashed: every prompt + raw verdict goes to
    `logs/groq_trace.jsonl`, each verdict carries its fallback agreement,
    and 5 consecutive disagreements force the deterministic fallback
    for 10 ticks (drift breaker).
12. `python -m pytest tests` — 84 tests covering ledger math, cage gates,
    live routing, validation, settlement, startup, and tick wiring.

## Notes

- `AAPLOLUSDT` does not exist on Bitget; the Apple rToken is `RAAPLUSDT`.
- `bgc bitget-signal` is not a real command (no news tool in the CLI),
  so event/sentiment degrade gracefully: news → NEUTRAL, sentiment →
  funding-rate positioning proxy, both labelled in the log.
- rTokens (stock tokens) are **not tradable in the paper environment**:
  market data + instruments list them, but place-order rejects them
  ("does not exist"). Live stock-spot exists for eligible users/regions,
  so the rToken leg is signal-only on paper; the BTC leg executes.
