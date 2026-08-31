"""The SHADOW auto-trader: automatic $10 simulated entries on live signals.

Sections 1-6, 41, 42, 45, 46 and 53 of the Shadow contract.

STRICT PAPER (:mod:`smart_money_bot.lab.entry`) answers *"is this a trade I
would defend?"*.  SHADOW answers a different, purely empirical question:

    "What would have happened if the bot had automatically bought $10 the
    moment this signal appeared?"

The two are deliberately separate strategy families with separate bankrolls,
separate positions and separate reports.  SHADOW may simulate a trade STRICT
PAPER refuses — that is the whole point of the experiment — and this module
therefore contains **no** import from the strict entry engine and exposes
nothing the strict engine consumes.  Eligibility cannot leak in either
direction.

Sizing here is deliberately rigid.  Every accepted SHADOW entry deploys exactly
:attr:`ShadowConfig.position_usd` — $10 — or it is refused with a reason.  There
is no evidence-weighted sizing, no $5 fallback and no upsizing of a strong
signal, because a variable stake would make the per-family expectancy numbers
uncomparable, which is the one thing this experiment exists to measure.

**No real money.**  This module has no signer, no keypair, no RPC client, no
transaction builder and no swap submission path.  :data:`SHADOW_REAL_MONEY_SPEND`
is a structural zero, asserted by the test suite.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal

from .bankroll import (
    TRADING_ACTIVE,
    TRADING_PAUSED,
    BankrollState,
    BreakerInputs,
    BreakerStatus,
)
from .config import DEFAULT_LAB_CONFIG, LabConfig
from .exits import PaperPosition, position_from_json, position_to_json

ZERO = Decimal("0")
CENT = Decimal("0.01")
UNIT = Decimal("0.000001")
HUNDRED = Decimal("100")

SHADOW_STRATEGY_VERSION = "shadow-v1"

#: The experiment identity persisted at the checkpoint (section 42).
SHADOW_EXPERIMENT_VERSION = "shadow-experiment-1"

#: Structural invariant (section 53).  SHADOW cannot spend, and this constant is
#: asserted by the test suite alongside a source scan for signer/RPC/swap paths.
SHADOW_REAL_MONEY_SPEND = Decimal("0")

# --- signal families (section 3) ---------------------------------------------
FAMILY_FAST_WATCH = "FAST_WATCH"
FAMILY_FRESH_RUNNER = "FRESH_RUNNER"
FAMILY_NOTABLE_EARLY = "NOTABLE_TRADER_EARLY"
FAMILY_NOTABLE_LATE = "NOTABLE_TRADER_LATE"
FAMILY_BREAKING_CATALYST = "BREAKING_CATALYST"
FAMILY_CATALYST_WATCH = "CATALYST_WATCH"
FAMILY_CONFLUENCE_WATCH = "CONFLUENCE_WATCH"
FAMILY_QUALIFIED_RESEARCH = "QUALIFIED_RESEARCH"
FAMILY_STRICT_PAPER = "STRICT_PAPER_ENTRY"
# --- Trending families (v2.42, section 64) -----------------------------------
# The Trending experiment runs on the same engine with its own bankroll, so its
# families are registered here rather than in a parallel list: one registry means
# one place where "is this a real family?" is answered, and it keeps every
# family-weight, attribution and explanation path working unchanged.
FAMILY_TRENDING_NEW_ENTRY = "TRENDING_NEW_ENTRY"
FAMILY_TRENDING_ACCELERATION = "TRENDING_ACCELERATION"
FAMILY_TRENDING_STORY = "TRENDING_STORY"
FAMILY_TRENDING_THESIS = "TRENDING_THESIS"
FAMILY_TRENDING_AI_PROJECT = "TRENDING_AI_PROJECT"
FAMILY_TRENDING_SMART_MONEY = "TRENDING_SMART_MONEY"
FAMILY_TRENDING_CONTINUATION = "TRENDING_CONTINUATION"
FAMILY_TRENDING_CONFLUENCE = "TRENDING_CONFLUENCE"

SIGNAL_FAMILIES: tuple[str, ...] = (
    FAMILY_FAST_WATCH,
    FAMILY_FRESH_RUNNER,
    FAMILY_NOTABLE_EARLY,
    FAMILY_NOTABLE_LATE,
    FAMILY_BREAKING_CATALYST,
    FAMILY_CATALYST_WATCH,
    FAMILY_CONFLUENCE_WATCH,
    FAMILY_QUALIFIED_RESEARCH,
    FAMILY_STRICT_PAPER,
    FAMILY_TRENDING_NEW_ENTRY,
    FAMILY_TRENDING_ACCELERATION,
    FAMILY_TRENDING_STORY,
    FAMILY_TRENDING_THESIS,
    FAMILY_TRENDING_AI_PROJECT,
    FAMILY_TRENDING_SMART_MONEY,
    FAMILY_TRENDING_CONTINUATION,
    FAMILY_TRENDING_CONFLUENCE,
)

#: Human labels used on cards and in reports.
FAMILY_LABELS: dict[str, str] = {
    FAMILY_FAST_WATCH: "FAST WATCH",
    FAMILY_FRESH_RUNNER: "FRESH RUNNER",
    FAMILY_NOTABLE_EARLY: "NOTABLE TRADER EARLY",
    FAMILY_NOTABLE_LATE: "NOTABLE TRADER LATE",
    FAMILY_BREAKING_CATALYST: "BREAKING CATALYST",
    FAMILY_CATALYST_WATCH: "CATALYST WATCH",
    FAMILY_CONFLUENCE_WATCH: "CONFLUENCE WATCH",
    FAMILY_QUALIFIED_RESEARCH: "QUALIFIED RESEARCH",
    FAMILY_STRICT_PAPER: "STRICT PAPER",
    FAMILY_TRENDING_NEW_ENTRY: "TRENDING NEW ENTRY",
    FAMILY_TRENDING_ACCELERATION: "TRENDING ACCELERATION",
    FAMILY_TRENDING_STORY: "TRENDING STORY",
    FAMILY_TRENDING_THESIS: "TRENDING THESIS",
    FAMILY_TRENDING_AI_PROJECT: "TRENDING AI / PROJECT",
    FAMILY_TRENDING_SMART_MONEY: "TRENDING SMART MONEY",
    FAMILY_TRENDING_CONTINUATION: "TRENDING CONTINUATION",
    FAMILY_TRENDING_CONFLUENCE: "TRENDING CONFLUENCE",
}

#: Families whose signal is genuinely urgent enough to interrupt someone
#: (section 27B).  Every other family still publishes to the live radar.
URGENT_FAMILIES: frozenset[str] = frozenset(
    {
        FAMILY_NOTABLE_EARLY,
        FAMILY_BREAKING_CATALYST,
        FAMILY_CONFLUENCE_WATCH,
        # Trending is the primary universe (section 59); a new entrant, an
        # acceleration and a second leg are all time-critical.
        FAMILY_TRENDING_NEW_ENTRY,
        FAMILY_TRENDING_ACCELERATION,
        FAMILY_TRENDING_CONTINUATION,
        FAMILY_TRENDING_CONFLUENCE,
    }
)

# --- shadow reason codes -----------------------------------------------------
# Append-only: these strings are persisted with every simulated trade.
S_ACCEPTED = "SHADOW_ENTRY_ACCEPTED"
S_DISABLED = "SHADOW_DISABLED"
S_UNKNOWN_FAMILY = "SHADOW_UNKNOWN_SIGNAL_FAMILY"
S_ALREADY_HOLDING = "SHADOW_ALREADY_HOLDING_MINT"
S_MAX_POSITIONS = "SHADOW_MAX_POSITIONS_REACHED"
S_MAX_EXPOSURE = "SHADOW_MAX_EXPOSURE_REACHED"
S_MAX_TOKEN_EXPOSURE = "SHADOW_MAX_TOKEN_EXPOSURE_REACHED"
S_INSUFFICIENT_BANKROLL = "SHADOW_INSUFFICIENT_BANKROLL_FOR_FULL_SIZE"
S_DAILY_LOSS_CAP = "SHADOW_DAILY_LOSS_CAP"
S_BREAKER_PAUSED = "SHADOW_CIRCUIT_BREAKER_PAUSED"
S_NO_EXECUTABLE_ROUTE = "SHADOW_NO_EXECUTABLE_ROUTE"
S_NO_PRICE = "SHADOW_NO_USABLE_PRICE"
S_SIGNAL_STALE = "SHADOW_SIGNAL_STALE"
S_LATENCY_TOO_HIGH = "SHADOW_FILL_LATENCY_TOO_HIGH"
S_IMPACT_TOO_HIGH = "SHADOW_PRICE_IMPACT_UNTRADEABLE"
S_RUGGED = "SHADOW_RUG_EVIDENCE"
S_BEFORE_EXPERIMENT = "SHADOW_BEFORE_EXPERIMENT_START"
S_NO_AVERAGE_DOWN = "SHADOW_NO_AVERAGE_DOWN"
S_FAMILY_DISABLED = "SHADOW_FAMILY_DISABLED_ON_FORWARD_RESULTS"

HUMAN_SHADOW_REASONS: dict[str, str] = {
    S_ACCEPTED: "Signal accepted — simulating a $10 buy",
    S_DISABLED: "The shadow auto-trader is switched off",
    S_UNKNOWN_FAMILY: "The signal has no recognised shadow family",
    S_ALREADY_HOLDING: "A shadow position for this mint and family is already open",
    S_MAX_POSITIONS: "Maximum concurrent shadow positions reached",
    S_MAX_EXPOSURE: "Maximum total shadow exposure reached",
    S_MAX_TOKEN_EXPOSURE: "Maximum shadow exposure for this token reached",
    S_INSUFFICIENT_BANKROLL: (
        "Not enough simulated bankroll left for a full $10 entry — refused honestly "
        "rather than faked at a smaller size"
    ),
    S_DAILY_LOSS_CAP: "Daily simulated loss cap reached",
    S_BREAKER_PAUSED: "A shadow circuit breaker is active",
    S_NO_EXECUTABLE_ROUTE: "No legitimate executable route to fill $10",
    S_NO_PRICE: "No usable executable price at decision time",
    S_SIGNAL_STALE: "The signal is older than the shadow freshness window",
    S_LATENCY_TOO_HIGH: "Decision-to-fill latency is too high to trust the quote",
    S_IMPACT_TOO_HIGH: "Price impact on $10 makes the fill unrealistic",
    S_RUGGED: "Rug evidence is already present",
    S_BEFORE_EXPERIMENT: "Observation predates the forward experiment checkpoint",
    S_NO_AVERAGE_DOWN: "Shadow never adds to an existing position",
    S_FAMILY_DISABLED: (
        "This signal family has lost money and rugged often enough, over a large "
        "enough forward sample, to stop trading it"
    ),
}


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    """Immutable SHADOW controls.  Every value is simulated.

    ``position_usd``, ``min_position_usd`` and ``max_position_usd`` are all $10
    on purpose: a SHADOW entry is either the full size or it does not happen.
    """

    strategy_version: str = SHADOW_STRATEGY_VERSION
    enabled: bool = True

    # ---- bankroll and sizing (sections 4, 5) -----------------------------
    bankroll_usd: Decimal = Decimal("100")
    position_usd: Decimal = Decimal("10")
    min_position_usd: Decimal = Decimal("10")
    max_position_usd: Decimal = Decimal("10")
    max_concurrent_positions: int = 5
    max_total_exposure_usd: Decimal = Decimal("50")
    max_token_exposure_usd: Decimal = Decimal("10")

    # ---- profit objective (sections 8, 9) --------------------------------
    net_profit_objective_usd: Decimal = Decimal("2")
    #: Fraction of the remaining position secured when the objective is met and
    #: the runner is no longer convincing.
    secure_fraction_weak: Decimal = Decimal("0.75")
    secure_fraction_mixed: Decimal = Decimal("0.5")
    #: Fraction taken when the objective is met while the runner is genuinely
    #: accelerating — take something, keep meaningful exposure (section 9).
    secure_fraction_healthy: Decimal = Decimal("0.25")
    #: Above this multiple of the objective, recover principal even on a healthy
    #: runner and let the rest run as a funded moon bag (section 10).
    principal_recovery_multiple: Decimal = Decimal("3")

    # ---- execution feasibility (sections 7, 23, 39, 40) ------------------
    max_price_impact_percent: Decimal = Decimal("12")
    #: How old the signal itself may be when the shadow trader acts on it.
    max_signal_age_seconds: int = 900
    #: How stale the *quote* may be between the decision and the simulated fill.
    #: This bounds execution realism, not signal freshness — the two are
    #: deliberately separate gates with separate units.
    max_fill_latency_ms: int = 30_000
    #: A SHADOW fill may use a penalised fallback price, but it is always
    #: labelled.  Set false to require an executable quote or a venue simulation.
    allow_fallback_fill: bool = True

    # ---- cost model (section 8) ------------------------------------------
    platform_fee_bps: int = 100
    network_fee_usd: Decimal = Decimal("0.0008")
    priority_fee_usd: Decimal = Decimal("0.02")
    slippage_bps: int = 80

    # ---- circuit breakers (section 45) -----------------------------------
    daily_loss_cap_usd: Decimal = Decimal("15")
    consecutive_loss_limit: int = 6
    max_bankroll_drawdown_percent: Decimal = Decimal("35")
    stale_data_seconds: int = 300

    # ---- exit engine overlay (sections 11, 12) ---------------------------
    hard_stop_loss_percent: Decimal = Decimal("35")
    time_stop_seconds: int = 5_400
    #: How long the bot may lose sight of a position before it is closed at the
    #: last price it could actually verify.  Without this a token that drops off
    #: the radar would sit in the book forever, and the account headline would
    #: quietly report an unrealized number nobody could still trade out of.
    stale_position_seconds: int = 5_400
    time_stop_min_progress_percent: Decimal = Decimal("6")
    trailing_arm_percent: Decimal = Decimal("35")
    trailing_giveback_percent: Decimal = Decimal("35")
    break_even_arm_percent: Decimal = Decimal("18")
    moon_bag_percent: Decimal = Decimal("15")

    # ---- reporting (section 32) ------------------------------------------
    min_forward_sample: int = 30

    def __post_init__(self) -> None:
        # The $10 rule is a structural invariant, not a preference.  A
        # misconfigured deployment must fail loudly at construction rather than
        # quietly produce a $5 experiment nobody can interpret.
        if self.position_usd <= 0:
            raise ValueError("shadow position size must be positive")
        if self.min_position_usd != self.position_usd:
            raise ValueError("shadow minimum entry must equal the standard entry size")
        if self.max_position_usd != self.position_usd:
            raise ValueError("shadow maximum entry must equal the standard entry size")
        if self.max_token_exposure_usd < self.position_usd:
            raise ValueError("shadow per-token exposure cannot be below one entry")
        if self.max_total_exposure_usd < self.position_usd:
            raise ValueError("shadow total exposure cannot be below one entry")
        if self.max_concurrent_positions < 1:
            raise ValueError("shadow must allow at least one concurrent position")
        if self.bankroll_usd < self.position_usd:
            raise ValueError("shadow bankroll cannot be smaller than one entry")
        if self.net_profit_objective_usd <= 0:
            raise ValueError("the shadow NET profit objective must be positive")

    def config_hash(self) -> str:
        payload = json.dumps(
            {key: _hashable(value) for key, value in sorted(asdict(self).items())},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def with_overrides(self, **overrides: object) -> ShadowConfig:
        return replace(self, **overrides)  # type: ignore[arg-type]

    @property
    def objective_percent(self) -> Decimal:
        """The NET objective expressed as a return on one full entry."""

        return (self.net_profit_objective_usd / self.position_usd * HUNDRED).quantize(CENT)

    #: The staged exit ladder SHADOW inherits.  Same shape as the strict lab's,
    #: because the strict ladder already encodes +10/+25/+50/+100 correctly and
    #: section 11 asks for reuse rather than a second implementation.
    exit_ladder: tuple[tuple[str, str], ...] = (
        ("10", "0.25"),
        ("25", "0.30"),
        ("50", "0.30"),
        ("100", "0.40"),
    )
    momentum_decay_exit_score: Decimal = Decimal("25")
    flow_reversal_ratio: Decimal = Decimal("0.6")
    liquidity_emergency_decline_percent: Decimal = Decimal("45")

    def exit_config(self, base: LabConfig = DEFAULT_LAB_CONFIG) -> LabConfig:
        """A :class:`LabConfig` view for the *existing* staged exit engine.

        SHADOW deliberately reuses ``plan_exit``/``apply_exit`` rather than
        forking them: the ladder, break-even arming, trailing protection,
        momentum decay, flow reversal, liquidity emergencies, the hard stop and
        the time stop are already correct and already tested.  This projects the
        SHADOW controls onto the fields that engine reads, so the shared code
        runs with SHADOW's numbers and STRICT PAPER's configuration is never
        mutated.
        """

        return base.with_overrides(
            strategy_version=self.strategy_version,
            bankroll_usd=self.bankroll_usd,
            normal_position_usd=self.position_usd,
            max_position_usd=self.max_position_usd,
            min_position_usd=self.min_position_usd,
            max_concurrent_positions=self.max_concurrent_positions,
            max_total_exposure_usd=self.max_total_exposure_usd,
            max_token_exposure_usd=self.max_token_exposure_usd,
            daily_loss_cap_usd=self.daily_loss_cap_usd,
            platform_fee_bps=self.platform_fee_bps,
            network_fee_usd=self.network_fee_usd,
            priority_fee_usd=self.priority_fee_usd,
            slippage_bps=self.slippage_bps,
            exit_ladder=self.exit_ladder,
            moon_bag_percent=self.moon_bag_percent,
            break_even_arm_percent=self.break_even_arm_percent,
            trailing_arm_percent=self.trailing_arm_percent,
            trailing_giveback_percent=self.trailing_giveback_percent,
            momentum_decay_exit_score=self.momentum_decay_exit_score,
            flow_reversal_ratio=self.flow_reversal_ratio,
            liquidity_emergency_decline_percent=self.liquidity_emergency_decline_percent,
            hard_stop_loss_percent=self.hard_stop_loss_percent,
            time_stop_seconds=self.time_stop_seconds,
            time_stop_min_progress_percent=self.time_stop_min_progress_percent,
            consecutive_loss_limit=self.consecutive_loss_limit,
            max_bankroll_drawdown_percent=self.max_bankroll_drawdown_percent,
            stale_data_seconds=self.stale_data_seconds,
            min_forward_sample=self.min_forward_sample,
        )


DEFAULT_SHADOW_CONFIG = ShadowConfig()


def shadow_config_from_settings(settings: object) -> ShadowConfig:
    """Build a :class:`ShadowConfig` from deployment settings without coupling.

    Missing attributes fall back to the code default, so a deployment never has
    to define a single Railway variable to run the experiment at $100 / $10 / 5
    positions / $50 exposure.
    """

    def value(name: str, attribute: str) -> object:
        raw = getattr(settings, attribute, None)
        return raw if raw is not None else getattr(DEFAULT_SHADOW_CONFIG, name)

    position = Decimal(str(value("position_usd", "fomo_shadow_position_usd")))
    return ShadowConfig(
        enabled=bool(value("enabled", "fomo_shadow_auto_enabled")),
        bankroll_usd=Decimal(str(value("bankroll_usd", "fomo_shadow_bankroll_usd"))),
        # All three are the same number on purpose: a shadow entry is the full
        # size or it does not happen.
        position_usd=position,
        min_position_usd=position,
        max_position_usd=position,
        max_concurrent_positions=int(
            value("max_concurrent_positions", "fomo_shadow_max_positions")
        ),
        max_total_exposure_usd=Decimal(
            str(value("max_total_exposure_usd", "fomo_shadow_max_exposure_usd"))
        ),
        max_token_exposure_usd=position,
        net_profit_objective_usd=Decimal(
            str(value("net_profit_objective_usd", "fomo_shadow_net_profit_objective_usd"))
        ),
        daily_loss_cap_usd=Decimal(
            str(value("daily_loss_cap_usd", "fomo_shadow_daily_loss_cap_usd"))
        ),
        max_price_impact_percent=Decimal(
            str(value("max_price_impact_percent", "fomo_shadow_max_price_impact_percent"))
        ),
        max_signal_age_seconds=int(
            value("max_signal_age_seconds", "fomo_shadow_max_signal_age_seconds")
        ),
        max_fill_latency_ms=int(
            value("max_fill_latency_ms", "fomo_shadow_max_fill_latency_ms")
        ),
        allow_fallback_fill=bool(
            value("allow_fallback_fill", "fomo_shadow_allow_fallback_fill")
        ),
        min_forward_sample=int(value("min_forward_sample", "fomo_shadow_min_forward_sample")),
        platform_fee_bps=int(
            getattr(settings, "fomo_lab_platform_fee_bps", None)
            or DEFAULT_SHADOW_CONFIG.platform_fee_bps
        ),
        slippage_bps=int(
            getattr(settings, "fomo_lab_slippage_bps", None)
            or DEFAULT_SHADOW_CONFIG.slippage_bps
        ),
        priority_fee_usd=Decimal(
            str(
                getattr(settings, "fomo_lab_priority_fee_usd", None)
                or DEFAULT_SHADOW_CONFIG.priority_fee_usd
            )
        ),
        network_fee_usd=Decimal(
            str(
                getattr(settings, "fomo_lab_network_fee_usd", None)
                or DEFAULT_SHADOW_CONFIG.network_fee_usd
            )
        ),
    )


def _hashable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple | list):
        return [_hashable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _hashable(item) for key, item in sorted(value.items())}
    return value


@dataclass(frozen=True, slots=True)
class ShadowTimestamps:
    """Every clock the experiment needs to explain how late it was (section 6)."""

    signal_at: int = 0
    source_event_at: int | None = None
    first_seen_at: int | None = None
    discord_at: int | None = None
    decision_at: int = 0
    quote_at: int | None = None
    fill_at: int | None = None

    @property
    def signal_to_decision_ms(self) -> int | None:
        return _gap_ms(self.signal_at, self.decision_at)

    @property
    def decision_to_quote_ms(self) -> int | None:
        return _gap_ms(self.decision_at, self.quote_at)

    @property
    def quote_to_fill_ms(self) -> int | None:
        return _gap_ms(self.quote_at, self.fill_at)

    @property
    def signal_age_seconds(self) -> int | None:
        """How old the signal was when the shadow trader decided on it."""

        if not self.signal_at or not self.decision_at:
            return None
        return max(0, self.decision_at - self.signal_at)

    @property
    def decision_to_fill_ms(self) -> int | None:
        """Decision → quote → fill: how stale the price is when it is used.

        This is the interval a millisecond budget can meaningfully bound.  How
        old the *signal* was is a different question with a different unit, and
        :attr:`signal_age_seconds` answers it — conflating the two would make
        the freshness window unreachable.
        """

        legs = [self.decision_to_quote_ms, self.quote_to_fill_ms]
        known = [item for item in legs if item is not None]
        return sum(known) if known else None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "signal_at": self.signal_at,
            "source_event_at": self.source_event_at,
            "first_seen_at": self.first_seen_at,
            "discord_at": self.discord_at,
            "decision_at": self.decision_at,
            "quote_at": self.quote_at,
            "fill_at": self.fill_at,
            "signal_to_decision_ms": self.signal_to_decision_ms,
            "decision_to_quote_ms": self.decision_to_quote_ms,
            "quote_to_fill_ms": self.quote_to_fill_ms,
        }


def _gap_ms(start: int | None, end: int | None) -> int | None:
    if not start or not end:
        return None
    return max(0, (end - start) * 1000)


@dataclass(frozen=True, slots=True)
class ShadowSignal:
    """One research signal, with everything known about it *at signal time*.

    Nothing on this record may come from after :attr:`timestamps.decision_at`.
    The no-look-ahead test asserts that entry decisions are identical whether or
    not later observations exist.
    """

    mint: str
    family: str
    timestamps: ShadowTimestamps = field(default_factory=ShadowTimestamps)

    name: str = ""
    symbol: str = ""

    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    buys: int = 0
    sells: int = 0
    independent_buyers: int | None = None
    organic_score: Decimal | None = None
    momentum_score: Decimal | None = None
    actionability_state: str = ""
    actionability_score: Decimal | None = None
    safety_status: str = "UNKNOWN"
    catalyst_state: str = ""
    token_event_confidence: str = ""
    notable_wallet_evidence: str = ""
    smart_wallet_entries: int = 0
    route_available: bool = True
    rugged: bool = False
    lifecycle_state: str = ""
    graduation_state: str = "UNKNOWN"
    #: The notable trader's own entry market cap, when the family is a wallet one.
    trader_entry_market_cap_usd: Decimal | None = None
    detection_market_cap_usd: Decimal | None = None
    #: Catalyst timing evidence (section 19).
    event_at: int | None = None
    first_credible_source: str = ""
    mint_created_at: int | None = None
    catalyst_alert_at: int | None = None
    why: tuple[str, ...] = ()

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.sells <= 0:
            return Decimal("99") if self.buys > 0 else None
        return (Decimal(self.buys) / Decimal(self.sells)).quantize(CENT)

    @property
    def label(self) -> str:
        return FAMILY_LABELS.get(self.family, self.family)

    @property
    def urgent(self) -> bool:
        return self.family in URGENT_FAMILIES

    def evidence(self) -> dict[str, str]:
        """Everything the bot knew, persisted verbatim for attribution (§43)."""

        return {
            key: value
            for key, value in {
                "family": self.family,
                "name": self.name,
                "symbol": self.symbol,
                "price_usd": _text(self.price_usd),
                "market_cap_usd": _text(self.market_cap_usd),
                "liquidity_usd": _text(self.liquidity_usd),
                "volume_usd": _text(self.volume_usd),
                "buys": str(self.buys),
                "sells": str(self.sells),
                "buy_sell_ratio": _text(self.buy_sell_ratio),
                "independent_buyers": (
                    None if self.independent_buyers is None else str(self.independent_buyers)
                ),
                "organic_score": _text(self.organic_score),
                "momentum_score": _text(self.momentum_score),
                "actionability_state": self.actionability_state or None,
                "actionability_score": _text(self.actionability_score),
                "safety_status": self.safety_status,
                "catalyst_state": self.catalyst_state or None,
                "token_event_confidence": self.token_event_confidence or None,
                "notable_wallet_evidence": self.notable_wallet_evidence or None,
                "smart_wallet_entries": str(self.smart_wallet_entries),
                "route_available": "1" if self.route_available else "0",
                "rugged": "1" if self.rugged else "0",
                "lifecycle_state": self.lifecycle_state or None,
                "graduation_state": self.graduation_state,
                "trader_entry_market_cap_usd": _text(self.trader_entry_market_cap_usd),
                "detection_market_cap_usd": _text(self.detection_market_cap_usd),
                "first_credible_source": self.first_credible_source or None,
                # Catalyst timing (section 19) has to be persisted at signal
                # time or it cannot be reconstructed afterwards.
                "event_at": None if self.event_at is None else str(self.event_at),
                "mint_created_at": (
                    None if self.mint_created_at is None else str(self.mint_created_at)
                ),
                "catalyst_alert_at": (
                    None if self.catalyst_alert_at is None else str(self.catalyst_alert_at)
                ),
                "why": " | ".join(self.why) if self.why else None,
            }.items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class ShadowExposure:
    """What the simulated book already holds, as persisted."""

    open_positions: int = 0
    open_exposure_usd: Decimal = ZERO
    token_exposure_usd: Decimal = ZERO
    holds_same_family: bool = False


#: An empty book, so callers need no sentinel of their own.
EMPTY_EXPOSURE = ShadowExposure()

#: No provider or control fault reported.
NO_BREAKER_INPUTS = BreakerInputs()


@dataclass(frozen=True, slots=True)
class ShadowEntryDecision:
    """Accept or refuse one signal.  An accepted entry is always exactly $10."""

    mint: str
    family: str
    accepted: bool = False
    size_usd: Decimal = ZERO
    reason_codes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    decided_at: int = 0
    strategy_version: str = SHADOW_STRATEGY_VERSION
    config_hash: str = ""

    @property
    def primary_reason(self) -> str:
        return self.reason_codes[0] if self.reason_codes else S_UNKNOWN_FAMILY

    @property
    def human_reason(self) -> str:
        return HUMAN_SHADOW_REASONS.get(self.primary_reason, self.primary_reason)


def evaluate_shadow_breakers(
    state: BankrollState,
    inputs: BreakerInputs = NO_BREAKER_INPUTS,
    *,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> BreakerStatus:
    """Even fake money models survival rules (section 45).

    Deliberately mirrors the strict lab's breaker vocabulary so one operator
    reads one set of reason codes, while keeping SHADOW's own, looser research
    thresholds off the strict configuration.
    """

    reasons: list[str] = []
    if state.consecutive_losses >= config.consecutive_loss_limit:
        reasons.append("CONSECUTIVE_LOSS_LIMIT")
    if state.drawdown_percent >= config.max_bankroll_drawdown_percent:
        reasons.append("ROLLING_DRAWDOWN")
    if state.day_realized_net_pnl_usd <= -config.daily_loss_cap_usd:
        reasons.append("DAILY_LOSS_CAP")
    if inputs.provider_outage:
        reasons.append("PROVIDER_OUTAGE")
    if inputs.rpc_unstable:
        reasons.append("RPC_INSTABILITY")
    if inputs.route_outage:
        reasons.append("ROUTE_OUTAGE")
    if inputs.abnormal_congestion:
        reasons.append("ABNORMAL_CONGESTION")
    if inputs.persistence_failure:
        reasons.append("PERSISTENCE_FAILURE")
    if (
        inputs.stale_critical_data_seconds is not None
        and inputs.stale_critical_data_seconds > config.stale_data_seconds
    ):
        reasons.append("STALE_CRITICAL_DATA")
    if reasons:
        return BreakerStatus(state=TRADING_PAUSED, reasons=tuple(reasons))
    return BreakerStatus(state=TRADING_ACTIVE)


def evaluate_shadow_entry(
    signal: ShadowSignal,
    state: BankrollState,
    exposure: ShadowExposure = EMPTY_EXPOSURE,
    *,
    route_price_impact_percent: Decimal | None = None,
    route_available: bool | None = None,
    fill_source: str | None = None,
    experiment_started_at: int | None = None,
    breakers: BreakerStatus | None = None,
    family_enabled: bool = True,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> ShadowEntryDecision:
    """Decide whether this signal opens a $10 simulated position.

    The size is never negotiated.  Every blocking rule returns a refusal with a
    reason code rather than a smaller trade, because a $7 entry booked as a "$10
    strategy" would corrupt every expectancy number the experiment produces
    (section 4).
    """

    decided_at = signal.timestamps.decision_at or signal.timestamps.signal_at
    reasons: list[str] = []
    notes: list[str] = []

    def refuse(code: str, note: str = "") -> ShadowEntryDecision:
        if note:
            notes.append(note)
        return ShadowEntryDecision(
            mint=signal.mint,
            family=signal.family,
            accepted=False,
            size_usd=ZERO,
            reason_codes=(code, *reasons),
            notes=tuple(notes),
            decided_at=decided_at,
            strategy_version=config.strategy_version,
            config_hash=config.config_hash(),
        )

    if not config.enabled:
        return refuse(S_DISABLED)
    if signal.family not in SIGNAL_FAMILIES:
        return refuse(S_UNKNOWN_FAMILY)
    if not family_enabled:
        # Forward evidence, not opinion, retired this family.  The refusal is
        # still recorded, so the demotion stays auditable and reversible.
        return refuse(S_FAMILY_DISABLED)
    if experiment_started_at is not None and decided_at < experiment_started_at:
        # Section 41: the forward experiment starts at deployment.  A historical
        # observation may be replayed, never booked as a live shadow trade.
        return refuse(S_BEFORE_EXPERIMENT)

    breaker_status = breakers if breakers is not None else evaluate_shadow_breakers(
        state, config=config
    )
    if breaker_status.paused or state.is_paused:
        return refuse(
            S_BREAKER_PAUSED,
            ", ".join(breaker_status.reasons) or state.paused_reason,
        )
    if state.day_realized_net_pnl_usd <= -config.daily_loss_cap_usd:
        return refuse(S_DAILY_LOSS_CAP)

    # --- execution feasibility ------------------------------------------
    if signal.rugged:
        return refuse(S_RUGGED)
    available = route_available if route_available is not None else signal.route_available
    if not available:
        return refuse(S_NO_EXECUTABLE_ROUTE)
    if signal.price_usd is None or signal.price_usd <= 0:
        return refuse(S_NO_PRICE)
    if fill_source is not None and fill_source not in {
        "EXECUTABLE_QUOTE",
        "SIMULATED_VENUE_STATE",
        "FALLBACK_PENALISED",
    }:
        return refuse(S_NO_EXECUTABLE_ROUTE)
    if fill_source == "FALLBACK_PENALISED" and not config.allow_fallback_fill:
        return refuse(S_NO_EXECUTABLE_ROUTE, "only a penalised fallback price was available")
    if (
        route_price_impact_percent is not None
        and route_price_impact_percent > config.max_price_impact_percent
    ):
        return refuse(
            S_IMPACT_TOO_HIGH,
            f"${config.position_usd} would move the price {route_price_impact_percent}%",
        )

    age = signal.timestamps.signal_age_seconds
    if age is not None and age > config.max_signal_age_seconds:
        return refuse(S_SIGNAL_STALE, f"signal was {age}s old at the decision")
    latency = signal.timestamps.decision_to_fill_ms
    if latency is not None and latency > config.max_fill_latency_ms:
        # The quote the fill would be priced from is too old to be believable.
        return refuse(S_LATENCY_TOO_HIGH, f"{latency}ms from decision to fill")

    # --- book rules (section 5) ------------------------------------------
    if exposure.holds_same_family:
        return refuse(S_ALREADY_HOLDING)
    if exposure.token_exposure_usd > 0:
        # Never average down, never add a second $10 to the same token.
        return refuse(S_NO_AVERAGE_DOWN)
    if exposure.open_positions >= config.max_concurrent_positions:
        return refuse(S_MAX_POSITIONS)

    size = config.position_usd
    if exposure.token_exposure_usd + size > config.max_token_exposure_usd:
        return refuse(S_MAX_TOKEN_EXPOSURE)
    if exposure.open_exposure_usd + size > config.max_total_exposure_usd:
        return refuse(
            S_MAX_EXPOSURE,
            f"${exposure.open_exposure_usd} of ${config.max_total_exposure_usd} deployed",
        )
    if state.cash_usd < size:
        # Section 4: if only $7 remains, refuse honestly.  Never book a $10
        # trade the simulated account could not have funded.
        return refuse(
            S_INSUFFICIENT_BANKROLL,
            f"${state.cash_usd} of simulated cash left; ${size} required",
        )

    return ShadowEntryDecision(
        mint=signal.mint,
        family=signal.family,
        accepted=True,
        size_usd=size,
        reason_codes=(S_ACCEPTED,),
        notes=tuple(notes),
        decided_at=decided_at,
        strategy_version=config.strategy_version,
        config_hash=config.config_hash(),
    )


def why_you_are_seeing_this(signal: ShadowSignal) -> tuple[str, ...]:
    """The mandatory per-alert explanation (section 30).

    Never empty: an alert the bot cannot explain is an alert the operator cannot
    act on, so a family with no specific evidence still states its own meaning.
    """

    if signal.why:
        return signal.why
    defaults: dict[str, tuple[str, ...]] = {
        FAMILY_FAST_WATCH: ("early acceleration on cheap evidence", "safety still pending"),
        FAMILY_FRESH_RUNNER: ("fresh pair with expanding activity",),
        FAMILY_NOTABLE_EARLY: ("a tracked public wallet bought recently",),
        FAMILY_NOTABLE_LATE: ("a tracked public wallet bought, observed late",),
        FAMILY_BREAKING_CATALYST: ("a credible external event was detected",),
        FAMILY_CATALYST_WATCH: ("an external event may be connected to this token",),
        FAMILY_CONFLUENCE_WATCH: (
            "catalyst, smart wallets and market acceleration agree",
        ),
        FAMILY_QUALIFIED_RESEARCH: ("the research funnel qualified this candidate",),
        FAMILY_STRICT_PAPER: ("the strict PAPER engine accepted this entry",),
        FAMILY_TRENDING_NEW_ENTRY: (
            "the token just entered the Trending board",
            "trending is attention, not safety",
        ),
        FAMILY_TRENDING_ACCELERATION: (
            "Trending rank is climbing quickly",
            "market data is confirming the attention",
        ),
        FAMILY_TRENDING_STORY: ("a corroborated story sits behind this exact mint",),
        FAMILY_TRENDING_THESIS: ("a supported public thesis names this exact mint",),
        FAMILY_TRENDING_AI_PROJECT: (
            "the named project publishes this exact mint",
        ),
        FAMILY_TRENDING_SMART_MONEY: (
            "a proven public wallet entered while the token is Trending",
        ),
        FAMILY_TRENDING_CONTINUATION: (
            "not early — a second leg is developing on new evidence",
        ),
        FAMILY_TRENDING_CONFLUENCE: (
            "several independent evidence families agree on this mint",
        ),
    }
    return defaults.get(signal.family, ("research signal",))


def shadow_publication_lane(signal: ShadowSignal, *, urgent_override: bool = False) -> str:
    """Which visibility layer this signal belongs to (sections 27-29).

    Two lanes, never one: a signal that does not deserve an @ mention is still
    worth publishing, so nothing is hidden merely because it is not urgent.
    """

    return "URGENT" if (urgent_override or signal.urgent) else "RADAR"


def family_from_alert_kind(kind: str) -> str:
    """Map an existing fast-alert class onto its shadow family."""

    mapping = {
        "FAST_WATCH": FAMILY_FAST_WATCH,
        "NOTABLE_TRADER_EARLY": FAMILY_NOTABLE_EARLY,
        "NOTABLE_TRADER_LATE": FAMILY_NOTABLE_LATE,
        "BREAKING_CATALYST": FAMILY_BREAKING_CATALYST,
        "CATALYST_WATCH": FAMILY_CATALYST_WATCH,
        "CONFLUENCE_WATCH": FAMILY_CONFLUENCE_WATCH,
    }
    return mapping.get(kind, "")


@dataclass(frozen=True, slots=True)
class ShadowPosition:
    """A simulated $10 position plus the shadow metadata around it.

    The position itself is the *existing* :class:`PaperPosition` so the shared
    exit engine, journal and cost model apply unchanged; everything SHADOW adds
    — which family produced the signal, which venue filled it, how the fill was
    priced, and the peak NET the position ever showed — lives beside it rather
    than inside strict PAPER's record.
    """

    position: PaperPosition
    family: str = ""
    experiment_version: str = SHADOW_EXPERIMENT_VERSION
    venue: str = "UNKNOWN"
    fill_source: str = ""
    graduation_state: str = "UNKNOWN"
    peak_net_pnl_usd: Decimal = ZERO
    signal_evidence: dict[str, str] = field(default_factory=dict)
    timestamps: ShadowTimestamps = field(default_factory=ShadowTimestamps)
    entry_route: dict[str, str] = field(default_factory=dict)
    exit_route: dict[str, str] = field(default_factory=dict)

    @property
    def mint(self) -> str:
        return self.position.mint

    @property
    def position_id(self) -> str:
        return self.position.position_id

    @property
    def is_open(self) -> bool:
        return self.position.is_open

    @property
    def label(self) -> str:
        return FAMILY_LABELS.get(self.family, self.family)

    def to_payload(self) -> dict[str, object]:
        return {
            "position": json.loads(position_to_json(self.position)),
            "family": self.family,
            "experiment_version": self.experiment_version,
            "venue": self.venue,
            "fill_source": self.fill_source,
            "graduation_state": self.graduation_state,
            "peak_net_pnl_usd": str(self.peak_net_pnl_usd),
            "signal_evidence": dict(self.signal_evidence),
            "timestamps": self.timestamps.as_dict(),
            "entry_route": dict(self.entry_route),
            "exit_route": dict(self.exit_route),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ShadowPosition:
        raw_position = payload.get("position")
        position = position_from_json(
            json.dumps(raw_position if isinstance(raw_position, dict) else {})
        )
        stamps = payload.get("timestamps")
        stamp_data = stamps if isinstance(stamps, dict) else {}
        return cls(
            position=position,
            family=str(payload.get("family") or ""),
            experiment_version=str(
                payload.get("experiment_version") or SHADOW_EXPERIMENT_VERSION
            ),
            venue=str(payload.get("venue") or "UNKNOWN"),
            fill_source=str(payload.get("fill_source") or ""),
            graduation_state=str(payload.get("graduation_state") or "UNKNOWN"),
            peak_net_pnl_usd=_decimal(payload.get("peak_net_pnl_usd")),
            signal_evidence={
                str(key): str(value)
                for key, value in (payload.get("signal_evidence") or {}).items()  # type: ignore[union-attr]
            },
            timestamps=ShadowTimestamps(
                signal_at=int(stamp_data.get("signal_at") or 0),
                source_event_at=_int_or_none(stamp_data.get("source_event_at")),
                first_seen_at=_int_or_none(stamp_data.get("first_seen_at")),
                discord_at=_int_or_none(stamp_data.get("discord_at")),
                decision_at=int(stamp_data.get("decision_at") or 0),
                quote_at=_int_or_none(stamp_data.get("quote_at")),
                fill_at=_int_or_none(stamp_data.get("fill_at")),
            ),
            entry_route={
                str(key): str(value)
                for key, value in (payload.get("entry_route") or {}).items()  # type: ignore[union-attr]
            },
            exit_route={
                str(key): str(value)
                for key, value in (payload.get("exit_route") or {}).items()  # type: ignore[union-attr]
            },
        )


def _decimal(value: object, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return None


def deterministic_position_id(
    *,
    mint: str,
    family: str,
    signal_at: int,
    strategy_version: str = SHADOW_STRATEGY_VERSION,
) -> str:
    """A replayed signal must be a no-op, not a second simulated position."""

    return f"{strategy_version}:{family}:{mint}:{signal_at}"


def total_exposure(sizes: Sequence[Decimal]) -> Decimal:
    return sum(sizes, ZERO).quantize(UNIT)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
