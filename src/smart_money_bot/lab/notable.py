"""Notable public-wallet intelligence on the fast path (sections 3-10, 33).

The product problem: a demonstrably useful public wallet buys, and the user
finds out several minutes later, after the market has already moved.  The
required shape is

    PUBLIC CHAIN BUY OBSERVED -> persist -> minimal context -> alert -> enrich

not "wait for every provider, then maybe alert".

Two rules shape everything here.

**Big is not good.**  "Notable" is never defined by transaction size.  It is
defined by the existing forward-outcome reputation, so a famous wallet with bad
entries carries no more weight than an unknown one.

**Late is still worth seeing, but it is not a reason to chase.**  When the bot
observes an entry after the move, the signal is published with the lateness
quantified — trader entry market cap, bot observation market cap, and the move
between them — and simultaneously marked ``EDGE_CONSUMED`` so the PAPER engine
refuses to chase it.  Visibility and entry eligibility are separate concerns and
this module keeps them separate.

Identity is never guessed.  A wallet with no verified public mapping is a
``Proven Wallet #n``, never a person's name.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig
from .smartmoney import (
    LATE_CHASER,
    POOR_HISTORY,
    PROVEN_EARLY,
    USEFUL_CONFIRMATION,
    WalletReputation,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- identity provenance (section 4) -----------------------------------------
ADMIN_DEFINED = "ADMIN_DEFINED"
J7_AUTHORIZED = "J7_AUTHORIZED"
PUMP_PUBLIC_PROFILE = "PUMP_PUBLIC_PROFILE"
FOMO_PUBLIC_PROFILE = "FOMO_PUBLIC_PROFILE"
PUBLIC_SOCIAL_DISCLOSURE = "PUBLIC_SOCIAL_DISCLOSURE"
DOCUMENTED_PROVIDER = "DOCUMENTED_PROVIDER"
ONCHAIN_ONLY = "ONCHAIN_ONLY"

PROVENANCE = frozenset(
    {
        ADMIN_DEFINED,
        J7_AUTHORIZED,
        PUMP_PUBLIC_PROFILE,
        FOMO_PUBLIC_PROFILE,
        PUBLIC_SOCIAL_DISCLOSURE,
        DOCUMENTED_PROVIDER,
        ONCHAIN_ONLY,
    }
)

#: Provenance that carries a real public identity claim.  ``ONCHAIN_ONLY`` does
#: not: such a wallet earns its standing from outcomes and stays anonymous.
IDENTIFIED_PROVENANCE = frozenset(
    {
        ADMIN_DEFINED,
        J7_AUTHORIZED,
        PUMP_PUBLIC_PROFILE,
        FOMO_PUBLIC_PROFILE,
        PUBLIC_SOCIAL_DISCLOSURE,
        DOCUMENTED_PROVIDER,
    }
)

# --- signal freshness (sections 7, 33) ---------------------------------------
FRESH = "FRESH"
RECENT = "RECENT"
LATE = "LATE"
EDGE_CONSUMED = "EDGE_CONSUMED"

#: Freshness states that must never produce an automatic PAPER chase.
NO_CHASE_STATES = frozenset({LATE, EDGE_CONSUMED})

# --- trade side ---------------------------------------------------------------
BUY = "BUY"
SELL = "SELL"

#: Reputation states that justify an urgent ping on their own.
URGENT_REPUTATIONS = frozenset({PROVEN_EARLY})

#: Reputation states worth surfacing at all.
SURFACEABLE_REPUTATIONS = frozenset({PROVEN_EARLY, USEFUL_CONFIRMATION})


@dataclass(frozen=True, slots=True)
class NotableWallet:
    """A verified public wallet mapping.  Identity is never inferred."""

    wallet: str
    label: str = ""
    provenance: str = ONCHAIN_ONLY
    verification_source: str = ""
    confidence: Decimal = ZERO
    category: str = "trader"
    enabled: bool = True
    last_verified_at: int | None = None
    anonymous_index: int | None = None

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE:
            raise ValueError(f"unknown provenance: {self.provenance}")
        if self.label and self.provenance == ONCHAIN_ONLY:
            raise ValueError("an on-chain-only wallet must not carry a public identity label")

    @property
    def identified(self) -> bool:
        return bool(self.label) and self.provenance in IDENTIFIED_PROVENANCE

    def display_name(self, reputation: WalletReputation | None = None) -> str:
        """A verified label, or an honest anonymous handle — never a guess."""

        if self.identified:
            return self.label
        if self.anonymous_index is not None:
            state = (reputation.state if reputation else None) or "Unknown"
            prefix = "Proven Wallet" if state == PROVEN_EARLY else "Wallet"
            return f"{prefix} #{self.anonymous_index}"
        return f"Wallet {self.wallet[:4]}…{self.wallet[-4:]}"


@dataclass(frozen=True, slots=True)
class NotableTrade:
    """One observed public on-chain trade by a monitored wallet."""

    wallet: str
    mint: str
    signature: str
    side: str = BUY
    chain_time: int = 0
    observed_at: int = 0
    amount_usd: Decimal | None = None
    entry_price_usd: Decimal | None = None
    entry_market_cap_usd: Decimal | None = None

    @property
    def detection_delay_seconds(self) -> int | None:
        """How long after the chain event the bot actually saw it."""

        if not self.chain_time or not self.observed_at:
            return None
        return max(0, self.observed_at - self.chain_time)


@dataclass(frozen=True, slots=True)
class NotableSignal:
    """A publishable notable-wallet event with the lateness fully quantified.

    Every field the product contract calls mandatory is present: the trader's
    entry time, price and market cap, the bot's detection time and market cap,
    the current price and market cap, and both moves.
    """

    trade: NotableTrade
    wallet_profile: NotableWallet
    reputation: WalletReputation | None = None
    detection_market_cap_usd: Decimal | None = None
    current_price_usd: Decimal | None = None
    current_market_cap_usd: Decimal | None = None
    now: int = 0

    @property
    def display_name(self) -> str:
        return self.wallet_profile.display_name(self.reputation)

    @property
    def reputation_state(self) -> str:
        return self.reputation.state if self.reputation else "UNKNOWN"

    @property
    def move_since_trader_entry_percent(self) -> Decimal | None:
        return _change(self.current_market_cap_usd, self.trade.entry_market_cap_usd)

    @property
    def move_since_detection_percent(self) -> Decimal | None:
        return _change(self.current_market_cap_usd, self.detection_market_cap_usd)

    @property
    def signal_age_seconds(self) -> int | None:
        if not self.now or not self.trade.chain_time:
            return None
        return max(0, self.now - self.trade.chain_time)

    def freshness(self, *, config: LabConfig = DEFAULT_LAB_CONFIG) -> str:
        """How much of the move has already happened since the trader entered."""

        move = self.move_since_trader_entry_percent
        age = self.signal_age_seconds
        if move is not None and move >= config.max_move_since_signal_percent:
            return EDGE_CONSUMED
        if age is not None and age > config.max_signal_age_seconds:
            return EDGE_CONSUMED if (move or ZERO) >= Decimal("40") else LATE
        if move is not None and move >= Decimal("40"):
            return LATE
        if age is not None and age <= 120:
            return FRESH
        return RECENT

    def may_chase(self, *, config: LabConfig = DEFAULT_LAB_CONFIG) -> bool:
        """Whether the PAPER engine may treat this as a current entry signal.

        Visibility is never the question here — a LATE signal is still
        published.  This only answers whether it may drive an automatic entry.
        """

        return self.freshness(config=config) not in NO_CHASE_STATES

    def warnings(self, *, config: LabConfig = DEFAULT_LAB_CONFIG) -> tuple[str, ...]:
        """Aggressive, quantified lateness markers for the card."""

        state = self.freshness(config=config)
        notes: list[str] = []
        if state in NO_CHASE_STATES:
            notes.append(f"⚠ {state.replace('_', ' ')}")
            move = self.move_since_trader_entry_percent
            if move is not None:
                notes.append(f"⚠ {move:+.1f}% since the trader entered")
            delay = self.trade.detection_delay_seconds
            if delay is not None:
                notes.append(f"⚠ observed {delay}s after the chain event")
        if self.reputation_state in {LATE_CHASER, POOR_HISTORY}:
            notes.append(f"⚠ wallet history: {self.reputation_state}")
        return tuple(notes)


@dataclass(frozen=True, slots=True)
class NotableConsensus:
    """Cluster-adjusted multi-wallet consensus (section 9).

    Four wallets funded from one upstream account are one actor, and are never
    reported as four independent confirmations.
    """

    raw_wallets: int = 0
    independent_wallets: int = 0
    funding_clusters: int = 0
    proven_early: int = 0
    earliest_entry_market_cap_usd: Decimal | None = None
    median_entry_market_cap_usd: Decimal | None = None
    current_market_cap_usd: Decimal | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def move_since_earliest_percent(self) -> Decimal | None:
        return _change(self.current_market_cap_usd, self.earliest_entry_market_cap_usd)

    @property
    def is_independent_consensus(self) -> bool:
        """Two or more genuinely independent wallets, not one funded swarm."""

        return self.independent_wallets >= 2 and self.independent_wallets > self.funding_clusters


def build_consensus(
    signals: Sequence[NotableSignal],
    *,
    cluster_of: dict[str, str] | None = None,
    current_market_cap_usd: Decimal | None = None,
) -> NotableConsensus:
    """Collapse per-wallet signals into cluster-adjusted consensus."""

    if not signals:
        return NotableConsensus()

    clusters = cluster_of or {}
    wallets = [item.trade.wallet for item in signals]
    groups = {clusters.get(wallet, f"solo:{wallet}") for wallet in wallets}
    shared = len(wallets) - len(groups)

    entries = sorted(
        item.trade.entry_market_cap_usd
        for item in signals
        if item.trade.entry_market_cap_usd is not None
    )
    warnings: list[str] = []
    if shared > 0:
        warnings.append(
            f"{shared + 1} wallets share a funding cluster and count as one actor"
        )

    return NotableConsensus(
        raw_wallets=len(wallets),
        independent_wallets=len(groups),
        funding_clusters=sum(1 for key in groups if not key.startswith("solo:")),
        proven_early=sum(1 for item in signals if item.reputation_state == PROVEN_EARLY),
        earliest_entry_market_cap_usd=entries[0] if entries else None,
        median_entry_market_cap_usd=_median(entries),
        current_market_cap_usd=current_market_cap_usd,
        warnings=tuple(warnings),
    )


# --- distribution / exit-liquidity (sections 10, 34) -------------------------
DISTRIBUTION_NONE = "NONE"
DISTRIBUTION_REDUCING = "REDUCING"
DISTRIBUTION_HEAVY = "HEAVY"


@dataclass(frozen=True, slots=True)
class DistributionSignal:
    """Meaningful selling by wallets that were previously accumulating."""

    previously_independent_holders: int = 0
    reducing_wallets: int = 0
    flow_weakening: bool = False
    liquidity_declining: bool = False
    momentum_decelerating: bool = False

    @property
    def state(self) -> str:
        if self.reducing_wallets <= 0:
            return DISTRIBUTION_NONE
        if self.reducing_wallets >= max(2, self.previously_independent_holders // 2):
            return DISTRIBUTION_HEAVY
        return DISTRIBUTION_REDUCING

    @property
    def alertable(self) -> bool:
        """One wallet selling is never an alert on its own."""

        return self.state != DISTRIBUTION_NONE and (
            self.reducing_wallets >= 2
            or self.flow_weakening
            or self.liquidity_declining
        )

    @property
    def exit_liquidity_risk(self) -> bool:
        """Smart money early, retail late — the shape worth warning about."""

        return self.state == DISTRIBUTION_HEAVY and (
            self.flow_weakening or self.momentum_decelerating
        )


def exit_liquidity_warning(
    signal: NotableSignal,
    distribution: DistributionSignal,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> str | None:
    """Warn when the early money is leaving into late interest."""

    move = signal.move_since_trader_entry_percent
    if move is None or move < config.max_move_since_signal_percent / 2:
        return None
    if distribution.exit_liquidity_risk:
        return "⚠ EXIT-LIQUIDITY RISK — smart money entered early and is distributing"
    if signal.freshness(config=config) in NO_CHASE_STATES:
        return "⚠ SMART MONEY EARLY — RETAIL LATE"
    return None


# --- ping policy (sections 30, 31) -------------------------------------------
PING_PROVEN_EARLY = "PROVEN_EARLY wallet entered {age}s ago"
PING_INDEPENDENT_CONSENSUS = "{count} independent historically strong wallets accumulating"
PING_DISTRIBUTION = "independent proven wallets are distributing"


@dataclass(frozen=True, slots=True)
class PingDecision:
    """Whether to ping, and the explicit reason the user will be shown."""

    ping: bool = False
    reason: str = ""
    urgent: bool = False

    @property
    def label(self) -> str:
        return f"PING REASON: {self.reason}" if self.reason else ""


def decide_ping(
    signal: NotableSignal,
    *,
    consensus: NotableConsensus | None = None,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> PingDecision:
    """Ping only for genuinely notable, still-current wallet activity.

    A late signal is still *published* — it simply does not earn an urgent ping,
    because the user does not need to be interrupted for a move that already
    happened.
    """

    if (
        consensus is not None
        and consensus.is_independent_consensus
        and consensus.proven_early >= 2
    ):
        return PingDecision(
            ping=True,
            urgent=True,
            reason=PING_INDEPENDENT_CONSENSUS.format(count=consensus.independent_wallets),
        )
    state = signal.freshness(config=config)
    if state in NO_CHASE_STATES:
        return PingDecision(ping=False, reason=f"observed {state.replace('_', ' ').lower()}")
    if signal.reputation_state in URGENT_REPUTATIONS:
        age = signal.signal_age_seconds
        return PingDecision(
            ping=True,
            urgent=True,
            reason=PING_PROVEN_EARLY.format(age=age if age is not None else "?"),
        )
    if signal.reputation_state in SURFACEABLE_REPUTATIONS:
        return PingDecision(ping=False, reason="useful confirmation, not urgent")
    return PingDecision(ping=False, reason="wallet has no material forward sample")


def _change(current: Decimal | None, base: Decimal | None) -> Decimal | None:
    if current is None or base is None or base <= 0:
        return None
    return ((current - base) / base * HUNDRED).quantize(Decimal("0.01"))


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))
