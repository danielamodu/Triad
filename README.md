# Triad — cross-asset execution agent (Bitget AI Hackathon S2, Track 3)

> The only trading agent that distrusts its own AI by design: every call
> is AI-decided, safety-checked, and audit-logged — and when the AI
> disagrees with the backup rules 5 times running, the backup rules take
> over automatically.

## TL;DR for judges

**What:** autonomous paper-trader for Apple stock-token vs BTC spreads —
3 signals → 1 AI decision → hard safety limits → both legs fire → every
call logged and inspectable. Live on EC2, dashboard on Vercel.

- Live dashboard: https://triadxbt.vercel.app (no login)
- Health: https://triadxbt.vercel.app/health · Calls: `/logs` ·
  Profit: `/equity` · Safety: `/risk`
- Verify in 30 seconds:
  `git clone https://github.com/danielamodu/Triad && cd Triad && python -m pytest tests -q`
  (116 tests, stdlib + pytest only)

Live snapshot (2026-09-16, paper):

| Metric | Value |
|---|---|
| Ticks / fills | 1411 / 42 |
| All-time bot P&L | −$13.89 |
| Safety halts hit | 0 (drawdown 0.4% vs 5% cap) |
| AI drift breaker | engaged in the wild (`groq_cooldown` seen live) |
| Loop | 300s countdown, triple wake, 60s floor |

## Track 3 submission mapping

| Handbook requirement | Where it lives |
|---|---|
| Thesis (no feature lists) | Thesis section below |
| Public repo + README | this repo (`pip install -r requirements.txt`, run: `python main.py --once`) |
| Paper log: timestamp, asset, direction, price, quantity, balance change | `logs/trades.jsonl`: per-leg fills (`symbol/side/fill_price/fill_value`) + `wallet_usd` + `balance_change_usd` every tick |
| Backtest with code | `python -m backtest.harness` (deterministic replay + walk-forward) |
| Demo | https://triadxbt.vercel.app |

## Thesis

**Problem:** stock-token/crypto spreads drift apart intraday, but humans
can't watch both tapes 24/7 — and bots that watch them trade without
guardrails. **Approach:** fuse three inputs (price divergence, live news
headlines, positioning sentiment) into one AI decision per tick, gated by
hard safety limits, with a deterministic fallback that seizes control
when the AI drifts. **Proof:** live paper-trading with every call logged
(signals, decider, safety verdict, fills) plus a deterministic replay
harness. **Use case:** always-on, auditable spread execution for
tokenized-stock/crypto pairs — and the cage + dashboard + harness as
reusable agent infra.

## Known limitations (read before judging)

- rToken legs are **signal-only on paper**: market data lists them but
  place-order rejects them, so only the BTC leg fills. Live stock-spot
  exists for eligible regions; the strategy is built for it.
- Backtest sample is thin (n≈5): supports the sizing cuts
  directionally, nothing more. Walk-forward holds, same caveat.
- Sentiment is a funding-rate positioning proxy, not an X firehose —
  labelled as such in every log entry.
- Paper P&L starts near zero by construction (adopted funds enter at
  the mark); judge the machinery, not the balance.

---

rToken/crypto divergence agent on the Bitget UTA API via the `bgc` CLI.
Decision loop: **3 signals → AI decision → safety limits → execute → log**.
Three reasons to wake early: price divergence, high-conviction event,
extreme sentiment. Both legs fire simultaneously.
(Code names: decision loop = `HEARTBEAT_INTERVAL` tick in `main.py`;
safety limits = risk cage in `src/risk/cage.py`.)

Dashboard words used below: **Trades** (positions), **AI check** (Groq trust),
**Past calls** (decision tape), **Safety limits** (risk cage),
**Profit chart** (equity curve).

```
Triad/
├── main.py                        # decision loop (300s countdown, triple wake: divergence/event/sentiment)
├── config.py                      # env, symbols, safety limits, divergence threshold
├── src/
│   ├── cli.py                     # bgc subprocess wrapper (always --paper-trading = practice mode)
│   ├── logger.py                  # append_log() -> logs/trades.jsonl (past calls)
│   ├── signals/
│   │   ├── price_divergence.py    # RAAPLUSDT vs BTCUSDT 24h gap
│   │   ├── event_signal.py        # RSS crypto headlines + expansion events
│   │   └── sentiment_signal.py    # sentiment scan + funding-rate proxy
│   ├── decision/engine.py         # AI decision (Groq) + backup-rules fallback 0.5/0.3/0.2
│   ├── risk/cage.py               # safety limits: $1000 cap, 5% halt, no doubling
│   └── execution/executor.py      # execute() practice orders + trades()
└── logs/trades.jsonl              # trade log output (past calls)
```

Pairs: Apple rToken `RAAPLUSDT` vs `BTCUSDT` (both SPOT).

## Setup (Windows PowerShell)

```powershell
pip install -r requirements.txt      # dotenv + pytest (groq optional)
$env:BITGET_API_KEY="demo-key"          # demo keys! (Demo mode -> API Management)
$env:BITGET_SECRET_KEY="demo-secret"
$env:BITGET_PASSPHRASE="demo-passphrase"
# or put them in a local .env file (never commit it)
python main.py --once   # single tick (testing)
python main.py          # loop: 300s sleep, instant wake on divergence
```

## Live mode (real money — triple-gated)

Practice is the default. Live requires ALL three, otherwise the run is
refused before any trading:

```powershell
$env:TRIAD_LIVE_OK="1"                  # 2nd key (in addition to --live)
python main.py --live                   # 1st key; 3rd key = no logs/KILL file
```

Every order carries a `triad-` client ID and, once filled, the order
detail is fetched: open trades book the broker's `avgPrice` and fee (signal
price is the fallback), and each log entry records `mode`, `fees_usd`,
and intended vs executed size.

## Backtest (offline, deterministic)

```powershell
python -m backtest.harness                          # cached candles, current thresholds
python -m backtest.harness --refresh                # refetch 1D candles (RAAPL/BTC, ~90d)
python -m backtest.harness --sweep                  # also try alternate sizing cutoffs
python -m backtest.harness --with-overlays          # replay event + sentiment from history
python -m backtest.harness --with-overlays --split 0.7           # walk-forward train/test
python -m backtest.harness --spread-bps 1 --slip-bps 2           # fuller cost model
```

Replays daily candles through the real scorer, sizing, and safety
limits (`src/risk/cage.py:validate`, code name "cage")
(AI excluded — non-deterministic). Fills at close, 0.1%/side plus
optional spread/slippage. Latest 89-day run: 5 trades, 40% win rate,
profit factor 2.41, +$80 vs +$114 buy-and-hold; high-confidence bucket
3 trades +$101, mid bucket 2 trades −$38. n=5 is far too thin to tune
on — directionally supportive of the 0.6/0.8 cuts, nothing more.
Walk-forward (@0.7 split) holds up out-of-sample, same caveat.

Stage-1 signals (all replayable, all behind `--with-overlays`):
event = 1H volume+range expansion vs 48h medians (4 events in-sample);
sentiment = funding-rate z-score vs trailing ~30d (active 61/89 bars)
plus perp-vs-spot basis. In-sample, the overlays demote the two
high-bucket winners to mid (train +$38 → −$0): at current weights they
dilute conviction rather than add it. Weights stay uncalibrated until
the trade count justifies it — the neutral baseline remains the
default replay, and live votes are logged per-tick for the soak to
judge. Endpoint lessons baked in: `fundingRateHistory` hangs under
`--paper-trading` (reads go direct), `candlesHistory` needs explicit
time bounds in ≤90-day pages, and no empty fetch is ever cached.

Two real bugs found by the harness and fixed: the backup-rules scorer
halved crypto-outperformance votes so HEDGE could never fire on price
alone (roach motel), and profit halts blocked exits, trapping trades.

## Decisions (Past calls show who picked each one)

`LONG_RTOKEN` (vote ≥ +0.30) · `HEDGE_CRYPTO` (≤ −0.30) ·
`EXIT` (≤ −0.60 + BEARISH event) · `HOLD` otherwise.
Weights: price_divergence 0.5, event 0.3, sentiment 0.2.
Decided by **AI** (Groq model `openai/gpt-oss-20b`) or **Backup rules**
(deterministic weighted fallback) — the Past-calls tape labels each
pick. Safety check passed = safety limits approved the pick.

## Safety model (dashboard: Safety limits — "within safe limits" when watching)

1. Practice (paper trading) is the default; live needs `--live` +
   `TRIAD_LIVE_OK=1` + no emergency-stop file (`logs/KILL`),
   otherwise the run is refused before any trading.
   Dashboard shows this as **Live · practice**.
2. Safety limits track money in play (bot-opened `exposure`) in a
   persisted ledger (`logs/risk_state.json`, atomic writes — survives
   restarts, unlike the append-only trade log): $1,000 max single
   bet, no doubling, halt on 5% drop from peak (`drawdown_pct`,
   peak-to-current) or 2% daily loss. Pre-existing wallet funds found
   at boot are recorded separately (`adopted` baseline) so the
   money-in-play card shows gross open inventory (everything the bot
   has working, adopted funds included); `exposure_bot` in the API is
   the bot-deployed net. Profit and the profit curve count bot
   performance only — ledgers seeded before this existed get the
   baseline backfilled once as ledger-minus-logged-fills. Gates still
   read the full ledger. Halts block new
   entries only — exits always pass (safety check passed) so a halt
   can never trap a trade; the emergency-stop file (`logs/KILL`) is
   the manual halt.
3. Fail-closed: an unreadable safety ledger or 3 consecutive failed
   orders snapshots (code: `broker_fail_streak`, dashboard: **failed
   orders**) block ALL trading (including HOLD) until resolved.
4. Next decision countdown: 300s calm interval, 60s floor even on
   divergence wake. (Code name: `HEARTBEAT_INTERVAL`.)
5. Every stage is guarded — a failed signal degrades to neutral, a
   crashed tick is logged, the loop never dies.
6. Logs scrub credential-shaped fields. Each entry records intended
   bet (`position_size_usd`, dashboard: **size**) vs actually filled
   (`executed_notional_usd`) plus the current drop from peak
   (`drawdown_pct`).
7. Pre-trade safety check blocks unlisted/offline symbols (dashboard:
   **asset**), sub-minimum sizes, insufficient balances, and >1%
   price drift since the signal — before any order is placed.
8. Order settlement: one retry on retryable errors (same `triad-`
   client ID, so no duplicates); partial fills cancel the remainder and
   book what filled; broker-confirmed zero fills report NO_FILL.
9. Boot reconciles from the broker: unknown balances are adopted into
   tracking and seeded into the ledger (never the reverse); resting
   open orders (dashboard: **open**) are reported, never touched.
10. Displayed profit is bot-attributed all-time P&L: realized closes
    plus unrealized on bot-opened legs only (`equity_pnl`, dashboard:
    **profit** / **profit so far**; adopted wallet drift excluded).
    Gates deliberately keep the full-ledger basis. Each entry also
    logs full-wallet `running_pnl` for diagnostics, plus
    `realized_pnl`.
11. AI is audited and leashed (dashboard: **AI check**): every prompt +
    raw verdict goes to `logs/groq_trace.jsonl` (See details), each
    verdict carries its backup-rules agreement and answer speed
    (`latency_ms`), and 5 consecutive AI-vs-backup disagreements force
    the backup rules for 10 ticks (drift breaker).
12. `python -m pytest tests` — 116 tests covering ledger math, safety
    gates, live routing, validation, settlement, startup, and tick
    wiring.

## Notes

- `AAPLOLUSDT` does not exist on Bitget; the Apple rToken is `RAAPLUSDT`.
- `bgc bitget-signal` is not a real command (no news tool in the CLI),
  so event/sentiment degrade gracefully: news → NEUTRAL, sentiment →
  funding-rate positioning proxy, both labelled in the log.
- rTokens (stock tokens) are **not tradable in the practice environment**:
  market data + instruments list them, but place-order rejects them
  ("does not exist"). Live stock-spot exists for eligible users/regions,
  so the rToken bet is signal-only in practice; the BTC bet executes.

## Dashboard map (same panels, readable names)

Trades (positions) · AI check (Groq trust) · Past calls (decision tape) ·
Safety limits (risk cage) · Profit chart (equity curve). Cards: profit
(`equity_pnl`), money in play (`exposure`), Live · practice, next
decision countdown (heartbeat), within safe limits. Rows read:
asset / bet / size / profit so far; open = legs. Past calls read:
picked by AI/backup, safety passed, profit — See details opens the
AI trace.
