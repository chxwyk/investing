# Smart Money Copy Bot

A Railway-ready Discord bot that **automatically discovers profitable public Solana
wallets**, independently verifies strict 24-hour and 7-day performance, rotates toward recent
Pump.fun memecoin activity every five minutes, reconstructs swaps from confirmed on-chain
transactions, and mirrors every newly detected hot-wallet swap in PAPER mode. PAPER can run
as either a forced source-price observation ledger or an executable Jupiter quote-shadow
trial; the two answer different questions and are labeled separately.

Version 2.39.0 adds the **SHADOW auto-trader**: a completely simulated $100 account that
automatically buys **exactly $10** whenever an eligible research signal appears, manages the
position with the existing staged exit engine plus a **$2 NET meaningful-profit objective**, sells
realistically, and records the NET result per signal family. It exists to answer one question with
forward evidence instead of opinion — *if the bot had automatically bought $10 every time it saw
one of these signals, would the $100 be worth more or less now?*

**No real money, structurally.** The shadow package contains no signer, no keypair, no RPC client,
no transaction builder and no swap submission path; `SHADOW_REAL_MONEY_SPEND` is zero and the test
suite proves it by walking each module's syntax tree. STRICT PAPER is untouched: it keeps every
gate it had, the two families use different strategy versions, different bankrolls and different
tables, and shadow eligibility can never reach the strict engine.

Version 2.38.0 turned the research pipeline into a **real-time alpha engine**, and closed the one
gap the v2.37 report named explicitly: FAST WATCH was implemented and tested but nothing
published it, so it never reached Discord. It does now.

The architecture is **DETECT → PERSIST → NOTIFY → ENRICH**, not "wait for every provider, then
maybe notify three minutes later". A stage-1 card is built only from evidence already in hand and
published immediately; deep forensics, safety, route quality and social confirmation arrive later
and **edit the same message** rather than sending a second ping. Every alert row is reserved in
SQLite *before* the notifier is called, so a restart, a duplicated stream event or a retried
coroutine cannot re-publish or re-ping the same observation.

Nine alert classes exist — `FAST_WATCH`, `NOTABLE_TRADER_EARLY`, `NOTABLE_TRADER_LATE`,
`NOTABLE_DISTRIBUTION`, `BREAKING_CATALYST`, `CATALYST_WATCH`, `CONFLUENCE_WATCH`,
`SHADOW_AUTO_ENTRY` and `SHADOW_AUTO_EXIT` — and every one of them carries
`entry_eligible = False` structurally. Only three may interrupt the user
(`NOTABLE_TRADER_EARLY`, `BREAKING_CATALYST`, `CONFLUENCE_WATCH`); a late observation is still
published, quantified and visible, but it never pings.

**Two visibility layers, so nothing has to be hidden to stay quiet.** v2.39 splits *where* a card
goes from *whether it pings*. `FOMO_URGENT_CHANNEL_ID` receives the genuinely urgent classes;
`FOMO_LIVE_RADAR_CHANNEL_ID` receives everything else worth reading — FAST WATCH, fresh runners,
late notable observations, ordinary catalysts and every shadow fill. Both variables are optional
and fall back to the existing alert channel, so a deployment that defines neither keeps exactly
today's behaviour. Every card now names its **signal family** and states **why you're seeing
this**. **Speed changes what you see, never what
the bot is allowed to do:** the PAPER entry gates, the fail-closed safety rules and the cost
floors are untouched, and there is still no live-execution path anywhere in the repository.

**Notable-trader intelligence** watches the existing wallet stream and publishes what it actually
observed: how much was bought, the trader's entry market cap, the bot's detection market cap, the
current market cap, both moves, and how many seconds after the chain event the bot saw it. Identity
is never inferred. A wallet without a verified public mapping stays anonymous behind a stable,
meaningless handle (`Wallet #17`), and the type system enforces it — an `ONCHAIN_ONLY` wallet that
is handed a public label raises. Four wallets funded from one upstream account are reported as one
actor, not four confirmations. A signal whose move is already spent grades `EDGE_CONSUMED`, is
published with the lateness spelled out, and is never chased.

**Catalyst intelligence** grades a real-world event on its own source integrity — primary source,
genuinely independent confirmations, circular sourcing, duplicate aggregator spam, possible
impersonation, staleness — entirely separately from any token. A verified event is never evidence
that a token is real: the token↔event connection is its own graded question, and `OFFICIAL`
requires the event's own authoritative source to have published the exact mint. Everything else
is labelled `NOT OFFICIAL`, however good the name match.

**Confluence** is where a credible catalyst, a plausibly related fresh token, independent
`PROVEN_EARLY` wallets and accelerating independent demand line up at the same moment. It raises
priority and nothing else — a `CONFLUENCE_WATCH` with `SAFETY PASS` is still not entry eligible.

Version 2.37.0 attacked the bottleneck that replaced intelligence: the system was often seeing
tokens too late, and then keeping stale ones next to fresh ones.

The headline latency number turned out to be partly a measurement artifact. `SOURCE → FIRST SEEN`
was computed against *pair creation* time, so a pair that a trending feed surfaced hours after it
was created reported an ~19-hour "ingestion latency" that no loop could produce. Every timing now
carries a quality grade — REALTIME, APPROXIMATE, HISTORICAL or UNKNOWN — and only realtime-graded
samples feed the percentiles. Historical samples are still counted and shown, labelled as what
they are. No timestamp is rewritten. `/fomo latency` additionally reports per-source statistics
and a full pipeline breakdown (source → first seen → fast watch → qualified → paper decision →
simulated fill) so the genuinely slow stage is visible rather than inferred.

There was also real latency to remove. First-seen was previously persisted only *after*
`analyze_runner` had finished its DEX, tracker-risk and quote work, so enrichment time was being
counted as ingestion time. A cheap discovery ledger now records the mint the instant the radar
detects it, before any enrichment, and never lets that timestamp move later. Never-seen mints are
also processed ahead of rechecks, so a backlog cannot push a genuinely new token to the next poll.

**FAST WATCH** surfaces early acceleration from cheap evidence — pair age, market-cap and volume
acceleration, price velocity, buy/sell pressure, holder and liquidity growth — without waiting for
wallet forensics, tracker risk or social enrichment. It is research visibility only: the verdict
type's `entry_eligible` is a structural `False`, the card lists the mandatory evidence it did not
wait for, and every PAPER entry gate is unchanged. A candidate that sat in a queue is re-checked
for freshness immediately before publication, so nothing publishes as "early" after the move.

**Current actionability** answers the second question the opportunity score never did: is this
still worth surfacing *now*? Historical opportunity is still stored and still drives research, but
the current radar ranks by current edge, and a candidate that is materially negative since first
seen with weakening flow — the JELLY case — is classified DETERIORATED and suppressed from the
current radar. Suppressed is never deleted: those tokens remain in `/fomo results`, `/fomo
quality`, lifecycle, replay, calibration and every forward observation, which is where their
value is. EDGE_CONSUMED is distinguished from mere weakness, and re-entry still requires the
existing RETRACED → COOLDOWN → REENTRY_WATCH → REENTRY_QUALIFIED evidence.

Three confirmed production bugs are fixed. A pre-v2.36 mint queried with `/fomo lifecycle` used to
re-initialise as `FIRST_DISCOVERY • FRESH` because no lifecycle row existed; it now reconstructs
from the runner's own candidate rows, snapshots, alert events and stage events, recovering the
real first-seen, first-surface market cap, historical peak, alert and qualification counts.
Missing evidence stays UNKNOWN/PARTIAL and is never invented, repeated lookups can only move the
earliest timestamp earlier, and a genuinely unseen mint is still FIRST_DISCOVERY. `/fomo
opportunities` and `/fomo lab mode:test` were failing with Discord HTTP 400 / 50035 because the
6000-character budget is per *message* while each card was clamped individually; one shared
renderer now budgets the whole message with headroom, trims optional detail before identity, mint,
decision, safety and WHY NOT ENTRY, falls back to compact and then minimal cards, and makes
exactly one emergency retry — the v2.35.1 guarantee that an interaction always resolves is
preserved. Buyer counts now distinguish five populations explicitly: raw on-chain buyers (never
sampled here, so *unavailable* rather than `0`), tracked wallets, independent tracked wallets,
verified buyers (where `0` is a real observation) and wallet clusters.

Provider diagnostics were also misleading. `runner_forensics` is an own-RPC feature, not billable
Solana Tracker traffic, and its cache hits — served by the per-mint forensic payload and the
persistent `wallet_funding_edges` table — were never counted, which is why `/fomo quality`
reported `cache 0` for a feature that mostly serves from cache. The hits are now recorded.

Version 2.36.1 makes the `/fomo opportunities` card's numbers match the evidence behind them.
Confidence was derived from the organic-demand score alone, so a 100/100 organic score rendered
`100%` confidence even when economic authenticity was UNKNOWN, the bounded SOL activity sample
was missing and safety was not PASS. Confidence is now ceilinged by the weakest evidence
supporting it — partial or unknown evidence quality, unknown/partial authenticity, an unsampled
activity profile, a non-PASS safety verdict, a weak or absent buyer trace, or degraded provider
data each impose a cap, and the strictest wins — with the ceiling and its reasons persisted on
the decision and shown on the card. The card could also print "14 independent buyer clusters"
beside "0 raw buyers": two different populations, since verified buyers come from tracked-wallet
swaps while independence is measured over the bounded holder trace. Independence is now always
reported against the population it was measured over (`14 of 14 traced`) and an unobserved
verified-buyer count says so instead of claiming zero, with the impossible pair unconstructable
by type. A high organic score with unknown or partial authenticity is now explicitly labelled
raw/unverified so it cannot be read as confirmed authentic demand. No score, threshold,
lifecycle rule, PAPER strategy, provider budget or Discord command changed, and SAFETY
FAIL/UNKNOWN remain fail-closed for PAPER entry.

Version 2.36.0 turns the Fomo runner into an autonomous **PAPER-only research laboratory**.
Nothing in it can move real funds: the whole `smart_money_bot.lab` package contains no wallet,
no signer and no live route, and `lab.LIVE_EXECUTION_ENABLED` is a hard `False` verified by the
self-check. Every evaluated candidate now produces one canonical decision — `ENTRY`, `WAIT`,
`REJECT`, `COOLDOWN`, `REENTRY_WATCH` or `REENTRY_QUALIFIED` — carrying stable machine reason
codes, Discord-readable reasons, an evidence-quality grade, the safety verdict, the strategy
version and a config hash, so an old decision stays attributable to the exact rules that made
it. Trade eligibility lives in that one layer; no Discord handler or provider re-derives it.

The laboratory answers "what is this coin, and has it already had its run?". Each mint gets a
durable lifecycle record: first discovery, first surface price and market cap, historical peak,
maximum and current drawdown, alert and qualification counts, and PAPER entry/exit history. A
token that surfaced at $32k, ran to $150k and fell back to $38k comes back as a **retraced old
winner**, never as a first discovery — and a Railway restart rehydrates that record instead of
rebuilding it, so the memory survives. Cheap again is not good again: a re-entry needs a
stabilized base, no continuing lower lows, returning momentum, re-accelerating volume, renewed
independent buyers, stable liquidity, no worsening clustering, `SAFETY PASS`, a healthy route
and sufficient net edge. A small bounce off the low with none of that is classified as a dead
cat and stays `REENTRY_WATCH`. Re-publication requires a real change — a lifecycle transition,
a material quality improvement, a safety improvement, a meaningful smart-wallet event or a risk
deterioration — so polling rediscovery alone can no longer repost the same card.

Money is modelled honestly. Every simulated position pays platform fees, Solana network fees,
priority fees, price impact and slippage on **both** legs, and only NET PnL counts as profit. An
entry needs an expected net edge that clears an absolute floor *and* a multiple of realistic
round-trip costs. Default simulated bankroll is $100 with a ~$5 normal position and a ~$10
exceptional maximum; sizing only ever moves down — for thin liquidity, weaker independence,
unknown authenticity, a re-entry, a hostile regime or a drawdown — and never up after losses.
There is no averaging down, no martingale and no revenge sizing anywhere. Exits are staged
rather than "sell everything at +10%": reference milestones at +10/+25/+50/+100 with an optional
moon bag, break-even protection, a real trailing stop off the post-entry high, momentum-decay,
flow-reversal, volume-exhaustion, liquidity, concentration and safety exits, plus a time stop
and a hard loss stop. A genuinely healthy runner — independent demand, momentum, liquidity and
controlled sellers — is allowed to keep more upside. Peak, maximum favourable and maximum
adverse excursion are tracked per position, so "was +110%, now +20%" is never scored like "never
went green", and every partial exit writes an immutable journal row with its own cost breakdown.

Evidence is event-sourced. One chronological stream per mint records prices, market caps,
liquidity, holders, flow, wallet events, lifecycle changes, alerts and PAPER entries/exits, each
with its provider, source timestamp, observation time, cache state and confidence. Replay and
counterfactuals read that stream through a `before(t)` gate, so a strategy physically cannot see
the future peak it is being scored against, and simulating eight entry policies against nine
exit policies costs zero provider requests because it runs entirely on persisted observations.
`SAMPLE_TOO_SMALL` is reported honestly, losing trades are never removed from the metrics, and a
challenger strategy cannot be promoted on in-sample replay — promotion demands out-of-sample
forward evidence of better NET expectancy, profit factor, drawdown and rug avoidance.

Public social sources are curated, measured and cheap. Tier A (official platform), Tier B
(on-chain / fast market) and Tier C (Solana sentiment) accounts are supporting evidence only; a
separate idea-only registry can surface a meme or a cultural event but can never qualify a
token, and **no account in any tier can produce a PAPER entry or a launch**. Everything outside
the registry is muted by default, the broad radar is off by default, windows are bounded to ten
recent posts per account, account metadata is cached aggressively and a daily request budget is
enforced. Account usefulness is learned from forward outcomes — before, during or after the move
— and material strategy weight requires a real sample, so tier membership is a starting
hypothesis, not a permanent trust grant. A mention is not a buy, and an early call found too
late is `EDGE_CONSUMED`, not an entry.

New admin commands: `/fomo opportunities` (the strongest real setups right now, with identity,
lifecycle, quality, activity, smart/social evidence, `WHY SURFACED`, `QUALITY WARNINGS` and
`WHY NOT ENTRY`), `/fomo trades`, `/fomo performance`, `/fomo exits`, `/fomo lifecycle <mint>`,
`/fomo smartmoney <mint>` and `/fomo sources`. Research visibility never weakens automatic PAPER
eligibility: a `REJECT` or `COOLDOWN` candidate is shown with its reasons, not promoted.

Version 2.35.0 replaces the single 0-100 runner score with a multi-stage funnel and three
separated models: **momentum** (how hard it is accelerating), **opportunity quality** (how
interesting the setup is, dominated by how much of the visible activity looks like independent
demand), and the unchanged fail-closed **safety** assessment where an `UNKNOWN` never becomes a
`PASS`. Discovery and silent watching are unchanged and just as fast — the funnel filters the
*user-facing* feed, not detection. `RAW_DISCOVERY` and `SILENT_WATCH` are silent; only a
`QUALIFIED_RESEARCH` candidate reaches the digest, and only `HEATING_UP` and above earn their own
message. Qualification needs at least two *affirmative* evidence families (flow, holders,
liquidity, price, smart money, forensics) rather than the absence of a catastrophe, so "this
graduated and has a pulse" no longer surfaces anything. Buyer independence is measured over the
wallets actually traced: shared direct funders, bounded upstream funders one hop above, wallets
funded close together with similar amounts that then buy close together, and wallets first active
only hours ago all reduce it. A funder-page truncation bug that let v2.34 report the wrong
transaction as a wallet's funding transfer is fixed — a truncated trace now reports unknown.
Every card carries `WHY SURFACED` and `QUALITY WARNINGS`, the digest ranks and caps instead of
listing, and `/fomo quality` reports funnel throughput, alert precision, the missed-runner
counterfactual over silently rejected tokens, latency percentiles and provider cost by feature.
Risk escalation, setup invalidation, dedupe, exact-mint Fomo/Pump/DEX/Solscan links and the
read-only guarantee are unchanged. No runner path buys, sells, signs, calls J7, or spends SOL.

Version 2.34.0 separates the existing-token runner into a fast, age-prioritized discovery lane
and a slower research digest. Fresh candidates can now produce one deduplicated, non-pinging
exact-mint Discord alert before the digest, then follow a staged 0/15/30/60-second through
15-minute watch. Detection, market-data, eligibility, Discord, entry, and strong-alert times
are persisted independently, and `/fomo latency` reports actual source-to-seen and seen-to-
visible percentiles plus market-cap slippage. The runner also records immutable detection-time
signal/safety snapshots, score progression, post-detection paths, and separate fail-closed scam
risk based on routes, concentration, authorities, liquidity, and bounded public-chain wallet
funding clusters. `/fomo forensic` and `/fomo calibration` expose the evidence read-only. Every
runner card links the exact mint with Fomo first; no runner button buys, sells, signs, calls J7,
or spends SOL.

Version 2.35.1 fixes `/fomo lab mode:test` remaining on Discord's "Investing is thinking...".
Token name and symbol come from on-chain metadata, so an unvetted RAW_DISCOVERY/SILENT_WATCH
observation — exactly what test mode is meant to inspect — could push the rendered card past
Discord's embed limits. Discord rejected the edit and the error escaped the response path,
stranding the deferred interaction. The runner card is now clamped to Discord's documented
limits, every `/fomo lab` exit routes through a resolver that degrades a rejected card to
visible text, and a hard 60-second deadline guarantees the spinner is always replaced by a card
or an error. An unreadable persisted row no longer blanks the cached observation pool. No
scoring, quality threshold, discovery, safety, alert threshold, or buy path changed.

Version 2.33.3 immediately acknowledges every `/fomo lab` invocation before any database or
provider work. Test mode renders a real cached runner observation without contacting live
providers, while empty-cache refreshes and timeouts visibly replace the original ephemeral
response. A persisted, deduplicated, non-pinging research digest now summarizes changed 35+
candidates below the unchanged 70-point public-alert floor every 15 minutes. `/fomo results`
also reports current score distribution, threshold counts, best research candidates, and the
last strong alert/digest/fast-watch activity. No automatic buy, SOL, J7, X, or scoring threshold
changed.

Version 2.33.2 makes `/fomo lab mode:test` provider-independent after the radar has captured a
real observation. It reads the persisted real-token pool and sends the Discord card immediately,
without deferring or contacting DEX/Jupiter/Tracker again. When the pool is genuinely empty it
posts a visible progress response before running one bounded refresh, so Discord never shows an
unexplained permanent spinner.

Version 2.33.1 fixes the live `/fomo lab` Discord response path. The command now resolves the
original deferred ephemeral response, analyzes a bounded candidate set concurrently, returns a
fresh background-radar snapshot immediately when available, and fails visibly within a hard
deadline instead of remaining on “Investing is thinking…”. No scoring, alert, X-budget, buy, or
J7-launch threshold changed.

Version 2.33 adds a separate, shadow-only **Fomo Runner Radar** for existing Solana tokens.
Public DEX nominations remain the cheap broad-discovery stage; promising young pairs enter a
temporary 20-second fast watch that records immutable price, market-cap, liquidity, rolling
volume/transaction flow, holders, Tracker risk, Jupiter route, and financially verified
smart-wallet evidence. Pair creation time is explicitly labeled as a DEX proxy—not an exact
Pump/Fomo graduation claim. `/fomo lab mode:test` deterministically displays real current
existing-token research below the public alert floor, while `/fomo results` measures forward
1m/5m/15m/30m/1h/4h/24h outcomes and simple baselines. The runner has no buy/J7 code path.
Launch Lab's deferred ephemeral Next, Regenerate, Edit, X, and J7 confirmation callbacks now
edit Discord's original webhook response so the visible card and attachment actually change.

Version 2.32.1 makes the existing product testable on demand without weakening a production
gate. `/smartmoney launch-lab mode:test` refreshes the authorized RSS feeds, analyzes the
freshest legitimate current items through the production normalization, scoring, DEX
competition, identity, and 1024x1024 artwork components, and displays them even below the
normal 60-point Launch Lab floor. Below-floor test items are explicitly research-only: the J7
button is disabled, the server rejects a launch attempt, no launch reservation is created, and
no SOL can be spent. An administrator may deliberately run one budget-confirmed official-X
test on the displayed item below the automatic X score floor; the existing 10-Post maximum,
SQLite budget guard, deduplication, cache, and error handling remain authoritative. A test item
that independently satisfies every normal production requirement may transition to the normal
J7 confirmation flow.

Version 2.32 adds a persistent, shared **official-X spend guard** without making X mandatory.
Free RSS/Fomo/DEX/Tracker/on-chain evidence now completes every cheap blocker and preliminary
score before one small targeted recent search can run. SQLite atomically caps targeted checks,
HTTP requests, estimated daily spend, and estimated experiment spend across automatic news,
Launch Lab, and coin callouts. Returned Post and optional User resources are counted separately,
deduplicated for the configured usage day, and cached across restarts. Launch Lab adds an
admin-only `X VERIFY` confirmation that updates the same candidate but cannot call J7 or spend
SOL. `/smartmoney sources`, `/smartmoney status`, and `/smartmoney launch-check` label these
figures as local estimates; the X Developer Console remains the billing source of truth.

Version 2.31 adds an admin-only **J7 Launch Lab** and a read-only launch readiness check.
`/smartmoney launch-check` verifies the configured J7 region, authenticated health endpoint,
Pinata authentication, public launch-wallet SOL balance, daily limits, and persistent SQLite
reservations without calling J7's submit route or spending SOL. `/smartmoney launch-lab`
refreshes the authorized RSS feeds, ranks and persists recent legitimate narratives, collapses
same-story articles, previews source-led 1024x1024 art, and lets an administrator browse, edit,
regenerate, cancel, or deliberately confirm one real J7 launch. Launch Lab uses J7 only and
never silently falls back to direct Pump signing. Its manual 60+ review floor does not lower the
automatic 78+ no-X alert gate. A timed-out J7 submission is persisted as `UNKNOWN_RESULT` and
cannot be blindly retried.

Version 2.30 adds a strict **free/no-X manual launch-candidate lane**. With paid X disabled,
an authorized RSS/Atom story can now become `LAUNCH CANDIDATE — NO X VERIFIED` only at 78+
with a credible publisher, fast detection, strong meme identity, completed low-competition
check, no source contract or blocker, and independent-source confirmation. The card explicitly
says X/social velocity was not verified and exposes the same admin-only J7 button, but never
launches automatically. Paid X remains optional; when enabled, the existing stronger promotion
and velocity rules can still produce `LAUNCH READY — X VERIFIED`. J7 remains the execution
backend rather than a required trend-data source, and every existing SQLite reservation,
duplicate guard, launch-count/SOL cap, artwork, IPFS, and administrator check remains active.

Version 2.29 adds a **J7-style launch-image recommender** without scraping J7. Authorized
RSS/Atom and optional X-stream alerts preserve up to three ranked lead images. The launch card
previews the first recommendation; the launcher downloads the first usable public HTTPS image,
center-crops it to 1024x1024, adds the coin name/ticker plus an `UNOFFICIAL MEME` label, uploads
the final PNG to IPFS, and sends its URL through J7's documented `image_url` field. Missing,
unsafe, non-image, oversized, or broken candidates fall back to the original generated category
art. Successful launches now show explicit Pump.fun and Fomo routes using the returned Solana
mint; Pump.fun is immediate while Fomo may need time to index a new mint.

Version 2.28 added an official **J7 Tracker launch route**. Qualified Discord alerts can send
their name, ticker, public source, capped initial buy, and generated topic artwork through J7's
documented regional deploy API using J7's encrypted per-wallet Solana key. The raw wallet private
key is not sent to or stored by this bot on the J7 path. The client warms the selected regional
endpoint before submitting and preserves the existing duplicate and daily-SOL locks. The coin
art is now an original category-specific 1024x1024 image rather than a plain name/ticker card.
J7's public documentation covers deploy/trade endpoints, but does not document an export API for
its authenticated social tracker; detection therefore remains on authorized RSS/news, optional
official X, and on-chain sources.

Version 2.27 switched the default alert experience to **zero-paid-X Fomo mode**. The master
X cost switch defaults off and prevents recent search and filtered-stream requests even when
an old bearer token remains configured. A free radar checks public DEX/Pump profiles and
trending nominations immediately and every five minutes, then requires complete Tracker risk,
authority, holder, concentration, cross-source liquidity, buy-flow, volume, official X-profile
link, and executable Jupiter-route evidence before sending a pinging `FOMO WATCH`. The card
opens directly in Fomo and clearly says that X views/engagement were not verified. Raw wallet
BUY/SELL, consensus, and PAPER fill cards default muted while all internal tracking continues.

Version 2.26 adds a proactive, budgeted **X contract radar**. It performs one search as soon
as the bot starts and repeats every 30 minutes by default, looking for current Solana/memecoin
launch language and exact contract addresses. One response can nominate several contracts;
the matching X posts are reused by the safety analyzer instead of charging for a second exact-
contract search. Automatic `X COIN WATCH` and `VERIFIED TREND` cards include direct links to
the underlying X posts plus Pump.fun, Jupiter, chart, and Solscan controls. One promoter alone
still cannot generate an automatic public callout.

Version 2.25 added a persistent **paid-X budget pipeline**. Automatic coin candidates now run
the free token, Tracker, DEX-flow, smart-wallet, and executable-route checks before the bot
spends an X recent-search request. SQLite enforces a restart-safe daily X-search ceiling.
Developing exact-contract activity can produce a clearly labeled, non-pinging `X COIN WATCH`,
while only the existing complete 70+ gate produces `VERIFIED TREND`. `/smartmoney sources`
shows recent free rejections, paid checks, X evidence, and reasons instead of hiding every
failed scan without exceeding Discord's 25-command group limit. The continuous paid X news
stream now defaults off, while RSS remains live;
every coin alert includes direct Jupiter BUY and SELL links.

Version 2.24 changed the alert pipeline to **verify first, alert second**. Automatic Discord
coin callouts now publish only `VERIFIED TREND` results. A token must have exact-mint X posts
from several credible crypto accounts, low duplicate/new-account pressure, complete Tracker
rug/bundler/insider/sniper evidence, disabled mint/freeze authorities, acceptable holder and
developer concentration, at least $5,000 liquidity confirmed by both Jupiter and DEX data,
active five-minute buy flow, and an executable $5 Jupiter route below 2% price impact. Name,
ticker, market-cap, volume, paid boosts, or a single influencer cannot qualify a token. Weak,
blocked, and incomplete results remain available through `/smartmoney coin` but are not sent
to the public channel. News `WATCH` cards are also suppressed, so every public news card has
an active `LAUNCH READY` path instead of a dead launch button.

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
and the feed adapter never accepts J7 login credentials.

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
- Runs a fully simulated **$100 SHADOW auto-trader** that automatically buys exactly $10 on every
  eligible research signal, manages the position toward a $2 NET meaningful-profit objective while
  letting a healthy runner keep running, sells realistically, and reports NET expectancy per signal
  family. It spends $0.00 and has no signing path of any kind.

## What it deliberately does not do

- It does not scrape or reverse-engineer Fomo or J7 Tracker. J7 launches use only its documented
  external deploy endpoint and encrypted per-wallet API key.
- It does not scrape or automate KOLScan. Only documented provider APIs and public on-chain
  activity are used for automatic candidate discovery.
- It does not map Fomo usernames to wallets or copy Fomo's leaderboard. Fomo does not expose
  documented public API/webhook credentials in this project; this bot uses an API intended
  for programmatic Solana wallet discovery instead. Fomo-native alerts or legitimately
  obtained public wallet addresses remain usable without scraping.
- It does not promise profit. The paper scoreboard exists specifically to prove or reject
  the strategy with evidence.
- The SHADOW auto-trader does not spend, sign, submit, or hold anything. There is no wallet, no
  keypair, no RPC client and no swap submission path in it, and its route logic prices trades
  without ever being able to place one. A future live executor could reuse the route selection,
  but no such executor exists in this repository and nothing here is connected to one.
- SHADOW does not cherry-pick. Rugs, illiquid exits, route failures and penalised fallback fills
  are all recorded at their real cost; no losing trade is ever excluded from a report.
- It does not trade perpetual futures, borrow funds, or use leverage.
- It never asks for a seed phrase in Discord or chat.
- The v2.36 PAPER research laboratory does not execute anything. The `smart_money_bot.lab`
  package contains no wallet, no signer, no private key handling and no live route; its
  "capital" is a bookkeeping entry. A future live-execution mode would need its own explicit
  acknowledgement, kill switch, dedicated low-balance wallet, per-trade and total exposure caps,
  daily loss limit, route re-verification and restart reconciliation — none of which this
  release enables.
- No public account, however famous, can produce a PAPER entry or a launch. Social evidence is
  supporting only and can never lift a safety, overextension, liquidity, cost or lifecycle
  block.
- It does not treat SOL spend as proof of legitimacy. Bots pay real network fees, so the
  authenticity model reports concentration alongside volume and rewards independence.
- It does not deanonymize wallets or infer where anyone is. Coordination analysis reads only
  publicly observable on-chain relationships, and correlation is never called common ownership.
- It does not crack, decrypt or scrape a J7 Tracker backup, and it does not fabricate account
  identities. The curated registry is the manually reviewed starting list; a legitimate
  plaintext export or documented feed can extend it later.

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

The v2.32.1 discovery, zero-cost Fomo radar, optional X verification, crypto-first launch-radar,
verified-only
callout, selective-entry, daily loss/profit
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
X_NEWS_STREAM_ENABLED=false
X_CRYPTO_TRUSTED_ACCOUNTS=WatcherGuru|CoinDesk|Cointelegraph|solana|pumpdotfun|lookonchain|ArkhamIntel
NEWS_POLL_SECONDS=30
NEWS_MIN_SCORE=45
NEWS_LAUNCH_READY_SCORE=72
NO_X_LAUNCH_CANDIDATES_ENABLED=true
NO_X_LAUNCH_MIN_SCORE=78
LAUNCH_LAB_ENABLED=true
LAUNCH_LAB_MIN_SCORE=60
LAUNCH_LAB_MAX_AGE_SECONDS=3600
LAUNCH_LAB_MAX_CANDIDATES=8
NEWS_X_VERIFY_MIN_SCORE=70
NEWS_X_TREND_CACHE_SECONDS=3600
NEWS_MAX_ALERTS_PER_HOUR=30
NEWS_SOURCE_IMAGE_ENABLED=true
X_SEARCH_MAX_RESULTS=10
X_DAILY_SEARCH_LIMIT=10
X_DAILY_SEARCH_TIMEZONE=America/Los_Angeles
X_PAID_SEARCH_ENABLED=false
X_BUDGET_GUARD_ENABLED=true
X_ESTIMATED_TOTAL_BUDGET_USD=10
X_ESTIMATED_DAILY_BUDGET_USD=0.50
X_MAX_TARGETED_VERIFICATIONS_PER_DAY=10
X_VERIFY_MAX_POSTS=10
X_ESTIMATED_POST_READ_USD=0.005
X_ESTIMATED_USER_READ_USD=0.010
X_BUDGET_PERIOD_ID=experiment-1
X_USER_CACHE_SECONDS=86400
X_RADAR_ENABLED=false
X_RADAR_POLL_SECONDS=1800
X_RADAR_MAX_CONTRACTS_PER_SCAN=3
COIN_X_PREFILTER_MIN_SCORE=60
COIN_WATCH_ALERTS_ENABLED=true
COIN_WATCH_MIN_SCORE=55
FOMO_RADAR_ENABLED=true
FOMO_RADAR_POLL_SECONDS=300
FOMO_RADAR_MAX_CANDIDATES_PER_SCAN=5
FOMO_RADAR_RECHECK_SECONDS=1800
FOMO_RUNNER_ENABLED=true
FOMO_RUNNER_FAST_WATCH_SECONDS=20
FOMO_RUNNER_FAST_WATCH_MINUTES=15
FOMO_RUNNER_FAST_WATCH_MIN_SCORE=35
FOMO_RUNNER_PUBLIC_ALERT_MIN_SCORE=70
FOMO_RUNNER_MAX_FAST_WATCH=5
FOMO_RUNNER_LAB_CANDIDATES=6
FOMO_RUNNER_MAX_GRADUATION_AGE_MINUTES=60
FOMO_RUNNER_OUTCOME_POLL_SECONDS=60
FOMO_RUNNER_DIGEST_ENABLED=true
FOMO_RUNNER_DIGEST_SECONDS=900
FOMO_RUNNER_DIGEST_MIN_SCORE=35
FOMO_RUNNER_DIGEST_MAX_CANDIDATES=3
FOMO_WATCH_MIN_SCORE=50
TRADE_ACTIVITY_ALERTS_ENABLED=false
NEWS_DEX_MATCH_ENABLED=true
NEWS_DEX_MATCH_MIN_LIQUIDITY_USD=2000
NEWS_DEX_MATCH_MAX_AGE_MINUTES=60
NEWS_PAIR_RECHECK_SECONDS=0,30,90,180
```

These values are hypotheses to validate in PAPER mode, not optimized or guaranteed-profit
settings. Use `/smartmoney paper`, `/smartmoney positions`, `/smartmoney paper-trades`, and
`/smartmoney readiness` to evaluate them before changing size.

## Crypto-first news and launch radar

The radar has independent free and optional paid inputs. The zero-cost Fomo radar checks public
DEX/Pump nominations every five minutes. RSS/Atom polls selected U.S. government, market, and
crypto feeds every 30 seconds. The proactive X recent-search loop and official filtered stream
remain available but make no requests while `X_PAID_SEARCH_ENABLED=false`. Routine world,
sports, entertainment, and product feeds are not included by default.

Every X evidence response separates generic authors from crypto-native authors, credible
crypto authors, promotion posts, and exact-contract posts. A generic keyword match cannot
unlock a launch. `X_CRYPTO_TRUSTED_ACCOUNTS` is an auditable pipe-separated allowlist used only
as author-quality evidence; an allowlisted account still cannot bypass duplicate, velocity,
competition, source, or confirmation checks.

Scores below the launch tiers remain internal `WATCH`/`SKIP` evidence. With X unavailable, a
78+ score can become `LAUNCH CANDIDATE — NO X VERIFIED` only when the credible-source,
freshness, meme-clarity, identity, independent-confirmation, and completed low-competition
sub-gates all pass. Crypto-native stories need additional independent pickup; qualifying major
U.S. stories need at least two additional independent confirmations. With paid X enabled, the
existing 72+ path can become `LAUNCH READY — X VERIFIED` only if the crypto-account promotion
and velocity gate also passes. If a source contains a contract,
`COIN FOUND` requires repeated exact-contract promotion by credible crypto accounts; weaker
single-post contracts go through the normal risk callout without becoming a high-confidence
news alert. Existing contracts can never launch a duplicate.

The default X rule follows a deliberately small set of crypto and major U.S. news accounts to
limit paid X reads. Customize `X_RADAR_QUERY` only with valid X recent-search syntax; broad
keywords can consume the daily budget without finding usable contracts. `X_SEARCH_MAX_RESULTS=10`
caps each radar and on-demand response. `/smartmoney sources` and `/smartmoney status` show the
proactive scan time, posts examined, new posts, extracted contracts, budget usage, last X error,
stream state, RSS state, J7 feed state, narrative matcher state, scoring thresholds, and one-click
launch lock.

### Budgeted official-X verification

Paid X is a precision layer after free discovery, not a firehose. The automatic news path may
request X only after the free score reaches `NEWS_X_VERIFY_MIN_SCORE=70` and the source,
freshness, existing-contract, duplicate, competition, and narrative-lane checks pass. Regular
coin callouts use the stricter `COIN_X_PREFILTER_MIN_SCORE=60` plus executable-route and safety
evidence. Launch Lab spends nothing until an administrator presses `X VERIFY` and confirms the
displayed ceiling. None of these actions launches a token; J7 still requires the separate real
launch confirmation.

The initial local estimate uses the configurable published unit assumptions of `$0.005` per
returned Post resource and `$0.010` per returned User resource. The bot requests Posts first and
only batches User hydration after at least two independent Post authors exist. User payloads are
cached for 24 hours and same-day resource IDs are counted once locally. Pricing, X-side billing
deduplication, and actual charges can change; check the X Developer Console before and during the
experiment. The bot does not purchase credits or enable auto-recharge.

If the local daily/experiment cap, request backstop, or verification limit is reached, paid X
fails closed with a readable reason. Missing credentials, timeouts, 429s, server errors, or X
billing failures never disable free `LAUNCH CANDIDATE — NO X VERIFIED` analysis or Launch Lab.
Keep `X_NEWS_STREAM_ENABLED=false` and `X_RADAR_ENABLED=false` for the controlled `$10`
experiment.

J7 documents regional token deploy and trade endpoints. This release uses only the documented
external deploy request. J7 does not currently document a feed/webhook that exports its
authenticated social tracker. If J7 support provides an official RSS/Atom URL, put that URL in
`J7_AUTHORIZED_FEED_URL`; do not copy an internal tracker endpoint or browser cookie.

### One-click token launch setup

This feature spends real SOL even while the copy-trading engine remains in PAPER mode. Keep only
the amount you accept losing in the launch wallet. The preferred J7 path uses the encrypted
per-wallet key shown by J7 under Deploy Settings > Wallets plus an official J7 session token; it
never accepts a recovery phrase or raw private key. Create a Pinata JWT for the generated public
coin image. Never paste any credential into Discord or ChatGPT.

```text
J7_LAUNCH_ENABLED=true
J7_LAUNCH_SESSION_TOKEN=<official J7 JWT>
J7_LAUNCH_API_KEY=<encrypted Solana wallet key copied from J7>
J7_LAUNCH_REGION=na-east
J7_LAUNCH_WALLET_ADDRESS=<public Full Address from J7 Deploy Settings>
J7_LAUNCH_MIN_BALANCE_BUFFER_SOL=0.002
PUMP_LAUNCH_ACK=I_UNDERSTAND_PUMP_LAUNCHES_SPEND_REAL_SOL
PINATA_JWT=<server-side Pinata JWT>
PUMP_LAUNCH_INITIAL_BUY_SOL=0.01
PUMP_LAUNCH_MIN_SCORE=72
PUMP_LAUNCH_MAX_PER_DAY=3
PUMP_LAUNCH_MAX_SOL_PER_DAY=0.05
PUMP_LAUNCH_TIMEZONE=America/Los_Angeles
```

The old direct Pump.fun signer remains available as a fallback by setting
`PUMP_ONE_CLICK_LAUNCH_ENABLED=true` and `PUMP_LAUNCH_PRIVATE_KEY` to a separate capped launch
wallet. If both paths are fully configured, J7 is preferred. Never use a recovery phrase.

The automatic-alert launch button is shown only on `LAUNCH CANDIDATE — NO X VERIFIED` or
`LAUNCH READY — X VERIFIED` alerts and only Discord administrators or configured
`DISCORD_ADMIN_ROLE_IDS` can press it. No alert ever launches automatically. Launch Lab adds one
concise `CONFIRM REAL LAUNCH` screen after its preview; preview, edit, regenerate, next, and cancel
never call J7. SQLite reserves the narrative before any upload
or signature so double clicks and restarts cannot produce a second coin from the same alert.
Metadata labels the asset as a community meme and explicitly says it is not official or
affiliated with the people, brands, publisher, or event in the source. The generated image is
uploaded to public IPFS and sent to J7 as the documented `image_url` field.

When `NEWS_SOURCE_IMAGE_ENABLED=true`, source-provided lead images take priority over generated
category art. The bot never guesses a private J7 image endpoint. If an authorized J7 feed later
includes its recommended image as RSS media/thumbnail/enclosure content, the same parser will use
that exact recommendation automatically. Review the source's image rights before commercial use.

`J7_LAUNCH_WALLET_ADDRESS` is a public Solana address only. Copy the Full Address from J7 Tracker
→ Deploy Settings → Wallets. It is used only for `getBalance`, readiness display, and the
pre-launch balance guard. The encrypted J7 API key is not a raw private key, and this project
never derives a signer from it. Never provide a recovery phrase or private key for the J7 path.

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

## SHADOW auto-trader (v2.39)

The strict PAPER laboratory answers *"is this a trade I would defend?"*. It is deliberately hard
to satisfy, and that is correct — but it means the forward sample grows slowly, and it cannot tell
you whether the **signals themselves** are worth acting on.

SHADOW answers a different, purely empirical question:

> What would have happened if the bot had automatically bought **$10** the moment this signal
> appeared?

### The rules, and why they are rigid

| Rule | Value | Why |
| --- | --- | --- |
| Starting bankroll | `$100` | One number an operator can hold in their head. |
| Every accepted entry | `$10` — exactly | A variable stake makes per-family expectancy uncomparable. |
| Maximum concurrent positions | `5` | Survival modelling applies to fake money too. |
| Maximum total exposure | `$50` | Half the bankroll is never at risk at once. |
| Maximum per-token exposure | `$10` | One position per token; no averaging down, ever. |
| Meaningful NET profit objective | `+$2` | On a $10 stake, "+25%" can be under a dollar after costs. |

`position_usd`, `min_position_usd` and `max_position_usd` are all $10 by construction, and
`ShadowConfig` raises if they disagree. If only $7 of simulated bankroll remains, the entry is
**refused honestly** with `SHADOW_INSUFFICIENT_BANKROLL_FOR_FULL_SIZE` rather than booked as a
smaller trade that would quietly corrupt every expectancy number in the report.

### It is a separate strategy family, not a looser mode

STRICT PAPER and SHADOW never share a bankroll row, a position row, an exit journal or a strategy
version. `smart_money_bot.lab.shadow` does not import the strict entry engine, and the strict
engine does not know shadow exists — both directions are asserted by tests that walk the modules'
syntax trees. SHADOW may simulate a trade STRICT PAPER refuses; that is the entire point of the
experiment, and it can never make STRICT PAPER accept anything.

### The $2 NET objective is not "sell at +$2"

The staged exit engine — the ladder, break-even arming, trailing protection, momentum decay, flow
reversal, smart-money distribution, liquidity deterioration, safety failure, the hard stop and the
time stop — is **reused unchanged**, not reimplemented. What v2.39 adds on top is the question a
percentage ladder cannot express: *has this cleared $2 NET, and is the runner still healthy?*

* Every emergency still fires first and unmodified. Safety failure, a liquidity collapse, a dead
  route and the hard stop are never overridden by a profit figure.
* Below `+$2 NET`, the staged engine decides alone, so a breaking setup still de-risks and a loser
  still stops out.
* At `+$2 NET` with **weak** structure, most of the position is secured.
* At `+$2 NET` with **mixed** structure, half is secured.
* At `+$2 NET` with a **healthy, accelerating** runner, a *small* slice is taken and meaningful
  exposure keeps running.
* Past three times the objective, principal is recovered and a funded moon bag runs on.

"NET" always means after platform fees, network fees, priority fees, price impact and slippage on
**both** legs, so a held position and a closed one are measured on identical terms.

### Fills come from routes that could have filled them

Every simulated buy and sell is priced against whichever venue actually offered the best
executable path at that moment — the Pump bonding curve (constant-product, with its published
fee), a PumpSwap/AMM pool sized by its own depth, or an executable aggregator quote. A position
that enters on the bonding curve can still exit after graduation, because the sell is routed
again from scratch.

Three fill provenances exist and are always persisted and displayed:

1. `EXECUTABLE_QUOTE` — a real quote the bot obtained.
2. `SIMULATED_VENUE_STATE` — arithmetic on live on-chain venue state.
3. `FALLBACK_PENALISED` — nothing executable was available; the observed price is used, charged an
   explicit penalty, and **labelled everywhere** so no report can treat it as real.

A completed bonding curve refuses to price a curve buy instead of inventing one. When the evidence
says there is **no route at all**, no fallback is offered: pricing an exit off the last chart print
when the market is gone is exactly the fantasy fill the contract forbids, so the failure is
recorded as a failure.

### What it learns

Per trade: realized NET, maximum favourable and adverse excursion, **peak NET versus final NET**
(profit given back), and **capture efficiency** — realized NET as a share of the best NET that was
actually available after entry. A $10 position that realized +$3 out of an available +$11.70
captured 25.6%, and that number is the honest way to ask whether the exits are too slow.

Future peak data is **evaluation only**. It is computed after the fact from persisted observations
and can never reach an earlier decision; the no-look-ahead tests assert that entry decisions and
runner-health assessments read no field that postdates them.

Twelve exit policies are compared on the **same single observation stream** — the existing staged
strategy, the $2-NET dynamic strategy, fixed +10/+20/+25/+50/+100%, a trailing runner, the staged
ladder, momentum-adaptive, smart-money-aware, and a no-trade baseline. Because they all replay
persisted rows, comparing twelve policies costs exactly as many provider requests as comparing
one: **none**.

### It cannot spend real money

`SHADOW_REAL_MONEY_SPEND` is `Decimal("0")`, and the invariant is structural rather than
aspirational. No shadow module imports `solders`, `aiohttp`, the market client, the RPC client or
the executor, and none references a keypair, a private key, a message signer, a versioned
transaction, an order execution call or a swap. The test suite and the self-check both parse each
module's AST and assert it.

## Railway deployment

### v2.39.0 Railway changes

Every new setting has a safe code default, so **no Railway variable has to be added**. The shadow
experiment runs at $100 / $10 / 5 positions / $50 exposure out of the box. Nothing here enables
live trading, live wallet copy, automatic J7 execution or automatic SOL spending; there is still
no live-execution path anywhere in the repository.

**ADD:** none required.  **CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_SHADOW_AUTO_ENABLED=true               # the $10 simulated auto-trader
FOMO_SHADOW_PUBLISH_CARDS=true              # entry/exit cards; false keeps the experiment quiet
FOMO_SHADOW_BANKROLL_USD=100
FOMO_SHADOW_POSITION_USD=10                 # every entry is exactly this, never $5
FOMO_SHADOW_MAX_POSITION_USD=10             # must equal FOMO_SHADOW_POSITION_USD
FOMO_SHADOW_MAX_POSITIONS=5
FOMO_SHADOW_MAX_EXPOSURE_USD=50
FOMO_SHADOW_NET_PROFIT_OBJECTIVE_USD=2
FOMO_SHADOW_DAILY_LOSS_CAP_USD=15
FOMO_SHADOW_MAX_PRICE_IMPACT_PERCENT=12
FOMO_SHADOW_MAX_SIGNAL_AGE_SECONDS=900      # how old a signal may be when acted on
FOMO_SHADOW_MAX_FILL_LATENCY_MS=30000       # how stale the quote may be at the fill
FOMO_SHADOW_ALLOW_FALLBACK_FILL=true        # penalised, always labelled, never a real fill
FOMO_SHADOW_MIN_FORWARD_SAMPLE=30
FOMO_LIVE_RADAR_CHANNEL_ID=                 # optional; falls back to the alert channel
FOMO_URGENT_CHANNEL_ID=                     # optional; falls back to the alert channel
```

`FOMO_SHADOW_POSITION_USD` and `FOMO_SHADOW_MAX_POSITION_USD` must be equal — a variable stake
would make the per-family expectancy numbers uncomparable, which is the one thing this experiment
exists to measure, so an unequal pair fails at startup rather than producing a sample nobody can
interpret. There is **no $5 default anywhere** in the shadow strategy.

### v2.38.0 Railway changes

Every new setting has a safe code default, so **no Railway variable has to be added**. Nothing
here enables live trading, live wallet copy, automatic J7 execution or automatic SOL spending;
there is still no live-execution path.

**ADD:** none required.  **CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_FAST_WATCH_PUBLISH_ENABLED=true      # publish the FAST WATCH card (research only)
FOMO_FAST_WATCH_MIN_SCORE=55
FOMO_FAST_WATCH_MAX_QUEUE_AGE_SECONDS=300 # nothing publishes as "early" after this
FOMO_FAST_WATCH_COOLDOWN_SECONDS=1800
FOMO_FAST_WATCH_MAX_PER_HOUR=12
FOMO_NOTABLE_ALERTS_ENABLED=true
FOMO_NOTABLE_MIN_TRADE_USD=250
FOMO_NOTABLE_PING_ENABLED=false           # pings stay OFF by default
FOMO_NOTABLE_MAX_SIGNAL_AGE_SECONDS=900
FOMO_CATALYST_ALERTS_ENABLED=true
FOMO_CATALYST_MAX_EVENT_AGE_SECONDS=3600
FOMO_CATALYST_PING_ENABLED=false          # pings stay OFF by default
FOMO_CONFLUENCE_ALERTS_ENABLED=true
FOMO_ALERT_ENRICHMENT_ENABLED=true        # stage 2 edits the card in place
FOMO_ALERT_ENRICHMENT_DELAY_SECONDS=45
```

Schema changes are five additive tables — `notable_wallets`, `notable_wallet_events`,
`catalyst_events`, `catalyst_token_links` and `fast_alerts` — all `CREATE TABLE IF NOT EXISTS`.
No existing table, column or row is dropped, renamed or rewritten.

### v2.37.0 Railway changes

Every new setting has a safe code default, so **no Railway variable has to be added**. Nothing
here enables live trading; there is still no live-execution path.

**ADD:** none required.  **CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_FAST_WATCH_ENABLED=true             # research-only early WATCH visibility
FOMO_FAST_WATCH_MIN_ACTIONABILITY=55
FOMO_CURRENT_RADAR_SUPPRESS_STALE=true   # keep deteriorated tokens out of the current radar
FOMO_DISCOVERY_SOURCE_NAME=dexscreener_trending
```

Schema change is one additive table, `runner_discovery` (`CREATE TABLE IF NOT EXISTS`), holding
the cheap-discovery ledger and per-stage pipeline times. No existing table, column or row is
dropped, renamed or rewritten.

### v2.36.0 Railway changes

Every laboratory setting has a safe code default, so **no Railway variable has to be added for
this release**. The entries below are optional overrides. Nothing here enables live trading;
there is no live-execution variable, because there is no live-execution path.

**ADD:** none required.

**CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_LAB_ENGINE_ENABLED=true            # persist lifecycle/decisions/positions
FOMO_LAB_AUTO_PAPER_ENABLED=true        # open simulated positions automatically
FOMO_LAB_BANKROLL_USD=100
FOMO_LAB_POSITION_USD=5
FOMO_LAB_MAX_POSITION_USD=10
FOMO_LAB_MAX_CONCURRENT_POSITIONS=5
FOMO_LAB_MAX_TOTAL_EXPOSURE_USD=30
FOMO_LAB_DAILY_LOSS_CAP_USD=15
FOMO_LAB_MIN_LIQUIDITY_USD=15000
FOMO_LAB_MAX_PRICE_IMPACT_PERCENT=2.5
FOMO_LAB_MAX_SLIPPAGE_PERCENT=2.5
FOMO_LAB_MIN_NET_EDGE_PERCENT=12
FOMO_LAB_PLATFORM_FEE_BPS=100
FOMO_LAB_SLIPPAGE_BPS=80
FOMO_LAB_PRIORITY_FEE_USD=0.02
FOMO_LAB_NETWORK_FEE_USD=0.0008
FOMO_LAB_COOLDOWN_SECONDS=3600
FOMO_LAB_MIN_FORWARD_SAMPLE=30
FOMO_SOCIAL_RADAR_ENABLED=false         # broad radar stays OFF by default
FOMO_SOCIAL_POSTS_PER_ACCOUNT=10
FOMO_SOCIAL_DAILY_REQUEST_BUDGET=40
```

Schema changes are additive only (`lab_token_lifecycle`, `lab_token_events`, `lab_decisions`,
`lab_positions`, `lab_exits`, `lab_bankroll`, `lab_publications`, `lab_wallet_reputation`,
`lab_social_signals`, `lab_account_performance`, `lab_account_cache`, `lab_social_budget`,
`lab_strategy_registry`, `lab_token_identity`). No existing table, column or row is dropped,
renamed or rewritten, and every statement is `CREATE TABLE IF NOT EXISTS`, so re-running the
migration is safe.

### v2.35.0 Railway changes

**Nothing is required.** Every new variable has a working default, and the upgrade migrates an
existing database in place — no runner row, snapshot, outcome or forensic record is dropped.

**ADD (optional — tune only after `/fomo calibration` shows enough forward outcomes):**

```text
FOMO_RUNNER_MIN_EVIDENCE_FAMILIES=2
FOMO_RUNNER_MIN_OPPORTUNITY_SCORE=45
FOMO_RUNNER_HEATING_MIN_OPPORTUNITY=55
FOMO_RUNNER_HEATING_MIN_MOMENTUM=60
FOMO_RUNNER_ENTRY_MIN_OPPORTUNITY=65
FOMO_RUNNER_ENTRY_MIN_MOMENTUM=50
FOMO_RUNNER_MIN_INDEPENDENCE_RATIO=0.45
FOMO_RUNNER_MAX_CLUSTER_SUPPLY_PERCENT=25
FOMO_RUNNER_FRESH_REQUIRES_QUALIFICATION=true
FOMO_RUNNER_FORENSICS_MAX_WALLETS=14
FOMO_RUNNER_FUNDING_MAX_DEPTH=2
FOMO_RUNNER_WALLET_HISTORY_LIMIT=60
FOMO_RUNNER_EXCLUDED_FUNDERS=
```

`FOMO_RUNNER_EXCLUDED_FUNDERS` takes a comma- or pipe-separated list of addresses that must never
form a wallet cluster. Protocol accounts are already excluded in code; add centralised-exchange
hot wallets here as you identify them, because a shared exchange withdrawal address would
otherwise look like a shared funder.

**CHANGE (recommended):**

```text
FOMO_RUNNER_DIGEST_MAX_CANDIDATES=5
```

The digest now ranks and caps rather than listing everything above a floor, so a smaller number
means "the best few worth looking at" rather than "fewer coins exist".

### v2.34.0 Railway changes

The application only reads these variables; deploying this version does not overwrite values
already stored in Railway.

**ADD:**

```text
FOMO_RUNNER_FRESH_ALERT_ENABLED=true
FOMO_RUNNER_FRESH_MAX_AGE_SECONDS=300
FOMO_RUNNER_FRESH_WATCH_ENABLED=true
FOMO_RUNNER_FRESH_WATCH_SECONDS=15
FOMO_RUNNER_FRESH_WATCH_MAX=15
FOMO_RUNNER_FORENSICS_MIN_SCORE=50
FOMO_RUNNER_INVALIDATION_DRAWDOWN_PERCENT=50
FOMO_RUNNER_INVALIDATION_LIQUIDITY_DECLINE_PERCENT=35
FOMO_RUNNER_INVALIDATION_LIQUIDITY_FLOOR_USD=500
```

**CHANGE:**

```text
FOMO_RADAR_POLL_SECONDS=60
FOMO_RADAR_MAX_CANDIDATES_PER_SCAN=12
FOMO_RUNNER_FAST_WATCH_SECONDS=15
FOMO_RUNNER_FAST_WATCH_MINUTES=15
FOMO_RUNNER_FAST_WATCH_MIN_SCORE=20
FOMO_RUNNER_MAX_FAST_WATCH=12
FOMO_RUNNER_DIGEST_ENABLED=true
FOMO_RUNNER_DIGEST_SECONDS=180
FOMO_RUNNER_DIGEST_MIN_SCORE=15
FOMO_RUNNER_DIGEST_MAX_CANDIDATES=10
FOMO_RUNNER_PUBLIC_ALERT_MIN_SCORE=70
```

Start with these bounded values. The current public discovery source is DEX Screener profile and
boost polling; no undocumented Fomo endpoint or unverifiable graduation event is assumed. Pair
creation time remains a labeled source proxy, while unavailable chain/graduation times remain
null. If Railway logs provider throttling, keep the 60-second radar interval and reduce
`FOMO_RADAR_MAX_CANDIDATES_PER_SCAN` and `FOMO_RUNNER_MAX_FAST_WATCH` to 8 before increasing
poll frequency. Jupiter quote checks are deliberately rate-limited and both buy and sell routes
must be known before an entry-quality classification.

**KEEP:** all Launch Lab, J7, PAPER, wallet-discovery, smart-wallet, X-budget, Pinata, and
persistent SQLite-volume settings.

**KEEP OFF:** `ENABLE_LIVE_TRADING=false`. Runner actions and links are research/navigation
only.

### v2.33 Railway changes

**ADD:**

```text
FOMO_RUNNER_ENABLED=true
FOMO_RUNNER_FAST_WATCH_SECONDS=20
FOMO_RUNNER_FAST_WATCH_MINUTES=15
FOMO_RUNNER_FAST_WATCH_MIN_SCORE=35
FOMO_RUNNER_PUBLIC_ALERT_MIN_SCORE=70
FOMO_RUNNER_MAX_FAST_WATCH=5
FOMO_RUNNER_LAB_CANDIDATES=6
FOMO_RUNNER_MAX_GRADUATION_AGE_MINUTES=60
FOMO_RUNNER_OUTCOME_POLL_SECONDS=60
FOMO_RUNNER_DIGEST_ENABLED=true
FOMO_RUNNER_DIGEST_SECONDS=900
FOMO_RUNNER_DIGEST_MIN_SCORE=35
FOMO_RUNNER_DIGEST_MAX_CANDIDATES=3
```

These defaults are shadow-research hypotheses, not proven trading thresholds. The runner never
buys and never calls J7.

**CHANGE:** nothing.

**RESTORE:**

```text
LAUNCH_LAB_MIN_SCORE=60
```

The deterministic Launch Lab test mode no longer requires the temporary 50-point production
floor.

**KEEP:** `NO_X_LAUNCH_MIN_SCORE=78`, `NEWS_X_VERIFY_MIN_SCORE=70`,
`PUMP_LAUNCH_MIN_SCORE=72`, all J7/Pinata/public-wallet settings, creator-buy and daily launch
caps, shared X budget limits, PAPER/discovery variables, and the existing persistent SQLite
volume. Do not reset the database.

**KEEP OFF:** `ENABLE_LIVE_TRADING=false`. Keep `X_PAID_SEARCH_ENABLED=false` if zero-cost mode
is desired. Runner research still works; `VERIFY ON X` stays unavailable until official X is
deliberately re-enabled.

After Railway deploys, run `/smartmoney launch-check`, then verify the repaired Launch Lab with
`/smartmoney launch-lab mode:test`. Existing `/smartmoney` already uses Discord's 25-child
command limit, so the separate existing-token product is exposed without removing any command:
run `/fomo lab mode:test`, test Next/Refresh, and use `/fomo results` as observations mature.

### v2.32.1 Railway changes

**ADD:** nothing.

**CHANGE:** nothing.

**REMOVE:** nothing.

**UNCHANGED:** `NO_X_LAUNCH_MIN_SCORE=78`, `NEWS_X_VERIFY_MIN_SCORE=70`,
`PUMP_LAUNCH_MIN_SCORE=72`, `LAUNCH_LAB_MIN_SCORE=60`, every J7/Pinata/public-wallet secret
or setting, the creator buy, daily launch count/SOL caps, the X budget settings, all PAPER and
wallet-discovery settings, and the existing SQLite database. Do not reset the database.

After deploying, run `/smartmoney launch-check`, then `/smartmoney launch-lab mode:test`.
The latter is research-only unless the displayed real item independently passes the normal
production Launch Lab rules.

### v2.32 Railway changes

**ADD:**

```text
X_API_BEARER_TOKEN=<secret official X app-only bearer token>
X_BUDGET_GUARD_ENABLED=true
X_ESTIMATED_TOTAL_BUDGET_USD=10
X_ESTIMATED_DAILY_BUDGET_USD=0.50
X_MAX_TARGETED_VERIFICATIONS_PER_DAY=10
X_VERIFY_MAX_POSTS=10
X_ESTIMATED_POST_READ_USD=0.005
X_ESTIMATED_USER_READ_USD=0.010
X_BUDGET_PERIOD_ID=experiment-1
X_USER_CACHE_SECONDS=86400
```

**CHANGE:**

```text
X_PAID_SEARCH_ENABLED=true
X_SEARCH_MAX_RESULTS=10
X_DAILY_SEARCH_LIMIT=10
COIN_X_PREFILTER_MIN_SCORE=60
```

**KEEP OFF:**

```text
X_NEWS_STREAM_ENABLED=false
X_RADAR_ENABLED=false
```

**REMOVE:** nothing.

**UNCHANGED:** `NEWS_X_VERIFY_MIN_SCORE=70`, the automatic no-X `78+` threshold, all existing
J7 session/API-key/region/wallet, Pinata, launch acknowledgment, creator-buy, daily count/SOL
caps, discovery, PAPER, and database variables. Do not reset or delete the SQLite database.

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
# Optional: enables budgeted official-X verification when X_PAID_SEARCH_ENABLED=true.
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
| `/smartmoney scan` | Run a wallet scan immediately. |
| `/smartmoney leaderboard` | Show the 24-hour or 7-day risk-adjusted ranking. |
| `/smartmoney paper` | Show P&L, drawdown, profit factor, expectancy, and 24H progress. |
| `/smartmoney positions` | Browse open positions with page, refresh, token-link, and confirmed manual PAPER-sell buttons. |
| `/smartmoney paper-trades` | Browse every fill, realized ROI, quote details, and exit reasons with page buttons. |
| `/smartmoney readiness` | Show the 14-day, sample-size, expectancy, drawdown, and quote gates. |
| `/smartmoney paper-reset` | Reset the paper challenge after exact confirmation. |
| `/smartmoney mode` | Show or set alerts, paper, or live mode. |
| `/smartmoney pause` | Pause/resume monitoring. |
| `/smartmoney kill-switch` | Immediately pause discovery, scanning, and new paper actions. |
| `/smartmoney launch-check` | Read-only J7/IPFS/public-wallet/limit/reservation readiness check. |
| `/smartmoney launch-lab mode:production` | Browse, edit, re-art, and deliberately confirm a qualifying recent J7-only candidate. |
| `/smartmoney launch-lab mode:test` | Immediately inspect real recent RSS evidence and test art/X without bypassing live J7 eligibility. |
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

### PAPER research laboratory (v2.36, admin only)

| Command | What it answers |
| --- | --- |
| `/fomo opportunities [count]` | What are the strongest real setups right now, and why is each one not an entry? |
| `/fomo trades` | Which simulated positions are open, with GROSS and NET results, peak unrealized and drawdown? |
| `/fomo performance` | Simulated bankroll, realized NET, unrealized separately, win rate, profit factor, expectancy, reach rates, fresh vs re-entry, and whether the sample is too small to mean anything. |
| `/fomo exits` | The immutable partial/full exit journal with its per-exit cost breakdown. |
| `/fomo lifecycle <mint>` | Everything the lab remembers about one exact mint: first surface, historical peak, drawdown, alerts, PAPER history, public-signal history and state transitions. |
| `/fomo smartmoney <mint>` | Independent smart-wallet evidence, posture and warnings for one exact mint. |
| `/fomo sources` | The curated Tier A/B/C and idea-only registry, and the guarantee that none of them can enter or launch. |

Every one of these is read-only research. They show `WAIT`, `REJECT`, `COOLDOWN` and
`REENTRY_WATCH` candidates on purpose; seeing a candidate never makes it entry eligible.

### SHADOW auto-trader (v2.39, admin only)

| Command | What it answers |
| --- | --- |
| `/fomo shadow` | **Is the $100 shadow account making money?** Current bankroll, realized and unrealized NET, ROI, open positions, exposure, win rate, profit factor, expectancy, max drawdown, and whether a circuit breaker is holding new entries. |
| `/fomo shadow view:trades` | Every open $10 position: signal family, entry MC, NET PnL, MFE, MAE, peak NET, profit given back, how much is still held, the route it filled on, and why it is still being held. |
| `/fomo shadow view:results` | The full forward record — win rate, average/median trade, average winner and loser, profit factor, expectancy, drawdown, the +$2 NET hit rate, +10/+20/+25/+50/+100/+200/+500% reach, capture efficiency and profit giveback — **separated by signal family**, never blended. |
| `/fomo shadow view:venues` | Fill quality for the same simulated $10 trade per venue: fills, average slippage, price impact, quote latency, total modeled cost and NET result. |
| `/fomo shadow view:policies` | What eleven alternative exit policies would have realized on the same persisted observations, plus the no-trade baseline. Costs zero provider requests. |

A Discord subcommand *group* cannot itself be invoked, so `/fomo shadow` is one command with a
`view:` option rather than a group with no default. That keeps the account answer one keystroke
away, which is the point of section 44: **the headline is never buried under diagnostics.**

Nine signal families are tracked as separate cohorts — `FAST_WATCH`, `FRESH_RUNNER`,
`NOTABLE_TRADER_EARLY`, `NOTABLE_TRADER_LATE`, `BREAKING_CATALYST`, `CATALYST_WATCH`,
`CONFLUENCE_WATCH`, `QUALIFIED_RESEARCH` and `STRICT_PAPER_ENTRY` — because a blended number
cannot tell you whether to keep watching FAST WATCH or only notable wallets.

`/smartmoney status` and `/fomo realtime` both carry the shadow block: whether it is on, the $10
position size, the bankroll, the position and exposure caps, the NET objective, how long ago the
last simulated entry and exit fired, which channels the two lanes publish to, and
**REAL MONEY: DISABLED — SHADOW_REAL_MONEY_SPEND = $0.00**.

### Real-time alpha lane (v2.38, admin only)

| Command | What it answers |
| --- | --- |
| `/fomo realtime` | Is the wallet stream connected, which alert lanes are on, how many alerts were published versus suppressed, and how long ago the last one fired? |
| `/fomo notable` | What public wallet activity did the realtime lane actually observe, with size, entry market cap, chain-event age and detection delay? |
| `/fomo catalysts` | Which real-world events currently grade as credible, with their source-integrity markers? |
| `/fomo catalyst <mint>` | How strongly is this exact mint connected to a graded event — and is that connection OFFICIAL or merely a name match? |
| `/fomo confluence` | Where do independent wallets, a graded event and current market evidence agree right now? |

These are visibility, not eligibility. `/fomo realtime` reports **live autonomous execution:
DISABLED** because that is a structural fact about the build, not a toggle. `/smartmoney status`
carries the same realtime block.

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

The v2.39 suite adds the SHADOW auto-trader: that every signal family deploys exactly $10 and
never $5, that the book stops at 5 positions and $50, that a restart or a replayed signal cannot
duplicate a position, that a completed bonding curve refuses to price a curve buy, that a missing
route is recorded rather than filled at a chart price, that `+$2 NET` secures a broken runner and
does *not* dump an accelerating one, that capture efficiency and profit giveback are computed
correctly, that counterfactuals and entry decisions cannot read the future, and that no shadow
module contains a signer, a wallet, an RPC client or a swap submission path.

`python tests/run_selfcheck.py` re-asserts the non-negotiables — including the $10 entry size, the
$100/5/$50 caps, the STRICT PAPER separation and `SHADOW_REAL_MONEY_SPEND = 0` — without pytest.
