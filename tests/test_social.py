from dataclasses import replace
from decimal import Decimal

from smart_money_bot.bot import _discovery_lines
from smart_money_bot.models import DiscoveryCandidate
from smart_money_bot.social import (
    SocialNomination,
    annotate_social_nominations,
    parse_pump_profile_index,
    parse_pump_profile_wallet,
    wallet_from_profile_slug,
)

WALLET = "HkFGQsW8mr8DTC2AE2WcC7MzwSnynfEryGMQSht271nf"
OTHER = "ApAKzJEqfnP7F74Za5xdTQxZMK4nD8dFTVBQ9bksTtGM"


def candidate(address: str = WALLET) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        address=address,
        alias="Verified trader",
        realized_pnl_24h=Decimal("500"),
        previous_pnl_24h=None,
        roi_24h_percent=Decimal("20"),
        win_rate_percent=Decimal("70"),
        trades_24h=20,
        buys_24h=10,
        sells_24h=10,
        closed_tokens=8,
        invested_24h_usd=Decimal("2000"),
        volume_24h_usd=Decimal("5000"),
        last_trade_ms=1_700_000_000_000,
        score=Decimal("80"),
        rank=1,
        realized_pnl_7d=Decimal("2500"),
        roi_7d_percent=Decimal("35"),
        win_rate_7d_percent=Decimal("68"),
        trades_7d=100,
        selection_reason="general evidence; strict 24H + 7D profit verified",
    )


def test_parse_official_profile_index_extracts_public_profiles() -> None:
    raw = f"""
    <a href="/profile/{WALLET}"><span>Alpha</span><span>12,345 followers</span></a>
    <a href="/profile/public-name"><span>Beta</span><span>999 followers</span></a>
    """

    parsed = parse_pump_profile_index(raw)

    assert [(item.slug, item.followers) for item in parsed] == [
        (WALLET, 12_345),
        ("public-name", 999),
    ]


def test_profile_wallet_resolution_requires_public_wallet_context() -> None:
    assert wallet_from_profile_slug(WALLET) == WALLET
    assert wallet_from_profile_slug("public-name") is None
    assert (
        parse_pump_profile_wallet(
            f'<a href="https://solscan.io/account/{OTHER}">wallet</a>'
        )
        == OTHER
    )
    assert parse_pump_profile_wallet("no public wallet here") is None


def test_social_nomination_cannot_create_or_rescore_candidate() -> None:
    verified = candidate()
    nominations = [
        SocialNomination(WALLET, "Popular", 50_000),
        SocialNomination(OTHER, "Unverified", 1_000_000),
    ]

    annotated, matched = annotate_social_nominations([verified], nominations)

    assert matched == 1
    assert len(annotated) == 1
    assert annotated[0].address == WALLET
    assert annotated[0].score == verified.score
    assert "50,000 followers" in annotated[0].selection_reason
    assert OTHER not in {item.address for item in annotated}


def test_incomplete_nomination_is_not_rendered_as_unavailable_metrics() -> None:
    text = _discovery_lines([replace(candidate(), metrics_limited_24h=True)])

    assert "unavailable" not in text
    assert text == "No qualified wallets in the latest snapshot."
