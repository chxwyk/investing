"""Strategy configuration for the autonomous PAPER research laboratory.

The lab deliberately keeps its own configuration object instead of reading
:class:`~smart_money_bot.config.Settings` directly.  The trading brain must stay
provider-, Discord- and deployment-independent so a future instance can be
configured without rewriting strategy code, and so every decision can persist an
exact ``config_hash`` of the rules that produced it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

STRATEGY_VERSION = "lab-v1"
CHALLENGER_STRATEGY_VERSION = "lab-v1-challenger"


@dataclass(frozen=True, slots=True)
class LabConfig:
    """Immutable strategy thresholds.

    Every value is a *simulated* control.  Nothing here can spend real funds:
    the lab has no signing path at all, by construction.
    """

    strategy_version: str = STRATEGY_VERSION

    # ---- bankroll / sizing (section F, AJ) -------------------------------
    bankroll_usd: Decimal = Decimal("100")
    normal_position_usd: Decimal = Decimal("5")
    max_position_usd: Decimal = Decimal("10")
    min_position_usd: Decimal = Decimal("2")
    max_concurrent_positions: int = 5
    max_total_exposure_usd: Decimal = Decimal("30")
    max_token_exposure_usd: Decimal = Decimal("10")
    max_narrative_exposure_usd: Decimal = Decimal("15")
    daily_loss_cap_usd: Decimal = Decimal("15")

    # ---- execution feasibility (P, AI) ----------------------------------
    min_liquidity_usd: Decimal = Decimal("15000")
    max_price_impact_percent: Decimal = Decimal("2.5")
    max_slippage_percent: Decimal = Decimal("2.5")
    max_decision_latency_ms: int = 12_000

    # ---- cost model (AH) -------------------------------------------------
    platform_fee_bps: int = 100
    network_fee_usd: Decimal = Decimal("0.0008")
    priority_fee_usd: Decimal = Decimal("0.02")
    slippage_bps: int = 80

    # ---- expected net edge (AI) -----------------------------------------
    min_expected_net_edge_percent: Decimal = Decimal("12")
    edge_cushion_multiple: Decimal = Decimal("2.5")
    min_edge_confidence: Decimal = Decimal("40")

    # ---- overextension / edge decay (O, Q) -------------------------------
    max_move_since_signal_percent: Decimal = Decimal("120")
    max_signal_age_seconds: int = 900
    max_expansion_from_first_surface_percent: Decimal = Decimal("400")
    max_price_acceleration_ratio: Decimal = Decimal("4")
    min_buyer_acceleration_ratio: Decimal = Decimal("0.6")

    # ---- authenticity / independence (S, R) ------------------------------
    min_independent_buyers: int = 12
    min_independence_ratio: Decimal = Decimal("0.55")
    max_cluster_supply_percent: Decimal = Decimal("25")
    max_fee_concentration_percent: Decimal = Decimal("45")
    min_authenticity_score: Decimal = Decimal("45")

    # ---- lifecycle / re-entry (J, L) -------------------------------------
    winner_return_percent: Decimal = Decimal("60")
    exhaustion_drawdown_percent: Decimal = Decimal("30")
    retraced_drawdown_percent: Decimal = Decimal("50")
    cooldown_seconds: int = 3_600
    reentry_min_stable_observations: int = 3
    reentry_max_lower_lows: int = 1
    reentry_min_volume_recovery_ratio: Decimal = Decimal("1.15")
    reentry_size_multiplier: Decimal = Decimal("0.6")
    dead_cat_max_bounce_percent: Decimal = Decimal("25")

    # ---- re-alert suppression (K) ----------------------------------------
    republish_min_seconds: int = 900
    republish_min_opportunity_gain: Decimal = Decimal("8")
    republish_min_momentum_gain: Decimal = Decimal("10")
    republish_min_organic_gain: Decimal = Decimal("8")
    republish_min_buyer_gain: int = 15
    republish_min_liquidity_gain_percent: Decimal = Decimal("25")

    # ---- exit engine (AL, AM) --------------------------------------------
    #: ``(gain percent, fraction of the *remaining* position to sell)``.
    exit_ladder: tuple[tuple[str, str], ...] = (
        ("10", "0.25"),
        ("25", "0.30"),
        ("50", "0.30"),
        ("100", "0.40"),
    )
    moon_bag_percent: Decimal = Decimal("15")
    break_even_arm_percent: Decimal = Decimal("18")
    trailing_arm_percent: Decimal = Decimal("35")
    trailing_giveback_percent: Decimal = Decimal("35")
    momentum_decay_exit_score: Decimal = Decimal("25")
    flow_reversal_ratio: Decimal = Decimal("0.6")
    liquidity_emergency_decline_percent: Decimal = Decimal("45")
    hard_stop_loss_percent: Decimal = Decimal("35")
    time_stop_seconds: int = 5_400
    time_stop_min_progress_percent: Decimal = Decimal("6")

    # ---- circuit breakers (BB) -------------------------------------------
    consecutive_loss_limit: int = 4
    max_bankroll_drawdown_percent: Decimal = Decimal("25")
    stale_data_seconds: int = 300

    # ---- social radar budget (Z) -----------------------------------------
    broad_social_radar_enabled: bool = False
    social_posts_per_account: int = 10
    social_max_accounts_per_check: int = 6
    social_account_cache_seconds: int = 21_600
    social_daily_request_budget: int = 40
    social_min_account_samples: int = 20

    # ---- validation (AV, BH) ---------------------------------------------
    min_forward_sample: int = 30
    challenger_min_forward_sample: int = 40
    challenger_min_expectancy_gain: Decimal = Decimal("0.15")
    challenger_max_drawdown_slack: Decimal = Decimal("1")

    def config_hash(self) -> str:
        """Stable content hash so an old decision stays attributable."""

        payload = json.dumps(
            {key: _hashable(value) for key, value in sorted(asdict(self).items())},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def with_overrides(self, **overrides: object) -> LabConfig:
        return replace(self, **overrides)  # type: ignore[arg-type]

    @property
    def exit_milestones(self) -> tuple[tuple[Decimal, Decimal], ...]:
        return tuple(
            (Decimal(gain), Decimal(fraction)) for gain, fraction in self.exit_ladder
        )


def _hashable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple | list):
        return [_hashable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _hashable(item) for key, item in sorted(value.items())}
    return value


DEFAULT_LAB_CONFIG = LabConfig()


def lab_config_from_settings(settings: object) -> LabConfig:
    """Build a :class:`LabConfig` from deployment settings without coupling to it.

    Missing attributes fall back to the code default, so a deployment never has
    to define dozens of Railway variables to get a safe configuration.
    """

    def value(name: str, attribute: str) -> object:
        raw = getattr(settings, attribute, None)
        return raw if raw is not None else getattr(DEFAULT_LAB_CONFIG, name)

    return LabConfig(
        bankroll_usd=Decimal(str(value("bankroll_usd", "fomo_lab_bankroll_usd"))),
        normal_position_usd=Decimal(
            str(value("normal_position_usd", "fomo_lab_position_usd"))
        ),
        max_position_usd=Decimal(
            str(value("max_position_usd", "fomo_lab_max_position_usd"))
        ),
        max_concurrent_positions=int(
            value("max_concurrent_positions", "fomo_lab_max_concurrent_positions")
        ),
        max_total_exposure_usd=Decimal(
            str(value("max_total_exposure_usd", "fomo_lab_max_total_exposure_usd"))
        ),
        daily_loss_cap_usd=Decimal(
            str(value("daily_loss_cap_usd", "fomo_lab_daily_loss_cap_usd"))
        ),
        min_liquidity_usd=Decimal(
            str(value("min_liquidity_usd", "fomo_lab_min_liquidity_usd"))
        ),
        max_price_impact_percent=Decimal(
            str(value("max_price_impact_percent", "fomo_lab_max_price_impact_percent"))
        ),
        max_slippage_percent=Decimal(
            str(value("max_slippage_percent", "fomo_lab_max_slippage_percent"))
        ),
        min_expected_net_edge_percent=Decimal(
            str(value("min_expected_net_edge_percent", "fomo_lab_min_net_edge_percent"))
        ),
        platform_fee_bps=int(value("platform_fee_bps", "fomo_lab_platform_fee_bps")),
        slippage_bps=int(value("slippage_bps", "fomo_lab_slippage_bps")),
        priority_fee_usd=Decimal(
            str(value("priority_fee_usd", "fomo_lab_priority_fee_usd"))
        ),
        network_fee_usd=Decimal(
            str(value("network_fee_usd", "fomo_lab_network_fee_usd"))
        ),
        cooldown_seconds=int(value("cooldown_seconds", "fomo_lab_cooldown_seconds")),
        broad_social_radar_enabled=bool(
            value("broad_social_radar_enabled", "fomo_social_radar_enabled")
        ),
        social_posts_per_account=int(
            value("social_posts_per_account", "fomo_social_posts_per_account")
        ),
        social_daily_request_budget=int(
            value("social_daily_request_budget", "fomo_social_daily_request_budget")
        ),
        min_forward_sample=int(value("min_forward_sample", "fomo_lab_min_forward_sample")),
    )
