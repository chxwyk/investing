# Smart Money Copy Bot

A Railway-ready Discord bot that **automatically discovers profitable public Solana
wallets**, independently verifies strict 24-hour and 7-day performance, rotates toward recent
Pump.fun memecoin activity every five minutes, reconstructs swaps from confirmed on-chain
transactions, and mirrors every newly detected hot-wallet swap in PAPER mode. PAPER can run
as either a forced source-price observation ledger or an executable Jupiter quote-shadow
trial; the two answer different questions and are labeled separately.

Version 2.23 makes the launch radar crypto-first. Existing coins need repeated exact-contract
promotion from credible crypto-native X accounts. New crypto narratives need active promotion
from several established crypto accounts before `LAUNCH READY`. Non-crypto news can enter only
through a major U.S. breakout lane that requires an exceptional event, two additional news
confirmations, fast broad X activity, and visible crypto-community pickup. Routine sports,
entertainment, product, and foreign-business headlines are suppressed rather than converted
into launch ideas. The 100-point score remains, but the crypto-demand gate is mandatory.

Version 2.23 also splits long `/smartmoney status` and `/smartmoney sources` responses beneath
Discord's 2,000-character limit and caps the live RPC health check at eight seconds.

For `LAUNCH READY` alerts, v2.23 can expose one admin-only Discord button that uploads a
generated community-meme image and metadata to public IPFS, asks Pump.fun's official build API
for a create + initial-buy transaction, signs with a separate low-balance launch wallet, and
submits it through the configured Solana RPC. There is no second confirmation modal. The path
is locked by default and requires a one-time environment acknowledgement, separate wallet,
Pinata JWT, minimum score, duplicate lock, and daily launch-count/SOL caps. Existing contracts
never receive a launch button; they route to research/buy links instead.

Version 2.21 made each news lead actionable and removed capitalized-acronym spam. Leads without
a contract gained matching-coin, Pump.fun, X, and original-source links; contract and newly
matched-pair alerts gained direct Fomo, Pump.fun, Jupiter, DEX chart, and Solscan controls.

Version 2.20 added a fast, research-only news radar in front of the existing coin-intelligence
pipeline. It consumes X's official filtered stream plus selected official RSS/Atom feeds,
alerts on scored breaking narratives, extracts any Solana contract address already present,
and rechecks DEX Screener for a newly created Solana pair matching the narrative. A detected
pair still passes the normal DEX, token-safety, verified-wallet, and X evidence checks; a
headline never becomes an automatic live buy. An optional `J7_AUTHORIZED_FEED_URL` accepts a
legitimately provided J7 RSS/Atom feed, but the bot does not scrape J7's authenticated product
or store J7 login credentials.

Version 2.19 added asynchronous coin-intelligence callouts. A detected BUY keeps the fast
quote/PAPER path; a separate task cross-checks the mint against DEX Screener pair flow,
existing Jupiter token safety metadata, independently verified smart-wallet buyers, and—when
`X_API_BEARER_TOKEN` is configured—the official X recent-search API. X evidence is searched
by full contract address, deduplicated, and discounted for young/low-quality author clusters.
The existing Solana Tracker key also supplies its documented 1–10 token risk score, rugged
state, bundler, insider, sniper, developer, and holder-concentration evidence. Paid DEX boosts
are labeled rather than treated as organic proof. Callouts never unlock live trading or bypass
the existing risk gate.

Automatic discovery uses the authorized Solana Tracker PnL V2 API—not Fomo or KOLScan
scraping. It combines the strict general-trader leaderboard with the provider's documented
public-KOL period leaderboard, refreshes the 24-hour pool every three hours, caches an
independently filtered 7-day pool for twelve hours, and requires qualifying evidence in both
windows. The resulting candidates must then show recent on-chain Pump activity before
entering the 25-wallet hot set. A reconnecting Solana/Helius WebSocket uses an early
`processed` trigger and rapid full-transaction fetch retries while one-minute polling stays
enabled as a fallback. The verified candidate pool is stored in SQLite so a Railway redeploy
does not erase it.

The bot starts in **PAPER** mode. Live Jupiter spot execution exists, but remains locked
unless four separate controls are deliberately configured.

## What the bot does

- Pulls rolling 1-day and 7-day general-trader and public-KOL leaderboards with public wallet
  addresses.
- Requires independent positive PnL evidence in both windows. Active rows must also pass
  ROI, win-rate, trade-count, and token-diversity thresholds. The documented public-KOL
  period feed can omit those metrics, so incomplete KOL rows are nomination-only and cannot
  enter the automatically copied hot set until a complete authorized response verifies them.
- Excludes arbitrage wallets, suspicious identity tags, hyperactive bot-like wallets, and
  one-token wonders through provider filters and local checks.
- Builds a pool of up to 100 strict candidates and rechecks recent on-chain activity every
  five minutes; only verified Pump traders occupy the 25-wallet hot set.
- Automatically adds new qualifiers and disables wallets that become inactive, stop passing
  both profit windows, lack Pump activity, or are outranked by stronger active candidates.
- Records every admission/removal reason, baseline/current rolling PnL, locally observed
  source-wallet PnL, and PAPER PnL attributable to that wallet.
- Keeps new wallets on forward-PAPER probation, then removes mature candidates whose own
  copied exits breach the configured loss or profit-factor floor.
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
- Posts breaking-news radar alerts from the official X filtered stream and selected RSS/Atom
  feeds across politics, sports, entertainment, products, internet culture, technology, world
  events, and crypto, then watches for a newly created matching Solana pair.
- Scores each event with visible source, speed, meme, X, confirmation, competition, and
  identity components instead of requiring crypto relevance.
- Optionally launches a `LAUNCH READY` community coin with one admin-only Discord click using
  a separate capped Pump wallet; it does not reuse the Jupiter trading wallet.
- Searches X coin evidence by exact mint plus DEX-listed token name/symbol identity, while
  discounting identity-only matches so ticker collisions cannot masquerade as contract proof.
- Suppresses automatic low-score blocked reports that used to flood the channel; every token
  remains inspectable on demand with `/smartmoney coin`.
- Posts scored coin callouts without delaying the copy path; a second independent verified
  wallet buying the same mint forces a fresh callout even inside the normal cooldown.
- Provides `/smartmoney coin` for an on-demand contract-address report covering smart money,
  DEX flow, token safety, and official X evidence.
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

- It does not log into, scrape, reverse-engineer, or control a Fomo or J7 Tracker account.
- It does not scrape or automate KOLScan. Only documented provider APIs and public on-chain
  activity are used for automatic candidate discovery.
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

### Forced PAPER observation

`PAPER_FORCE_OBSERVATION_MODE=false` is the safe default. Setting it to `true` creates a
complete PAPER outcome from every valid detected source-wallet swap. This mode records a buy
immediately from the source
transaction price plus `PAPER_OBSERVATION_PENALTY_BPS`; sells use the source price minus the
same penalty. Normal simulated slippage and fees are then applied. It does not wait for a
Jupiter route and bypasses liquidity, holder, organic-score, entry-drift, price-impact,
quote-latency, position-count, and per-wallet/token capacity gates. The raw hard stop,
take-profit, trailing lock, maximum hold, and account-level daily loss lock still run in this
mode; observation pricing must never disable loss controls.

This is deliberately an **observation ledger**, not evidence that a live transaction could
have landed at that price. Its trades are labeled `FORCED_OBSERVATION`, remain `quote_based=0`,
and are excluded from `/smartmoney readiness`. It can still skip when the source transaction
contains no usable token price, fake cash is exhausted, a duplicate signature arrives, or a
sell has no earlier matching PAPER buy. A processed WebSocket trigger reduces delay, but no
bot can react before the tracked transaction is publicly observed, and a tracked purchase can
itself move a low-liquidity token before a copy order exists.

### Existing-holding tracking baselines

`PAPER_SEED_TRACKING_BASELINES=false` is the safe default. Setting it to `true` prevents a
newly tracked wallet's first sell from being
orphaned merely because its corresponding buy happened before monitoring began. The bot uses
the reconstructed public source inventory, opens a clearly labeled `TRACKING_BASELINE` paper
lot at the current observed price, and measures only movement after that baseline. It never
invents the wallet's earlier entry or claims profit from before tracking. Tokens that already
received any PAPER buy are never baseline-seeded again, so a risk or manual exit cannot
silently reopen a closed position. `PAPER_BASELINE_MAX_POSITIONS_PER_WALLET` limits existing
holdings seeded per wallet (default `10`). Baseline trades are excluded from quote-based
readiness evidence.

For a deliberately unfiltered observation run (not the recommended readiness setup):

```text
PAPER_FORCE_OBSERVATION_MODE=true
PAPER_OBSERVATION_PENALTY_BPS=300
PAPER_MIRROR_RAW_SWAPS=true
REALTIME_WALLET_STREAM_ENABLED=true
REALTIME_STREAM_COMMITMENT=processed
ENABLE_LIVE_TRADING=false
```

When the observation ledger has enough trades, disable forced observation and run the separate
quote-shadow readiness trial below. Do not treat forced-observation P&L as expected live P&L.

### Low-liquidity sniper PAPER lane

`PAPER_SNIPER_TEST_ENABLED=true` adds a smaller, isolated PAPER lane for Pump launch tokens
that fail the normal entry guard only because of launch-stage liquidity, holder count,
organic score, or concentration. It does **not** disable suspicious-token, mint-authority,
freeze-authority, stale-signal, daily-loss, position-count, or account-capacity checks. The
default simulated size is `$2`, with absolute floors of `$2,000` liquidity and `20` holders.

When Jupiter can quote the token, this lane permits at most `20%` entry drift and `5%` price
impact. When a Pump bonding-curve token has no executable route, it uses the detected source
price with a `500bps` adverse penalty. Both kinds are labeled `SNIPER_*` and excluded from
live-readiness evidence. This produces more launch-stage outcomes without pretending that an
unroutable paper fill was a real fill. A source SELL after a skipped BUY still cannot create a
sale: the bot cannot sell fake inventory it never bought.

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

New Pump.fun bonding-curve tokens often do not have a Jupiter price or route yet. In PAPER
mode only, `PAPER_ALLOW_PUMP_SOURCE_FALLBACK=true` records those detected buys and sells using
the transaction's on-chain price with an additional adverse
`PAPER_PUMP_SOURCE_FALLBACK_BPS` penalty before the normal simulated slippage and fee. The bot
labels these rows `PUMP SOURCE FALLBACK`. They let the strategy ledger advance, but they are
not executable-quote evidence and do not count toward `/smartmoney readiness`.

The next raw SELL from that same wallet sells the matching fake lot proportionally while it
remains open. Separately, the paper risk manager can close the entire lot first at the raw
hard stop, take-profit, trailing-profit threshold, or maximum hold. A later source SELL will
then correctly show `SKIPPED` because the protected paper lot is already closed. Different
wallets remain separate even when they trade the same token.

Bootstrap history is recorded for scoring and reconstructing source holdings, but it is never
purchased retroactively at an old price. Optional baseline seeding can instead open a
current-price tracking baseline for eligible existing holdings. A sell can still be unmatched
when no prior public BUY exists
inside the scanned history, no current baseline price is available, or the PAPER account lacks
cash. Set `PAPER_MIRROR_RAW_SWAPS=false` to restore consensus-only paper behavior.

The v2.23.0 discovery, crypto-first launch-radar, callout, selective-entry, daily loss/profit
locks, social nomination, quote, fallback, rotation, and exit controls are
intentionally configurable:

```text
PAPER_REQUIRE_CURRENT_PRICE=true
PAPER_ALLOW_PUMP_SOURCE_FALLBACK=false
PAPER_PUMP_SOURCE_FALLBACK_BPS=300
PAPER_RAW_ENTRY_FILTER_ENABLED=true
PAPER_FORCE_OBSERVATION_MODE=false
PAPER_OBSERVATION_PENALTY_BPS=300
PAPER_SEED_TRACKING_BASELINES=false
PAPER_BASELINE_MAX_POSITIONS_PER_WALLET=10
PAPER_SNIPER_TEST_ENABLED=false
PAPER_SNIPER_COPY_USD=2
PAPER_SNIPER_MIN_LIQUIDITY_USD=2000
PAPER_SNIPER_MIN_HOLDERS=20
PAPER_SNIPER_MAX_TOP_HOLDERS_PERCENT=85
PAPER_SNIPER_SOURCE_PENALTY_BPS=500
PAPER_SNIPER_MAX_ENTRY_DRIFT_PERCENT=20
PAPER_SNIPER_MAX_QUOTE_PRICE_IMPACT_PERCENT=5
PAPER_DAILY_TARGET_USD=100
PAPER_DAILY_PROFIT_LOCK_ENABLED=true
PAPER_DAILY_LOSS_LIMIT_USD=20
PAPER_DAILY_LOSS_LOCK_ENABLED=true
PAPER_DAILY_LOCK_TIMEZONE=America/Los_Angeles
PAPER_DAILY_PROFIT_CHECK_SECONDS=15
PAPER_USE_EXECUTABLE_QUOTES=true
PAPER_QUOTE_OUTPUT_BUFFER_BPS=50
MAX_ADVERSE_ENTRY_DRIFT_PERCENT=5
MAX_QUOTE_PRICE_IMPACT_PERCENT=1.5
MAX_QUOTE_LATENCY_MS=5000
MAX_CONSECUTIVE_QUOTE_FAILURES=5
RAW_MIRROR_STOP_LOSS_PERCENT=6
RAW_MIRROR_TAKE_PROFIT_PERCENT=15
RAW_MIRROR_TRAILING_ACTIVATION_PERCENT=5
RAW_MIRROR_TRAILING_STOP_PERCENT=3
RAW_MIRROR_MAX_HOLD_SECONDS=3600
DISCOVERY_MAX_WALLETS=25
DISCOVERY_INCLUDE_KOLS=true
DISCOVERY_KOL_LIMIT=100
ROTATION_REFRESH_SECONDS=300
ROTATION_MAX_IDLE_SECONDS=3600
ROTATION_MIN_RECENT_SWAPS=1
ROTATION_MIN_PUMP_SWAPS=1
ROTATION_REQUIRE_PUMP_ACTIVITY=true
FORWARD_EVIDENCE_MIN_CLOSED_SELLS=5
FORWARD_EVIDENCE_MIN_PROFIT_FACTOR=1.0
FORWARD_EVIDENCE_MAX_LOSS_USD=10
REALTIME_WALLET_STREAM_ENABLED=true
REALTIME_STREAM_COMMITMENT=processed
NEWS_RADAR_ENABLED=true
X_NEWS_STREAM_ENABLED=true
X_CRYPTO_TRUSTED_ACCOUNTS=WatcherGuru|CoinDesk|Cointelegraph|solana|pumpdotfun|lookonchain|ArkhamIntel
NEWS_POLL_SECONDS=30
NEWS_MIN_SCORE=45
NEWS_LAUNCH_READY_SCORE=72
NEWS_X_VERIFY_MIN_SCORE=35
NEWS_X_TREND_CACHE_SECONDS=90
NEWS_MAX_ALERTS_PER_HOUR=30
NEWS_DEX_MATCH_ENABLED=true
NEWS_DEX_MATCH_MIN_LIQUIDITY_USD=2000
NEWS_DEX_MATCH_MAX_AGE_MINUTES=60
NEWS_PAIR_RECHECK_SECONDS=0,30,90,180
```

These values are hypotheses to validate in PAPER mode, not optimized or guaranteed-profit
settings. Use `/smartmoney paper`, `/smartmoney positions`, `/smartmoney paper-trades`, and
`/smartmoney readiness` to evaluate them before changing size.

## Crypto-first news and launch radar

The radar has two independent inputs. X uses the official filtered-stream endpoint, so a
matching public post arrives without polling. Its default rule follows established crypto
sources, exact contract/launch language, and a small set of major U.S. breaking-news accounts.
RSS/Atom polls selected U.S. government, market, and crypto feeds every 30 seconds. Routine
world, sports, entertainment, and product feeds are not included by default.

Every X evidence response separates generic authors from crypto-native authors, credible
crypto authors, promotion posts, and exact-contract posts. A generic keyword match cannot
unlock a launch. `X_CRYPTO_TRUSTED_ACCOUNTS` is an auditable pipe-separated allowlist used only
as author-quality evidence; an allowlisted account still cannot bypass duplicate, velocity,
competition, source, or confirmation checks.

Scores from 45 through 71 are `WATCH`. A score of 72 or more can be `LAUNCH READY` only if the
normal source, speed, viral clarity, identity, and competition sub-gates pass **and** either the
crypto-trend promotion gate or major-U.S.-breakout gate passes. If a source contains a contract,
`COIN FOUND` requires repeated exact-contract promotion by credible crypto accounts; weaker
single-post contracts go through the normal risk callout without becoming a high-confidence
news alert. Existing contracts can never launch a duplicate.

The default X rule follows a deliberately small set of crypto and major U.S. news accounts to
limit paid X reads. Customize `X_NEWS_STREAM_RULE` using valid X filtered-stream syntax instead
of broad keywords that can consume credits quickly. `X_SEARCH_MAX_RESULTS=10` similarly caps
each on-demand coin-evidence search. `/smartmoney sources` and `/smartmoney status` show the
last X search error, stream state, RSS state, J7 feed state, narrative matcher state, scoring
thresholds, and one-click launch lock.

J7's public site does not document an integration API in this release. If J7 support gives you
an official RSS/Atom URL, store only that URL as `J7_AUTHORIZED_FEED_URL`. Never place a J7
password, browser cookie, session token, or copied private endpoint in Railway.

### One-click Pump.fun launch setup

This feature spends real SOL even while the copy-trading engine remains in PAPER mode. Create a
separate burner-style Solana wallet, keep only the amount you accept losing in it, and store its
private key only in Railway Variables. Create a Pinata JWT for public file uploads and store it
there too. Never reuse `TRADING_PRIVATE_KEY`.

```text
PUMP_ONE_CLICK_LAUNCH_ENABLED=true
PUMP_LAUNCH_ACK=I_UNDERSTAND_PUMP_LAUNCHES_SPEND_REAL_SOL
PUMP_LAUNCH_PRIVATE_KEY=<dedicated launch wallet secret>
PINATA_JWT=<server-side Pinata JWT>
PUMP_LAUNCH_INITIAL_BUY_SOL=0.01
PUMP_LAUNCH_MIN_SCORE=72
PUMP_LAUNCH_MAX_PER_DAY=3
PUMP_LAUNCH_MAX_SOL_PER_DAY=0.05
PUMP_LAUNCH_TIMEZONE=America/Los_Angeles
```

The launch button is shown only on `LAUNCH READY` alerts and only Discord administrators or
configured `DISCORD_ADMIN_ROLE_IDS` can press it. One click creates and submits the transaction;
there is no per-launch confirmation dialog. SQLite reserves the source alert before any upload
or signature so double clicks and restarts cannot produce a second coin from the same alert.
Pump metadata labels the asset as a community meme and explicitly says it is not official or
affiliated with the people, brands, publisher, or event in the source.

## Official PAPER readiness trial

After deploying v2.13.0 and disabling forced observation, run
`/smartmoney paper-reset confirmation:RESET PAPER` once to begin a clean trial.
`/smartmoney readiness` reports **KEEP TESTING** until all defaults pass:

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

1. Refresh authorized strict general-trader and public-KOL 24-hour and 7-day leaderboards.
2. Require qualifying PnL in both windows so a one-day spike alone cannot qualify a wallet;
   apply every additional metric actually supplied by the source.
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
key for automatic discovery. A Jupiter API key is required for quote-shadow PAPER and
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
DISCOVERY_INCLUDE_KOLS=true
DISCOVERY_KOL_LIMIT=100
PUMP_PROFILE_DISCOVERY_ENABLED=true
PUMP_PROFILE_PAGES=1
PUMP_PROFILE_MIN_FOLLOWERS=1000
PUMP_PROFILE_LIMIT=50
PUMP_PROFILE_MAX_PAGE_FETCHES=25
PUMP_PROFILE_REFRESH_SECONDS=21600
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
FORWARD_EVIDENCE_MIN_CLOSED_SELLS=5
FORWARD_EVIDENCE_MIN_PROFIT_FACTOR=1.0
FORWARD_EVIDENCE_MAX_LOSS_USD=10
REALTIME_WALLET_STREAM_ENABLED=true
```

The bot calls Solana Tracker's documented `GET /v2/pnl/leaderboard/top` endpoint with
`days=1` and `days=7`, plus the documented
`GET /v2/pnl/leaderboard/kols/period` endpoint with `period=1d` and `period=7d`. General-feed
requests use strict PnL mode, arbitrage exclusion, concentration filtering, and cursor
pagination. The public-KOL period response only guarantees PnL, volume, and trading days;
incomplete KOL rows remain nomination-only and are never rendered as verified wallets. A KOL
identity never bypasses dual-window PnL, ROI, win rate, trade-count, repeat-day, on-chain Pump,
or forward-PAPER checks.

The optional Pump social source reads Pump's official public profiles pages at a slow six-hour
cadence, resolves only publicly exposed Solana wallet identities, and records follower counts as
nomination evidence. Follower count never increases a wallet's financial score and cannot create
a tracked wallet. The wallet must independently appear with complete qualifying fields in both
the strict 24H and 7D feeds before it can proceed to recent Pump-activity verification. If Pump's
public HTML changes or blocks the request, the financial discovery engine keeps working and the
social source reports zero/newest error instead of trusting an unverified profile.

Fomo's official product exposes in-app leaderboards, profiles, follows, and alerts, but this
release does not claim access to an undocumented/private Fomo API. A Fomo identity can enter the
same verifier only after Fomo supplies a documented official feed/webhook or the profile exposes
a legitimate public wallet identity. The bot does not scrape authenticated pages or replay app
credentials.

The runtime
automatically clamps full multi-page refreshes to at least three
hours for 24H data and twelve hours for 7D data. Every five minutes the cached pool is checked
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
# Optional; required only for scored X/Twitter evidence in coin callouts.
X_API_BEARER_TOKEN=...
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
| `/smartmoney candidates` | Show pool size, Pump verification, selected wallets, and exact rejection reasons. |
| `/smartmoney rotation` | Show recent admissions/removals and the exact reason for each. |
| `/smartmoney sources` | Show which discovery/platform/stream sources are actually connected. |
| `/smartmoney coin` | Score any Solana contract using verified buyers, token safety, DEX flow, and official X evidence. |
| `/smartmoney trader-add` | Optionally add a manual public-wallet override. |
| `/smartmoney trader-import` | Optionally bulk import `alias,wallet,weight` CSV rows. |
| `/smartmoney trader-remove` | Remove a tracked wallet. |
| `/smartmoney traders` | List monitored wallets. |
| `/smartmoney scan` | Run a scan immediately. |
| `/smartmoney leaderboard` | Show the 24-hour or 7-day risk-adjusted ranking. |
| `/smartmoney paper` | Show P&L, drawdown, profit factor, expectancy, and 24H progress. |
| `/smartmoney positions` | Browse open positions with page, refresh, token-link, and confirmed manual PAPER-sell buttons. |
| `/smartmoney paper-trades` | Browse every fill, realized ROI, quote details, and exit reasons with page buttons. |
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

### Clean PAPER controls and manual exits

`/smartmoney positions` now opens a private one-position-at-a-time panel. Previous/next
buttons move between fake lots, **Refresh prices** updates their current mark and unrealized
P&L, and the token links open Fomo, Pump.fun, Jupiter, DexScreener, or Solscan without filling
the channel with messages. Administrators also see **Sell this PAPER position**. The sell
requires a second confirmation, closes only that selected fake lot, returns its simulated
proceeds to PAPER cash, and records the result as realized P&L. It never signs or broadcasts a
transaction and cannot touch real funds.

A manual exit is deliberately labeled `MANUAL_EXIT` or `MANUAL_OBSERVATION_EXIT` and is
excluded from `/smartmoney readiness`, so manually locking a winner cannot make the automatic
strategy appear ready. A later source-wallet SELL correctly skips if the linked lot was already
closed manually.

`/smartmoney paper-trades` is private and paginated. It reads the complete stored history five
fills at a time instead of posting only ten rows or flooding the alert channel.

When automatic rotation removes a wallet that still has an open source-linked PAPER lot, the
wallet now remains subscribed in **exit-only** mode. Fresh buys from that wallet are ignored,
but the matching sell remains monitored until the fake lot closes. `/smartmoney status` shows
the number of exit-only wallets.

### Daily PAPER profit/loss lock

The v2.13.0 daily lock uses the account's **marked** change for the current local day—realized
P&L plus the current value of open positions relative to the first account mark that day. With
the defaults, reaching `+$100` or `-$20` immediately records the lock, attempts to sell every priced
PAPER position, blocks every new PAPER buy, and suspends automatic discovery/rotation work for
the rest of the day. A dedicated account guard re-marks positions every 15 seconds without
performing another leaderboard refresh. A position whose current price is temporarily
unavailable stays open and is retried on the next guard cycle; the entry lock remains active
while it retries.

The lock resets automatically on the next day in `America/Los_Angeles`. It is persisted in
SQLite, so a Railway restart cannot silently unlock the account. `/smartmoney paper`,
`/smartmoney status`, and `/smartmoney limits` show the target and current lock state. Change
`PAPER_DAILY_TARGET_USD` later to adjust the target, or set
`PAPER_DAILY_PROFIT_LOCK_ENABLED=false` to disable the upside lock. The downside breaker uses
`PAPER_DAILY_LOSS_LIMIT_USD=20` and `PAPER_DAILY_LOSS_LOCK_ENABLED=true`. The lock is PAPER-only and does
not promise an exact $100 realized result: simulated costs and price movement between the mark
and exit can reduce the final amount.

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
- The v2.13.0 discovery, observation, forward-evidence, quote, fallback, rotation, raw-entry,
  and raw-lot
  guards are PAPER-only. Live mode remains
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

The tests cover multi-source dual-window qualification, public-KOL parsing, Pump activity
admission, wallet rotation auditing, forward PAPER evidence, WebSocket derivation, swap
detection, score behavior, Swap V2 quote parsing, entry-drift and price-impact rejection,
quote-based round trips, readiness metrics, raw-lot caps, hard/trailing/time exits, risk
gating, database migration, and live-mode locking.
