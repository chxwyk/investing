# Smart Money Copy Bot

A Railway-ready Discord bot that **automatically discovers profitable public Solana
wallets**, independently verifies strict 24-hour and 7-day performance, rotates toward recent
Pump.fun memecoin activity every five minutes, reconstructs swaps from confirmed on-chain
transactions, and mirrors every newly detected hot-wallet swap in PAPER mode. PAPER can run
as either a forced source-price observation ledger or an executable Jupiter quote-shadow
trial; the two answer different questions and are labeled separately.

Version 2.43.0 gives the bot **its own terminal-grade intelligence, built from public
on-chain data**. v2.42 made Trending the primary universe but ran it on `TRENDING_PROXY` —
DEX Screener's boost and profile ordering, which is *paid placement*. A token ranked there
because someone bought a slot, not because anyone was trading it. That is now one small
capped feature among many, and the ranking is computed from activity.

**Three universes, two of them primary.** `TRENDING` (attention and momentum) and
`PUMPFUN TRENCHES` (new, bonding and near-graduation candidates) are the primary lanes;
legacy graduated research is retained as secondary. The old failure mode — *wait until a
token graduates, then analyse it* — is gone: the engine now tracks a coin from creation
through the curve, graduation, PumpSwap and continuation.

**Everything runs on public Solana RPC and Pump.fun program state.** Bonding progress is
decoded from the curve account's real reserves, not guessed from age. Holder concentration
comes from `getTokenLargestAccounts` with the bonding curve and pool excluded, because
counting the liquidity pool as a whale makes every token look either dangerously
concentrated or artificially safe depending on stage. Wallet age comes from signature
history; bundles from slot grouping. If DEX Screener is degraded, Fomo is unavailable and
Solana Tracker has no credits, this keeps working.

**Source honesty, extended to everything.** Terminal (formerly Padre) publishes
documentation describing the *kinds* of signal active traders care about — multi-timeframe
momentum, bonding progress, dev holding, bundles, fresh wallets, holder concentration. That
is a legitimate design reference, and it is all this release used. Their ranking is
proprietary and their feed is not something this deployment can legitimately read, so what
we compute is labelled `PUBLIC_TRENDING_MODEL` and never Terminal's or Fomo's.
`assert_honest_ranking_name` makes a dishonest label a crash rather than a card. Nothing
reads a logged-in session, reuses cookies or auth tokens, calls a private endpoint, or
reverse-engineers a proprietary algorithm.

Four things drove the work:

* **One 5-minute number cannot describe a trend.** Five windows — 1m, 5m, 15m, 30m, 1h — are
  now computed **independently**, with no leakage; a window with too few samples reports
  nothing rather than borrowing its neighbour's reading. The shape *across* them is the
  signal, and it is read from **velocity**, not level: a spike in the last minute is inside
  the fifteen-minute window too, so only per-minute rates separate "just started moving"
  from "has been moving for an hour and is stalling".
* **Raw counts are not demand.** 1,000 buys from 4 bots must not score like 300 buys from 250
  independent participants. Wallets that share a funder, were funded in one burst, or land in
  the same slot collapse to **one actor**, so twenty sybils funded from one source contribute
  exactly as much independent demand as one wallet. Fresh wallets are tracked because their
  absence is informative — never because they are bullish.
* **Launch → observation was still unmeasured.** v2.41 fixed observation→alert; v2.42 improved
  Trending. Neither fixed the bot seeing a coin late in the first place. A public
  `logsSubscribe` on the Pump.fun program now detects creation in the same second it lands,
  turning first-observation from *up to a poll interval* into **sub-second**, with the poll
  demoted to a safety net. Latency is recorded per source, so a slow stream and a slow poll
  are separable problems.
* **A single risk number hides which thing is wrong.** Ten dimensions are graded separately —
  liquidity, dev, concentration, bundle, related wallets, fresh-wallet cluster, route,
  sellability, story and thesis provenance. `UNKNOWN` never becomes `PASS`; a provider outage
  is not a finding about a token; and a confirmed sell failure, collapsed pool, lost route or
  hard malicious evidence outranks every positive signal in the system.

Version 2.42.0 makes **FOMO Trending the primary research universe**. The product question is no
longer "which token just graduated?" but *what is trending right now, why, who is talking about
it, are those theses real or bullshit, who is buying, and is there still a tradeable
continuation?* Graduated discovery is **demoted to a secondary lane, not deleted**.

**What this release can honestly see.** There is no documented public Fomo Trending API available
to this deployment and no authorised Fomo feed is configured by default, so the bot does **not**
pretend to have one. Provenance is a persisted, first-class value with exactly three states:
`FOMO_TRENDING` (only when an administrator supplies `FOMO_TRENDING_API_URL`), `TRENDING_PROXY`
(a public DEX Screener approximation of attention), and `NO_SOURCE_CONFIGURED`. Out of the box
this runs as `TRENDING_PROXY`, and every card, ledger row and status surface says so. No code path
promotes the proxy to `FOMO_TRENDING` — not on a heuristic, not on a hostname, not on a response
shape. Nothing scrapes a protected endpoint, reuses a session, replays a cookie or works around a
rate limit.

**Trending is not safety.** The release deliberately encodes none of `TRENDING = SAFE`,
`VERIFIED = CANNOT RUG`, or `THESIS = FACT`. Attention, story, thesis, public chatter, smart money
and market structure are all *evidence*; a confirmed sell failure or a collapsed pool beats every
one of them.

Three concrete defects drove the work:

* **The threshold cliff.** A candidate with ~1,532 buys, ~789 sells, a 1.94 buy/sell ratio and
  heavy volume scored ~50 against a 55 gate with a 2.00 organic requirement. It produced a silent
  heads-up and then ran. 1.94 and 2.00 are not different universes, so every component of the new
  **Trending edge score** is a continuous ramp between a floor and a target rather than a boolean,
  and strength in one dimension can compensate for a marginal miss in another. Everything stays
  bounded and auditable — each contribution is returned with its own line.
* **The 30-minute wait.** Even with better scoring, a candidate that missed by a hair was not
  reconsidered for a full recheck window. **HOT WATCH** fixes that: a strong near miss is
  reevaluated every 45 seconds for a bounded window from cached evidence, does **not** ping on
  entry, promotes with exactly **one** ping when the evidence genuinely strengthens, and expires
  silently when it does not. Promotion lateness is measured honestly — a heads-up at $500K and a
  promotion at $1M is recorded as a 100% late promotion, not dressed up as early.
* **The wallet lane was dead and said nothing useful.** `/fomo realtime` reported
  `DISCONNECTED / subscriptions: 0 / reconnects: 0`. Zero reconnects means nothing ever *failed* —
  the lane was never started or never subscribing. Three unrelated faults produced that identical
  output. The stream now reports a **named state** (`DISABLED_BY_CONFIG`, `NO_WS_URL`,
  `NO_WALLETS_SUBSCRIBED`, `CONNECTING`, `CONNECTED`, `RECONNECTING`, `STALE_NO_TRAFFIC`), runs its
  supervisor even when it cannot connect, detects an open-but-silent socket and rebuilds its
  subscriptions, counts every reconnect attempt, and escalates to the operator when the lane stays
  down — losing smart-money intelligence silently is not acceptable.

And the release adds the measurement that decides whether any of this was a good idea: a second,
**completely isolated** `$100 / $10 / 5 positions / $50 exposure` forward experiment. The legacy
shadow experiment is untouched — same version string, same bankroll, same history — and the two
books are partitioned by `strategy_version` at the storage layer, so the same mint can be open in
both at once without either seeing the other. `/fomo profit view:universes` reports which one
actually makes more money and which one actually gets rugged less, and refuses to name a winner
until both have a real sample.

Version 2.41.0 is the **FOMO alpha engine**: ultra-early discovery and story-first alpha. It
exists to fix one measured product failure — the bot recorded Grok Pocket at a **~$31.18K** market
cap and the operator did not get useful visibility until **~$61.49K**, a **+97%** move that had
already happened by the time the card arrived. "First seen $31.18K" was printed as historical
trivia beside a doubled price.

The cause was architectural, not a threshold. Every operator-visible alert sat behind
`analyze_runner` — deep enrichment with a 30-second budget, gathered across a whole batch, so the
slowest mint delayed all of them, and a mint that missed the bar on its first pass was not looked
at again for the full 30-minute recheck window. So the cheap operator lane now runs **before** the
deep gather, from one DEX snapshot the pipeline already fetches: an ORGANIC RUNNER verdict is
produced in **~5 ms** where enrichment takes up to 20 seconds. The first-seen market cap is written
**once** and can never be rewritten, and an alert that arrives after the move is labelled
**EDGE CONSUMED** instead of being dressed up as early.

Being early is worthless if the channel fills with noise, so the restraint is built in the same
release: a tier that pings needs a *named serious evidence category*, a large buy must be
corroborated by independent follow-on demand before it counts as demand, a **creator self-buy is
never demand**, and — because **MINT IS IDENTITY** — a token that merely copied a campaign URL can
never inherit the real story's credibility.

Version 2.40.0 was a **profit-first forward optimization**. It builds nothing new for its own
sake; every change traces either to a failure observed in the production logs or to a number the
account was leaking. Three things came straight out of a live log window:

* **A paid provider was being hammered while it was down.** Solana Tracker returned
  `HTTP 403 Insufficient credits` on *every* discovery refresh, once a minute, indefinitely —
  roughly 1,440 failing paid requests a day against an intended budget of about 40. Two defects
  combined: the refresh throttle only engaged when the candidate pool was **non-empty**, so it
  disengaged exactly when the provider was failing, and the discovery client had no backoff at
  all. Both are fixed, and a failing provider is now called *less*, not more.
* **Failures were logging as nothing.** Dozens of `Fomo fresh analysis 55sWLQ39: ` lines with
  nothing after the colon — all of them `TimeoutError`, whose `str()` is empty. The one fact an
  operator needed (a 30-second analysis budget was being blown, starving every downstream lane)
  was the one fact the log omitted.
* **A provider outage would have been read as a token failure.** The shared exit engine de-risks
  50% whenever safety reads `UNKNOWN` while a position is in profit. With Tracker 403ing for
  hours, that would have half-sold every profitable shadow position for a reason that had nothing
  to do with any token. Section 8's rule is now enforced: a *confirmed* hard failure still exits
  immediately and in full; a provider outage with a healthy route, healthy liquidity and healthy
  flow is monitored instead.

On top of those, forward results — not opinion — now decide which signal families are ranked,
pinged and traded, with **bounded, versioned, auditable** weights that shrink toward the pool and
do nothing at all below a minimum sample. One coin doing 10x cannot move the ranking.

Version 2.39.0 added the **SHADOW auto-trader**: a completely simulated $100 account that
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

## Early-candidate promotion and top-trader intelligence (v2.44)

A production card read `RESEARCH CANDIDATE — VALIDATION PENDING (EARLY HEADS-UP)`:
a three-minute-old token at **$71.93K** with **$21.09K** liquidity, **78 buys against 48
sells**, five-minute volume at **1.21x liquidity** and price **up 46.48%**. It scored
**76/100** — twenty-one points clear of the runner bar of 55 — and never interrupted anyone.

### The exact reason, reconstructed

The verdict was correct at the instant it was made. A runner needs a *serious evidence
category*, and none was present: `EV_ORGANIC` wanted a 2.0 buy/sell ratio and the flow was
1.625; no large buy had been observed, so there was no market-structure impulse; and there
was no story, wallet or catalyst evidence yet. The persisted suppression reason was
`NO_SERIOUS_EVIDENCE_CATEGORY`, with `edge_state = EDGE_AVAILABLE`.

**It did not enter Hot Watch**, so there were no rechecks and no promotion. Hot Watch
existed, but only the Trending board could open one — and this candidate came from the early
lane and never appeared on a board. The early lane published one card and moved on.

That is the whole gap: **a strong near-miss got a single look.**

### What changed

| | Before | After |
| --- | --- | --- |
| Near-miss handling | One card, then a 15-minute cooldown | `should_open_watch()` opens a bounded hot watch on any visible, non-late, unpinged candidate at ≥45 |
| Re-evaluation | Only the radar cycle | A 30-second timer **and** event-driven rechecks that bypass it entirely |
| Promotion | None from the early lane | `evaluate_promotion()` against the baseline captured at the heads-up |
| The record | A suppression row | The full baseline, every recheck, and the exact decision — queryable |

### Promotion is a difference, not a retry

Every decision compares the picture *now* against the baseline captured when the watch
opened. A score drifting from 76 to 77 is not news. Seven evidence families can promote —
`MARKET_ACCELERATION`, `KNOWN_TRADER`, `HOLDER_EXPANSION`, `STORY`, `THESIS`, `CATALYST`,
`TRENDING` — and two *independent* ones become `MULTI_SOURCE_CONFLUENCE`. Market
acceleration and a trending-board move collapse to one family between them, so a single
market observation cannot manufacture its own corroboration.

Three things look like good news and are not:

- **Holders growing while ownership concentrates** is accumulation by someone, not
  distribution to many → `OWNERSHIP_CONCENTRATION_WORSENING`.
- **Known wallets present but selling** into the people arriving now →
  `KNOWN_MONEY_DISTRIBUTING`.
- **Evidence that arrives after the move** → `EDGE_CONSUMED_BEFORE_PROMOTION`.

Promotion latches: exactly one operator ping per candidate, at the moment of promotion, and
`EARLY_PROMOTION:<mint>` is the deduplicator behind that latch.

### Who is actually buying

`lab/toptraders.py` builds a participant picture for **one exact mint** from public fills.
It is built around three refusals:

- **A large wallet is not a smart wallet.** Size decides *ranking*; forward history decides
  *weight*, and a `PROVEN_EARLY` label on fewer than 8 observed outcomes carries none.
- **Five wallets sharing one funder are one actor.** Confirmations collapse per cluster
  before they are counted, so a sybil group cannot confirm itself.
- **A position is a story over time.** `BUYING → ADDING → HOLDING → PARTIAL_SELLING →
  DISTRIBUTING → EXITED`, decided on *tokens* rather than dollars — a wallet that sold half
  its tokens into a tripling price took more money out than it put in while still holding
  half the position, and a dollar rule calls that distribution.

`known_money_flow()` then says which side the wallets that already know this token are on.
Accumulating and distributing print the same candle.

### Holders as a shape

`26 → 51 → 94`, with the time between observations. Acceleration needs three samples,
because it takes two rates to compare rates — two samples return `None`, never "flat". A
stale read racing a fresh one is dropped rather than folded in, so it cannot invent a dip.
Concentration is tracked as a trend across every recorded sample: `48% → 31%` and
`20% → 42%` are opposite stories about the same level.

Holder *counts* come from the Trending board when it publishes them and are `None`
otherwise. The public RPC methods this bot uses cannot count holders — the largest-accounts
call returns twenty — and a guessed count would feed the promotion gate a number nobody
measured.

### Discord

`/fomo trending view:whynotpinged` answers section 30 from the record: every watch, its
baseline, its rechecks, and why it did or did not interrupt you.
`/fomo trending view:traders <mint>` is our own top-trader board — not anyone else's UI —
with the independence collapse shown explicitly. Both are views, not commands: Discord
allows 25 children per group and this product is at 25.

`Open in Terminal` is navigation only, built from the exact mint, from a single template in
`constants.py` that every card shares. `TERMINAL_TOKEN_URL_TEMPLATE` overrides it and an
empty value removes the button. Nothing authenticates against it, reads it back, or treats
it as a data source.

### Section 32 and the hotfix, reconciled

The v2.43.1 hotfix forbids actionable language on an unvalidated card. Section 32 of this
release permits a research alert with `SAFETY: UNKNOWN` *if clearly labelled*. Both hold:
the early lane's first card stays `RESEARCH CANDIDATE — VALIDATION PENDING`, and a
**promotion** — which by definition developed serious evidence — may lead with
`🚨 EARLY RUNNER — LOOK NOW`, provided it states the safety it does not know and hands out
no buy control. Identity still outranks evidence: an unverified mint loses the actionable
title regardless of how good the market case is.

### Railway

Nothing is required. Every value has a safe code default and the loop runs on data the bot
already fetches. Optional: `FOMO_EARLY_WATCH_ENABLED` (default `true`),
`FOMO_EARLY_WATCH_SECONDS` (`1800`), `FOMO_EARLY_WATCH_RECHECK_SECONDS` (`30`),
`FOMO_EARLY_WATCH_MAX` (`40`), `FOMO_EARLY_WATCH_MIN_SCORE` (`45`),
`FOMO_EARLY_PROMOTION_MIN_SCORE_GAIN` (`6`), `FOMO_EARLY_PROMOTION_MIN_NEW_BUYS` (`25`),
`FOMO_EARLY_PROMOTION_MIN_NEW_HOLDERS` (`15`),
`FOMO_EARLY_PROMOTION_LARGE_BUY_USD` (`2500`), `FOMO_TOP_TRADERS_ENABLED` (`true`),
`FOMO_TOP_TRADERS_LIMIT` (`10`), `TERMINAL_TOKEN_URL_TEMPLATE`.

### Does any of it help?

Ten evidence cohorts (`lab/forward.py`) are assigned from evidence that existed *at entry*,
so none can be labelled with hindsight: no known trader, one, two-or-more independent,
holder expansion, concentration improving, dev distributing, fresh-wallet cluster, and story
/ thesis / trending each combined with a trader. Forward NET, expectancy, profit factor,
MFE, MAE, drawdown and severe-failure rate are measured per cohort with the same maths the
signal families use. Nothing here claims an edge; the cohorts exist so the claim can be
checked.

## Token identity is chain + exact mint (v2.43.1 hotfix)

A card once reached the operator for `7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump` — a
minutes-old token that shared a ticker with the one they were actually watching. It was
titled `ORGANIC RUNNER — LOOK NOW`, admitted `SAFETY: UNKNOWN` two fields further down, and
offered a one-click buy. Four independent defects had to line up for that, and each one is
now closed by an invariant the deploy self-check enforces.

**A token is its chain plus its exact mint. Name and ticker are display metadata.** They are
never used to resolve, substitute, merge, deduplicate, enrich or choose between tokens.

| Defect | What it did | What happens now |
| --- | --- | --- |
| Symbol resolution | A narrative search broke ties by picking the *youngest* matching pair, which is always the freshest clone | Two or more live tokens answering to one term resolve to **nothing**. A single unambiguous match is still only a lead, carrying `SYMBOL_SEARCH` provenance that no promotion gate accepts |
| Organic classification | `EV_ORGANIC` came from raw flow — 542 buys against 144 sells | Flow bars are necessary but no longer sufficient: the category also needs confirmed *independent* buyers. Unknown independence is recorded as `None`, which is not zero, and the token stays visible under the neutral `EARLY RUNNER` name |
| Card language | An unvalidated card led with `LOOK NOW` | The early lane publishes as `🔬 RESEARCH CANDIDATE — VALIDATION PENDING (<tier>)`. The tier is kept in parentheses because it is real information; it just no longer sets the headline |
| Buy CTA | The buy button rendered unconditionally | `_token_view(..., trade_eligible=...)` defaults to `False`. Research links — Fomo, Pump.fun, DEX, Solscan — always render, and so does *Sell*, because an operator already holding a token needs an exit |

### Provenance travels with every candidate

`smart_money_bot.token_identity` is a pure module — it imports no HTTP client and no signer,
so it structurally cannot resolve a token by asking someone. Every candidate carries
`source`, `source_chain`, `source_mint`, `resolved_chain`, `resolved_mint`,
`resolution_method`, `symbol_collision` and `identity_verified`. When a source supplied an
address, `source_mint` **must** equal `resolved_mint`; `assert_exact_propagation()` checks
that at each hand-off (enrichment, scoring, persistence, render) and raises rather than
letting a swapped mint surface as a wrong card.

Exact enrichment that fails returns `UNRESOLVED_EXACT_MINT`. It never falls back to a symbol
search. **Failure is preferable to substitution.**

### A symbol collision never picks a winner

`detect_symbol_collision()` groups every known mint sharing a normalised ticker and reports
it — on the card, as `⚠ SYMBOL COLLISION`, naming the exact mint the card is about. It has no
`best`, `winner` or `preferred` accessor, because there is no basis on which to choose one.
`Database.known_symbols()` is deliberately keyed by mint rather than symbol: a symbol-keyed
index is how a substituting lookup gets written by accident.

The deploy self-check (`tests/run_selfcheck.py`) refuses to pass if the age-based tie-break
returns, if enrichment stops filtering on the exact address, if an unvalidated card regains
actionable language, if the buy control loses its gate, or if a raw buy count is enough to be
called organic again.

## Terminal-style trenches intelligence (v2.43)

### What was reviewed, and what was actually accessible

| | |
| --- | --- |
| Terminal/Padre documentation | reviewed as a **design reference** for which signal classes matter |
| `docs.padre.gg` direct fetch | **blocked by this environment's network egress proxy** — the documented signal classes were obtained from public secondary sources describing those docs |
| `pump.fun/docs` direct fetch | **blocked by the same proxy**; bonding-curve constants and the account layout came from a public open-source SDK's documentation |
| Terminal data used at runtime | **none.** No feed, no session, no endpoint |
| Terminal ranking reproduced | **none.** Our ranking is our own model over public data |

**No unauthorised access of any kind.** No logged-in session is read, no cookies or auth
tokens are reused, no private or undocumented endpoint is called, no proprietary ranking is
reverse-engineered, and no Terminal or Pump.fun API is invented. A `TERMINAL_AUTHORIZED`
value exists only when an administrator supplies one by hand.

### Every intelligence source is attributed

`PUMP_ONCHAIN` · `PUMPSWAP_ONCHAIN` · `SOLANA_RPC` · `DEXSCREENER_PUBLIC` ·
`AUTHORIZED_SOCIAL` · `J7_AUTHORIZED` · `FOMO_AUTHORIZED` · `TERMINAL_AUTHORIZED` ·
`PUBLIC_WEB` · `DERIVED_PUBLIC_MODEL`

The first three are on-chain facts rather than a vendor's opinion, which is why the engine is
built on them. Consensus is counted over **evidence families**, not feeds: three market
vendors relaying the same chain are one observation with three invoices.

### Pump.fun lifecycle, from the program's own accounts

`NEW` → `EARLY_CURVE` → `MID_CURVE` → `ALMOST_BONDED` → `GRADUATING` → `RECENTLY_BONDED` →
`PUMPSWAP` → `MATURE`, plus an honest `UNKNOWN`.

Bonding progress is `(sold / available)` read from the curve account's `real_token_reserves`,
and graduation from its `complete` flag. **Age never infers graduation** — a six-hour-old
token can sit at 4% and a four-minute-old one can be at 96%. An unreadable curve reports
`UNKNOWN`, never 0%. Crossing 25/50/75/90/95% is an *event* that recomputes the candidate
immediately rather than at the next tick.

### Multi-timeframe momentum

Five windows computed independently from one observation stream, each using only samples
inside its own span. Under two samples, a window reports `None` — a fabricated 1-minute
reading is worse than an absent one.

The shape across them is the signal:

| shape | meaning |
| --- | --- |
| `VERY_EARLY_ACCELERATION` | the short window is running several times faster than the long one — the move *just started* |
| `SUSTAINED_TREND` | short, medium and long all strong together |
| `BUILDING` | shorter windows improving together |
| `COOLING` | the short window has turned while longer ones hold |
| `FADING` | short and medium falling after a strong longer window |

And a second-derivative question a level cannot answer: is acceleration itself `INCREASING`,
`STEADY`, `COOLING` or `REVERSING`? "Price is currently green" is not that.

`$50K MC on $1K liquidity` and `$50K MC on $15K liquidity` are scored as the different things
they are, via liquidity/MC, volume/liquidity and estimated impact.

### Participants, not transactions

| | |
| --- | --- |
| Wallet age | `VERY_NEW` / `RECENTLY_FUNDED` / `ESTABLISHED` / `UNKNOWN`, from first observable signature |
| Clustering | shared funder, funding burst in one window, or same-slot buys |
| Effect | every wallet in a cluster collapses to **one** independent actor |

A fresh wallet is **not inherently bullish** — it can be a new trader, a bot, an insider or a
sybil, and only coordination tells them apart. Large buys are sized against the pool they
landed in, and count as demand only once independent buyers follow.

### Dev, holders and bundles

* **Dev funding** — source type, amount and timing where publicly observable. Funded three
  minutes before launch is *context*, explicitly not proof of anything.
* **Dev holding** — `STABLE` / `REDUCED` / `SELLING` / `DISTRIBUTED`.
* **Dev history** — neutral labels only. A creator whose prior tokens collapsed gets
  `DEV_HISTORY_HIGH_FAILURE_RATE`; this codebase never calls a person a scammer and never
  asserts identity beyond an observed funding edge.
* **Holders** — top 10 / top 20 / largest, with infrastructure excluded and measured against
  circulating rather than total supply. Reported as a **trend**: `43% → 37% → 31%` and
  `18% → 35%` mean opposite things.
* **Bundles** — same-slot, same-direction groups, and only inside the launch window. On a busy
  mature pool, same-slot co-trading is block production, not coordination. A bundle that is
  *distributing* escalates to `HIGH` on behaviour regardless of its size.
* **Bot transactions** — trading-app routing share, recorded as attention context. Bot
  activity is **not** smart money.
* **Metadata reuse** — image, website, description and socials fingerprinted (hashes only, no
  third-party text retained) so copying across mints is detectable. Copying is evidence, not
  proof of malice.

### Two scores, for two different questions

**`PUMP_TRENCH_SCORE`** asks *is the early participation in this token real?* — so its heaviest
weights are independent demand, holder distribution, dev behaviour, bundle exposure and
fresh-wallet quality. A token up 300% on the curve can score badly, because 300% bought by
nine wallets from one funder is one person's spending.

**`PUBLIC_TRENDING_MODEL`** asks *which Solana tokens are experiencing the strongest meaningful
attention right now?* — from multi-timeframe momentum, independent participants, holder
expansion and liquidity depth. Paid DEX placement is worth at most 3 of ~100 points and is
structurally incapable of lifting a token nothing is happening to.

Both use continuous ramps rather than boolean gates, both are bounded and fully printable, and
a hard safety failure zeroes either one and clears every reason.

### Alert tiers and cadence

`TRENCH_HEADS_UP` · `TRENCH_RUNNER` · `TRENDING_WATCH` · `TRENDING_ALPHA` ·
`CONTINUATION_WATCH` · `HIGH_CONFLUENCE` — the two "watch" tiers publish to radar and never
interrupt anyone.

Rechecks run in bounded tiers (`HOT` 15s / `WARM` 45s / `NORMAL` 120s) rather than one
interval for everything, capped in *population* as well as speed so cost stays flat, and
reading cached state so a faster cadence costs CPU rather than provider calls. Meaningful
events — a large independent buy, a buyer burst, a notable wallet, a story match, a new
thesis, holder acceleration, a rank jump, a bonding milestone, graduation — recompute a
candidate immediately instead of waiting for the timer.

**Hard gates that no score can override:** coordinated demand, liquidity too thin to exit,
heavy launch bundling, or a creator distributing. A high score built on those inputs is a
measurement of the wrong thing.

### Time to first observation

A public `logsSubscribe` on the Pump.fun program detects creation in the same second it
lands. First observation is persisted **before any enrichment**, because that stamp is what
every latency metric is measured against. Latency is recorded per source, so the stream and
the poll are separately attributable — visible in `/fomo trending view:latency`.

### The shadow decision

The trenches lane rides the **existing Trending book** and is separated by *family attribution*
rather than a third bankroll. A third `$100` account would take three times as long to reach a
meaningful sample, and "did the pre-graduation lane pay?" is answerable from
`PUMP_TRENCH_RUNNER`, `PUMP_ALMOST_BONDED` and `PUBLIC_TRENDING_MODEL` attribution inside one
book. `FOMO_TRENCH_SHADOW_SEPARATE_BANKROLL=true` splits it later; the families are already
distinct either way. **Both existing experiments are untouched** — same version strings, same
bankrolls, same forward history.

### Real money

Unchanged and non-negotiable: no signer, no private key, no swap, no transaction submission,
no SOL spending. The entire `smart_money_bot.trenches` package holds no network client, no
database handle and no wallet — asserted by the test suite and by the deploy self-check.

## Trending-first alpha engine (v2.42)

### What the bot can legitimately see

| | |
| --- | --- |
| Authorised Fomo Trending feed | **not available by default** — no documented public API is reachable from this deployment |
| Default source | `TRENDING_PROXY` — DEX Screener `token-boosts/top`, `token-boosts/latest`, `token-profiles/latest` for the ordering, and the documented batch endpoint `tokens/v1/solana/{addresses}` (30 mints per request) for market data |
| Authorised path | set `FOMO_TRENDING_API_URL` (optionally `FOMO_TRENDING_API_KEY`) and the adapter labels its rows `FOMO_TRENDING` |
| Nothing configured | `NO_SOURCE_CONFIGURED` — the lane says so instead of showing an empty board |

The proxy is an approximation of *attention*, not Fomo's ranking. Its rank is a position in our
own ordering, and that caveat travels with every card:

> Rank is a PROXY ordering from public attention data, not Fomo's Trending rank.

**No unauthorised access of any kind.** No cookies, no reused sessions, no replayed credentials,
no undocumented or authenticated endpoints, no reverse-engineered private APIs, no rate-limit
circumvention, and no invented feed.

### Percentage windows are never guessed

If a source displays `+325%` and does not document what window it covers, the bot persists
`CHANGE_WINDOW_UNKNOWN` and prints `+325.0% (window unknown)`. It does not silently call it 24h.

### The Trending ledger

One row per **exact mint**. `first_seen_at`, `first_rank`, `first_market_cap_usd`,
`first_holder_count` and `first_top10_percent` are written once by an `INSERT OR IGNORE` and never
appear in an `UPDATE SET` clause, so no code path — including a buggy one — can move them. The
read path re-derives those fields from those protected columns rather than from the JSON payload,
so a corrupted write cannot round-trip its corruption back out. That immutability is what makes
"was the alert early?" answerable rather than reconstructable.

Re-entry is decided by the board diff (`on_board`), never by the gap between two observations: a
slow poll, a restart or a busy loop all produce large gaps while the token never left the board.
Time on the board is credited only from observations actually made, capped per gap, so a three-hour
outage is never credited as three hours of observation.

### Trending event states

`TRENDING_NEW_ENTRY` · `TRENDING_RANK_RISING` · `TRENDING_ACCELERATING` · `TRENDING_HEALTHY` ·
`TRENDING_CONTINUATION` · `TRENDING_REENTRY` · `TRENDING_COOLING` · `TRENDING_FADING` ·
`TRENDING_EXITED` · `TRENDING_EDGE_CONSUMED`

Every one is derived from **movement** — rank velocity, market-cap acceleration, holder growth,
time on the board — never from absolute rank. `#44 → #31 → #18 → #9` in minutes is a signal;
`#2` flat for six hours is a position. `TRENDING_CONTINUATION` requires genuinely **new** evidence
(a fresh supported thesis, a new catalyst, renewed accumulation, holder acceleration); "it pumped,
therefore buy" cannot reach it, and a continuation card states plainly that it is **not early**.

### The Trending edge score

The legacy opportunity score was built for a different question and is kept only as supporting
context. The primary lens is a bounded 0–100 **Trending edge score** with a printable derivation:
rank velocity, new-entry status, market acceleration, holder growth, liquidity, thesis quality,
story, public social, and smart money, minus concentration and edge-consumed penalties.

Every component is a continuous ramp, which is the fix for the threshold cliff. A hard safety
failure zeroes the score outright and clears every reason — attention never outvotes a confirmed
sell failure.

**A score is never a reason.** An urgent alert must carry a named serious category:
`TRENDING_ACCELERATION`, `TRENDING_NEW_ENTRY`, `STORY`, `THESIS`, `AI_PROJECT`, `SMART_MONEY`,
`PUBLIC_SOCIAL`, `HOLDER_EXPANSION`, `CONFLUENCE`, `EXCEPTIONAL_MARKET_STRUCTURE` or
`TRENDING_CONTINUATION`. A candidate that clears the threshold with no named reason is suppressed,
as is one whose only reason is chatter without market confirmation.

### HOT WATCH

| | |
| --- | --- |
| Entry | a strong near miss within 12 points of the alpha threshold, or a new entrant with strong evidence |
| Recheck cadence | **45 s** (versus the legacy 1800 s recheck) |
| Window | 900 s, then it expires |
| Population cap | 12 concurrent, so cost stays bounded |
| On entry | a quiet radar card. **No ping.** |
| On promotion | exactly **one** escalation ping, and only with a named reason |
| On fade | silent expiry |

Promotion timing is persisted and reported: first-seen MC, Trending-entry MC, heads-up MC, hot
watch time, promotion MC and urgent-ping MC. `/fomo trending view:hotwatch` shows the heads-up →
promotion p50, the promotion miss rate, and how many expired without a ping.

### Claims, theses and stories are kept apart from facts

* **About** is summarised, never dumped, and rendered as *the project's own claim*.
* A named project is only `SUPPORTED` when an official source publishes **this exact mint**. A real
  project with a real website that has never heard of the token is `UNVERIFIED` — that is the most
  common trap in the category.
* Theses are graded on **specificity, timing, exact-mint provenance, independence and the author's
  forward record** — never on how confident they sound or how many likes they have. Generic moon
  posts, copy-paste, developer self-promotion, circular sourcing, claims lifted from another mint,
  unsupported "insider" assertions and post-move hindsight are all penalised by name.
* Near-identical theses are clustered: three copies of one post are **one** information source;
  three analysts reaching the same conclusion separately are three.
* Public commentary is called **PUBLIC EARLY CHATTER**, never "insider info". Nothing reads private
  messages, leaks or non-public sources.
* Engagement counts are recorded only when a source actually supplies them. A missing count is
  `None`, never a confident-looking `0`.

### Holders

Genuine participant growth is distinguished from repeat transactions: a thousand transactions from
ten wallets is not five hundred new independent holders. Concentration is tracked as a **trend**,
because "top 10 hold 40%" means nothing without knowing whether it was 25% or 60% ten minutes ago.

### Mint is identity

A name, a ticker, a story, an About blurb and an image are all attributes several unrelated tokens
routinely share. Evidence never crosses mints — a same-name token's thesis, story or wallet event
is not this token's evidence — and a ledger entry raises rather than merging a different mint.
Every Fomo link is derived from the mint itself and verified to resolve to it, so a card can never
show one token and link to another. Collisions are surfaced, not hidden.

### The two forward experiments

|  | LEGACY | TRENDING |
| --- | --- | --- |
| Strategy version | `shadow-v1` | `trending-shadow-v1` |
| Bankroll | $100 | $100 |
| Per position | $10 | $10 |
| Max open | 5 | 5 |
| Max exposure | $50 | $50 |

Identical shape so the **strategy is the only variable**. Isolation is structural: the store keys
bankrolls by `strategy_version` and open positions by `(mint, family, strategy_version)`, so the
same mint can be open in both books simultaneously and neither can see, spend or block the other.
The legacy experiment's config, version and entire forward history are untouched.

Trending Radar shows everything relevant; the Trending experiment only simulates configured
strategy signals. Chatter or holder growth alone is deliberately not tradeable.

`/fomo profit view:universes` reports current bankroll, NET, ROI, trades, win rate, profit factor,
expectancy, drawdown, MFE, MAE, severe failures and rug / liquidity-collapse rates for both — and
reports **safety** and **upside** separately, because "Trending rugs less" and "Trending runs
further" are different questions that can come out in opposite directions. It refuses to name a
winner until both books have at least 10 resolved trades.

### Trending-aware exits are challengers, not replacements

The hypothesis that Trending tokens deserve more patience is **tested**, not assumed. Twelve
counterfactual policies — current champion, Trending persistence, rank trailing, thesis/story/
holder continuation, adaptive trail, principal-recovery runner, and fixed 5m/15m/30m/1h holds —
replay the observation stream the engine already records, so they cost zero extra provider
requests. Every one of them, including the most patient, exits immediately on a confirmed sell
failure, liquidity collapse or hard malicious evidence. A `SOFT_PAUSE` on a token still on the
board with growing holders and healthy liquidity is not a reversal.

### Real money

Unchanged and non-negotiable: no signer, no private key, no swap, no SOL spending, no live
execution. The entire `smart_money_bot.trending` package holds no network client, no database
handle and no wallet — asserted by the test suite and by the deploy self-check.

## Ultra-early discovery and story-first alpha (v2.41)

### The Grok Pocket postmortem

| | value |
| --- | --- |
| Market cap when the bot first saw it | ~$31.18K |
| Market cap when the operator got useful visibility | ~$61.49K |
| Move already spent before the human could act | ~+97% |
| Cause | first visibility was gated behind deep enrichment, not behind a threshold |

Deep enrichment ran with a 30-second per-mint budget inside one `asyncio.gather` over the whole
batch, on a 60-second poll with a 1,800-second recheck. Nothing about the evidence was wrong; the
operator simply got it after the trade was over.

### The cheap lane runs first, by construction

`lab/early.py` decides operator visibility from **one DEX snapshot and two timestamps**. It
imports no HTTP client, no RPC client, no wallet forensics and no social lookup — a test walks its
syntax tree to prove it — so it cannot be slowed down by a provider even in principle. Safety is
reported honestly as `UNKNOWN`; it is never implied to be `PASS`, and the cheap lane can never make
anything entry eligible.

In the radar loop the early lane is awaited **before** the enrichment gather, and a test asserts
that ordering by source position, because the ordering *is* the fix.

### Three visibility tiers

| Tier | Means | Lane |
| --- | --- | --- |
| `EARLY_HEADS_UP` | "This is beginning to move. Watch." | radar only, never pings by default |
| `EARLY_RUNNER` | Serious early evidence from more than one family — look now. | urgent, may ping |
| `ORGANIC_RUNNER` | An early runner whose whole case is market structure. No story is not a defect. | urgent, may ping |

A tier that pings must name a **serious evidence category**: organic market evidence, exceptional
market structure, a story, a proven early wallet, a catalyst, or multi-source confluence. A high
score on its own is recorded as `NO_SERIOUS_EVIDENCE_CATEGORY` and demoted to a heads-up.
Confluence means *independent* families agreeing — organic flow and a structural impulse are both
market evidence and count once between them, so a single market observation cannot manufacture its
own corroboration.

### Large buys are measured relatively, and whose money it is matters

There is no dollar threshold. A buy is an impulse when it is worth **5%+ of liquidity**, or **8x**
the recent average trade, or moves the market cap **8%+** in the window. It only counts as
*demand* once **8+ independent buyers** follow it. A large buy whose only follower is the creator
is graded `LARGE_BUY_CREATOR_LINKED`, **subtracts** from the score, and blocks the surface with
`LARGE_BUY_WAS_CREATOR_LINKED` — pretending a self-buy is demand is the easiest possible way to
fill a channel with traps.

### A late alert says so

| Move since first seen | State |
| --- | --- |
| < 35% | `EDGE_AVAILABLE` |
| 35–80% | `EDGE_NARROWING` |
| 80–150% | `EDGE_CONSUMED` |
| > 150% | `MOVE_ALREADY_EXTENDED` |

An `EDGE_CONSUMED` card is titled **"⚠ RUNNER — EDGE CONSUMED"**, publishes to the radar lane,
**never pings**, and reports the move that happened *before* the alert. The Grok Pocket case now
renders as "+97.21% move before alert" rather than "first seen $31.18K".

### The timeline is write-once

Ten stages are persisted with a timestamp **and the market cap at that moment**:
`SOURCE_CREATED`, `BOT_FIRST_SEEN`, `CHEAP_SIGNAL_TRIGGER`, `OPERATOR_HEADS_UP_SENT`,
`EARLY_RUNNER_TRIGGER`, `URGENT_PING_SENT`, `DEEP_ENRICHMENT_COMPLETE`, `QUALIFIED_RESEARCH`,
`SHADOW_DECISION`, `SHADOW_FILL`. The primary key is `(mint, stage)` and writes are
`INSERT OR IGNORE`, so a re-observation at $61K cannot rewrite what the bot knew at $31K. Every
suppression is persisted too, with a structured reason code, so **"why wasn't I pinged for this"**
is a query rather than a guess.

### MINT IS IDENTITY

Narratives are first-class entities that exist independently of any token, and every token-to-story
link is **graded and directional**:

* `UNRELATED` → `NAME_ONLY` → `WEAK` → `PLAUSIBLE` → `STRONG` → `DIRECTLY_LINKED` → `OFFICIAL`
* only `STRONG` and above may display the story as the token's own,
* and a link the **token** claims about itself is capped at `PLAUSIBLE` with a
  `METADATA_ONLY_EVIDENCE` warning, because metadata can be copied.

`OFFICIAL` requires the story side naming the exact mint **and** a named authority; asking for
`OFFICIAL` from token-side evidence raises rather than returning a softer answer. Same name is
never the same token: lookalikes are grouped into a collision group with a ranked resolution and
the reasons for it, and a token that predates the story is flagged, not condemned.

### A pause is not a reversal

The shared exit engine used to cut 50% the first time momentum printed weak, which on fresh
volatile tokens sold live runners into noise. Momentum is now classified as `HEALTHY`,
`SOFT_PAUSE`, `CONFIRMED_DECAY` or `HARD_REVERSAL`. A softenable exit (`MOMENTUM_DECAY`,
`BUY_FLOW_REVERSAL`, `VOLUME_EXHAUSTION`) with an inconclusive verdict returns
`SHADOW_SOFT_PAUSE_HOLD` and sells nothing. Confirmation needs **two** weak observations and
**two** independent negatives — but facts about the market, not noisy scores, still act on one
observation: heavy selling (a 2:1 sell imbalance) and smart-money distribution are conclusive
alone, and a confirmed hard safety failure is untouched and still exits immediately and in full.

### Honest status instead of a green tick

`/fomo realtime` reports the X/social lane as `DISABLED_BY_CONFIG` when it is switched off rather
than as a generic healthy, and reports the wallet stream's reconnects and last-message age. The bot
never invents engagement counts: engagement is recorded only where a source genuinely exposes it.

## Profit-first forward optimization (v2.40)

The objective of this release is one number: **forward NET expectancy**. Not more alerts, not more
coins, not more API calls.

### Cost: a failing provider is called less, not more

`SolanaTrackerClient` now carries the same breaker its risk-lookup sibling already had. A credit
status (401/402/403/429) opens an exponential backoff window — 60s, 5m, 15m, 30m, capped at an
hour — and every call inside that window is refused locally and **counted**, so the saving is
visible on the dashboard. A 404 is deliberately *not* a credit failure: "this token is unknown"
must not starve the pipeline.

The caller backs off too. `refresh_discovery` now throttles on the last *attempt* rather than the
last success, and persists that timestamp so a crash loop cannot re-open the budget on every boot.

| | before | after |
| --- | --- | --- |
| Refresh attempts while the plan is exhausted | every 60s, forever | first attempt, then exponential backoff |
| Failing requests per day (observed) | ~1,440 | a handful |
| Where the spend shows up | only in the error log | `/fomo profit providers` |

### Solana Tracker is optional enrichment

The provider map is written down in code and surfaced in `/fomo profit providers`, including
whether each feature is essential and whether a cheaper on-chain path exists. Both Tracker
features — wallet discovery and token-risk enrichment — are marked **optional with an on-chain
fallback**. Only DEX market data and Solana RPC are marked essential, because those genuinely have
no substitute. When Tracker is unavailable the evidence reads `UNKNOWN`, never `PASS`, and never
`FAIL`.

### A dead provider is not a dead token

This is the false-exit fix, and it was live. A partial safety de-risk is downgraded to *monitoring*
only when **all** of these hold: the verdict is `UNKNOWN` rather than `FAIL`, the reason is a named
provider outage, a sell route still exists, liquidity has not collapsed, and buy flow is not
reversing. Anything less and the original defensive plan stands. A confirmed hard failure is never
rescued. STRICT PAPER entry safety is untouched — the rescue lives in the shadow overlay and a test
asserts the strict entry module has never heard of it.

### Forward results decide which families are worth trading

`calibrate_families` turns closed forward trades into a bounded multiplier per signal family:

* below **10 closed trades** a family's weight is exactly `1.0` and says `INSUFFICIENT_SAMPLE`,
* above it, the measured expectancy is shrunk toward the pooled mean by `n / (n + 20)`,
* the result is clamped to `0.5 … 1.5`, whatever the data says,
* and retiring a family from SHADOW needs a much bigger claim: **25+ closed trades**, negative
  shrunk expectancy *and* a 40%+ severe-failure rate.

Every weight records its sample, raw expectancy, shrunk expectancy, pooled mean, shrinkage factor,
severe-failure rate, the `as_of` it was computed at, and a calibration version — so any ranking
decision can be re-derived by hand. Weights only ever move ranking, publication priority and
shadow eligibility; a test asserts they never touch a safety gate, a liquidity floor or a cost
model.

### Current edge outranks historical opportunity

`forward_edge_score` ranks what is worth surfacing *now*: current actionability (30%), freshness,
expected NET edge, independent buyers, route quality, catalyst confidence, notable-wallet lead and
flow — multiplied by the family's forward weight. The historical opportunity score contributes at
most **10 of 100 points**, so a spent setup cannot keep sitting beside a fresh one on past glory.

### Pings have to earn themselves

The forward ping gate is strictly subtractive: it can only withhold a ping the existing rules
already allowed, never create one. A ping now needs current edge ≥ 70, at least two independent
confirmations, and a family the forward data has not demoted — and is refused outright if the move
has already happened. Volume, a famous wallet or one viral post are explicitly not enough. Nothing
is hidden as a result: everything still publishes to the live radar.

### Exits are measured against what actually happened next

`/fomo profit exits` scores every persisted exit against the observation stream that followed it
within an hour, and classifies each as **premature** (the token ran 25%+ past the exit),
**good defensive** (it fell 15%+ further) or neutral. Per exit reason it reports count, average NET,
premature rate, defensive rate, upside given up, loss avoided and **net regret** — with the rule
leaking the most money sorted first. That is the number that says which exit rule to fix.

This lookahead is evaluation only and structurally confined: `score_exit` is a pure function taking
observations explicitly, and a test asserts no module in the live exit path imports it.

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

### v2.43.0 Railway changes

Every new setting has a safe code default, so **no Railway variable has to be added**. Nothing
here enables live trading or real-money execution, no forward history is reset, and both
existing shadow experiments keep their own bankrolls and records. The whole engine runs on
public Solana RPC and Pump.fun program state — **no paid provider is required**.

**ADD:** none required.  **CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_TRENCHES_ENABLED=true                    # the Pump.fun trenches universe
FOMO_PUMP_CREATION_STREAM_ENABLED=true        # realtime creation detection (public program logs)
FOMO_TRENCHES_POLL_SECONDS=30                 # the safety net behind the stream
FOMO_TRENCHES_MAX_TRACKED=80                  # candidates evaluated per pass
FOMO_TRENCHES_RUNNER_MIN_SCORE=62             # the bar a trench ping must clear
FOMO_TRENCHES_HEADS_UP_MIN_SCORE=38           # the quiet radar tier
FOMO_TRENCHES_MAX_ALERTS_PER_HOUR=8           # hourly ceiling on trench interruptions
FOMO_TRENCHES_COOLDOWN_SECONDS=1800           # per-mint cooldown between trench pings
FOMO_TRENCHES_HOT_RECHECK_SECONDS=15          # cadence tiers: hot / warm / normal
FOMO_TRENCHES_WARM_RECHECK_SECONDS=45
FOMO_TRENCHES_NORMAL_RECHECK_SECONDS=120
FOMO_TRENCHES_MAX_HOT=6                       # population caps, so cost stays flat
FOMO_TRENCHES_MAX_WARM=16
FOMO_TRENCHES_MAX_ENRICHMENT_PER_SCAN=12      # who gets the expensive reads each pass
FOMO_TRENCHES_WALLET_LOOKUPS_PER_TOKEN=25     # fresh-wallet history budget per token
FOMO_TRENCHES_HOLDER_READS_PER_SCAN=10
FOMO_PUBLIC_TRENDING_ENABLED=true             # our own ranking over public data
FOMO_PUBLIC_TRENDING_MIN_SCORE=10             # floor below which nothing is ranked
FOMO_TRENCH_SHADOW_SEPARATE_BANKROLL=false    # true splits trenches into its own $100 book
```

**Worth knowing without changing anything:**

* **Provider cost is on-chain, not vendor.** A pass costs one batched `getMultipleAccounts`
  per 100 curve reads, plus holder and wallet reads capped by
  `FOMO_TRENCHES_HOLDER_READS_PER_SCAN` and `FOMO_TRENCHES_WALLET_LOOKUPS_PER_TOKEN`. Curve
  state is cached for 10s, holders for 45s and wallet history for 30 minutes — a wallet's
  first activity is immutable once observed, so re-reading it is pure waste. `SOLANA_TRACKER_API_KEY`
  remains entirely optional and is not used by this lane.
* **The stream is the speed; the poll is the safety net.** With
  `FOMO_PUMP_CREATION_STREAM_ENABLED=false` the lane still works, but first observation
  degrades from sub-second to up to `FOMO_TRENCHES_POLL_SECONDS`. Check which is carrying the
  load in `/fomo trending view:latency`.
* **Cadence tiers must stay ordered.** Configuration validation rejects
  `hot > warm > normal`, and rejects a `MAX_WARM` smaller than `MAX_HOT`.
* **Turning the trenches lane off does not restore v2.42 behaviour by itself.**
  `FOMO_TRENCHES_ENABLED=false` stops the Pump lane; Trending and the graduated lane keep
  running on their own flags.
* **A separate trench bankroll is opt-in and one-way in practice.** Splitting mid-experiment
  starts a fresh $100 book with no history, so the comparison restarts. Attribution inside the
  Trending book is the default for exactly that reason.

### v2.42.0 Railway changes

Every new setting has a safe code default, so **no Railway variable has to be added**. Nothing here
enables live trading or real-money execution, no shadow forward history is reset, and the legacy
$100 / $10 / 5 / $50 experiment keeps its own bankroll and its whole record.

**ADD:** none required.  **CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_TRENDING_PRIMARY_ENABLED=true          # Trending as the primary discovery universe
FOMO_GRADUATED_SECONDARY_ENABLED=true       # keep graduated discovery as the secondary lane
FOMO_TRENDING_PROXY_ENABLED=true            # allow the public TRENDING_PROXY approximation
FOMO_TRENDING_POLL_SECONDS=45               # source-safe cadence for the Trending loop
FOMO_TRENDING_MAX_TRACKED=60                # board rows tracked per poll
FOMO_TRENDING_ALPHA_MIN_SCORE=62            # the bar an urgent Trending alert must clear
FOMO_TRENDING_WATCH_MIN_SCORE=40            # the quiet "strengthening" tier
FOMO_TRENDING_MAX_ALERTS_PER_HOUR=10        # hourly ceiling on Trending interruptions
FOMO_TRENDING_COOLDOWN_SECONDS=1800         # per-mint cooldown between Trending pings
FOMO_TRENDING_HOT_WATCH_ENABLED=true        # the fast near-miss reevaluation lane
FOMO_TRENDING_HOT_WATCH_SECONDS=900         # how long a hot watch may live
FOMO_TRENDING_HOT_WATCH_RECHECK_SECONDS=45  # how often it is reconsidered
FOMO_TRENDING_HOT_WATCH_MAX=12              # concurrent hot watches, so cost stays bounded
FOMO_TRENDING_HOT_WATCH_BAND=12             # points below the alpha bar that count as a near miss
FOMO_TRENDING_SOCIAL_ENRICH_ENABLED=true    # attach public social evidence to Trending candidates
FOMO_TRENDING_SHADOW_ENABLED=true           # the second, isolated $100 forward experiment
FOMO_TRENDING_STALE_SNAPSHOT_SECONDS=600    # after this the lane reports STALE, not ACTIVE
```

**Only if an administrator has an authorised Fomo Trending feed:**

```text
FOMO_TRENDING_API_URL=https://<authorised-feed>   # ONLY this promotes provenance to FOMO_TRENDING
FOMO_TRENDING_API_KEY=<key>                       # optional, sent as x-api-key
FOMO_TRENDING_CHANGE_WINDOW=24H                   # only if the feed documents its window
```

Leave `FOMO_TRENDING_API_URL` unset and the bot runs on `TRENDING_PROXY` and says so everywhere.
Leave `FOMO_TRENDING_CHANGE_WINDOW` unset and displayed percentages are recorded as
`CHANGE_WINDOW_UNKNOWN` rather than guessed.

**Worth knowing without changing anything:**

* **Provider cost is roughly flat.** The Trending loop costs 3 small list requests plus
  `ceil(tracked / 30)` batch requests per poll — about 5 requests every 45 seconds — and enrichment
  reuses the DEX snapshot cache the radar already fills. Hot-watch rechecks read *cached* state and
  add no board fetches. `FOMO_TRENDING_POLL_SECONDS` is bounded to 15–3600 s; do not set it below
  what the source tolerates.
* **The hot-watch cadence must stay well under its window.** Configuration validation rejects a
  recheck interval that is not shorter than `FOMO_TRENDING_HOT_WATCH_SECONDS`, because a hot watch
  that reevaluates as slowly as the legacy radar is precisely the bug it exists to fix.
* **Turning the Trending lane off does not restore v2.41 behaviour by itself.** Set
  `FOMO_TRENDING_PRIMARY_ENABLED=false` to stop the Trending loop; the graduated lane keeps running
  because `FOMO_GRADUATED_SECONDARY_ENABLED` defaults to true.
* **`FOMO_TRENDING_SHADOW_ENABLED=false` pauses the new experiment only.** The legacy shadow book is
  a different `strategy_version` and is unaffected either way.

### v2.41.0 Railway changes

Every new setting has a safe code default, so **no Railway variable has to be added**. Nothing here
enables live trading or real-money execution, and no shadow forward history is reset.

**ADD:** none required.  **CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_EARLY_LANE_ENABLED=true          # the cheap operator lane ahead of deep enrichment
FOMO_EARLY_HEADS_UP_PING=false        # keep heads-ups on the radar lane (recommended)
FOMO_EARLY_MIN_LIQUIDITY_USD=4000     # floor a $10 simulated trade actually needs
FOMO_EARLY_MAX_AGE_SECONDS=3600       # how fresh "early" means
FOMO_EARLY_RUNNER_MIN_SCORE=55        # the bar an EARLY_RUNNER must clear
FOMO_EARLY_MAX_RUNNERS_PER_HOUR=12    # hourly ceiling on the pinging tier
FOMO_EARLY_COOLDOWN_SECONDS=1800      # per-mint cooldown between early cards
```

**Worth knowing without changing anything:** turning `FOMO_EARLY_HEADS_UP_PING` on will
substantially increase interruptions — a heads-up is deliberately the tier that has *not* earned
one. The early lane adds **no** provider calls: it reads the DEX snapshot the radar already
fetches.

### v2.40.0 Railway changes

Every new setting has a safe code default, so **no Railway variable has to be added**. Nothing here
enables live trading or real-money execution.

**ADD:** none required.  **CHANGE:** none required.

**OPTIONAL:**

```text
FOMO_FORWARD_PING_GATE_ENABLED=true       # pings must clear the forward-edge bar
FOMO_RUNNER_ANALYSIS_BUDGET_SECONDS=30    # per-mint enrichment budget; timeouts are now named
```

**Worth knowing without changing anything:** if `SOLANA_TRACKER_API_KEY` is out of credits, the bot
now backs off instead of retrying every minute, keeps running on DEX data and public RPC, and
reports the outage as `UNKNOWN` evidence rather than failing tokens. `/fomo profit providers` shows
the health, the wasted calls and the calls the breaker skipped.

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

### Terminal-style trenches (v2.43, admin only)

`/fomo` sits at **24 of Discord's 25** child commands, so v2.43 adds its surfaces as *views*
on the existing `/fomo trending` rather than claiming the last slot.

| View | What it answers |
| --- | --- |
| `/fomo trending view:trenches` | **What is happening on Pump.fun right now?** All four sections at once — NEW, ALMOST BONDED, RECENTLY BONDED and HOT — with bonding progress, stage, market cap versus first-seen, holders and top-10 for each. |
| `/fomo trending view:new` | Freshly created coins, newest first. |
| `/fomo trending view:almostbonded` | Approaching graduation — where the trading route is about to change. |
| `/fomo trending view:recentlybonded` | Just migrated to PumpSwap, kept for continuation. |
| `/fomo trending view:hot` | Whatever the engine is rechecking fastest right now, which is the answer to "what are you actually watching?" |
| `/fomo trending view:public` | **Our own public ranking**, with its caveat attached: not Terminal's proprietary rank and not Fomo's. Shows rank, model score, multi-timeframe shape and whether acceleration is itself increasing. |
| `/fomo trending view:trenchtoken mint:<exact mint>` | One Pump token in full: lifecycle and bonding, first-seen versus current market cap, the top-10 concentration *path*, dev posture and holding, bundle risk and whether those wallets are distributing, buyer independence and clustering, metadata reuse, the creator's neutral record, and which lanes discovered it. A name or ticker is refused. |
| `/fomo trending view:latency` | **Launch → observation**, per discovery source, with p50/p90/best. The remaining gap after v2.41 fixed observation→alert. |

`/fomo realtime` gains the trenches block: creation-stream state, whether it is actually
subscribed, creations seen, reconnects, tracked candidates, alerts published and suppressed,
and the on-chain read counters with cache hits — so an expensive lane cannot hide.

`/fomo latency` is unchanged and still measures the *lab* pipeline (observation → alert); the
new `view:latency` measures discovery (launch → observation). They are different stages with
different fixes.

### Trending-first alpha (v2.42, admin only)

| Command | What it answers |
| --- | --- |
| `/fomo trending` | **What is trending right now, and is any of it still tradeable?** The board by rank, each token's exact mint, its market cap when it entered Trending versus now, best rank, time on the board, holder growth, and the active hot-watch count. Always states which source produced the rank. |
| `/fomo trending view:token mint:<exact mint>` | One token in full: rank and best rank, entry rank, stints, first Trending MC → now, peak, liquidity, the displayed change *with its window* (or `window unknown`), holder count and concentration trend, the About section as a **claim** beside its external verification as a **fact**, graded theses with author and timing, and the Fomo verification badge labelled as a badge. A name or ticker is refused — only the exact mint is accepted. |
| `/fomo trending view:hotwatch` | Is the fast lane actually promoting in time? Active, promoted, expired and dropped counts, the heads-up → promotion p50, the promotion miss rate, and recent entries with their entry score, best score, recheck count and how much the market cap moved between heads-up and promotion. |
| `/fomo trending view:why` | **Why wasn't I pinged?** Every structured Trending suppression reason with its count — `HOT_WATCH`, `EDGE_CONSUMED`, `SOCIAL_ONLY`, `NOT_STRONG_ENOUGH`, `HARD_SAFETY_FAILURE`, `NO_NAMED_SERIOUS_REASON`, `RATE_LIMIT`, `COOLDOWN` and the rest. |
| `/fomo profit view:universes` | **$100 LEGACY versus $100 TRENDING — which one actually made more money, and which one actually got rugged less?** Both bankrolls side by side with NET, ROI, win rate, profit factor, expectancy, drawdown, severe failures, rug and liquidity-collapse rates, and +25/+50/+100/+200 hit rates. Safety and upside leaders are reported separately, and no winner is named until both books have a real sample. |

`/fomo trending` is **one** child command with a `view` parameter rather than four separate ones:
Discord allows 25 subcommands per group and `/fomo` was already at 23, so views are the only shape
that leaves room to grow. The group now sits at **24 of 25**.

`/fomo realtime` carries the Trending block: the source kind and lane state, last snapshot age,
tracked rows, new entries, rank movers, active hot watches, promotions, alerts published and
suppressed — plus the wallet lane's **named** state, its reconnect count, how long it has been down,
and whether the polling fallback is carrying the load.

### Ultra-early alpha (v2.41, admin only)

| Command | What it answers |
| --- | --- |
| `/fomo runners` | **What is moving right now that I should look at?** Every live early surface with its tier, edge state, score, first-seen market cap, current market cap, the move already spent, and the evidence categories behind it. |
| `/fomo runner <mint>` | The write-once timeline for one exact mint: what the bot knew at each stage, the market cap at that moment, when a human first saw it, and — if nobody was pinged — the structured reason why. |
| `/fomo collisions` | Tokens sharing a name or a story: the collision group, each candidate's exact mint, the graded directional link, and which one the resolver ranks first with its reasons. |
| `/fomo profit view:alerts` | Alert performance as timing, not volume: how early alerts actually were, median move before and after the alert, the late-alert rate, and an audit of runners the bot saw but never surfaced. |

`/fomo realtime` carries the early block: whether the lane is on, heads-ups and runners published,
how long ago the last early alert fired, and the honest status of the X/social and wallet-stream
lanes.

### Profit dashboard (v2.40, admin only)

| Command | What it answers |
| --- | --- |
| `/fomo profit` | **Is the simulated account making money?** Bankroll, NET PnL, ROI, expectancy per $10 trade, profit factor, max drawdown, best and worst signal family, premature exit rate, and provider calls per 100 published signals. |
| `/fomo profit view:signals` | Every signal family ranked by measured forward expectancy, with sample, NET, profit factor, drawdown, severe-failure rate and the bounded ranking weight the forward data earned it. |
| `/fomo profit view:exits` | Every exit rule scored against what happened next: count, average NET, premature rate, defensive rate, upside given up, loss avoided and net regret — worst offender first. |
| `/fomo profit view:providers` | Per provider: calls, cache hits, errors, calls the breaker skipped, health, whether it is essential, and whether a cheaper on-chain path exists. |

Provider cost is reported as **calls**, not dollars: request pricing differs per plan, and inventing
a dollar figure the bot cannot verify is exactly the kind of fabricated number the rest of this
codebase refuses to produce.

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

The v2.40 suite adds the profit-first work: that a credit failure opens a bounded backoff and a
404 does not, that the refresh throttle no longer depends on a non-empty candidate pool, that a
timeout never logs as an empty string, that a provider outage is monitored rather than sold while a
confirmed failure still exits in full, that one lucky coin cannot move the ranking, that a losing
family is demoted and a losing *and rugging* family is retired, that weights stay bounded and
auditable, that historical opportunity cannot outrank current edge, that the ping gate can only
withhold, and that exit regret never reaches a live decision.

The v2.41 suite adds the alpha engine: that the operator is alerted before a deliberately slow
20-second enrichment pass finishes, that the cheap lane runs before the deep gather in the radar
loop, that the alert fires at the first-seen market cap and that cap can never be rewritten, that a
late alert is labelled `EDGE_CONSUMED` and never pings, that a high score with no serious evidence
category is demoted to a heads-up, that a creator self-buy is not demand, that a large buy needs
independent follow-on buyers, that same name is never the same token, that a token that only claims
a story cannot inherit it and can never be marked `OFFICIAL`, that a lone weak momentum print no
longer dumps a healthy runner while heavy selling and distribution still de-risk on one
observation, that a missing snapshot records a structured reason instead of failing silently, that
an early-lane failure never breaks the radar, and that no module in the early lane can call a
provider.

`python tests/run_selfcheck.py` re-asserts the non-negotiables — including the $10 entry size, the
$100/5/$50 caps, the STRICT PAPER separation, `SHADOW_REAL_MONEY_SPEND = 0`, the provider backoff,
the outage-is-not-a-failure rule, the small-sample protection, the cheap-lane ordering, the
"a score alone never pings" rule, MINT IS IDENTITY and the soft-pause hold — without pytest.
