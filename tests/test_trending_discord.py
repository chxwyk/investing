"""The Trending operator surfaces: commands, cards and Discord's hard limits.

Two structural constraints shape this release's UX and are asserted here.

Discord allows **25 subcommands per group** and `/fomo` was already at 23 when
this work started.  So Trending gets exactly one child command with a ``view``
parameter (sections 86, 87) rather than four, which is the only shape that
leaves room to grow.

And every card has to survive Discord's embed limits *after* rendering, not in
principle — an oversized card is a card the operator never sees.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from smart_money_bot import fast_alerts as fa
from smart_money_bot.constants import fomo_coin_url
from smart_money_bot.discord_render import (
    MESSAGE_EMBED_LIMIT,
    build_embed,
    render_message,
)
from smart_money_bot.trending import (
    TrendingLedgerEntry,
    TrendingObservation,
    assess_holders,
    build_risk_panel,
    classify_trending_event,
    link_matches_mint,
    rank_velocity,
    score_trending_edge,
    source_from_settings,
)

D = Decimal
NOW = 1_700_000_000
MINT = "Mint1111111111111111111111111111111111111111"
PROXY = source_from_settings(api_url=None, api_key=None, proxy_enabled=True)


def _candidate():
    entry = TrendingLedgerEntry.from_first_observation(
        TrendingObservation(
            mint=MINT,
            observed_at=NOW - 300,
            rank=19,
            market_cap_usd=D("820000"),
            liquidity_usd=D("90000"),
            holder_count=300,
            top10_percent=D("22"),
            name="Quant",
            symbol="QUANT",
            source=PROXY,
        )
    )
    entry = entry.observe(
        TrendingObservation(
            mint=MINT,
            observed_at=NOW,
            rank=7,
            market_cap_usd=D("940000"),
            liquidity_usd=D("90000"),
            holder_count=760,
            top10_percent=D("22"),
            source=PROXY,
        )
    )
    velocity = rank_velocity(entry.rank_history, now=NOW, first_seen_at=entry.first_seen_at)
    event = classify_trending_event(entry, velocity, now=NOW, market_cap_velocity=D("3"))
    holders = assess_holders(
        MINT,
        holder_count=760,
        first_holder_count=300,
        seconds_elapsed=300,
        top10_percent=D("22"),
        first_top10_percent=D("22"),
    )
    score = score_trending_edge(entry, event, holders=holders, market_cap_velocity=D("3"))
    return entry, event, score, holders


# ---------------------------------------------------------------------------
# command registration (sections 86, 87)
# ---------------------------------------------------------------------------
def test_trending_is_one_child_command_with_views_not_four_commands() -> None:
    from smart_money_bot.bot import FomoCommands

    names = {command.name for command in FomoCommands.__cog_app_commands__}
    assert "trending" in names
    # The views live on the parameter, not on extra slots.
    for absent in ("trending-token", "trending-hotwatch", "trending-why", "hotwatch"):
        assert absent not in names
    # Discord's hard ceiling, with headroom deliberately left over.
    assert len(names) <= 25


def test_the_group_keeps_room_to_grow() -> None:
    from smart_money_bot.bot import FomoCommands

    names = {command.name for command in FomoCommands.__cog_app_commands__}
    assert len(names) <= 24, "leave at least one child slot free for the next release"


async def test_the_trending_command_refuses_a_name_instead_of_a_mint() -> None:
    """Section 13, at the command boundary: a ticker is not an identity."""

    from smart_money_bot.bot import FomoCommands

    bot = SimpleNamespace(
        engine=SimpleNamespace(trending_status=AsyncMock(return_value={})),
        settings=SimpleNamespace(fomo_referral_code=None),
    )
    cog = FomoCommands.__new__(FomoCommands)
    cog.bot = bot
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await FomoCommands.trending.callback(cog, interaction, view="token", mint=None)
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert "exact mint" in content


async def test_the_trending_command_never_leaves_the_spinner_hanging() -> None:
    from smart_money_bot.bot import FomoCommands

    bot = SimpleNamespace(
        engine=SimpleNamespace(
            trending_status=AsyncMock(side_effect=RuntimeError("provider down"))
        ),
        settings=SimpleNamespace(fomo_referral_code=None),
    )
    cog = FomoCommands.__new__(FomoCommands)
    cog.bot = bot
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await FomoCommands.trending.callback(cog, interaction, view="board")
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert "RuntimeError" in content
    assert "$0.00" in content


async def test_the_profit_command_exposes_the_universe_comparison() -> None:
    """Section 89: the scoreboard is a view, not a new command slot."""

    from smart_money_bot.bot import FomoCommands

    payload = {
        "trending": {"net_usd": "4.20", "trades": 12, "provisional": False},
        "legacy": {"net_usd": "-1.10", "trades": 15, "provisional": False},
        "verdict": "NET: TRENDING • SAFETY: TRENDING • UPSIDE: LEGACY",
        "net_leader": "TRENDING",
        "safety_leader": "TRENDING",
        "upside_leader": "LEGACY",
    }
    bot = SimpleNamespace(
        engine=SimpleNamespace(trending_universes=AsyncMock(return_value=payload)),
        settings=SimpleNamespace(fomo_referral_code=None),
    )
    cog = FomoCommands.__new__(FomoCommands)
    cog.bot = bot
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await FomoCommands.profit.callback(cog, interaction, view="universes")
    embed = interaction.edit_original_response.await_args.kwargs["embed"]
    text = f"{embed.title} {embed.description}"
    assert "TRENDING" in text and "LEGACY" in text
    assert "DISABLED" in embed.description


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------
def test_the_trending_card_separates_a_claim_from_its_corroboration() -> None:
    """Section 17: developer marketing must never render as verified."""

    entry, event, score, holders = _candidate()
    alert = fa.build_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENDING_ALPHA,
        entry=entry,
        event=event,
        score=score,
        holders=holders,
        about_summary="Claims a quantum AI platform connected with project XYZ.",
        project_claim="AI",
        external_verification="UNVERIFIED — the project does not mention this mint",
        now=NOW,
    )
    about = next(item for item in alert.spec.fields if "ABOUT" in item.name)
    assert "the project's own claim" in about.name
    assert "UNVERIFIED" in about.value


def test_the_trending_card_never_promises_safety_or_profit() -> None:
    """Section 61: LOOK NOW is allowed; guaranteed, safe and free money are not."""

    entry, event, score, holders = _candidate()
    alert = fa.build_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENDING_ALPHA,
        entry=entry,
        event=event,
        score=score,
        holders=holders,
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
    assert "manual decision" in text


def test_the_trending_card_names_why_it_pinged() -> None:
    """Section 57: no card may exist whose only justification is a number."""

    entry, event, score, holders = _candidate()
    alert = fa.build_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENDING_ALPHA,
        entry=entry,
        event=event,
        score=score,
        holders=holders,
        now=NOW,
    )
    why = next(item for item in alert.spec.fields if "WHY" in item.name)
    assert score.reasons
    assert "no named reason" not in why.value


def test_a_verified_badge_renders_as_a_badge_not_as_protection() -> None:
    entry, event, score, holders = _candidate()
    risk = build_risk_panel(
        MINT, liquidity_usd=D("90000"), holders=holders, fomo_verified="VERIFIED"
    )
    alert = fa.build_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENDING_ALPHA,
        entry=entry,
        event=event,
        score=score,
        holders=holders,
        risk=risk,
        now=NOW,
    )
    risk_field = next(item for item in alert.spec.fields if item.name == "RISK")
    assert "not rug protection" in risk_field.value or "not a safety guarantee" in risk_field.value


def test_an_already_large_token_is_labelled_not_early() -> None:
    """Section 11: do not pretend a $1M token is early."""

    entry, event, score, holders = _candidate()
    assert event.already_large is True
    alert = fa.build_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENDING_CONTINUATION_ALERT,
        entry=entry,
        event=event,
        score=score,
        holders=holders,
        now=NOW,
    )
    warnings = next(item for item in alert.spec.fields if item.name == "⚠")
    assert "NOT EARLY" in warnings.value


def test_the_proxy_caveat_travels_with_the_card() -> None:
    """Section 4: a proxy rank must be labelled wherever it is shown."""

    entry, event, score, holders = _candidate()
    alert = fa.build_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENDING_ALPHA,
        entry=entry,
        event=event,
        score=score,
        holders=holders,
        source_caveat=PROXY.rank_caveat(),
        now=NOW,
    )
    warnings = next(item for item in alert.spec.fields if item.name == "⚠")
    assert "PROXY" in warnings.value


def test_every_link_on_the_card_points_at_this_exact_mint() -> None:
    """Section 14: never show one token and link to another."""

    entry, event, score, holders = _candidate()
    alert = fa.build_trending_alert(
        mint=MINT,
        name="Quant",
        symbol="QUANT",
        fomo_url=fomo_coin_url(MINT),
        kind=fa.TRENDING_ALPHA,
        entry=entry,
        event=event,
        score=score,
        holders=holders,
        now=NOW,
    )
    links = next(item for item in alert.spec.fields if item.name == "LINKS")
    assert MINT in links.value
    fomo_link = links.value.split("[FOMO](")[1].split(")")[0]
    assert link_matches_mint(fomo_link, MINT) is True
    assert link_matches_mint(fomo_link, "MintOther") is False


def test_the_hot_watch_card_is_quiet_by_construction() -> None:
    """Section 44: a hot watch is not an interruption."""

    entry, _, score, _ = _candidate()
    card = fa.build_trending_hot_watch_card(
        mint=MINT,
        symbol="QUANT",
        name="Quant",
        fomo_url=fomo_coin_url(MINT),
        entry=entry,
        score=score,
        gap=D("6"),
        now=NOW,
    )
    assert card.lane == fa.LANE_RADAR
    assert card.may_ping is False
    assert fa.TRENDING_HOT_WATCH not in fa.PINGABLE


def test_every_trending_card_fits_inside_one_discord_message() -> None:
    """An oversized card is a card the operator never sees."""

    entry, event, score, holders = _candidate()
    risk = build_risk_panel(
        MINT, liquidity_usd=D("90000"), holders=holders, fomo_verified="VERIFIED"
    )
    cards = [
        fa.build_trending_alert(
            mint=MINT,
            name="Quant" * 20,
            symbol="QUANT",
            fomo_url=fomo_coin_url(MINT),
            kind=kind,
            entry=entry,
            event=event,
            score=score,
            holders=holders,
            risk=risk,
            about_summary="A very long description. " * 40,
            project_claim="AI",
            external_verification="UNVERIFIED " * 20,
            story="A long story. " * 60,
            thesis_summary="Theses. " * 60,
            strongest_thesis="The strongest. " * 40,
            social_summary="Mentions. " * 60,
            notable_wallets=4,
            collision_warning="⚠ 4 OTHER TOKENS SHARE THIS NAME/STORY " * 5,
            source_caveat=PROXY.rank_caveat(),
            market_cap_velocity=D("3"),
            now=NOW,
        )
        for kind in (
            fa.TRENDING_ALPHA,
            fa.TRENDING_ACCELERATION_ALERT,
            fa.TRENDING_CONTINUATION_ALERT,
            fa.OFF_TRENDING_EXCEPTION,
        )
    ]
    cards.append(
        fa.build_trending_hot_watch_card(
            mint=MINT,
            symbol="QUANT",
            name="Quant",
            fomo_url=fomo_coin_url(MINT),
            entry=entry,
            score=score,
            gap=D("6"),
            now=NOW,
        )
    )
    for card in cards:
        embeds, _ = render_message([card.spec])
        assert len(embeds) <= MESSAGE_EMBED_LIMIT
        for embed in embeds:
            assert len(embed) <= 6000


def test_trending_classes_ride_the_urgent_lane_except_hot_watch() -> None:
    """Section 59: the primary universe's serious classes earn an interruption."""

    assert fa.TRENDING_ALPHA in fa.URGENT_CLASSES
    assert fa.TRENDING_CONTINUATION_ALERT in fa.URGENT_CLASSES
    assert fa.OFF_TRENDING_EXCEPTION in fa.URGENT_CLASSES
    assert fa.TRENDING_HOT_WATCH not in fa.URGENT_CLASSES
    assert set(fa.PINGABLE) <= set(fa.URGENT_CLASSES)
