# Smart Money Copy Bot

A Railway-ready Discord bot that **automatically discovers profitable public Solana
wallets**, reconstructs their swaps from confirmed on-chain transactions, ranks their recent
performance, produces multi-wallet consensus signals, and records copy results after
simulated fees and slippage.

Automatic discovery uses the authorized Solana Tracker PnL V2 API—not Fomo scraping. It
refreshes a strict rolling 24-hour leaderboard every 20 minutes, rotates the watchlist, and
starts monitoring qualifying wallets without CSV files or manual wallet entry.

The bot starts in **PAPER** mode. Live Jupiter spot execution exists, but remains locked
unless four separate controls are deliberately configured.

## What the bot does

- Pulls a rolling 1-day Solana trader leaderboard with actual public wallet addresses.
- Requires configurable minimum PnL, win rate, ROI, trade count, and closed positions.
- Excludes arbitrage wallets, suspicious identity tags, hyperactive bot-like wallets, and
  one-token wonders through provider filters and local checks.
- Automatically adds new qualifiers, disables wallets that rotate out, and reports PnL
  momentum between refreshes.
- Backfills up to 24 hours of transactions when a wallet is first discovered.
- Throttles Solana RPC calls and retries temporary `429`/server failures with backoff.
- Detects SOL/USDC/USDT-to-token buys and sells from wallet balance changes.
- Maintains an average-cost inventory for each tracked wallet.
- Calculates realized P&L, realized ROI, win rate, trade count, volume, and maximum drawdown.
- Scores traders on repeatability, ROI, 24-hour/7-day consistency, activity, and drawdown.
- Requires independent-wallet consensus inside a configurable time window.
- Posts raw wallet activity, consensus signals, risk results, and fills to Discord.
- Blocks suspicious, low-liquidity, concentrated, mintable, or freezable tokens when the
  required Jupiter safety metadata is available.
- Paper-trades buys and exits with configurable fee/slippage assumptions.
- Enforces configurable stop-loss, take-profit, and maximum-hold exits.
- Tracks equity, realized/unrealized P&L, win rate, and maximum drawdown.
- Optionally executes personal-wallet **spot** swaps through Jupiter Swap API V2.

## What it deliberately does not do

- It does not log into, scrape, reverse-engineer, or control a Fomo account.
- It does not map Fomo usernames to wallets or copy Fomo's leaderboard. Fomo does not expose
  an authorized public leaderboard API; this bot uses an API intended for programmatic
  Solana wallet discovery instead.
- It does not promise profit. The paper scoreboard exists specifically to prove or reject
  the strategy with evidence.
- It does not trade perpetual futures, borrow funds, or use leverage.
- It never asks for a seed phrase in Discord or chat.

## Strategy

The raw 24-hour profit leaderboard is not the signal. A single lucky token can put a risky
wallet at the top. This bot uses a risk-adjusted pipeline:

1. Refresh the authorized rolling 24-hour wallet leaderboard.
2. Filter for repeatable, copyable performance and rotate the watchlist automatically.
3. Ingest confirmed swaps for each watched wallet.
4. Reconstruct inventory and realized results in chronological order.
5. Blend the provider's strict 24-hour score with locally reconstructed performance.
6. Ignore wallets below `MIN_TRADER_SCORE`.
7. Require `CONSENSUS_MIN_TRADERS` unique wallets to buy the same mint within
   `CONSENSUS_WINDOW_SECONDS`; a qualified sell can trigger an exit so positions do not
   remain stuck waiting for full sell consensus.
8. Reject stale signals and unsafe token conditions.
9. Record a paper fill including configured fees and slippage.

The defaults require two qualified traders within five minutes. Set
`CONSENSUS_MIN_TRADERS=3` for a strict three-wallet strategy.

## Local setup

Requirements: Python 3.12+, a Discord bot token, a Solana RPC URL, and a Solana Tracker API
key for automatic discovery. A Jupiter API key is strongly recommended and required for
live mode because token safety metadata is part of the live risk gate.

## Automatic wallet discovery

Create a Solana Tracker Data API key at
<https://www.solanatracker.io/account/data-api>. The free tier supports leaderboard reads
within its quota. Store the key only in Railway Variables:

```text
SOLANA_TRACKER_API_KEY=...
AUTO_DISCOVERY_ENABLED=true
DISCOVERY_REFRESH_SECONDS=1200
DISCOVERY_MAX_WALLETS=12
DISCOVERY_MIN_24H_PNL_USD=100
DISCOVERY_MIN_WIN_RATE_PERCENT=55
DISCOVERY_MIN_ROI_PERCENT=3
DISCOVERY_MIN_TRADES=5
DISCOVERY_MAX_TRADES=250
DISCOVERY_MIN_CLOSED_TOKENS=2
DISCOVERY_MAX_SINGLE_TOKEN_PERCENT=70
```

The bot calls Solana Tracker's documented `GET /v2/pnl/leaderboard/top` endpoint with
`days=1`, strict PnL mode, arbitrage exclusion, and concentration filtering. Existing
manually added wallets are never removed by automatic rotation.

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
| `/smartmoney discover` | Refresh and display the automatic 24-hour wallet feed. |
| `/smartmoney trader-add` | Optionally add a manual public-wallet override. |
| `/smartmoney trader-import` | Optionally bulk import `alias,wallet,weight` CSV rows. |
| `/smartmoney trader-remove` | Remove a tracked wallet. |
| `/smartmoney traders` | List monitored wallets. |
| `/smartmoney scan` | Run a scan immediately. |
| `/smartmoney leaderboard` | Show the 24-hour or 7-day risk-adjusted ranking. |
| `/smartmoney paper` | Show strategy equity, net P&L, win rate, and drawdown. |
| `/smartmoney positions` | Show open paper positions. |
| `/smartmoney paper-reset` | Reset the paper challenge after exact confirmation. |
| `/smartmoney mode` | Show or set alerts, paper, or live mode. |
| `/smartmoney pause` | Pause/resume monitoring. |
| `/smartmoney status` | Check RPC and scanner health. |
| `/smartmoney limits` | Show active risk limits. |

Mutation commands require Discord Administrator or a role listed in
`DISCORD_ADMIN_ROLE_IDS`.

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
- A source wallet can still dump after followers enter. Consensus and liquidity filters
  reduce this risk; they cannot remove it.

## Verification

```bash
ruff check .
pytest -q
```

The tests cover swap detection, score behavior, paper accounting, risk gating, and live-mode
locking.
