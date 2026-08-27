from __future__ import annotations

from decimal import Decimal

from smart_money_bot.launch import (
    render_opportunity_image,
    score_launch_opportunity,
    should_publish_news_opportunity,
)
from smart_money_bot.models import (
    NarrativeCompetition,
    NewsAlert,
    XSocialSnapshot,
)


def _strong_x() -> XSocialSnapshot:
    return XSocialSnapshot(
        available=True,
        posts=10,
        unique_authors=8,
        established_authors=4,
        influential_authors=3,
        crypto_authors=5,
        credible_crypto_authors=4,
        coin_intent_posts=5,
        promoter_posts=4,
        engagements=2_500,
        posts_per_minute=Decimal("1.5"),
        duplicate_percent=Decimal("10"),
        query='"Sprite Chill" -is:retweet',
    )


def test_routine_non_crypto_product_event_cannot_be_launch_ready() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="Reuters",
        headline='Sprite unveils unexpected "Sprite Chill" flavor after viral fan campaign',
        summary="The limited release starts today.",
        url="https://reuters.com/world/sprite-chill",
        narrative_terms=("Sprite Chill", "Sprite"),
        created_at=now - 45,
        received_at=now,
    )

    result = score_launch_opportunity(
        alert,
        x_evidence=_strong_x(),
        competition=NarrativeCompetition(query="Sprite Chill"),
        cross_source_count=2,
        now=now,
    )

    assert result.category == "BRAND / PRODUCT"
    assert result.verdict == "SKIP"
    assert result.score < 45
    assert result.coin_name == "Sprite Chill"
    assert result.coin_symbol == "SC"


def test_major_sports_event_is_not_rejected_for_being_non_crypto() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="ESPN",
        headline='BREAKING: rookie wins championship with "Miracle Shot" at the buzzer',
        summary="The upset set a league record and is already viral.",
        url="https://espn.com/story/miracle-shot",
        narrative_terms=("Miracle Shot",),
        created_at=now - 20,
        received_at=now,
    )

    result = score_launch_opportunity(
        alert,
        x_evidence=_strong_x(),
        competition=NarrativeCompetition(query="Miracle Shot"),
        cross_source_count=2,
        now=now,
    )

    assert result.category == "SPORTS"
    assert result.verdict == "LAUNCH READY"
    assert result.lane == "MAJOR U.S. BREAKING"


def test_crypto_native_narrative_requires_crypto_promotion() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="CoinDesk",
        headline='Solana traders rally around viral "Kitchen Coin" meme',
        summary="Crypto accounts are discussing a possible community token.",
        url="https://coindesk.com/markets/kitchen-coin",
        narrative_terms=("Kitchen Coin",),
        created_at=now - 30,
        received_at=now,
    )

    weak = score_launch_opportunity(
        alert,
        x_evidence=XSocialSnapshot(
            available=True,
            posts=10,
            unique_authors=10,
            established_authors=8,
            influential_authors=6,
            engagements=3_000,
            posts_per_minute=Decimal("2"),
        ),
        competition=NarrativeCompetition(query="Kitchen Coin"),
        now=now,
    )
    strong = score_launch_opportunity(
        alert,
        x_evidence=_strong_x(),
        competition=NarrativeCompetition(query="Kitchen Coin"),
        now=now,
    )

    assert weak.verdict != "LAUNCH READY"
    assert strong.verdict == "LAUNCH READY"
    assert strong.lane == "CRYPTO TREND"
    assert should_publish_news_opportunity(weak) is False
    assert should_publish_news_opportunity(strong) is True


def test_generic_x_authors_cannot_turn_foreign_business_news_into_launch() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="Reuters",
        headline="Advisers launch committee to organize Venezuela commercial creditors",
        summary="A routine creditor committee was announced.",
        url="https://reuters.com/world/americas/venezuela-creditors",
        narrative_terms=("Advisers", "Venezuela"),
        created_at=now - 5,
        received_at=now,
    )
    generic_x = XSocialSnapshot(
        available=True,
        posts=10,
        unique_authors=10,
        established_authors=7,
        influential_authors=7,
        engagements=36,
        posts_per_minute=Decimal("0.27"),
    )

    result = score_launch_opportunity(
        alert,
        x_evidence=generic_x,
        competition=NarrativeCompetition(query="Advisers"),
        now=now,
    )

    assert result.verdict == "SKIP"
    assert result.score < 45
    assert result.lane == "GENERAL NEWS — NOT ELIGIBLE"
    assert should_publish_news_opportunity(result) is False


def test_existing_contract_requires_repeated_crypto_account_promotion() -> None:
    now = 1_800_000_000
    mint = "HkFGQsW8mr8DTC2AE2WcC7MzwSnynfEryGMQSht271nf"
    alert = NewsAlert(
        source="X filtered stream",
        headline=f"New Solana memecoin CA: {mint}",
        summary="Community launch.",
        url="https://x.com/crypto/status/1",
        author="@crypto",
        narrative_terms=("Community",),
        token_mints=(mint,),
        created_at=now - 20,
        received_at=now,
    )
    promoted = XSocialSnapshot(
        available=True,
        posts=5,
        contract_posts=4,
        unique_authors=4,
        established_authors=3,
        crypto_authors=3,
        credible_crypto_authors=2,
        contract_authors=3,
        credible_contract_authors=2,
        coin_intent_posts=4,
        promoter_posts=3,
        engagements=150,
        posts_per_minute=Decimal("1"),
    )

    result = score_launch_opportunity(
        alert,
        x_evidence=promoted,
        competition=NarrativeCompetition(query="Community"),
        now=now,
    )

    assert result.verdict == "COIN FOUND"
    assert result.crypto_attention_ready is True


def test_dry_reporting_story_stays_below_watch_threshold() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="X filtered stream",
        headline=(
            "Chainalysis estimates taxable activity while the OECD CARF reporting "
            "framework covers a smaller share"
        ),
        summary="A technical compliance framework methodology update.",
        url="https://x.com/publisher/status/1",
        author="@publisher",
        author_followers=2_000_000,
        author_verified=True,
        narrative_terms=("OECD", "CARF"),
        created_at=now - 60,
        received_at=now,
    )

    result = score_launch_opportunity(
        alert,
        competition=NarrativeCompetition(query="OECD", error="not checked"),
        now=now,
    )

    assert result.verdict == "SKIP"
    assert result.score < 45


def test_unconfirmed_claim_cannot_be_launch_ready() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="Reuters",
        headline='Unconfirmed rumor says celebrity will launch "Moon Shoes" today',
        summary="Sources say the product might be announced.",
        url="https://reuters.com/story/moon-shoes",
        narrative_terms=("Moon Shoes",),
        created_at=now - 20,
        received_at=now,
    )

    result = score_launch_opportunity(
        alert,
        x_evidence=_strong_x(),
        competition=NarrativeCompetition(query="Moon Shoes"),
        cross_source_count=2,
        now=now,
    )

    assert result.verdict != "LAUNCH READY"
    assert any("rumor" in item for item in result.blockers)


def test_launch_card_is_a_real_png() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="Reuters",
        headline='Sprite launches "Sprite Chill"',
        summary="Unexpected product release.",
        url="https://reuters.com/story/sprite",
        narrative_terms=("Sprite Chill",),
        created_at=now - 10,
        received_at=now,
    )
    opportunity = score_launch_opportunity(
        alert,
        x_evidence=_strong_x(),
        competition=NarrativeCompetition(query="Sprite Chill"),
        cross_source_count=2,
        now=now,
    )

    image = render_opportunity_image(opportunity)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 1_000
