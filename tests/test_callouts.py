from datetime import UTC, datetime, timedelta
from decimal import Decimal

from smart_money_bot.callouts import (
    build_x_narrative_query,
    build_x_query,
    parse_dex_snapshot,
    parse_tracker_risk,
    parse_x_snapshot,
    score_callout,
)
from smart_money_bot.models import DexSnapshot, TokenInfo, XSocialSnapshot

MINT = "HkFGQsW8mr8DTC2AE2WcC7MzwSnynfEryGMQSht271nf"


def test_dex_parser_chooses_highest_liquidity_sol_pair() -> None:
    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "baseToken": {"address": MINT},
                "liquidity": {"usd": 5_000},
                "txns": {"m5": {"buys": 3, "sells": 2}},
            },
            {
                "chainId": "solana",
                "baseToken": {"address": MINT},
                "liquidity": {"usd": 50_000},
                "marketCap": 100_000,
                "txns": {"m5": {"buys": 25, "sells": 10}},
                "volume": {"m5": 12_000},
                "priceChange": {"m5": 8},
                "info": {
                    "websites": [{"url": "https://example.test"}],
                    "socials": [{"type": "twitter", "url": "https://x.com/example"}],
                },
                "boosts": {"active": 2},
            },
        ]
    }

    item = parse_dex_snapshot(payload, mint=MINT)

    assert item.available
    assert item.liquidity_usd == Decimal("50000")
    assert item.buys_5m == 25
    assert item.has_x_profile
    assert item.active_boosts == 2


def test_x_parser_counts_unique_quality_and_duplicate_authors() -> None:
    old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    recent = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    payload = {
        "data": [
            {
                "author_id": "1",
                "created_at": recent,
                "text": f"watch {MINT}",
                "public_metrics": {"like_count": 20, "retweet_count": 5},
            },
            {
                "author_id": "2",
                "created_at": recent,
                "text": f"watch {MINT}",
                "public_metrics": {"like_count": 5},
            },
        ],
        "includes": {
            "users": [
                {
                    "id": "1",
                    "created_at": old,
                    "verified": True,
                    "public_metrics": {"followers_count": 10_000, "following_count": 100},
                },
                {
                    "id": "2",
                    "created_at": old,
                    "public_metrics": {"followers_count": 500, "following_count": 100},
                },
            ]
        },
    }

    item = parse_x_snapshot(payload, query=MINT, contract=MINT)

    assert item.available
    assert item.unique_authors == 2
    assert item.established_authors == 2
    assert item.influential_authors == 1
    assert item.duplicate_percent == Decimal("50.00")
    assert item.contract_posts == 2
    assert item.identity_posts == 0


def test_x_query_adds_verified_identity_without_dropping_contract() -> None:
    query = build_x_query(MINT, symbol="PATS", name="Patriots")

    assert f'"{MINT}"' in query
    assert '"$PATS"' in query
    assert '"Patriots"' in query
    assert query.endswith("-is:retweet")


def test_narrative_query_requires_explicit_crypto_language() -> None:
    query = build_x_narrative_query("Project Kitchen")

    assert '"Project Kitchen"' in query
    assert "memecoin" in query
    assert '"contract address"' in query
    assert "lang:en" in query


def test_x_parser_identifies_credible_crypto_promoters() -> None:
    old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    recent = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    payload = {
        "data": [
            {
                "author_id": "1",
                "created_at": recent,
                "text": f"Just launched $KITCHEN on pump.fun CA: {MINT}",
                "public_metrics": {"like_count": 100},
            },
            {
                "author_id": "2",
                "created_at": recent,
                "text": f"Ape in carefully — contract address {MINT}",
                "public_metrics": {"like_count": 50},
            },
        ],
        "includes": {
            "users": [
                {
                    "id": "1",
                    "username": "knowncrypto",
                    "description": "Solana memecoin trader",
                    "created_at": old,
                    "public_metrics": {"followers_count": 5_000, "following_count": 100},
                },
                {
                    "id": "2",
                    "username": "secondcrypto",
                    "description": "Crypto and onchain research",
                    "created_at": old,
                    "public_metrics": {"followers_count": 1_000, "following_count": 100},
                },
            ]
        },
    }

    item = parse_x_snapshot(payload, query=MINT, contract=MINT)

    assert item.crypto_authors == 2
    assert item.credible_crypto_authors == 2
    assert item.coin_intent_posts == 2
    assert item.promoter_posts == 2
    assert item.contract_posts == 2


def test_tracker_risk_parser_preserves_launch_manipulation_evidence() -> None:
    item = parse_tracker_risk(
        {
            "risk": {
                "score": 8,
                "rugged": False,
                "bundlers": {"totalPercentage": 25},
                "insiders": {"totalPercentage": 4},
                "snipers": {"totalPercentage": 12},
                "risks": [
                    {"name": "Bundler concentration", "level": "danger"},
                    {"name": "Informational", "level": "warn"},
                ],
            }
        }
    )

    assert item.available
    assert item.score == Decimal("8")
    assert item.bundlers_percent == Decimal("25")
    assert item.danger_flags == ("Bundler concentration",)


def test_callout_never_overrides_hard_token_risk() -> None:
    report = score_callout(
        mint=MINT,
        token_info=TokenInfo(
            mint=MINT,
            holder_count=1_000,
            liquidity_usd=Decimal("500000"),
            top_holders_percent=Decimal("10"),
            suspicious=True,
            mint_authority_disabled=False,
            freeze_authority_disabled=True,
        ),
        dex=DexSnapshot(
            available=True,
            liquidity_usd=Decimal("500000"),
            buys_5m=100,
            sells_5m=10,
            volume_5m_usd=Decimal("50000"),
            has_website=True,
            has_x_profile=True,
        ),
        social=XSocialSnapshot(
            available=True,
            posts=100,
            contract_posts=100,
            unique_authors=50,
            established_authors=20,
            influential_authors=10,
            engagements=5_000,
            posts_per_minute=Decimal("5"),
        ),
        smart_wallets=("Alpha", "Beta", "Gamma"),
    )

    assert report.verdict == "BLOCKED"
    assert report.hard_blockers


def test_strong_watch_requires_cross_source_confirmation() -> None:
    report = score_callout(
        mint=MINT,
        token_info=TokenInfo(
            mint=MINT,
            symbol="TEST",
            holder_count=600,
            liquidity_usd=Decimal("100000"),
            top_holders_percent=Decimal("18"),
            suspicious=False,
            verified=True,
            mint_authority_disabled=True,
            freeze_authority_disabled=True,
        ),
        dex=DexSnapshot(
            available=True,
            liquidity_usd=Decimal("100000"),
            buys_5m=50,
            sells_5m=20,
            volume_5m_usd=Decimal("15000"),
            has_website=True,
            has_x_profile=True,
        ),
        social=XSocialSnapshot(
            available=True,
            posts=40,
            contract_posts=40,
            unique_authors=20,
            established_authors=8,
            influential_authors=3,
            engagements=500,
            posts_per_minute=Decimal("2"),
        ),
        smart_wallets=("Alpha", "Beta"),
    )

    assert report.score >= 70
    assert report.verdict == "STRONG WATCH"
