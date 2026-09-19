"""The cards and commands: what a human actually sees.

Two invariants: a probability never appears without the base rate it should be
compared against, and a value we could not compute never renders as a number.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import smart_money_bot.fast_alerts as fa
from smart_money_bot.pretrend.features import FeatureVector
from smart_money_bot.pretrend.forensics import ForensicReport
from smart_money_bot.pretrend.groundtruth import FOMO_TREND_ENTER, TrendEntryEvent
from smart_money_bot.pretrend.model import Prediction
from smart_money_bot.pretrend.states import TokenState
from smart_money_bot.pretrend_cards import (
    build_confirmation_card,
    build_pretrend_card,
    render_forensics,
)
from smart_money_bot.pretrend_runtime import PretrendSignal, TrendConfirmation

MINT = ("M" * 44)[:44]


def _vector(**overrides) -> FeatureVector:
    values = {
        "market_cap_usd": Decimal("68000"),
        "liquidity_usd": Decimal("22000"),
        "token_age_seconds": Decimal("540"),
        "unique_fomo_buyers_1m": Decimal("7"),
        "quality_fomo_buyers_1m": Decimal("4"),
        "independent_quality_buyers": Decimal("3"),
        "fomo_net_buy_usd_3m": Decimal("4200"),
        "fomo_thesis_count_3m": Decimal("2"),
        "holders_level_1m": Decimal("310"),
        "unique_buyers_level_1m": Decimal("88"),
        "volume_usd_level_5m": Decimal("51000"),
        "top10_percent": None,
    }
    values.update(overrides)
    missing = frozenset(name for name, value in values.items() if value is None)
    return FeatureVector(
        mint=MINT,
        observed_at=1_000,
        feature_version="pretrend.v1",
        values=values,
        missing=missing,
    )


def _signal(**overrides) -> PretrendSignal:
    vector = overrides.pop("vector", _vector())
    defaults = dict(
        mint=MINT,
        at=1_000,
        probability=Decimal("0.42"),
        horizon_seconds=300,
        reason="STATE_ENTERED_PRE_TREND",
        model_version="logistic_v1",
        feature_version="pretrend.v1",
        vector=vector,
        state=TokenState(mint=MINT),
        prediction=Prediction(
            probability=Decimal("0.42"),
            model_version="logistic_v1",
            feature_version="pretrend.v1",
            reason_codes=(("new_fomo_buyer_velocity_1m", Decimal("1.2")),),
            sample_support=1_840,
        ),
        base_rate=Decimal("0.008"),
        lift=Decimal("52.5"),
        sample_support=1_840,
    )
    defaults.update(overrides)
    return PretrendSignal(**defaults)


# --- taxonomy ----------------------------------------------------------------
def test_the_pretrend_classes_are_registered_and_may_interrupt() -> None:
    assert fa.PRE_TREND_SIGNAL in fa.ALERT_CLASSES
    assert fa.TRENDING_CONFIRMED_ALERT in fa.ALERT_CLASSES
    assert fa.PRE_TREND_SIGNAL in fa.PINGABLE
    # A pingable class must also ride the urgent lane, or the ping and the card
    # disagree about how important the message is.
    assert set(fa.PINGABLE) <= set(fa.URGENT_CLASSES)


# --- the pre-trend card ------------------------------------------------------
def test_the_probability_never_appears_without_its_base_rate() -> None:
    card = build_pretrend_card(_signal(), symbol="ABC", name="Alpha Beta")
    decision = next(field for field in card.fields if field.name == "TREND ESTIMATE")
    assert "42.0%" in decision.value
    assert "0.8%" in decision.value, "base rate must sit beside the probability"
    assert "52.5x" in decision.value
    assert "n=1840" in decision.value


def test_an_unknown_value_is_never_rendered_as_zero() -> None:
    card = build_pretrend_card(_signal(), symbol="ABC", name="Alpha Beta")
    onchain = next(field for field in card.fields if field.name == "ON-CHAIN")
    assert "top-10 unknown" in onchain.value
    assert "top-10 0" not in onchain.value


def test_the_card_names_why_it_fired_from_the_models_own_contributions() -> None:
    card = build_pretrend_card(_signal(), symbol="ABC", name="Alpha Beta")
    why = next(field for field in card.fields if field.name == "WHY THIS FIRED")
    assert "new_fomo_buyer_velocity_1m" in why.value


def test_a_sparse_vector_is_flagged_as_low_confidence() -> None:
    sparse = _vector(
        unique_fomo_buyers_1m=None,
        quality_fomo_buyers_1m=None,
        independent_quality_buyers=None,
        fomo_net_buy_usd_3m=None,
        fomo_thesis_count_3m=None,
        holders_level_1m=None,
        unique_buyers_level_1m=None,
        volume_usd_level_5m=None,
    )
    card = build_pretrend_card(_signal(vector=sparse), symbol="ABC", name="Alpha")
    risks = next(field for field in card.fields if field.name == "RISKS")
    assert "low-confidence" in risks.value


def test_the_card_says_it_is_research_and_places_no_trade() -> None:
    card = build_pretrend_card(_signal())
    assert "shadow mode" in card.description.lower()
    assert "no trade" in card.description.lower()


def test_no_measured_risk_is_not_reported_as_safe() -> None:
    clean = _vector(top10_percent=Decimal("12"), liquidity_usd=Decimal("80000"))
    card = build_pretrend_card(_signal(vector=clean))
    risks = next(field for field in card.fields if field.name == "RISKS")
    assert "not the same as safe" in risks.value


# --- the confirmation card ---------------------------------------------------
def test_the_confirmation_states_the_lead_time_when_we_called_it() -> None:
    confirmation = TrendConfirmation(
        mint=MINT,
        entered_at=1_500,
        initial_rank=6,
        market_cap_usd=Decimal("140000"),
        liquidity_usd=Decimal("40000"),
        volume_usd=Decimal("300000"),
        holders=900,
        token_age_seconds=1_040,
        predicted=True,
        lead_seconds=500,
        first_alert_at=1_000,
        first_alert_probability=Decimal("0.42"),
        first_alert_market_cap_usd=Decimal("68000"),
    )
    card = build_confirmation_card(confirmation)
    verdict = next(field for field in card.fields if "PREDICT IT" in field.name)
    assert "YES" in verdict.name
    assert "8m 20s early" in verdict.value
    assert "$68.0K" in verdict.value
    assert "$140.0K" in verdict.value


def test_the_confirmation_also_publishes_the_misses() -> None:
    """A card that only appears when the bot was right is marketing."""

    confirmation = TrendConfirmation(
        mint=MINT,
        entered_at=1_500,
        initial_rank=2,
        market_cap_usd=Decimal("140000"),
        liquidity_usd=None,
        volume_usd=None,
        holders=None,
        token_age_seconds=None,
        predicted=False,
    )
    card = build_confirmation_card(confirmation)
    verdict = next(field for field in card.fields if "PREDICT IT" in field.name)
    assert "MISSED" in verdict.name
    assert "false-negative" in verdict.value
    entry = next(field for field in card.fields if field.name == "AT ENTRY")
    assert "LIQ unknown" in entry.value


# --- forensics ---------------------------------------------------------------
def test_forensics_without_an_entry_refuses_to_guess() -> None:
    body = render_forensics(ForensicReport(mint=MINT, entry=None))
    assert "No FOMO_TREND_ENTER" in body
    assert "Nothing is inferred from a ticker" in body


def test_forensics_states_its_own_limitations() -> None:
    report = ForensicReport(
        mint=MINT,
        entry=TrendEntryEvent(
            kind=FOMO_TREND_ENTER,
            mint=MINT,
            occurred_at=1_500,
            initial_rank=4,
            market_cap_usd=Decimal("90000"),
        ),
        limitations=("no FOMO-native activity was collected for this mint",),
    )
    body = render_forensics(report)
    assert "LIMITATIONS OF THIS RECONSTRUCTION" in body
    assert "no FOMO-native activity" in body
    assert "no PRE_TREND alert preceded this entry" in body


# --- commands ----------------------------------------------------------------
async def test_the_forensics_command_refuses_a_ticker() -> None:
    from smart_money_bot.bot import PretrendCommands

    cog = PretrendCommands.__new__(PretrendCommands)
    cog.bot = SimpleNamespace(engine=SimpleNamespace(), settings=SimpleNamespace())
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await PretrendCommands.forensics.callback(cog, interaction, mint="BONK")
    content = interaction.response.send_message.await_args.args[0]
    assert "exact valid Solana mint" in content


async def test_pretrendstats_refuses_to_quote_precision_on_a_thin_sample() -> None:
    from smart_money_bot.bot import PretrendCommands

    cog = PretrendCommands.__new__(PretrendCommands)
    cog.bot = SimpleNamespace(
        engine=SimpleNamespace(
            pretrend_stats=AsyncMock(
                return_value={
                    "days": 7,
                    "feature_version": "pretrend.v1",
                    "trend_entries": 4,
                    "metrics": {
                        "resolved": 9,
                        "positives": 2,
                        "base_rate": 0.22,
                        "precision": 1.0,
                        "alerts": 2,
                        "alert_hits": 2,
                        "lift": 4.5,
                        "median_lead_seconds": 120,
                        "sufficient": False,
                    },
                    "alert_rate": {"by_kind": {}},
                    "snapshot_health": {
                        "accepted": 10,
                        "rejected": 1,
                        "acceptance_rate": 0.9,
                        "rejections": {},
                    },
                    "paper": {},
                    "model": None,
                    "activity_lane": {"configured": False, "detail": "not configured"},
                }
            )
        ),
        settings=SimpleNamespace(),
    )
    cog._require_admin = AsyncMock(return_value=True)
    cog._resolve = AsyncMock()
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await PretrendCommands.stats.callback(cog, interaction, days=7)

    embed = cog._resolve.await_args.kwargs["embed"]
    precision = next(field for field in embed.fields if field.name == "PRECISION")
    assert "Not enough resolved predictions" in precision.value
    assert "100" not in precision.value, "a 100% figure from 2 alerts must not be shown"


def test_the_pretrend_group_has_its_own_slots() -> None:
    from smart_money_bot.bot import FomoCommands, PretrendCommands

    fomo = {command.name for command in FomoCommands.__cog_app_commands__}
    pretrend = {command.name for command in PretrendCommands.__cog_app_commands__}
    # Discord caps a group at 25 children; both groups must stay under it.
    assert len(fomo) <= 24
    assert len(pretrend) <= 24
    assert {"stats", "forensics", "missed", "falsepositives", "modelhealth"} <= pretrend
