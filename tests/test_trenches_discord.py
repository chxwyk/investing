"""The Trenches operator surfaces: commands, cards and Discord's hard limits.

`/fomo` was at 24 of Discord's 25 children before this release, so v2.43 adds
its five browsable sections, the public model board and the latency panel as
*views* on the existing `/fomo trending` rather than claiming the last slot
(section 78).  These tests hold that line, and hold the card honesty rules:
our rank is labelled as ours, an early card never implies safety, and every link
is derived from the exact mint.
"""

from __future__ import annotations

import struct
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from smart_money_bot import fast_alerts as fa
from smart_money_bot.constants import fomo_coin_url
from smart_money_bot.discord_render import MESSAGE_EMBED_LIMIT, build_embed, render_message
from smart_money_bot.pump_chain import decode_bonding_curve
from smart_money_bot.trenches import (
    MODEL_CAVEAT,
    PUBLIC_TRENDING_MODEL,
    BuyerRecord,
    HolderAccount,
    MarketObservation,
    assess_bundles,
    assess_depth,
    assess_participants,
    build_holder_snapshot,
    build_risk_profile,
    build_timeframe_profile,
    classify_lifecycle,
    score_public_trend,
    score_pump_trench,
)
from smart_money_bot.trenches.alerts import TRENCH_RUNNER, TierDecision
from smart_money_bot.trenches_runtime import TrenchCandidate

D = Decimal
NOW = 1_700_000_000
MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


def _candidate(*, almost_bonded: bool = True) -> TrenchCandidate:
    remaining = 79_310_000_000_000 if almost_bonded else 400_000_000_000_000
    curve = decode_bonding_curve(
        MINT,
        b"\x00" * 8
        + struct.pack(
            "<QQQQQ",
            1_073_000_000_000_000,
            30_000_000_000,
            remaining,
            5_000_000_000,
            1_000_000_000_000_000,
        )
        + b"\x00",
    )
    lifecycle = classify_lifecycle(curve, now=NOW, created_at=NOW - 192)
    buyers = [
        BuyerRecord(
            wallet=f"W{index}",
            at=NOW,
            amount_usd=D("40"),
            first_activity_at=NOW - 900_000,
            funded_by=f"S{index}",
        )
        for index in range(136)
    ]
    participants = assess_participants(MINT, buyers, buys=212, sells=78)
    timeframes = build_timeframe_profile(
        MINT,
        [
            MarketObservation(at=NOW - 880, market_cap_usd=D("18200")),
            MarketObservation(at=NOW - 300, market_cap_usd=D("21400")),
            MarketObservation(at=NOW - 30, market_cap_usd=D("22100")),
        ],
        now=NOW,
    )
    holders = build_holder_snapshot(
        MINT,
        [HolderAccount(address=f"H{index}", amount=D("10")) for index in range(50)],
        total_supply=D("500"),
        at=NOW,
    )
    depth = assess_depth(market_cap_usd=D("22100"), liquidity_usd=D("18700"))
    bundles = assess_bundles(MINT, [], created_at=NOW - 192)
    risk = build_risk_profile(
        MINT, liquidity_usd=D("18700"), top10_percent=holders.top10_percent
    )
    score = score_pump_trench(
        MINT,
        lifecycle=lifecycle,
        participants=participants,
        timeframes=timeframes,
        depth=depth,
        holders=holders,
        bundles=bundles,
        risk=risk,
    )
    public = score_public_trend(
        MINT, timeframes=timeframes, depth=depth, independent_buyers=136
    )
    return TrenchCandidate(
        mint=MINT,
        lifecycle=lifecycle,
        score=score,
        decision=TierDecision(
            mint=MINT, tier=TRENCH_RUNNER, ping=True, reasons=score.reasons
        ),
        timeframes=timeframes,
        participants=participants,
        bundles=bundles,
        risk=risk,
        public_trend=public,
        name="Quant",
        symbol="QUANT",
        market_cap_usd=D("22100"),
        first_market_cap_usd=D("18200"),
        liquidity_usd=D("18700"),
        holders=194,
        top10_percent=holders.top10_percent,
        age_seconds=192,
    )


# ---------------------------------------------------------------------------
# command budget (section 78)
# ---------------------------------------------------------------------------
def test_trenches_are_views_not_new_command_slots() -> None:
    from smart_money_bot.bot import FomoCommands

    names = {command.name for command in FomoCommands.__cog_app_commands__}
    for absent in ("trenches", "pump", "public", "trench", "almostbonded"):
        assert absent not in names, f"{absent} must be a view, not a child command"
    assert len(names) <= 24, "leave at least one child slot free for the next release"
    # `/fomo latency` predates this release and measures a different thing — the
    # lab's observation→alert pipeline.  v2.43's discovery latency (launch→
    # observation) is `view:latency` on `trending`, so the two stay separable.
    assert "latency" in names


async def test_every_trenches_view_resolves() -> None:
    """A view that leaves the spinner hanging is a broken command."""

    from smart_money_bot.bot import FomoCommands

    engine = SimpleNamespace(
        trenches_status=AsyncMock(
            return_value={"creation_stream": {"state": "CONNECTED"}, "tracked": 3}
        ),
        trenches_sections=AsyncMock(
            return_value={"new": [], "almost_bonded": [], "recently_bonded": [], "hot": []}
        ),
        trenches_public_board=AsyncMock(return_value=[]),
        trenches_latency=AsyncMock(return_value={}),
        trenches_token=AsyncMock(return_value=None),
    )
    cog = FomoCommands.__new__(FomoCommands)
    cog.bot = SimpleNamespace(engine=engine, settings=SimpleNamespace(fomo_referral_code=None))
    cog._require_admin = AsyncMock(return_value=True)

    for view in ("trenches", "new", "almostbonded", "recentlybonded", "hot", "public", "latency"):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
            user=SimpleNamespace(id=1),
        )
        await FomoCommands.trending.callback(cog, interaction, view=view)
        interaction.edit_original_response.assert_awaited()


async def test_the_trench_token_view_refuses_a_name() -> None:
    """Mint is identity, enforced at the command boundary."""

    from smart_money_bot.bot import FomoCommands

    cog = FomoCommands.__new__(FomoCommands)
    cog.bot = SimpleNamespace(
        engine=SimpleNamespace(trenches_token=AsyncMock(return_value=None)),
        settings=SimpleNamespace(fomo_referral_code=None),
    )
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await FomoCommands.trending.callback(cog, interaction, view="trenchtoken", mint=None)
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert "exact mint" in content


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------
def test_the_trench_runner_card_never_promises_safety_or_profit() -> None:
    candidate = _candidate()
    alert = fa.build_trench_runner_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENCH_RUNNER_ALERT,
        candidate=candidate,
        now=NOW,
    )
    embed = build_embed(alert.spec)
    text = " ".join(
        [embed.title or "", embed.description or "", embed.footer.text or ""]
        + [f"{field.name} {field.value}" for field in embed.fields]
    ).casefold()
    for forbidden in ("guaranteed", "safe winner", "free money", "cannot rug", "risk-free"):
        assert forbidden not in text
    assert "research only" in text
    assert "early is not safe" in text


def test_the_trench_card_states_bonding_stage_and_participation() -> None:
    """Section 47: the operator needs stage, bonding and independence."""

    candidate = _candidate()
    alert = fa.build_trench_runner_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.ALMOST_BONDED_ALERT,
        candidate=candidate,
        now=NOW,
    )
    names = {field.name for field in alert.spec.fields}
    assert "STAGE" in names
    assert "PARTICIPATION" in names
    stage = next(item for item in alert.spec.fields if item.name == "STAGE")
    assert "bonding" in stage.value
    participation = next(item for item in alert.spec.fields if item.name == "PARTICIPATION")
    assert "independent" in participation.value.casefold()


def test_the_public_trending_card_labels_the_rank_as_ours() -> None:
    """Sections 32, 48, 97: never claim someone else's ranking."""

    candidate = _candidate()
    alert = fa.build_public_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        candidate=candidate,
        rank=8,
        previous_rank=19,
        now=NOW,
    )
    embed = build_embed(alert.spec)
    text = " ".join(
        [embed.description or "", embed.footer.text or ""]
        + [f"{field.name} {field.value}" for field in embed.fields]
    )
    assert MODEL_CAVEAT in text
    assert "not Terminal" in text
    assert "not Fomo" in text
    rank_field = next(item for item in alert.spec.fields if "RANK" in item.name)
    assert "#8" in rank_field.value
    assert "was #19" in rank_field.value


def test_every_link_is_derived_from_the_exact_mint() -> None:
    """Sections 49-52: navigation only, and always this mint."""

    candidate = _candidate()
    alert = fa.build_trench_runner_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENCH_RUNNER_ALERT,
        candidate=candidate,
        now=NOW,
    )
    links = next(item for item in alert.spec.fields if item.name == "LINKS")
    for label in ("FOMO", "PUMP.FUN", "TERMINAL", "JUPITER", "DEX", "SOLSCAN"):
        assert label in links.value
    # Every URL carries this mint and no other.
    assert links.value.count(MINT) >= 5


def test_a_special_mode_token_is_flagged_on_the_card() -> None:
    """Section 28: a documented special state is not an ordinary token."""

    from dataclasses import replace

    candidate = _candidate()
    flagged = replace(
        candidate, lifecycle=replace(candidate.lifecycle, special_mode="MAYHEM")
    )
    alert = fa.build_trench_runner_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENCH_RUNNER_ALERT,
        candidate=flagged,
        now=NOW,
    )
    stage = next(item for item in alert.spec.fields if item.name == "STAGE")
    assert "MAYHEM" in stage.value
    assert "not an ordinary token" in stage.value


def test_every_trenches_card_fits_inside_one_discord_message() -> None:
    candidate = _candidate()
    cards = [
        fa.build_trench_runner_alert(
            mint=MINT,
            name="Quant" * 20,
            symbol="QUANT",
            fomo_url=fomo_coin_url(MINT),
            kind=kind,
            candidate=candidate,
            story="A long story. " * 60,
            chatter="Lots of chatter. " * 60,
            notable_wallets=3,
            reuse_warning="⚠ metadata reuse " * 20,
            now=NOW,
        )
        for kind in (
            fa.TRENCH_RUNNER_ALERT,
            fa.ALMOST_BONDED_ALERT,
            fa.TRENCH_HEADS_UP_ALERT,
        )
    ]
    cards.append(
        fa.build_public_trending_alert(
            mint=MINT,
            name="Quant",
            symbol="QUANT",
            fomo_url=fomo_coin_url(MINT),
            candidate=candidate,
            rank=3,
            story="A long story. " * 60,
            thesis="A long thesis. " * 60,
            mentions="Mentions. " * 60,
            now=NOW,
        )
    )
    for card in cards:
        embeds, _ = render_message([card.spec])
        assert len(embeds) <= MESSAGE_EMBED_LIMIT
        for embed in embeds:
            assert len(embed) <= 6000


def test_a_heads_up_is_radar_and_a_runner_is_urgent() -> None:
    """Section 36: radar broad, pings selective."""

    assert fa.TRENCH_HEADS_UP_ALERT not in fa.PINGABLE
    assert fa.TRENCH_RUNNER_ALERT in fa.PINGABLE
    assert fa.ALMOST_BONDED_ALERT in fa.PINGABLE
    assert fa.PUBLIC_TRENDING_ALERT in fa.PINGABLE
    assert set(fa.PINGABLE) <= set(fa.URGENT_CLASSES)


def test_the_public_model_name_is_never_a_third_party_claim() -> None:
    candidate = _candidate()
    assert candidate.public_trend.model == PUBLIC_TRENDING_MODEL
    assert "TERMINAL_TRENDING" not in candidate.public_trend.model
