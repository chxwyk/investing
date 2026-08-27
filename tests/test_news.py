from datetime import UTC, datetime

from smart_money_bot.models import NewsAlert
from smart_money_bot.news import (
    extract_narrative_terms,
    extract_solana_mints,
    is_coin_actionable_news,
    parse_feed,
    parse_x_news_payload,
)

MINT = "HkFGQsW8mr8DTC2AE2WcC7MzwSnynfEryGMQSht271nf"


def test_extracts_valid_solana_contracts_only() -> None:
    assert extract_solana_mints(f"CA: {MINT} and not-a-contract") == (MINT,)


def test_extracts_specific_narratives_not_generic_breaking_label() -> None:
    terms = extract_narrative_terms('BREAKING: Elon mentioned "Kitchen Alpha" $KITCHEN')

    assert "BREAKING" not in terms
    assert "Kitchen Alpha" in terms
    assert "KITCHEN" in terms


def test_extracts_named_news_narratives_without_all_caps() -> None:
    terms = extract_narrative_terms("Trump announces a new federal initiative")

    assert "Trump" in terms


def test_technical_crypto_publisher_story_is_not_coin_actionable() -> None:
    alert = NewsAlert(
        source="X filtered stream",
        headline=(
            "Chainalysis estimates global taxable onchain crypto activity reached at least "
            "$457 billion, while the OECD's CARF reporting framework covers only 14%"
        ),
        summary="",
        url="https://x.com/Cointelegraph/status/1",
        author="@Cointelegraph",
        author_followers=2_936_642,
        author_verified=True,
        score=53,
        urgency="MEDIUM",
        narrative_terms=("OECD", "CARF"),
    )

    assert is_coin_actionable_news(alert) is False


def test_named_breaking_event_is_coin_actionable_before_contract_exists() -> None:
    alert = NewsAlert(
        source="X filtered stream",
        headline="BREAKING: Trump announces Project Kitchen",
        summary="",
        url="https://x.com/newsdesk/status/2",
        author="@newsdesk",
        score=55,
        urgency="HIGH",
        narrative_terms=("Trump", "Kitchen"),
    )

    assert is_coin_actionable_news(alert) is True


def test_routine_sports_and_foreign_business_items_are_not_actionable() -> None:
    sports = NewsAlert(
        source="ESPN",
        headline="Predictions for every major NBA award",
        summary="Analysts publish their preseason picks.",
        url="https://espn.com/nba/predictions",
        narrative_terms=("NBA", "Predictions"),
    )
    business = NewsAlert(
        source="Reuters",
        headline="Advisers launch Venezuela commercial creditor committee",
        summary="The committee will organize claims.",
        url="https://reuters.com/world/venezuela-creditors",
        narrative_terms=("Venezuela", "Advisers"),
    )

    assert is_coin_actionable_news(sports) is False
    assert is_coin_actionable_news(business) is False


def test_x_stream_payload_becomes_fast_news_alert() -> None:
    now = datetime.now(UTC).isoformat()
    alert = parse_x_news_payload(
        {
            "data": {
                "id": "123",
                "author_id": "9",
                "created_at": now,
                "text": f'BREAKING: "KITCHEN" CA {MINT}',
            },
            "includes": {
                "users": [
                    {
                        "id": "9",
                        "username": "newsdesk",
                        "verified": True,
                        "public_metrics": {"followers_count": 1_000_000},
                    }
                ]
            },
            "matching_rules": [{"tag": "smart-money-news-v220"}],
        }
    )

    assert alert is not None
    assert alert.author == "@newsdesk"
    assert alert.token_mints == (MINT,)
    assert alert.urgency == "HIGH"
    assert alert.url == "https://x.com/newsdesk/status/123"


def test_rss_parser_creates_narrative_alert() -> None:
    feed = """
    <rss><channel><title>Official News</title><item>
      <title>Agency announces PROJECT ALPHA</title>
      <description>A major launch is confirmed.</description>
      <link>https://example.test/story</link>
      <pubDate>Tue, 26 Aug 2026 10:00:00 +0000</pubDate>
    </item></channel></rss>
    """

    alerts = parse_feed(feed, source_url="https://example.test/feed")

    assert len(alerts) == 1
    assert alerts[0].source == "Official News"
    assert alerts[0].url == "https://example.test/story"
    assert "ALPHA" in alerts[0].narrative_terms
    assert alerts[0].score >= 20
