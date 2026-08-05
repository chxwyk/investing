# Smart Money Copy Bot

A Railway-ready Discord bot that monitors **public Solana wallets**, reconstructs their
swaps from confirmed on-chain transactions, ranks their recent performance, produces
multi-wallet consensus signals, and records copy results after simulated fees and slippage.

The bot starts in **PAPER** mode. Live Jupiter spot execution exists, but remains locked
unless four separate controls are deliberately configured.

## What the bot does

- Backfills up to 24 hours of transactions when a wallet is first added.
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
- It cannot turn a Fomo username into a wallet address. Add only public wallet addresses
  that the owner has shared or that you already lawfully possess.
- It does not promise profit. The paper scoreboard exists specifically to prove or reject
  the strategy with evidence.
- It does not trade perpetual futures, borrow funds, or use leverage.
- It never asks for a seed phrase in Discord or chat.

## Strategy

The raw 24-hour profit leaderboard is not the signal. A single lucky token can put a risky
wallet at the top. This bot uses a risk-adjusted pipeline:

1. Ingest confirmed swaps for each watched wallet.
2. Reconstruct inventory and realized results in chronological order.
3. Score every wallet from 0–100.
4. Ignore wallets below `MIN_TRADER_SCORE`.
5. Require `CONSENSUS_MIN_TRADERS` unique wallets to buy the same mint within
   `CONSENSUS_WINDOW_SECONDS`; a qualified sell can trigger an exit so positions do not
   remain stuck waiting for full sell consensus.
6. Reject stale signals and unsafe token conditions.
7. Record a paper fill including configured fees and slippage.

The defaults require two qualified traders within five minutes. Set
`CONSENSUS_MIN_TRADERS=3` for a strict three-wallet strategy.

## Local setup

Requirements: Python 3.12+, a Discord bot token, and a Solana RPC URL. A free Jupiter API
key is strongly recommended and required for live mode because token safety metadata is
part of the live risk gate.

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
4. Add the required variables from `.env.example`.
5. Set `DISCORD_GUILD_ID` while testing so slash-command updates appear immediately.
6. Deploy. Railway uses the included `Dockerfile` and `railway.toml`.
7. In Discord run `/smartmoney setup`, then add wallets with
   `/smartmoney trader-add`.

The minimum useful variables are:

```text
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_ALERT_CHANNEL_ID=...
SOLANA_RPC_URL=...
JUPITER_API_KEY=...
DATABASE_PATH=/data/smart_money.db
```

A dedicated RPC is recommended for production. The public Solana endpoint may rate-limit
24-hour backfills for active wallets.

## Discord commands

| Command | Purpose |
|---|---|
| `/smartmoney setup` | Select the alert channel. |
| `/smartmoney trader-add` | Add a public Solana wallet and alias. |
| `/smartmoney trader-import` | Bulk import `alias,wallet,weight` CSV rows. |
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
