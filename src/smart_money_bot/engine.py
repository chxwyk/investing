from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from .callouts import (
    CoinCalloutAnalyzer,
    DexScreenerClient,
    SolanaTrackerTokenRiskClient,
    XRecentSearchClient,
    should_publish_coin_callout,
    should_publish_coin_watch,
    should_publish_fomo_watch,
)
from .config import Settings
from .constants import (
    BOT_VERSION,
    INFRASTRUCTURE_ADDRESSES,
    PAPER_DEMO_ENTRY_PRICE_USD,
    PAPER_DEMO_MINT,
    fomo_coin_url,
)
from .database import Database
from .detector import SwapDetector
from .discovery import (
    DiscoveryPolicy,
    SolanaTrackerClient,
    WindowCandidate,
    merge_verified_windows,
)
from .errors import (
    DiscoveryError,
    JupiterError,
    PumpLaunchError,
    RpcError,
    UnknownLaunchResultError,
)
from .executor import ExecutionManager
from .fast_alerts import (
    ALMOST_BONDED_ALERT,
    PUBLIC_TRENDING_ALERT,
    TRENCH_RUNNER_ALERT,
    TRENDING_ACCELERATION_ALERT,
    TRENDING_ALPHA,
    TRENDING_CONTINUATION_ALERT,
    EnrichmentUpdate,
    FastAlert,
    build_catalyst_alert,
    build_early_alert,
    build_fast_watch_alert,
    build_notable_trader_alert,
    build_promotion_alert,
    build_public_trending_alert,
    build_shadow_entry_alert,
    build_shadow_exit_alert,
    build_trench_runner_alert,
    build_trending_alert,
    build_trending_hot_watch_card,
    enrichment_from_evidence,
)
from .lab.actionability import RankedCandidate as LabRankedCandidate
from .lab.actionability import rank_by_current_edge
from .lab.catalyst import (
    CatalystAlert,
    CatalystEvent,
    ConfluenceInputs,
    EventSource,
    TokenEventLink,
    assess_event,
    assess_token_link,
    classify_catalyst_alert,
)
from .lab.config import lab_config_from_settings
from .lab.early import (
    EDGE_AVAILABLE as EARLY_EDGE_AVAILABLE,
)
from .lab.early import (
    HUMAN_WHY as HUMAN_EARLY_WHY,
)
from .lab.early import (
    PINGABLE_TIERS as EARLY_PINGABLE_TIERS,
)
from .lab.early import (
    STAGE_BOT_FIRST_SEEN,
    STAGE_CHEAP_SIGNAL,
    STAGE_EARLY_RUNNER,
    STAGE_OPERATOR_HEADS_UP,
    STAGE_URGENT_PING,
    AlertTiming,
    EarlySignals,
    early_config_from_settings,
    evaluate_early_signal,
    summarize_alert_performance,
)
from .lab.early import (
    WHY_DUPLICATE as EARLY_WHY_DUPLICATE,
)
from .lab.early import (
    WHY_NO_DATA as EARLY_WHY_NO_DATA,
)
from .lab.exits import (
    EXIT_LIQUIDITY_DETERIORATION,
    EXIT_LIQUIDITY_EMERGENCY,
    EXIT_SAFETY_EMERGENCY,
)
from .lab.exits import ExitContext as LabExitContext
from .lab.fastwatch import evaluate_fast_watch, signals_from_candidate, still_current
from .lab.forward import EdgeInputs as ForwardEdgeInputs
from .lab.forward import PingVerdict, forward_edge_score, should_ping
from .lab.latency import HISTORICAL as LAB_HISTORICAL
from .lab.latency import UNKNOWN as LAB_UNKNOWN
from .lab.latency import LatencySample as LabLatencySample
from .lab.latency import pipeline_breakdown as lab_pipeline_breakdown
from .lab.latency import slowest_stage as lab_slowest_stage
from .lab.latency import summarize_sources as summarize_lab_sources
from .lab.notable import (
    ADMIN_DEFINED,
    ONCHAIN_ONLY,
    PROVENANCE,
    NotableConsensus,
    NotableSignal,
    NotableTrade,
    NotableWallet,
    build_consensus,
    decide_ping,
)
from .lab.promotion import (
    EarlyWatchEntry,
    PromotionEvidence,
    early_watch_config_from_settings,
    open_early_watch,
    should_open_watch,
)
from .lab.promotion import entry_from_json as early_watch_from_json
from .lab.promotion import evaluate_promotion as evaluate_early_promotion
from .lab.promotion import prune as prune_early_watches
from .lab.promotion import summarise as summarise_early_watches
from .lab.providers import (
    PROVIDER_FEATURES,
    ProviderState,
    build_provider_report,
)
from .lab.providers import backoff_seconds as provider_backoff_seconds
from .lab.providers import cost_per_signals as provider_cost_per_signals
from .lab.regime import RegimeSample as LabRegimeSample
from .lab.registry import account_tier
from .lab.shadow import (
    FAMILY_BREAKING_CATALYST,
    FAMILY_CATALYST_WATCH,
    FAMILY_CONFLUENCE_WATCH,
    FAMILY_FAST_WATCH,
    FAMILY_FRESH_RUNNER,
    FAMILY_LABELS,
    FAMILY_NOTABLE_EARLY,
    FAMILY_NOTABLE_LATE,
    FAMILY_PUBLIC_TRENDING,
    FAMILY_QUALIFIED_RESEARCH,
    FAMILY_STRICT_PAPER,
    FAMILY_TRENCH_ALMOST_BONDED,
    FAMILY_TRENCH_RUNNER,
    SHADOW_STRATEGY_VERSION,
    ShadowSignal,
    ShadowTimestamps,
    shadow_config_from_settings,
    why_you_are_seeing_this,
)
from .lab.shadow_exits import RunnerEvidence as ShadowRunnerEvidence
from .lab.smartmoney import WalletReputation
from .lab.toptraders import (
    MIN_PROVEN_SAMPLES,
    TraderConfirmation,
    TraderFill,
    build_positions,
    independent_confirmations,
    join_known_traders,
    known_money_flow,
)
from .lab.venues import classify_graduation
from .lab_runtime import QUALIFIED_STAGES as LAB_QUALIFIED_STAGES
from .lab_runtime import LabEvaluation, LabRuntime
from .lab_store import LabStore
from .launch import (
    OneClickLaunchClient,
    alert_key,
    default_launch_draft,
    is_launch_lab_eligible,
    is_manual_launch_opportunity,
    launch_cluster_key,
    launch_draft_key,
    launch_opportunity_from_json,
    launch_opportunity_to_json,
    score_launch_opportunity,
    should_publish_news_opportunity,
    should_request_x_for_launch_opportunity,
    validate_launch_draft,
)
from .market import JupiterClient
from .models import (
    CoinCallout,
    DetectedSwap,
    DiscoveryCandidate,
    DiscoveryRefresh,
    ExecutionMode,
    ExecutionResult,
    LaunchDraft,
    LaunchOpportunity,
    NarrativePairMatch,
    NewsAlert,
    PaperDailyLockStatus,
    PaperReadiness,
    PaperSummary,
    PumpLaunchResult,
    RiskDecision,
    RunnerCandidate,
    RunnerForensics,
    RunnerFundingObservation,
    RunnerMarketSnapshot,
    ScoredTrader,
    Side,
    Signal,
    TokenInfo,
    TrackedTrader,
    TraderMetrics,
)
from .news import (
    DexNarrativeMatcher,
    RssNewsPoller,
    XFilteredNewsStream,
    is_coin_actionable_news,
)
from .pump_chain import PumpChainReader
from .pump_stream import PumpCreation, PumpCreationStream
from .quality import (
    STAGE_ENTRY,
    STAGE_HEATING,
    STAGE_QUALIFIED,
    STAGE_RAW,
    STAGE_STRONG,
    STAGE_UNSAFE,
    USER_FACING_STAGES,
    merge_best_stage,
    quality_config_from_settings,
    rank_for_attention,
)
from .risk import RiskEngine
from .rotation import CandidateRotator, RotationResult, is_pump_mint
from .rpc import SolanaRPC
from .runner import (
    RUNNER_HORIZONS_SECONDS,
    forward_return_percent,
    fresh_watch_schedule,
    funding_observation_from_transaction,
    is_fresh_research_worthy,
    runner_candidate_from_json,
    runner_candidate_to_json,
    runner_forensics_from_json,
    runner_forensics_to_json,
    runner_path_metrics,
    runner_snapshot_from_callout,
    runner_snapshot_from_json,
    runner_snapshot_to_json,
    score_runner_candidate,
    summarize_forensics,
)
from .scoring import rank_traders
from .shadow_runtime import ShadowRuntime
from .shadow_store import ShadowStore
from .social import (
    PumpProfileDiscovery,
    SocialNomination,
    annotate_social_nominations,
)
from .strategy import ConsensusStrategy
from .stream import RealtimeWalletStream, StreamEvent, StreamHealth
from .token_identity import (
    assert_exact_propagation,
    detect_symbol_collision,
    from_symbol_search,
)
from .token_identity import (
    exact as exact_identity,
)
from .trenches import (
    CadenceConfig,
    TokenMetadata,
    detect_reuse,
)
from .trenches.holders import HolderSnapshot as TrenchHolderSnapshot
from .trenches.holders import assess_concentration_trend
from .trenches.lifecycle import STAGE_ALMOST_BONDED, STAGE_GRADUATING
from .trenches.participants import BuyerRecord, detect_clusters
from .trenches_runtime import (
    SOURCE_CREATION_STREAM,
    TrenchCandidate,
    TrenchesRuntime,
)
from .trenches_store import TrenchesStore
from .trending import (
    TRENDING_CONTINUATION,
    TRENDING_EXPERIMENT_VERSION,
    TRENDING_FAMILY_LABELS,
    TRENDING_NEW_ENTRY,
    TRENDING_REENTRY,
    TRENDING_STRATEGY_VERSION,
    HotWatchConfig,
    TrendingEventConfig,
    TrendingShadowConfig,
    UniverseTrade,
    build_risk_panel,
    family_for_reasons,
    source_from_settings,
)
from .trending.holders import HolderSample, HolderSeries
from .trending_runtime import TrendingCandidate, TrendingRuntime
from .trending_source import build_trending_client
from .trending_store import TrendingStore
from .x_budget import XBudgetManager

logger = logging.getLogger(__name__)

#: Stages that earn their own Discord message instead of a digest row.
ALERT_STAGES = frozenset({STAGE_HEATING, STAGE_UNSAFE, STAGE_ENTRY, STAGE_STRONG})
#: Stages that may be described as entry quality. Never reachable while safety
#: is UNKNOWN or FAIL — that gate lives in ``assess_runner_safety``.
ENTRY_QUALITY_STAGES = frozenset({STAGE_ENTRY, STAGE_STRONG})


def _launch_failure_status(message: str) -> str:
    upper = message.upper()
    for status in (
        "J7 SESSION EXPIRED",
        "J7 AUTH FAILED",
        "J7 RATE LIMITED",
        "PINATA UPLOAD FAILED",
        "INSUFFICIENT SOL",
        "NETWORK FAILURE",
        "J7 RESPONSE MISSING MINT",
        "UNKNOWN SUBMISSION STATE",
    ):
        if status in upper:
            return status.replace(" ", "_").replace("/", "_")
    return "FAILED"


class Notifier(Protocol):
    async def on_discovery(self, refresh: DiscoveryRefresh) -> None: ...

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None: ...

    async def on_signal(
        self, signal: Signal, token_info: TokenInfo | None, decision: RiskDecision
    ) -> None: ...

    async def on_execution(self, result: ExecutionResult) -> None: ...

    async def on_coin_callout(self, callout: CoinCallout) -> None: ...

    async def on_coin_watch(self, callout: CoinCallout) -> None: ...

    async def on_fomo_watch(self, callout: CoinCallout) -> None: ...

    async def on_runner_alert(self, candidate: RunnerCandidate) -> bool: ...

    async def on_runner_fresh(self, candidate: RunnerCandidate) -> bool: ...

    async def on_fast_alert(self, alert: FastAlert) -> bool: ...

    async def on_fast_alert_enrichment(
        self, alert: FastAlert, update: EnrichmentUpdate
    ) -> bool: ...

    async def on_runner_risk_escalation(
        self, candidate: RunnerCandidate, changes: tuple[str, ...]
    ) -> bool: ...

    async def on_runner_invalidated(
        self,
        candidate: RunnerCandidate,
        metrics: dict[str, object],
        reasons: tuple[str, ...],
    ) -> bool: ...

    async def on_runner_digest(
        self,
        candidates: tuple[RunnerCandidate, ...],
        public_floor: Decimal,
    ) -> None: ...

    async def on_news_alert(
        self,
        alert: NewsAlert,
        opportunity: LaunchOpportunity,
    ) -> None: ...

    async def on_narrative_match(self, alert: NewsAlert, match: NarrativePairMatch) -> None: ...

    async def on_daily_profit_lock(self, status: PaperDailyLockStatus) -> None: ...

    async def on_error(self, context: str, error: Exception) -> None: ...


class NullNotifier:
    async def on_discovery(self, refresh: DiscoveryRefresh) -> None:
        return None

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        return None

    async def on_signal(
        self, signal: Signal, token_info: TokenInfo | None, decision: RiskDecision
    ) -> None:
        return None

    async def on_execution(self, result: ExecutionResult) -> None:
        return None

    async def on_coin_callout(self, callout: CoinCallout) -> None:
        return None

    async def on_coin_watch(self, callout: CoinCallout) -> None:
        return None

    async def on_fomo_watch(self, callout: CoinCallout) -> None:
        return None

    async def on_runner_alert(self, candidate: RunnerCandidate) -> bool:
        return False

    async def on_runner_fresh(self, candidate: RunnerCandidate) -> bool:
        return False

    async def on_fast_alert(self, alert: FastAlert) -> bool:
        return False

    async def on_fast_alert_enrichment(
        self, alert: FastAlert, update: EnrichmentUpdate
    ) -> bool:
        return False

    async def on_runner_risk_escalation(
        self, candidate: RunnerCandidate, changes: tuple[str, ...]
    ) -> bool:
        return False

    async def on_runner_invalidated(
        self,
        candidate: RunnerCandidate,
        metrics: dict[str, object],
        reasons: tuple[str, ...],
    ) -> bool:
        return False

    async def on_runner_digest(
        self,
        candidates: tuple[RunnerCandidate, ...],
        public_floor: Decimal,
    ) -> None:
        return None

    async def on_news_alert(
        self,
        alert: NewsAlert,
        opportunity: LaunchOpportunity,
    ) -> None:
        return None

    async def on_narrative_match(self, alert: NewsAlert, match: NarrativePairMatch) -> None:
        return None

    async def on_daily_profit_lock(self, status: PaperDailyLockStatus) -> None:
        return None

    async def on_error(self, context: str, error: Exception) -> None:
        logger.error("%s: %s", context, error)


class SmartMoneyEngine:
    def __init__(self, settings: Settings, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.notifier: Notifier = notifier or NullNotifier()
        self.database = Database(settings.database_path, settings.paper_starting_usd)
        self.x_budget = XBudgetManager(self.database, settings)
        self.rpc = SolanaRPC(
            settings.solana_rpc_url,
            max_requests_per_second=settings.rpc_requests_per_second,
            max_retries=settings.rpc_max_retries,
        )
        self.market = JupiterClient(settings.jupiter_api_key)
        self.dex_screener = DexScreenerClient()
        self.x_social = XRecentSearchClient(
            settings.x_api_bearer_token,
            max_results=settings.x_verify_max_posts,
            cache_seconds=settings.news_x_trend_cache_seconds,
            trusted_crypto_accounts=settings.x_crypto_trusted_accounts,
            budget_manager=self.x_budget,
            paid_search_enabled=settings.x_paid_search_enabled,
        )
        self.tracker_token_risk = SolanaTrackerTokenRiskClient(settings.solana_tracker_api_key)
        self.callout_analyzer = CoinCalloutAnalyzer(
            self.dex_screener,
            self.x_social,
            self.tracker_token_risk,
            self.market,
            prefilter_min_score=settings.coin_x_prefilter_min_score,
        )
        self.x_news_stream = XFilteredNewsStream(
            settings.x_api_bearer_token,
            settings.x_news_stream_rule,
        )
        news_feeds = settings.news_rss_feeds + (
            (settings.j7_authorized_feed_url,) if settings.j7_authorized_feed_url else ()
        )
        self.news_poller = RssNewsPoller(
            news_feeds,
            poll_seconds=settings.news_poll_seconds,
        )
        self.news_matcher = DexNarrativeMatcher(
            min_liquidity_usd=settings.news_dex_match_min_liquidity_usd,
            max_age_minutes=settings.news_dex_match_max_age_minutes,
        )
        self.pump_launcher = OneClickLaunchClient(settings, self.rpc)
        self.discovery = (
            SolanaTrackerClient(settings.solana_tracker_api_key)
            if settings.solana_tracker_api_key
            else None
        )
        self.discovery_policy = DiscoveryPolicy.from_settings(settings)
        self.profile_discovery = (
            PumpProfileDiscovery() if settings.pump_profile_discovery_enabled else None
        )
        self.detector = SwapDetector(self.market, settings.min_source_trade_usd)
        self.rotator = CandidateRotator(settings, self.rpc, self.detector)
        self.stream = RealtimeWalletStream(
            self.database,
            rpc_url=settings.solana_rpc_url,
            explicit_ws_url=settings.solana_ws_url,
            enabled=settings.realtime_wallet_stream_enabled,
            commitment=settings.realtime_stream_commitment,
            on_health_warning=self._warn_wallet_stream,
        )
        # --- the primary Trending universe (v2.42) -------------------------
        # Provenance is resolved from configuration alone.  With no authorised
        # feed configured this is a TRENDING_PROXY and every surface says so.
        self.trending_source = source_from_settings(
            api_url=settings.fomo_trending_api_url,
            api_key=settings.fomo_trending_api_key,
            proxy_enabled=settings.fomo_trending_proxy_enabled,
            change_window=settings.fomo_trending_change_window,
        )
        self.trending_store = TrendingStore(self.database)
        self.trending_client = build_trending_client(
            self.trending_source,
            api_url=settings.fomo_trending_api_url,
            api_key=settings.fomo_trending_api_key,
            referral_code=settings.fomo_referral_code,
        )
        # --- Terminal-style trenches intelligence (v2.43) ------------------
        # Built on public Solana RPC and Pump.fun program state, so it keeps
        # working when every third-party vendor is degraded (section 4).
        self.pump_chain = PumpChainReader(self.rpc)
        self.trenches_store = TrenchesStore(self.database)
        self.pump_creation_stream = PumpCreationStream(
            rpc_url=settings.solana_rpc_url,
            explicit_ws_url=settings.solana_ws_url,
            enabled=settings.fomo_pump_creation_stream_enabled,
            commitment=settings.realtime_stream_commitment,
            on_creation=self._handle_pump_creation,
        )
        self.trenches = TrenchesRuntime(
            self.trenches_store,
            self.pump_chain,
            enabled=settings.fomo_trenches_enabled,
            max_tracked=settings.fomo_trenches_max_tracked,
            runner_threshold=settings.fomo_trenches_runner_min_score,
            heads_up_threshold=settings.fomo_trenches_heads_up_min_score,
            max_alerts_per_hour=settings.fomo_trenches_max_alerts_per_hour,
            cooldown_seconds=settings.fomo_trenches_cooldown_seconds,
            cadence_config=CadenceConfig(
                hot_seconds=settings.fomo_trenches_hot_recheck_seconds,
                warm_seconds=settings.fomo_trenches_warm_recheck_seconds,
                normal_seconds=settings.fomo_trenches_normal_recheck_seconds,
                max_hot=settings.fomo_trenches_max_hot,
                max_warm=settings.fomo_trenches_max_warm,
            ),
            max_enrichment_per_scan=settings.fomo_trenches_max_enrichment_per_scan,
            wallet_lookups_per_token=settings.fomo_trenches_wallet_lookups_per_token,
            holder_reads_per_scan=settings.fomo_trenches_holder_reads_per_scan,
            public_model_enabled=settings.fomo_public_trending_enabled,
            public_model_min_score=settings.fomo_public_trending_min_score,
            enrich=self._enrich_trench,
            publish=self._publish_trench,
        )
        self.trending = TrendingRuntime(
            self.trending_store,
            self.trending_client,
            max_tracked=settings.fomo_trending_max_tracked,
            alpha_threshold=settings.fomo_trending_alpha_min_score,
            watch_threshold=settings.fomo_trending_watch_min_score,
            hot_watch_config=HotWatchConfig(
                ttl_seconds=settings.fomo_trending_hot_watch_seconds,
                recheck_seconds=settings.fomo_trending_hot_watch_recheck_seconds,
                max_entries=settings.fomo_trending_hot_watch_max,
                near_miss_band=settings.fomo_trending_hot_watch_band,
            ),
            hot_watch_enabled=settings.fomo_trending_hot_watch_enabled,
            event_config=TrendingEventConfig(),
            shadow_config=(
                TrendingShadowConfig() if settings.fomo_trending_shadow_enabled else None
            ),
            max_alerts_per_hour=settings.fomo_trending_max_alerts_per_hour,
            cooldown_seconds=settings.fomo_trending_cooldown_seconds,
            stale_snapshot_seconds=settings.fomo_trending_stale_snapshot_seconds,
            enabled=settings.fomo_trending_primary_enabled,
            enrich=self._enrich_trending,
            publish=self._publish_trending,
        )
        self.strategy = ConsensusStrategy(
            self.database,
            minimum_traders=settings.consensus_min_traders,
            window_seconds=settings.consensus_window_seconds,
            cooldown_seconds=settings.signal_cooldown_seconds,
            minimum_trader_score=settings.min_trader_score,
        )
        self.risk = RiskEngine(settings, self.database)
        self.executor = ExecutionManager(settings, self.database, self.market)
        self._task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._trending_task: asyncio.Task[None] | None = None
        self._trending_hot_watch_task: asyncio.Task[None] | None = None
        self._trenches_task: asyncio.Task[None] | None = None
        # --- early-candidate hot watch (v2.44, sections 2, 3, 29) -------
        self._early_watch_task: asyncio.Task[None] | None = None
        self._early_watches: dict[str, EarlyWatchEntry] = {}
        self._holder_series: dict[str, HolderSeries] = {}
        self._early_watch_config = early_watch_config_from_settings(settings)
        self.early_watches_opened = 0
        self.early_promotions = 0
        self.early_watch_event_rechecks = 0
        self._pump_creation_task: asyncio.Task[None] | None = None
        self._pump_creation_consumer_task: asyncio.Task[None] | None = None
        self.trending_hot_watch_cards = 0
        self._stream_consumer_task: asyncio.Task[None] | None = None
        self._daily_profit_task: asyncio.Task[None] | None = None
        self._callout_tasks: set[asyncio.Task[None]] = set()
        self._news_stream_task: asyncio.Task[None] | None = None
        self._news_rss_task: asyncio.Task[None] | None = None
        self._x_radar_task: asyncio.Task[None] | None = None
        self._fomo_radar_task: asyncio.Task[None] | None = None
        self._runner_outcome_task: asyncio.Task[None] | None = None
        self._runner_digest_task: asyncio.Task[None] | None = None
        self._runner_fast_watch_tasks: dict[str, asyncio.Task[None]] = {}
        self._news_match_tasks: set[asyncio.Task[None]] = set()
        self._news_alert_times: deque[int] = deque()
        self._recent_news_events: deque[tuple[int, str, frozenset[str]]] = deque()
        self._narrative_matches_seen: set[str] = set()
        self._last_callout_state: dict[str, tuple[int, int]] = {}
        self._recent_coin_scans: deque[CoinCallout] = deque(maxlen=12)
        self._coin_scan_counts = {
            "total": 0,
            "free_rejected": 0,
            "free_checked": 0,
            "x_checked": 0,
            "x_unavailable": 0,
            "watch": 0,
            "verified": 0,
            "fomo_watch": 0,
        }
        self._fomo_radar_seen: dict[str, int] = {}
        self._runner_last_alert: dict[str, tuple[int, Decimal]] = {}
        # --- realtime alpha engine (v2.38) state -------------------------
        self._fast_watch_published: dict[str, int] = {}
        self._fast_watch_times: deque[int] = deque()
        self._fast_alerts: dict[str, FastAlert] = {}
        self._enrichment_tasks: set[asyncio.Task[None]] = set()
        self._notable_tasks: set[asyncio.Task[None]] = set()
        self._catalyst_sources: dict[str, list[EventSource]] = {}
        self._catalyst_headlines: dict[str, tuple[int, str]] = {}
        self._notable_recent: dict[str, list[NotableSignal]] = {}
        self._notable_anonymous_index: dict[str, int] = {}
        self.fast_alerts_published = 0
        self.fast_alerts_suppressed = 0
        self.last_fast_alert_at: int | None = None
        self.last_fast_alert_kind: str = ""
        self._quality_config = quality_config_from_settings(settings)
        self._lab_config = lab_config_from_settings(settings)
        self.lab_store = LabStore(self.database)
        # The lab is a research laboratory, not an execution path: it holds no
        # signer, no wallet and no live route, so enabling it can only ever
        # write simulated rows.
        self.lab = LabRuntime(
            self.lab_store,
            config=self._lab_config,
            enabled=settings.fomo_lab_auto_paper_enabled,
        )
        self.lab_enabled = settings.fomo_lab_engine_enabled
        self._lab_regime_refreshed_at = 0
        # --- SHADOW auto-trader (v2.39) ----------------------------------
        # A second, independent strategy family.  It shares the runner's
        # evidence but never its bankroll, its positions or its eligibility, and
        # like the lab it holds no signer, no wallet and no live route.
        self._shadow_config = shadow_config_from_settings(settings)
        self.shadow_store = ShadowStore(self.database)
        self.shadow = ShadowRuntime(
            self.shadow_store,
            config=self._shadow_config,
            enabled=settings.fomo_shadow_auto_enabled,
        )
        self.shadow_enabled = settings.fomo_shadow_auto_enabled
        # --- the second, isolated forward experiment (sections 62, 63) -----
        # Identical shape to legacy — $100 bankroll, $10 entries, 5 positions,
        # $50 exposure — so the strategy is the only variable.  Isolation is
        # structural: the store keys bankrolls by ``strategy_version`` and open
        # positions by ``(mint, family, strategy_version)``, so these two books
        # cannot share a row even if the same mint appears in both.  The legacy
        # experiment's config, version and entire forward history are untouched.
        self._trending_shadow_config = replace(
            self._shadow_config,
            strategy_version=TRENDING_STRATEGY_VERSION,
            bankroll_usd=Decimal("100"),
            position_usd=Decimal("10"),
            min_position_usd=Decimal("10"),
            max_position_usd=Decimal("10"),
            max_token_exposure_usd=Decimal("10"),
            max_concurrent_positions=5,
            max_total_exposure_usd=Decimal("50"),
            enabled=settings.fomo_trending_shadow_enabled,
        )
        self.trending_shadow = ShadowRuntime(
            self.shadow_store,
            config=self._trending_shadow_config,
            enabled=settings.fomo_trending_shadow_enabled,
            experiment_version=TRENDING_EXPERIMENT_VERSION,
        )
        self.trending_shadow_enabled = settings.fomo_trending_shadow_enabled
        # The experiment can run without publishing its own cards, so an
        # operator can quieten the feed without losing the forward sample.
        self.shadow_cards_enabled = settings.fomo_shadow_publish_cards
        self._shadow_sweep_at = 0
        self.runner_last_evaluated_at: int | None = None
        self.runner_last_candidate_mint: str | None = None
        self.runner_last_fast_watch_mint: str | None = None
        self.runner_last_fast_watch_at: int | None = None
        self._scan_lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()
        self._daily_profit_lock = asyncio.Lock()
        self._launch_lab_lock = asyncio.Lock()
        self._processing_signatures: set[str] = set()
        self._initialized = False
        self.last_scan_started_at: int | None = None
        self.last_scan_finished_at: int | None = None
        self.last_error: str | None = None
        self.last_discovery_refresh_at: int | None = None
        self.last_weekly_refresh_at: int | None = None
        self.last_profile_refresh_at: int | None = None
        self.profile_discovery_last_error: str | None = None
        self.profile_verified_matches = 0
        self.last_rotation_at: int | None = None
        self.last_rotation_result: RotationResult | None = None
        self._weekly_pool: list[WindowCandidate] = []
        self._candidate_pool = []
        self._social_nominations: list[SocialNomination] = []
        # Time of the last discovery *attempt*, successful or not.  The refresh
        # budget is spent by attempts, not by successes, so this — not the last
        # success — is what the throttle has to measure.
        self.last_discovery_attempt_at: int | None = None
        self._discovery_retry_at = 0
        self._discovery_consecutive_failures = 0
        # A stalled enrichment pass starves every downstream lane, so it is
        # counted rather than only logged.
        self.runner_analysis_timeouts = 0
        self.runner_analysis_errors = 0
        self.forward_pings_withheld = 0
        # --- ultra-early operator lane (v2.41) ---------------------------
        # Cheap, synchronous-ish, and deliberately ahead of deep enrichment.
        self._early_config = early_config_from_settings(settings)
        self._early_published: dict[str, int] = {}
        self._early_runner_times: deque[int] = deque()
        self.early_heads_up_published = 0
        self.early_runners_published = 0
        self.last_early_alert_at: int | None = None
        self.last_early_alert_mint: str = ""

    def _x_usage_day(self) -> str:
        return datetime.now(ZoneInfo(self.settings.x_daily_search_timezone)).date().isoformat()

    async def _reserve_x_search(self) -> bool:
        allowed, _count = await self.database.reserve_daily_api_request(
            provider="x",
            operation="recent_search",
            usage_day=self._x_usage_day(),
            request_limit=self.settings.x_daily_search_limit,
        )
        return allowed

    async def x_search_usage_today(self) -> int:
        return await self.database.daily_api_request_count(
            provider="x",
            operation="recent_search",
            usage_day=self._x_usage_day(),
        )

    def _record_coin_scan(self, callout: CoinCallout) -> None:
        self._recent_coin_scans.appendleft(callout)
        self._coin_scan_counts["total"] += 1
        if callout.scan_stage == "FREE_REJECTED":
            self._coin_scan_counts["free_rejected"] += 1
        elif callout.scan_stage == "FREE_CHECKED":
            self._coin_scan_counts["free_checked"] += 1
        elif callout.scan_stage == "X_CHECKED":
            self._coin_scan_counts["x_checked"] += 1
        elif callout.scan_stage == "X_UNAVAILABLE":
            self._coin_scan_counts["x_unavailable"] += 1
        if callout.public_alert_eligible:
            self._coin_scan_counts["verified"] += 1

    def recent_coin_scans(self) -> tuple[CoinCallout, ...]:
        return tuple(self._recent_coin_scans)

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.database.connect()
        if self.settings.discord_alert_channel_id:
            await self.database.set_setting(
                "alert_channel_id", str(self.settings.discord_alert_channel_id)
            )
        raw_refresh = await self.database.get_setting("discovery_last_refresh")
        self.last_discovery_refresh_at = int(raw_refresh) if raw_refresh else None
        # A restart must not reset the spend clock, or a crash loop would
        # re-open the budget on every boot.
        raw_attempt = await self.database.get_setting("discovery_last_attempt")
        self.last_discovery_attempt_at = (
            int(raw_attempt) if raw_attempt else self.last_discovery_refresh_at
        )
        raw_weekly = await self.database.get_setting("discovery_7d_last_refresh")
        self.last_weekly_refresh_at = int(raw_weekly) if raw_weekly else None
        raw_rotation = await self.database.get_setting("rotation_last_refresh")
        self.last_rotation_at = int(raw_rotation) if raw_rotation else None
        raw_profiles = await self.database.get_setting("pump_profile_last_refresh")
        self.last_profile_refresh_at = int(raw_profiles) if raw_profiles else None
        self._candidate_pool = await self.database.load_discovery_candidates()
        if self.lab_enabled:
            await self.lab_store.register_strategy(
                strategy_version=self._lab_config.strategy_version,
                role="CHAMPION",
                config_hash=self._lab_config.config_hash(),
            )
        if self.shadow_enabled:
            # Written once and never rewritten: this timestamp is what makes the
            # forward experiment enforceable (sections 41, 42).
            with suppress(Exception):
                await self.shadow.start_experiment()
        self._initialized = True

    async def start(self) -> None:
        await self.initialize()
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="smart-money-monitor")
        if (
            self.settings.paper_daily_profit_lock_enabled
            or self.settings.paper_daily_loss_lock_enabled
        ):
            self._daily_profit_task = asyncio.create_task(
                self._run_daily_profit_guard(), name="smart-money-daily-risk-guard"
            )
        # The supervisor runs even when the lane cannot connect.  Ending the task
        # for a disabled or URL-less lane is what produced the production
        # "DISCONNECTED / 0 subscriptions / 0 reconnects" with no way to tell a
        # switched-off lane from a broken one (section 52).
        self._stream_task = asyncio.create_task(
            self.stream.run(), name="smart-money-wallet-stream"
        )
        self._stream_consumer_task = asyncio.create_task(
            self._consume_stream_events(), name="smart-money-stream-consumer"
        )
        if self.settings.news_radar_enabled:
            if (
                self.settings.x_paid_search_enabled
                and self.settings.x_news_stream_enabled
                and self.x_news_stream.configured
            ):
                self._news_stream_task = asyncio.create_task(
                    self.x_news_stream.run(self._handle_news_alert),
                    name="smart-money-x-news-stream",
                )
            if self.news_poller.configured:
                self._news_rss_task = asyncio.create_task(
                    self.news_poller.run(self._handle_news_alert),
                    name="smart-money-news-rss",
                )
        if (
            self.settings.x_radar_enabled
            and self.settings.coin_callouts_enabled
            and self.x_social.search_enabled
        ):
            self._x_radar_task = asyncio.create_task(
                self._run_x_radar(),
                name="smart-money-x-radar",
            )
        # Pump.fun trenches: realtime creation detection plus a polling safety
        # net.  The stream is what makes first-observation sub-second; the poll
        # exists so a dropped socket degrades latency rather than coverage.
        if self.settings.fomo_trenches_enabled:
            self._trenches_task = asyncio.create_task(
                self._run_trenches(), name="smart-money-pump-trenches"
            )
            self._pump_creation_task = asyncio.create_task(
                self.pump_creation_stream.run(), name="smart-money-pump-creations"
            )
            self._pump_creation_consumer_task = asyncio.create_task(
                self._consume_pump_creations(), name="smart-money-pump-creation-intake"
            )
        # Trending is the PRIMARY discovery universe (section 39).  It gets its
        # own lightweight loop rather than sharing the graduated radar's 60s
        # poll and 1800s recheck, because a new Trending entrant is only
        # interesting for minutes (sections 74, 77).
        if self.settings.fomo_trending_primary_enabled:
            self._trending_task = asyncio.create_task(
                self._run_trending_radar(),
                name="smart-money-trending-radar",
            )
            if self.settings.fomo_trending_hot_watch_enabled:
                self._trending_hot_watch_task = asyncio.create_task(
                    self._run_trending_hot_watch(),
                    name="smart-money-trending-hot-watch",
                )
        # The early lane's own second look.  It is independent of the Trending
        # board on purpose: the candidate that produced the section 1 failure
        # never appeared on a board at all, so a board-scoped watch could never
        # have caught it.
        if self.settings.fomo_early_watch_enabled:
            await self._restore_early_watches()
            self._early_watch_task = asyncio.create_task(
                self._run_early_watch(),
                name="smart-money-early-watch",
            )
        # Graduated discovery is retained as the SECONDARY universe.  It is
        # demoted, never deleted.
        if (
            self.settings.fomo_radar_enabled
            and self.settings.fomo_graduated_secondary_enabled
            and (self.settings.coin_callouts_enabled or self.settings.fomo_runner_enabled)
        ):
            self._fomo_radar_task = asyncio.create_task(
                self._run_fomo_radar(),
                name="smart-money-fomo-radar",
            )
        if self.settings.fomo_runner_enabled:
            self._runner_outcome_task = asyncio.create_task(
                self._run_runner_outcomes(),
                name="smart-money-runner-outcomes",
            )
            if self.settings.fomo_runner_digest_enabled:
                self._runner_digest_task = asyncio.create_task(
                    self._run_runner_digest(),
                    name="smart-money-runner-digest",
                )

    async def close(self) -> None:
        for pending in (self._news_match_tasks, self._notable_tasks, self._enrichment_tasks):
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            pending.clear()
        background = (
            self._news_stream_task,
            self._news_rss_task,
            self._x_radar_task,
            self._fomo_radar_task,
            self._runner_outcome_task,
            self._runner_digest_task,
            self._trending_task,
            self._trending_hot_watch_task,
            self._early_watch_task,
            self._trenches_task,
            self._pump_creation_task,
            self._pump_creation_consumer_task,
        )
        for task in background:
            if task:
                task.cancel()
        for task in background:
            if task:
                with suppress(asyncio.CancelledError):
                    await task
        self._news_stream_task = None
        self._news_rss_task = None
        self._x_radar_task = None
        self._fomo_radar_task = None
        self._runner_outcome_task = None
        self._runner_digest_task = None
        self._trending_task = None
        self._trending_hot_watch_task = None
        self._early_watch_task = None
        self._trenches_task = None
        self._pump_creation_task = None
        self._pump_creation_consumer_task = None
        for task in self._runner_fast_watch_tasks.values():
            task.cancel()
        if self._runner_fast_watch_tasks:
            await asyncio.gather(
                *self._runner_fast_watch_tasks.values(),
                return_exceptions=True,
            )
        self._runner_fast_watch_tasks.clear()
        for task in self._callout_tasks:
            task.cancel()
        if self._callout_tasks:
            await asyncio.gather(*self._callout_tasks, return_exceptions=True)
        self._callout_tasks.clear()
        if self._daily_profit_task:
            self._daily_profit_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._daily_profit_task
            self._daily_profit_task = None
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in (self._stream_task, self._stream_consumer_task):
            if task:
                task.cancel()
        for task in (self._stream_task, self._stream_consumer_task):
            if task:
                with suppress(asyncio.CancelledError):
                    await task
        self._stream_task = None
        self._stream_consumer_task = None
        await self.stream.close()
        await self.rpc.close()
        await self.market.close()
        await self.dex_screener.close()
        await self.trending_client.close()
        await self.pump_creation_stream.close()
        await self.x_social.close()
        await self.tracker_token_risk.close()
        await self.x_news_stream.close()
        await self.news_poller.close()
        await self.news_matcher.close()
        await self.pump_launcher.close()
        if self.discovery:
            await self.discovery.close()
        if self.profile_discovery:
            await self.profile_discovery.close()
        await self.database.close()

    async def _run_loop(self) -> None:
        while True:
            try:
                paused = (await self.database.get_setting("paused", "false")) == "true"
                if not paused:
                    daily_locked = await self._enforce_daily_profit_lock()
                    if not daily_locked:
                        try:
                            await self.refresh_discovery()
                            self._discovery_consecutive_failures = 0
                            self._discovery_retry_at = 0
                        except DiscoveryError as exc:
                            self.last_error = f"Discovery: {exc}"
                            self._note_discovery_failure()
                            await self.notifier.on_error("Refreshing wallet discovery", exc)
                        try:
                            await self.rotate_wallets()
                        except DiscoveryError as exc:
                            self.last_error = f"Rotation: {exc}"
                            await self.notifier.on_error("Rotating hot wallets", exc)
                        await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Monitor loop failed")
                await self.notifier.on_error("Monitor loop", exc)
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _run_daily_profit_guard(self) -> None:
        while True:
            try:
                paused = (await self.database.get_setting("paused", "false")) == "true"
                if not paused:
                    await self._enforce_daily_profit_lock()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"Daily paper guard: {exc}"
                logger.exception("Daily paper profit/loss guard failed")
                await self.notifier.on_error("Daily paper profit/loss guard", exc)
            await asyncio.sleep(self.settings.paper_daily_profit_check_seconds)

    def _note_discovery_failure(self) -> None:
        """Back off after a failed discovery refresh.

        The provider client backs off its own HTTP calls; this backs off the
        *caller*, so a failure that never reaches HTTP (an empty feed, a parse
        error) cannot spin either.
        """

        self._discovery_consecutive_failures += 1
        self._discovery_retry_at = int(time.time()) + provider_backoff_seconds(
            self._discovery_consecutive_failures
        )

    async def refresh_discovery(self, *, force: bool = False) -> DiscoveryRefresh | None:
        if not self.settings.auto_discovery_enabled or self.discovery is None:
            return None
        if self._discovery_lock.locked():
            return None
        now = int(time.time())
        if (
            not force
            # The candidate pool is deliberately NOT part of this condition any
            # more.  Requiring a non-empty pool disengaged the throttle exactly
            # when the provider was failing, so a plan that was out of credits
            # was retried every poll — production ran ~1,440 failing paid
            # requests a day against a ~40-request budget.  Time since the last
            # *attempt* is what bounds the spend.
            and self.last_discovery_attempt_at is not None
            and now - self.last_discovery_attempt_at
            < self.settings.effective_discovery_refresh_seconds
        ):
            return None
        if not force and now < self._discovery_retry_at:
            # A failure adds its own backoff on top of the budget window, so a
            # provider that is down is not retried on the budget's schedule
            # either.  It only ever lengthens the wait, never shortens it.
            return None
        self.last_discovery_attempt_at = now
        with suppress(Exception):
            await self.database.set_setting("discovery_last_attempt", str(now))

        async with self._discovery_lock:
            refresh_weekly = (
                force
                or not self._weekly_pool
                or self.last_weekly_refresh_at is None
                or now - self.last_weekly_refresh_at
                >= self.settings.effective_discovery_7d_refresh_seconds
            )
            if refresh_weekly:
                self._weekly_pool = await self.discovery.weekly_pool(self.discovery_policy)
                if self.discovery_policy.include_kols:
                    try:
                        self._weekly_pool.extend(
                            await self.discovery.kol_weekly_pool(self.discovery_policy)
                        )
                    except DiscoveryError as exc:
                        logger.warning(
                            "Public-KOL 7D discovery unavailable; using general feed: %s",
                            exc,
                        )
                if not self._weekly_pool:
                    raise DiscoveryError(
                        "The strict 7-day feed returned no qualifying wallets; "
                        "the existing hot set was preserved"
                    )
                self.last_weekly_refresh_at = int(time.time())
                await self.database.set_setting(
                    "discovery_7d_last_refresh", str(self.last_weekly_refresh_at)
                )

            daily_pool = await self.discovery.daily_pool(self.discovery_policy)
            if self.discovery_policy.include_kols:
                try:
                    daily_pool.extend(await self.discovery.kol_daily_pool(self.discovery_policy))
                except DiscoveryError as exc:
                    logger.warning(
                        "Public-KOL 24H discovery unavailable; using general feed: %s",
                        exc,
                    )
            await self._refresh_profile_nominations()
            candidates = merge_verified_windows(
                daily_pool, self._weekly_pool, self.discovery_policy
            )
            candidates, self.profile_verified_matches = annotate_social_nominations(
                candidates, self._social_nominations
            )
            if not candidates:
                raise DiscoveryError(
                    "No wallets were independently profitable in both strict 24-hour "
                    "and 7-day feeds; the existing hot set was preserved"
                )
            self._candidate_pool = candidates
            await self.database.cache_discovery_candidates(candidates)
            self.last_discovery_refresh_at = int(time.time())
            await self.database.set_setting(
                "discovery_last_refresh", str(self.last_discovery_refresh_at)
            )
            self.last_error = None
        return await self.rotate_wallets(force=True)

    async def _refresh_profile_nominations(self) -> None:
        """Refresh public social candidates without weakening financial admission."""

        if self.profile_discovery is None:
            return
        now = int(time.time())
        if (
            self._social_nominations
            and self.last_profile_refresh_at is not None
            and now - self.last_profile_refresh_at < self.settings.pump_profile_refresh_seconds
        ):
            return
        try:
            nominations = await self.profile_discovery.nominations(
                pages=self.settings.pump_profile_pages,
                minimum_followers=self.settings.pump_profile_min_followers,
                limit=self.settings.pump_profile_limit,
                max_profile_fetches=self.settings.pump_profile_max_page_fetches,
            )
        except Exception as exc:
            self.profile_discovery_last_error = str(exc)
            logger.warning(
                "Pump public-profile nominations unavailable; strict financial feeds "
                "remain active: %s",
                exc,
            )
            return
        if nominations:
            self._social_nominations = nominations
        else:
            logger.info(
                "Pump public profile page returned no resolvable public wallets; "
                "the previous nomination cache was preserved"
            )
        self.last_profile_refresh_at = now
        self.profile_discovery_last_error = None
        await self.database.set_setting("pump_profile_last_refresh", str(now))

    async def rotate_wallets(self, *, force: bool = False) -> DiscoveryRefresh | None:
        if not self._candidate_pool:
            return None
        now = int(time.time())
        if (
            not force
            and self.last_rotation_at is not None
            and now - self.last_rotation_at < self.settings.rotation_refresh_seconds
        ):
            return None
        eligible, forward_rejections, forward_evaluated = await self._apply_forward_paper_evidence(
            self._candidate_pool
        )
        if not eligible:
            self.last_rotation_result = RotationResult(
                selected=(),
                evaluated=tuple(forward_evaluated),
                rejection_reasons=forward_rejections,
                pool_size=len(self._candidate_pool),
                verified_pump_wallets=0,
            )
            raise DiscoveryError(
                "Every candidate failed mature forward PAPER evidence; the existing hot set "
                "was preserved"
            )
        raw_result = await self.rotator.evaluate(eligible, now=now)
        current_hot_set = await self.database.list_discovered(limit=50)
        current_pool_addresses = {candidate.address for candidate in self._candidate_pool}
        feed_removed = tuple(
            candidate
            for candidate in current_hot_set
            if candidate.address not in current_pool_addresses
        )
        feed_rejections = {
            candidate.address: (
                "no longer present in the current dual-window qualifying pool; "
                "the 24H/7D feed filters or ranking changed"
            )
            for candidate in feed_removed
        }
        result = RotationResult(
            selected=raw_result.selected,
            evaluated=(raw_result.evaluated + tuple(forward_evaluated) + feed_removed),
            rejection_reasons={
                **feed_rejections,
                **raw_result.rejection_reasons,
                **forward_rejections,
            },
            pool_size=len(self._candidate_pool),
            verified_pump_wallets=raw_result.verified_pump_wallets,
        )
        self.last_rotation_result = result
        if not result.selected:
            raise DiscoveryError(
                "No dual-window profitable wallets passed the recent Pump activity checks; "
                "the existing hot set was preserved"
            )
        refresh = await self.database.apply_discovery(
            list(result.selected),
            evaluated_candidates=list(result.evaluated),
            removal_reasons=result.rejection_reasons,
            candidate_pool_size=result.pool_size,
            verified_pump_wallets=result.verified_pump_wallets,
        )
        self.last_rotation_at = refresh.refreshed_at
        self.last_error = None
        if refresh.added_wallets or refresh.disabled_wallets:
            await self.notifier.on_discovery(refresh)
        return refresh

    async def _apply_forward_paper_evidence(
        self, candidates: list[DiscoveryCandidate]
    ) -> tuple[list[DiscoveryCandidate], dict[str, str], list[DiscoveryCandidate]]:
        """Penalize proven forward losers without judging brand-new candidates early."""

        performance = await self.database.paper_wallet_performance(
            [candidate.address for candidate in candidates]
        )
        eligible = []
        rejected: dict[str, str] = {}
        evaluated = []
        for candidate in candidates:
            metrics = performance.get(candidate.address)
            if metrics is None or int(metrics["closed_sells"]) < (
                self.settings.forward_evidence_min_closed_sells
            ):
                eligible.append(candidate)
                continue
            closed_sells = int(metrics["closed_sells"])
            pnl = Decimal(metrics["pnl"])
            profit_factor = Decimal(metrics["profit_factor"])
            reason: str | None = None
            if pnl <= -self.settings.forward_evidence_max_loss_usd:
                reason = (
                    f"forward PAPER failed after {closed_sells} exits: PnL ${pnl:,.2f} "
                    f"breached -${self.settings.forward_evidence_max_loss_usd:,.2f}"
                )
            elif profit_factor < self.settings.forward_evidence_min_profit_factor:
                reason = (
                    f"forward PAPER failed after {closed_sells} exits: profit factor "
                    f"{profit_factor:.2f} is below "
                    f"{self.settings.forward_evidence_min_profit_factor:.2f}"
                )
            if reason is not None:
                rejected[candidate.address] = reason
                evaluated.append(candidate)
                continue

            forward_bonus = min(
                Decimal("5"),
                max(Decimal("0"), (profit_factor - Decimal("1")) * Decimal("2")),
            )
            eligible.append(
                replace(
                    candidate,
                    score=min(Decimal("100"), candidate.score + forward_bonus),
                    selection_reason=(
                        f"{candidate.selection_reason}; forward PAPER {closed_sells} exits, "
                        f"${pnl:,.2f}, PF {profit_factor:.2f}"
                    ),
                )
            )
        return eligible, rejected, evaluated

    async def _consume_stream_events(self) -> None:
        while True:
            event = await self.stream.events.get()
            try:
                if await self.is_paused():
                    continue
                await self._process_stream_event(event)
            except asyncio.CancelledError:
                raise
            except (RpcError, JupiterError, ValueError) as exc:
                self.last_error = f"Realtime stream: {exc}"
                await self.notifier.on_error("Processing realtime wallet event", exc)
            finally:
                self.stream.events.task_done()

    async def _process_stream_event(self, event: StreamEvent) -> None:
        trader = await self.database.resolve_trader(event.wallet)
        if trader is None or not trader.enabled or trader.last_signature is None:
            return
        if await self.database.is_processed(event.signature):
            return
        transaction = None
        retry_delays = (0, 0.15, 0.35, 0.75, 1.5, 2.5)
        for delay in retry_delays:
            if delay:
                await asyncio.sleep(delay)
            transaction = await self.rpc.get_transaction(event.signature)
            if transaction is not None:
                break
        if transaction is None:
            raise RpcError("realtime transaction was unavailable after rapid fetch retries")
        block_time = int(transaction.get("blockTime") or time.time())
        await self._process_transaction(
            trader,
            signature=event.signature,
            transaction=transaction,
            block_time=block_time,
            is_bootstrap=False,
        )

    async def scan_once(self) -> dict[str, int]:
        if self._scan_lock.locked():
            return {"wallets": 0, "transactions": 0, "swaps": 0}
        async with self._scan_lock:
            self.last_scan_started_at = int(time.time())
            totals = {"wallets": 0, "transactions": 0, "swaps": 0}
            if await self._enforce_daily_profit_lock():
                self.last_scan_finished_at = int(time.time())
                return totals
            traders = await self.database.list_traders(enabled_only=True)
            for trader in traders:
                try:
                    counts = await self._sync_trader(trader)
                    totals["wallets"] += 1
                    totals["transactions"] += counts["transactions"]
                    totals["swaps"] += counts["swaps"]
                except (RpcError, JupiterError, ValueError) as exc:
                    self.last_error = f"{trader.alias}: {exc}"
                    await self.notifier.on_error(f"Scanning {trader.alias}", exc)
            try:
                await self._check_position_exits()
            except (JupiterError, ValueError) as exc:
                self.last_error = f"Risk exits: {exc}"
                await self.notifier.on_error("Checking risk exits", exc)
            await self._enforce_daily_profit_lock()
            self.last_scan_finished_at = int(time.time())
            return totals

    async def _sync_trader(self, trader: TrackedTrader) -> dict[str, int]:
        # Existing databases may already contain bootstrap inventory from before this
        # release. Seed those holdings before processing the next sell so a legitimate
        # exit cannot race ahead of its forward-test baseline.
        await self._seed_tracking_baselines(trader)
        candidates, newest, is_bootstrap = await self._signature_candidates(trader)
        counts = {"transactions": 0, "swaps": 0}
        if not candidates or newest is None:
            return counts

        had_retryable_failure = False
        for item in reversed(candidates):
            signature = item.get("signature")
            if not signature or item.get("err") is not None:
                continue
            if await self.database.is_processed(signature):
                continue
            try:
                transaction = await self.rpc.get_transaction(signature)
            except RpcError:
                had_retryable_failure = True
                continue
            if transaction is None:
                had_retryable_failure = True
                continue

            block_time = int(item.get("blockTime") or transaction.get("blockTime") or 0)
            processed = await self._process_transaction(
                trader,
                signature=str(signature),
                transaction=transaction,
                block_time=block_time,
                is_bootstrap=is_bootstrap,
            )
            counts["transactions"] += processed["transactions"]
            counts["swaps"] += processed["swaps"]

        if is_bootstrap:
            # The first history scan intentionally does not fire old BUY alerts. It does,
            # however, reconstruct the source wallet's current inventory. Establish a
            # current-price PAPER baseline now so later sells can be measured from the
            # moment tracking began rather than being reported as unmatched.
            await self._seed_tracking_baselines(trader)

        if not had_retryable_failure:
            await self.database.update_last_signature(trader.address, newest)
        return counts

    async def _seed_tracking_baselines(self, trader: TrackedTrader) -> None:
        """Open forward-only PAPER lots for holdings that predate monitoring."""

        if not self.settings.paper_seed_tracking_baselines:
            return
        if await self.execution_mode() is not ExecutionMode.PAPER:
            return
        if not self.settings.paper_mirror_raw_swaps:
            return
        if await self._daily_profit_entries_locked():
            return

        candidates = await self.database.paper_tracking_baseline_candidates(
            trader.address,
            limit=self.settings.paper_baseline_max_positions_per_wallet,
        )
        size = min(self.settings.default_copy_usd, self.settings.max_copy_usd)
        for candidate in candidates:
            token_mint = str(candidate["token_mint"])
            source_quantity = Decimal(str(candidate["source_quantity"]))
            if source_quantity <= 0:
                continue
            try:
                current_price = await self.market.price(token_mint)
            except JupiterError:
                current_price = None
            if current_price is None or current_price <= 0:
                # A stale historical transaction price would manufacture profit or loss
                # from before monitoring began, so wait for a real current price.
                continue

            baseline_swap = DetectedSwap(
                signature=f"tracking-baseline:{trader.address}:{token_mint}",
                trader_address=trader.address,
                block_time=int(time.time()),
                side=Side.BUY,
                token_mint=token_mint,
                token_amount=source_quantity,
                quote_mint="TRACKING_BASELINE",
                quote_amount=size,
                usd_value=size,
                token_price_usd=current_price,
            )
            result = await self.executor.execute_paper_mirror(
                swap=baseline_swap,
                trader=trader,
                market_price_usd=current_price,
                size_usd=size,
                baseline_mode=True,
            )
            if result.success:
                await self.notifier.on_execution(result)

    async def _process_transaction(
        self,
        trader: TrackedTrader,
        *,
        signature: str,
        transaction: dict,
        block_time: int,
        is_bootstrap: bool,
    ) -> dict[str, int]:
        # The websocket stream and polling fallback can observe the same confirmed
        # transaction concurrently. Keep one task responsible for it so alerts and
        # paper mirror attempts are never duplicated inside this process.
        if signature in self._processing_signatures:
            return {"transactions": 0, "swaps": 0}
        self._processing_signatures.add(signature)
        try:
            if await self.database.is_processed(signature):
                return {"transactions": 0, "swaps": 0}
            swap = await self.detector.detect(
                transaction,
                wallet=trader.address,
                signature=signature,
                block_time=block_time,
            )
            swap_count = 0
            if swap is not None:
                inserted = await self.database.record_swap(swap)
                swap_count = int(inserted)
                should_handle = inserted
                if (
                    not inserted
                    and not is_bootstrap
                    and self.settings.paper_mirror_raw_swaps
                    and await self.execution_mode() is ExecutionMode.PAPER
                ):
                    should_handle = not await self.database.has_paper_mirror_execution(
                        swap.signature
                    )
                if should_handle and not is_bootstrap:
                    await self._handle_new_swap(swap, trader)
            await self.database.mark_processed(signature, trader.address, block_time)
            return {"transactions": 1, "swaps": swap_count}
        finally:
            self._processing_signatures.discard(signature)

    async def _signature_candidates(
        self, trader: TrackedTrader
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        is_bootstrap = trader.last_signature is None
        cutoff = int(time.time()) - (self.settings.bootstrap_hours * 3600)
        collected: list[dict[str, object]] = []
        before: str | None = None
        newest: str | None = None

        while len(collected) < self.settings.max_backfill_transactions:
            batch = await self.rpc.get_signatures_for_address(
                trader.address,
                limit=min(100, self.settings.max_backfill_transactions - len(collected)),
                before=before,
                until=trader.last_signature if not is_bootstrap else None,
            )
            if not batch:
                break
            if newest is None:
                newest = batch[0].get("signature")

            should_stop = False
            for item in batch:
                signature = item.get("signature")
                if trader.last_signature and signature == trader.last_signature:
                    should_stop = True
                    break
                block_time = int(item.get("blockTime") or 0)
                if is_bootstrap and block_time and block_time < cutoff:
                    should_stop = True
                    break
                collected.append(item)
                if len(collected) >= self.settings.max_backfill_transactions:
                    should_stop = True
                    break
            if should_stop or len(batch) < 100:
                break
            before = batch[-1].get("signature")
            if not before:
                break

        return collected, str(newest) if newest else None, is_bootstrap

    async def _consider_signal(self, swap: DetectedSwap) -> None:
        rankings = await self.rankings()
        signal = await self.strategy.ingest(swap, rankings)
        if signal is None:
            return
        await self._process_signal(signal)

    async def _handle_new_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        mode = await self.execution_mode()
        daily_locked = mode is ExecutionMode.PAPER and await self._daily_profit_entries_locked()
        if daily_locked:
            if swap.side is Side.BUY:
                return
            if not await self.database.has_paper_mirror_position(trader.address, swap.token_mint):
                return
        exit_only = (
            mode is ExecutionMode.PAPER
            and self.settings.paper_mirror_raw_swaps
            and await self.database.trader_is_exit_only(trader.address)
        )
        has_linked_lot = (
            await self.database.has_paper_mirror_position(trader.address, swap.token_mint)
            if exit_only and swap.side is Side.SELL
            else False
        )
        # Rotation may remove a wallet from new entries while one of its linked
        # fake lots is still open. Keep monitoring only the sell that can close
        # an existing lot; ignore fresh buys and unrelated sells from that wallet.
        if exit_only and (swap.side is Side.BUY or not has_linked_lot):
            return

        await self.notifier.on_swap(swap, trader)
        # Realtime alpha lane: surface the observation immediately, on its own
        # task, so a slow public lookup can never delay the existing execution
        # pipeline.  It is research visibility only and can never open a
        # position.
        self._queue_notable_alert(swap, trader)
        if mode is ExecutionMode.PAPER and self.settings.paper_mirror_raw_swaps:
            await self._mirror_paper_swap(swap, trader)
        else:
            await self._consider_signal(swap)
        if swap.side is Side.BUY and self.settings.coin_callouts_enabled:
            self._queue_coin_callout(swap.token_mint)

    def _queue_coin_callout(self, mint: str, *, force_x_search: bool = False) -> None:
        task = asyncio.create_task(
            self._run_coin_callout(mint, force_x_search=force_x_search),
            name=f"coin-callout-{mint[:8]}",
        )
        self._callout_tasks.add(task)
        task.add_done_callback(self._callout_tasks.discard)

    async def _run_x_radar(self) -> None:
        """Use budgeted recent search as a proactive exact-contract nomination source."""

        while True:
            try:
                mints = await self.x_social.discover_contracts(self.settings.x_radar_query)
                for mint in mints[: self.settings.x_radar_max_contracts_per_scan]:
                    # discover_contracts cached the matching exact-contract X posts,
                    # so this forced analysis does not spend another paid X request.
                    self._queue_coin_callout(mint, force_x_search=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.x_social.last_radar_error = str(exc)[:200]
                await self.notifier.on_error("Proactive X radar", exc)
            await asyncio.sleep(self.settings.x_radar_poll_seconds)

    # ------------------------------------------------------------------
    # Ultra-early operator visibility (v2.41): sections 1-14, 45-52
    # ------------------------------------------------------------------

    async def _run_early_lane(self, mint: str, *, now: int) -> bool:
        """Give the operator a chance to look while the edge still exists.

        One cheap DEX snapshot, one pure evaluation, one card.  No wallet
        forensics, no Solana Tracker, no social lookup, no safety provider —
        safety is published honestly as UNKNOWN.  Nothing in here may block, and
        nothing in here may raise into the radar loop.
        """

        try:
            return await self._early_lane_task(mint, now=now)
        except Exception:
            logger.exception("Early lane failed for %s", mint[:8])
            return False

    async def _early_lane_task(self, mint: str, *, now: int) -> bool:
        # The mint that entered this lane is the mint that leaves it.  The DEX
        # snapshot is fetched *by address* and its parser drops any pair whose
        # baseToken is not this exact mint, so a same-symbol clone cannot be
        # substituted here — this assertion is what keeps that true if the
        # snapshot path ever changes.
        provenance = exact_identity(mint, source="early_lane")
        snapshot = await self.dex_screener.snapshot(mint)
        if not snapshot.available or snapshot.market_cap_usd is None:
            # Exact enrichment failed.  We say so; we never fall back to a
            # symbol search and publish whatever it returns.
            await self._record_suppression(mint, EARLY_WHY_NO_DATA, now=now)
            return False
        assert_exact_propagation(mint, mint, stage="early lane → DEX snapshot")

        # The first market cap the bot ever saw for this mint, written once.
        # It is a historical fact and must survive every later enrichment pass
        # (sections 3, 52) — that immutability is what makes "was this early?"
        # answerable at all.
        await self.database.record_alert_stage(
            mint=mint,
            stage=STAGE_BOT_FIRST_SEEN,
            occurred_at=now,
            market_cap_usd=snapshot.market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
        )
        timeline = {
            str(row["stage"]): row for row in await self.database.alert_timeline(mint)
        }
        first_seen_row = timeline.get(STAGE_BOT_FIRST_SEEN, {})
        first_seen_at = int(first_seen_row.get("occurred_at") or now)
        first_seen_mc = _engine_decimal(first_seen_row.get("market_cap_usd"))

        signals = EarlySignals(
            mint=mint,
            now=now,
            first_seen_at=first_seen_at,
            pair_age_seconds=(
                snapshot.pair_age_minutes * 60
                if snapshot.pair_age_minutes is not None
                else None
            ),
            market_cap_usd=snapshot.market_cap_usd,
            first_seen_market_cap_usd=first_seen_mc,
            liquidity_usd=snapshot.liquidity_usd,
            volume_5m_usd=snapshot.volume_5m_usd,
            price_change_5m_percent=snapshot.price_change_5m_percent,
            buys_5m=snapshot.buys_5m,
            sells_5m=snapshot.sells_5m,
            buys_1h=snapshot.buys_1h,
            sells_1h=snapshot.sells_1h,
            route_available=True,
            # Independent-buyer evidence for the organic gate.  ``None`` when the
            # participant intelligence could not establish it, which is honestly
            # different from zero and never counts as proof of independence.
            independent_buyers_5m=await self._independent_buyers_5m(mint, now=now),
            **await self._early_corroboration(mint),
        )
        verdict = evaluate_early_signal(signals, config=self._early_config)

        await self.database.record_alert_stage(
            mint=mint,
            stage=STAGE_CHEAP_SIGNAL,
            occurred_at=now,
            market_cap_usd=snapshot.market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
            tier=verdict.tier,
            edge_state=verdict.edge_state,
            evidence={
                "score": str(verdict.score),
                "categories": list(verdict.evidence_categories),
                "reasons": list(verdict.reasons[:6]),
                "why_not_pinged": list(verdict.why_not_pinged),
            },
        )
        for reason in verdict.why_not_pinged:
            await self._record_suppression(
                mint, reason, now=now, market_cap=snapshot.market_cap_usd, tier=verdict.tier
            )
        if not verdict.visible:
            return False

        if not self._early_lane_allows(mint, verdict, now=now):
            return False

        name, symbol = await self._cached_token_names(mint)
        alert = build_early_alert(
            mint=mint,
            name=name if name != "Unknown token" else (symbol or mint[:8]),
            symbol=symbol,
            fomo_url=self._fomo_url(mint),
            verdict=verdict,
            age_seconds=signals.pair_age_seconds,
            first_seen_seconds_ago=signals.seconds_since_first_seen,
            first_seen_market_cap_usd=first_seen_mc,
            alert_market_cap_usd=snapshot.market_cap_usd,
            current_market_cap_usd=snapshot.market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
            buys=snapshot.buys_5m,
            sells=snapshot.sells_5m,
            image_url=snapshot.image_url,
            safety_status="UNKNOWN",
            identity_verified=provenance.identity_verified,
            symbol_collision=await self._symbol_collides(mint, symbol),
        )
        published = await self._publish_fast_alert(alert, now=now)
        if not published:
            await self._record_suppression(mint, EARLY_WHY_DUPLICATE, now=now)
            return False

        stage = (
            STAGE_EARLY_RUNNER
            if verdict.tier in EARLY_PINGABLE_TIERS
            else STAGE_OPERATOR_HEADS_UP
        )
        await self.database.record_alert_stage(
            mint=mint,
            stage=stage,
            occurred_at=now,
            market_cap_usd=snapshot.market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
            tier=verdict.tier,
            edge_state=verdict.edge_state,
            evidence={"categories": list(verdict.evidence_categories)},
        )
        if alert.may_ping:
            await self.database.record_alert_stage(
                mint=mint,
                stage=STAGE_URGENT_PING,
                occurred_at=now,
                market_cap_usd=snapshot.market_cap_usd,
                tier=verdict.tier,
            )
        # Section 2: a strong near-miss is not finished being interesting just
        # because it did not clear the bar on its first look.  This is the exact
        # step whose absence produced the section 1 failure.
        await self._open_early_watch(
            mint,
            verdict=verdict,
            snapshot=snapshot,
            first_seen_market_cap_usd=first_seen_mc,
            independent_buyers=signals.independent_buyers_5m,
            now=now,
        )
        self._early_published[mint] = now
        if verdict.tier in EARLY_PINGABLE_TIERS:
            self._early_runner_times.append(now)
            self.early_runners_published += 1
        else:
            self.early_heads_up_published += 1
        self.last_early_alert_at = now
        self.last_early_alert_mint = mint
        return True

    # ------------------------------------------------------------------
    # early-candidate HOT WATCH and event-driven promotion (v2.44)
    # ------------------------------------------------------------------

    async def _restore_early_watches(self) -> None:
        """Reload open watches so a redeploy does not lose a live candidate."""

        now = int(time.time())
        with suppress(Exception):
            for row in await self.database.early_watch_rows(open_only=True, now=now, limit=200):
                entry = early_watch_from_json(row)
                if entry.mint:
                    self._early_watches[entry.mint] = entry

    async def _open_early_watch(
        self,
        mint: str,
        *,
        verdict: Any,
        snapshot: Any,
        first_seen_market_cap_usd: Decimal | None,
        independent_buyers: int | None,
        now: int,
    ) -> bool:
        """Keep looking at a strong near-miss instead of publishing once (section 2).

        This is the direct fix for the section 1 failure.  That candidate scored
        76/100 with no serious evidence category, which is exactly the shape this
        opens a watch on: real evidence, not yet a reason to interrupt anyone.
        """

        if not self.settings.fomo_early_watch_enabled:
            return False
        if not should_open_watch(verdict, config=self._early_watch_config):
            return False
        if mint in self._early_watches:
            return False
        live = prune_early_watches(self._early_watches.values(), now=now)
        self._early_watches = {entry.mint: entry for entry in live}
        if len(self._early_watches) >= self._early_watch_config.max_entries:
            return False

        holders = await self._holder_count(mint, now=now)
        entry = open_early_watch(
            mint,
            verdict=verdict,
            now=now,
            market_cap_usd=snapshot.market_cap_usd,
            first_seen_market_cap_usd=first_seen_market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
            buys=snapshot.buys_5m,
            independent_buyers=independent_buyers,
            holder_count=holders,
            config=self._early_watch_config,
        )
        self._early_watches[mint] = entry
        self.early_watches_opened += 1
        with suppress(Exception):
            await self.database.save_early_watch(entry.to_json(), now=now)
        return True

    async def _run_early_watch(self) -> None:
        """Timer-driven rechecks.  Events do not wait for this (section 29)."""

        while True:
            with suppress(asyncio.CancelledError):
                try:
                    await self._recheck_early_watches(now=int(time.time()))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Early hot-watch recheck failed")
            await asyncio.sleep(self.settings.fomo_early_watch_recheck_seconds)

    async def note_early_watch_event(self, mint: str, *, trigger: str) -> bool:
        """Re-evaluate one watched candidate immediately (section 29).

        A known trader buying at second 12 of a 45-second timer is news at
        second 12.  Waiting for the next tick is how a promotion arrives after
        the move it was meant to catch.
        """

        if mint not in self._early_watches:
            return False
        self.early_watch_event_rechecks += 1
        return await self._recheck_early_watches(
            now=int(time.time()), trigger=trigger, mints=(mint,)
        )

    async def _recheck_early_watches(
        self,
        *,
        now: int,
        trigger: str = "timer",
        mints: tuple[str, ...] | None = None,
    ) -> bool:
        """Evaluate due watches against *new* evidence and promote what earned it."""

        promoted_any = False
        targets = mints if mints is not None else tuple(self._early_watches)
        for mint in targets:
            entry = self._early_watches.get(mint)
            if entry is None:
                continue
            if trigger == "timer" and not entry.due(now=now, config=self._early_watch_config):
                continue
            evidence = await self._promotion_evidence(mint, entry, now=now, trigger=trigger)
            if evidence is None:
                continue
            outcome = evaluate_early_promotion(
                entry, evidence, config=self._early_watch_config
            )
            self._early_watches[mint] = outcome.entry
            with suppress(Exception):
                await self.database.save_early_watch(outcome.entry.to_json(), now=now)
            if outcome.expired or outcome.entry.promoted:
                self._early_watches.pop(mint, None)
            if outcome.decision.promote:
                promoted_any = await self._publish_promotion(
                    outcome.entry, outcome.decision, now=now
                ) or promoted_any
            else:
                await self._record_suppression(
                    mint, outcome.entry.suppression_reason, now=now, tier=entry.entry_tier
                )
        return promoted_any

    async def _promotion_evidence(
        self,
        mint: str,
        entry: EarlyWatchEntry,
        *,
        now: int,
        trigger: str,
    ) -> PromotionEvidence | None:
        """Gather what is knowable cheaply.  Unknown stays ``None``, never zero.

        Everything here is already-fetched or free: the cached DEX snapshot, the
        bot's own persisted swaps, and public RPC concentration.  No new paid
        provider is introduced by this loop.
        """

        snapshot = await self.dex_screener.snapshot(mint)
        if not snapshot.available or snapshot.market_cap_usd is None:
            return None
        assert_exact_propagation(mint, mint, stage="early watch → DEX snapshot")

        signals = EarlySignals(
            mint=mint,
            now=now,
            first_seen_at=entry.opened_at,
            pair_age_seconds=(
                snapshot.pair_age_minutes * 60 if snapshot.pair_age_minutes is not None else None
            ),
            market_cap_usd=snapshot.market_cap_usd,
            first_seen_market_cap_usd=entry.first_seen_market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
            volume_5m_usd=snapshot.volume_5m_usd,
            price_change_5m_percent=snapshot.price_change_5m_percent,
            buys_5m=snapshot.buys_5m or 0,
            sells_5m=snapshot.sells_5m or 0,
            buys_1h=snapshot.buys_1h,
            sells_1h=snapshot.sells_1h,
            route_available=True,
            independent_buyers_5m=await self._independent_buyers_5m(mint, now=now),
            **await self._early_corroboration(mint),
        )
        verdict = evaluate_early_signal(signals, config=self._early_config)

        confirmation = await self._known_trader_confirmation(mint, now=now)
        holders = await self._holder_count(mint, now=now)
        series = self._holder_series.get(mint)
        concentration = await self._concentration_trend(mint, now=now)
        return PromotionEvidence(
            now=now,
            score=verdict.score,
            edge_available=verdict.edge_state == EARLY_EDGE_AVAILABLE,
            market_cap_usd=snapshot.market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
            buys=snapshot.buys_5m,
            sells=snapshot.sells_5m,
            independent_buyers=signals.independent_buyers_5m,
            holder_count=holders,
            holders_per_minute=series.per_minute if series is not None else None,
            concentration_trend=concentration,
            proven_independent_traders=(
                confirmation.proven_independent_count if confirmation is not None else 0
            ),
            known_money_flow=(
                known_money_flow(confirmation) if confirmation is not None else ""
            ),
            story_state=str(signals.story_state or ""),
            story_relationship=str(signals.story_relationship or ""),
            catalyst_confidence=str(signals.catalyst_confidence or ""),
            trigger=trigger,
        )

    async def _holder_count(self, mint: str, *, now: int) -> int | None:
        """The holder count when a source actually supplies one (sections 10, 11).

        The public RPC methods this bot uses cannot count holders — the largest
        accounts call returns twenty and nothing more — so this reads the count
        the Trending board publishes for tokens that are on it, and returns
        ``None`` otherwise.  A guessed holder count would feed the promotion
        gate a number nobody measured.
        """

        count: int | None = None
        with suppress(Exception):
            entry = self.trending.entry_for(mint)
            if entry is not None:
                count = entry.holder_count
        if count is None:
            return None
        series = self._holder_series.get(mint) or HolderSeries(mint=mint)
        self._holder_series[mint] = series.record(
            HolderSample(at=now, holder_count=count)
        )
        with suppress(Exception):
            await self.database.record_holder_sample(
                mint=mint, observed_at=now, holder_count=count
            )
        return count

    async def _concentration_trend(self, mint: str, *, now: int) -> str:
        """Whether ownership is broadening or tightening (section 12)."""

        with suppress(Exception):
            snapshot = await self.pump_chain.holder_snapshot(mint, at=now)
            if snapshot.top10_percent is None:
                return ""
            await self.database.record_holder_sample(
                mint=mint,
                observed_at=now,
                holder_count=int(snapshot.holder_count or 0),
                top10_percent=float(snapshot.top10_percent),
            )
            # The trend is read across every recorded sample, not against the
            # single previous one: one noisy read must not flip the verdict.
            history = [
                TrenchHolderSnapshot(
                    mint=mint,
                    at=int(row["observed_at"]),
                    top10_percent=Decimal(str(row["top10_percent"])),
                )
                for row in await self.database.holder_samples(mint, limit=24)
                if row.get("top10_percent") is not None
            ]
            return assess_concentration_trend(mint, history).state
        return ""

    async def _known_trader_confirmation(
        self, mint: str, *, now: int
    ) -> TraderConfirmation | None:
        """Which registry wallets hold this exact mint, and how independent they are.

        Built from the bot's own persisted swaps, so it costs nothing and it can
        never attribute another token's activity to this one.
        """

        if not self.settings.fomo_top_traders_enabled:
            return None
        try:
            rows = await self.database.token_swap_rows(
                mint, limit=self.settings.fomo_top_traders_limit * 20
            )
        except Exception:
            return None
        if not rows:
            return None
        fills = [
            TraderFill(
                wallet=str(row.get("trader_address") or ""),
                mint=mint,
                side=str(row.get("side") or ""),
                at=int(row.get("block_time") or 0),
                amount_usd=_engine_decimal(row.get("usd_value")) or Decimal("0"),
                tokens=_engine_decimal(row.get("token_amount")) or Decimal("0"),
                # The swaps table stores a price, not a market cap; an entry
                # market cap is only stated when a source actually supplied one.
                market_cap_usd=None,
                signature=str(row.get("signature") or ""),
            )
            for row in rows
        ]
        positions = build_positions(fills, mint=mint)
        if not positions:
            return None

        registry: dict[str, str] = {}
        reputations: dict[str, tuple[str, int]] = {}
        for position in positions:
            profile = await self._notable_wallet(position.wallet, alias="")
            if profile is None:
                continue
            registry[position.wallet] = str(getattr(profile, "display_name", "") or "")
            reputation = await self._wallet_reputation(position.wallet)
            reputations[position.wallet] = (
                str(getattr(reputation, "state", "UNKNOWN") or "UNKNOWN"),
                int(getattr(reputation, "samples", 0) or 0),
            )
        if not registry:
            return None

        clusters = await self._wallet_clusters(tuple(registry), mint=mint)
        known = join_known_traders(
            positions,
            mint=mint,
            registry=registry,
            reputations=reputations,
            clusters=clusters,
        )
        confirmation = independent_confirmations(known, mint=mint)
        with suppress(Exception):
            for position in positions[: self.settings.fomo_top_traders_limit]:
                await self.database.record_token_trader(
                    mint=mint,
                    wallet=position.wallet,
                    position=position.to_json(),
                    cluster_id=clusters.get(position.wallet, ""),
                    now=now,
                )
        return confirmation

    async def _wallet_clusters(self, wallets: tuple[str, ...], *, mint: str) -> dict[str, str]:
        """Group wallets that share a funder, so they count once (section 8)."""

        records: list[BuyerRecord] = []
        for wallet in wallets:
            funder = ""
            funded_at: int | None = None
            with suppress(Exception):
                history = await self.pump_chain.wallet_history(wallet)
                funder = str(getattr(history, "funded_by", "") or "")
                funded_at = getattr(history, "funded_at", None)
            if funder:
                records.append(
                    BuyerRecord(wallet=wallet, at=0, funded_by=funder, funded_at=funded_at)
                )
        if not records:
            return {}
        mapping: dict[str, str] = {}
        for cluster in detect_clusters(records):
            for wallet in cluster.wallets:
                mapping.setdefault(wallet, cluster.cluster_id)
        return mapping

    async def _publish_promotion(
        self,
        entry: EarlyWatchEntry,
        decision: Any,
        *,
        now: int,
    ) -> bool:
        """The card the section 1 candidate never got."""

        mint = entry.mint
        snapshot = await self.dex_screener.snapshot(mint)
        name, symbol = await self._cached_token_names(mint)
        confirmation = await self._known_trader_confirmation(mint, now=now)
        series = self._holder_series.get(mint)
        alert = build_promotion_alert(
            mint=mint,
            name=name if name != "Unknown token" else (symbol or mint[:8]),
            symbol=symbol,
            fomo_url=self._fomo_url(mint),
            decision=decision,
            entry=entry,
            age_seconds=(
                snapshot.pair_age_minutes * 60 if snapshot.pair_age_minutes is not None else None
            ),
            current_market_cap_usd=snapshot.market_cap_usd,
            liquidity_usd=snapshot.liquidity_usd,
            change_5m_percent=snapshot.price_change_5m_percent,
            buys=snapshot.buys_5m,
            sells=snapshot.sells_5m,
            holder_series=series.render() if series is not None else "",
            holders_added=series.added if series is not None else None,
            holder_window_seconds=series.span_seconds if series is not None else None,
            independent_buyers=await self._independent_buyers_5m(mint, now=now),
            known_traders=confirmation.traders if confirmation is not None else (),
            known_money_flow=(
                known_money_flow(confirmation) if confirmation is not None else ""
            ),
            cluster_note="; ".join(confirmation.notes) if confirmation is not None else "",
            safety_status="UNKNOWN",
            identity_verified=True,
            symbol_collision=await self._symbol_collides(mint, symbol),
            image_url=snapshot.image_url,
            terminal_url=self._terminal_url(mint),
        )
        published = await self._publish_fast_alert(alert, now=now)
        if published:
            self.early_promotions += 1
            self._schedule_alert_enrichment(alert)
            with suppress(Exception):
                await self.database.record_alert_stage(
                    mint=mint,
                    stage=STAGE_EARLY_RUNNER,
                    occurred_at=now,
                    market_cap_usd=snapshot.market_cap_usd,
                    liquidity_usd=snapshot.liquidity_usd,
                    tier="EARLY_PROMOTION",
                    evidence={"families": list(decision.families)},
                )
        return published

    def _terminal_url(self, mint: str) -> str:
        """An admin-supplied Terminal deep link, or nothing (section 20).

        Navigation only.  The template comes from a Railway variable an operator
        set from documented product behaviour; this codebase never guesses one,
        never logs in, and never reads an authenticated page.
        """

        template = self.settings.terminal_token_url_template
        if not template or "{mint}" not in template:
            return ""
        return template.replace("{mint}", mint)

    async def early_watch_report(self, *, limit: int = 25) -> dict[str, Any]:
        """Everything section 30 asks to be answerable after the fact."""

        now = int(time.time())
        rows: list[dict[str, Any]] = []
        with suppress(Exception):
            rows = await self.database.early_watch_rows(limit=limit)
        entries = [early_watch_from_json(row) for row in rows]
        status = summarise_early_watches(entries, now=now)
        return {
            "now": now,
            "status": status.to_json(),
            "entries": [entry.to_json() for entry in entries],
            "live": [entry.to_json() for entry in self._early_watches.values()],
            "opened": self.early_watches_opened,
            "promotions": self.early_promotions,
            "event_rechecks": self.early_watch_event_rechecks,
        }

    async def top_traders_report(self, mint: str, *, limit: int = 12) -> dict[str, Any]:
        """`view:traders` — our own top-trader board for one exact mint (section 21)."""

        now = int(time.time())
        rows: list[dict[str, Any]] = []
        with suppress(Exception):
            rows = await self.database.token_trader_rows(mint, limit=limit)
        confirmation = await self._known_trader_confirmation(mint, now=now)
        return {
            "mint": mint,
            "now": now,
            "rows": rows,
            "confirmation": confirmation.to_json() if confirmation is not None else None,
            "flow": known_money_flow(confirmation) if confirmation is not None else "",
            "terminal_url": self._terminal_url(mint),
        }

    async def _independent_buyers_5m(self, mint: str, *, now: int) -> int | None:
        """Distinct independent buyers behind the recent flow, when knowable.

        Returns ``None`` rather than ``0`` when the evidence is unavailable: a
        raw buy count says nothing about how many actors produced it, and the
        organic gate must be able to tell "few buyers" apart from "we do not
        know yet".
        """

        with suppress(Exception):
            buyers = await self.database.recent_verified_token_buyers(mint, now - 300)
            if buyers:
                # Count wallets, not rows: the same address under two aliases is
                # one buyer, and inflating this number is exactly the mistake the
                # organic gate exists to stop.
                return len({str(item[0] if isinstance(item, tuple) else item) for item in buyers})
        return None

    async def _symbol_collides(self, mint: str, symbol: str) -> bool:
        """Whether other live tokens answer to this one's ticker (hotfix §3).

        Informational only.  It never selects between them — there is no basis
        on which to select — it raises the operator's guard and, elsewhere,
        raises the bar for promotion.
        """

        if not symbol:
            return False
        with suppress(Exception):
            known = await self.database.known_symbols(limit=500)
            return detect_symbol_collision(symbol, known, subject_mint=mint).detected
        return False

    async def _early_corroboration(self, mint: str) -> dict[str, Any]:
        """Cheap, already-persisted corroboration — never a new provider call."""

        payload: dict[str, Any] = {
            "notable_wallet_count": 0,
            "proven_early_wallet_count": 0,
            "story_state": "",
            "story_relationship": "",
        }
        with suppress(Exception):
            links = await self.database.narrative_link_rows(mint=mint, limit=1)
            if links:
                payload["story_relationship"] = str(links[0].get("relationship") or "")
                narratives = await self.database.narrative_rows(limit=50)
                by_id = {str(row["narrative_id"]): row for row in narratives}
                story = by_id.get(str(links[0].get("narrative_id") or ""))
                if story:
                    payload["story_state"] = str(story.get("virality") or "")
        return payload

    def _early_lane_allows(self, mint: str, verdict: Any, *, now: int) -> bool:
        """Cooldown and hourly ceiling, so being early does not become spam."""

        last = self._early_published.get(mint)
        if last is not None and now - last < self.settings.fomo_early_cooldown_seconds:
            return False
        if verdict.tier in EARLY_PINGABLE_TIERS:
            while (
                self._early_runner_times
                and now - self._early_runner_times[0] >= 3_600
            ):
                self._early_runner_times.popleft()
            if len(self._early_runner_times) >= self.settings.fomo_early_max_runners_per_hour:
                self.fast_alerts_suppressed += 1
                return False
        return True

    async def _record_suppression(
        self,
        mint: str,
        reason_code: str,
        *,
        now: int,
        market_cap: Decimal | None = None,
        tier: str = "",
    ) -> None:
        """Section 12: "why wasn't I pinged?" must be answerable afterwards."""

        with suppress(Exception):
            await self.database.record_alert_suppression(
                mint=mint,
                reason_code=reason_code,
                occurred_at=now,
                market_cap_usd=market_cap,
                tier=tier,
                detail=HUMAN_EARLY_WHY.get(reason_code, ""),
            )

    async def _record_discovery(self, mint: str, *, now: int) -> None:
        """Write the cheap-discovery timestamp; never let it break a scan."""

        if self.database.connection is None:
            return
        try:
            await self.database.record_discovery(
                mint=mint,
                source_name=self.settings.fomo_discovery_source_name,
                source_event_at=None,
                now=now,
                source_is_realtime=True,
            )
        except Exception:
            logger.exception("Could not persist cheap discovery for %s", mint)

    async def _mark_discovery_stage(self, mint: str, stage: str, *, at: int) -> None:
        if self.database.connection is None:
            return
        try:
            await self.database.mark_discovery_stage(mint=mint, stage=stage, at=at)
        except Exception:
            logger.exception("Could not record %s stage for %s", stage, mint)

    async def _run_fomo_radar(self) -> None:
        """Fast public nomination lane; the digest remains a separate slow summary."""

        while True:
            try:
                now = int(time.time())
                mints = await self.dex_screener.trending_mints()
                due = [
                    mint
                    for mint in mints
                    if now - self._fomo_radar_seen.get(mint, 0)
                    >= self.settings.fomo_radar_recheck_seconds
                ]
                # Never-seen mints go first.  A backlog of rechecks used to be
                # able to consume the whole per-scan budget and push a genuinely
                # new token to the next poll, which is pure added latency.
                due.sort(key=lambda mint: mint in self._fomo_radar_seen)
                selected = due[: self.settings.fomo_radar_max_candidates_per_scan]
                for mint in selected:
                    self._fomo_radar_seen[mint] = now
                    # Persist first-seen the instant cheap discovery detects the
                    # mint.  Everything below this line is enrichment, and none
                    # of it may delay the timestamp we measure ingestion with.
                    await self._record_discovery(mint, now=now)
                    if self.settings.coin_callouts_enabled:
                        self._queue_coin_callout(mint)

                # The cheap operator lane runs HERE — before the deep gather
                # below, not after it.  This is the whole fix: every
                # operator-visible alert used to sit behind ``analyze_runner``,
                # which is budgeted at 30 seconds *per mint* and gathered across
                # the batch, so the slowest token delayed all of them.  A token
                # first seen at $31K could not reach a human until the deep pass
                # finished, by which point it was $61K.  One DEX snapshot per
                # mint is enough to say "this is moving, look now".
                if self.settings.fomo_early_lane_enabled:
                    await asyncio.gather(
                        *(self._run_early_lane(mint, now=now) for mint in selected)
                    )
                if self.settings.fomo_runner_enabled:
                    async def evaluate(
                        mint: str,
                        radar_seen_at: int = now,
                    ) -> RunnerCandidate | None:
                        budget = self.settings.fomo_runner_analysis_budget_seconds
                        try:
                            async with asyncio.timeout(budget):
                                return await self.analyze_runner(
                                    mint,
                                    radar_seen_at=radar_seen_at,
                                )
                        except TimeoutError:
                            # Say which budget was blown.  Production logged
                            # these as an empty message, which made a systemic
                            # enrichment stall look like nothing at all.
                            self.runner_analysis_timeouts += 1
                            logger.warning(
                                "Fomo fresh analysis %s exceeded its %ss budget; "
                                "the candidate was skipped this pass",
                                mint[:8],
                                budget,
                            )
                            return None
                        except Exception as exc:
                            self.runner_analysis_errors += 1
                            await self.notifier.on_error(
                                f"Fomo fresh analysis {mint[:8]}", exc
                            )
                            return None

                    evaluated = await asyncio.gather(*(evaluate(mint) for mint in selected))
                    candidates = [item for item in evaluated if item is not None]
                    candidates.sort(
                        key=lambda item: (
                            item.pair_created_at is not None,
                            item.pair_created_at or 0,
                            item.score,
                        ),
                        reverse=True,
                    )
                    for candidate in candidates:
                        await self._maybe_publish_fast_watch(candidate)
                        await self._maybe_publish_fresh(candidate)
                        await self._maybe_publish_runner(candidate)
                        self._start_runner_fast_watch(candidate)
                if len(self._fomo_radar_seen) > 1000:
                    cutoff = now - self.settings.fomo_radar_recheck_seconds * 2
                    self._fomo_radar_seen = {
                        mint: seen_at
                        for mint, seen_at in self._fomo_radar_seen.items()
                        if seen_at >= cutoff
                    }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.dex_screener.last_radar_error = str(exc)[:200]
                await self.notifier.on_error("Fomo public-data radar", exc)
            await asyncio.sleep(self.settings.fomo_radar_poll_seconds)

    async def analyze_runner(
        self,
        mint: str,
        *,
        refresh_market: bool = True,
        x_evidence=None,
        allow_automatic_x: bool = True,
        deep_forensics: bool = False,
        radar_seen_at: int | None = None,
    ) -> RunnerCandidate:
        """Capture and persist one time-T existing-token runner evaluation."""

        await self.initialize()
        analysis_started_at = int(time.time())
        radar_seen_at = radar_seen_at or analysis_started_at
        buyer_evidence = await self.database.recent_verified_token_buy_evidence(
            mint,
            analysis_started_at - max(3_600, self.settings.coin_callout_window_seconds),
        )
        buyers = await self.database.recent_verified_token_buyers(
            mint,
            analysis_started_at - max(3_600, self.settings.coin_callout_window_seconds),
        )
        callout = await self.analyze_coin(
            mint,
            buyers=buyers,
            allow_x_search=False,
            refresh_market=refresh_market,
            verify_sell_route=deep_forensics,
        )
        now = int(time.time())
        if x_evidence is not None:
            callout = replace(callout, social=x_evidence)
        current = runner_snapshot_from_callout(
            callout,
            captured_at=now,
            verified_unique_buyers=int(buyer_evidence["unique_buyers"]),
            largest_verified_buyer_percent=buyer_evidence["largest_buyer_percent"],
        )
        prior_raw = await self.database.runner_candidate_payload(mint)
        if prior_raw:
            prior_candidate = runner_candidate_from_json(prior_raw)
            first = prior_candidate.first
            legacy_pair_proxy = bool(
                prior_candidate.graduated_at is not None
                and prior_candidate.graduation_source.startswith("DEX_PAIR_CREATED_PROXY")
            )
            pair_created_at = prior_candidate.pair_created_at or (
                prior_candidate.graduated_at if legacy_pair_proxy else None
            )
            graduated_at = None if legacy_pair_proxy else prior_candidate.graduated_at
            graduation_source = prior_candidate.graduation_source
        else:
            first = current
            pair_age = callout.dex.pair_age_minutes
            pair_created_at = now - pair_age * 60 if pair_age is not None else None
            graduated_at = None
            graduation_source = (
                "DEX_PAIR_CREATED_PROXY — not exact Pump graduation"
                if pair_age is not None
                else "UNAVAILABLE"
            )
            prior_candidate = None
        forensic_payload = (
            await self.database.runner_forensics_payload(mint)
            if self.database.connection is not None
            else None
        )
        forensics = (
            runner_forensics_from_json(forensic_payload)
            if forensic_payload
            else RunnerForensics()
        )
        if deep_forensics and (
            not forensics.available or now - forensics.dynamic_checked_at >= 60
        ):
            forensics = await self._collect_runner_forensics(
                mint,
                raw_unique_buyers=int(buyer_evidence["unique_buyers"]),
                raw_top10_percent=current.top10_percent,
                buyer_first_seen_at=dict(buyer_evidence.get("buyer_first_seen_at", {})),
                now=now,
                cached=forensics if forensics.available else None,
            )
        history = tuple(
            runner_snapshot_from_json(item)
            for item in await self.database.runner_snapshot_payloads(
                mint,
                before_at=now,
                limit=30,
            )
        )
        score_history = (
            await self.database.runner_score_history(mint)
            if self.database.connection is not None
            else ()
        )
        candidate = score_runner_candidate(
            callout,
            first=first,
            current=current,
            history=history,
            graduated_at=graduated_at,
            graduation_source=graduation_source,
            earliest_smart_entry_at=buyer_evidence["earliest_buy_at"],
            smart_wallets=tuple(buyer_evidence["wallets"]),
            smart_wallet_addresses=tuple(buyer_evidence.get("wallet_addresses", ())),
            forensics=forensics,
            score_history=score_history,
            pair_created_at=pair_created_at,
            quality_config=self._quality_config,
            now=now,
        )
        if prior_candidate is not None:
            candidate = replace(
                candidate,
                # The funnel high-water mark and the immutable detection
                # snapshot both survive every re-evaluation, so "did this ever
                # qualify?" stays answerable for missed-runner analysis.
                best_stage=merge_best_stage(prior_candidate.best_stage, candidate.stage),
                qualified_at=prior_candidate.qualified_at or candidate.qualified_at,
                qualified_market_cap_usd=(
                    prior_candidate.qualified_market_cap_usd
                    or candidate.qualified_market_cap_usd
                ),
                heating_at=prior_candidate.heating_at or candidate.heating_at,
                detection_quality=(
                    prior_candidate.detection_quality
                    if prior_candidate.detection_quality.evaluated_at
                    else candidate.detection_quality
                ),
                chain_created_at=prior_candidate.chain_created_at,
                pair_created_at=prior_candidate.pair_created_at or candidate.pair_created_at,
                radar_first_seen_at=(
                    prior_candidate.radar_first_seen_at or prior_candidate.first_seen_at
                ),
                first_market_data_at=(
                    prior_candidate.first_market_data_at or candidate.first_market_data_at
                ),
                first_research_eligible_at=(
                    prior_candidate.first_research_eligible_at
                    or candidate.first_research_eligible_at
                ),
                first_discord_visible_at=prior_candidate.first_discord_visible_at,
                entry_eligible_at=(
                    prior_candidate.entry_eligible_at or candidate.entry_eligible_at
                ),
                strong_alert_at=prior_candidate.strong_alert_at,
                detection_safety=prior_candidate.detection_safety,
                detection_forensics=prior_candidate.detection_forensics,
                detection_score=prior_candidate.detection_score,
            )
        else:
            candidate = replace(candidate, radar_first_seen_at=radar_seen_at)
        public_ready = bool(
            candidate.stage in ENTRY_QUALITY_STAGES
            and candidate.score >= self.settings.fomo_runner_public_alert_min_score
            and candidate.safety.status == "PASS"
            and candidate.safety.entry_eligible
            and not candidate.overextended
        )
        x_worthy = bool(
            candidate.score >= self.settings.fomo_runner_public_alert_min_score
            and candidate.safety.status == "PASS"
            and not candidate.overextended
        )
        candidate = replace(candidate, research_only=not public_ready)

        if (
            allow_automatic_x
            and x_worthy
            and self.x_social.search_enabled
            and not candidate.x_evidence.available
        ):
            social = await self.x_social.snapshot(
                mint,
                symbol=candidate.symbol,
                name=candidate.name,
                context="fomo_runner_automatic",
                free_score=int(candidate.score),
            )
            if social.available:
                callout = replace(callout, social=social)
                candidate = score_runner_candidate(
                    callout,
                    first=first,
                    current=current,
                    history=history,
                    graduated_at=graduated_at,
                    graduation_source=graduation_source,
                    earliest_smart_entry_at=buyer_evidence["earliest_buy_at"],
                    smart_wallets=tuple(buyer_evidence["wallets"]),
                    smart_wallet_addresses=tuple(
                        buyer_evidence.get("wallet_addresses", ())
                    ),
                    forensics=forensics,
                    score_history=candidate.score_history[:-1],
                    pair_created_at=pair_created_at,
                    quality_config=self._quality_config,
                    now=now,
                )
                candidate = replace(
                    candidate,
                    best_stage=(
                        merge_best_stage(prior_candidate.best_stage, candidate.stage)
                        if prior_candidate
                        else candidate.stage
                    ),
                    qualified_at=(
                        (prior_candidate.qualified_at if prior_candidate else None)
                        or candidate.qualified_at
                    ),
                    qualified_market_cap_usd=(
                        (
                            prior_candidate.qualified_market_cap_usd
                            if prior_candidate
                            else None
                        )
                        or candidate.qualified_market_cap_usd
                    ),
                    heating_at=(
                        (prior_candidate.heating_at if prior_candidate else None)
                        or candidate.heating_at
                    ),
                    detection_quality=(
                        prior_candidate.detection_quality
                        if prior_candidate and prior_candidate.detection_quality.evaluated_at
                        else candidate.detection_quality
                    ),
                    chain_created_at=(
                        prior_candidate.chain_created_at if prior_candidate else None
                    ),
                    pair_created_at=(
                        prior_candidate.pair_created_at
                        if prior_candidate and prior_candidate.pair_created_at
                        else candidate.pair_created_at
                    ),
                    radar_first_seen_at=(
                        prior_candidate.radar_first_seen_at
                        if prior_candidate
                        else radar_seen_at
                    ),
                    first_market_data_at=(
                        prior_candidate.first_market_data_at
                        if prior_candidate
                        else candidate.first_market_data_at
                    ),
                    first_research_eligible_at=(
                        prior_candidate.first_research_eligible_at
                        if prior_candidate and prior_candidate.first_research_eligible_at
                        else candidate.first_research_eligible_at
                    ),
                    first_discord_visible_at=(
                        prior_candidate.first_discord_visible_at if prior_candidate else None
                    ),
                    entry_eligible_at=(
                        prior_candidate.entry_eligible_at
                        if prior_candidate and prior_candidate.entry_eligible_at
                        else candidate.entry_eligible_at
                    ),
                    strong_alert_at=(
                        prior_candidate.strong_alert_at if prior_candidate else None
                    ),
                    detection_safety=(
                        prior_candidate.detection_safety
                        if prior_candidate
                        else candidate.detection_safety
                    ),
                    detection_forensics=(
                        prior_candidate.detection_forensics
                        if prior_candidate
                        else candidate.detection_forensics
                    ),
                    detection_score=(
                        prior_candidate.detection_score
                        if prior_candidate
                        else candidate.detection_score
                    ),
                    research_only=bool(
                        candidate.stage not in ENTRY_QUALITY_STAGES
                        or candidate.score < self.settings.fomo_runner_public_alert_min_score
                        or candidate.safety.status != "PASS"
                        or candidate.overextended
                    ),
                )

        await self.database.store_runner_candidate(
            candidate,
            payload_json=runner_candidate_to_json(candidate),
            snapshot_json=runner_snapshot_to_json(candidate.current),
        )
        if forensics.available:
            await self.database.store_runner_forensics(
                mint=mint,
                payload_json=runner_forensics_to_json(forensics),
                funding_checked_at=forensics.funding_checked_at or forensics.checked_at,
                dynamic_checked_at=forensics.dynamic_checked_at or forensics.checked_at,
            )
        await self._record_runner_outcomes(candidate)
        if prior_candidate is not None:
            await self._evaluate_runner_transitions(prior_candidate, candidate)
        await self._run_lab_cycle(candidate, now=now, callout=callout)
        await self._flush_provider_usage()
        self.runner_last_evaluated_at = now
        self.runner_last_candidate_mint = mint
        return candidate

    @staticmethod
    def _lab_metadata(callout: CoinCallout | None) -> dict[str, object]:
        """Identity fields already present on the DEX response the runner paid for.

        No description is invented here: DEX Screener publishes no ABOUT text, so
        the card honestly reports that none is available until a documented
        metadata source supplies one.
        """

        if callout is None or not callout.dex.available:
            return {}
        dex = callout.dex
        return {
            "source": "dexscreener",
            "name": (callout.token_info.name if callout.token_info else None),
            "symbol": (callout.token_info.symbol if callout.token_info else None),
            "image": dex.image_url,
            "website": dex.website_url,
            "x_handle": dex.x_handle,
            "telegram": dex.telegram_url,
            "discord": dex.discord_url,
        }

    async def _run_lab_cycle(
        self,
        candidate: RunnerCandidate,
        *,
        now: int,
        callout: CoinCallout | None = None,
    ) -> LabEvaluation | None:
        """Advance the PAPER laboratory for one freshly evaluated candidate.

        This spends no provider budget: it reads only the evidence the runner
        already collected, and writes simulated rows.  A failure here must never
        take down the research pipeline, so it is logged and swallowed.
        """

        if not self.lab_enabled or self.database.connection is None:
            return None
        try:
            await self._refresh_lab_regime(now=now)
            result = await self.lab.evaluate_candidate(
                candidate,
                now=now,
                metadata=self._lab_metadata(callout),
                surfaced=candidate.first_discord_visible_at is not None,
            )
            paper_position = None
            if self.settings.fomo_lab_auto_paper_enabled:
                paper_position = await self.lab.maybe_open_position(result, now=now)
            await self._manage_lab_position(candidate, now=now)
            await self._run_shadow_cycle(
                candidate, result, now=now, strict_entry=paper_position is not None
            )
            return result
        except Exception:
            logger.exception("PAPER laboratory cycle failed for %s", candidate.mint)
            return None

    async def _run_shadow_cycle(
        self,
        candidate: RunnerCandidate,
        result: LabEvaluation,
        *,
        now: int,
        strict_entry: bool,
    ) -> None:
        """Advance the shadow experiment for one freshly evaluated candidate.

        The two research-lane families that have no fast-alert path of their own
        enter here, and every open shadow position for this mint is advanced by
        the same observation.  Like the strict lab, this spends no provider
        budget: it reads evidence the runner already collected, and it never
        raises into the pipeline.

        A STRICT PAPER entry produces its *own* shadow cohort so the two
        families can be compared on identical terms — it never makes the strict
        decision depend on anything shadow did, and shadow eligibility never
        reaches the strict engine.
        """

        if not self.shadow_enabled or self.database.connection is None:
            return
        try:
            if strict_entry:
                await self._run_shadow_signal(
                    self._shadow_signal(
                        candidate,
                        family=FAMILY_STRICT_PAPER,
                        now=now,
                        why=tuple(result.evaluation.decision.reason_codes[:4]),
                    ),
                    now=now,
                    observed_route_impact_percent=(
                        candidate.current.route_price_impact_percent
                    ),
                )
            elif candidate.stage in LAB_QUALIFIED_STAGES:
                await self._run_shadow_signal(
                    self._shadow_signal(
                        candidate,
                        family=FAMILY_QUALIFIED_RESEARCH,
                        now=now,
                        why=tuple(candidate.why_surfaced[:4])
                        or ("the research funnel qualified this candidate",),
                        signal_at=candidate.qualified_at,
                    ),
                    now=now,
                    observed_route_impact_percent=(
                        candidate.current.route_price_impact_percent
                    ),
                )
            await self._manage_shadow_positions(candidate, now=now)
            await self._sweep_stale_shadow_positions(now=now)
        except Exception:
            logger.exception("SHADOW cycle failed for %s", candidate.mint)

    async def _sweep_stale_shadow_positions(self, *, now: int) -> None:
        """Close shadow positions the pipeline has stopped seeing.

        A token that falls out of the radar would otherwise keep an open $10
        position forever and leave the account headline reporting an unrealized
        number nobody could still trade out of.  Throttled, because the sweep
        reads the whole open book and nothing about it is urgent.
        """

        if now - self._shadow_sweep_at < 300:
            return
        self._shadow_sweep_at = now
        for position, assessment in await self.shadow.sweep_stale_positions(now=now):
            await self._publish_shadow_exit(
                position, assessment, candidate=None, now=now
            )

    async def _refresh_lab_regime(self, *, now: int, max_age_seconds: int = 86_400) -> None:
        """Recompute the bounded market regime from already-persisted outcomes.

        Costs no provider request, and is throttled so a busy radar does not
        rebuild it on every candidate.  A weak regime only makes the lab smaller
        and more selective; it never relaxes a safety gate.
        """

        if now - self._lab_regime_refreshed_at < 300:
            return
        self._lab_regime_refreshed_at = now
        rows = await self.database.runner_results_rows()
        samples: list[LabRegimeSample] = []
        seen: set[str] = set()
        for row in rows:
            mint = str(row.get("mint") or "")
            if not mint or mint in seen:
                continue
            first_seen = int(row.get("first_seen_at") or 0)
            if first_seen and now - first_seen > max_age_seconds:
                continue
            horizon = row.get("horizon_seconds")
            if horizon is None:
                continue
            seen.add(mint)
            forward = row.get("market_cap_return_percent")
            samples.append(
                LabRegimeSample(
                    mint=mint,
                    observed_at=int(row.get("observed_at") or first_seen),
                    liquidity_usd=None,
                    forward_return_percent=(
                        Decimal(str(forward)) if forward is not None else None
                    ),
                    max_favourable_percent=(
                        Decimal(str(forward)) if forward is not None else None
                    ),
                    max_adverse_percent=(
                        abs(Decimal(str(forward))) if forward is not None and forward < 0 else None
                    ),
                    route_available=bool(row.get("route_available")),
                    rugged=bool(row.get("rugged")),
                    graduated=bool(row.get("graduated_at")),
                )
            )
        if samples:
            self.lab.update_regime(samples)

    async def _manage_lab_position(self, candidate: RunnerCandidate, *, now: int) -> None:
        """Advance any open simulated position for this mint by one observation."""

        position = await self.lab_store.open_position_for(
            candidate.mint, strategy_version=self._lab_config.strategy_version
        )
        if position is None:
            return
        current = candidate.current
        first = candidate.first
        await self.lab.manage_position(
            position,
            LabExitContext(
                now=now,
                price_usd=current.price_usd,
                market_cap_usd=current.market_cap_usd,
                liquidity_usd=current.liquidity_usd,
                entry_liquidity_usd=first.liquidity_usd,
                momentum_score=candidate.quality.momentum_score,
                organic_score=candidate.quality.organic_score,
                buys=current.buys_5m,
                sells=current.sells_5m,
                volume_usd=current.volume_5m_usd,
                entry_volume_usd=first.volume_5m_usd,
                cluster_supply_percent=candidate.quality.demand.cluster_supply_percent,
                safety_status=candidate.safety.status,
                route_available=current.route_available,
                price_impact_percent=current.sell_route_price_impact_percent,
            ),
        )

    # ------------------------------------------------------------------
    # SHADOW auto-trader (v2.39): SIGNAL -> $10 SIMULATED BUY -> MANAGE -> SELL
    # ------------------------------------------------------------------

    def _shadow_signal(
        self,
        candidate: RunnerCandidate,
        *,
        family: str,
        now: int,
        why: tuple[str, ...] = (),
        signal_at: int | None = None,
        catalyst_state: str = "",
        token_event_confidence: str = "",
        notable_evidence: str = "",
        event_at: int | None = None,
        first_credible_source: str = "",
        catalyst_alert_at: int | None = None,
    ) -> ShadowSignal:
        """Project an already-analysed candidate onto one shadow signal.

        Reads only evidence the runner already collected, so producing a shadow
        signal costs no provider request (section 54).  Nothing here may consult
        anything observed after ``now``.
        """

        current = candidate.current
        first = candidate.first
        moment = signal_at or candidate.radar_first_seen_at or candidate.first_seen_at or now
        return ShadowSignal(
            mint=candidate.mint,
            family=family,
            timestamps=ShadowTimestamps(
                signal_at=moment,
                source_event_at=candidate.chain_created_at or candidate.pair_created_at,
                first_seen_at=candidate.first_seen_at,
                discord_at=candidate.first_discord_visible_at,
                decision_at=now,
            ),
            name=candidate.name or candidate.symbol or "Unknown token",
            symbol=candidate.symbol or "?",
            price_usd=current.price_usd,
            market_cap_usd=current.market_cap_usd,
            liquidity_usd=current.liquidity_usd,
            volume_usd=current.volume_5m_usd,
            buys=current.buys_5m,
            sells=current.sells_5m,
            independent_buyers=candidate.quality.demand.estimated_independent_buyers,
            organic_score=candidate.quality.organic_score,
            momentum_score=candidate.quality.momentum_score,
            safety_status=candidate.safety.status,
            catalyst_state=catalyst_state,
            token_event_confidence=token_event_confidence,
            notable_wallet_evidence=notable_evidence,
            smart_wallet_entries=candidate.estimated_independent_smart_wallets,
            route_available=current.route_available,
            rugged=bool(current.rugged),
            lifecycle_state=candidate.stage,
            graduation_state=classify_graduation(
                graduated_at=candidate.graduated_at,
                graduation_source=candidate.graduation_source,
                pool_liquidity_usd=current.liquidity_usd,
            ),
            detection_market_cap_usd=first.market_cap_usd,
            event_at=event_at,
            first_credible_source=first_credible_source,
            mint_created_at=candidate.chain_created_at or candidate.pair_created_at,
            catalyst_alert_at=catalyst_alert_at,
            why=why or why_you_are_seeing_this(
                ShadowSignal(mint=candidate.mint, family=family)
            ),
        )

    async def _run_shadow_signal(
        self,
        signal: ShadowSignal,
        *,
        now: int,
        observed_route_impact_percent: Decimal | None = None,
        image_url: str = "",
    ) -> bool:
        """Consider one signal and publish the entry card when it fills.

        Never raises into the pipeline: the shadow experiment is research, and a
        failure inside it must not take down discovery, alerts or the strict
        PAPER lab.
        """

        if not self.shadow_enabled or self.database.connection is None:
            return False
        try:
            return await self._shadow_signal_task(
                signal,
                now=now,
                observed_route_impact_percent=observed_route_impact_percent,
                image_url=image_url,
            )
        except Exception:
            logger.exception("SHADOW signal evaluation failed for %s", signal.mint)
            return False

    async def _shadow_signal_task(
        self,
        signal: ShadowSignal,
        *,
        now: int,
        observed_route_impact_percent: Decimal | None = None,
        image_url: str = "",
    ) -> bool:
        decision, position = await self.shadow.consider_signal(
            signal,
            now=now,
            observed_route_impact_percent=observed_route_impact_percent,
        )
        if position is None:
            return False

        paper = position.position
        alert = build_shadow_entry_alert(
            mint=signal.mint,
            name=signal.name,
            symbol=signal.symbol,
            fomo_url=self._fomo_url(signal.mint),
            family=signal.family,
            family_label=FAMILY_LABELS.get(signal.family, signal.family),
            why=signal.why or why_you_are_seeing_this(signal),
            size_usd=decision.size_usd,
            fill_market_cap_usd=paper.entry_market_cap_usd,
            fill_price_usd=paper.entry_price_usd,
            venue=position.venue,
            fill_source=position.fill_source,
            graduation_state=position.graduation_state,
            modeled_cost_usd=paper.entry_costs.total_cost_usd,
            net_objective_usd=self._shadow_config.net_profit_objective_usd,
            signal_to_fill_seconds=(
                max(0, now - signal.timestamps.signal_at)
                if signal.timestamps.signal_at
                else None
            ),
            image_url=image_url,
            position_id=paper.position_id,
        )
        if self.shadow_cards_enabled:
            await self._publish_fast_alert(alert, now=now)
        return True

    async def _manage_shadow_positions(
        self, candidate: RunnerCandidate, *, now: int
    ) -> None:
        """Advance every open shadow position for this mint by one observation.

        A mint can carry one position per signal family, so each is advanced
        independently and each keeps its own cohort attribution.
        """

        if not self.shadow_enabled or self.database.connection is None:
            return
        try:
            positions = await self.shadow_store.open_positions_for_mint(
                candidate.mint, strategy_version=self._shadow_config.strategy_version
            )
        except Exception:
            logger.exception("Could not load shadow positions for %s", candidate.mint)
            return
        if not positions:
            return

        current = candidate.current
        first = candidate.first
        context = LabExitContext(
            now=now,
            price_usd=current.price_usd,
            market_cap_usd=current.market_cap_usd,
            liquidity_usd=current.liquidity_usd,
            entry_liquidity_usd=first.liquidity_usd,
            momentum_score=candidate.quality.momentum_score,
            organic_score=candidate.quality.organic_score,
            buys=current.buys_5m,
            sells=current.sells_5m,
            volume_usd=current.volume_5m_usd,
            entry_volume_usd=first.volume_5m_usd,
            cluster_supply_percent=candidate.quality.demand.cluster_supply_percent,
            entry_cluster_supply_percent=None,
            safety_status=candidate.safety.status,
            route_available=current.route_available,
            price_impact_percent=current.sell_route_price_impact_percent,
        )
        # Section 8: a provider that is down makes safety UNKNOWN, which is a
        # statement about the provider and not about the token.  Passing that
        # distinction through is what stops an outage from half-selling every
        # profitable shadow position.
        safety_degraded = self.tracker_token_risk.degraded or (
            self.discovery is not None and getattr(self.discovery, "degraded", False)
        )
        evidence = ShadowRunnerEvidence(
            independent_buyer_growth=(
                current.verified_unique_buyers - first.verified_unique_buyers
            ),
            volume_ratio=(
                (current.volume_5m_usd / first.volume_5m_usd)
                if first.volume_5m_usd
                else None
            ),
            route_quality="OK" if current.route_available else "POOR",
            safety_provider_degraded=bool(safety_degraded),
            safety_confirmed_fail=candidate.safety.status == "FAIL",
        )
        for position in positions:
            before = len(position.position.exits)
            try:
                updated, assessment = await self.shadow.manage_position(
                    position, context, evidence
                )
                if len(updated.position.exits) <= before:
                    continue
                await self._publish_shadow_exit(
                    updated, assessment, candidate=candidate, now=now
                )
            except Exception:
                logger.exception(
                    "SHADOW position management failed for %s", position.position_id
                )
                continue

    async def _publish_shadow_exit(
        self,
        position: Any,
        assessment: Any,
        *,
        candidate: RunnerCandidate | None,
        now: int,
    ) -> bool:
        if not self.shadow_cards_enabled:
            return False
        journal = position.position.exits[-1]
        net_now = assessment.net.total_net_usd
        alert = build_shadow_exit_alert(
            mint=position.mint,
            name=(candidate.name if candidate else "") or position.mint[:8],
            symbol=(candidate.symbol if candidate else "") or "?",
            fomo_url=self._fomo_url(position.mint),
            family=position.family,
            family_label=FAMILY_LABELS.get(position.family, position.family),
            size_usd=position.position.size_usd,
            entry_market_cap_usd=position.position.entry_market_cap_usd,
            exit_market_cap_usd=(
                candidate.current.market_cap_usd if candidate is not None else None
            ),
            gross_pnl_usd=journal.realized_gross_pnl_usd,
            cost_usd=journal.costs.total_cost_usd,
            net_pnl_usd=journal.realized_net_pnl_usd,
            peak_net_pnl_usd=position.peak_net_pnl_usd,
            given_back_usd=max(Decimal("0"), position.peak_net_pnl_usd - net_now),
            exit_reason=journal.reason_code,
            venue=position.venue,
            fraction_sold=journal.fraction_sold,
            final=journal.final,
            remaining_fraction=position.position.remaining_fraction,
            why=assessment.why,
            position_id=position.position_id,
            sequence=journal.sequence,
        )
        return await self._publish_fast_alert(alert, now=now)

    async def _flush_provider_usage(self) -> None:
        """Drain in-memory client counters into per-feature daily accounting."""

        usage = self.tracker_token_risk.usage_snapshot()
        if usage["calls"] or usage["cache_hits"] or usage["errors"]:
            self.tracker_token_risk.reset_usage()
            await self._record_provider_call(
                "solana_tracker",
                "runner_token_risk",
                calls=int(usage["calls"] or 0),
                cache_hits=int(usage["cache_hits"] or 0),
                errors=int(usage["errors"] or 0),
            )
        # Discovery is the lane that ran ~1,440 failing requests a day, so its
        # spend is now accounted for alongside everything else rather than being
        # invisible until someone reads the logs.
        if self.discovery is not None and hasattr(self.discovery, "usage_snapshot"):
            discovery_usage = self.discovery.usage_snapshot()
            if (
                discovery_usage["calls"]
                or discovery_usage["errors"]
                or discovery_usage["calls_skipped"]
            ):
                self.discovery.reset_usage()
                await self._record_provider_call(
                    "solana_tracker",
                    "wallet_discovery",
                    calls=int(discovery_usage["calls"] or 0),
                    errors=int(discovery_usage["errors"] or 0),
                    calls_skipped=int(discovery_usage["calls_skipped"] or 0),
                )

    async def _resolve_wallet_origin(
        self,
        wallet: str,
        *,
        history_limit: int,
        now: int,
        counter: list[int],
    ) -> tuple[RunnerFundingObservation, bool]:
        """Find a wallet's first transaction and the account that funded it.

        ``getSignaturesForAddress`` returns newest first.  If the bounded page
        comes back short, its last entry really is the wallet's first ever
        transaction, so the funder and the wallet age are exact.  If the page
        is full, the wallet is older than the page and both stay unknown —
        v2.34 treated the oldest entry of a 20-signature page as the funding
        transfer, which is the wrong transaction for any active wallet.

        Returns the observation and whether the trace actually completed.
        """

        signatures = await self.rpc.get_signatures_for_address(wallet, limit=history_limit)
        counter[0] += 1
        if not signatures:
            return RunnerFundingObservation(wallet=wallet), False
        complete = len(signatures) < history_limit
        if not complete:
            return RunnerFundingObservation(wallet=wallet, trace_complete=False), False
        signature = str(signatures[-1].get("signature") or "")
        if not signature:
            return RunnerFundingObservation(wallet=wallet), False
        transaction = await self.rpc.get_transaction(signature)
        counter[0] += 1
        observation = funding_observation_from_transaction(
            transaction,
            wallet=wallet,
            trace_complete=True,
            now=now,
        )
        return observation, True

    async def _collect_runner_forensics(
        self,
        mint: str,
        *,
        raw_unique_buyers: int,
        raw_top10_percent: Decimal | None,
        buyer_first_seen_at: dict[str, int],
        now: int,
        cached: RunnerForensics | None = None,
    ) -> RunnerForensics:
        """Bounded documented-RPC holder/funder trace for promoted candidates only.

        Cost is capped three ways: at most
        ``FOMO_RUNNER_FORENSICS_MAX_WALLETS`` holders are traced, upstream
        tracing stops at ``FOMO_RUNNER_FUNDING_MAX_DEPTH`` hops, and every
        resolved funding edge is cached permanently because a funding
        relationship, once observed, never changes.
        """

        max_wallets = max(4, self.settings.fomo_runner_forensics_max_wallets)
        history_limit = max(10, self.settings.fomo_runner_wallet_history_limit)
        max_depth = max(1, self.settings.fomo_runner_funding_max_depth)
        excluded = frozenset(
            (*INFRASTRUCTURE_ADDRESSES, *self.settings.fomo_runner_excluded_funders)
        )
        counter = [0]
        # Cache hits are counted alongside real calls so the provider diagnostic
        # reflects the caching that actually happens.  Declared here so the
        # error path can report it too.
        cache_hits = [0]
        try:
            largest_rows, supply_row = await asyncio.gather(
                self.rpc.get_token_largest_accounts(mint),
                self.rpc.get_token_supply(mint),
            )
            counter[0] += 2
            token_accounts = [
                str(item.get("address") or "")
                for item in largest_rows[:max_wallets]
                if item.get("address")
            ]
            accounts = await self.rpc.get_multiple_parsed_accounts(token_accounts)
            counter[0] += 1
            supply_raw = Decimal(str(supply_row.get("amount") or 0))
            owner_supply: dict[str, Decimal] = {}
            for largest, account in zip(largest_rows[:max_wallets], accounts, strict=False):
                if not isinstance(account, dict):
                    continue
                info = (((account.get("data") or {}).get("parsed") or {}).get("info") or {})
                owner = str(info.get("owner") or "")
                amount_raw = Decimal(str(largest.get("amount") or 0))
                if owner and owner not in excluded and supply_raw > 0:
                    owner_supply[owner] = owner_supply.get(owner, Decimal("0")) + (
                        amount_raw / supply_raw * Decimal("100")
                    )

            cached_observations = {
                item.wallet: item
                for item in (cached.observations if cached else ())
                if item.trace_complete
            }
            stored_edges = await self.database.cached_funding_edges(list(owner_supply))
            semaphore = asyncio.Semaphore(4)
            new_edges: list[dict[str, Any]] = []
            # The trace is already heavily cached, by the per-mint forensic
            # payload and by the persistent wallet_funding_edges table.  Those
            # hits were never counted, which is why `/fomo quality` reported
            # "cache 0" for a feature that mostly serves from cache.

            async def trace(owner: str, supply_percent: Decimal) -> RunnerFundingObservation:
                bought_at = buyer_first_seen_at.get(owner)
                prior = cached_observations.get(owner)
                if prior is not None:
                    cache_hits[0] += 1
                    return replace(
                        prior,
                        supply_percent=supply_percent,
                        bought_at=prior.bought_at or bought_at,
                        wallet_age_seconds=(
                            max(0, now - prior.first_activity_at)
                            if prior.first_activity_at is not None
                            else prior.wallet_age_seconds
                        ),
                    )
                edge = stored_edges.get(owner)
                if edge and edge.get("trace_complete"):
                    cache_hits[0] += 1
                    first_activity = edge.get("first_activity_at")
                    return RunnerFundingObservation(
                        wallet=owner,
                        funder=edge.get("funder"),
                        funded_at=edge.get("funded_at"),
                        amount_sol=(
                            Decimal(str(edge["amount_sol"]))
                            if edge.get("amount_sol") is not None
                            else None
                        ),
                        bought_at=bought_at,
                        supply_percent=supply_percent,
                        first_activity_at=first_activity,
                        wallet_age_seconds=(
                            max(0, now - int(first_activity))
                            if first_activity is not None
                            else None
                        ),
                        trace_complete=True,
                    )
                try:
                    async with semaphore:
                        observation, resolved = await self._resolve_wallet_origin(
                            owner,
                            history_limit=history_limit,
                            now=now,
                            counter=counter,
                        )
                except (RpcError, ValueError, TypeError):
                    return RunnerFundingObservation(wallet=owner, supply_percent=supply_percent)
                if resolved:
                    new_edges.append(
                        {
                            "wallet": owner,
                            "funder": observation.funder,
                            "funded_at": observation.funded_at,
                            "amount_sol": (
                                float(observation.amount_sol)
                                if observation.amount_sol is not None
                                else None
                            ),
                            "first_activity_at": observation.first_activity_at,
                            "trace_complete": True,
                        }
                    )
                funder = observation.funder if observation.funder not in excluded else None
                return replace(
                    observation,
                    funder=funder,
                    funded_at=observation.funded_at if funder else None,
                    amount_sol=observation.amount_sol if funder else None,
                    supply_percent=supply_percent,
                    bought_at=bought_at,
                )

            observations = list(
                await asyncio.gather(
                    *(trace(owner, percent) for owner, percent in owner_supply.items())
                )
            )
            observations = await self._trace_upstream_funders(
                observations,
                excluded=excluded,
                history_limit=history_limit,
                max_depth=max_depth,
                now=now,
                counter=counter,
                new_edges=new_edges,
            )
            await self.database.store_funding_edges(new_edges)
            await self._record_provider_call(
                "solana_rpc",
                "runner_forensics",
                calls=counter[0],
                cache_hits=cache_hits[0],
            )
            result = summarize_forensics(
                observations,
                raw_unique_buyers=raw_unique_buyers,
                raw_top10_percent=raw_top10_percent,
                checked_at=now,
                excluded_funders=excluded,
                provider_calls=counter[0],
                warnings=(
                    "Bounded top-holder trace; untraced wallets remain independent "
                    "only as unknown.",
                    "Funding links are coordination evidence, not real-person identification.",
                    "Wallet age and funder are reported only where the bounded signature "
                    "page actually reached the wallet's first transaction.",
                    "Creator history is unavailable unless direct public-chain evidence "
                    "identifies it.",
                ),
            )
            return replace(
                result,
                funding_checked_at=(
                    cached.funding_checked_at
                    if cached and cached.funding_checked_at
                    else now
                ),
                dynamic_checked_at=now,
            )
        except (RpcError, ValueError, TypeError) as exc:
            await self._record_provider_call(
                "solana_rpc",
                "runner_forensics",
                calls=counter[0],
                cache_hits=cache_hits[0],
                errors=1,
            )
            return RunnerForensics(
                available=False,
                raw_unique_buyers=raw_unique_buyers,
                warnings=(f"bounded public RPC forensics unavailable: {str(exc)[:160]}",),
                checked_at=now,
                provider_calls=counter[0],
                degraded=True,
            )

    async def _trace_upstream_funders(
        self,
        observations: list[RunnerFundingObservation],
        *,
        excluded: frozenset[str],
        history_limit: int,
        max_depth: int,
        now: int,
        counter: list[int],
        new_edges: list[dict[str, Any]],
    ) -> list[RunnerFundingObservation]:
        """Resolve one bounded hop above funders that only fund a single holder.

        Catches the common shape where one source funds several intermediaries
        which each fund one fresh wallet, so every direct funder differs while
        the upstream source is identical.  Funders that already form a direct
        cluster are skipped: their relationship is established, and paying for
        another hop would buy nothing.
        """

        if max_depth < 2:
            return observations
        counts: dict[str, int] = {}
        for item in observations:
            if item.funder:
                counts[item.funder] = counts.get(item.funder, 0) + 1
        singletons = [funder for funder, count in counts.items() if count == 1]
        budget = max(0, self.settings.fomo_runner_forensics_max_wallets - len(observations))
        targets = singletons[: max(0, min(len(singletons), budget))]
        if not targets:
            return observations
        stored = await self.database.cached_funding_edges(targets)
        semaphore = asyncio.Semaphore(3)

        async def upstream(funder: str) -> tuple[str, str | None]:
            edge = stored.get(funder)
            if edge and edge.get("trace_complete"):
                return funder, edge.get("funder")
            try:
                async with semaphore:
                    observation, resolved = await self._resolve_wallet_origin(
                        funder,
                        history_limit=history_limit,
                        now=now,
                        counter=counter,
                    )
            except (RpcError, ValueError, TypeError):
                return funder, None
            if resolved:
                new_edges.append(
                    {
                        "wallet": funder,
                        "funder": observation.funder,
                        "funded_at": observation.funded_at,
                        "amount_sol": (
                            float(observation.amount_sol)
                            if observation.amount_sol is not None
                            else None
                        ),
                        "first_activity_at": observation.first_activity_at,
                        "trace_complete": True,
                    }
                )
            return funder, observation.funder

        resolved_pairs = dict(await asyncio.gather(*(upstream(item) for item in targets)))
        return [
            replace(
                item,
                upstream_funder=(
                    resolved_pairs.get(item.funder)
                    if item.funder
                    and resolved_pairs.get(item.funder) not in excluded
                    else None
                ),
                funder_depth=2 if item.funder and resolved_pairs.get(item.funder) else 1,
            )
            if item.funder
            else item
            for item in observations
        ]

    async def _record_provider_call(
        self,
        provider: str,
        feature: str,
        *,
        calls: int = 1,
        cache_hits: int = 0,
        errors: int = 0,
        calls_skipped: int = 0,
    ) -> None:
        """Attribute provider spend to a feature; never let accounting break a scan."""

        if self.database.connection is None or not (
            calls or cache_hits or errors or calls_skipped
        ):
            return
        try:
            await self.database.record_provider_call(
                provider=provider,
                feature=feature,
                usage_day=self._x_usage_day(),
                calls=calls,
                cache_hits=cache_hits,
                errors=errors,
                calls_skipped=calls_skipped,
            )
        except Exception:  # pragma: no cover - accounting is never load-bearing
            logger.debug("provider accounting write failed", exc_info=True)

    async def runner_forensic(self, mint: str) -> RunnerCandidate:
        """Run read-only exact-mint forensics; never executes, signs, launches, or spends."""

        return await self.analyze_runner(
            mint,
            refresh_market=True,
            allow_automatic_x=False,
            deep_forensics=True,
        )

    async def verify_runner_x(self, candidate: RunnerCandidate) -> RunnerCandidate:
        """Run one exact-contract official-X lookup through the shared budget guard."""

        social = await self.x_social.snapshot(
            candidate.mint,
            symbol=candidate.symbol,
            name=candidate.name,
            context="fomo_runner_manual",
            free_score=int(candidate.score),
        )
        return await self.analyze_runner(
            candidate.mint,
            refresh_market=False,
            x_evidence=social,
            allow_automatic_x=False,
        )

    async def runner_lab_candidates(
        self,
        *,
        research_test: bool,
    ) -> tuple[RunnerCandidate, ...]:
        """Return current real existing tokens without holding Discord open indefinitely.

        Runner analysis includes several independent public providers.  Evaluating the
        entire nomination list serially meant that a single slow Jupiter/Tracker request
        could leave the deferred slash-command response spinning for minutes.  Prefer a
        genuinely fresh persisted snapshot when the background radar already has one;
        otherwise refresh a bounded set concurrently and enforce a per-token deadline.
        Test mode still bypasses only the *display* floor.
        """

        await self.initialize()
        now = int(time.time())
        cached = list(
            await self.runner_lab_cached_candidates(
                research_test=research_test,
                max_age_seconds=86_400,
            )
        )
        fresh_cached = [
            item for item in cached if now - item.current.captured_at <= 120
        ]
        if fresh_cached:
            return tuple(fresh_cached[: self.settings.fomo_runner_lab_candidates])

        try:
            async with asyncio.timeout(15):
                discovered = await self.dex_screener.trending_mints()
        except Exception:
            # Cached/on-chain nominations below remain usable when DEX discovery has a
            # transient outage.  Individual provider failures are already represented
            # explicitly in each candidate's evidence.
            discovered = ()
        observed = await self.database.recent_observed_token_mints(
            limit=self.settings.fomo_runner_lab_candidates * 2,
        )
        mints = list(
            dict.fromkeys(
                (*discovered, *(item.mint for item in cached), *observed)
            )
        )
        analysis_limit = min(len(mints), self.settings.fomo_runner_lab_candidates)

        async def evaluate(mint: str) -> RunnerCandidate | None:
            try:
                async with asyncio.timeout(22):
                    return await self.analyze_runner(
                        mint,
                        refresh_market=True,
                        allow_automatic_x=False,
                    )
            except Exception:
                return None

        refreshed = await asyncio.gather(*(evaluate(mint) for mint in mints[:analysis_limit]))
        by_mint = {
            item.mint: item
            for item in cached
            if now - item.current.captured_at <= 900
        }
        for item in refreshed:
            if item is None:
                continue
            if not item.current.market_cap_usd and not item.current.price_usd:
                continue
            # A qualified candidate belongs in the pool regardless of the legacy
            # score floor: a genuinely early setup can be interesting long before
            # the old additive score climbs.
            if research_test or (
                (
                    item.stage in USER_FACING_STAGES
                    or item.score >= self.settings.fomo_runner_fast_watch_min_score
                )
                and not item.hard_blockers
            ):
                by_mint[item.mint] = item
        candidates = list(by_mint.values())
        candidates.sort(
            key=lambda item: (item.score, item.current.captured_at),
            reverse=True,
        )
        return tuple(candidates[: self.settings.fomo_runner_lab_candidates])

    async def runner_lab_cached_candidates(
        self,
        *,
        research_test: bool,
        max_age_seconds: int = 86_400,
        limit: int | None = None,
    ) -> tuple[RunnerCandidate, ...]:
        """Read real persisted runner observations without touching a network provider.

        ``limit`` widens the pool for callers that rank before truncating; the
        digest must choose the best few out of the whole watched universe, not
        out of whichever six happen to hold the highest legacy score.
        """

        await self.initialize()
        now = int(time.time())
        keep = limit or self.settings.fomo_runner_lab_candidates
        candidates: list[RunnerCandidate] = []
        rows = await self.database.recent_runner_candidate_payloads(
            now=now,
            max_age_seconds=max_age_seconds,
            limit=keep * 2,
        )
        for raw in rows:
            try:
                item = runner_candidate_from_json(raw)
            except Exception:
                # One unreadable legacy row must not blank the whole observation
                # pool; `/fomo lab mode:test` still has to show the rest.
                continue
            if not item.current.market_cap_usd and not item.current.price_usd:
                continue
            # A qualified candidate belongs in the pool regardless of the legacy
            # score floor: a genuinely early setup can be interesting long before
            # the old additive score climbs.
            if research_test or (
                (
                    item.stage in USER_FACING_STAGES
                    or item.score >= self.settings.fomo_runner_fast_watch_min_score
                )
                and not item.hard_blockers
            ):
                candidates.append(item)
        candidates.sort(
            key=lambda item: (item.score, item.current.captured_at),
            reverse=True,
        )
        return tuple(candidates[:keep])

    async def lab_opportunities(
        self,
        *,
        limit: int = 5,
        max_age_seconds: int = 86_400,
    ) -> tuple[tuple[RunnerCandidate, LabEvaluation], ...]:
        """Rank the strongest *real* setups the lab currently sees.

        Reads only persisted observations, so answering "what do you see right
        now?" costs no provider credits and never relaxes an entry gate: a
        WAIT/REJECT/COOLDOWN candidate is shown with its reasons, not promoted.
        """

        await self.initialize()
        if not self.lab_enabled:
            return ()
        now = int(time.time())
        candidates = await self.runner_lab_cached_candidates(
            research_test=True,
            max_age_seconds=max_age_seconds,
            limit=max(limit * 3, 12),
        )
        results: list[tuple[RunnerCandidate, LabEvaluation]] = []
        for candidate in candidates:
            try:
                evaluation = await self.lab.evaluate_candidate(candidate, now=now)
            except Exception:
                logger.exception("Lab evaluation failed for %s", candidate.mint)
                continue
            results.append((candidate, evaluation))
        # Rank by CURRENT edge, not by the historical opportunity score: a token
        # that once scored 86 but has collapsed must fall below a genuinely
        # accelerating new setup.
        ranked = rank_by_current_edge(
            [
                LabRankedCandidate(
                    mint=candidate.mint,
                    actionability=evaluation.actionability,
                    expected_net_edge_percent=evaluation.decision.expected_net_edge_percent,
                    historical_opportunity_score=candidate.quality.opportunity_score,
                    decision=str(evaluation.decision.decision),
                    lifecycle_state=evaluation.lifecycle.state,
                )
                for candidate, evaluation in results
            ]
        )
        order = {item.mint: index for index, item in enumerate(ranked)}
        results.sort(key=lambda row: order.get(row[0].mint, len(order)))
        if self.settings.fomo_current_radar_suppress_stale:
            # Suppressed is never deleted: these rows stay in results, quality,
            # lifecycle, replay and every forward observation.
            current = [
                row for row in results if not row[1].actionability.suppressed
            ]
            if current:
                results = current
        return tuple(results[:limit])

    async def lab_trades(self, *, limit: int = 10) -> tuple[object, ...]:
        await self.initialize()
        return tuple(await self.lab.trades(limit=limit))

    async def lab_performance(self) -> dict[str, object]:
        await self.initialize()
        return await self.lab.performance()

    async def lab_exit_rows(self, *, limit: int = 15) -> tuple[dict[str, object], ...]:
        await self.initialize()
        return tuple(await self.lab_store.exit_rows(limit=limit))

    async def lab_lifecycle(self, mint: str) -> dict[str, object]:
        """Everything the lab remembers about one exact mint."""

        await self.initialize()
        lifecycle = await self.lab_store.load_lifecycle(mint)
        identity = await self.lab_store.identity_payload(mint)
        decision = await self.lab_store.latest_decision(
            mint, strategy_version=self._lab_config.strategy_version
        )
        open_position = await self.lab_store.open_position_for(
            mint, strategy_version=self._lab_config.strategy_version
        )
        timeline = await self.lab_store.timeline(mint, limit=200)
        signals = await self.lab_store.social_signals_for(mint)
        return {
            "mint": mint,
            "lifecycle": lifecycle,
            "identity": identity,
            "decision": decision,
            "open_position": open_position,
            "event_count": len(timeline),
            "events": timeline.events[-8:],
            "social_signals": tuple(signals),
        }

    async def lab_smart_money(self, mint: str) -> dict[str, object]:
        await self.initialize()
        raw = await self.database.runner_candidate_payload(mint)
        if not raw:
            return {"mint": mint, "available": False}
        candidate = runner_candidate_from_json(raw)
        evaluation = await self.lab.evaluate_candidate(candidate, now=int(time.time()))
        reputations = await self.lab_store.load_reputations(list(candidate.smart_wallets))
        return {
            "mint": mint,
            "available": True,
            "assessment": evaluation.smart_money,
            "reputations": tuple(reputations.values()),
            "wallets": candidate.smart_wallets,
            "decision": evaluation.decision,
        }

    # --- SHADOW reports (sections 33-38, 44) ---------------------------

    async def shadow_account(self) -> Any:
        """The `/fomo shadow` headline: is the $100 account making money?"""

        await self.initialize()
        return await self.shadow.account()

    async def shadow_open_trades(self) -> list[dict[str, Any]]:
        await self.initialize()
        return await self.shadow.open_trades()

    async def shadow_venues(self) -> tuple[Any, ...]:
        await self.initialize()
        return await self.shadow.venues()

    async def shadow_status(self) -> dict[str, Any]:
        await self.initialize()
        status = await self.shadow.status()
        status["live_radar_channel_id"] = self.settings.fomo_live_radar_channel_id
        status["urgent_channel_id"] = self.settings.fomo_urgent_channel_id
        status["fast_watch_enabled"] = self.settings.fomo_fast_watch_publish_enabled
        status["cards_enabled"] = self.shadow_cards_enabled
        return status

    async def early_lane_status(self) -> dict[str, Any]:
        """The early-lane half of the `/fomo realtime` truth panel (section 74)."""

        await self.initialize()
        report = await self.alert_performance()
        performance = report["performance"]
        return {
            "enabled": self.settings.fomo_early_lane_enabled,
            "heads_up_published": self.early_heads_up_published,
            "runners_published": self.early_runners_published,
            "last_early_alert_at": self.last_early_alert_at,
            "last_early_alert_mint": self.last_early_alert_mint,
            "median_first_seen_to_alert_seconds": (
                performance.median_first_seen_to_alert_seconds
            ),
            "median_move_before_alert_percent": (
                performance.median_move_before_alert_percent
            ),
            "early_rate_percent": performance.early_rate_percent,
            "late_alerts": performance.late_alerts,
            "analysis_timeouts": self.runner_analysis_timeouts,
            "analysis_errors": self.runner_analysis_errors,
            "suppressions": report["suppressions"],
            "social": self.social_status(),
            "stream_connected": bool(getattr(self.stream, "connected", False)),
            "stream_subscriptions": int(getattr(self.stream, "subscription_count", 0) or 0),
            "stream_reconnects": int(getattr(self.stream, "reconnects", 0) or 0),
        }

    async def shadow_refusals(self, *, since: int = 0) -> dict[str, int]:
        await self.initialize()
        return await self.shadow.refusals(since=since)

    async def shadow_counterfactuals(self, position_id: str) -> tuple[Any, ...]:
        """All twelve alternative exit policies for one simulated trade.

        Runs entirely on persisted observations, so it costs zero provider
        requests no matter how many policies are compared (section 54).
        """

        await self.initialize()
        return await self.shadow.counterfactuals(position_id)

    # --- early-alert visibility reports (sections 74-77) ----------------

    async def early_runners(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """`/fomo runners` — what is running right now and how early we were."""

        await self.initialize()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for stage in (STAGE_EARLY_RUNNER, STAGE_OPERATOR_HEADS_UP):
            for row in await self.database.alert_stage_rows(stage=stage, limit=limit * 3):
                mint = str(row["mint"])
                if mint in seen:
                    continue
                seen.add(mint)
                rows.append(await self._early_runner_row(mint, row))
                if len(rows) >= limit:
                    return rows
        return rows

    async def _early_runner_row(self, mint: str, alert_row: dict[str, Any]) -> dict[str, Any]:
        timeline = {
            str(item["stage"]): item for item in await self.database.alert_timeline(mint)
        }
        first_seen = timeline.get(STAGE_BOT_FIRST_SEEN, {})
        name, symbol = await self._cached_token_names(mint)
        context = await self._cached_token_context(mint)
        timing = AlertTiming(
            mint=mint,
            first_seen_at=_int_or_none_engine(first_seen.get("occurred_at")),
            alert_at=_int_or_none_engine(alert_row.get("occurred_at")),
            first_seen_market_cap_usd=_engine_decimal(first_seen.get("market_cap_usd")),
            alert_market_cap_usd=_engine_decimal(alert_row.get("market_cap_usd")),
            current_market_cap_usd=_engine_decimal(context.get("market_cap_usd")),
            tier=str(alert_row.get("tier") or ""),
        )
        return {
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "tier": timing.tier,
            "edge_state": str(alert_row.get("edge_state") or ""),
            "timing": timing,
            "liquidity_usd": _engine_decimal(context.get("liquidity_usd")),
            "route_available": bool(context.get("route_available", True)),
        }

    async def runner_timeline(self, mint: str) -> dict[str, Any]:
        """`/fomo runner <mint>` — the full story of one exact mint (section 76)."""

        await self.initialize()
        name, symbol = await self._cached_token_names(mint)
        return {
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "stages": await self.database.alert_timeline(mint),
            "suppressions": await self.database.alert_suppression_rows(mint=mint),
            "narratives": await self.database.narrative_link_rows(mint=mint),
            "shadow": await self.shadow_store.open_positions_for_mint(
                mint, strategy_version=self._shadow_config.strategy_version
            ),
        }

    async def narrative_collisions(self, *, limit: int = 6) -> list[dict[str, Any]]:
        """`/fomo collisions` — same story, different mints (sections 25, 77)."""

        await self.initialize()
        payload: list[dict[str, Any]] = []
        for story in await self.database.narrative_rows(limit=limit * 2):
            narrative_id = str(story["narrative_id"])
            links = await self.database.narrative_link_rows(narrative_id=narrative_id)
            if len(links) < 2:
                continue
            payload.append({"narrative": story, "links": links})
            if len(payload) >= limit:
                break
        return payload

    async def alert_performance(self) -> dict[str, Any]:
        """How often the operator saw the coin before it moved (section 14)."""

        await self.initialize()
        timings: list[AlertTiming] = []
        for stage in (STAGE_EARLY_RUNNER, STAGE_OPERATOR_HEADS_UP):
            for row in await self.database.alert_stage_rows(stage=stage, limit=200):
                mint = str(row["mint"])
                timeline = {
                    str(item["stage"]): item
                    for item in await self.database.alert_timeline(mint)
                }
                first_seen = timeline.get(STAGE_BOT_FIRST_SEEN, {})
                context = await self._cached_token_context(mint)
                timings.append(
                    AlertTiming(
                        mint=mint,
                        first_seen_at=_int_or_none_engine(first_seen.get("occurred_at")),
                        alert_at=_int_or_none_engine(row.get("occurred_at")),
                        first_seen_market_cap_usd=_engine_decimal(
                            first_seen.get("market_cap_usd")
                        ),
                        alert_market_cap_usd=_engine_decimal(row.get("market_cap_usd")),
                        current_market_cap_usd=_engine_decimal(
                            context.get("market_cap_usd")
                        ),
                        tier=str(row.get("tier") or ""),
                    )
                )
        return {
            "performance": summarize_alert_performance(timings, config=self._early_config),
            "suppressions": await self.database.suppression_counts(),
            "heads_up_published": self.early_heads_up_published,
            "runners_published": self.early_runners_published,
            "last_early_alert_at": self.last_early_alert_at,
            "last_early_alert_mint": self.last_early_alert_mint,
        }

    def social_status(self) -> dict[str, Any]:
        """The honest state of the X/social lane (section 31).

        Production diagnostics showed X sitting at zero activity next to a
        generic HEALTHY.  A provider that has never produced a usable signal is
        not healthy — it is off, unconfigured, or unauthenticated, and each of
        those is a different thing for an operator to do something about.
        """

        client = self.x_social
        configured = bool(getattr(client, "bearer_token", None) or getattr(client, "api_key", None))
        enabled = bool(getattr(client, "search_enabled", False))
        error = getattr(client, "last_radar_error", None) or ""
        searches = int(getattr(client, "searches", 0) or 0)

        if not self.settings.x_radar_enabled and not enabled:
            state = "DISABLED_BY_CONFIG"
        elif not configured:
            state = "AUTH_MISSING"
        elif not enabled:
            state = "DISABLED_BY_CONFIG"
        elif not self.settings.x_radar_query.strip():
            state = "NO_SOURCE_CONFIGURED"
        elif "rate" in error.casefold() or "429" in error:
            state = "RATE_LIMITED"
        elif error:
            state = "PROVIDER_DEGRADED"
        elif searches == 0:
            state = "ACTIVE_NO_EVENTS"
        else:
            state = "ACTIVE"
        return {
            "state": state,
            "searches": searches,
            "configured": configured,
            "enabled": enabled,
            "last_error": error,
        }

    # --- profit-first dashboard (sections 21-24) ------------------------

    async def profit_summary(self) -> dict[str, Any]:
        """`/fomo profit` — is the $100 shadow account making money? (§21)"""

        await self.initialize()
        report = await self.shadow.account()
        status = await self.shadow.status()
        weights = await self.shadow.family_weights()
        exits = await self.shadow.exit_quality()

        ranked = [
            (name, item)
            for name, item in report.by_family.items()
            if item.expectancy_usd is not None
        ]
        ranked.sort(
            key=lambda pair: pair[1].expectancy_usd or Decimal("0"), reverse=True
        )
        signals = self.fast_alerts_published
        calls = await self._provider_call_total()
        return {
            "report": report,
            "status": status,
            "weights": weights,
            "exits": exits,
            "best_family": ranked[0][0] if ranked else "",
            "worst_family": ranked[-1][0] if len(ranked) > 1 else "",
            "best_exit_reason": exits.best_reason,
            "worst_exit_reason": exits.worst_reason,
            "premature_exit_rate_percent": exits.premature_rate_percent,
            "provider_calls": calls,
            "signals_published": signals,
            "provider_calls_per_100_signals": provider_cost_per_signals(calls, signals),
        }

    async def profit_signals(self) -> dict[str, Any]:
        """`/fomo profit signals` — families ranked by forward record (§22)."""

        await self.initialize()
        report = await self.shadow.account()
        return {
            "report": report,
            "weights": await self.shadow.family_weights(),
        }

    async def profit_exits(self) -> Any:
        """`/fomo profit exits` — which exit rules cost the most money (§23)."""

        await self.initialize()
        return await self.shadow.exit_quality()

    async def profit_providers(self) -> list[dict[str, Any]]:
        """`/fomo profit providers` — where the money goes (§24)."""

        await self.initialize()
        rows = await self.database.provider_call_rows()
        totals: dict[str, dict[str, int]] = {}
        for row in rows:
            name = str(row.get("provider") or "unknown")
            bucket = totals.setdefault(
                name, {"calls": 0, "cache_hits": 0, "errors": 0, "calls_skipped": 0}
            )
            bucket["calls"] += int(row.get("calls") or 0)
            bucket["cache_hits"] += int(row.get("cache_hits") or 0)
            bucket["errors"] += int(row.get("errors") or 0)
            bucket["calls_skipped"] = bucket.get("calls_skipped", 0) + int(
                row.get("calls_skipped") or 0
            )

        live: dict[str, dict[str, Any]] = {}
        if self.discovery is not None and hasattr(self.discovery, "usage_snapshot"):
            live["solana_tracker"] = self.discovery.usage_snapshot()
        risk = self.tracker_token_risk.usage_snapshot()
        if risk.get("degraded"):
            live.setdefault("solana_tracker", {})["degraded"] = True

        payload: list[dict[str, Any]] = []
        for name in sorted({*totals, *live, *(item.provider for item in PROVIDER_FEATURES)}):
            counters = totals.get(
                name, {"calls": 0, "cache_hits": 0, "errors": 0, "calls_skipped": 0}
            )
            state = ProviderState(
                name=name,
                calls=counters["calls"],
                cache_hits=counters["cache_hits"],
                errors=counters["errors"],
                calls_skipped=(
                    counters.get("calls_skipped", 0)
                    + int(live.get(name, {}).get("calls_skipped") or 0)
                ),
                degraded_until=(
                    time.monotonic() + int(live.get(name, {}).get(
                        "degraded_seconds_remaining"
                    ) or 0)
                ),
                consecutive_failures=int(
                    live.get(name, {}).get("credit_failures") or 0
                ),
                last_error=str(live.get(name, {}).get("last_error") or ""),
            )
            payload.append(
                {
                    "report": build_provider_report(state, now=time.monotonic()),
                    "live": live.get(name, {}),
                }
            )
        return payload

    async def _provider_call_total(self) -> int:
        rows = await self.database.provider_call_rows()
        return sum(int(row.get("calls") or 0) for row in rows)

    async def shadow_latest_counterfactuals(self) -> tuple[str, str, tuple[Any, ...]]:
        await self.initialize()
        return await self.shadow.latest_counterfactuals()

    async def lab_status(self) -> dict[str, object]:
        """Operational state of the laboratory, for the status card."""

        await self.initialize()
        bankroll = await self.lab.bankroll()
        return {
            "enabled": self.lab_enabled,
            "auto_paper": self.settings.fomo_lab_auto_paper_enabled,
            "strategy_version": self._lab_config.strategy_version,
            "config_hash": self._lab_config.config_hash(),
            "bankroll_usd": bankroll.equity_usd,
            "open_positions": bankroll.open_positions,
            "events": await self.lab_store.event_count(),
            "broad_social_radar": self._lab_config.broad_social_radar_enabled,
            "live_execution": False,
        }

    async def _maybe_publish_fresh(self, candidate: RunnerCandidate) -> bool:
        """STAGE 1 -> Discord only when STAGE 2 evidence is already present.

        v2.34 fired this the moment a token was young, alive and not obviously
        rugged, which is a graduation mirror rather than a signal.  The token is
        still admitted to the silent watch at the same instant; only the message
        waits for affirmative evidence, so detection latency is unchanged and
        the alert now means something.
        """

        if not self.settings.fomo_runner_fresh_alert_enabled or not is_fresh_research_worthy(
            candidate,
            max_age_seconds=self.settings.fomo_runner_fresh_max_age_seconds,
        ):
            return False
        if (
            self.settings.fomo_runner_fresh_requires_qualification
            and candidate.stage not in USER_FACING_STAGES
        ):
            return False
        now = int(time.time())
        reserved = await self.database.reserve_runner_alert(
            mint=candidate.mint,
            event_type="FRESH",
            fingerprint="fresh-v1",
            now=now,
        )
        if not reserved:
            return False
        sent = await self.notifier.on_runner_fresh(candidate)
        if sent is False:
            await self.database.release_runner_alert(
                mint=candidate.mint,
                event_type="FRESH",
            )
            return False
        await self._run_shadow_signal(
            self._shadow_signal(
                candidate,
                family=FAMILY_FRESH_RUNNER,
                now=now,
                why=tuple(candidate.why_surfaced[:4]) or ("fresh pair with real activity",),
            ),
            now=now,
            observed_route_impact_percent=candidate.current.route_price_impact_percent,
        )
        visible_at = int(time.time())
        await self.database.mark_runner_visible(
            mint=candidate.mint,
            visible_at=visible_at,
            market_cap_usd=candidate.current.market_cap_usd,
        )
        return True

    # ------------------------------------------------------------------
    # Realtime alpha engine (v2.38): DETECT -> PERSIST -> NOTIFY -> ENRICH
    # ------------------------------------------------------------------

    async def _publish_fast_alert(self, alert: FastAlert, *, now: int) -> bool:
        """Persist the claim, then notify.  Never the other way round.

        The database reservation is what makes a restart, a duplicated stream
        event or a retried coroutine unable to re-publish or re-ping the same
        observation.  Nothing here can authorise an entry: every
        :class:`FastAlert` is ``entry_eligible = False`` by construction.
        """

        if alert.entry_eligible:  # pragma: no cover - structurally impossible
            raise AssertionError("a fast alert can never be entry eligible")
        if self.database.connection is None:
            return False
        try:
            reserved = await self.database.reserve_fast_alert(
                alert_key=alert.alert_key,
                kind=alert.kind,
                mint=alert.mint,
                now=now,
                fingerprint=alert.fingerprint,
                pinged=alert.may_ping,
            )
        except Exception:
            logger.exception("Could not reserve fast alert %s", alert.alert_key)
            return False
        if not reserved:
            self.fast_alerts_suppressed += 1
            return False
        self._fast_alerts[alert.alert_key] = alert
        sent = await self.notifier.on_fast_alert(alert)
        if sent is False:
            self._fast_alerts.pop(alert.alert_key, None)
            with suppress(Exception):
                await self.database.release_fast_alert(alert.alert_key)
            return False
        self.fast_alerts_published += 1
        self.last_fast_alert_at = now
        self.last_fast_alert_kind = alert.kind
        return True

    def _fast_watch_rate_limited(self, now: int) -> bool:
        while self._fast_watch_times and now - self._fast_watch_times[0] >= 3600:
            self._fast_watch_times.popleft()
        return len(self._fast_watch_times) >= self.settings.fomo_fast_watch_max_per_hour

    async def _maybe_publish_fast_watch(self, candidate: RunnerCandidate) -> bool:
        """Close the v2.37 gap: FAST WATCH now actually reaches Discord.

        The verdict was implemented and tested in v2.37 but nothing published
        it, so early acceleration stayed invisible.  It publishes here as
        research visibility only — ``entry_eligible`` is a hard ``False``, the
        missing evidence is named on the card, and the PAPER entry gates are
        untouched.
        """

        if not (
            self.settings.fomo_fast_watch_enabled
            and self.settings.fomo_fast_watch_publish_enabled
        ):
            return False
        now = int(time.time())
        last = self._fast_watch_published.get(candidate.mint)
        if last is not None and now - last < self.settings.fomo_fast_watch_cooldown_seconds:
            return False
        if self._fast_watch_rate_limited(now):
            self.fast_alerts_suppressed += 1
            return False

        signals = signals_from_candidate(candidate, now=now)
        verdict = evaluate_fast_watch(
            signals,
            min_score=self.settings.fomo_fast_watch_min_score,
            config=self._lab_config,
        )
        if not verdict.watch:
            return False
        # A candidate that sat in a queue must not publish as "early" after the
        # move already happened.
        current, reason = still_current(
            signals,
            first_seen_at=candidate.radar_first_seen_at or candidate.first_seen_at,
            max_queue_age_seconds=self.settings.fomo_fast_watch_max_queue_age_seconds,
        )
        if not current:
            logger.info("FAST WATCH suppressed for %s: %s", candidate.mint[:8], reason)
            self.fast_alerts_suppressed += 1
            return False

        alert = build_fast_watch_alert(
            mint=candidate.mint,
            name=candidate.name or candidate.symbol or "Unknown token",
            symbol=candidate.symbol or "?",
            fomo_url=self._fomo_url(candidate.mint),
            verdict=verdict,
            age_seconds=signals.pair_age_seconds,
            market_cap_usd=signals.market_cap_usd,
            first_seen_market_cap_usd=signals.first_seen_market_cap_usd,
            liquidity_usd=signals.liquidity_usd,
            move_since_first_seen_percent=signals.price_change_percent,
            momentum_score=candidate.quality.momentum_score or None,
            organic_score=candidate.quality.organic_score or None,
            buys=signals.buys,
            sells=signals.sells,
            now=now,
        )
        published = await self._publish_fast_alert(alert, now=now)
        if published:
            self._fast_watch_published[candidate.mint] = now
            self._fast_watch_times.append(now)
            self._schedule_alert_enrichment(alert)
            # The same verdict that earned the radar card also feeds the shadow
            # experiment, so "would a $10 buy on FAST WATCH have made money?"
            # is answerable from forward data rather than from opinion.
            await self._run_shadow_signal(
                self._shadow_signal(
                    candidate,
                    family=FAMILY_FAST_WATCH,
                    now=now,
                    why=tuple(verdict.reasons),
                ),
                now=now,
                observed_route_impact_percent=(
                    candidate.current.route_price_impact_percent
                ),
            )
        return published

    async def _forward_ping_verdict(
        self,
        *,
        family: str,
        edge_inputs: ForwardEdgeInputs,
        independent_confirmations: int,
        still_early: bool = True,
        move_already_made_percent: Decimal | None = None,
        now: int,
    ) -> PingVerdict:
        """Section 18: an interruption has to earn itself.

        This only ever *withholds* a ping the existing rules already allowed —
        it can never create one — so the ping surface can only get quieter and
        more selective as forward evidence accumulates.
        """

        weights = await self.shadow.cached_family_weights(now=now)
        edge = forward_edge_score(edge_inputs, weights=weights)
        return should_ping(
            edge,
            family=family,
            independent_confirmations=independent_confirmations,
            still_early=still_early,
            move_already_made_percent=move_already_made_percent,
            weights=weights,
        )

    def _fomo_url(self, mint: str) -> str:
        """The same canonical Fomo coin link every other card already uses."""

        return fomo_coin_url(mint, self.settings.fomo_referral_code)

    # --- notable wallet intelligence -----------------------------------

    async def _notable_wallet(
        self, address: str, *, alias: str = ""
    ) -> NotableWallet | None:
        """Resolve a wallet to a *verified* public identity, or an honest anon.

        An identity is never inferred.  An operator-defined alias is an
        admin-defined label and is used as one; anything else stays anonymous
        with a stable handle, and no attempt is made to work out who it is.
        """

        rows = (
            await self.database.notable_wallet_rows(enabled_only=True)
            if self.database.connection is not None
            else []
        )
        for row in rows:
            if str(row.get("wallet")) != address:
                continue
            provenance = str(row.get("provenance") or ONCHAIN_ONLY)
            if provenance not in PROVENANCE:
                provenance = ONCHAIN_ONLY
            label = str(row.get("label") or "")
            if provenance == ONCHAIN_ONLY:
                label = ""
            return NotableWallet(
                wallet=address,
                label=label,
                provenance=provenance,
                verification_source=str(row.get("verification_source") or ""),
                confidence=Decimal(str(row.get("confidence") or "0")),
                category=str(row.get("category") or "trader"),
                enabled=bool(row.get("enabled", 1)),
                last_verified_at=row.get("last_verified_at"),
                anonymous_index=row.get("anonymous_index"),
            )
        if alias:
            # The operator named this wallet when they added it to the tracked
            # set; that is a documented, admin-defined mapping, not a guess.
            return NotableWallet(
                wallet=address,
                label=alias,
                provenance=ADMIN_DEFINED,
                verification_source="operator-tracked wallet",
                category="trader",
            )
        return NotableWallet(
            wallet=address,
            provenance=ONCHAIN_ONLY,
            anonymous_index=self._anonymous_index(address),
        )

    def _anonymous_index(self, address: str) -> int:
        """A stable, meaningless handle number.  It identifies nobody."""

        index = self._notable_anonymous_index.get(address)
        if index is None:
            index = len(self._notable_anonymous_index) + 1
            self._notable_anonymous_index[address] = index
        return index

    async def _wallet_reputation(self, address: str) -> WalletReputation | None:
        try:
            return (await self.lab_store.load_reputations([address])).get(address)
        except Exception:
            logger.exception("Could not load reputation for a notable wallet")
            return None

    async def _maybe_publish_notable(
        self, swap: DetectedSwap, trader: TrackedTrader
    ) -> bool:
        """The realtime notable-wallet fast path (sections 5-11).

        Persist the observation immediately, publish a small card built only
        from evidence already in hand, and let stage-2 enrichment fill the rest
        in place.  Lateness is quantified and published, never hidden, and a
        late observation never earns a ping.
        """

        if not self.settings.fomo_notable_alerts_enabled or swap.side is not Side.BUY:
            return False
        if (
            swap.usd_value is None
            or swap.usd_value < self.settings.fomo_notable_min_trade_usd
        ):
            return False
        now = int(time.time())

        # PERSIST first: a slow provider must never delay the record, and a
        # replayed stream event must never produce a second row.
        try:
            fresh = await self.database.record_notable_event(
                signature=swap.signature,
                wallet=swap.trader_address,
                mint=swap.token_mint,
                side=swap.side.value,
                chain_time=swap.block_time,
                observed_at=now,
                amount_usd=float(swap.usd_value),
                entry_price_usd=(
                    float(swap.token_price_usd) if swap.token_price_usd is not None else None
                ),
            )
        except Exception:
            logger.exception("Could not persist a notable wallet event")
            return False
        if not fresh:
            return False

        context = await self._cached_token_context(swap.token_mint)
        # The trader's entry market cap comes from the trade's own executed
        # price; the detection market cap must be a *live* reading, never the
        # last persisted one, or a cached number minutes old would be published
        # as "now" and manufacture a move that did not happen.
        entry_market_cap = self._entry_market_cap(swap, context)
        detection_market_cap: Decimal | None = None
        with suppress(Exception):
            async with asyncio.timeout(3):
                snapshot = await self.dex_screener.snapshot(swap.token_mint)
            if snapshot.available:
                detection_market_cap = snapshot.market_cap_usd
        if detection_market_cap is None:
            # No live reading in the budget: the honest statement is that the
            # bot arrived at the trade's own level, which is what a detection
            # seconds after the chain event means.  Enrichment refreshes it.
            detection_market_cap = entry_market_cap

        profile = await self._notable_wallet(swap.trader_address, alias=trader.alias)
        if profile is None:
            return False
        trade = NotableTrade(
            wallet=swap.trader_address,
            mint=swap.token_mint,
            signature=swap.signature,
            side=swap.side.value,
            chain_time=swap.block_time,
            observed_at=now,
            amount_usd=swap.usd_value,
            entry_price_usd=swap.token_price_usd,
            entry_market_cap_usd=entry_market_cap,
        )
        signal = NotableSignal(
            trade=trade,
            wallet_profile=profile,
            reputation=await self._wallet_reputation(swap.trader_address),
            detection_market_cap_usd=detection_market_cap,
            current_price_usd=swap.token_price_usd,
            current_market_cap_usd=detection_market_cap,
            now=now,
        )
        consensus = self._notable_consensus(signal, now=now)
        ping = decide_ping(signal, consensus=consensus, config=self._lab_config)
        if not self.settings.fomo_notable_ping_enabled:
            ping = replace(ping, ping=False, urgent=False)
        if ping.ping and self.settings.fomo_forward_ping_gate_enabled:
            # A famous wallet is not by itself a reason to interrupt someone.
            verdict = await self._forward_ping_verdict(
                family=(
                    FAMILY_NOTABLE_EARLY
                    if signal.may_chase()
                    else FAMILY_NOTABLE_LATE
                ),
                edge_inputs=ForwardEdgeInputs(
                    family=FAMILY_NOTABLE_EARLY,
                    freshness_seconds=signal.signal_age_seconds,
                    liquidity_usd=_engine_decimal(context.get("liquidity_usd")),
                    route_available=bool(context.get("route_available", True)),
                    notable_lead_percent=signal.move_since_trader_entry_percent,
                ),
                independent_confirmations=max(
                    1, getattr(consensus, "independent_wallets", 1)
                ),
                still_early=signal.may_chase(),
                move_already_made_percent=signal.move_since_trader_entry_percent,
                now=now,
            )
            if not verdict.ping:
                ping = replace(
                    ping,
                    ping=False,
                    urgent=False,
                    reason="; ".join(verdict.blockers[:2]) or ping.reason,
                )
                self.forward_pings_withheld += 1

        # Section 7: a wallet earns the louder headline from its forward record,
        # never from its size.  ``MIN_PROVEN_SAMPLES`` is part of the question —
        # PROVEN_EARLY on three observations is a coincidence with a label.
        proven = bool(
            signal.reputation_state in {"PROVEN_EARLY", "USEFUL_CONFIRMATION"}
            and int(getattr(signal.reputation, "samples", 0) or 0) >= MIN_PROVEN_SAMPLES
        )
        alert = build_notable_trader_alert(
            signal=signal,
            fomo_url=self._fomo_url(swap.token_mint),
            name=str(context.get("name") or "Unknown token"),
            symbol=str(context.get("symbol") or "?"),
            consensus=consensus if consensus.raw_wallets > 1 else None,
            ping_decision=ping,
            token_state=self._token_lifecycle_state(swap.token_mint),
            story_summary=str(context.get("story_summary") or ""),
            safety_status=str(context.get("safety_status") or "UNKNOWN"),
            proven=proven,
            terminal_url=self._terminal_url(swap.token_mint),
        )
        published = await self._publish_fast_alert(alert, now=now)
        if published:
            self._schedule_alert_enrichment(alert)
            await self._run_shadow_notable(signal, alert=alert, context=context, now=now)
            # Section 29: a known wallet entering a hot-watched candidate is news
            # now, not at the next timer tick.  Re-evaluate promotion immediately.
            if swap.side.value == "BUY":
                with suppress(Exception):
                    await self.note_early_watch_event(
                        swap.token_mint, trigger="known_trader_buy"
                    )
        return published

    def _token_lifecycle_state(self, mint: str) -> str:
        """Where this exact mint sits in the pipeline right now (section 6)."""

        if mint in self._early_watches:
            return "HOT WATCH"
        with suppress(Exception):
            if self.trending.is_hot_watched(mint):
                return "TRENDING HOT WATCH"
            if self.trending.entry_for(mint) is not None:
                return "TRENDING"
        if mint in self._early_published:
            return "EARLY"
        return ""

    async def _run_shadow_notable(
        self,
        signal: NotableSignal,
        *,
        alert: FastAlert,
        context: dict[str, Any],
        now: int,
    ) -> bool:
        """Feed a notable-wallet observation to the shadow experiment.

        Early and late observations are separate cohorts on purpose: the whole
        question section 18 asks is whether smart-money intelligence arrives
        early enough to be worth acting on, and blending them would hide the
        answer.
        """

        trade = signal.trade
        family = (
            FAMILY_NOTABLE_EARLY if alert.kind == "NOTABLE_TRADER_EARLY"
            else FAMILY_NOTABLE_LATE
        )
        price = signal.current_price_usd or trade.entry_price_usd
        shadow_signal = ShadowSignal(
            mint=trade.mint,
            family=family,
            timestamps=ShadowTimestamps(
                signal_at=trade.chain_time or trade.observed_at or now,
                source_event_at=trade.chain_time,
                first_seen_at=trade.observed_at,
                decision_at=now,
            ),
            name=str(context.get("name") or "Unknown token"),
            symbol=str(context.get("symbol") or "?"),
            price_usd=price,
            market_cap_usd=signal.current_market_cap_usd,
            liquidity_usd=_engine_decimal(context.get("liquidity_usd")),
            safety_status="UNKNOWN",
            notable_wallet_evidence=(
                f"{signal.display_name} ({signal.reputation_state}) bought "
                f"{trade.amount_usd}"
            ),
            smart_wallet_entries=1,
            route_available=bool(context.get("route_available", True)),
            rugged=bool(context.get("rugged", False)),
            trader_entry_market_cap_usd=trade.entry_market_cap_usd,
            detection_market_cap_usd=signal.detection_market_cap_usd,
            why=(
                f"{signal.display_name} ({signal.reputation_state}) entered",
                f"observed {trade.detection_delay_seconds}s after the chain event"
                if trade.detection_delay_seconds is not None
                else f"freshness {signal.freshness()}",
            ),
        )
        return await self._run_shadow_signal(shadow_signal, now=now)

    def _queue_notable_alert(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        """Run the fast alert beside the pipeline, never in front of it."""

        if not self.settings.fomo_notable_alerts_enabled or swap.side is not Side.BUY:
            return
        task = asyncio.create_task(
            self._notable_alert_task(swap, trader),
            name=f"notable-{swap.signature[:8]}",
        )
        self._notable_tasks.add(task)
        task.add_done_callback(self._notable_tasks.discard)

    async def _notable_alert_task(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        try:
            await self._maybe_publish_notable(swap, trader)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an alert must never break a scan
            await self.notifier.on_error("Notable wallet fast alert", exc)

    @staticmethod
    def _entry_market_cap(swap: DetectedSwap, context: dict[str, Any]) -> Decimal | None:
        """The trader's entry market cap, derived from two measured values.

        Circulating supply is implied by the last persisted price and market cap
        for this mint, so the trade's own executed price gives the market cap at
        the moment the trader entered.  When either measurement is missing the
        answer stays ``None`` and the card says "unknown" — it is never filled
        in with the detection-time market cap, which would silently claim the
        trader entered where the bot arrived.
        """

        price = context.get("price_usd")
        market_cap = context.get("market_cap_usd")
        entry_price = swap.token_price_usd
        if not (price and market_cap and entry_price) or price <= 0:
            return None
        supply = Decimal(market_cap) / Decimal(price)
        return (Decimal(entry_price) * supply).quantize(Decimal("0.01"))

    def _notable_consensus(self, signal: NotableSignal, *, now: int) -> NotableConsensus:
        """Cluster-adjusted consensus over the recent notable buys of one mint.

        Each remembered signal keeps its *own* wallet profile and reputation, so
        a PROVEN_EARLY count can never be manufactured by reusing one wallet's
        history for another.
        """

        window = max(60, self.settings.fomo_notable_max_signal_age_seconds)
        recent = [
            item
            for item in self._notable_recent.get(signal.trade.mint, [])
            if now - item.trade.observed_at <= window
            and item.trade.wallet != signal.trade.wallet
        ]
        recent.append(signal)
        self._notable_recent[signal.trade.mint] = recent[-20:]
        if len(self._notable_recent) > 500:
            self._notable_recent = dict(list(self._notable_recent.items())[-250:])
        return build_consensus(
            recent,
            current_market_cap_usd=signal.current_market_cap_usd,
        )

    async def _cached_token_context(self, mint: str) -> dict[str, Any]:
        """Identity and last-known market values from what we already stored.

        Deliberately free: the fast path reads the persisted runner row rather
        than spending a provider call before it publishes.
        """

        with suppress(Exception):
            payload = await self.database.runner_candidate_payload(mint)
            if payload:
                candidate = runner_candidate_from_json(payload)
                if candidate is not None:
                    return {
                        "name": candidate.name or candidate.symbol or "Unknown token",
                        "symbol": candidate.symbol or "?",
                        "price_usd": candidate.current.price_usd,
                        "market_cap_usd": candidate.current.market_cap_usd,
                        # Read from the same persisted row, so the shadow lane
                        # can judge route feasibility without a provider call.
                        "liquidity_usd": candidate.current.liquidity_usd,
                        "route_available": candidate.current.route_available,
                        "rugged": candidate.current.rugged,
                    }
        return {}

    async def _cached_token_names(self, mint: str) -> tuple[str, str]:
        context = await self._cached_token_context(mint)
        return (
            str(context.get("name") or "Unknown token"),
            str(context.get("symbol") or "?"),
        )

    # --- catalyst and confluence ---------------------------------------

    def _catalyst_key(self, alert: NewsAlert) -> str:
        terms = sorted({item.casefold() for item in alert.narrative_terms if item})
        if not terms:
            return ""
        return hashlib.sha256("|".join(terms).encode()).hexdigest()[:16]

    def _event_source(self, alert: NewsAlert) -> EventSource:
        """Grade one publication of a claim.  Primary is never assumed."""

        handle = (alert.author or alert.source).strip()
        tier = account_tier(handle)
        return EventSource(
            name=handle or alert.source or "unknown",
            url=alert.url,
            published_at=alert.created_at or alert.received_at,
            # Primary means the authoritative account itself published it.  A
            # verified Tier-A account is the only thing we treat that way, and
            # an absent primary demotes the event rather than being assumed.
            is_primary=bool(alert.author_verified and tier == "TIER_A_OFFICIAL"),
            account_verified=alert.author_verified,
            tier=tier,
            content_hash=hashlib.sha256(
                (alert.headline or alert.summary or alert.url).casefold().encode()
            ).hexdigest()[:16],
        )

    async def observe_catalyst(
        self, alert: NewsAlert, *, now: int | None = None
    ) -> CatalystEvent | None:
        """Fold one news observation into a graded, persisted catalyst event."""

        if not self.settings.fomo_catalyst_alerts_enabled:
            return None
        key = self._catalyst_key(alert)
        if not key:
            return None
        moment = now if now is not None else int(time.time())
        horizon = self.settings.fomo_catalyst_max_event_age_seconds
        self._catalyst_headlines = {
            event_id: value
            for event_id, value in self._catalyst_headlines.items()
            if moment - value[0] <= horizon
        }
        self._catalyst_sources = {
            event_id: sources
            for event_id, sources in self._catalyst_sources.items()
            if event_id in self._catalyst_headlines
        }
        first_seen, headline = self._catalyst_headlines.get(
            key, (moment, alert.headline or alert.summary or alert.url)
        )
        self._catalyst_headlines[key] = (first_seen, headline)
        sources = self._catalyst_sources.setdefault(key, [])
        candidate = self._event_source(alert)
        if not any(
            item.name == candidate.name and item.content_hash == candidate.content_hash
            for item in sources
        ):
            sources.append(candidate)

        event = assess_event(
            CatalystEvent(
                event_id=key,
                headline=headline,
                detected_at=first_seen,
                occurred_at=min(
                    (item.published_at for item in sources if item.published_at), default=None
                ),
                sources=tuple(sources),
                discussion_velocity=self._discussion_velocity(sources, first_seen, moment),
                novelty=self._event_novelty(first_seen, moment, horizon=horizon),
                crypto_relevance=Decimal(str(alert.score)) if alert.score else None,
            ),
            now=moment,
            max_age_seconds=horizon,
        )
        await self._store_catalyst_event(event, now=moment)
        return event

    async def _store_catalyst_event(self, event: CatalystEvent, *, now: int) -> None:
        """Persist the graded event.  A token link is meaningless without it."""

        with suppress(Exception):
            await self.database.store_catalyst_event(
                event_id=event.event_id,
                headline=event.headline,
                detected_at=event.detected_at,
                occurred_at=event.occurred_at,
                confidence=event.confidence,
                priority=event.priority,
                markers_json=json.dumps(list(event.markers)),
                payload_json=json.dumps(
                    {
                        "sources": [
                            {
                                "name": item.name,
                                "url": item.url,
                                "published_at": item.published_at,
                                "is_primary": item.is_primary,
                                "tier": item.tier,
                            }
                            for item in event.sources
                        ],
                        "independent_confirmations": event.independent_confirmations,
                    }
                ),
                now=now,
            )

    @staticmethod
    def _discussion_velocity(
        sources: Sequence[EventSource], first_seen: int, now: int
    ) -> Decimal | None:
        """How fast distinct outlets are picking the story up — measured, not guessed.

        Counts distinct source names over the elapsed window.  Three separate
        outlets inside a minute is a fast-moving story; one over an hour is not.
        """

        distinct = len({item.name.casefold() for item in sources if item.name})
        if distinct <= 0:
            return None
        minutes = max(Decimal("1"), Decimal(max(60, now - first_seen)) / 60)
        rate = Decimal(distinct) / minutes
        return min(Decimal("100"), (rate * 40).quantize(Decimal("0.01")))

    @staticmethod
    def _event_novelty(first_seen: int, now: int, *, horizon: int) -> Decimal:
        """How new this story is to us, measured from our own first observation.

        Deliberately not a judgement about how surprising the news is: it is the
        age of the claim in our own records, decaying to zero at the retention
        horizon, so a story we have been carrying for an hour stops presenting
        itself as breaking.
        """

        age = max(0, now - first_seen)
        if age <= 300:
            return Decimal("100")
        if age >= horizon:
            return Decimal("0")
        remaining = Decimal(horizon - age) / Decimal(max(1, horizon - 300))
        return (remaining * 100).quantize(Decimal("0.01"))

    async def _maybe_publish_catalyst(
        self,
        event: CatalystEvent,
        *,
        now: int,
        mint: str = "",
        link: TokenEventLink | None = None,
        candidate: RunnerCandidate | None = None,
        confluence: ConfluenceInputs | None = None,
    ) -> bool:
        """BREAKING CATALYST / CATALYST WATCH / CONFLUENCE WATCH."""

        if not self.settings.fomo_catalyst_alerts_enabled:
            return False
        inputs = confluence or ConfluenceInputs(event=event, link=link)
        decision: CatalystAlert = classify_catalyst_alert(
            inputs, now=now, config=self._lab_config
        )
        if not decision.alerts:
            return False
        if (
            decision.kind == "CONFLUENCE_WATCH"
            and not self.settings.fomo_confluence_alerts_enabled
        ):
            return False
        if not self.settings.fomo_catalyst_ping_enabled:
            decision = replace(decision, ping=False)
        if decision.ping and self.settings.fomo_forward_ping_gate_enabled:
            # A real story still has to be confirmed by the market before it is
            # allowed to interrupt anyone (sections 5, 18).
            verdict = await self._forward_ping_verdict(
                family={
                    "BREAKING_CATALYST": FAMILY_BREAKING_CATALYST,
                    "CATALYST_WATCH": FAMILY_CATALYST_WATCH,
                    "CONFLUENCE_WATCH": FAMILY_CONFLUENCE_WATCH,
                }.get(decision.kind, FAMILY_CATALYST_WATCH),
                edge_inputs=ForwardEdgeInputs(
                    family=FAMILY_CONFLUENCE_WATCH,
                    freshness_seconds=inputs.token_age_seconds,
                    liquidity_usd=(
                        candidate.current.liquidity_usd if candidate is not None else None
                    ),
                    independent_buyers=inputs.independent_notable_wallets or None,
                    organic_score=inputs.organic_score,
                    actionability_score=inputs.current_actionability,
                    catalyst_confidence=str(event.confidence),
                    route_available=(
                        candidate.current.route_available if candidate is not None else True
                    ),
                ),
                independent_confirmations=max(
                    event.independent_confirmations,
                    inputs.independent_notable_wallets,
                ),
                now=now,
            )
            if not verdict.ping:
                decision = replace(
                    decision,
                    ping=False,
                    ping_reason="; ".join(verdict.blockers[:2]),
                )
                self.forward_pings_withheld += 1
        current = candidate.current if candidate is not None else None
        name, symbol = ("", "")
        if candidate is not None:
            name = candidate.name or candidate.symbol or "Unknown token"
            symbol = candidate.symbol or "?"
        elif mint:
            name, symbol = await self._cached_token_names(mint)
        alert = build_catalyst_alert(
            alert=decision,
            event=event,
            link=link,
            mint=mint,
            name=name,
            symbol=symbol,
            fomo_url=self._fomo_url(mint) if mint else "",
            token_age_seconds=inputs.token_age_seconds,
            market_cap_usd=getattr(current, "market_cap_usd", None),
            liquidity_usd=getattr(current, "liquidity_usd", None),
            notable_summary=(
                f"Independent notable wallets `{inputs.independent_notable_wallets}`"
                if inputs.independent_notable_wallets
                else ""
            ),
        )
        if mint and link is not None:
            # The link row is joined against the event row, so persist the event
            # first regardless of which entry point produced it.
            await self._store_catalyst_event(event, now=now)
            with suppress(Exception):
                await self.database.store_catalyst_link(
                    event_id=event.event_id,
                    mint=mint,
                    connection=link.connection,
                    name_similarity=(
                        float(link.name_similarity) if link.name_similarity is not None else None
                    ),
                    seconds_after_event=link.seconds_after_event,
                    official=link.official,
                    payload_json=json.dumps({"notes": list(link.notes)}),
                    now=now,
                )
        published = await self._publish_fast_alert(alert, now=now)
        if published and mint:
            self._schedule_alert_enrichment(alert)
            if candidate is not None:
                family = {
                    "BREAKING_CATALYST": FAMILY_BREAKING_CATALYST,
                    "CATALYST_WATCH": FAMILY_CATALYST_WATCH,
                    "CONFLUENCE_WATCH": FAMILY_CONFLUENCE_WATCH,
                }.get(alert.kind, FAMILY_CATALYST_WATCH)
                await self._run_shadow_signal(
                    self._shadow_signal(
                        candidate,
                        family=family,
                        now=now,
                        why=tuple(decision.reasons[:4]),
                        catalyst_state=str(event.confidence),
                        token_event_confidence=(
                            link.connection if link is not None else "NO_EVIDENCE"
                        ),
                        event_at=event.occurred_at or event.detected_at,
                        first_credible_source=next(
                            (item.name for item in event.sources if item.is_primary), ""
                        ),
                        catalyst_alert_at=now,
                    ),
                    now=now,
                    observed_route_impact_percent=(
                        candidate.current.route_price_impact_percent
                    ),
                )
        return published

    async def evaluate_catalyst_token(
        self,
        *,
        mint: str,
        event: CatalystEvent,
        now: int | None = None,
    ) -> bool:
        """Correlate a fresh token with a graded event, then decide the alert."""

        moment = now if now is not None else int(time.time())
        payload = await self.database.runner_candidate_payload(mint)
        candidate = runner_candidate_from_json(payload) if payload else None
        if candidate is None:
            return False
        created = candidate.pair_created_at or candidate.chain_created_at
        reference = event.occurred_at if event.occurred_at is not None else event.detected_at
        # A token that existed *before* the event cannot have been created for
        # it, so the sign of this difference matters and is never clamped away.
        delta = created - reference if created else None
        link = assess_token_link(
            mint=mint,
            event=event,
            name_similarity=self._name_similarity(candidate, event),
            minted_after_event=None if delta is None else delta >= 0,
            seconds_after_event=delta if delta is not None and delta >= 0 else None,
        )
        evaluation = await self.lab.evaluate_candidate(candidate, now=moment)
        inputs = ConfluenceInputs(
            event=event,
            link=link,
            token_age_seconds=(
                max(0, moment - created) if created else None
            ),
            independent_notable_wallets=candidate.estimated_independent_smart_wallets,
            proven_early_wallets=evaluation.smart_money.proven_early,
            current_market_cap_usd=candidate.current.market_cap_usd,
            independent_buyers_accelerating=(
                candidate.current.verified_unique_buyers > candidate.first.verified_unique_buyers
            ),
            liquidity_growing=(
                candidate.current.liquidity_usd is not None
                and candidate.first.liquidity_usd is not None
                and candidate.current.liquidity_usd > candidate.first.liquidity_usd
            ),
            organic_score=candidate.quality.organic_score,
            current_actionability=evaluation.actionability.score,
            safety_status=candidate.safety.status,
        )
        return await self._maybe_publish_catalyst(
            event,
            now=moment,
            mint=mint,
            link=link,
            candidate=candidate,
            confluence=inputs,
        )

    @staticmethod
    def _name_similarity(candidate: RunnerCandidate, event: CatalystEvent) -> Decimal | None:
        """Token-name overlap with the headline, on the grader's 0-100 scale.

        Evidence, never proof: a perfect name match still grades no higher than
        PLAUSIBLE, because anyone can name a token after a real event.
        """

        name = (candidate.name or candidate.symbol or "").casefold()
        if not name:
            return None
        words = {item for item in name.replace("-", " ").split() if len(item) >= 3}
        if not words:
            words = {name}
        headline = event.headline.casefold()
        hits = sum(1 for word in words if word in headline)
        if not hits:
            return Decimal("0")
        return (Decimal(hits) / Decimal(len(words)) * Decimal("100")).quantize(Decimal("0.01"))

    # --- stage 2: async enrichment in place ----------------------------

    def _schedule_alert_enrichment(self, alert: FastAlert) -> None:
        if not self.settings.fomo_alert_enrichment_enabled or not alert.mint:
            return
        task = asyncio.create_task(
            self._enrich_fast_alert(alert),
            name=f"fast-enrich-{alert.mint[:8]}",
        )
        self._enrichment_tasks.add(task)
        task.add_done_callback(self._enrichment_tasks.discard)

    async def _enrich_fast_alert(self, alert: FastAlert) -> None:
        """Stage 2: edit the published card with real evidence, never re-ping.

        A degraded provider becomes an explicit UNKNOWN on the card.  Missing
        evidence never becomes PASS, and enrichment can never make a fast alert
        entry eligible.
        """

        await asyncio.sleep(self.settings.fomo_alert_enrichment_delay_seconds)
        degraded = ""
        candidate: RunnerCandidate | None = None
        try:
            candidate = await self.analyze_runner(alert.mint, refresh_market=True)
        except (DiscoveryError, JupiterError, RpcError, ValueError) as exc:
            degraded = "market data"
            logger.info("Fast alert enrichment degraded for %s: %s", alert.mint[:8], exc)
        except Exception:
            degraded = "market data"
            logger.exception("Fast alert enrichment failed for %s", alert.mint[:8])

        if candidate is None:
            update = enrichment_from_evidence(
                alert_key=alert.alert_key,
                provider_degraded=degraded or "market data",
            )
        else:
            evaluation = await self.lab.evaluate_candidate(candidate, now=int(time.time()))
            update = enrichment_from_evidence(
                alert_key=alert.alert_key,
                safety_status=candidate.safety.status,
                route_status=candidate.current.sell_route_status,
                independent_wallets=candidate.estimated_independent_smart_wallets,
                expected_net_edge_percent=evaluation.decision.expected_net_edge_percent,
                cost_percent=evaluation.evaluation.edge.cost_percent,
                provider_degraded=degraded,
            )
        await self.notifier.on_fast_alert_enrichment(alert, update)
        with suppress(Exception):
            await self.database.mark_fast_alert_enriched(
                alert_key=alert.alert_key, now=int(time.time())
            )

    # --- read models for the Discord commands --------------------------

    async def notable_activity(self, mint: str = "", *, limit: int = 20) -> tuple[dict, ...]:
        if self.database.connection is None:
            return ()
        rows = (
            await self.database.notable_events_for(mint, limit=limit)
            if mint
            else await self.database.recent_notable_events(limit=limit)
        )
        return tuple(rows)

    async def catalyst_feed(self, *, limit: int = 10) -> tuple[dict, ...]:
        if self.database.connection is None:
            return ()
        return tuple(await self.database.recent_catalyst_events(limit=limit))

    async def catalyst_links(self, mint: str, *, limit: int = 10) -> tuple[dict, ...]:
        if self.database.connection is None:
            return ()
        return tuple(await self.database.catalyst_links_for(mint, limit=limit))

    async def fast_alert_feed(self, *, limit: int = 20) -> tuple[dict, ...]:
        if self.database.connection is None:
            return ()
        return tuple(await self.database.recent_fast_alerts(limit=limit))

    # ------------------------------------------------------------------
    # the primary Trending universe (v2.42)
    # ------------------------------------------------------------------
    async def _warn_wallet_stream(self, health: StreamHealth) -> None:
        """Escalate a wallet lane that has been down long enough to matter (§54).

        Losing the smart-money lane silently is the worst outcome: the bot keeps
        working, the cards keep rendering, and the wallet evidence simply stops
        arriving with nothing to show for it.
        """

        detail = (
            f"Wallet stream {health.state} for {health.down_for_seconds}s — "
            f"subscriptions {health.subscriptions}, reconnects {health.reconnects}. "
            f"{health.detail}."
            + (f" Last error: {health.last_error}" if health.last_error else "")
            + " Smart-money evidence is degraded until this recovers; the polling "
            "scan lane is the fallback."
        )
        logger.warning(detail)
        with suppress(Exception):
            await self.notifier.on_error(
                "Wallet stream infrastructure", RuntimeError(detail)
            )

    async def _enrich_trending(self, entry: Any) -> dict[str, Any]:
        """Targeted, cheap enrichment for one Trending mint (sections 76, 112).

        One cached DEX snapshot per mint, plus whatever the existing lanes have
        already established.  Nothing here fetches per-candidate forensics: the
        radar has to stay affordable at a 45-second cadence, and a card that
        cannot prove something says UNKNOWN rather than paying to guess.
        """

        payload: dict[str, Any] = {}
        snapshot = None
        with suppress(Exception):
            snapshot = await self.dex_screener.snapshot(entry.mint)
        if snapshot is not None and snapshot.available:
            buys = snapshot.buys_5m or snapshot.buys_1h
            payload["buys"] = buys
            payload["risk"] = build_risk_panel(
                entry.mint,
                liquidity_usd=snapshot.liquidity_usd,
                # Safety stays UNKNOWN unless a provider actually said otherwise.
                # UNKNOWN never silently becomes PASS.
                sell_route_status="UNKNOWN",
                holders=None,
                exact_mint_confirmed=True,
                fomo_verified=entry.verification,
                safety_status="UNKNOWN",
                liquidity_collapsed=(
                    snapshot.liquidity_usd is not None
                    and snapshot.liquidity_usd < Decimal("1000")
                ),
            )

        # Narrative and wallet evidence are read from what the existing lanes
        # already persisted for this exact mint — never inferred from a name.
        with suppress(Exception):
            links = await self.database.narrative_link_rows(mint=entry.mint, limit=5)
            if links:
                payload["story_present"] = True
                payload["story_verified"] = any(
                    str(link.get("relationship") or "") in {"AUTHENTIC", "CONFIRMED"}
                    for link in links
                )
        return payload

    async def _publish_trending(self, candidate: TrendingCandidate) -> bool:
        """Render and publish one urgent Trending card.

        The kind is chosen from the *event*, so a continuation card can never
        describe itself as a new entrant and vice versa.
        """

        entry = candidate.entry
        kind = TRENDING_ALPHA
        if candidate.event.state == TRENDING_CONTINUATION:
            kind = TRENDING_CONTINUATION_ALERT
        elif candidate.event.state in {TRENDING_NEW_ENTRY, TRENDING_REENTRY}:
            kind = TRENDING_ALPHA
        elif candidate.event.rank_velocity is not None and candidate.event.rank_velocity.climbing:
            kind = TRENDING_ACCELERATION_ALERT

        theses = None
        with suppress(Exception):
            theses = await self.trending_store.about_for(entry.mint)

        alert = build_trending_alert(
            mint=entry.mint,
            name=entry.name,
            symbol=entry.symbol,
            fomo_url=entry.fomo_url or fomo_coin_url(entry.mint, self.settings.fomo_referral_code),
            kind=kind,
            entry=entry,
            event=candidate.event,
            score=candidate.score,
            holders=candidate.holders,
            risk=candidate.risk,
            about_summary=str((theses or {}).get("summary") or ""),
            project_claim=str((theses or {}).get("token_link") or ""),
            external_verification=str((theses or {}).get("external_state") or ""),
            source_caveat=self.trending_source.rank_caveat(),
            market_cap_velocity=candidate.market_cap_velocity,
            promoted_from_hot_watch=self.trending.is_hot_watched(candidate.mint),
            now=int(time.time()),
        )
        published = await self.notifier.on_fast_alert(alert)
        if published:
            self.fast_alerts_published += 1
            self.last_fast_alert_at = int(time.time())
            self.last_fast_alert_kind = kind
            with suppress(Exception):
                await self._run_trending_shadow(candidate, now=int(time.time()))
        return bool(published)

    async def _run_trending_shadow(self, candidate: TrendingCandidate, *, now: int) -> bool:
        """Offer one published Trending alert to the *separate* shadow bankroll.

        Trending Radar shows everything relevant; the Trending shadow only
        simulates configured strategy signals (section 65).  A candidate whose
        only named reason is chatter or holder growth is deliberately not
        tradeable on its own — that is the "social without market" case
        (section 102) — and :func:`family_for_reasons` returns ``None`` for it.

        Never raises into the alert path: a failure in the experiment must not
        cost the operator the alert that was already worth sending.
        """

        if not self.trending_shadow_enabled or self.database.connection is None:
            return False
        family = family_for_reasons(candidate.score.reasons)
        if family is None:
            return False
        config = self._trending_shadow_config
        if candidate.score.score < Decimal("0"):
            return False
        entry = candidate.entry
        try:
            signal = ShadowSignal(
                mint=entry.mint,
                family=family,
                timestamps=ShadowTimestamps(
                    signal_at=entry.last_observed_at or now,
                    first_seen_at=entry.first_seen_at,
                    decision_at=now,
                ),
                name=entry.name or entry.symbol or "Unknown token",
                symbol=entry.symbol or "?",
                price_usd=entry.price_usd,
                market_cap_usd=entry.current_market_cap_usd,
                liquidity_usd=entry.liquidity_usd,
                # Safety is whatever the risk panel could actually establish.
                # UNKNOWN stays UNKNOWN; it never becomes PASS to unblock a fill.
                safety_status=(
                    candidate.risk.safety_status if candidate.risk else "UNKNOWN"
                ),
                route_available=True,
                detection_market_cap_usd=entry.first_market_cap_usd,
                lifecycle_state=candidate.event.state,
                why=candidate.score.reasons,
            )
            decision, position = await self.trending_shadow.consider_signal(signal, now=now)
        except Exception:
            logger.exception("Trending shadow evaluation failed for %s", entry.mint)
            return False
        if position is None:
            return False
        paper = position.position
        alert = build_shadow_entry_alert(
            mint=entry.mint,
            name=signal.name,
            symbol=signal.symbol,
            fomo_url=self._fomo_url(entry.mint),
            family=family,
            family_label=TRENDING_FAMILY_LABELS.get(family, family),
            why=candidate.score.reasons,
            size_usd=decision.size_usd,
            fill_market_cap_usd=paper.entry_market_cap_usd,
            fill_price_usd=paper.entry_price_usd,
            venue=position.venue,
            fill_source=position.fill_source,
            graduation_state=position.graduation_state,
            modeled_cost_usd=paper.entry_costs.total_cost_usd,
            net_objective_usd=config.net_profit_objective_usd,
            signal_to_fill_seconds=max(0, now - (signal.timestamps.signal_at or now)),
            position_id=paper.position_id,
        )
        if self.shadow_cards_enabled:
            await self._publish_fast_alert(alert, now=now)
        return True

    # ------------------------------------------------------------------
    # Terminal-style trenches intelligence (v2.43)
    # ------------------------------------------------------------------
    async def _handle_pump_creation(self, creation: PumpCreation) -> None:
        """Persist a brand-new Pump.fun mint the instant the program log lands.

        Nothing here waits on enrichment.  The whole point of the realtime lane
        is that first-observation happens in the same second as the launch, and
        anything that blocks it corrupts the latency it exists to fix (§73, §74).
        """

        with suppress(Exception):
            await self.trenches.observe_creation(
                creation.mint,
                at=creation.observed_at,
                created_at=creation.observed_at,
                source=SOURCE_CREATION_STREAM,
            )

    async def _enrich_trench(self, mint: str) -> dict[str, Any]:
        """Budgeted public enrichment for one Trenches candidate (section 71).

        Market data comes from the cached DEX snapshot the rest of the pipeline
        already fetches; holders and buyer history come from public RPC through
        the shared reader, which caches and batches.  Everything unavailable
        stays absent, so the risk model reports UNKNOWN rather than guessing.
        """

        payload: dict[str, Any] = {}

        snapshot = None
        with suppress(Exception):
            snapshot = await self.dex_screener.snapshot(mint)
        if snapshot is not None and snapshot.available:
            payload.update(
                {
                    "market_cap_usd": snapshot.market_cap_usd,
                    "liquidity_usd": snapshot.liquidity_usd,
                    "volume_usd": snapshot.volume_5m_usd or snapshot.volume_1h_usd,
                    "buys": snapshot.buys_5m or snapshot.buys_1h,
                    "sells": snapshot.sells_5m or snapshot.sells_1h,
                    "dex_paid": bool(snapshot.has_website and snapshot.has_x_profile),
                    "dex_boosts": snapshot.active_boosts,
                }
            )
            # Metadata reuse across mints (section 27): fingerprints only, never
            # a copy of the third-party text.
            with suppress(Exception):
                metadata = TokenMetadata(
                    mint=mint,
                    image_url=snapshot.image_url,
                    website=snapshot.website_url,
                    twitter=(f"https://x.com/{snapshot.x_handle}" if snapshot.x_handle else ""),
                    telegram=snapshot.telegram_url,
                    discord=snapshot.discord_url,
                )
                prints = metadata.fingerprints()
                if prints:
                    others = await self.trenches_store.mints_sharing_prints(
                        prints, exclude_mint=mint
                    )
                    await self.trenches_store.save_metadata_prints(mint, prints)
                    reuse = detect_reuse(metadata, others)
                    payload["metadata_reuse"] = reuse

        with suppress(Exception):
            holders = await self.pump_chain.holder_snapshot(mint)
            if holders.top10_percent is not None:
                payload["holder_snapshot"] = holders

        # Narrative and thesis evidence for this exact mint, read from what the
        # existing lanes already persisted — never inferred from a name.
        with suppress(Exception):
            links = await self.database.narrative_link_rows(mint=mint, limit=5)
            if links:
                payload["story_verified"] = any(
                    str(link.get("relationship") or "") in {"AUTHENTIC", "CONFIRMED"}
                    for link in links
                )
        return payload

    async def _publish_trench(self, candidate: TrenchCandidate) -> bool:
        """Render and publish one Trenches card."""

        kind = TRENCH_RUNNER_ALERT
        if candidate.lifecycle.stage in {STAGE_ALMOST_BONDED, STAGE_GRADUATING}:
            kind = ALMOST_BONDED_ALERT
        elif not candidate.lifecycle.pre_graduation and candidate.public_trend is not None:
            kind = PUBLIC_TRENDING_ALERT

        fomo_url = self._fomo_url(candidate.mint)
        if kind == PUBLIC_TRENDING_ALERT:
            board = {row["mint"]: row["rank"] for row in await self.trenches.public_board(limit=50)}
            alert = build_public_trending_alert(
                mint=candidate.mint,
                name=candidate.name,
                symbol=candidate.symbol,
                fomo_url=fomo_url,
                candidate=candidate,
                rank=board.get(candidate.mint),
                notable_wallets=0,
                now=int(time.time()),
            )
        else:
            alert = build_trench_runner_alert(
                mint=candidate.mint,
                name=candidate.name,
                symbol=candidate.symbol,
                fomo_url=fomo_url,
                kind=kind,
                candidate=candidate,
                now=int(time.time()),
            )

        published = await self.notifier.on_fast_alert(alert)
        if published:
            self.fast_alerts_published += 1
            self.last_fast_alert_at = int(time.time())
            self.last_fast_alert_kind = kind
            with suppress(Exception):
                await self._run_trench_shadow(candidate, kind=kind, now=int(time.time()))
        return bool(published)

    async def _run_trench_shadow(
        self,
        candidate: TrenchCandidate,
        *,
        kind: str,
        now: int,
    ) -> bool:
        """Offer a published trench alert to the Trending shadow book (section 63).

        Attribution, not a third bankroll: the family distinguishes a
        pre-graduation entry from a Trending one inside the same $100 experiment,
        so the question "did the trenches lane pay?" is answerable without
        tripling the time to a meaningful sample.  Set
        ``FOMO_TRENCH_SHADOW_SEPARATE_BANKROLL`` to split it later.
        """

        if not self.trending_shadow_enabled or self.database.connection is None:
            return False
        family = {
            TRENCH_RUNNER_ALERT: FAMILY_TRENCH_RUNNER,
            ALMOST_BONDED_ALERT: FAMILY_TRENCH_ALMOST_BONDED,
            PUBLIC_TRENDING_ALERT: FAMILY_PUBLIC_TRENDING,
        }.get(kind)
        if family is None:
            return False
        try:
            signal = ShadowSignal(
                mint=candidate.mint,
                family=family,
                timestamps=ShadowTimestamps(signal_at=now, decision_at=now),
                name=candidate.name or candidate.symbol or "Unknown token",
                symbol=candidate.symbol or "?",
                market_cap_usd=candidate.market_cap_usd,
                liquidity_usd=candidate.liquidity_usd,
                # Safety is whatever the risk model could actually establish.
                # UNKNOWN stays UNKNOWN; it never becomes PASS to unblock a fill.
                safety_status=(
                    "FAIL"
                    if candidate.risk is not None and candidate.risk.blocked
                    else "UNKNOWN"
                ),
                route_available=True,
                detection_market_cap_usd=candidate.first_market_cap_usd,
                lifecycle_state=candidate.lifecycle.stage,
                independent_buyers=(
                    candidate.participants.independent_buyers
                    if candidate.participants
                    else None
                ),
                why=candidate.score.reasons,
            )
            decision, position = await self.trending_shadow.consider_signal(signal, now=now)
        except Exception:
            logger.exception("Trench shadow evaluation failed for %s", candidate.mint)
            return False
        if position is None:
            return False
        paper = position.position
        alert = build_shadow_entry_alert(
            mint=candidate.mint,
            name=signal.name,
            symbol=signal.symbol,
            fomo_url=self._fomo_url(candidate.mint),
            family=family,
            family_label=FAMILY_LABELS.get(family, family),
            why=candidate.score.reasons,
            size_usd=decision.size_usd,
            fill_market_cap_usd=paper.entry_market_cap_usd,
            fill_price_usd=paper.entry_price_usd,
            venue=position.venue,
            fill_source=position.fill_source,
            graduation_state=position.graduation_state,
            modeled_cost_usd=paper.entry_costs.total_cost_usd,
            net_objective_usd=self._trending_shadow_config.net_profit_objective_usd,
            signal_to_fill_seconds=0,
            position_id=paper.position_id,
        )
        if self.shadow_cards_enabled:
            await self._publish_fast_alert(alert, now=now)
        return True

    async def _run_trenches(self) -> None:
        """The Pump.fun trenches loop: the safety net behind the realtime stream."""

        while True:
            try:
                result = await self.trenches.scan_once()
                if result.error:
                    logger.debug("Trenches scan: %s", result.error)
                for mint in result.graduated:
                    # Graduation is context, and it is recorded once — the moment
                    # never moves on a later pass (sections 39, 46).
                    with suppress(Exception):
                        candidate = next(
                            (item for item in result.candidates if item.mint == mint), None
                        )
                        await self.trenches_store.mark_graduated(
                            mint,
                            at=int(time.time()),
                            market_cap_usd=(
                                candidate.market_cap_usd if candidate is not None else None
                            ),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notifier.on_error("Pump trenches", exc)
            await asyncio.sleep(self.settings.fomo_trenches_poll_seconds)

    async def _consume_pump_creations(self) -> None:
        """Drain the creation queue, so a slow consumer never stalls the socket."""

        while True:
            creation = await self.pump_creation_stream.events.get()
            try:
                await self._handle_pump_creation(creation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notifier.on_error("Pump creation intake", exc)
            finally:
                self.pump_creation_stream.events.task_done()

    # --- operator surfaces --------------------------------------------
    async def trenches_status(self) -> dict[str, Any]:
        status = await self.trenches.status()
        status["creation_stream"] = self.pump_creation_stream.status()
        return status

    async def trenches_sections(self, *, limit: int = 8) -> dict[str, Any]:
        return await self.trenches.sections(limit=limit)

    async def trenches_public_board(self, *, limit: int = 12) -> list[dict[str, Any]]:
        return await self.trenches.public_board(limit=limit)

    async def trenches_token(self, mint: str) -> dict[str, Any] | None:
        row = await self.trenches_store.token(mint)
        if row is None:
            return None
        payload = dict(row)
        payload["intel"] = await self.trenches_store.intel(mint)
        payload["nominations"] = await self.trenches_store.nominations_for(mint)
        payload["holder_history"] = [
            item.to_json() for item in await self.trenches_store.holder_snapshots(mint)
        ]
        creator = str(row.get("creator") or "")
        payload["dev_profile"] = await self.trenches_store.dev_profile(creator)
        return payload

    async def trenches_suppressions(self, *, since: int = 0) -> dict[str, int]:
        return await self.trenches_store.suppression_counts(since=since)

    async def trenches_latency(self) -> dict[str, Any]:
        """Time-to-first-observation, per discovery source (section 73)."""

        return await self.trenches_store.discovery_latency_by_source()

    async def record_benchmark_snapshot(
        self,
        *,
        board_name: str,
        captured_at: int,
        captured_by: str,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Store an administrator's manual observation and compare it (section 83).

        Manual only.  Nothing in this codebase fetches a third-party board, and
        the comparison exists to calibrate our own model honestly — not to
        reproduce anyone's proprietary ranking.
        """

        from .trenches import BenchmarkEntry, BenchmarkSnapshot, compare_to_benchmark

        rows = tuple(
            BenchmarkEntry(
                mint=str(item["mint"]),
                rank=int(item["rank"]),
                observed_at=captured_at,
            )
            for item in entries
            if item.get("mint") and item.get("rank") is not None
        )
        snapshot = BenchmarkSnapshot(
            captured_at=captured_at,
            entries=rows,
            board_name=board_name,
            captured_by=captured_by,
        )
        ours = {
            str(row["mint"]): int(row["rank"])
            for row in await self.trenches.public_board(limit=100)
        }
        first_seen: dict[str, int] = {}
        for mint in ours:
            token = await self.trenches_store.token(mint)
            if token is not None:
                first_seen[mint] = int(token["first_observed_at"])
        comparison = compare_to_benchmark(snapshot, ours, first_seen=first_seen)
        await self.trenches_store.save_benchmark(
            f"{board_name}:{captured_at}",
            board_name=board_name,
            captured_at=captured_at,
            captured_by=captured_by,
            source=snapshot.source,
            entries=[
                {"mint": item.mint, "rank": item.rank} for item in snapshot.entries
            ],
            comparison=comparison.to_json(),
        )
        return comparison.to_json()

    async def _run_trending_radar(self) -> None:
        """The primary discovery loop.  Cheap, fast, and independent of the legacy radar."""

        while True:
            try:
                result = await self.trending.poll_once()
                if result.error:
                    logger.debug("Trending poll: %s", result.error)
                # A near-miss card is radar-only visibility; it never pings.
                for mint in result.hot_watched:
                    candidate = next(
                        (item for item in result.candidates if item.mint == mint), None
                    )
                    if candidate is None:
                        continue
                    card = build_trending_hot_watch_card(
                        mint=candidate.mint,
                        symbol=candidate.entry.symbol,
                        name=candidate.entry.name,
                        fomo_url=(
                            candidate.entry.fomo_url
                            or fomo_coin_url(candidate.mint, self.settings.fomo_referral_code)
                        ),
                        entry=candidate.entry,
                        score=candidate.score,
                        gap=candidate.verdict.near_miss_gap,
                        now=int(time.time()),
                    )
                    with suppress(Exception):
                        if await self.notifier.on_fast_alert(card):
                            self.trending_hot_watch_cards += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notifier.on_error("Trending radar", exc)
            await asyncio.sleep(self.settings.fomo_trending_poll_seconds)

    async def _run_trending_hot_watch(self) -> None:
        """The fast recheck lane.  A strong near miss is not left for 30 minutes."""

        while True:
            try:
                await self.trending.recheck_hot_watches()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notifier.on_error("Trending hot watch", exc)
            await asyncio.sleep(self.settings.fomo_trending_hot_watch_recheck_seconds)

    # --- operator surfaces --------------------------------------------
    async def trending_status(self) -> dict[str, Any]:
        return await self.trending.status()

    def trending_board(self, *, limit: int = 12) -> tuple[Any, ...]:
        return self.trending.board(limit=limit)

    def trending_entry(self, mint: str) -> Any:
        return self.trending.entry_for(mint)

    async def trending_hot_watch_report(self) -> dict[str, Any]:
        return await self.trending.hot_watch_report()

    async def trending_suppressions(self, *, since: int = 0) -> dict[str, int]:
        return await self.trending_store.suppression_counts(since=since)

    async def trending_universes(self) -> dict[str, Any]:
        """`/fomo profit view:universes` — $100 TRENDING vs $100 LEGACY (§66).

        Both books are read through the same shadow store using their own
        ``strategy_version``, which is what makes them independent rather than
        two views of one account.
        """

        legacy = await self._universe_trades(SHADOW_STRATEGY_VERSION)
        trending = await self._universe_trades(TRENDING_STRATEGY_VERSION)
        comparison = TrendingRuntime.compare(trending, legacy)
        payload = comparison.to_json()
        payload["trending_enabled"] = self.settings.fomo_trending_shadow_enabled
        payload["legacy_strategy_version"] = SHADOW_STRATEGY_VERSION
        payload["trending_strategy_version"] = TRENDING_STRATEGY_VERSION
        return payload

    async def _universe_trades(self, strategy_version: str) -> list[UniverseTrade]:
        """Resolved simulated trades for one isolated bankroll.

        The two universes differ only in ``strategy_version``.  Everything else —
        the cost model, the exit engine, the arithmetic — is shared, which is the
        point: a comparison is only fair when the strategy is the sole variable.
        """

        rows: list[UniverseTrade] = []
        try:
            positions = await self.shadow_store.closed_positions(
                strategy_version=strategy_version
            )
        except Exception:
            return rows
        for shadow in positions:
            position = shadow.position
            reason = position.close_reason or ""
            mae = position.max_adverse_percent
            rows.append(
                UniverseTrade(
                    mint=position.mint,
                    family=shadow.family,
                    opened_at=position.opened_at,
                    closed_at=position.closed_at or position.opened_at,
                    net_pnl_usd=Decimal(str(position.realized_net_pnl_usd or 0)),
                    size_usd=Decimal(str(position.size_usd or 0)),
                    mfe_percent=position.max_favourable_percent,
                    mae_percent=mae,
                    # A "severe failure" is a structural loss, not an ordinary
                    # losing trade: the position gave back more than half.
                    severe_failure=bool(mae is not None and mae <= Decimal("-50")),
                    rugged=reason in {EXIT_SAFETY_EMERGENCY, EXIT_LIQUIDITY_EMERGENCY},
                    liquidity_collapsed=reason
                    in {EXIT_LIQUIDITY_EMERGENCY, EXIT_LIQUIDITY_DETERIORATION},
                    unsellable=reason == EXIT_SAFETY_EMERGENCY,
                )
            )
        return rows

    def realtime_status(self) -> dict[str, object]:
        """What the realtime lane is actually doing right now (sections 33, 88).

        The wallet-stream fields report a *named state* rather than a bare
        boolean, because "DISCONNECTED" with zero subscriptions and zero
        reconnects described three unrelated faults and told an operator how to
        fix none of them (section 52).
        """

        now = int(time.time())
        stream_health = self.stream.health(now=now)
        return {
            "stream_connected": stream_health.connected,
            "stream_state": stream_health.state,
            "stream_detail": stream_health.detail,
            "stream_reconnects": stream_health.reconnects,
            "stream_failed_attempts": stream_health.failed_attempts,
            "stream_last_message_age": stream_health.last_message_age,
            "stream_down_for": stream_health.down_for_seconds,
            "stream_fallback_active": stream_health.fallback_active,
            "stream_last_error": stream_health.last_error,
            "stream_last_event_at": getattr(self.stream, "last_event_at", None),
            "stream_last_event_age": stream_health.last_event_age,
            "stream_subscriptions": stream_health.subscriptions,
            "fast_watch_enabled": (
                self.settings.fomo_fast_watch_enabled
                and self.settings.fomo_fast_watch_publish_enabled
            ),
            "notable_alerts_enabled": self.settings.fomo_notable_alerts_enabled,
            "notable_ping_enabled": self.settings.fomo_notable_ping_enabled,
            "catalyst_alerts_enabled": self.settings.fomo_catalyst_alerts_enabled,
            "confluence_alerts_enabled": self.settings.fomo_confluence_alerts_enabled,
            "social_radar_enabled": self.settings.fomo_social_radar_enabled,
            "enrichment_enabled": self.settings.fomo_alert_enrichment_enabled,
            "alerts_published": self.fast_alerts_published,
            "alerts_suppressed": self.fast_alerts_suppressed,
            "last_alert_at": self.last_fast_alert_at,
            "last_alert_kind": self.last_fast_alert_kind,
            # The primary universe's own lane, reported separately from the
            # legacy graduated one so a healthy secondary can never make a dead
            # primary look fine.
            "trending_enabled": self.settings.fomo_trending_primary_enabled,
            "trending_source": self.trending_source.kind,
            "trending_source_label": self.trending_source.label,
            "trending_authorised": self.trending_source.authorised,
            "trending_health": self.trending.lane_health(now=now),
            "trending_polls": self.trending.polls,
            "trending_last_poll_at": self.trending.last_poll_at,
            "trending_tracked": sum(
                1 for entry in self.trending.board(limit=1000) if entry.on_board
            ),
            "trending_hot_watch": self.trending.hot_watch_status(),
            "trending_alerts_published": self.trending.alerts_published,
            "trending_alerts_suppressed": self.trending.alerts_suppressed,
            "trending_promotions": self.trending.promotions,
            "trending_hot_watch_cards": self.trending_hot_watch_cards,
            "graduated_secondary_enabled": self.settings.fomo_graduated_secondary_enabled,
            "trending_shadow_enabled": self.settings.fomo_trending_shadow_enabled,
            # The Pump trenches lane and the realtime creation stream, reported
            # separately so a healthy poll cannot make a dead stream look fine.
            "trenches_enabled": self.settings.fomo_trenches_enabled,
            "trenches_tracked": len(self.trenches._tracked),
            "trenches_scans": self.trenches.scans,
            "trenches_creations_seen": self.trenches.creations_seen,
            "trenches_alerts_published": self.trenches.alerts_published,
            "trenches_alerts_suppressed": self.trenches.alerts_suppressed,
            "creation_stream": self.pump_creation_stream.status(now=now),
            "public_model_enabled": self.settings.fomo_public_trending_enabled,
            "chain_usage": self.pump_chain.usage_snapshot(),
            "live_execution": False,
        }

    @staticmethod
    def _runner_risk_changes(
        previous: RunnerCandidate,
        current: RunnerCandidate,
    ) -> tuple[str, ...]:
        changes: list[str] = []
        pairs = (
            ("Top10", previous.current.top10_percent, current.current.top10_percent),
            (
                "Largest cluster",
                previous.forensics.largest_cluster_supply_percent,
                current.forensics.largest_cluster_supply_percent,
            ),
        )
        for label, before, after in pairs:
            if before is not None and after is not None and after - before >= Decimal("15"):
                changes.append(f"{label}: {before:.1f}% → {after:.1f}%")
        before_liquidity = previous.current.liquidity_usd
        after_liquidity = current.current.liquidity_usd
        if (
            before_liquidity is not None
            and before_liquidity > 0
            and after_liquidity is not None
            and after_liquidity <= before_liquidity * Decimal("0.75")
        ):
            changes.append(
                f"Liquidity: ${before_liquidity:,.0f} → ${after_liquidity:,.0f}"
            )
        if (
            previous.current.sell_route_status == "PASS"
            and current.current.sell_route_status != "PASS"
        ):
            changes.append(
                f"Sell route: PASS → {current.current.sell_route_status}"
            )
        if previous.safety.status != "FAIL" and current.safety.status == "FAIL":
            changes.append("Safety: non-fail → FAIL")
        return tuple(changes)

    async def _evaluate_runner_transitions(
        self,
        previous: RunnerCandidate,
        current: RunnerCandidate,
    ) -> None:
        if previous.first_discord_visible_at is None:
            return
        changes = self._runner_risk_changes(previous, current)
        if changes:
            fingerprint = hashlib.sha256("|".join(changes).encode()).hexdigest()
            if await self.database.reserve_runner_alert(
                mint=current.mint,
                event_type="RISK_ESCALATION",
                fingerprint=fingerprint,
                now=current.generated_at,
                allow_changed_fingerprint=True,
            ):
                sent = await self.notifier.on_runner_risk_escalation(current, changes)
                if sent is False:
                    await self.database.release_runner_alert(
                        mint=current.mint,
                        event_type="RISK_ESCALATION",
                    )

        snapshots = [
            runner_snapshot_from_json(raw)
            for raw in await self.database.runner_snapshot_payloads(current.mint, limit=200)
        ]
        market_caps = [item.market_cap_usd for item in snapshots if item.market_cap_usd is not None]
        peak_mc = max(market_caps) if market_caps else current.current.market_cap_usd
        current_mc = current.current.market_cap_usd
        drawdown = (
            (Decimal("1") - current_mc / peak_mc) * Decimal("100")
            if peak_mc is not None and peak_mc > 0 and current_mc is not None
            else None
        )
        first_liquidity = current.first.liquidity_usd
        current_liquidity = current.current.liquidity_usd
        liquidity_decline = (
            (Decimal("1") - current_liquidity / first_liquidity) * Decimal("100")
            if first_liquidity is not None
            and first_liquidity > 0
            and current_liquidity is not None
            else None
        )
        reasons: list[str] = []
        if (
            drawdown is not None
            and drawdown >= self.settings.fomo_runner_invalidation_drawdown_percent
        ):
            reasons.append(f"post-detection peak drawdown reached {drawdown:.1f}%")
        if (
            liquidity_decline is not None
            and liquidity_decline
            >= self.settings.fomo_runner_invalidation_liquidity_decline_percent
        ):
            reasons.append(f"liquidity declined {liquidity_decline:.1f}%")
        if (
            current_liquidity is None
            or current_liquidity
            < self.settings.fomo_runner_invalidation_liquidity_floor_usd
        ):
            reasons.append("liquidity fell below the hard floor")
        if current.current.rugged:
            reasons.append("rug flag appeared")
        if (
            previous.current.sell_route_status == "PASS"
            and current.current.sell_route_status != "PASS"
        ):
            reasons.append("sell route disappeared or degraded")
        if previous.safety.status != "FAIL" and current.safety.status == "FAIL":
            concentration_failures = tuple(
                failure
                for failure in current.safety.failures
                if any(
                    label in failure.casefold()
                    for label in ("top10", "cluster", "bundler", "insider", "sniper", "dev")
                )
            )
            if concentration_failures:
                reasons.append("critical holder or linked-cluster risk appeared")
        if not reasons:
            return
        fingerprint = hashlib.sha256("|".join(sorted(reasons)).encode()).hexdigest()
        reserved = await self.database.reserve_runner_alert(
            mint=current.mint,
            event_type="INVALIDATED",
            fingerprint=fingerprint,
            now=current.generated_at,
        )
        if not reserved:
            return
        sent = await self.notifier.on_runner_invalidated(
            current,
            {
                "first_market_cap": current.first.market_cap_usd,
                "peak_market_cap": peak_mc,
                "current_market_cap": current_mc,
                "peak_return": forward_return_percent(peak_mc, current.first.market_cap_usd),
                "drawdown_from_peak": drawdown,
                "liquidity_decline": liquidity_decline,
            },
            tuple(reasons),
        )
        if sent is False:
            await self.database.release_runner_alert(
                mint=current.mint,
                event_type="INVALIDATED",
            )

    async def _maybe_publish_runner(self, candidate: RunnerCandidate) -> None:
        """Individual alert lane: HEATING UP and above only.

        Plain QUALIFIED_RESEARCH candidates flow through the ranked digest so a
        merely-interesting setup competes for attention instead of pinging.
        """

        if candidate.stage not in ALERT_STAGES and candidate.research_only:
            return
        now = int(time.time())
        previous = self._runner_last_alert.get(candidate.mint)
        if previous and now - previous[0] < 300 and candidate.score < previous[1] + 5:
            return
        fingerprint = (
            f"{candidate.stage}:{int(candidate.score // 5)}:{candidate.safety.status}"
        )
        if not await self.database.reserve_runner_alert(
            mint=candidate.mint,
            event_type="STRONG",
            fingerprint=fingerprint,
            now=now,
            allow_changed_fingerprint=True,
        ):
            return
        self._runner_last_alert[candidate.mint] = (now, candidate.score)
        sent = await self.notifier.on_runner_alert(candidate)
        if sent is False:
            await self.database.release_runner_alert(
                mint=candidate.mint,
                event_type="STRONG",
            )
            return
        visible_at = int(time.time())
        await self.database.mark_runner_visible(
            mint=candidate.mint,
            visible_at=visible_at,
            market_cap_usd=candidate.current.market_cap_usd,
        )
        await self.database.mark_runner_visible(
            mint=candidate.mint,
            visible_at=visible_at,
            market_cap_usd=candidate.current.market_cap_usd,
            strong=True,
        )
        await self.database.set_setting("runner_last_strong_alert_at", str(now))
        await self.database.set_setting("runner_last_strong_alert_mint", candidate.mint)

    def _start_runner_fast_watch(self, candidate: RunnerCandidate) -> None:
        if candidate.mint in self._runner_fast_watch_tasks:
            return
        age_minutes = (
            max(
                0,
                candidate.generated_at
                - (candidate.graduated_at or candidate.pair_created_at or 0),
            )
            // 60
            if candidate.graduated_at or candidate.pair_created_at
            else None
        )
        fresh = is_fresh_research_worthy(
            candidate,
            max_age_seconds=self.settings.fomo_runner_fresh_max_age_seconds,
        )
        watch_as_fresh = fresh and self.settings.fomo_runner_fresh_watch_enabled
        maximum_watch = (
            self.settings.fomo_runner_fresh_watch_max
            if watch_as_fresh
            else self.settings.fomo_runner_max_fast_watch
        )
        if (
            (
                candidate.score < self.settings.fomo_runner_fast_watch_min_score
                and not watch_as_fresh
                # A qualified candidate is watched on its evidence, not on the
                # legacy additive score, which can lag a genuinely early setup.
                and candidate.stage not in USER_FACING_STAGES
            )
            or age_minutes is None
            or age_minutes > self.settings.fomo_runner_max_graduation_age_minutes
            or len(self._runner_fast_watch_tasks) >= maximum_watch
        ):
            return
        task = asyncio.create_task(
            self._fast_watch_runner(candidate.mint, fresh=watch_as_fresh),
            name=f"runner-fast-{candidate.mint[:8]}",
        )
        self._runner_fast_watch_tasks[candidate.mint] = task
        self.runner_last_fast_watch_mint = candidate.mint
        self.runner_last_fast_watch_at = int(time.time())
        task.add_done_callback(
            lambda _task, mint=candidate.mint: self._runner_fast_watch_tasks.pop(mint, None)
        )

    async def _fast_watch_runner(self, mint: str, *, fresh: bool = False) -> None:
        started_at = int(time.time())
        await self.database.set_setting("runner_last_fast_watch_mint", mint)
        await self.database.set_setting("runner_last_fast_watch_at", str(started_at))
        schedule = fresh_watch_schedule(
            (
                self.settings.fomo_runner_fresh_watch_seconds
                if fresh
                else self.settings.fomo_runner_fast_watch_seconds
            ),
            self.settings.fomo_runner_fast_watch_minutes,
        )
        started_monotonic = time.monotonic()
        for offset in schedule[1:]:
            delay = offset - (time.monotonic() - started_monotonic)
            if delay > 0:
                await asyncio.sleep(delay)
            prior_payload = await self.database.runner_candidate_payload(mint)
            prior = runner_candidate_from_json(prior_payload) if prior_payload else None
            # Progressive analysis: the expensive holder/funding trace is spent on
            # candidates that are actually close to qualifying, so buyer-independence
            # evidence exists at the moment the qualification decision is made rather
            # than arriving after the alert. It still refreshes at most once a minute
            # inside ``analyze_runner``, and never blocks first observation.
            deep = bool(
                prior
                and (
                    prior.stage in USER_FACING_STAGES
                    or prior.score >= self.settings.fomo_runner_forensics_min_score
                    or prior.quality.opportunity_score
                    >= self._quality_config.min_opportunity_score - 10
                )
            )
            candidate = await self.analyze_runner(
                mint,
                refresh_market=True,
                deep_forensics=deep,
            )
            await self._maybe_publish_fast_watch(candidate)
            await self._maybe_publish_fresh(candidate)
            await self._maybe_publish_runner(candidate)
            elapsed = int(time.monotonic() - started_monotonic)
            current = candidate.current
            if (
                current.rugged
                or current.sell_route_status == "FAIL"
                or current.market_cap_usd is None
                or current.liquidity_usd is None
                or current.liquidity_usd
                < self.settings.fomo_runner_invalidation_liquidity_floor_usd
                or (
                    elapsed >= 60
                    and current.buys_5m + current.sells_5m == 0
                    and current.volume_5m_usd < Decimal("50")
                )
            ):
                break

    async def _run_runner_outcomes(self) -> None:
        while True:
            try:
                now = int(time.time())
                for mint in await self.database.runner_due_mints(now=now, limit=10):
                    await self.analyze_runner(
                        mint,
                        refresh_market=True,
                        allow_automatic_x=False,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notifier.on_error("Runner outcome tracking", exc)
            await asyncio.sleep(self.settings.fomo_runner_outcome_poll_seconds)

    async def _run_runner_digest(self) -> None:
        """Publish a persisted, non-pinging research summary on a slow cadence."""

        while True:
            await asyncio.sleep(self.settings.fomo_runner_digest_seconds)
            try:
                await self._publish_runner_digest()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notifier.on_error("Runner research digest", exc)

    @staticmethod
    def _runner_digest_fingerprint(candidates: tuple[RunnerCandidate, ...]) -> str:
        """Bucket volatile values so minor market noise does not resend a digest."""

        rows: list[dict[str, object]] = []
        for item in candidates:
            price_change = forward_return_percent(
                item.current.price_usd,
                item.first.price_usd,
            )
            cap_change = forward_return_percent(
                item.current.market_cap_usd,
                item.first.market_cap_usd,
            )
            rows.append(
                {
                    "mint": item.mint,
                    "score_bucket": int(item.score // 5),
                    "price_bucket": int((price_change or Decimal("0")) // 10),
                    "cap_bucket": int((cap_change or Decimal("0")) // 10),
                    "blockers": item.hard_blockers,
                }
            )
        raw = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _digest_age_seconds(self, candidate: RunnerCandidate) -> int | None:
        source = (
            candidate.chain_created_at
            or candidate.graduated_at
            or candidate.pair_created_at
        )
        return max(0, candidate.generated_at - source) if source else None

    async def _publish_runner_digest(self) -> bool:
        """Rank the qualified research lane; never mirror the graduated universe.

        The old digest published "every changed candidate above a low score",
        which is how a 27/100 with unknown safety ended up next to a real
        setup.  Selection is now qualification, and ordering is opportunity
        quality, acceleration, buyer independence, freshness and safety
        confidence — the best few things worth looking at right now.
        """

        candidates = await self.runner_lab_cached_candidates(
            research_test=True,
            max_age_seconds=86_400,
            limit=max(20, self.settings.fomo_runner_digest_max_candidates * 4),
        )
        eligible = [
            item
            for item in candidates
            if item.stage == STAGE_QUALIFIED
            and item.research_only
            and item.score >= self.settings.fomo_runner_digest_min_score
        ]
        selected = tuple(
            cast(RunnerCandidate, item)
            for item in rank_for_attention(
                [
                    (
                        item.quality,
                        item.safety,
                        self._digest_age_seconds(item),
                        item,
                    )
                    for item in eligible
                ],
                limit=self.settings.fomo_runner_digest_max_candidates,
            )
        )
        if not selected:
            return False
        fingerprint = self._runner_digest_fingerprint(selected)
        previous = await self.database.get_setting("runner_digest_fingerprint")
        if fingerprint == previous:
            return False
        await self.notifier.on_runner_digest(
            selected,
            self.settings.fomo_runner_public_alert_min_score,
        )
        now = int(time.time())
        # The digest IS Discord visibility. Recording it keeps risk escalation
        # and setup invalidation working for digest-only candidates, which both
        # key off first_discord_visible_at, and keeps the MC-at-first-visible
        # timing honest now that fewer candidates fire a standalone alert.
        for item in selected:
            await self.database.mark_runner_visible(
                mint=item.mint,
                visible_at=now,
                market_cap_usd=item.current.market_cap_usd,
            )
        await self.database.set_setting("runner_digest_fingerprint", fingerprint)
        await self.database.set_setting("runner_last_digest_at", str(now))
        await self.database.set_setting(
            "runner_last_digest_mints",
            ",".join(item.mint for item in selected),
        )
        return True

    async def _record_runner_outcomes(self, candidate: RunnerCandidate) -> None:
        snapshots = [
            runner_snapshot_from_json(raw)
            for raw in await self.database.runner_snapshot_payloads(
                candidate.mint,
                limit=200,
            )
        ]
        tolerance = max(90, self.settings.fomo_runner_outcome_poll_seconds * 2)
        for horizon in RUNNER_HORIZONS_SECONDS:
            eligible = [
                snapshot
                for snapshot in snapshots
                if snapshot.captured_at - candidate.first_seen_at >= horizon
            ]
            if not eligible:
                continue
            observed = min(eligible, key=lambda item: item.captured_at)
            observed_age = observed.captured_at - candidate.first_seen_at
            # A restart after a long outage must not relabel a future price as a
            # historical 1m/5m outcome. Missing windows remain honestly pending.
            if observed_age > horizon + tolerance:
                continue
            liquidity_return = forward_return_percent(
                observed.liquidity_usd,
                candidate.first.liquidity_usd,
            )
            liquidity_disappeared = bool(
                observed.liquidity_usd is None
                or observed.liquidity_usd < Decimal("500")
                or (
                    candidate.first.liquidity_usd
                    and observed.liquidity_usd
                    < candidate.first.liquidity_usd * Decimal("0.10")
                )
            )
            await self.database.record_runner_outcome(
                mint=candidate.mint,
                horizon_seconds=horizon,
                observed_at=observed.captured_at,
                price_return_percent=forward_return_percent(
                    observed.price_usd,
                    candidate.first.price_usd,
                ),
                market_cap_return_percent=forward_return_percent(
                    observed.market_cap_usd,
                    candidate.first.market_cap_usd,
                ),
                liquidity_return_percent=liquidity_return,
                liquidity_disappeared=liquidity_disappeared,
                rugged=observed.rugged,
                route_available=observed.route_available,
            )

    async def runner_results(self) -> dict[str, object]:
        rows = await self.database.runner_results_rows()
        snapshots = await self.database.runner_all_snapshot_rows()
        candidates: dict[str, dict[str, object]] = {}
        candidate_objects: dict[str, RunnerCandidate] = {}
        outcomes: list[dict[str, object]] = []
        for row in rows:
            mint = str(row["mint"])
            candidates.setdefault(mint, row)
            if row["horizon_seconds"] is not None:
                outcomes.append(row)
        returns = [
            Decimal(str(row["price_return_percent"]))
            if row["price_return_percent"] is not None
            else Decimal(str(row["market_cap_return_percent"]))
            for row in outcomes
            if row["price_return_percent"] is not None
            or row["market_cap_return_percent"] is not None
        ]
        ordered = sorted(returns)
        median = ordered[len(ordered) // 2] if ordered else Decimal("0")
        average = sum(returns, Decimal("0")) / len(returns) if returns else Decimal("0")
        by_horizon: dict[int, dict[str, object]] = {}
        for horizon in RUNNER_HORIZONS_SECONDS:
            horizon_rows = [row for row in outcomes if int(row["horizon_seconds"]) == horizon]
            horizon_returns = [
                Decimal(str(row["price_return_percent"]))
                if row["price_return_percent"] is not None
                else Decimal(str(row["market_cap_return_percent"]))
                for row in horizon_rows
                if row["price_return_percent"] is not None
                or row["market_cap_return_percent"] is not None
            ]
            by_horizon[horizon] = {
                "count": len(horizon_rows),
                "average": (
                    sum(horizon_returns, Decimal("0")) / len(horizon_returns)
                    if horizon_returns
                    else None
                ),
                "hit_10": sum(value >= 10 for value in horizon_returns),
                "hit_25": sum(value >= 25 for value in horizon_returns),
                "hit_50": sum(value >= 50 for value in horizon_returns),
                "hit_100": sum(value >= 100 for value in horizon_returns),
                "failures": sum(
                    bool(row["rugged"] or row["liquidity_disappeared"]) for row in horizon_rows
                ),
            }
        excursions: dict[str, dict[str, object]] = {}
        for mint in candidates:
            first_payload = await self.database.runner_candidate_payload(mint)
            if not first_payload:
                continue
            candidate = runner_candidate_from_json(first_payload)
            candidate_objects[mint] = candidate
            first = candidate.first
            series = [
                runner_snapshot_from_json(str(row["snapshot_json"]))
                for row in snapshots
                if str(row["mint"]) == mint
            ]
            path = runner_path_metrics(first, series)
            excursions[mint] = {
                **path,
                # Backward-compatible names retained for existing result consumers.
                "maximum_favorable": path["maximum_favorable_excursion"],
                "maximum_drawdown": path["maximum_adverse_excursion"],
            }

        latest_outcome: dict[str, dict[str, object]] = {}
        for row in outcomes:
            mint = str(row["mint"])
            prior = latest_outcome.get(mint)
            if prior is None or int(row["horizon_seconds"]) > int(prior["horizon_seconds"]):
                latest_outcome[mint] = row

        def row_return(row: dict[str, object]) -> Decimal | None:
            value = row["price_return_percent"]
            if value is None:
                value = row["market_cap_return_percent"]
            return Decimal(str(value)) if value is not None else None

        def aggregate(mints: list[str]) -> dict[str, object]:
            selected = [latest_outcome[mint] for mint in mints if mint in latest_outcome]
            values = [value for row in selected if (value := row_return(row)) is not None]
            return {
                "count": len(selected),
                "average": (
                    (sum(values, Decimal("0")) / len(values)).quantize(Decimal("0.01"))
                    if values
                    else None
                ),
                "hit_25_percent": (
                    (Decimal(sum(value >= 25 for value in values)) / Decimal(len(values)) * 100)
                    .quantize(Decimal("0.01"))
                    if values
                    else None
                ),
                "failure_rate_percent": (
                    (
                        Decimal(
                            sum(
                                bool(row["rugged"] or row["liquidity_disappeared"])
                                for row in selected
                            )
                        )
                        / Decimal(len(selected))
                        * 100
                    ).quantize(Decimal("0.01"))
                    if selected
                    else None
                ),
            }

        bucket_mints: dict[str, dict[str, list[str]]] = {
            "score": {},
            "graduation_age": {},
            "market_cap": {},
            "smart_wallets": {},
            "holder_quality": {},
            "x": {},
            "safety": {},
        }
        detection_delays: list[int] = []
        for mint, item in candidate_objects.items():
            score_label = (
                "0-49"
                if item.score < 50
                else "50-69"
                if item.score < 70
                else "70-84"
                if item.score < 85
                else "85-100"
            )
            age = (
                max(0, item.first_seen_at - item.graduated_at)
                if item.graduated_at
                else None
            )
            if age is not None:
                detection_delays.append(age)
            age_label = (
                "unknown"
                if age is None
                else "0-5m"
                if age <= 300
                else "5-15m"
                if age <= 900
                else "15-30m"
                if age <= 1_800
                else "30m+"
            )
            cap = item.first.market_cap_usd
            cap_label = (
                "unknown"
                if cap is None
                else "under-50k"
                if cap < 50_000
                else "50k-150k"
                if cap < 150_000
                else "150k+"
            )
            smart_label = (
                "0" if not item.smart_wallets else "1" if len(item.smart_wallets) == 1 else "2+"
            )
            holder_label = (
                "unknown"
                if item.first.holder_count is None or item.first.top10_percent is None
                else "healthy"
                if item.first.holder_count >= 100 and item.first.top10_percent <= 35
                else "thin/concentrated"
            )
            labels = {
                "score": score_label,
                "graduation_age": age_label,
                "market_cap": cap_label,
                "smart_wallets": smart_label,
                "holder_quality": holder_label,
                "x": "verified" if item.x_evidence.available else "not-verified",
                "safety": item.detection_safety.status,
            }
            for group, label in labels.items():
                bucket_mints[group].setdefault(label, []).append(mint)
        breakdowns = {
            group: {label: aggregate(mints) for label, mints in labels.items()}
            for group, labels in bucket_mints.items()
        }

        all_mints = list(candidate_objects)
        sample = max(1, len(all_mints) // 4) if all_mints else 0
        baselines = {
            "all_new_candidates": aggregate(all_mints),
            "random_newly_graduated": aggregate(
                sorted(
                    all_mints,
                    key=lambda mint: sum(
                        (index + 1) * ord(character)
                        for index, character in enumerate(mint)
                    ),
                )[:sample]
            ),
            "lowest_age": aggregate(
                sorted(
                    all_mints,
                    key=lambda mint: (
                        candidate_objects[mint].first_seen_at
                        - (candidate_objects[mint].graduated_at or 0)
                    ),
                )[:sample]
            ),
            "highest_5m_volume": aggregate(
                sorted(
                    all_mints,
                    key=lambda mint: candidate_objects[mint].first.volume_5m_usd,
                    reverse=True,
                )[:sample]
            ),
            "highest_5m_price_gain": aggregate(
                sorted(
                    all_mints,
                    key=lambda mint: (
                        candidate_objects[mint].first.dex_price_change_5m_percent
                        or Decimal("-999")
                    ),
                    reverse=True,
                )[:sample]
            ),
            "highest_market_cap": aggregate(
                sorted(
                    all_mints,
                    key=lambda mint: (
                        candidate_objects[mint].first.market_cap_usd or Decimal("0")
                    ),
                    reverse=True,
                )[:sample]
            ),
        }
        score_values = sorted(
            Decimal(str(row["latest_score"])) for row in candidates.values()
        )

        def percentile(values: list[Decimal], quantile: Decimal) -> Decimal | None:
            if not values:
                return None
            if len(values) == 1:
                return values[0]
            position = Decimal(len(values) - 1) * quantile
            lower = int(position)
            upper = min(lower + 1, len(values) - 1)
            fraction = position - Decimal(lower)
            return values[lower] + (values[upper] - values[lower]) * fraction

        score_distribution = {
            "max": max(score_values) if score_values else None,
            "median": percentile(score_values, Decimal("0.50")),
            "p90": percentile(score_values, Decimal("0.90")),
            "p95": percentile(score_values, Decimal("0.95")),
            "gte_15": sum(value >= 15 for value in score_values),
            "gte_20": sum(value >= 20 for value in score_values),
            "gte_35": sum(value >= 35 for value in score_values),
            "gte_50": sum(value >= 50 for value in score_values),
            "gte_60": sum(value >= 60 for value in score_values),
            "gte_70": sum(value >= 70 for value in score_values),
        }
        best_current = tuple(
            sorted(
                candidate_objects.values(),
                key=lambda item: (item.score, item.current.captured_at),
                reverse=True,
            )[:3]
        )
        last_strong_alert = await self.database.get_setting("runner_last_strong_alert_at")
        last_strong_mint = await self.database.get_setting("runner_last_strong_alert_mint")
        last_digest = await self.database.get_setting("runner_last_digest_at")
        stored_fast_watch_mint = await self.database.get_setting(
            "runner_last_fast_watch_mint"
        )
        stored_fast_watch_at = await self.database.get_setting("runner_last_fast_watch_at")

        def metric_values(key: str) -> list[Decimal]:
            return sorted(
                Decimal(str(row[key]))
                for row in excursions.values()
                if row.get(key) is not None
            )

        def event_rate(key: str) -> Decimal | None:
            values = [row.get(key) for row in excursions.values() if row.get(key) is not None]
            if not values:
                return None
            return (
                Decimal(sum(value is True for value in values))
                / Decimal(len(values))
                * Decimal("100")
            ).quantize(Decimal("0.01"))

        def metric_median(key: str) -> Decimal | None:
            return percentile(metric_values(key), Decimal("0.50"))

        path_analytics = {
            "plus_10_before_minus_25_rate": event_rate("plus_10_before_minus_25"),
            "plus_25_before_minus_25_rate": event_rate("plus_25_before_minus_25"),
            "plus_50_before_minus_50_rate": event_rate("plus_50_before_minus_50"),
            "plus_100_before_minus_50_rate": event_rate("plus_100_before_minus_50"),
            "median_time_to_25_seconds": metric_median("time_to_25"),
            "median_time_to_50_seconds": metric_median("time_to_50"),
            "median_maximum_favorable_excursion": metric_median(
                "maximum_favorable_excursion"
            ),
            "median_maximum_adverse_excursion": metric_median(
                "maximum_adverse_excursion"
            ),
            "median_post_peak_drawdown": metric_median("max_drawdown_from_peak"),
            "severe_failure_rate": (
                (
                    Decimal(
                        sum(
                            bool(row.get("rug_or_liquidity_failure"))
                            or Decimal(str(row.get("max_drawdown_from_peak") or 0)) >= 80
                            for row in excursions.values()
                        )
                    )
                    / Decimal(len(excursions))
                    * Decimal("100")
                ).quantize(Decimal("0.01"))
                if excursions
                else None
            ),
            "peak_return_distribution": {
                "median": metric_median("peak_return"),
                "p90": percentile(metric_values("peak_return"), Decimal("0.90")),
            },
            "post_peak_drawdown_distribution": {
                "median": metric_median("max_drawdown_from_peak"),
                "p90": percentile(metric_values("max_drawdown_from_peak"), Decimal("0.90")),
            },
        }
        return {
            "candidates": len(candidates),
            "outcomes": len(outcomes),
            "average_return": average.quantize(Decimal("0.01")),
            "median_return": median.quantize(Decimal("0.01")),
            "by_horizon": by_horizon,
            "excursions": excursions,
            "breakdowns": breakdowns,
            "baselines": baselines,
            "score_distribution": score_distribution,
            "path_analytics": path_analytics,
            "best_current_candidates": best_current,
            "last_strong_alert_at": (
                int(last_strong_alert) if last_strong_alert else None
            ),
            "last_strong_alert_mint": last_strong_mint,
            "last_digest_at": int(last_digest) if last_digest else None,
            "last_fast_watch_mint": (
                self.runner_last_fast_watch_mint or stored_fast_watch_mint
            ),
            "last_fast_watch_at": (
                self.runner_last_fast_watch_at
                or (int(stored_fast_watch_at) if stored_fast_watch_at else None)
            ),
            "average_detection_delay_seconds": (
                sum(detection_delays) // len(detection_delays) if detection_delays else None
            ),
            "shadow_mode": True,
            "baseline_status": (
                "collecting — compare RunnerScore with age/volume/price-gain baselines "
                "after at least 30 completed 1h observations"
            ),
        }

    async def discovery_latency(self, *, limit: int = 100) -> dict[str, object]:
        """Source-specific latency with an honest timing grade per sample.

        The old global number treated pair-creation time as a realtime discovery
        event, so an old pair appearing on a trending feed produced an
        ~19-hour "ingestion latency".  Timings are now graded, and only
        realtime-grade samples feed the percentiles.
        """

        await self.initialize()
        rows = await self.database.discovery_latency_rows(limit=limit)
        samples = [
            LabLatencySample(
                mint=str(row["mint"]),
                source_name=str(row.get("source_name") or "unknown"),
                source_event_at=_first_int(
                    row.get("source_event_at"),
                    row.get("pair_created_at"),
                    row.get("chain_created_at"),
                ),
                ingested_at=_first_int(row.get("ingested_at")),
                first_seen_at=_first_int(row.get("first_seen_at")),
                first_watch_at=_first_int(row.get("first_watch_at")),
                first_qualified_at=_first_int(row.get("first_qualified_at")),
                first_discord_at=_first_int(row.get("first_discord_visible_at")),
                first_paper_decision_at=_first_int(row.get("first_paper_decision_at")),
                simulated_fill_at=_first_int(row.get("simulated_fill_at")),
                source_is_realtime=bool(row.get("source_is_realtime", 1)),
            )
            for row in rows
        ]
        breakdown = lab_pipeline_breakdown(samples)
        return {
            "samples": len(samples),
            "sources": summarize_lab_sources(samples),
            "pipeline": breakdown,
            "slowest_stage": lab_slowest_stage(breakdown),
            "realtime_samples": sum(1 for item in samples if item.counts_as_realtime),
            "historical_samples": sum(
                1 for item in samples if item.timing_quality == LAB_HISTORICAL
            ),
            "unknown_samples": sum(
                1 for item in samples if item.timing_quality == LAB_UNKNOWN
            ),
        }

    async def runner_latency(self, *, limit: int = 100) -> dict[str, object]:
        rows = await self.database.runner_latency_rows(limit=limit)

        def percentile(values: list[Decimal], quantile: Decimal) -> Decimal | None:
            ordered = sorted(values)
            if not ordered:
                return None
            if len(ordered) == 1:
                return ordered[0]
            position = Decimal(len(ordered) - 1) * quantile
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            fraction = position - Decimal(lower)
            return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

        source_delays: list[Decimal] = []
        visible_delays: list[Decimal] = []
        first_caps: list[Decimal] = []
        visible_caps: list[Decimal] = []
        appreciations: list[Decimal] = []
        token_rows: list[dict[str, object]] = []
        for row in rows:
            source_at = row.get("chain_created_at") or row.get("pair_created_at") or row.get(
                "graduated_at"
            )
            first_seen = row.get("radar_first_seen_at")
            visible_at = row.get("first_discord_visible_at")
            source_delay = (
                max(0, int(first_seen) - int(source_at))
                if source_at is not None and first_seen is not None
                else None
            )
            visible_delay = (
                max(0, int(visible_at) - int(first_seen))
                if visible_at is not None and first_seen is not None
                else None
            )
            if source_delay is not None:
                source_delays.append(Decimal(source_delay))
            if visible_delay is not None:
                visible_delays.append(Decimal(visible_delay))
            first_cap = row.get("first_market_cap_usd")
            visible_cap = row.get("first_visible_market_cap_usd")
            if first_cap is not None:
                first_caps.append(Decimal(str(first_cap)))
            if visible_cap is not None:
                visible_caps.append(Decimal(str(visible_cap)))
            appreciation = (
                forward_return_percent(Decimal(str(visible_cap)), Decimal(str(first_cap)))
                if first_cap is not None and visible_cap is not None
                else None
            )
            if appreciation is not None:
                appreciations.append(appreciation)
            token_rows.append(
                {
                    "mint": row["mint"],
                    "source_to_first_seen_seconds": source_delay,
                    "first_seen_to_discord_seconds": visible_delay,
                    "mc_at_first_seen": first_cap,
                    "mc_at_first_visible_alert": visible_cap,
                    "mc_at_entry_eligible": row.get("entry_market_cap_usd"),
                    "peak_mc_after_detection": row.get("peak_market_cap_usd"),
                }
            )

        def pct_within(seconds: int) -> Decimal | None:
            if not source_delays:
                return None
            return (
                Decimal(sum(value <= seconds for value in source_delays))
                / Decimal(len(source_delays))
                * Decimal("100")
            ).quantize(Decimal("0.01"))

        return {
            "count": len(rows),
            "source_samples": len(source_delays),
            "visible_samples": len(visible_delays),
            "source_to_first_seen_median": percentile(source_delays, Decimal("0.50")),
            "source_to_first_seen_p90": percentile(source_delays, Decimal("0.90")),
            "first_seen_to_discord_median": percentile(visible_delays, Decimal("0.50")),
            "first_seen_to_discord_p90": percentile(visible_delays, Decimal("0.90")),
            "discovered_within": {
                "30s": pct_within(30),
                "60s": pct_within(60),
                "2m": pct_within(120),
                "5m": pct_within(300),
                "10m": pct_within(600),
            },
            "median_mc_first_seen": percentile(first_caps, Decimal("0.50")),
            "median_mc_first_visible": percentile(visible_caps, Decimal("0.50")),
            "median_mc_appreciation_to_visible": percentile(
                appreciations, Decimal("0.50")
            ),
            "tokens": tuple(token_rows),
        }

    async def runner_calibration(self) -> dict[str, object]:
        """Describe detection-time evidence against later outcomes; never tune thresholds."""

        results = await self.runner_results()
        rows = await self.database.runner_results_rows()
        mints = tuple(dict.fromkeys(str(row["mint"]) for row in rows))
        candidates: dict[str, RunnerCandidate] = {}
        for mint in mints:
            payload = await self.database.runner_candidate_payload(mint)
            if payload:
                candidates[mint] = runner_candidate_from_json(payload)
        excursions = results["excursions"]
        assert isinstance(excursions, dict)

        def cohort(kind: str) -> list[RunnerCandidate]:
            selected: list[RunnerCandidate] = []
            for mint, item in candidates.items():
                path = excursions.get(mint, {})
                if not isinstance(path, dict):
                    continue
                if kind == "winner" and Decimal(
                    str(path.get("maximum_favorable_excursion") or -999)
                ) >= 25:
                    selected.append(item)
                if kind == "failure" and (
                    bool(path.get("rug_or_liquidity_failure"))
                    or Decimal(str(path.get("max_drawdown_from_peak") or 0)) >= 80
                ):
                    selected.append(item)
            return selected

        def median(values: list[Decimal]) -> Decimal | None:
            ordered = sorted(values)
            return ordered[len(ordered) // 2] if ordered else None

        def characteristics(items: list[RunnerCandidate]) -> dict[str, object]:
            def decimals(getter) -> list[Decimal]:
                return [value for item in items if (value := getter(item)) is not None]

            return {
                "count": len(items),
                "initial_market_cap": median(decimals(lambda item: item.first.market_cap_usd)),
                "liquidity": median(decimals(lambda item: item.first.liquidity_usd)),
                "holders": median(
                    decimals(
                        lambda item: (
                            Decimal(item.first.holder_count)
                            if item.first.holder_count is not None
                            else None
                        )
                    )
                ),
                "pair_age_seconds": median(
                    decimals(
                        lambda item: (
                            Decimal(item.first_seen_at - item.pair_created_at)
                            if item.pair_created_at is not None
                            else None
                        )
                    )
                ),
                "top10": median(decimals(lambda item: item.first.top10_percent)),
                "dev": median(decimals(lambda item: item.first.dev_percent)),
                "bundlers": median(decimals(lambda item: item.first.bundlers_percent)),
                "insiders": median(decimals(lambda item: item.first.insiders_percent)),
                "snipers": median(decimals(lambda item: item.first.snipers_percent)),
                "largest_cluster": median(
                    decimals(
                        lambda item: item.detection_forensics.largest_cluster_supply_percent
                    )
                ),
                "shared_funders": median(
                    [
                        Decimal(len(item.detection_forensics.shared_funder_groups))
                        for item in items
                    ]
                ),
                "buyer_independence": median(
                    decimals(
                        lambda item: (
                            Decimal(item.detection_forensics.estimated_independent_clusters)
                            if item.detection_forensics.estimated_independent_clusters is not None
                            else None
                        )
                    )
                ),
                "smart_wallet_overlap": median(
                    [Decimal(item.first.smart_wallet_count) for item in items]
                ),
                "sell_route_pass_rate": (
                    Decimal(sum(item.first.sell_route_status == "PASS" for item in items))
                    / Decimal(len(items))
                    * Decimal("100")
                    if items
                    else None
                ),
            }

        detection_scores = sorted(
            item.detection_score if item.detection_score is not None else item.score
            for item in candidates.values()
        )

        def score_percentile(quantile: Decimal) -> Decimal | None:
            if not detection_scores:
                return None
            if len(detection_scores) == 1:
                return detection_scores[0]
            position = Decimal(len(detection_scores) - 1) * quantile
            lower = int(position)
            upper = min(lower + 1, len(detection_scores) - 1)
            fraction = position - Decimal(lower)
            return detection_scores[lower] + (
                detection_scores[upper] - detection_scores[lower]
            ) * fraction

        detection_distribution = {
            "max": max(detection_scores) if detection_scores else None,
            "median": score_percentile(Decimal("0.50")),
            "p90": score_percentile(Decimal("0.90")),
            "p95": score_percentile(Decimal("0.95")),
            "gte_15": sum(value >= 15 for value in detection_scores),
            "gte_20": sum(value >= 20 for value in detection_scores),
            "gte_35": sum(value >= 35 for value in detection_scores),
            "gte_50": sum(value >= 50 for value in detection_scores),
            "gte_60": sum(value >= 60 for value in detection_scores),
            "gte_70": sum(value >= 70 for value in detection_scores),
        }
        # Chronological split, never random: a random split would let a token
        # observed after a threshold was chosen validate that threshold.
        ordered_candidates = sorted(
            candidates.values(),
            key=lambda item: item.first_seen_at,
        )
        split_index = int(len(ordered_candidates) * 0.7)
        calibration_slice = ordered_candidates[:split_index]
        holdout_slice = ordered_candidates[split_index:]

        def qualification_rate(items: list[RunnerCandidate]) -> dict[str, object]:
            measured = [item for item in items if item.detection_quality.evaluated_at]
            qualified_items = [
                item
                for item in measured
                if self.database.user_facing_stage(item.detection_quality.stage)
            ]
            winners = 0
            for item in qualified_items:
                path = excursions.get(item.mint, {})
                if isinstance(path, dict) and path.get("plus_25_before_minus_25") is True:
                    winners += 1
            return {
                "observed": len(items),
                "with_decision_snapshot": len(measured),
                "qualified": len(qualified_items),
                "qualified_plus_25_before_minus_25": winners,
                "precision_percent": (
                    (
                        Decimal(winners) / Decimal(len(qualified_items)) * Decimal("100")
                    ).quantize(Decimal("0.01"))
                    if qualified_items
                    else None
                ),
                "window": (
                    (items[0].first_seen_at, items[-1].first_seen_at) if items else None
                ),
            }

        return {
            "candidate_count": results["candidates"],
            "outcome_count": results["outcomes"],
            "score_distribution": detection_distribution,
            "winner_characteristics": characteristics(cohort("winner")),
            "failure_characteristics": characteristics(cohort("failure")),
            "safety_buckets": results["breakdowns"]["safety"],
            "walk_forward": {
                "split": "chronological 70/30 on first_seen_at",
                "calibration": qualification_rate(calibration_slice),
                "holdout": qualification_rate(holdout_slice),
                "note": (
                    "Defaults ship unfitted. Compare these two slices before "
                    "changing any FOMO_RUNNER_* threshold; a rule that only "
                    "works on the calibration slice is overfitted."
                ),
            },
            "thresholds_changed": False,
            "no_look_ahead": True,
        }

    async def runner_quality_report(self, *, since_days: int = 7) -> dict[str, object]:
        """Funnel throughput, alert precision and the missed-runner counterfactual.

        Silent and rejected candidates are scored with exactly the same forward
        maths as alerted ones.  Without that, a filter that quietly rejects
        every winner looks perfect.  All returns are measured forward from an
        observation that already existed at the time, so nothing here uses
        information the decision could not have had.
        """

        now = int(time.time())
        since = now - max(1, since_days) * 86_400
        rows = await self.database.runner_funnel_rows(since=since)
        stage_counts = await self.database.runner_stage_counts(since=since)
        snapshot_rows = await self.database.runner_all_snapshot_rows()
        by_mint: dict[str, list[RunnerMarketSnapshot]] = {}
        for row in snapshot_rows:
            mint = str(row["mint"])
            by_mint.setdefault(mint, []).append(
                runner_snapshot_from_json(str(row["snapshot_json"]))
            )

        qualified: list[dict[str, object]] = []
        silent: list[dict[str, object]] = []
        for row in rows:
            mint = str(row["mint"])
            series = sorted(by_mint.get(mint, ()), key=lambda item: item.captured_at)
            if not series:
                continue
            path = runner_path_metrics(series[0], series)
            qualified_at = row.get("qualified_at")
            post_alert = None
            if qualified_at is not None:
                after = [
                    item for item in series if item.captured_at >= int(qualified_at)
                ]
                if after:
                    post_alert = runner_path_metrics(after[0], after)
            entry = {
                "mint": mint,
                "best_stage": str(row.get("best_stage") or STAGE_RAW),
                "qualified_at": qualified_at,
                "first_seen_at": row.get("radar_first_seen_at") or row.get("first_seen_at"),
                "source_at": (
                    row.get("chain_created_at")
                    or row.get("pair_created_at")
                    or row.get("graduated_at")
                ),
                "visible_at": row.get("first_discord_visible_at"),
                "mc_first_seen": row.get("first_market_cap_usd"),
                "mc_qualified": row.get("qualified_market_cap_usd"),
                "mc_visible": row.get("first_visible_market_cap_usd"),
                "mc_entry": row.get("entry_market_cap_usd"),
                "mc_peak": row.get("peak_market_cap_usd"),
                "path": path,
                "post_alert_path": post_alert,
            }
            if self.database.user_facing_stage(entry["best_stage"]):
                qualified.append(entry)
            else:
                silent.append(entry)

        def performance(items: list[dict[str, object]], *, key: str = "path") -> dict[str, object]:
            paths = [
                item[key] for item in items if isinstance(item.get(key), dict)
            ]

            def hits(field: str) -> int:
                return sum(1 for path in paths if path.get(field) is True)

            def reached(level: int) -> int:
                return sum(
                    1
                    for path in paths
                    if path.get("peak_return") is not None
                    and Decimal(str(path["peak_return"])) >= level
                )

            severe = sum(
                1
                for path in paths
                if path.get("rug_or_liquidity_failure")
                or Decimal(str(path.get("max_drawdown_from_peak") or 0)) >= 80
            )
            return {
                "count": len(items),
                "measured": len(paths),
                "plus_10_before_minus_25": hits("plus_10_before_minus_25"),
                "plus_25_before_minus_25": hits("plus_25_before_minus_25"),
                "plus_50_before_minus_50": hits("plus_50_before_minus_50"),
                "plus_100_before_minus_50": hits("plus_100_before_minus_50"),
                "reached_50": reached(50),
                "reached_100": reached(100),
                "reached_200": reached(200),
                "severe_failures": severe,
                "severe_failure_rate_percent": (
                    (Decimal(severe) / Decimal(len(paths)) * Decimal("100")).quantize(
                        Decimal("0.01")
                    )
                    if paths
                    else None
                ),
            }

        qualified_performance = performance(qualified)
        post_alert_performance = performance(qualified, key="post_alert_path")
        silent_performance = performance(silent)
        missed = [
            item
            for item in silent
            if isinstance(item.get("path"), dict)
            and item["path"].get("peak_return") is not None
            and Decimal(str(item["path"]["peak_return"])) >= 50
        ]
        measured_qualified = int(qualified_performance["measured"] or 0)
        precision = (
            Decimal(int(qualified_performance["plus_25_before_minus_25"]))
            / Decimal(measured_qualified)
            * Decimal("100")
        ).quantize(Decimal("0.01")) if measured_qualified else None
        missed_rate = (
            Decimal(len(missed))
            / Decimal(len(missed) + int(qualified_performance["reached_50"] or 0))
            * Decimal("100")
        ).quantize(Decimal("0.01")) if (missed or qualified_performance["reached_50"]) else None

        def percentile(values: list[int], quantile: Decimal) -> int | None:
            ordered = sorted(values)
            if not ordered:
                return None
            position = int((Decimal(len(ordered) - 1) * quantile).to_integral_value())
            return ordered[position]

        source_delays = [
            int(item["first_seen_at"]) - int(item["source_at"])
            for item in (*qualified, *silent)
            if item.get("source_at") and item.get("first_seen_at")
            and int(item["first_seen_at"]) >= int(item["source_at"])
        ]
        qualify_delays = [
            int(item["qualified_at"]) - int(item["first_seen_at"])
            for item in qualified
            if item.get("qualified_at") and item.get("first_seen_at")
            and int(item["qualified_at"]) >= int(item["first_seen_at"])
        ]
        lost_moves = [
            forward_return_percent(
                Decimal(str(item["mc_qualified"])),
                Decimal(str(item["mc_first_seen"])),
            )
            for item in qualified
            if item.get("mc_qualified") and item.get("mc_first_seen")
        ]
        lost_moves = [value for value in lost_moves if value is not None]
        provider_rows = await self.database.provider_call_rows(usage_day=self._x_usage_day())
        return {
            "window_days": since_days,
            "raw_universe": len(rows),
            "stage_counts": stage_counts,
            "silent_watched": len(silent),
            "qualified": len(qualified),
            "qualified_performance": qualified_performance,
            "post_alert_performance": post_alert_performance,
            "silent_performance": silent_performance,
            "missed_runners": len(missed),
            "missed_runner_examples": tuple(item["mint"] for item in missed[:5]),
            "alert_precision_percent": precision,
            "missed_runner_rate_percent": missed_rate,
            "latency": {
                "source_to_first_seen_p50": percentile(source_delays, Decimal("0.50")),
                "source_to_first_seen_p90": percentile(source_delays, Decimal("0.90")),
                "first_seen_to_qualified_p50": percentile(qualify_delays, Decimal("0.50")),
                "first_seen_to_qualified_p90": percentile(qualify_delays, Decimal("0.90")),
            },
            "move_lost_before_visibility_median": (
                sorted(lost_moves)[len(lost_moves) // 2] if lost_moves else None
            ),
            "provider_calls": tuple(provider_rows),
            "degraded_providers": tuple(
                name
                for name, degraded in (
                    ("solana_tracker", self.tracker_token_risk.degraded),
                )
                if degraded
            ),
            "no_look_ahead": True,
            "thresholds_changed": False,
        }

    async def _handle_news_alert(self, alert: NewsAlert) -> None:
        await self._evaluate_news_alert(alert, publish=True)

    async def _evaluate_news_alert(self, alert: NewsAlert, *, publish: bool) -> None:
        now = int(time.time())
        while self._news_alert_times and now - self._news_alert_times[0] >= 3600:
            self._news_alert_times.popleft()
        if not is_coin_actionable_news(alert):
            return
        # A contract in a news/X post is only a nomination. Run the complete coin
        # verification pipeline first; never publish the headline as proof that the
        # token is safe, liquid, or authentically promoted.
        event = await self.observe_catalyst(alert, now=now)
        if alert.token_mints:
            self._remember_news_event(alert, now=now)
            for mint in alert.token_mints:
                self._queue_coin_callout(mint)
            if event is not None and publish:
                for mint in alert.token_mints:
                    with suppress(Exception):
                        await self.evaluate_catalyst_token(mint=mint, event=event, now=now)
            return
        if event is not None and publish and not alert.token_mints:
            # An event with no token yet is still worth surfacing on its own
            # merits; the token half stays absent rather than being guessed.
            with suppress(Exception):
                await self._maybe_publish_catalyst(event, now=now)
        preliminary = score_launch_opportunity(
            alert,
            now=now,
            watch_score=self.settings.news_min_score,
            launch_ready_score=self.settings.news_launch_ready_score,
            no_x_candidates_enabled=self.settings.no_x_launch_candidates_enabled,
            no_x_launch_min_score=self.settings.no_x_launch_min_score,
        )
        preliminary_floor = (
            self.settings.news_min_score
            if self.settings.no_x_launch_candidates_enabled
            else self.settings.news_x_verify_min_score
        )
        if preliminary.score < preliminary_floor and not alert.token_mints:
            return

        # Complete every free blocker before spending a paid X resource.
        competition = (
            await self.news_matcher.competition(preliminary.primary_narrative)
            if self.settings.news_dex_match_enabled
            else preliminary.competition
        )
        cross_sources = self._cross_source_count(alert, now=now)
        free_opportunity = score_launch_opportunity(
            alert,
            competition=competition,
            cross_source_count=cross_sources,
            now=now,
            watch_score=self.settings.news_min_score,
            launch_ready_score=self.settings.news_launch_ready_score,
            no_x_candidates_enabled=self.settings.no_x_launch_candidates_enabled,
            no_x_launch_min_score=self.settings.no_x_launch_min_score,
        )
        x_eligible, _x_reason = should_request_x_for_launch_opportunity(
            free_opportunity,
            minimum_score=self.settings.news_x_verify_min_score,
        )
        x_evidence = free_opportunity.x_evidence
        if self.x_social.search_enabled and x_eligible:
            x_evidence = await self.x_social.narrative_snapshot(
                free_opportunity.primary_narrative,
                context="automatic_news",
                free_score=free_opportunity.score,
            )
        opportunity = score_launch_opportunity(
            alert,
            x_evidence=x_evidence,
            competition=competition,
            cross_source_count=cross_sources,
            now=now,
            watch_score=self.settings.news_min_score,
            launch_ready_score=self.settings.news_launch_ready_score,
            no_x_candidates_enabled=self.settings.no_x_launch_candidates_enabled,
            no_x_launch_min_score=self.settings.no_x_launch_min_score,
            pre_x_score=free_opportunity.score,
        )
        if x_evidence.available:
            outcome = "UPGRADED" if opportunity.x_verified else "WEAK"
            await self.x_budget.record_outcome(
                x_evidence.verification_id,
                free_score=free_opportunity.score,
                final_score=opportunity.score,
                outcome=outcome,
            )
        self._remember_news_event(alert, now=now)
        await self._cache_launch_candidate(opportunity, now=now)
        if not publish:
            return
        # Discord receives only something the user can actually launch. WATCH/SKIP
        # rows remain internal research evidence and no longer create dead buttons.
        if not should_publish_news_opportunity(opportunity):
            return
        if len(self._news_alert_times) >= self.settings.news_max_alerts_per_hour:
            return
        self._news_alert_times.append(now)
        await self.notifier.on_news_alert(opportunity.alert, opportunity)
        if self.settings.news_dex_match_enabled and not alert.token_mints and alert.narrative_terms:
            task = asyncio.create_task(
                self._run_narrative_match(alert),
                name=f"news-narrative-match-{now}",
            )
            self._news_match_tasks.add(task)
            task.add_done_callback(self._news_match_tasks.discard)

    async def _cache_launch_candidate(
        self,
        opportunity: LaunchOpportunity,
        *,
        now: int,
    ) -> None:
        if not hasattr(self, "database"):
            return
        if opportunity.score < self.settings.news_min_score or opportunity.alert.token_mints:
            return
        await self.database.cache_launch_candidate(
            cluster_key=launch_cluster_key(opportunity),
            alert_key=alert_key(opportunity.alert),
            payload_json=launch_opportunity_to_json(opportunity),
            headline=opportunity.alert.headline,
            source_url=opportunity.alert.url,
            category=opportunity.category,
            score=opportunity.score,
            verdict=opportunity.verdict,
            evaluated_at=now,
            expires_at=now + self.settings.launch_lab_max_age_seconds,
        )

    def _cross_source_count(self, alert: NewsAlert, *, now: int) -> int:
        while self._recent_news_events and now - self._recent_news_events[0][0] > 600:
            self._recent_news_events.popleft()
        terms = {item.casefold() for item in alert.narrative_terms}
        if not terms:
            return 0
        current_source = (alert.author or alert.source).casefold()
        sources = {
            source
            for _, source, previous_terms in self._recent_news_events
            if source != current_source and terms.intersection(previous_terms)
        }
        return len(sources)

    def _remember_news_event(self, alert: NewsAlert, *, now: int) -> None:
        terms = frozenset(item.casefold() for item in alert.narrative_terms)
        if terms:
            self._recent_news_events.append((now, (alert.author or alert.source).casefold(), terms))

    async def launch_lab_candidates(self, *, topic: str = "") -> tuple[LaunchOpportunity, ...]:
        """Refresh authorized feeds and return recent, clustered, manual-review candidates."""

        if not self.settings.launch_lab_enabled:
            return ()
        async with self._launch_lab_lock:
            alerts = await self.news_poller.snapshot(
                max_age_seconds=self.settings.launch_lab_max_age_seconds
            )
            for alert in alerts:
                await self._evaluate_news_alert(alert, publish=False)
        now = int(time.time())
        payloads = await self.database.recent_launch_candidate_payloads(
            now=now,
            limit=self.settings.launch_lab_max_candidates * 3,
        )
        needle = topic.strip().casefold()
        candidates: list[LaunchOpportunity] = []
        for payload in payloads:
            try:
                opportunity = launch_opportunity_from_json(payload)
            except (TypeError, ValueError, KeyError):
                continue
            if not is_launch_lab_eligible(
                opportunity,
                minimum_score=self.settings.launch_lab_min_score,
                max_age_seconds=self.settings.launch_lab_max_age_seconds,
                now=now,
            ):
                continue
            searchable = (
                f"{opportunity.alert.headline} {opportunity.primary_narrative} "
                f"{opportunity.alert.url}"
            ).casefold()
            if needle and needle not in searchable:
                continue
            candidates.append(opportunity)
            if len(candidates) >= self.settings.launch_lab_max_candidates:
                break
        if candidates:
            await self.database.set_setting(
                "launch_last_lab_candidate",
                str(int(time.time())),
            )
        return tuple(candidates)

    async def launch_lab_test_candidates(
        self,
        *,
        topic: str = "",
    ) -> tuple[LaunchOpportunity, ...]:
        """Analyze real current evidence while bypassing only the Lab display floor."""

        if not self.settings.launch_lab_enabled:
            return ()
        research_age = max(self.settings.launch_lab_max_age_seconds, 21_600)
        preferred_alert_key = ""
        async with self._launch_lab_lock:
            alerts = list(await self.news_poller.snapshot(max_age_seconds=research_age))
            requested = topic.strip()
            if requested.casefold().startswith("https://"):
                article = await self.news_poller.public_article(requested)
                if article is not None:
                    preferred_alert_key = alert_key(article)
                    alerts.insert(0, article)
            elif requested:
                alerts = (
                    list(
                        await self.news_poller.topic_snapshot(
                            requested,
                            max_age_seconds=research_age,
                        )
                    )
                    + alerts
                )
        if not alerts:
            return ()

        # Analyze a bounded current set with the production scorer and DEX competition
        # client. Nothing in this path publishes, launches, or reserves launch funds.
        seen_alerts: set[str] = set()
        current: list[NewsAlert] = []
        for alert in alerts:
            key = alert_key(alert)
            if key in seen_alerts or not alert.headline or not alert.url.startswith("https://"):
                continue
            seen_alerts.add(key)
            current.append(alert)
            if len(current) >= max(12, self.settings.launch_lab_max_candidates * 3):
                break

        now = int(time.time())
        preliminary = [
            score_launch_opportunity(
                alert,
                now=now,
                watch_score=self.settings.news_min_score,
                launch_ready_score=self.settings.news_launch_ready_score,
                no_x_candidates_enabled=self.settings.no_x_launch_candidates_enabled,
                no_x_launch_min_score=self.settings.no_x_launch_min_score,
            )
            for alert in current
        ]
        competitions = await asyncio.gather(
            *(
                self.news_matcher.competition(item.primary_narrative)
                if self.settings.news_dex_match_enabled
                else asyncio.sleep(0, result=item.competition)
                for item in preliminary
            )
        )

        def cross_source_count(alert: NewsAlert) -> int:
            terms = {value.casefold() for value in alert.narrative_terms}
            if not terms:
                return 0
            source = (alert.author or alert.source).casefold()
            sources = {
                (other.author or other.source).casefold()
                for other in current
                if (other.author or other.source).casefold() != source
                and terms.intersection(value.casefold() for value in other.narrative_terms)
            }
            return len(sources)

        opportunities = [
            score_launch_opportunity(
                alert,
                competition=competition,
                cross_source_count=cross_source_count(alert),
                now=now,
                watch_score=self.settings.news_min_score,
                launch_ready_score=self.settings.news_launch_ready_score,
                no_x_candidates_enabled=self.settings.no_x_launch_candidates_enabled,
                no_x_launch_min_score=self.settings.no_x_launch_min_score,
            )
            for alert, competition in zip(current, competitions, strict=True)
        ]

        # Keep one representative per narrative. A topic is a preference in test
        # mode, not a hard filter, so the command remains deterministic on demand.
        needle = "" if requested.casefold().startswith("https://") else requested.casefold()
        clustered: dict[str, LaunchOpportunity] = {}
        for opportunity in opportunities:
            key = launch_cluster_key(opportunity)
            existing = clustered.get(key)
            if existing is None or (
                opportunity.score,
                opportunity.alert.created_at,
            ) > (existing.score, existing.alert.created_at):
                clustered[key] = opportunity
        ordered = sorted(
            clustered.values(),
            key=lambda item: (
                int(alert_key(item.alert) == preferred_alert_key),
                int(
                    bool(needle)
                    and needle
                    in (
                        f"{item.alert.headline} {item.primary_narrative} {item.alert.url}"
                    ).casefold()
                ),
                item.alert.created_at,
                item.score,
            ),
            reverse=True,
        )[: self.settings.launch_lab_max_candidates]
        for opportunity in ordered:
            await self._cache_launch_candidate(opportunity, now=now)
        if ordered:
            await self.database.set_setting("launch_last_lab_candidate", str(now))
        return tuple(ordered)

    async def verify_launch_lab_candidate(
        self,
        opportunity: LaunchOpportunity,
        *,
        research_test: bool = False,
    ) -> LaunchOpportunity:
        """Run one admin-requested targeted X check; this never calls J7."""

        eligible, reason = should_request_x_for_launch_opportunity(
            opportunity,
            minimum_score=self.settings.launch_lab_min_score,
        )
        if not eligible and not research_test:
            return replace(
                opportunity,
                x_evidence=replace(
                    opportunity.x_evidence,
                    available=False,
                    error=f"X VERIFICATION SKIPPED — {reason}",
                    verification_state="NOT_VERIFIED",
                ),
            )
        x_evidence = await self.x_social.narrative_snapshot(
            opportunity.primary_narrative,
            context="launch_lab_test" if research_test else "launch_lab_manual",
            free_score=opportunity.score,
        )
        updated = score_launch_opportunity(
            opportunity.alert,
            x_evidence=x_evidence,
            competition=opportunity.competition,
            cross_source_count=opportunity.cross_source_count,
            watch_score=self.settings.news_min_score,
            launch_ready_score=self.settings.news_launch_ready_score,
            no_x_candidates_enabled=self.settings.no_x_launch_candidates_enabled,
            no_x_launch_min_score=self.settings.no_x_launch_min_score,
            pre_x_score=opportunity.score,
        )
        if x_evidence.available:
            await self.x_budget.record_outcome(
                x_evidence.verification_id,
                free_score=opportunity.score,
                final_score=updated.score,
                outcome="UPGRADED" if updated.x_verified else "WEAK",
            )
            await self._cache_launch_candidate(updated, now=int(time.time()))
        return updated

    async def launch_readiness(self) -> dict[str, object]:
        """Read-only J7/IPFS/wallet/database probe. This method cannot submit a launch."""

        self.x_budget.database = self.database
        checked_at = int(time.time())
        j7 = self.pump_launcher.j7
        j7_healthy, j7_status = await j7.health_check()
        pinata_healthy, pinata_status = await j7.pinata_health()
        wallet_balance: Decimal | None = None
        wallet_error: str | None = None
        try:
            wallet_balance = await j7.wallet_balance()
        except PumpLaunchError as exc:
            wallet_error = str(exc)
        reservation_healthy, pending, unknown = await self.database.launch_reservation_health()
        start_at, end_at = self._launch_day_bounds()
        launches, spent_sol = await self.database.pump_launch_daily_usage(
            start_at=start_at,
            end_at=end_at,
        )
        required_balance = (
            self.settings.pump_launch_initial_buy_sol
            + self.settings.j7_launch_min_balance_buffer_sol
        )
        failures: list[str] = []
        if not self.settings.j7_launch_is_unlocked:
            failures.append("J7 configuration is incomplete")
        if not j7_healthy:
            failures.append(j7_status)
        if not pinata_healthy:
            failures.append(pinata_status)
        if wallet_error:
            failures.append(wallet_error)
        elif wallet_balance is not None and wallet_balance < required_balance:
            failures.append(f"INSUFFICIENT SOL — at least {required_balance} SOL is required")
        if not reservation_healthy:
            failures.append("launch reservations are unhealthy")
        if unknown:
            failures.append(f"{unknown} launch has an UNKNOWN_RESULT requiring reconciliation")
        await self.database.set_setting("launch_last_j7_health_check", str(checked_at))
        if wallet_balance is not None:
            await self.database.set_setting("launch_last_wallet_balance_check", str(checked_at))
        stats = await self.database.launch_candidate_stats(start_at=start_at, end_at=end_at)
        x_budget_status = await self.x_budget.status()
        last_lab_candidate = await self.database.get_setting("launch_last_lab_candidate")
        last_pinata_success = await self.database.get_setting("launch_last_pinata_success")
        last_launch_attempt = await self.database.get_setting("launch_last_attempt")
        last_successful_mint = await self.database.get_setting("launch_last_successful_mint")
        return {
            "bot_version": BOT_VERSION,
            "provider": "J7 Tracker",
            "j7_configured": self.settings.j7_launch_is_unlocked,
            "j7_api_key_configured": bool(self.settings.j7_launch_api_key),
            "j7_session_configured": bool(self.settings.j7_launch_session_token),
            "j7_region": self.settings.j7_launch_region,
            "j7_endpoint": j7_status,
            "pinata": pinata_status,
            "wallet": j7.wallet_address,
            "wallet_configured_value": bool(self.settings.j7_launch_wallet_address),
            "wallet_balance": wallet_balance,
            "creator_buy": self.settings.pump_launch_initial_buy_sol,
            "required_balance": required_balance,
            "launches_today": launches,
            "launches_limit": self.settings.pump_launch_max_per_day,
            "sol_today": spent_sol,
            "sol_limit": self.settings.pump_launch_max_sol_per_day,
            "duplicate_protection": "READY",
            "reservations": "HEALTHY" if reservation_healthy else "UNHEALTHY",
            "pending_reservations": pending,
            "unknown_results": unknown,
            "candidate_stats": stats,
            "x_budget": x_budget_status,
            "last_lab_candidate": int(last_lab_candidate) if last_lab_candidate else None,
            "last_pinata_success": int(last_pinata_success) if last_pinata_success else None,
            "last_launch_attempt": int(last_launch_attempt) if last_launch_attempt else None,
            "last_successful_mint": last_successful_mint,
            "overall_ready": not failures,
            "failures": tuple(dict.fromkeys(failures)),
            "checked_at": checked_at,
        }

    def _launch_day_bounds(self) -> tuple[int, int]:
        timezone = ZoneInfo(self.settings.pump_launch_timezone)
        local_now = datetime.now(timezone)
        day_start = datetime.combine(local_now.date(), datetime_time.min, tzinfo=timezone)
        return int(day_start.timestamp()), int((day_start + timedelta(days=1)).timestamp())

    async def launch_lab_draft(
        self,
        draft: LaunchDraft,
        *,
        requested_by: str,
    ) -> PumpLaunchResult:
        """Submit one confirmed Launch Lab draft through J7 only."""

        now = int(time.time())
        opportunity = draft.opportunity
        key = launch_draft_key(draft)
        try:
            draft = validate_launch_draft(
                draft,
                maximum_buy_sol=self.settings.pump_launch_initial_buy_sol,
            )
        except PumpLaunchError as exc:
            return PumpLaunchResult(
                success=False,
                status="BLOCKED",
                message=str(exc),
                alert_key=key,
                name=draft.name,
                symbol=draft.symbol,
                created_at=now,
                provider="J7 Tracker",
            )
        if not is_launch_lab_eligible(
            opportunity,
            minimum_score=self.settings.launch_lab_min_score,
            max_age_seconds=self.settings.launch_lab_max_age_seconds,
            now=now,
        ):
            return PumpLaunchResult(
                success=False,
                status="BLOCKED",
                message="Candidate is weak, stale, blocked, or no longer competition-safe.",
                alert_key=key,
                name=draft.name,
                symbol=draft.symbol,
                created_at=now,
                provider="J7 Tracker",
            )
        if not self.pump_launcher.j7.configured:
            return PumpLaunchResult(
                success=False,
                status="J7_NOT_READY",
                message="J7 configuration is incomplete; direct Pump fallback was not used.",
                alert_key=key,
                name=draft.name,
                symbol=draft.symbol,
                created_at=now,
                provider="J7 Tracker",
            )
        try:
            balance = await self.pump_launcher.j7.wallet_balance()
        except PumpLaunchError as exc:
            return PumpLaunchResult(
                success=False,
                status="BALANCE_CHECK_FAILED",
                message=str(exc),
                alert_key=key,
                name=draft.name,
                symbol=draft.symbol,
                created_at=now,
                provider="J7 Tracker",
            )
        required_balance = draft.creator_buy_sol + self.settings.j7_launch_min_balance_buffer_sol
        if balance < required_balance:
            return PumpLaunchResult(
                success=False,
                status="INSUFFICIENT_SOL",
                message=(
                    f"INSUFFICIENT SOL — wallet has {balance} SOL; controlled launch "
                    f"requires at least {required_balance} SOL. J7 was not called."
                ),
                alert_key=key,
                name=draft.name,
                symbol=draft.symbol,
                created_at=now,
                provider="J7 Tracker",
            )
        start_at, end_at = self._launch_day_bounds()
        launches, spent_sol = await self.database.pump_launch_daily_usage(
            start_at=start_at,
            end_at=end_at,
        )
        if launches >= self.settings.pump_launch_max_per_day:
            return self._launch_block_result(
                draft,
                key,
                "DAILY_LIMIT",
                "Daily launch-count limit reached.",
            )
        if spent_sol + draft.creator_buy_sol > self.settings.pump_launch_max_sol_per_day:
            return self._launch_block_result(
                draft,
                key,
                "DAILY_LIMIT",
                "Daily launch initial-buy SOL limit reached.",
            )
        if hasattr(self.database, "pump_launch_identity_exists") and (
            await self.database.pump_launch_identity_exists(
                name=draft.name,
                symbol=draft.symbol,
            )
        ):
            return self._launch_block_result(
                draft,
                key,
                "DUPLICATE",
                "This coin name or ticker already has a persistent launch record.",
            )
        reserved = await self.database.reserve_pump_launch(
            alert_key=key,
            source_url=opportunity.alert.url,
            headline=opportunity.alert.headline,
            name=draft.name,
            symbol=draft.symbol,
            score=opportunity.score,
            initial_buy_sol=draft.creator_buy_sol,
            requested_by=requested_by,
        )
        if not reserved:
            return self._launch_block_result(
                draft,
                key,
                "DUPLICATE",
                "This narrative already has a persistent launch record; retry was blocked.",
            )
        await self.database.set_setting("launch_last_attempt", str(now))
        try:
            result = await self.pump_launcher.j7.launch(
                opportunity,
                draft=draft,
                allow_launch_lab=True,
            )
            await self.database.complete_pump_launch(
                alert_key=key,
                status=result.status,
                mint=result.mint,
                signature=result.signature,
                metadata_uri=result.metadata_uri,
            )
            await self.database.set_setting("launch_last_successful_mint", result.mint)
            await self.database.set_setting("launch_last_pinata_success", str(int(time.time())))
            return result
        except UnknownLaunchResultError as exc:
            await self.database.mark_pump_launch_unknown(key, str(exc))
            return self._launch_block_result(draft, key, "UNKNOWN_RESULT", str(exc))
        except Exception as exc:
            message = str(exc)[:500]
            await self.database.fail_pump_launch(key, message)
            return self._launch_block_result(
                draft,
                key,
                _launch_failure_status(message),
                message,
            )

    def _launch_block_result(
        self,
        draft: LaunchDraft,
        key: str,
        status: str,
        message: str,
    ) -> PumpLaunchResult:
        return PumpLaunchResult(
            success=False,
            status=status,
            message=message,
            alert_key=key,
            name=draft.name,
            symbol=draft.symbol,
            created_at=int(time.time()),
            provider="J7 Tracker",
        )

    async def launch_news_opportunity(
        self,
        opportunity: LaunchOpportunity,
        *,
        requested_by: str,
    ) -> PumpLaunchResult:
        now = int(time.time())
        key = alert_key(opportunity.alert)
        if not is_manual_launch_opportunity(opportunity):
            return PumpLaunchResult(
                success=False,
                status="BLOCKED",
                message="This alert did not pass a manual launch-candidate tier.",
                alert_key=key,
                name=opportunity.coin_name,
                symbol=opportunity.coin_symbol,
                created_at=now,
            )
        j7 = getattr(self.pump_launcher, "j7", None)
        if j7 is not None and j7.configured:
            return await self.launch_lab_draft(
                default_launch_draft(
                    opportunity,
                    self.settings.pump_launch_initial_buy_sol,
                ),
                requested_by=requested_by,
            )
        if not self.pump_launcher.configured:
            return PumpLaunchResult(
                success=False,
                status="LOCKED",
                message="One-click launch is locked or missing its J7/direct-Pump credentials.",
                alert_key=key,
                name=opportunity.coin_name,
                symbol=opportunity.coin_symbol,
                created_at=now,
            )

        timezone = ZoneInfo(self.settings.pump_launch_timezone)
        local_now = datetime.now(timezone)
        day_start = datetime.combine(local_now.date(), datetime_time.min, tzinfo=timezone)
        day_end = day_start + timedelta(days=1)
        launches, spent_sol = await self.database.pump_launch_daily_usage(
            start_at=int(day_start.timestamp()),
            end_at=int(day_end.timestamp()),
        )
        next_spend = spent_sol + self.settings.pump_launch_initial_buy_sol
        if launches >= self.settings.pump_launch_max_per_day:
            return PumpLaunchResult(
                success=False,
                status="DAILY_LIMIT",
                message="Daily token launch-count limit reached.",
                alert_key=key,
                name=opportunity.coin_name,
                symbol=opportunity.coin_symbol,
                created_at=now,
            )
        if next_spend > self.settings.pump_launch_max_sol_per_day:
            return PumpLaunchResult(
                success=False,
                status="DAILY_LIMIT",
                message="Daily launch initial-buy SOL limit reached.",
                alert_key=key,
                name=opportunity.coin_name,
                symbol=opportunity.coin_symbol,
                created_at=now,
            )

        reserved = await self.database.reserve_pump_launch(
            alert_key=key,
            source_url=opportunity.alert.url,
            headline=opportunity.alert.headline,
            name=opportunity.coin_name,
            symbol=opportunity.coin_symbol,
            score=opportunity.score,
            initial_buy_sol=self.settings.pump_launch_initial_buy_sol,
            requested_by=requested_by,
        )
        if not reserved:
            return PumpLaunchResult(
                success=False,
                status="DUPLICATE",
                message="This alert already has a launch record; a second coin was blocked.",
                alert_key=key,
                name=opportunity.coin_name,
                symbol=opportunity.coin_symbol,
                created_at=now,
            )
        try:
            result = await self.pump_launcher.launch(opportunity)
            await self.database.complete_pump_launch(
                alert_key=key,
                status=result.status,
                mint=result.mint,
                signature=result.signature,
                metadata_uri=result.metadata_uri,
            )
            return result
        except Exception as exc:
            await self.database.fail_pump_launch(key, str(exc))
            return PumpLaunchResult(
                success=False,
                status="FAILED",
                message=str(exc)[:500],
                alert_key=key,
                name=opportunity.coin_name,
                symbol=opportunity.coin_symbol,
                created_at=now,
            )

    async def _run_narrative_match(self, alert: NewsAlert) -> None:
        elapsed = 0
        for target in sorted(set(self.settings.news_pair_recheck_seconds)):
            delay = max(0, target - elapsed)
            if delay:
                await asyncio.sleep(delay)
            elapsed = target
            for narrative in alert.narrative_terms[:3]:
                match = await self.news_matcher.search(narrative)
                if match is None or match.mint in self._narrative_matches_seen:
                    continue
                # A ticker/name match is not a verified coin.  The mint came from
                # a *text search*, so its provenance is UNVERIFIED by
                # construction, and the analysis below must run against that
                # exact address and no other.  If anything downstream resolves to
                # a different mint, that is a substitution and it hard-fails
                # rather than reaching the channel.
                provenance = from_symbol_search(
                    match.mint,
                    source="dex_narrative_search",
                    query=narrative,
                )
                buyers = await self.database.recent_verified_token_buyers(
                    match.mint,
                    int(time.time()) - self.settings.coin_callout_window_seconds,
                )
                callout = await self.analyze_coin(match.mint, buyers=buyers)
                assert_exact_propagation(
                    match.mint, callout.mint, stage="narrative match → callout"
                )
                if not provenance.identity_verified:
                    logger.info(
                        "Narrative %r produced %s from a text search; publishing as "
                        "research only, never as a verified identity",
                        narrative,
                        match.mint[:8],
                    )
                if should_publish_coin_callout(
                    callout,
                    configured_score_floor=self.settings.coin_callout_min_alert_score,
                ):
                    self._narrative_matches_seen.add(match.mint)
                    await self.notifier.on_coin_callout(callout)
                    return

    async def _run_coin_callout(self, mint: str, *, force_x_search: bool = False) -> None:
        try:
            buyers = await self.database.recent_verified_token_buyers(
                mint,
                int(time.time()) - self.settings.coin_callout_window_seconds,
            )
            buyer_count = len(buyers)
            now = int(time.time())
            previous = self._last_callout_state.get(mint)
            if (
                previous
                and buyer_count <= previous[1]
                and (now - previous[0] < self.settings.coin_callout_cooldown_seconds)
            ):
                return
            callout = await self.analyze_coin(
                mint,
                buyers=buyers,
                force_x_search=force_x_search,
            )
            self._last_callout_state[mint] = (now, buyer_count)
            # Automatic alerts are for evidence-rich leads. Very weak blocked rows remain
            # available through /smartmoney coin, but no longer flood the channel merely
            # because a missing-data or safety blocker exists.
            if should_publish_coin_callout(
                callout,
                configured_score_floor=self.settings.coin_callout_min_alert_score,
            ):
                await self.notifier.on_coin_callout(callout)
            elif self.settings.coin_watch_alerts_enabled and should_publish_coin_watch(
                callout,
                configured_score_floor=self.settings.coin_watch_min_score,
            ):
                self._coin_scan_counts["watch"] += 1
                await self.notifier.on_coin_watch(callout)
            elif should_publish_fomo_watch(
                callout,
                configured_score_floor=self.settings.fomo_watch_min_score,
            ):
                self._coin_scan_counts["fomo_watch"] += 1
                await self.notifier.on_fomo_watch(callout)
        except Exception as exc:
            await self.notifier.on_error("Analyzing coin callout", exc)

    async def analyze_coin(
        self,
        mint: str,
        *,
        buyers: list[tuple[str, str]] | None = None,
        force_x_search: bool = False,
        allow_x_search: bool = True,
        refresh_market: bool = False,
        verify_sell_route: bool = False,
    ) -> CoinCallout:
        if buyers is None:
            buyers = await self.database.recent_verified_token_buyers(
                mint,
                int(time.time()) - self.settings.coin_callout_window_seconds,
            )
        try:
            token_info = await self.market.token_info(mint)
        except JupiterError:
            token_info = None
        aliases = tuple(alias for _address, alias in buyers)
        callout = await self.callout_analyzer.analyze(
            mint=mint,
            token_info=token_info,
            smart_wallets=aliases,
            force_x_search=force_x_search,
            allow_x_search=allow_x_search,
            refresh_market=refresh_market,
            verify_sell_route=verify_sell_route,
        )
        self._record_coin_scan(callout)
        return callout

    async def _mirror_paper_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        if self.settings.paper_force_observation_mode:
            source_price = swap.token_price_usd
            size = self.settings.default_copy_usd
            if source_price is None or source_price <= 0:
                result = ExecutionResult(
                    success=False,
                    mode=ExecutionMode.PAPER,
                    token_mint=swap.token_mint,
                    side=swap.side,
                    size_usd=size,
                    message=(
                        "Skipped: the source transaction did not contain a valid token "
                        "price, so even the forced observation ledger cannot value it"
                    ),
                )
                await self.database.log_execution(
                    signal_id=None,
                    mode=result.mode,
                    token_mint=result.token_mint,
                    side=result.side,
                    size_usd=result.size_usd,
                    success=result.success,
                    signature=None,
                    message=result.message,
                )
            else:
                penalty = Decimal(self.settings.paper_observation_penalty_bps) / Decimal(10_000)
                observation_price = (
                    source_price * (Decimal("1") + penalty)
                    if swap.side is Side.BUY
                    else source_price * (Decimal("1") - penalty)
                )
                result = await self.executor.execute_paper_mirror(
                    swap=swap,
                    trader=trader,
                    market_price_usd=observation_price,
                    size_usd=size,
                    observation_mode=True,
                )
            await self.notifier.on_execution(result)
            return

        sniper_mode = (
            swap.side is Side.SELL
            and self.settings.paper_sniper_test_enabled
            and await self.database.paper_mirror_open_lot_is_sniper(trader.address, swap.token_mint)
        )

        # A tracked wallet's transaction price is historical by the time this process
        # observes it. Price the shadow fill at detection time so PAPER results include
        # the latency that live copy execution would face.
        try:
            price = await self.market.price(swap.token_mint)
        except JupiterError:
            price = None
        pump_source_fallback = False
        sniper_source_price = False
        if price is None or price <= 0:
            source_price = swap.token_price_usd
            if (
                self.settings.paper_allow_pump_source_fallback
                and is_pump_mint(swap.token_mint)
                and source_price is not None
                and source_price > 0
            ):
                penalty = Decimal(self.settings.paper_pump_source_fallback_bps) / Decimal(10_000)
                price = (
                    source_price * (Decimal("1") + penalty)
                    if swap.side is Side.BUY
                    else source_price * (Decimal("1") - penalty)
                )
                pump_source_fallback = True
            elif (
                self.settings.paper_sniper_test_enabled
                and is_pump_mint(swap.token_mint)
                and source_price is not None
                and source_price > 0
            ):
                penalty = Decimal(self.settings.paper_sniper_source_penalty_bps) / Decimal(10_000)
                price = (
                    source_price * (Decimal("1") + penalty)
                    if swap.side is Side.BUY
                    else source_price * (Decimal("1") - penalty)
                )
                pump_source_fallback = True
                sniper_source_price = True
            elif not self.settings.paper_require_current_price:
                price = source_price
        size = min(self.settings.default_copy_usd, self.settings.max_copy_usd)
        if price is None or price <= 0:
            result = ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=size,
                message=(
                    "Skipped: no current Jupiter price was available for a realistic paper fill"
                ),
            )
            await self.database.log_execution(
                signal_id=None,
                mode=result.mode,
                token_mint=result.token_mint,
                side=result.side,
                size_usd=result.size_usd,
                success=result.success,
                signature=None,
                message=result.message,
            )
        else:
            token_info: TokenInfo | None = None
            if swap.side is Side.BUY:
                try:
                    token_info = await self.market.token_info(swap.token_mint)
                except JupiterError:
                    token_info = None
            if swap.side is Side.BUY and self.settings.paper_raw_entry_filter_enabled:
                signal = Signal(
                    token_mint=swap.token_mint,
                    side=Side.BUY,
                    created_at=swap.block_time or int(time.time()),
                    trader_addresses=(trader.address,),
                    trader_aliases=(trader.alias,),
                    source_signatures=(swap.signature,),
                    combined_score=Decimal("100"),
                    reference_price_usd=price,
                )
                already_open = await self.database.has_paper_mirror_position(
                    trader.address, swap.token_mint
                )
                decision = await self.risk.assess(
                    signal=signal,
                    mode=ExecutionMode.PAPER,
                    token_info=token_info,
                    market_price_usd=price,
                    require_consensus=False,
                    enforce_position_limit=not already_open,
                )
                size = decision.size_usd
                if not decision.allowed:
                    sniper_allowed, sniper_reason = self._paper_sniper_entry_allowed(
                        swap=swap,
                        token_info=token_info,
                        decision=decision,
                    )
                    if sniper_allowed:
                        sniper_mode = True
                        size = min(
                            self.settings.paper_sniper_copy_usd,
                            self.settings.max_copy_usd,
                        )
                    else:
                        reasons = "; ".join(decision.reasons) or "risk policy blocked entry"
                        if self.settings.paper_sniper_test_enabled and sniper_reason:
                            reasons = f"{reasons}; sniper lane rejected — {sniper_reason}"
                        result = ExecutionResult(
                            success=False,
                            mode=ExecutionMode.PAPER,
                            token_mint=swap.token_mint,
                            side=swap.side,
                            size_usd=size,
                            message=f"Skipped: paper entry guard — {reasons}",
                        )
                        await self.database.log_execution(
                            signal_id=None,
                            mode=result.mode,
                            token_mint=result.token_mint,
                            side=result.side,
                            size_usd=result.size_usd,
                            success=result.success,
                            signature=None,
                            message=result.message,
                        )
                        await self.notifier.on_execution(result)
                        return
                elif sniper_source_price:
                    sniper_allowed, sniper_reason = self._paper_sniper_entry_allowed(
                        swap=swap,
                        token_info=token_info,
                        decision=decision,
                    )
                    if not sniper_allowed:
                        result = ExecutionResult(
                            success=False,
                            mode=ExecutionMode.PAPER,
                            token_mint=swap.token_mint,
                            side=swap.side,
                            size_usd=size,
                            message=(
                                "Skipped: no executable current route and sniper lane "
                                f"rejected — {sniper_reason}"
                            ),
                        )
                        await self.database.log_execution(
                            signal_id=None,
                            mode=result.mode,
                            token_mint=result.token_mint,
                            side=result.side,
                            size_usd=result.size_usd,
                            success=result.success,
                            signature=None,
                            message=result.message,
                        )
                        await self.notifier.on_execution(result)
                        return
                    sniper_mode = True
                    size = min(
                        self.settings.paper_sniper_copy_usd,
                        self.settings.max_copy_usd,
                    )
            result = await self.executor.execute_paper_mirror(
                swap=swap,
                trader=trader,
                market_price_usd=price,
                size_usd=size,
                token_info=token_info,
                pump_source_fallback=pump_source_fallback,
                sniper_mode=sniper_mode,
            )
        await self.notifier.on_execution(result)

    def _paper_sniper_entry_allowed(
        self,
        *,
        swap: DetectedSwap,
        token_info: TokenInfo | None,
        decision: RiskDecision,
    ) -> tuple[bool, str]:
        """Allow a smaller, separately labeled PAPER launch observation."""

        if not self.settings.paper_sniper_test_enabled:
            return False, "disabled"
        if not is_pump_mint(swap.token_mint):
            return False, "token is not a Pump launch mint"
        if token_info is None:
            return False, "token safety metadata is unavailable"

        soft_prefixes = (
            "Liquidity $",
            "Only ",
            "Organic score is only ",
            "Top-holder concentration is ",
        )
        hard_reasons = [
            reason for reason in decision.reasons if not reason.startswith(soft_prefixes)
        ]
        if hard_reasons:
            return False, "; ".join(hard_reasons)
        if token_info.suspicious:
            return False, "Jupiter flags the token as suspicious"
        if token_info.freeze_authority_disabled is False:
            return False, "freeze authority is enabled"
        if token_info.mint_authority_disabled is False:
            return False, "mint authority is enabled"
        if token_info.liquidity_usd is None:
            return False, "liquidity is unknown"
        if token_info.liquidity_usd < self.settings.paper_sniper_min_liquidity_usd:
            return (
                False,
                f"liquidity ${token_info.liquidity_usd:,.0f} is below the sniper floor",
            )
        if token_info.holder_count is None:
            return False, "holder count is unknown"
        if token_info.holder_count < self.settings.paper_sniper_min_holders:
            return (
                False,
                f"only {token_info.holder_count:,} holders; sniper floor is "
                f"{self.settings.paper_sniper_min_holders:,}",
            )
        if (
            token_info.top_holders_percent is not None
            and token_info.top_holders_percent > self.settings.paper_sniper_max_top_holders_percent
        ):
            return (
                False,
                f"top-holder concentration {token_info.top_holders_percent}% exceeds "
                f"the sniper ceiling",
            )
        return True, "launch-stage PAPER lane"

    async def _process_signal(self, signal: Signal, *, known_price: Decimal | None = None) -> None:
        signal_id = await self.database.record_signal(signal)
        token_info: TokenInfo | None
        try:
            token_info = await self.market.token_info(signal.token_mint)
        except JupiterError:
            token_info = None
        price = known_price
        if price is None:
            try:
                price = await self.market.price(signal.token_mint)
            except JupiterError:
                price = None
        price = price or signal.reference_price_usd
        mode = await self.execution_mode()

        if (
            mode is ExecutionMode.PAPER
            and signal.side is Side.BUY
            and await self._daily_profit_entries_locked()
        ):
            decision = RiskDecision(
                allowed=False,
                size_usd=Decimal("0"),
                reasons=("Daily paper-profit target is locked until the next day",),
            )
            await self.notifier.on_signal(signal, token_info, decision)
            return

        if mode is ExecutionMode.ALERTS:
            decision = RiskDecision(
                allowed=True,
                size_usd=Decimal("0"),
                reasons=("Alert-only mode",),
            )
        else:
            decision = await self.risk.assess(
                signal=signal,
                mode=mode,
                token_info=token_info,
                market_price_usd=price,
            )
        await self.notifier.on_signal(signal, token_info, decision)
        if not decision.allowed or price is None:
            return

        result = await self.executor.execute(
            signal_id=signal_id,
            signal=signal,
            mode=mode,
            token_info=token_info,
            market_price_usd=price,
            size_usd=decision.size_usd,
        )
        await self.notifier.on_execution(result)

    async def _check_position_exits(self) -> None:
        mode = await self.execution_mode()
        if mode is ExecutionMode.ALERTS:
            return
        if mode is ExecutionMode.PAPER:
            strategy_positions = await self.database.paper_positions()
            strategy_positions = [
                item for item in strategy_positions if item["token_mint"] != PAPER_DEMO_MINT
            ]
            mirror_positions = await self.database.paper_mirror_positions()
            positions = strategy_positions + mirror_positions
        else:
            positions = await self.database.live_positions()
        if not positions:
            return

        now = int(time.time())
        prices = await self.market.prices(
            list(dict.fromkeys(str(item["token_mint"]) for item in positions))
        )
        if mode is ExecutionMode.PAPER:
            await self._check_strategy_paper_exits(strategy_positions, prices, now)
            # Observation mode changes how an entry is priced, not whether losses
            # may run without bounds. Raw stop, profit, trailing, and time exits
            # must protect every paper lot in every fill mode.
            await self._check_raw_mirror_exits(mirror_positions, prices, now)
            await self.database.paper_summary(prices)
            return

        for position in positions:
            mint = str(position["token_mint"])
            price = prices.get(mint)
            if price is None or price <= 0:
                continue
            quantity = Decimal(str(position["quantity_raw"])) / (
                Decimal(10) ** int(position["decimals"])
            )
            if quantity <= 0:
                continue
            average_entry = Decimal(str(position["cost_basis_usd"])) / quantity
            if average_entry <= 0:
                continue

            change_percent = ((price / average_entry) - Decimal("1")) * Decimal("100")
            age_seconds = now - int(position["opened_at"])
            reason: str | None = None
            if change_percent <= -self.settings.stop_loss_percent:
                reason = f"stop loss ({change_percent:.2f}%)"
            elif change_percent >= self.settings.take_profit_percent:
                reason = f"take profit (+{change_percent:.2f}%)"
            elif age_seconds >= self.settings.max_hold_seconds:
                reason = f"maximum hold time ({age_seconds // 3600}h)"
            if reason is None:
                continue
            if await self.database.recent_signal_exists(mint, Side.SELL, now - 60):
                continue

            signal = Signal(
                token_mint=mint,
                side=Side.SELL,
                created_at=now,
                trader_addresses=("RISK_ENGINE",),
                trader_aliases=(f"Risk engine: {reason}",),
                source_signatures=(f"risk-{mint}-{now}",),
                combined_score=Decimal("100"),
                reference_price_usd=price,
            )
            await self._process_signal(signal, known_price=price)

    async def _check_strategy_paper_exits(
        self,
        positions: list[dict[str, object]],
        prices: dict[str, Decimal],
        now: int,
    ) -> None:
        for position in positions:
            mint = str(position["token_mint"])
            price = prices.get(mint)
            average_entry = Decimal(str(position["average_entry_usd"]))
            if price is None or price <= 0 or average_entry <= 0:
                continue
            change_percent = ((price / average_entry) - Decimal("1")) * Decimal("100")
            age_seconds = now - int(position["opened_at"])
            reason: str | None = None
            if change_percent <= -self.settings.stop_loss_percent:
                reason = f"stop loss ({change_percent:.2f}%)"
            elif change_percent >= self.settings.take_profit_percent:
                reason = f"take profit (+{change_percent:.2f}%)"
            elif age_seconds >= self.settings.max_hold_seconds:
                reason = f"maximum hold time ({age_seconds // 3600}h)"
            if reason is None:
                continue
            if await self.database.recent_signal_exists(mint, Side.SELL, now - 60):
                continue

            signal = Signal(
                token_mint=mint,
                side=Side.SELL,
                created_at=now,
                trader_addresses=("RISK_ENGINE",),
                trader_aliases=(f"Risk engine: {reason}",),
                source_signatures=(f"risk-{mint}-{now}",),
                combined_score=Decimal("100"),
                reference_price_usd=price,
            )
            await self._process_signal(signal, known_price=price)

    async def _check_raw_mirror_exits(
        self,
        positions: list[dict[str, object]],
        prices: dict[str, Decimal],
        now: int,
    ) -> None:
        for position in positions:
            mint = str(position["token_mint"])
            trader_address = str(position["trader_address"])
            price = prices.get(mint)
            average_entry = Decimal(str(position["average_entry_usd"]))
            if price is None or price <= 0 or average_entry <= 0:
                continue

            peak = await self.database.update_paper_mirror_peak(trader_address, mint, price)
            change_percent = ((price / average_entry) - Decimal("1")) * Decimal("100")
            peak_gain_percent = ((peak / average_entry) - Decimal("1")) * Decimal("100")
            pullback_percent = ((price / peak) - Decimal("1")) * Decimal("100")
            age_seconds = now - int(position["opened_at"])

            reason: str | None = None
            if change_percent <= -self.settings.raw_mirror_stop_loss_percent:
                reason = f"hard stop ({change_percent:.2f}%)"
            elif change_percent >= self.settings.raw_mirror_take_profit_percent:
                reason = f"take profit (+{change_percent:.2f}%)"
            elif (
                peak_gain_percent >= self.settings.raw_mirror_trailing_activation_percent
                and pullback_percent <= -self.settings.raw_mirror_trailing_stop_percent
            ):
                reason = (
                    f"trailing-profit lock (peak +{peak_gain_percent:.2f}%, "
                    f"pullback {pullback_percent:.2f}%)"
                )
            elif age_seconds >= self.settings.raw_mirror_max_hold_seconds:
                reason = f"maximum raw hold time ({age_seconds // 60}m)"
            if reason is None:
                continue

            result = await self.executor.execute_paper_mirror_risk_exit(
                position=position,
                market_price_usd=price,
                reason=reason,
            )
            await self.notifier.on_execution(result)

    async def rankings(self) -> list[ScoredTrader]:
        metrics_24h, metrics_7d = await asyncio.gather(
            self.database.metrics(86_400), self.database.metrics(604_800)
        )
        local_rankings = rank_traders(metrics_24h, metrics_7d)
        discovered = await self.database.list_discovered(limit=50)
        merged = {item.metrics_24h.address: item for item in local_rankings}
        for candidate in discovered:
            local = merged.get(candidate.address)
            wins = int(
                Decimal(candidate.closed_tokens) * candidate.win_rate_percent / Decimal("100")
            )
            losses = max(0, candidate.closed_tokens - wins)
            external_metrics = TraderMetrics(
                address=candidate.address,
                alias=candidate.alias,
                window_seconds=86_400,
                trades=candidate.trades_24h,
                buys=candidate.buys_24h,
                sells=candidate.sells_24h,
                wins=wins,
                losses=losses,
                realized_pnl_usd=candidate.realized_pnl_24h,
                matched_cost_usd=candidate.invested_24h_usd,
                volume_usd=candidate.volume_24h_usd,
                max_drawdown_usd=Decimal("0"),
            )
            weekly_wins = int(
                Decimal(max(candidate.trades_7d, 0))
                * candidate.win_rate_7d_percent
                / Decimal("100")
            )
            weekly_losses = max(0, candidate.trades_7d - weekly_wins)
            weekly_cost = (
                candidate.realized_pnl_7d / (candidate.roi_7d_percent / Decimal("100"))
                if candidate.roi_7d_percent > 0
                else Decimal("0")
            )
            external_week = TraderMetrics(
                address=candidate.address,
                alias=candidate.alias,
                window_seconds=604_800,
                trades=candidate.trades_7d,
                buys=0,
                sells=0,
                wins=weekly_wins,
                losses=weekly_losses,
                realized_pnl_usd=candidate.realized_pnl_7d,
                matched_cost_usd=weekly_cost,
                volume_usd=Decimal("0"),
                max_drawdown_usd=Decimal("0"),
            )
            if local is None:
                merged[candidate.address] = ScoredTrader(
                    metrics_24h=external_metrics,
                    metrics_7d=external_week,
                    score=candidate.score,
                )
                continue

            closed_local = local.metrics_24h.wins + local.metrics_24h.losses
            if local.metrics_24h.trades >= 10 and closed_local >= 3:
                blended_score = (
                    local.score * Decimal("0.60") + candidate.score * Decimal("0.40")
                ).quantize(Decimal("0.01"))
            else:
                blended_score = candidate.score
            merged[candidate.address] = ScoredTrader(
                metrics_24h=external_metrics,
                metrics_7d=external_week,
                score=blended_score,
            )

        return sorted(
            merged.values(),
            key=lambda item: (
                item.score,
                item.metrics_24h.realized_pnl_usd,
                item.metrics_24h.trades,
            ),
            reverse=True,
        )

    async def execution_mode(self) -> ExecutionMode:
        raw = await self.database.get_setting("mode", ExecutionMode.PAPER.value)
        try:
            return ExecutionMode(raw or ExecutionMode.PAPER.value)
        except ValueError:
            return ExecutionMode.PAPER

    async def set_execution_mode(self, mode: ExecutionMode) -> None:
        if mode is ExecutionMode.LIVE and not self.settings.live_is_unlocked:
            raise ValueError("Live mode is not unlocked by environment configuration")
        await self.database.set_setting("mode", mode.value)

    async def set_paused(self, paused: bool) -> None:
        await self.database.set_setting("paused", "true" if paused else "false")

    async def is_paused(self) -> bool:
        return (await self.database.get_setting("paused", "false")) == "true"

    def _paper_day_window(self, now: int) -> tuple[str, int, int]:
        zone = ZoneInfo(self.settings.paper_daily_lock_timezone)
        local_now = datetime.fromtimestamp(now, zone)
        start = datetime.combine(local_now.date(), datetime_time.min, tzinfo=zone)
        end = start + timedelta(days=1)
        return local_now.date().isoformat(), int(start.timestamp()), int(end.timestamp())

    async def _daily_profit_entries_locked(self, *, now: int | None = None) -> bool:
        if not (
            self.settings.paper_daily_profit_lock_enabled
            or self.settings.paper_daily_loss_lock_enabled
        ):
            return False
        timestamp = int(time.time()) if now is None else now
        day, _, _ = self._paper_day_window(timestamp)
        stored_day = await self.database.get_setting("paper_daily_lock_day")
        if stored_day != day:
            return False
        return (await self.database.get_setting("paper_daily_lock_triggered", "false")) == "true"

    async def _paper_daily_lock_status_from_summary(
        self,
        summary: PaperSummary,
        *,
        now: int,
    ) -> PaperDailyLockStatus:
        day, start_timestamp, end_timestamp = self._paper_day_window(now)
        stored_day = await self.database.get_setting("paper_daily_lock_day")
        raw_baseline = await self.database.get_setting("paper_daily_lock_baseline_equity_usd")
        if stored_day != day or raw_baseline is None:
            baseline = await self.database.first_paper_equity_between(
                start_timestamp, end_timestamp
            )
            baseline = baseline if baseline is not None else summary.equity_usd
            await self.database.set_setting("paper_daily_lock_day", day)
            await self.database.set_setting("paper_daily_lock_baseline_equity_usd", str(baseline))
            await self.database.set_setting("paper_daily_lock_triggered", "false")
            await self.database.set_setting("paper_daily_lock_triggered_at", "")
            await self.database.set_setting("paper_daily_lock_reason", "")
            locked = False
            triggered_at = None
            lock_reason = None
        else:
            baseline = Decimal(raw_baseline)
            locked = (
                await self.database.get_setting("paper_daily_lock_triggered", "false")
            ) == "true"
            raw_triggered_at = await self.database.get_setting("paper_daily_lock_triggered_at", "")
            triggered_at = int(raw_triggered_at) if raw_triggered_at else None
            lock_reason = await self.database.get_setting("paper_daily_lock_reason", "") or None

        positions = [
            item
            for item in await self.database.paper_all_positions()
            if str(item["token_mint"]) != PAPER_DEMO_MINT
        ]
        return PaperDailyLockStatus(
            enabled=(
                self.settings.paper_daily_profit_lock_enabled
                or self.settings.paper_daily_loss_lock_enabled
            ),
            day=day,
            target_usd=self.settings.paper_daily_target_usd,
            loss_limit_usd=self.settings.paper_daily_loss_limit_usd,
            baseline_equity_usd=baseline,
            current_equity_usd=summary.equity_usd,
            marked_pnl_usd=summary.equity_usd - baseline,
            locked=locked,
            triggered_at=triggered_at,
            open_positions=len(positions),
            lock_reason=lock_reason,
        )

    async def paper_daily_lock_status(self) -> PaperDailyLockStatus:
        summary = await self.paper_summary()
        async with self._daily_profit_lock:
            return await self._paper_daily_lock_status_from_summary(summary, now=int(time.time()))

    async def _enforce_daily_profit_lock(self) -> bool:
        if not (
            self.settings.paper_daily_profit_lock_enabled
            or self.settings.paper_daily_loss_lock_enabled
        ):
            return False
        if await self.execution_mode() is not ExecutionMode.PAPER:
            return False

        async with self._daily_profit_lock:
            now = int(time.time())
            summary = await self.paper_summary()
            status = await self._paper_daily_lock_status_from_summary(summary, now=now)
            lock_reason: str | None = None
            if (
                not status.locked
                and self.settings.paper_daily_profit_lock_enabled
                and status.marked_pnl_usd >= status.target_usd
            ):
                lock_reason = "PROFIT_TARGET"
            elif (
                not status.locked
                and self.settings.paper_daily_loss_lock_enabled
                and status.marked_pnl_usd <= -status.loss_limit_usd
            ):
                lock_reason = "LOSS_LIMIT"

            if lock_reason is not None:
                await self.database.set_setting("paper_daily_lock_triggered", "true")
                await self.database.set_setting("paper_daily_lock_triggered_at", str(now))
                await self.database.set_setting("paper_daily_lock_reason", lock_reason)
                status = replace(
                    status,
                    locked=True,
                    triggered_at=now,
                    lock_reason=lock_reason,
                )
                await self.notifier.on_daily_profit_lock(status)

            if status.locked:
                await self._liquidate_daily_profit_positions(status)
            return status.locked

    async def _liquidate_daily_profit_positions(self, status: PaperDailyLockStatus) -> None:
        positions = [
            item
            for item in await self.database.paper_all_positions()
            if str(item["token_mint"]) != PAPER_DEMO_MINT
        ]
        if not positions:
            return
        mints = sorted({str(item["token_mint"]) for item in positions})
        try:
            prices = await self.market.prices(mints)
        except JupiterError:
            prices = {}

        unavailable = 0
        loss_lock = status.lock_reason == "LOSS_LIMIT"
        if loss_lock:
            reason = (
                f"daily marked PAPER loss reached -${status.loss_limit_usd:.2f}; "
                f"entry lock for {status.day}"
            )
            requested_by = "daily loss lock"
            execution_kind = "DAILY_LOSS_LOCK_EXIT"
            message_label = "Daily loss-lock PAPER SELL"
        else:
            reason = (
                f"daily marked PAPER profit reached ${status.target_usd:.2f}; "
                f"entry lock for {status.day}"
            )
            requested_by = "daily profit lock"
            execution_kind = "DAILY_PROFIT_LOCK_EXIT"
            message_label = "Daily profit-lock PAPER SELL"
        for position in positions:
            mint = str(position["token_mint"])
            price = prices.get(mint)
            if price is None or price <= 0:
                unavailable += 1
                continue

            if str(position.get("position_kind")) == "RAW_MIRROR":
                result = await self.executor.execute_paper_mirror_manual_exit(
                    position={
                        **position,
                        "trader_address": str(position["source_trader"]),
                    },
                    market_price_usd=price,
                    requested_by=requested_by,
                    execution_kind=execution_kind,
                    exit_reason=reason,
                    message_label=message_label,
                )
                if result.success:
                    await self.notifier.on_execution(result)
                else:
                    unavailable += 1
                continue

            now = int(time.time())
            signal = Signal(
                token_mint=mint,
                side=Side.SELL,
                created_at=now,
                trader_addresses=(execution_kind,),
                trader_aliases=(reason,),
                source_signatures=(f"daily-lock-{mint}-{time.time_ns()}",),
                combined_score=Decimal("100"),
                reference_price_usd=price,
            )
            signal_id = await self.database.record_signal(signal)
            cost_basis = Decimal(str(position["cost_basis_usd"]))
            fill = await self.database.paper_execute(
                signal_id=signal_id,
                token_mint=mint,
                side=Side.SELL,
                market_price_usd=price,
                size_usd=cost_basis,
                fee_bps=self.settings.simulated_fee_bps,
                slippage_bps=self.settings.simulated_slippage_bps,
                execution_kind=execution_kind,
                exit_reason=reason,
            )
            if fill is None:
                result = ExecutionResult(
                    success=False,
                    mode=ExecutionMode.PAPER,
                    token_mint=mint,
                    side=Side.SELL,
                    size_usd=cost_basis,
                    message="Skipped: the daily-lock paper position was already closed",
                )
            else:
                result = ExecutionResult(
                    success=True,
                    mode=ExecutionMode.PAPER,
                    token_mint=mint,
                    side=Side.SELL,
                    size_usd=cost_basis,
                    message=(
                        f"{message_label} filled at ${fill['price']:.8f}; "
                        f"fee ${fill['fee']:.4f}; realized P&L "
                        f"${fill['realized_pnl']:.2f}. New entries remain locked "
                        f"for {status.day}."
                    ),
                )
            await self.database.log_execution(
                signal_id=signal_id,
                mode=result.mode,
                token_mint=result.token_mint,
                side=result.side,
                size_usd=result.size_usd,
                success=result.success,
                signature=None,
                message=result.message,
            )
            await self.notifier.on_execution(result)

        if unavailable:
            self.last_error = (
                f"Daily paper lock: {unavailable} open PAPER position(s) are still "
                "waiting for a current exit price; liquidation will retry"
            )
        elif self.last_error and self.last_error.startswith("Daily paper lock:"):
            self.last_error = None

    async def paper_summary(self) -> PaperSummary:
        positions = await self.database.paper_all_positions()
        mints = sorted(
            {item["token_mint"] for item in positions if item["token_mint"] != PAPER_DEMO_MINT}
        )
        try:
            prices = await self.market.prices(mints) if mints else {}
        except JupiterError:
            prices = {}
        if any(item["token_mint"] == PAPER_DEMO_MINT for item in positions):
            prices[PAPER_DEMO_MINT] = Decimal(PAPER_DEMO_ENTRY_PRICE_USD)
        return await self.database.paper_summary(prices)

    async def paper_readiness(self) -> PaperReadiness:
        return await self.database.paper_readiness(
            min_active_days=self.settings.readiness_min_active_days,
            min_closed_trades=self.settings.readiness_min_closed_trades,
            min_profit_factor=self.settings.readiness_min_profit_factor,
            max_drawdown_percent=self.settings.readiness_max_drawdown_percent,
            min_quote_success_percent=self.settings.readiness_min_quote_success_percent,
        )

    async def manual_paper_exit(
        self,
        *,
        position_kind: str,
        token_mint: str,
        source_trader: str | None,
        requested_by: str,
    ) -> ExecutionResult:
        """Close one selected fake position; never touch a live wallet."""

        if await self.execution_mode() is not ExecutionMode.PAPER:
            return ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=Decimal("0"),
                message="Skipped: manual paper sells only work while mode is PAPER",
            )
        if token_mint == PAPER_DEMO_MINT:
            return ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=Decimal("0"),
                message="Skipped: close the demo with /smartmoney paper-demo",
            )

        try:
            market_price = await self.market.price(token_mint)
        except JupiterError:
            market_price = None
        if market_price is None or market_price <= 0:
            return ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=Decimal("0"),
                message="Skipped: no current market price is available for this paper exit",
            )

        if position_kind == "RAW_MIRROR" and source_trader:
            position = next(
                (
                    item
                    for item in await self.database.paper_mirror_positions()
                    if str(item["trader_address"]) == source_trader
                    and str(item["token_mint"]) == token_mint
                ),
                None,
            )
            if position is None:
                return ExecutionResult(
                    success=False,
                    mode=ExecutionMode.PAPER,
                    token_mint=token_mint,
                    side=Side.SELL,
                    size_usd=Decimal("0"),
                    message="Skipped: that paper position is already closed",
                )
            return await self.executor.execute_paper_mirror_manual_exit(
                position=position,
                market_price_usd=market_price,
                requested_by=requested_by,
            )

        position = next(
            (
                item
                for item in await self.database.paper_positions()
                if str(item["token_mint"]) == token_mint
            ),
            None,
        )
        if position is None:
            return ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=Decimal("0"),
                message="Skipped: that paper position is already closed",
            )

        now = int(time.time())
        signal = Signal(
            token_mint=token_mint,
            side=Side.SELL,
            created_at=now,
            trader_addresses=("MANUAL_PAPER",),
            trader_aliases=(requested_by,),
            source_signatures=(f"paper-manual-strategy-{time.time_ns()}",),
            combined_score=Decimal("100"),
            reference_price_usd=market_price,
        )
        signal_id = await self.database.record_signal(signal)
        cost_basis = Decimal(str(position["cost_basis_usd"]))
        fill = await self.database.paper_execute(
            signal_id=signal_id,
            token_mint=token_mint,
            side=Side.SELL,
            market_price_usd=market_price,
            size_usd=cost_basis,
            fee_bps=self.settings.simulated_fee_bps,
            slippage_bps=self.settings.simulated_slippage_bps,
            execution_kind="MANUAL_EXIT",
            exit_reason=f"manual PAPER sell requested by {requested_by}",
        )
        if fill is None:
            result = ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=cost_basis,
                message="Skipped: that paper position is already closed",
            )
        else:
            result = ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=cost_basis,
                message=(
                    f"Manual PAPER SELL filled at ${fill['price']:.8f}; fee "
                    f"${fill['fee']:.4f}; realized P&L ${fill['realized_pnl']:.2f}."
                ),
            )
        await self.database.log_execution(
            signal_id=signal_id,
            mode=result.mode,
            token_mint=result.token_mint,
            side=result.side,
            size_usd=result.size_usd,
            success=result.success,
            signature=None,
            message=result.message,
        )
        return result

    async def status(self) -> dict[str, object]:
        self.x_budget.database = self.database
        try:
            rpc_health = await asyncio.wait_for(self.rpc.health(), timeout=8)
        except TimeoutError:
            rpc_health = "timeout after 8s"
        except RpcError as exc:
            rpc_health = f"error: {exc}"
        daily_lock = await self.paper_daily_lock_status()
        x_budget_status = await self.x_budget.status()
        return {
            "rpc": rpc_health,
            "mode": (await self.execution_mode()).value,
            "paused": await self.is_paused(),
            "wallets": len(await self.database.list_traders(enabled_only=True)),
            "exit_only_wallets": await self.database.exit_only_trader_count(),
            "last_scan": self.last_scan_finished_at,
            "last_error": self.last_error,
            "live_unlocked": self.settings.live_is_unlocked,
            "discovery_enabled": self.settings.auto_discovery_enabled,
            "discovery_configured": self.settings.discovery_is_configured,
            "discovery_last_refresh": self.last_discovery_refresh_at,
            "discovery_7d_last_refresh": self.last_weekly_refresh_at,
            "rotation_last_refresh": self.last_rotation_at,
            "candidate_pool_size": len(self._candidate_pool),
            "kol_discovery_enabled": self.settings.discovery_include_kols,
            "pump_profile_discovery_enabled": (self.settings.pump_profile_discovery_enabled),
            "pump_profile_nominations": len(self._social_nominations),
            "pump_profile_verified_matches": self.profile_verified_matches,
            "pump_profile_last_refresh": self.last_profile_refresh_at,
            "pump_profile_last_error": self.profile_discovery_last_error,
            "rotation_verified_pump_wallets": (
                self.last_rotation_result.verified_pump_wallets
                if self.last_rotation_result
                else None
            ),
            "discovered_wallets": len(await self.database.list_discovered(limit=50)),
            "stream_enabled": self.stream.enabled,
            "stream_connected": self.stream.connected,
            "stream_subscriptions": self.stream.subscription_count,
            "stream_last_event": self.stream.last_event_at,
            "stream_last_error": self.stream.last_error,
            "stream_reconnects": self.stream.reconnects,
            "stream_commitment": self.stream.commitment,
            "paper_mirror_raw_swaps": self.settings.paper_mirror_raw_swaps,
            "paper_use_executable_quotes": self.settings.paper_use_executable_quotes,
            "paper_force_observation_mode": (self.settings.paper_force_observation_mode),
            "paper_daily_profit_lock": daily_lock,
            "quote_ready": bool(self.settings.jupiter_api_key),
            "consecutive_quote_failures": self.executor.consecutive_quote_failures,
            "coin_callouts_enabled": self.settings.coin_callouts_enabled,
            "coin_watch_alerts_enabled": self.settings.coin_watch_alerts_enabled,
            "coin_scan_counts": dict(self._coin_scan_counts),
            "x_social_configured": self.x_social.configured,
            "x_paid_search_enabled": self.settings.x_paid_search_enabled,
            "x_social_last_success": self.x_social.last_success_at,
            "x_social_last_error": self.x_social.last_error,
            "x_search_usage_today": x_budget_status["verifications"],
            "x_search_daily_limit": self.settings.x_daily_search_limit,
            "x_budget": x_budget_status,
            "x_radar_enabled": self.settings.x_radar_enabled,
            "x_radar_poll_seconds": self.settings.x_radar_poll_seconds,
            "x_radar_scans": self.x_social.radar_scans,
            "x_radar_last_scan": self.x_social.last_radar_at,
            "x_radar_last_posts": self.x_social.last_radar_posts,
            "x_radar_last_new_posts": self.x_social.last_radar_new_posts,
            "x_radar_last_contracts": self.x_social.last_radar_contracts,
            "x_radar_last_error": self.x_social.last_radar_error,
            "fomo_radar_enabled": self.settings.fomo_radar_enabled,
            "fomo_radar_poll_seconds": self.settings.fomo_radar_poll_seconds,
            "fomo_radar_scans": self.dex_screener.radar_scans,
            "fomo_radar_last_scan": self.dex_screener.last_radar_at,
            "fomo_radar_last_candidates": self.dex_screener.last_radar_candidates,
            "fomo_radar_last_error": self.dex_screener.last_radar_error,
            "fomo_runner_enabled": self.settings.fomo_runner_enabled,
            "fomo_runner_shadow_mode": True,
            "fomo_runner_fast_watch_seconds": (self.settings.fomo_runner_fast_watch_seconds),
            "fomo_runner_fast_watch_active": len(self._runner_fast_watch_tasks),
            "fomo_runner_observations": await self.database.runner_observation_count(),
            "fomo_runner_last_evaluated": self.runner_last_evaluated_at,
            "fomo_runner_last_mint": self.runner_last_candidate_mint,
            "fomo_runner_digest_enabled": self.settings.fomo_runner_digest_enabled,
            "fomo_runner_digest_seconds": self.settings.fomo_runner_digest_seconds,
            "fomo_runner_last_digest": (
                await self.database.get_setting("runner_last_digest_at")
            ),
            "trade_activity_alerts_enabled": self.settings.trade_activity_alerts_enabled,
            "news_radar_enabled": self.settings.news_radar_enabled,
            "news_source_image_enabled": self.settings.news_source_image_enabled,
            "no_x_launch_candidates_enabled": (self.settings.no_x_launch_candidates_enabled),
            "no_x_launch_min_score": self.settings.no_x_launch_min_score,
            "x_news_stream_enabled": self.settings.x_news_stream_enabled,
            "x_news_stream_configured": self.x_news_stream.configured,
            "x_news_stream_connected": self.x_news_stream.connected,
            "x_news_stream_rule_active": self.x_news_stream.rule_active,
            "x_news_stream_last_event": self.x_news_stream.last_event_at,
            "x_news_stream_last_error": self.x_news_stream.last_error,
            "news_rss_ready": self.news_poller.ready,
            "news_rss_last_refresh": self.news_poller.last_refresh_at,
            "news_rss_last_error": self.news_poller.last_error,
            "j7_feed_configured": bool(self.settings.j7_authorized_feed_url),
            "j7_feed_health": (
                self.news_poller.feed_health.get(
                    self.settings.j7_authorized_feed_url,
                    "waiting for first refresh",
                )
                if self.settings.j7_authorized_feed_url
                else "not configured"
            ),
            "pump_one_click_launch_enabled": self.settings.pump_one_click_launch_enabled,
            "pump_launch_unlocked": self.pump_launcher.configured,
            "pump_launch_wallet": self.pump_launcher.wallet_address,
            "launch_provider": self.pump_launcher.provider,
            "launch_lab_enabled": self.settings.launch_lab_enabled,
            "launch_lab_min_score": self.settings.launch_lab_min_score,
            "j7_public_wallet_configured": bool(self.pump_launcher.j7.wallet_address),
        }


def _first_int(*values: object) -> int | None:
    """First usable integer among the candidates, else None."""

    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _int_or_none_engine(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _engine_decimal(value: Any) -> Decimal | None:
    """Coerce a persisted value to :class:`Decimal`, or ``None`` when unusable."""

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
