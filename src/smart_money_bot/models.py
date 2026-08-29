from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionMode(StrEnum):
    ALERTS = "ALERTS"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class TrackedTrader:
    address: str
    alias: str
    enabled: bool = True
    last_signature: str | None = None
    weight: Decimal = Decimal("1")
    source: str = "manual"


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    address: str
    alias: str
    realized_pnl_24h: Decimal
    previous_pnl_24h: Decimal | None
    roi_24h_percent: Decimal
    win_rate_percent: Decimal
    trades_24h: int
    buys_24h: int
    sells_24h: int
    closed_tokens: int
    invested_24h_usd: Decimal
    volume_24h_usd: Decimal
    last_trade_ms: int | None
    score: Decimal
    rank: int
    realized_pnl_7d: Decimal = Decimal("0")
    roi_7d_percent: Decimal = Decimal("0")
    win_rate_7d_percent: Decimal = Decimal("0")
    trades_7d: int = 0
    recent_swaps: int = 0
    pump_swaps: int = 0
    last_activity_at: int | None = None
    selection_reason: str = ""
    metrics_limited_24h: bool = False
    metrics_limited_7d: bool = False

    @property
    def pnl_momentum_usd(self) -> Decimal | None:
        if self.previous_pnl_24h is None:
            return None
        return self.realized_pnl_24h - self.previous_pnl_24h


@dataclass(frozen=True, slots=True)
class DiscoveryRefresh:
    candidates: tuple[DiscoveryCandidate, ...]
    added_wallets: tuple[str, ...]
    disabled_wallets: tuple[str, ...]
    refreshed_at: int
    candidate_pool_size: int = 0
    verified_pump_wallets: int = 0
    removal_events: tuple[WalletRotationEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class WalletRotationEvent:
    address: str
    alias: str
    action: str
    reason: str
    score: Decimal
    pnl_24h_usd: Decimal
    pnl_7d_usd: Decimal
    baseline_pnl_24h_usd: Decimal
    baseline_pnl_7d_usd: Decimal
    observed_source_pnl_usd: Decimal
    paper_pnl_usd: Decimal
    recorded_at: int


@dataclass(frozen=True, slots=True)
class DetectedSwap:
    signature: str
    trader_address: str
    block_time: int
    side: Side
    token_mint: str
    token_amount: Decimal
    quote_mint: str
    quote_amount: Decimal
    usd_value: Decimal | None
    token_price_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class TokenInfo:
    mint: str
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    usd_price: Decimal | None = None
    liquidity_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    holder_count: int | None = None
    organic_score: Decimal | None = None
    verified: bool | None = None
    suspicious: bool = False
    mint_authority_disabled: bool | None = None
    freeze_authority_disabled: bool | None = None
    top_holders_percent: Decimal | None = None
    dev_balance_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DexSnapshot:
    available: bool
    liquidity_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    pair_age_minutes: int | None = None
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    volume_5m_usd: Decimal = Decimal("0")
    volume_1h_usd: Decimal = Decimal("0")
    price_change_5m_percent: Decimal | None = None
    price_change_1h_percent: Decimal | None = None
    active_boosts: int = 0
    has_website: bool = False
    has_x_profile: bool = False
    website_url: str = ""
    x_handle: str = ""
    pair_url: str = ""


@dataclass(frozen=True, slots=True)
class XSocialSnapshot:
    available: bool
    posts: int = 0
    contract_posts: int = 0
    identity_posts: int = 0
    unique_authors: int = 0
    established_authors: int = 0
    influential_authors: int = 0
    suspicious_authors: int = 0
    crypto_authors: int = 0
    credible_crypto_authors: int = 0
    contract_authors: int = 0
    credible_contract_authors: int = 0
    trusted_crypto_authors: int = 0
    trusted_news_authors: int = 0
    trusted_investigator_authors: int = 0
    trusted_official_authors: int = 0
    trusted_market_authors: int = 0
    million_follower_authors: int = 0
    coin_intent_posts: int = 0
    promoter_posts: int = 0
    engagements: int = 0
    duplicate_percent: Decimal = Decimal("0")
    posts_per_minute: Decimal = Decimal("0")
    notable_accounts: tuple[str, ...] = ()
    notable_posts: tuple[str, ...] = ()
    query: str = ""
    error: str | None = None
    verification_id: int | None = None
    verification_state: str = "NOT_VERIFIED"
    checked_at: int = 0


@dataclass(frozen=True, slots=True)
class NewsAlert:
    source: str
    headline: str
    summary: str
    url: str
    author: str = ""
    author_followers: int = 0
    author_verified: bool = False
    score: int = 0
    urgency: str = "LOW"
    matched_rule: str = ""
    narrative_terms: tuple[str, ...] = ()
    token_mints: tuple[str, ...] = ()
    image_urls: tuple[str, ...] = ()
    created_at: int = 0
    received_at: int = 0


@dataclass(frozen=True, slots=True)
class NarrativePairMatch:
    narrative: str
    mint: str
    symbol: str
    name: str
    liquidity_usd: Decimal | None
    market_cap_usd: Decimal | None
    pair_age_minutes: int | None
    buys_5m: int
    sells_5m: int
    volume_5m_usd: Decimal
    pair_url: str


@dataclass(frozen=True, slots=True)
class NarrativeCompetition:
    query: str
    matching_pairs: int = 0
    liquid_pairs: int = 0
    strongest_liquidity_usd: Decimal | None = None
    strongest_market_cap_usd: Decimal | None = None
    strongest_mint: str = ""
    strongest_symbol: str = ""
    strongest_pair_url: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LaunchOpportunity:
    alert: NewsAlert
    score: int
    verdict: str
    confidence: str
    category: str
    coin_name: str
    coin_symbol: str
    primary_narrative: str
    source_score: int
    speed_score: int
    viral_score: int
    x_score: int
    confirmation_score: int
    competition_score: int
    identity_score: int
    lane: str
    crypto_attention_ready: bool
    x_verified: bool
    no_x_candidate_ready: bool
    exceptional_event: bool
    us_relevant: bool
    cross_source_count: int
    competition: NarrativeCompetition
    x_evidence: XSocialSnapshot
    positives: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    generated_at: int = 0
    pre_x_score: int = 0


@dataclass(frozen=True, slots=True)
class LaunchDraft:
    """Admin-reviewed metadata for one Launch Lab submission."""

    opportunity: LaunchOpportunity
    name: str
    symbol: str
    description: str
    creator_buy_sol: Decimal
    website_url: str = ""
    x_url: str = ""
    art_variant: int = 0


@dataclass(frozen=True, slots=True)
class PumpLaunchResult:
    success: bool
    status: str
    message: str
    alert_key: str
    name: str
    symbol: str
    mint: str = ""
    signature: str = ""
    metadata_uri: str = ""
    explorer_url: str = ""
    created_at: int = 0
    provider: str = ""


@dataclass(frozen=True, slots=True)
class TokenRiskSnapshot:
    available: bool
    score: Decimal | None = None
    rugged: bool = False
    snipers_percent: Decimal | None = None
    insiders_percent: Decimal | None = None
    bundlers_percent: Decimal | None = None
    top10_percent: Decimal | None = None
    dev_percent: Decimal | None = None
    danger_flags: tuple[str, ...] = ()
    jupiter_verified: bool | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CoinCallout:
    mint: str
    symbol: str | None
    name: str | None
    score: Decimal
    verdict: str
    confidence: str
    smart_wallets: tuple[str, ...]
    token_info: TokenInfo | None
    dex: DexSnapshot
    social: XSocialSnapshot
    tracker_risk: TokenRiskSnapshot
    positives: tuple[str, ...]
    warnings: tuple[str, ...]
    hard_blockers: tuple[str, ...]
    generated_at: int
    executable_quote: SwapQuote | None = None
    quote_error: str | None = None
    public_alert_eligible: bool = False
    prefilter_score: Decimal = Decimal("0")
    x_search_attempted: bool = False
    scan_stage: str = "UNSCANNED"
    scan_reason: str = ""
    sell_quote: SwapQuote | None = None
    sell_quote_error: str | None = None


@dataclass(frozen=True, slots=True)
class RunnerFundingObservation:
    """Bounded public-chain funding evidence for one holder/buyer wallet.

    ``trace_complete`` records whether the bounded signature page actually
    reached the wallet's first transaction.  When it did not, ``funder`` and
    ``wallet_age_seconds`` stay unknown instead of being guessed from the
    oldest signature the page happened to contain.
    """

    wallet: str
    funder: str | None = None
    funded_at: int | None = None
    amount_sol: Decimal | None = None
    bought_at: int | None = None
    supply_percent: Decimal | None = None
    first_activity_at: int | None = None
    wallet_age_seconds: int | None = None
    upstream_funder: str | None = None
    funder_depth: int = 0
    trace_complete: bool = False


@dataclass(frozen=True, slots=True)
class RunnerFundingCluster:
    """Wallets linked by an observed public funding relationship.

    This is public-chain coordination evidence only.  It never asserts that the
    wallets belong to one real person or that any offence occurred.
    """

    cluster_id: str
    wallets: tuple[str, ...]
    wallet_count: int
    supply_percent: Decimal | None = None
    funding_interval_seconds: int | None = None
    similar_amounts: bool = False
    time_linked: bool = False
    confidence: str = "LOW"
    cluster_kind: str = "DIRECT_FUNDER"
    buy_interval_seconds: int | None = None
    median_amount_sol: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RunnerForensics:
    """Read-only wallet-cluster evidence; UNKNOWN values are never treated as safe."""

    available: bool = False
    raw_unique_buyers: int = 0
    estimated_independent_clusters: int | None = None
    largest_cluster_size: int | None = None
    largest_cluster_supply_percent: Decimal | None = None
    cluster_adjusted_percent: Decimal | None = None
    shared_funder_groups: tuple[RunnerFundingCluster, ...] = ()
    time_linked_groups: tuple[RunnerFundingCluster, ...] = ()
    observations: tuple[RunnerFundingObservation, ...] = ()
    creator_wallet: str | None = None
    creator_percent: Decimal | None = None
    creator_linked_wallets: tuple[str, ...] = ()
    previous_token_deployments: int | None = None
    previous_severe_collapses: int | None = None
    warnings: tuple[str, ...] = ()
    checked_at: int = 0
    funding_checked_at: int = 0
    dynamic_checked_at: int = 0
    traced_wallets: int = 0
    resolved_funders: int = 0
    fresh_wallet_count: int | None = None
    upstream_traced_wallets: int = 0
    provider_calls: int = 0
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class RunnerDemandProfile:
    """Who is actually buying, as opposed to how many transactions there were.

    ``estimated_independent_buyers`` stays ``None`` when the bounded trace did
    not run.  Unknown independence is never treated as confirmed independence.
    """

    raw_buyers: int = 0
    estimated_independent_buyers: int | None = None
    independence_ratio: Decimal | None = None
    largest_cluster_wallets: int | None = None
    cluster_supply_percent: Decimal | None = None
    fresh_wallet_count: int | None = None
    fresh_wallet_percent: Decimal | None = None
    traced_wallets: int = 0
    time_linked_clusters: int = 0
    time_linked_wallets: int = 0
    upstream_linked_clusters: int = 0
    largest_cluster_id: str | None = None
    raw_smart_wallets: int = 0
    independent_smart_clusters: int = 0
    confidence: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RunnerQualityAssessment:
    """Separated momentum / opportunity / funnel-stage decision at one time.

    Persisted immutably at detection time so calibration never scores a past
    decision with information that only became available later.
    """

    momentum_score: Decimal = Decimal("0")
    opportunity_score: Decimal = Decimal("0")
    organic_score: Decimal = Decimal("0")
    liquidity_quality: Decimal | None = None
    volume_quality: Decimal | None = None
    holder_quality: Decimal | None = None
    price_quality: Decimal | None = None
    stage: str = "RAW_DISCOVERY"
    qualified: bool = False
    evidence: tuple[str, ...] = ()
    evidence_families: tuple[str, ...] = ()
    quality_warnings: tuple[str, ...] = ()
    score_velocity: Decimal | None = None
    liquidity_to_market_cap: Decimal | None = None
    volume_to_liquidity: Decimal | None = None
    volume_to_market_cap: Decimal | None = None
    overextended: bool = False
    coordination_veto: bool = False
    demand: RunnerDemandProfile = field(default_factory=RunnerDemandProfile)
    decision_version: str = "quality-v1"
    evaluated_at: int = 0


@dataclass(frozen=True, slots=True)
class RunnerSafetyAssessment:
    """Separate scam-risk and entry-safety result at one point in time."""

    scam_risk_score: Decimal = Decimal("0")
    scam_risk_level: str = "UNKNOWN"
    status: str = "UNKNOWN"
    entry_eligible: bool = False
    critical_unknowns: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerMarketSnapshot:
    """Immutable market evidence captured at one runner-evaluation time."""

    mint: str
    captured_at: int
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_5m_usd: Decimal = Decimal("0")
    dex_price_change_5m_percent: Decimal | None = None
    buys_5m: int = 0
    sells_5m: int = 0
    holder_count: int | None = None
    verified_unique_buyers: int = 0
    largest_verified_buyer_percent: Decimal | None = None
    smart_wallet_count: int = 0
    top10_percent: Decimal | None = None
    dev_percent: Decimal | None = None
    bundlers_percent: Decimal | None = None
    insiders_percent: Decimal | None = None
    snipers_percent: Decimal | None = None
    risk_score: Decimal | None = None
    rugged: bool = False
    route_available: bool = False
    route_price_impact_percent: Decimal | None = None
    buy_route_status: str = "UNKNOWN"
    sell_route_status: str = "UNKNOWN"
    sell_route_price_impact_percent: Decimal | None = None
    suspicious: bool = False
    mint_authority_disabled: bool | None = None
    freeze_authority_disabled: bool | None = None


@dataclass(frozen=True, slots=True)
class RunnerScoreBreakdown:
    graduation_recency: int = 0
    momentum: int = 0
    acceleration: int = 0
    buy_quality: int = 0
    liquidity: int = 0
    holders: int = 0
    smart_wallets: int = 0
    safety_route: int = 0
    x_social: int = 0
    penalties: int = 0


@dataclass(frozen=True, slots=True)
class RunnerMomentumWindow:
    """Change between two immutable snapshots near one requested lookback."""

    seconds: int
    price_change_percent: Decimal | None = None
    market_cap_change_percent: Decimal | None = None
    rolling_volume_change_percent: Decimal | None = None
    rolling_transactions_change_percent: Decimal | None = None
    holder_growth: int | None = None


@dataclass(frozen=True, slots=True)
class RunnerCandidate:
    """Existing-token research candidate; never a launch or automatic buy order."""

    mint: str
    symbol: str | None
    name: str | None
    first_seen_at: int
    graduated_at: int | None
    graduation_source: str
    first: RunnerMarketSnapshot
    current: RunnerMarketSnapshot
    score: Decimal
    tier: str
    breakdown: RunnerScoreBreakdown
    momentum_windows: tuple[RunnerMomentumWindow, ...] = ()
    smart_wallets: tuple[str, ...] = ()
    earliest_smart_entry_at: int | None = None
    earliest_smart_entry_age_seconds: int | None = None
    top_trader_overlap: int | None = None
    x_evidence: XSocialSnapshot = field(default_factory=lambda: XSocialSnapshot(available=False))
    positives: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    hard_blockers: tuple[str, ...] = ()
    overextended: bool = False
    research_only: bool = True
    pair_url: str = ""
    generated_at: int = 0
    chain_created_at: int | None = None
    pair_created_at: int | None = None
    radar_first_seen_at: int | None = None
    first_market_data_at: int | None = None
    first_research_eligible_at: int | None = None
    first_discord_visible_at: int | None = None
    entry_eligible_at: int | None = None
    strong_alert_at: int | None = None
    score_history: tuple[Decimal, ...] = ()
    state: str = "👀 EARLY RESEARCH"
    safety: RunnerSafetyAssessment = field(default_factory=RunnerSafetyAssessment)
    detection_safety: RunnerSafetyAssessment = field(default_factory=RunnerSafetyAssessment)
    forensics: RunnerForensics = field(default_factory=RunnerForensics)
    detection_forensics: RunnerForensics = field(default_factory=RunnerForensics)
    detection_score: Decimal | None = None
    raw_smart_wallet_count: int = 0
    estimated_independent_smart_wallets: int = 0
    quality: RunnerQualityAssessment = field(default_factory=RunnerQualityAssessment)
    detection_quality: RunnerQualityAssessment = field(default_factory=RunnerQualityAssessment)
    stage: str = "RAW_DISCOVERY"
    best_stage: str = "RAW_DISCOVERY"
    qualified_at: int | None = None
    qualified_market_cap_usd: Decimal | None = None
    heating_at: int | None = None
    why_surfaced: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SwapQuote:
    """A quote-only Jupiter Swap V2 order normalized for paper execution."""

    input_mint: str
    output_mint: str
    input_amount_raw: int
    output_amount_raw: int
    other_amount_threshold_raw: int | None
    input_amount: Decimal
    output_amount: Decimal
    input_usd_value: Decimal | None
    output_usd_value: Decimal | None
    price_impact_percent: Decimal
    router: str
    fee_bps: int
    api_time_ms: int | None
    observed_latency_ms: int
    quoted_at: int


@dataclass(frozen=True, slots=True)
class TraderMetrics:
    address: str
    alias: str
    window_seconds: int
    trades: int
    buys: int
    sells: int
    wins: int
    losses: int
    realized_pnl_usd: Decimal
    matched_cost_usd: Decimal
    volume_usd: Decimal
    max_drawdown_usd: Decimal

    @property
    def win_rate(self) -> Decimal:
        closed = self.wins + self.losses
        return Decimal(self.wins) / Decimal(closed) if closed else Decimal("0")

    @property
    def realized_roi(self) -> Decimal:
        if self.matched_cost_usd <= 0:
            return Decimal("0")
        return self.realized_pnl_usd / self.matched_cost_usd


@dataclass(frozen=True, slots=True)
class ScoredTrader:
    metrics_24h: TraderMetrics
    metrics_7d: TraderMetrics
    score: Decimal


@dataclass(frozen=True, slots=True)
class Signal:
    token_mint: str
    side: Side
    created_at: int
    trader_addresses: tuple[str, ...]
    trader_aliases: tuple[str, ...]
    source_signatures: tuple[str, ...]
    combined_score: Decimal
    reference_price_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    size_usd: Decimal
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    success: bool
    mode: ExecutionMode
    token_mint: str
    side: Side
    size_usd: Decimal
    signature: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class PaperSummary:
    starting_cash_usd: Decimal
    cash_usd: Decimal
    positions_value_usd: Decimal
    equity_usd: Decimal
    realized_pnl_usd: Decimal
    unrealized_pnl_usd: Decimal
    trades: int
    wins: int
    losses: int
    max_drawdown_usd: Decimal
    current_drawdown_usd: Decimal
    realized_pnl_24h_usd: Decimal
    gross_profit_usd: Decimal
    gross_loss_usd: Decimal
    average_win_usd: Decimal
    average_loss_usd: Decimal
    expectancy_usd: Decimal
    profit_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class PaperDailyLockStatus:
    enabled: bool
    day: str
    target_usd: Decimal
    loss_limit_usd: Decimal
    baseline_equity_usd: Decimal
    current_equity_usd: Decimal
    marked_pnl_usd: Decimal
    locked: bool
    triggered_at: int | None
    open_positions: int
    lock_reason: str | None


@dataclass(frozen=True, slots=True)
class PaperReadiness:
    trial_started_at: int
    active_days: int
    quote_attempts: int
    quote_successes: int
    quote_success_percent: Decimal
    accepted_entries: int
    closed_trades: int
    gross_profit_usd: Decimal
    gross_loss_usd: Decimal
    expectancy_usd: Decimal
    profit_factor: Decimal | None
    max_drawdown_usd: Decimal
    max_drawdown_percent: Decimal
    ready: bool
    blockers: tuple[str, ...]
