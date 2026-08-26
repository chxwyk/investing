from datetime import UTC, datetime

from smart_money_bot.news import (
    extract_narrative_terms,
    extract_solana_mints,
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
