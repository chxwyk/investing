"""Curated public-account registry and social-signal semantics.

Sections W, X, Y, Z, AA, AB and AC.

Design rules that this module enforces structurally rather than by convention:

* The broad X firehose is **off by default**.  Everything outside the curated
  registry is muted, so the ~1,494 J7 accounts cost nothing until an operator
  explicitly supplies a legitimate list.
* No single public account can produce an entry.  ``can_enter`` is ``False`` for
  every account in every tier, and the idea-only registry additionally cannot
  qualify a token or trigger any launch.
* Tier membership is a *starting* hypothesis.  Predictive value is measured from
  forward observations, and material strategy weight requires a real sample.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig

ZERO = Decimal("0")

TIER_A = "TIER_A_OFFICIAL"
TIER_B = "TIER_B_ONCHAIN_MARKET"
TIER_C = "TIER_C_SENTIMENT"
TIER_IDEA = "IDEA_ONLY"
TIER_MUTED = "MUTED"

# --- social signal classification (section AB) ------------------------------
MENTIONED = "MENTIONED"
PROMOTED = "PROMOTED"
DISCLOSED_BUY = "DISCLOSED_BUY"
DISCLOSED_HOLD = "DISCLOSED_HOLD"
PUBLIC_WALLET_BUY = "PUBLIC_WALLET_BUY"
PUBLIC_WALLET_SELL = "PUBLIC_WALLET_SELL"

SIGNAL_CLASSIFICATIONS = frozenset(
    {MENTIONED, PROMOTED, DISCLOSED_BUY, DISCLOSED_HOLD, PUBLIC_WALLET_BUY, PUBLIC_WALLET_SELL}
)

# --- edge state (section O / AB) --------------------------------------------
EDGE_FRESH = "FRESH"
EDGE_AGING = "AGING"
EDGE_CONSUMED = "EDGE_CONSUMED"

# --- account classification (section AA) ------------------------------------
HIGH_VALUE_EARLY = "HIGH_VALUE_EARLY"
USEFUL_CONFIRMATION = "USEFUL_CONFIRMATION"
NARRATIVE_ONLY = "NARRATIVE_ONLY"
LATE_CHASER = "LATE_CHASER"
PROMOTIONAL_LOW_CONFIDENCE = "PROMOTIONAL_LOW_CONFIDENCE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

BEFORE_MOVE = "BEFORE_MOVE"
DURING_MOVE = "DURING_MOVE"
AFTER_MOVE = "AFTER_MOVE"

FORWARD_HORIZONS_SECONDS: tuple[int, ...] = (60, 300, 900, 1_800, 3_600, 14_400, 86_400)
FORWARD_HORIZON_LABELS: dict[int, str] = {
    60: "+1m",
    300: "+5m",
    900: "+15m",
    1_800: "+30m",
    3_600: "+1h",
    14_400: "+4h",
    86_400: "+24h",
}


@dataclass(frozen=True, slots=True)
class CuratedAccount:
    """One manually reviewed public account and exactly what it may do."""

    handle: str
    tier: str
    purpose: str
    platform: str = "x"

    @property
    def idea_only(self) -> bool:
        return self.tier == TIER_IDEA

    @property
    def can_qualify_token(self) -> bool:
        """Idea-only accounts may never qualify a token."""

        return self.tier in {TIER_A, TIER_B, TIER_C}

    @property
    def can_enter(self) -> bool:
        """No public account can ever independently produce a PAPER entry."""

        return False

    @property
    def can_launch(self) -> bool:
        """No public account can ever trigger a token or J7 launch."""

        return False


def _accounts(tier: str, purpose: str, handles: Iterable[str]) -> tuple[CuratedAccount, ...]:
    return tuple(
        CuratedAccount(handle=handle.lstrip("@").casefold(), tier=tier, purpose=purpose)
        for handle in handles
    )


TIER_A_ACCOUNTS: tuple[CuratedAccount, ...] = _accounts(
    TIER_A,
    "official platform / ecosystem / infrastructure context",
    (
        "solana",
        "solanafndn",
        "pumpfun",
        "jupiterexchange",
        "raydium",
        "meteoraag",
        "orca_so",
        "bagsapp",
        "phantom",
    ),
)

TIER_B_ACCOUNTS: tuple[CuratedAccount, ...] = _accounts(
    TIER_B,
    "public on-chain events, listings and fast market context",
    (
        "lookonchain",
        "arkham",
        "newlistingsfeed",
        "deitaone",
        "firstsquawk",
        "dbnewswire",
        "aggrnews",
        "tier10k",
        "unusual_whales",
        "degeneratenews",
    ),
)

TIER_C_ACCOUNTS: tuple[CuratedAccount, ...] = _accounts(
    TIER_C,
    "Solana sentiment, trench narrative and trader commentary",
    (
        "toly",
        "mert",
        "ansem",
        "muststopmurad",
        "theflowhorse",
        "andrewkang",
        "circus_trade",
        "trenchtoday01",
    ),
)

IDEA_ONLY_ACCOUNTS: tuple[CuratedAccount, ...] = _accounts(
    TIER_IDEA,
    "meme / culture / event idea discovery only — never a token signal",
    (
        "truth_terminal",
        "knowyourmeme",
        "dexerto",
        "pubity",
        "dailyloud",
        "discussingfilm",
        "elonmusk",
        "matt_furie",
        "mrbeast",
        "gtavi_countdown",
        "colossal",
        "khaokheowzoo",
        "nishiyama_zoo",
        "ichikawa_zoo",
        "spacex",
        "nasa",
    ),
)

CURATED_ACCOUNTS: Mapping[str, CuratedAccount] = {
    account.handle: account
    for account in (
        *TIER_A_ACCOUNTS,
        *TIER_B_ACCOUNTS,
        *TIER_C_ACCOUNTS,
        *IDEA_ONLY_ACCOUNTS,
    )
}

SIGNAL_TIERS: tuple[str, ...] = (TIER_A, TIER_B, TIER_C)


def normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").casefold()


def lookup_account(handle: str) -> CuratedAccount | None:
    return CURATED_ACCOUNTS.get(normalize_handle(handle))


def account_tier(handle: str) -> str:
    account = lookup_account(handle)
    return account.tier if account else TIER_MUTED


def is_muted(handle: str) -> bool:
    """Everything outside the curated registry is muted by default (section Y)."""

    return lookup_account(handle) is None


def registry_snapshot() -> dict[str, tuple[str, ...]]:
    return {
        TIER_A: tuple(item.handle for item in TIER_A_ACCOUNTS),
        TIER_B: tuple(item.handle for item in TIER_B_ACCOUNTS),
        TIER_C: tuple(item.handle for item in TIER_C_ACCOUNTS),
        TIER_IDEA: tuple(item.handle for item in IDEA_ONLY_ACCOUNTS),
    }


@dataclass(frozen=True, slots=True)
class SocialSignal:
    """One public post, classified honestly.  A mention is not a buy."""

    platform: str
    account: str
    url: str
    observed_at: int
    source_timestamp: int
    classification: str = MENTIONED
    mint: str | None = None
    exact_mint_confidence: Decimal = ZERO
    price_at_signal: Decimal | None = None
    market_cap_at_signal: Decimal | None = None
    tier: str = TIER_MUTED
    text_hash: str = ""

    def __post_init__(self) -> None:
        if self.classification not in SIGNAL_CLASSIFICATIONS:
            raise ValueError(f"unknown social classification: {self.classification}")

    @property
    def idea_only(self) -> bool:
        return self.tier == TIER_IDEA

    @property
    def is_disclosed_position(self) -> bool:
        return self.classification in {DISCLOSED_BUY, PUBLIC_WALLET_BUY}

    @property
    def can_qualify_token(self) -> bool:
        return self.tier in SIGNAL_TIERS and self.exact_mint_confidence >= Decimal("80")

    @property
    def can_enter(self) -> bool:
        """Structural guarantee: no post, from any tier, ever enters."""

        return False

    @property
    def can_launch(self) -> bool:
        return False

    @property
    def dedupe_key(self) -> str:
        return f"{self.platform}:{self.account}:{self.text_hash or self.url}"


def build_signal(
    *,
    platform: str,
    account: str,
    url: str,
    observed_at: int,
    source_timestamp: int,
    classification: str = MENTIONED,
    mint: str | None = None,
    exact_mint_confidence: Decimal = ZERO,
    price_at_signal: Decimal | None = None,
    market_cap_at_signal: Decimal | None = None,
    text_hash: str = "",
) -> SocialSignal:
    return SocialSignal(
        platform=platform,
        account=normalize_handle(account),
        url=url,
        observed_at=observed_at,
        source_timestamp=source_timestamp,
        classification=classification,
        mint=mint,
        exact_mint_confidence=exact_mint_confidence,
        price_at_signal=price_at_signal,
        market_cap_at_signal=market_cap_at_signal,
        tier=account_tier(account),
        text_hash=text_hash,
    )


def dedupe_signals(signals: Iterable[SocialSignal]) -> tuple[SocialSignal, ...]:
    seen: set[str] = set()
    unique: list[SocialSignal] = []
    for signal in signals:
        if signal.dedupe_key in seen:
            continue
        seen.add(signal.dedupe_key)
        unique.append(signal)
    return tuple(unique)


def signal_edge_state(
    signal: SocialSignal,
    *,
    current_price: Decimal | None,
    now: int,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> tuple[str, Decimal | None]:
    """Classify how much of a signal's edge is already gone (section O)."""

    age = max(0, now - (signal.source_timestamp or signal.observed_at))
    move: Decimal | None = None
    if signal.price_at_signal and signal.price_at_signal > 0 and current_price is not None:
        move = ((current_price - signal.price_at_signal) / signal.price_at_signal * 100).quantize(
            Decimal("0.01")
        )
    if move is not None and move >= config.max_move_since_signal_percent:
        return EDGE_CONSUMED, move
    if age > config.max_signal_age_seconds:
        return EDGE_CONSUMED, move
    if age > config.max_signal_age_seconds // 2 or (move is not None and move >= 40):
        return EDGE_AGING, move
    return EDGE_FRESH, move


@dataclass(frozen=True, slots=True)
class AccountObservation:
    """One measured forward outcome of one account's exact-mint signal."""

    account: str
    mint: str
    signalled_at: int
    move_before_signal_percent: Decimal | None = None
    forward_returns: Mapping[int, Decimal] = field(default_factory=dict)
    max_favourable_percent: Decimal | None = None
    max_adverse_percent: Decimal | None = None
    rugged: bool = False
    repeated_promotion: bool = False
    smart_wallet_overlap: int = 0
    coordinated_wallet_overlap: int = 0


@dataclass(frozen=True, slots=True)
class AccountPerformance:
    """Measured predictive usefulness — never assumed from tier membership."""

    account: str
    tier: str = TIER_MUTED
    samples: int = 0
    lead_lag: str = INSUFFICIENT_DATA
    classification: str = INSUFFICIENT_DATA
    median_forward_return_percent: Decimal | None = None
    hit_10_percent: Decimal | None = None
    hit_25_percent: Decimal | None = None
    hit_50_percent: Decimal | None = None
    hit_100_percent: Decimal | None = None
    median_drawdown_percent: Decimal | None = None
    failure_rate_percent: Decimal | None = None
    repeated_promotion_rate_percent: Decimal | None = None
    smart_wallet_correlation: Decimal | None = None
    coordinated_wallet_correlation: Decimal | None = None
    forward_medians: Mapping[int, Decimal] = field(default_factory=dict)
    strategy_weight: Decimal = ZERO

    @property
    def has_material_sample(self) -> bool:
        return self.classification != INSUFFICIENT_DATA


def measure_account(
    account: str,
    observations: Sequence[AccountObservation],
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> AccountPerformance:
    """Learn whether an account leads, coincides with, or lags the move."""

    tier = account_tier(account)
    if not observations:
        return AccountPerformance(account=normalize_handle(account), tier=tier)

    before = [
        item.move_before_signal_percent
        for item in observations
        if item.move_before_signal_percent is not None
    ]
    peaks = [
        item.max_favourable_percent
        for item in observations
        if item.max_favourable_percent is not None
    ]
    drawdowns = sorted(
        item.max_adverse_percent for item in observations if item.max_adverse_percent is not None
    )
    horizon_medians: dict[int, Decimal] = {}
    for horizon in FORWARD_HORIZONS_SECONDS:
        values = sorted(
            item.forward_returns[horizon]
            for item in observations
            if horizon in item.forward_returns
        )
        median = _median(values)
        if median is not None:
            horizon_medians[horizon] = median

    overall = _median(
        sorted(
            value
            for item in observations
            for horizon, value in item.forward_returns.items()
            if horizon == 3_600
        )
    ) or _median(sorted(horizon_medians.values()))

    rugs = sum(1 for item in observations if item.rugged)
    promotions = sum(1 for item in observations if item.repeated_promotion)
    smart_overlap = sum(1 for item in observations if item.smart_wallet_overlap > 0)
    coordinated = sum(1 for item in observations if item.coordinated_wallet_overlap > 0)
    sample = len(observations)

    lead_lag = _lead_lag(before)
    performance = AccountPerformance(
        account=normalize_handle(account),
        tier=tier,
        samples=sample,
        lead_lag=lead_lag,
        median_forward_return_percent=overall,
        hit_10_percent=_hit_rate(peaks, Decimal("10")),
        hit_25_percent=_hit_rate(peaks, Decimal("25")),
        hit_50_percent=_hit_rate(peaks, Decimal("50")),
        hit_100_percent=_hit_rate(peaks, Decimal("100")),
        median_drawdown_percent=_median(drawdowns),
        failure_rate_percent=_rate(rugs, sample),
        repeated_promotion_rate_percent=_rate(promotions, sample),
        smart_wallet_correlation=_rate(smart_overlap, sample),
        coordinated_wallet_correlation=_rate(coordinated, sample),
        forward_medians=horizon_medians,
    )
    classification = classify_account(performance, config=config)
    weight = _strategy_weight(performance, classification, config=config)
    return AccountPerformance(
        account=performance.account,
        tier=performance.tier,
        samples=performance.samples,
        lead_lag=performance.lead_lag,
        classification=classification,
        median_forward_return_percent=performance.median_forward_return_percent,
        hit_10_percent=performance.hit_10_percent,
        hit_25_percent=performance.hit_25_percent,
        hit_50_percent=performance.hit_50_percent,
        hit_100_percent=performance.hit_100_percent,
        median_drawdown_percent=performance.median_drawdown_percent,
        failure_rate_percent=performance.failure_rate_percent,
        repeated_promotion_rate_percent=performance.repeated_promotion_rate_percent,
        smart_wallet_correlation=performance.smart_wallet_correlation,
        coordinated_wallet_correlation=performance.coordinated_wallet_correlation,
        forward_medians=performance.forward_medians,
        strategy_weight=weight,
    )


def classify_account(
    performance: AccountPerformance,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> str:
    """Require a real sample before an account earns any label at all."""

    if performance.samples < config.social_min_account_samples:
        return INSUFFICIENT_DATA
    if (
        performance.repeated_promotion_rate_percent is not None
        and performance.repeated_promotion_rate_percent >= 50
    ):
        return PROMOTIONAL_LOW_CONFIDENCE
    if performance.failure_rate_percent is not None and performance.failure_rate_percent >= 40:
        return PROMOTIONAL_LOW_CONFIDENCE
    if performance.lead_lag == AFTER_MOVE:
        return LATE_CHASER
    if (
        performance.lead_lag == BEFORE_MOVE
        and performance.hit_25_percent is not None
        and performance.hit_25_percent >= 40
    ):
        return HIGH_VALUE_EARLY
    if performance.hit_10_percent is not None and performance.hit_10_percent >= 40:
        return USEFUL_CONFIRMATION
    return NARRATIVE_ONLY


def _strategy_weight(
    performance: AccountPerformance,
    classification: str,
    *,
    config: LabConfig,
) -> Decimal:
    """Material weight requires both a sample and a signal-capable tier."""

    if classification == INSUFFICIENT_DATA:
        return ZERO
    if performance.tier not in SIGNAL_TIERS:
        return ZERO
    if performance.samples < config.social_min_account_samples:
        return ZERO
    base = {
        HIGH_VALUE_EARLY: Decimal("1"),
        USEFUL_CONFIRMATION: Decimal("0.5"),
        NARRATIVE_ONLY: Decimal("0.15"),
        LATE_CHASER: ZERO,
        PROMOTIONAL_LOW_CONFIDENCE: ZERO,
    }.get(classification, ZERO)
    return base


# --- request / credit control (section Z) -----------------------------------


@dataclass(frozen=True, slots=True)
class SocialFetchPlan:
    """Exactly which accounts to poll, and how many posts each may cost."""

    accounts: tuple[str, ...] = ()
    posts_per_account: int = 0
    skipped_muted: tuple[str, ...] = ()
    skipped_cached: tuple[str, ...] = ()
    skipped_budget: tuple[str, ...] = ()
    reason: str = ""

    @property
    def estimated_requests(self) -> int:
        return len(self.accounts)

    @property
    def estimated_posts(self) -> int:
        return len(self.accounts) * self.posts_per_account


def plan_social_fetch(
    candidate_accounts: Sequence[str],
    *,
    now: int,
    cache: Mapping[str, int] | None = None,
    requests_used_today: int = 0,
    config: LabConfig = DEFAULT_LAB_CONFIG,
    require_relevance: bool = True,
) -> SocialFetchPlan:
    """Stage relevance -> cache -> budget before spending a single X request.

    With the broad radar disabled (the default) nothing is fetched unless a
    concrete candidate or event named the account as relevant.
    """

    cache = cache or {}
    if not config.broad_social_radar_enabled and require_relevance and not candidate_accounts:
        return SocialFetchPlan(reason="broad social radar is disabled and nothing is relevant")

    muted: list[str] = []
    cached: list[str] = []
    budget_blocked: list[str] = []
    selected: list[str] = []

    remaining = max(0, config.social_daily_request_budget - requests_used_today)
    for raw in candidate_accounts:
        handle = normalize_handle(raw)
        if is_muted(handle):
            muted.append(handle)
            continue
        fetched_at = cache.get(handle)
        if fetched_at is not None and now - fetched_at < config.social_account_cache_seconds:
            cached.append(handle)
            continue
        if len(selected) >= config.social_max_accounts_per_check:
            budget_blocked.append(handle)
            continue
        if len(selected) >= remaining:
            budget_blocked.append(handle)
            continue
        selected.append(handle)

    reason = ""
    if not selected:
        if budget_blocked and remaining <= 0:
            reason = "daily social request budget exhausted"
        elif cached:
            reason = "every relevant account is still cached"
        elif muted:
            reason = "every named account is muted by default"
    return SocialFetchPlan(
        accounts=tuple(selected),
        posts_per_account=config.social_posts_per_account if selected else 0,
        skipped_muted=tuple(muted),
        skipped_cached=tuple(cached),
        skipped_budget=tuple(budget_blocked),
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class SocialBudgetUsage:
    calls: int = 0
    posts_processed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    useful_signals: int = 0
    useless_signals: int = 0
    estimated_cost_usd: Decimal = ZERO

    def merged(self, other: SocialBudgetUsage) -> SocialBudgetUsage:
        return SocialBudgetUsage(
            calls=self.calls + other.calls,
            posts_processed=self.posts_processed + other.posts_processed,
            cache_hits=self.cache_hits + other.cache_hits,
            cache_misses=self.cache_misses + other.cache_misses,
            useful_signals=self.useful_signals + other.useful_signals,
            useless_signals=self.useless_signals + other.useless_signals,
            estimated_cost_usd=self.estimated_cost_usd + other.estimated_cost_usd,
        )


def idea_only_topics(signals: Iterable[SocialSignal]) -> tuple[str, ...]:
    """Idea-only accounts contribute topics to search, never tokens to buy."""

    return tuple(
        dict.fromkeys(
            signal.account for signal in signals if signal.tier == TIER_IDEA
        )
    )


def _lead_lag(before_move: Sequence[Decimal]) -> str:
    if not before_move:
        return INSUFFICIENT_DATA
    median = _median(sorted(before_move))
    if median is None:
        return INSUFFICIENT_DATA
    if median <= Decimal("15"):
        return BEFORE_MOVE
    if median <= Decimal("60"):
        return DURING_MOVE
    return AFTER_MOVE


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))


def _hit_rate(values: Sequence[Decimal], threshold: Decimal) -> Decimal | None:
    if not values:
        return None
    hits = sum(1 for value in values if value >= threshold)
    return (Decimal(hits) / Decimal(len(values)) * 100).quantize(Decimal("0.01"))


def _rate(count: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(count) / Decimal(total) * 100).quantize(Decimal("0.01"))
