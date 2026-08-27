from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from smart_money_bot.callouts import (
    CoinCalloutAnalyzer,
    XRecentSearchClient,
    build_x_narrative_query,
    build_x_query,
    parse_dex_snapshot,
    parse_tracker_risk,
    parse_x_snapshot,
    score_callout,
    should_publish_coin_callout,
    should_publish_coin_watch,
)
from smart_money_bot.models import (
    DexSnapshot,
    SwapQuote,
    TokenInfo,
    TokenRiskSnapshot,
    XSocialSnapshot,
)

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


def test_x_query_uses_exact_contract_instead_of_spoofable_identity() -> None:
    query = build_x_query(MINT, symbol="PATS", name="Patriots")

    assert f'"{MINT}"' in query
    assert "$PATS" not in query
    assert "Patriots" not in query
    assert query.endswith("lang:en")


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
    assert item.contract_authors == 2
    assert item.credible_contract_authors == 2
    assert item.notable_accounts[0].startswith("@knowncrypto")


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


def _executable_quote() -> SwapQuote:
    return SwapQuote(
        input_mint="USDC",
        output_mint=MINT,
        input_amount_raw=5_000_000,
        output_amount_raw=1_000_000,
        other_amount_threshold_raw=990_000,
        input_amount=Decimal("5"),
        output_amount=Decimal("1"),
        input_usd_value=Decimal("5"),
        output_usd_value=Decimal("4.99"),
        price_impact_percent=Decimal("0.2"),
        router="iris",
        fee_bps=0,
        api_time_ms=25,
        observed_latency_ms=30,
        quoted_at=1_800_000_000,
    )


def _complete_tracker_risk() -> TokenRiskSnapshot:
    return TokenRiskSnapshot(
        available=True,
        score=Decimal("2"),
        rugged=False,
        snipers_percent=Decimal("5"),
        insiders_percent=Decimal("2"),
        bundlers_percent=Decimal("3"),
        top10_percent=Decimal("18"),
        dev_percent=Decimal("1"),
    )


def test_public_callout_requires_complete_cross_source_confirmation() -> None:
    report = score_callout(
        mint=MINT,
        token_info=TokenInfo(
            mint=MINT,
            symbol="TEST",
            holder_count=600,
            liquidity_usd=Decimal("100000"),
            top_holders_percent=Decimal("18"),
            dev_balance_percent=Decimal("1"),
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
            crypto_authors=8,
            credible_crypto_authors=5,
            contract_authors=5,
            credible_contract_authors=3,
            trusted_crypto_authors=1,
            promoter_posts=8,
            engagements=500,
            posts_per_minute=Decimal("2"),
        ),
        tracker_risk=_complete_tracker_risk(),
        smart_wallets=("Alpha", "Beta"),
        executable_quote=_executable_quote(),
    )

    assert report.score >= 70
    assert report.verdict == "VERIFIED TREND"
    assert report.public_alert_eligible is True
    assert should_publish_coin_callout(
        report,
        configured_score_floor=Decimal("45"),
    ) is True


def test_name_ticker_buzz_without_exact_contract_promotion_is_not_public() -> None:
    report = score_callout(
        mint=MINT,
        token_info=TokenInfo(
            mint=MINT,
            holder_count=600,
            liquidity_usd=Decimal("100000"),
            top_holders_percent=Decimal("18"),
            dev_balance_percent=Decimal("1"),
            suspicious=False,
            mint_authority_disabled=True,
            freeze_authority_disabled=True,
        ),
        dex=DexSnapshot(
            available=True,
            liquidity_usd=Decimal("100000"),
            buys_5m=50,
            sells_5m=20,
            volume_5m_usd=Decimal("15000"),
        ),
        social=XSocialSnapshot(
            available=True,
            posts=50,
            contract_posts=0,
            unique_authors=20,
            established_authors=10,
            influential_authors=5,
            crypto_authors=10,
            credible_crypto_authors=8,
            promoter_posts=10,
            engagements=5_000,
            posts_per_minute=Decimal("3"),
        ),
        tracker_risk=_complete_tracker_risk(),
        smart_wallets=("Alpha", "Beta"),
        executable_quote=_executable_quote(),
    )

    assert report.public_alert_eligible is False
    assert report.verdict != "VERIFIED TREND"
    assert should_publish_coin_callout(
        report,
        configured_score_floor=Decimal("45"),
    ) is False


def test_large_liquidity_disagreement_blocks_fake_market_claim() -> None:
    report = score_callout(
        mint=MINT,
        token_info=TokenInfo(
            mint=MINT,
            holder_count=600,
            liquidity_usd=Decimal("5000"),
            top_holders_percent=Decimal("18"),
            dev_balance_percent=Decimal("1"),
            suspicious=False,
            mint_authority_disabled=True,
            freeze_authority_disabled=True,
        ),
        dex=DexSnapshot(
            available=True,
            liquidity_usd=Decimal("500000"),
            buys_5m=50,
            sells_5m=20,
            volume_5m_usd=Decimal("15000"),
        ),
        social=XSocialSnapshot(
            available=True,
            posts=10,
            contract_posts=10,
            unique_authors=8,
            crypto_authors=8,
            credible_crypto_authors=5,
            contract_authors=5,
            credible_contract_authors=3,
            promoter_posts=8,
            trusted_crypto_authors=1,
        ),
        tracker_risk=_complete_tracker_risk(),
        smart_wallets=("Alpha", "Beta"),
        executable_quote=_executable_quote(),
    )

    assert report.verdict == "BLOCKED"
    assert report.public_alert_eligible is False
    assert any("disagree" in item for item in report.hard_blockers)


async def test_x_daily_budget_blocks_before_network_request() -> None:
    calls = 0

    async def reject_budget() -> bool:
        nonlocal calls
        calls += 1
        return False

    client = XRecentSearchClient("x-token", budget_reserver=reject_budget)
    try:
        result = await client.snapshot(MINT)
    finally:
        await client.close()

    assert calls == 1
    assert result.available is False
    assert result.error == "daily X search budget exhausted"
    assert client.requests_attempted == 0
    assert client.budget_rejections == 1


async def test_free_prefilter_rejects_hard_risk_without_spending_x() -> None:
    class FakeDex:
        async def snapshot(self, mint: str) -> DexSnapshot:
            del mint
            return DexSnapshot(
                available=True,
                liquidity_usd=Decimal("50000"),
                buys_5m=10,
                sells_5m=2,
                volume_5m_usd=Decimal("5000"),
            )

    class FakeSocial:
        calls = 0

        async def snapshot(self, mint: str, **kwargs) -> XSocialSnapshot:
            del mint, kwargs
            self.calls += 1
            return XSocialSnapshot(available=True, posts=10)

    class FakeTracker:
        async def snapshot(self, mint: str) -> TokenRiskSnapshot:
            del mint
            return _complete_tracker_risk()

    social = FakeSocial()
    analyzer = CoinCalloutAnalyzer(FakeDex(), social, FakeTracker())
    report = await analyzer.analyze(
        mint=MINT,
        token_info=TokenInfo(
            mint=MINT,
            holder_count=100,
            liquidity_usd=Decimal("50000"),
            top_holders_percent=Decimal("10"),
            dev_balance_percent=Decimal("1"),
            suspicious=True,
            mint_authority_disabled=True,
            freeze_authority_disabled=True,
        ),
        smart_wallets=("Alpha",),
    )

    assert social.calls == 0
    assert report.scan_stage == "FREE_REJECTED"
    assert report.x_search_attempted is False

    forced = await analyzer.analyze(
        mint=MINT,
        token_info=report.token_info,
        smart_wallets=("Alpha",),
        force_x_search=True,
    )
    assert social.calls == 1
    assert forced.scan_stage == "X_CHECKED"
    assert forced.x_search_attempted is True


def test_developing_watch_requires_real_exact_contract_x_activity() -> None:
    report = score_callout(
        mint=MINT,
        token_info=TokenInfo(
            mint=MINT,
            holder_count=100,
            liquidity_usd=Decimal("50000"),
            top_holders_percent=Decimal("10"),
            dev_balance_percent=Decimal("1"),
            suspicious=False,
            mint_authority_disabled=True,
            freeze_authority_disabled=True,
        ),
        dex=DexSnapshot(
            available=True,
            liquidity_usd=Decimal("50000"),
            buys_5m=20,
            sells_5m=5,
            volume_5m_usd=Decimal("5000"),
        ),
        social=XSocialSnapshot(
            available=True,
            posts=3,
            contract_posts=3,
            unique_authors=2,
            crypto_authors=2,
            contract_authors=2,
            credible_contract_authors=1,
            promoter_posts=2,
            posts_per_minute=Decimal("0.2"),
        ),
        tracker_risk=_complete_tracker_risk(),
        smart_wallets=("Alpha",),
        executable_quote=_executable_quote(),
    )
    report = replace(
        report,
        x_search_attempted=True,
        scan_stage="X_CHECKED",
    )

    assert should_publish_coin_watch(
        report,
        configured_score_floor=Decimal("50"),
    ) is True
