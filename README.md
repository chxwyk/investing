# Smart Money Copy Bot

A Railway-ready Discord bot that **automatically discovers profitable public Solana
wallets**, independently verifies strict 24-hour and 7-day performance, rotates toward recent
Pump.fun memecoin activity every five minutes, reconstructs swaps from confirmed on-chain
transactions, mirrors every newly detected hot-wallet swap in PAPER mode, and records copy
results from quote-only Jupiter Swap V2 orders plus a conservative output buffer.

Automatic discovery uses the authorized Solana Tracker PnL V2 API—not Fomo scraping. It
paginates five strict leaderboard pages per window, refreshes the 24-hour pool every three
hours, caches an independently filtered 7-day pool for twelve hours, and intersects both
windows. The resulting candidates must then show recent on-chain Pump activity before
entering the 25-wallet hot set. A
reconnecting Solana/Helius WebSocket reduces detection latency while one-minute polling stays
enabled as a fallback.

The bot starts in **PAPER** mode. Live Jupiter spot execution exists, but remains locked
unless four separate controls are deliberately configured.

## What the bot does

- Pulls rolling 1-day and 7-day Solana trader leaderboards with public wallet addresses.
- Requires independent positive PnL, win rate, ROI, and trade-count evidence in both windows.
- Excludes arbitrage wallets, suspicious identity tags, hyperactive bot-like wallets, and
  one-token wonders through provider filters and local checks.
- Builds a pool of up to 100 strict candidates and rechecks recent on-chain activity every
  five minutes; only verified Pump traders occupy the 25-wallet hot set.
- Automatically adds new qualifiers and disables wallets that become inactive, stop passing
  both profit windows, lack Pump activity, or are outranked by stronger active candidates.
- Records every admission/removal reason, baseline/current rolling PnL, locally observed
  source-wallet PnL, and PAPER PnL attributable to that wallet.
- Backfills up to 24 hours of transactions when a wallet is first discovered.
- Throttles Solana RPC calls and retries temporary `429`/server failures with backoff.
- Uses one reconnecting wallet WebSocket when available, with polling retained as a recovery
  path so a stream failure does not silently stop monitoring.
- Retries temporary Solana Tracker leaderboard `5xx` failures before preserving the
  existing watchlist.
- Detects SOL/USDC/USDT-to-token buys and sells from wallet balance changes.
- Maintains an average-cost inventory for each tracked wallet.
- Calculates realized P&L, realized ROI, win rate, trade count, volume, and maximum drawdown.
- Scores traders on repeatability, ROI, 24-hour/7-day consistency, activity, and drawdown.
- In PAPER mode, evaluates every newly detected tracked-wallet buy for an immediate,
  guarded fake-money copy.
- Keeps a separate fake lot for each source wallet and mirrors its partial/full sells
  proportionally.
- Uses independent-wallet consensus and risk gates for alert/live strategy execution.
- Posts raw wallet activity and the matching paper fills to Discord.
- Adds one-tap Fomo, Pump.fun, Jupiter, DexScreener, and Solscan buttons to every token alert.
- Can mention one Discord user on every newly detected raw buy.
- Blocks suspicious, low-liquidity, concentrated, mintable, or freezable tokens when the
  required Jupiter safety metadata is available.
- Requests a quote-only Jupiter Swap V2 order for the configured copy size, without a wallet,
  signature, or real transaction.
- Rejects late entries when the executable route has drifted too far above the source-wallet
  price, has excessive price impact, or arrives too slowly.
- Records the winning Jupiter router, route fee, price impact, entry drift, and quote latency.
- Applies a conservative output buffer and quotes exits too, so weak sell liquidity appears in
  PAPER P&L instead of being hidden by a clean reference price.
- Protects every raw-mirror paper lot with an independent hard stop, take-profit,
  trailing-profit lock, and maximum-hold exit while still mirroring source-wallet sells.
- Tracks equity, realized/unrealized P&L, win rate, maximum drawdown, profit factor,
  average win/loss, expectancy, and rolling 24-hour realized P&L.
- Optionally executes personal-wallet **spot** swaps through Jupiter Swap API V2.

## What it deliberately does not do

- It does not log into, scrape, reverse-engineer, or control a Fomo account.
- It does not map Fomo usernames to wallets or copy Fomo's leaderboard. Fomo does not expose
  documented public API/webhook credentials in this project; this bot uses an API intended
  for programmatic Solana wallet discovery instead. Fomo-native alerts or legitimately
  obtained public wallet addresses remain usable without scraping.
- It does not promise profit. The paper scoreboard exists specifically to prove or reject
  the strategy with evidence.
- It does not trade perpetual futures, borrow funds, or use leverage.
- It never asks for a seed phrase in Discord or chat.

## Paper raw-mirror strategy

`PAPER_MIRROR_RAW_SWAPS=true` is the default. After a wallet's initial bootstrap finishes,
every new raw BUY alert is evaluated automatically. With the default
`PAPER_RAW_ENTRY_FILTER_ENABLED=true`, a buy must have a current price, be fresh, fit the
daily-loss/open-position limits, and pass any available Jupiter safety metadata. An allowed
entry spends `DEFAULT_COPY_USD`; repeated buys cannot push one wallet/token lot above
`MAX_COPY_USD`.

With `PAPER_USE_EXECUTABLE_QUOTES=true`, the paper fill requests Jupiter's quote-only
`GET /swap/v2/order` route for the exact configured size. It compares that route price with
the tracked wallet's transaction price. A buy is skipped when adverse entry drift exceeds
`MAX_ADVERSE_ENTRY_DRIFT_PERCENT`, price impact exceeds
`MAX_QUOTE_PRICE_IMPACT_PERCENT`, or quote latency exceeds `MAX_QUOTE_LATENCY_MS`. An
accepted fill uses the quoted output after `PAPER_QUOTE_OUTPUT_BUFFER_BPS`; it does not
double-subtract the older generic fee/slippage assumptions. `SIMULATED_FEE_BPS` and
`SIMULATED_SLIPPAGE_BPS` remain in use for the paper demo, consensus-paper flow, and legacy
mode.

Quote-only orders do not need a trading wallet or private key, but Jupiter requires
`JUPITER_API_KEY`. The route is much closer to a tradeable result than a spot price; it is
still not a confirmed fill because the bot does not sign or land a PAPER transaction.

The next raw SELL from that same wallet sells the matching fake lot proportionally while it
remains open. Separately, the paper risk manager can close the entire lot first at the raw
hard stop, take-profit, trailing-profit threshold, or maximum hold. A later source SELL will
then correctly show `SKIPPED` because the protected paper lot is already closed. Different
wallets remain separate even when they trade the same token.

Bootstrap history is recorded for scoring but is not purchased retroactively. Therefore a
sell can also show `SKIPPED` if its matching buy happened before raw mirroring was deployed.
A new detected buy must occur first. Set `PAPER_MIRROR_RAW_SWAPS=false` to restore the older
consensus-only paper behavior.

The v2.9.1 quote, rotation, and exit guardrails are intentionally configurable:

```text
PAPER_REQUIRE_CURRENT_PRICE=true
PAPER_RAW_ENTRY_FILTER_ENABLED=true
PAPER_USE_EXECUTABLE_QUOTES=true
PAPER_QUOTE_OUTPUT_BUFFER_BPS=50
MAX_ADVERSE_ENTRY_DRIFT_PERCENT=8
MAX_QUOTE_PRICE_IMPACT_PERCENT=2
MAX_QUOTE_LATENCY_MS=5000
MAX_CONSECUTIVE_QUOTE_FAILURES=5
RAW_MIRROR_STOP_LOSS_PERCENT=8
RAW_MIRROR_TAKE_PROFIT_PERCENT=20
RAW_MIRROR_TRAILING_ACTIVATION_PERCENT=8
RAW_MIRROR_TRAILING_STOP_PERCENT=4
RAW_MIRROR_MAX_HOLD_SECONDS=7200
DISCOVERY_MAX_WALLETS=25
ROTATION_REFRESH_SECONDS=300
ROTATION_MAX_IDLE_SECONDS=3600
ROTATION_MIN_RECENT_SWAPS=1
ROTATION_MIN_PUMP_SWAPS=1
ROTATION_REQUIRE_PUMP_ACTIVITY=true
REALTIME_WALLET_STREAM_ENABLED=true
```

These values are hypotheses to validate in PAPER mode, not optimized or guaranteed-profit
settings. Use `/smartmoney paper`, `/smartmoney positions`, `/smartmoney paper-trades`, and
`/smartmoney readiness` to evaluate them before changing size.

## Official PAPER readiness trial

After deploying v2.9.1, run `/smartmoney paper-reset confirmation:RESET PAPER` once to begin a
clean trial. `/smartmoney readiness` reports **KEEP TESTING** until all defaults pass:

- 14 separate active test days;
- at least 100 quote-based exits;
- positive expectancy and profit factor of at least 1.25;
- trial maximum drawdown no higher than 10%; and
- at least 95% quote-request reliability.

Historical replay can help find obvious strategy bugs faster, but it cannot reproduce the
future route, latency, failed landing, liquidity, and wallet-selection conditions this bot will
face. Coding cannot honestly compress 14 forward-observation days into one afternoon. Passing
the report means review a tiny live pilot; it never guarantees a daily profit or automatically
unlocks LIVE mode.

## Consensus and live strategy

The raw 24-hour profit leaderboard is not the signal. A single lucky token can put a risky
wallet at the top. This bot uses a risk-adjusted pipeline:

1. Refresh authorized strict 24-hour and 7-day wallet leaderboards.
2. Intersect both windows so a one-day spike alone cannot qualify a wallet.
3. Verify recent Pump-native or graduated-Pump activity on-chain.
4. Re-rank the hot set every five minutes and rotate inactive or deteriorating wallets.
5. Ingest confirmed swaps for each watched wallet.
6. Reconstruct inventory and realized results in chronological order.
7. Blend provider metrics with locally reconstructed performance.
8. Ignore wallets below `MIN_TRADER_SCORE`.
9. For consensus/live execution, require `CONSENSUS_MIN_TRADERS` unique wallets to buy the
   same mint within `CONSENSUS_WINDOW_SECONDS`; a qualified sell can trigger an exit so
   positions do not remain stuck waiting for full sell consensus.
10. Reject stale signals and unsafe token conditions.
11. Record the resulting fill including configured fees and slippage.

The defaults require two qualified traders within five minutes. Set
`CONSENSUS_MIN_TRADERS=3` for a strict three-wallet strategy.

## Local setup

Requirements: Python 3.12+, a Discord bot token, a Solana RPC URL, and a Solana Tracker API
key for automatic discovery. A Jupiter API key is required for v2.9.1 quote-shadow PAPER and
live mode because the current Swap V2 order endpoints require it.

## Automatic wallet discovery

Create a Solana Tracker Data API key at
<https://www.solanatracker.io/account/data-api>. The free tier supports leaderboard reads
within its quota. Store the key only in Railway Variables:

```text
SOLANA_TRACKER_API_KEY=...
AUTO_DISCOVERY_ENABLED=true
DISCOVERY_REFRESH_SECONDS=1200
DISCOVERY_7D_REFRESH_SECONDS=21600
DISCOVERY_CANDIDATE_PAGES=5
DISCOVERY_FETCH_LIMIT=100
DISCOVERY_MAX_WALLETS=25
DISCOVERY_MIN_24H_PNL_USD=100
DISCOVERY_MIN_WIN_RATE_PERCENT=55
DISCOVERY_MIN_ROI_PERCENT=3
DISCOVERY_MIN_TRADES=5
DISCOVERY_MAX_TRADES=250
DISCOVERY_MIN_CLOSED_TOKENS=2
DISCOVERY_MAX_SINGLE_TOKEN_PERCENT=70
DISCOVERY_MIN_7D_PNL_USD=300
DISCOVERY_MIN_7D_WIN_RATE_PERCENT=55
DISCOVERY_MIN_7D_ROI_PERCENT=5
DISCOVERY_MIN_7D_TRADES=10
DISCOVERY_MAX_7D_TRADES=1000
ROTATION_REFRESH_SECONDS=300
ROTATION_MAX_IDLE_SECONDS=3600
ROTATION_PROBE_TRANSACTIONS=6
ROTATION_MIN_RECENT_SWAPS=1
ROTATION_MIN_PUMP_SWAPS=1
ROTATION_REQUIRE_PUMP_ACTIVITY=true
REALTIME_WALLET_STREAM_ENABLED=true
```

The bot calls Solana Tracker's documented `GET /v2/pnl/leaderboard/top` endpoint with
`days=1` and `days=7`, strict PnL mode, arbitrage exclusion, concentration filtering, and
cursor pagination. Five pages widen each source pool before intersection. To remain inside a
2.5K monthly request budget, the runtime automatically clamps full multi-page refreshes to at
least three hours for 24H data and twelve hours for 7D data, even if older Railway variables
still contain `1200` and `21600`. Every five minutes the cached intersection is checked
against recent on-chain swaps. Existing manually added wallets are never removed by automatic
rotation.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Load the `.env` variables through your shell or deployment platform, then run:

```bash
python -m smart_money_bot
```

The bot needs these Discord application permissions:

- `bot`
- `applications.commands`
- View Channels
- Send Messages
- Embed Links
- Use Application Commands

No privileged Discord gateway intents are required.

## Railway deployment

1. Create a new GitHub repository and upload this project.
2. Create a new Railway service from that repository.
3. Add a persistent volume mounted at `/data`.
4. Add the required variables from `.env.example`. Set `RAILWAY_RUN_UID=0` so the
   process can write to Railway's root-mounted volume.
5. Set `DISCORD_GUILD_ID` while testing so slash-command updates appear immediately.
6. Deploy. Railway uses the included `Dockerfile` and `railway.toml`.
7. In Discord run `/smartmoney setup`, followed by `/smartmoney discover`. Wallets are
   selected and rotated automatically; manual wallet entry is optional.

The minimum useful variables are:

```text
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_ALERT_CHANNEL_ID=...
DISCORD_ALERT_USER_ID=...
SOLANA_RPC_URL=...
RPC_REQUESTS_PER_SECOND=8
RPC_MAX_RETRIES=4
SOLANA_TRACKER_API_KEY=...
JUPITER_API_KEY=...
DATABASE_PATH=/data/smart_money.db
RAILWAY_RUN_UID=0
```

A provider RPC is required for reliable monitoring. The public Solana endpoint rate-limits
multi-wallet history scans. The defaults cap RPC traffic at eight requests per second,
retry temporary failures, scan once per minute, and backfill at most 100 transactions per
wallet. These settings favor free-tier stability over sub-minute alerts.

For free paper testing, create a Helius account, copy its Mainnet HTTPS RPC endpoint, and
store the complete endpoint only in Railway as `SOLANA_RPC_URL`. Keep the API key embedded
in that URL private. Helius documents the endpoint format as
`https://mainnet.helius-rpc.com/?api-key=YOUR_API_KEY`.

## Discord commands

| Command | Purpose |
|---|---|
| `/smartmoney setup` | Select the alert channel. |
| `/smartmoney discover` | Refresh strict 24H/7D data and run an immediate Pump rotation. |
| `/smartmoney hot-wallets` | Show each active wallet's metrics, admission evidence, observed PnL, and PAPER PnL. |
| `/smartmoney rotation` | Show recent admissions/removals and the exact reason for each. |
| `/smartmoney sources` | Show which discovery/platform/stream sources are actually connected. |
| `/smartmoney trader-add` | Optionally add a manual public-wallet override. |
| `/smartmoney trader-import` | Optionally bulk import `alias,wallet,weight` CSV rows. |
| `/smartmoney trader-remove` | Remove a tracked wallet. |
| `/smartmoney traders` | List monitored wallets. |
| `/smartmoney scan` | Run a scan immediately. |
| `/smartmoney leaderboard` | Show the 24-hour or 7-day risk-adjusted ranking. |
| `/smartmoney paper` | Show P&L, drawdown, profit factor, expectancy, and 24H progress. |
| `/smartmoney positions` | Show open paper positions. |
| `/smartmoney paper-trades` | Show recent fills, realized ROI, and automatic exit reasons. |
| `/smartmoney readiness` | Show the 14-day, sample-size, expectancy, drawdown, and quote gates. |
| `/smartmoney paper-demo` | Instantly create and close a clearly labeled fake paper trade. |
| `/smartmoney paper-reset` | Reset the paper challenge after exact confirmation. |
| `/smartmoney mode` | Show or set alerts, paper, or live mode. |
| `/smartmoney pause` | Pause/resume monitoring. |
| `/smartmoney kill-switch` | Immediately pause discovery, scanning, and new paper actions. |
| `/smartmoney status` | Check RPC and scanner health. |
| `/smartmoney limits` | Show active risk limits. |

Set `DISCORD_ALERT_USER_ID` to your numeric Discord user ID if raw wallet buys should
mention you. The bot uses restricted allowed-mention settings and never permits role or
`@everyone` pings. Every Solana token alert builds an exact Fomo coin link using Solana
`chainId=1399811149` and an exact Pump.fun coin link using the detected mint. Set
`FOMO_REFERRAL_CODE` to keep a referral code in generated Fomo links, or leave it blank to
omit the referral parameter. A link button only opens the detected coin; it never places or
authorizes a trade.

Mutation commands require Discord Administrator or a role listed in
`DISCORD_ADMIN_ROLE_IDS`.

### Instant paper walkthrough

The demo command lets you verify the full accounting flow without waiting for a real
multi-wallet signal and without changing the genuine consensus or risk rules:

1. Run `/smartmoney paper-demo action:open` to spend the configured fake copy size.
2. Run `/smartmoney positions` to see position value, unrealized P&L, and ROI.
3. Run `/smartmoney paper` to see account equity, total P&L, and total ROI.
4. Run `/smartmoney paper-demo action:close-win` to simulate a 30% market rise, or use
   `close-loss` to simulate a 12% market fall. Configured fees and slippage still apply.
5. Run `/smartmoney paper` again to see realized P&L, completed trades, and win rate.
6. Before a real observation period, erase demo history with
   `/smartmoney paper-reset confirmation:RESET PAPER`.
7. Run `/smartmoney readiness` during the official trial; do not treat a short green streak as
   proof.

`paper-demo` never calls a swap API, never accesses a private key, and cannot move funds.

## Fair bot-versus-human challenge

To compare this strategy against a human trader without moving the goalposts:

1. Reset the paper account immediately before the start.
2. Give both sides the same starting bankroll and start/end timestamps.
3. Keep fee and slippage assumptions enabled.
4. Do not add or remove watched wallets during the challenge.
5. Compare ending equity, realized P&L, maximum drawdown, win rate, and number of trades.

Ending P&L alone is not enough. A strategy that makes $100 while risking a $900 drawdown
is not automatically better than one that makes $70 with a $40 drawdown.

## Live-mode lock

Paper mode is the default. Live mode requires all of the following:

```text
ENABLE_LIVE_TRADING=true
LIVE_TRADING_ACK=I_UNDERSTAND_LIVE_TRADING_CAN_LOSE_ALL_FUNDS
JUPITER_API_KEY=...
TRADING_PRIVATE_KEY=...
```

Then an administrator must run `/smartmoney mode new_mode:live` and type `ENABLE LIVE`
as the confirmation.

Use a **new, low-balance hot wallet created only for this bot**. Never use a primary wallet.
Store its 64-byte private key only in Railway variables. Never paste it into Discord, a
support ticket, a screenshot, or chat. The default live base asset is Solana USDC.

## Important measurement limits

- The reconstructed ranking covers only swaps that the bot successfully ingests.
- Solana Tracker's 24-hour values use its PnL methodology and can differ from Fomo's
  leaderboard. They are used for discovery and bootstrap scoring, not as a profit promise.
- PnL momentum is the change between rolling-window snapshots; it can fall as old trades
  age out even when the wallet makes no new trade.
- Average-cost P&L cannot assign profit to tokens acquired before the available history;
  unmatched sells are recorded but excluded from realized-return calculations.
- SOL-denominated historical trades are converted using the price available when the bot
  processes them, so backfilled dollar P&L is approximate.
- Transfers, liquidity operations, multi-output transactions, and non-quote token swaps are
  intentionally ignored to avoid false copy signals.
- Quote-only Jupiter orders provide a current route and expected output before slippage, not a
  confirmed transaction. The output buffer makes PAPER more conservative, but priority fees,
  route expiry, failed landing, token-account rent, and price movement can still make live
  results worse.
- Paper stops are evaluated after each scanner cycle. Fast markets can gap through a threshold,
  so an 8% configured stop does not guarantee an 8% maximum loss.
- The v2.9.1 quote, rotation, raw-entry, and raw-lot guards are PAPER-only. Live mode remains
  the independent-wallet consensus spot strategy and is never enabled automatically by this
  upgrade.
- A wallet can pass every historical filter and lose immediately afterward. “Verified” means
  the reported past metrics and recent Pump activity passed the configured checks; it is not
  a promise of future profitability or proof of insider information.
- A source wallet can still dump after followers enter. Consensus and liquidity filters
  reduce this risk; they cannot remove it.

## Verification

```bash
ruff check .
pytest -q
```

The tests cover dual-window intersection, Pump activity admission, wallet rotation auditing,
WebSocket derivation, swap detection, score behavior, Swap V2 quote parsing, entry-drift and
price-impact rejection, quote-based round trips, readiness metrics, raw-lot caps,
hard/trailing/time exits, risk gating, database migration, and live-mode locking.
