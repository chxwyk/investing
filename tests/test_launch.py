from __future__ import annotations

import io
from dataclasses import replace
from decimal import Decimal

from PIL import Image

from smart_money_bot.launch import (
    J7_REGION_BASES,
    NO_X_LAUNCH_VERDICT,
    X_VERIFIED_LAUNCH_VERDICT,
    build_j7_launch_payload,
    is_manual_launch_opportunity,
    is_safe_launch_image_url,
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


def _strong_free_story(now: int) -> NewsAlert:
    return NewsAlert(
        source="CoinDesk",
        headline=(
            'BREAKING: Solana traders launch viral "Kitchen Moment" meme after record rally'
        ),
        summary=(
            "Crypto traders say the Kitchen Moment is spreading across Solana communities."
        ),
        url="https://coindesk.com/markets/kitchen-moment",
        narrative_terms=("Kitchen Moment", "Solana"),
        created_at=now - 20,
        received_at=now,
    )


def test_x_disabled_weak_story_is_not_a_launch_candidate() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="CoinDesk",
        headline="Solana market commentary mentions a possible kitchen theme",
        summary="Analysts discussed the market during a routine update.",
        url="https://coindesk.com/markets/commentary",
        narrative_terms=("Kitchen",),
        created_at=now - 600,
        received_at=now,
    )

    result = score_launch_opportunity(
        alert,
        competition=NarrativeCompetition(query="Kitchen"),
        cross_source_count=0,
        now=now,
        no_x_candidates_enabled=True,
        no_x_launch_min_score=78,
    )

    assert result.verdict != NO_X_LAUNCH_VERDICT
    assert result.no_x_candidate_ready is False
    assert should_publish_news_opportunity(result) is False


def test_x_disabled_strong_multi_source_story_becomes_no_x_candidate() -> None:
    now = 1_800_000_000

    result = score_launch_opportunity(
        _strong_free_story(now),
        competition=NarrativeCompetition(query="Kitchen Moment"),
        cross_source_count=2,
        now=now,
        no_x_candidates_enabled=True,
        no_x_launch_min_score=78,
    )

    assert result.score >= 78
    assert result.verdict == NO_X_LAUNCH_VERDICT
    assert result.no_x_candidate_ready is True
    assert result.x_verified is False
    assert result.x_evidence.available is False
    assert "X/social velocity was not verified." in result.warnings
    assert should_publish_news_opportunity(result) is True
    assert is_manual_launch_opportunity(result) is True


def test_no_x_general_news_requires_two_additional_confirmations() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="Reuters",
        headline=(
            'BREAKING: President signs unexpected "Kitchen Act" after viral campaign'
        ),
        summary="The surprise announcement is spreading rapidly across U.S. markets.",
        url="https://reuters.com/world/us/kitchen-act",
        narrative_terms=("Kitchen Act", "President"),
        created_at=now - 20,
        received_at=now,
    )

    one_confirmation = score_launch_opportunity(
        alert,
        competition=NarrativeCompetition(query="Kitchen Act"),
        cross_source_count=1,
        now=now,
    )
    two_confirmations = score_launch_opportunity(
        alert,
        competition=NarrativeCompetition(query="Kitchen Act"),
        cross_source_count=2,
        now=now,
    )

    assert one_confirmation.no_x_candidate_ready is False
    assert two_confirmations.verdict == NO_X_LAUNCH_VERDICT
    assert two_confirmations.lane == "MAJOR U.S. BREAKING"


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
    assert strong.verdict == X_VERIFIED_LAUNCH_VERDICT
    assert strong.lane == "CRYPTO TREND"
    assert strong.x_verified is True
    assert strong.no_x_candidate_ready is False
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
    assert result.no_x_candidate_ready is False
    assert is_manual_launch_opportunity(result) is False
    assert should_publish_news_opportunity(result) is False


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


def test_source_photo_becomes_square_labeled_launch_art() -> None:
    now = 1_800_000_000
    alert = NewsAlert(
        source="CoinDesk",
        headline="BREAKING: Kitchen Coin catches crypto attention",
        summary="The meme is spreading.",
        url="https://coindesk.com/story/kitchen",
        narrative_terms=("Kitchen Coin",),
        created_at=now - 10,
        received_at=now,
    )
    opportunity = score_launch_opportunity(
        alert,
        x_evidence=_strong_x(),
        competition=NarrativeCompetition(query="Kitchen Coin"),
        cross_source_count=2,
        now=now,
    )
    source = Image.new("RGB", (1600, 900), (210, 90, 40))
    raw = io.BytesIO()
    source.save(raw, format="JPEG")

    rendered = render_opportunity_image(opportunity, raw.getvalue())

    with Image.open(io.BytesIO(rendered)) as result:
        assert result.size == (1024, 1024)
        assert result.format == "PNG"
    assert rendered != render_opportunity_image(opportunity)


def test_launch_image_url_rejects_local_or_credentialed_hosts() -> None:
    assert is_safe_launch_image_url("https://cdn.example.com/lead.jpg") is True
    assert is_safe_launch_image_url("http://cdn.example.com/lead.jpg") is False
    assert is_safe_launch_image_url("https://127.0.0.1/secret") is False
    assert is_safe_launch_image_url("https://user:pass@example.com/photo") is False


def test_j7_payload_uses_encrypted_key_and_generated_image(settings) -> None:
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
    opportunity = score_launch_opportunity(
        alert,
        x_evidence=_strong_x(),
        competition=NarrativeCompetition(query="Kitchen Coin"),
        cross_source_count=2,
        now=now,
    )
    configured = replace(
        settings,
        j7_launch_api_key="encrypted-solana-wallet-key",
        j7_launch_session_token="secret-session-jwt",
    )

    payload = build_j7_launch_payload(
        opportunity,
        configured,
        image_url="https://ipfs.io/ipfs/example",
    )

    assert payload["external"] is True
    assert payload["type"] == "create_token"
    assert payload["mode"] == "pump"
    assert payload["api_key"] == "encrypted-solana-wallet-key"
    assert payload["image_url"] == "https://ipfs.io/ipfs/example"
    assert payload["website"] == alert.url
    assert "session_id" not in payload
    assert "private_key" not in payload
    assert J7_REGION_BASES["na-east"].startswith("https://")
