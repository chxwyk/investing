from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import math
import re
import textwrap
import time
from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from solders.pubkey import Pubkey

from .config import Settings
from .errors import PumpLaunchError, UnknownLaunchResultError
from .market import load_keypair, sign_versioned_transaction
from .models import (
    LaunchDraft,
    LaunchOpportunity,
    NarrativeCompetition,
    NewsAlert,
    PumpLaunchResult,
    XSocialSnapshot,
)
from .rpc import SolanaRPC

NO_X_LAUNCH_VERDICT = "LAUNCH CANDIDATE — NO X VERIFIED"
X_VERIFIED_LAUNCH_VERDICT = "LAUNCH READY"
MANUAL_LAUNCH_VERDICTS = frozenset(
    {NO_X_LAUNCH_VERDICT, X_VERIFIED_LAUNCH_VERDICT}
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "POLITICS": (
        "president",
        "white house",
        "congress",
        "senate",
        "election",
        "trump",
        "governor",
        "minister",
        "policy",
        "tariff",
    ),
    "SPORTS": (
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "fifa",
        "ufc",
        "game",
        "match",
        "champion",
        "record",
        "trade",
        "touchdown",
        "goal",
    ),
    "CELEBRITY": (
        "celebrity",
        "actor",
        "actress",
        "rapper",
        "singer",
        "artist",
        "album",
        "movie",
        "drake",
        "taylor swift",
        "mrbeast",
    ),
    "BRAND / PRODUCT": (
        "product",
        "brand",
        "drink",
        "sprite",
        "coca-cola",
        "pepsi",
        "nike",
        "apple",
        "tesla",
        "disney",
        "netflix",
        "launches",
        "releases",
        "unveils",
        "recall",
    ),
    "INTERNET / MEME": (
        "meme",
        "viral",
        "trend",
        "tiktok",
        "streamer",
        "creator",
        "internet",
        "challenge",
        "mascot",
        "animal",
        "dog",
        "cat",
    ),
    "GAMING / TECH": (
        "game",
        "gaming",
        "fortnite",
        "roblox",
        "playstation",
        "xbox",
        "nintendo",
        "openai",
        "ai",
        "robot",
        "space",
        "nasa",
        "spacex",
    ),
    "CRYPTO": (
        "bitcoin",
        "ethereum",
        "solana",
        "crypto",
        "token",
        "coin",
        "pump.fun",
        "listing",
        "wallet",
    ),
    "WORLD EVENT": (
        "breaking",
        "emergency",
        "explosion",
        "storm",
        "earthquake",
        "war",
        "ceasefire",
        "arrest",
        "resigns",
        "dies",
        "died",
        "death",
    ),
}

EVENT_ACTION_WORDS = {
    "announce",
    "announces",
    "arrest",
    "arrested",
    "ban",
    "banned",
    "breaking",
    "cancel",
    "canceled",
    "challenge",
    "collapse",
    "crash",
    "dies",
    "died",
    "divorce",
    "emergency",
    "feud",
    "fight",
    "fired",
    "fires",
    "hired",
    "launch",
    "launched",
    "marries",
    "pregnant",
    "recall",
    "record",
    "release",
    "released",
    "resign",
    "resigned",
    "suspended",
    "sues",
    "sued",
    "scores",
    "breaks",
    "shatters",
    "beats",
    "signs",
    "trade",
    "traded",
    "unveil",
    "unveiled",
    "upset",
    "viral",
    "boycott",
    "trend",
    "win",
    "wins",
}

DRY_TECHNICAL_PHRASES = {
    "accounting standard",
    "compliance framework",
    "reporting framework",
    "tax reporting",
    "taxable activity",
    "quarterly filing",
    "methodology update",
}

RUMOR_WORDS = {"allegedly", "rumor", "rumour", "unconfirmed", "sources say", "might"}

KNOWN_NEWS_DOMAINS = {
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "coindesk.com",
    "cointelegraph.com",
    "espn.com",
    "reuters.com",
    "sec.gov",
    "whitehouse.gov",
}

CRYPTO_NATIVE_TERMS = {
    "bitcoin",
    "blockchain",
    "crypto",
    "ethereum",
    "memecoin",
    "meme coin",
    "onchain",
    "pump.fun",
    "solana",
    "token",
    "wallet",
}

CRYPTO_SOURCE_ACCOUNTS = {
    "@arkhamintel",
    "@coindesk",
    "@cointelegraph",
    "@lookonchain",
    "@pumpdotfun",
    "@solana",
    "@watcherguru",
}

US_SOURCE_ACCOUNTS = {
    "@ap",
    "@elonmusk",
    "@realdonaldtrump",
    "@whitehouse",
}

US_RELEVANCE_TERMS = {
    "america",
    "american",
    "congress",
    "federal",
    "nasa",
    "nba",
    "nfl",
    "president",
    "sec",
    "supreme court",
    "trump",
    "u.s.",
    "united states",
    "white house",
}

EXCEPTIONAL_EVENT_PHRASES = {
    "arrested",
    "assassination",
    "attack",
    "banned",
    "ceasefire",
    "championship",
    "declares emergency",
    "died",
    "dies",
    "emergency",
    "explosion",
    "indicted",
    "resigned",
    "resigns",
    "shutdown",
    "super bowl",
    "supreme court",
    "world series",
}

IDENTITY_STOP = {
    "agency",
    "breaking",
    "exclusive",
    "live",
    "news",
    "official",
    "project",
    "report",
    "reports",
    "says",
    "update",
    "world",
}


def alert_key(alert: NewsAlert) -> str:
    stable = alert.url or f"{alert.source}\n{alert.headline}\n{alert.created_at}"
    return hashlib.sha256(stable.encode("utf-8", errors="ignore")).hexdigest()


def launch_cluster_key(opportunity: LaunchOpportunity) -> str:
    """Stable narrative key used to collapse same-story articles and launch retries."""

    normalized = re.sub(
        r"[^a-z0-9]+", " ", opportunity.primary_narrative.casefold()
    ).strip()
    stable = normalized or opportunity.alert.headline.casefold()
    return hashlib.sha256(stable.encode("utf-8", errors="ignore")).hexdigest()


def launch_draft_key(draft: LaunchDraft) -> str:
    return launch_cluster_key(draft.opportunity)


def classify_event(text: str) -> str:
    lowered = text.casefold()
    scores = {
        category: sum(1 for item in keywords if _keyword_present(lowered, item))
        for category, keywords in CATEGORY_KEYWORDS.items()
    }
    category, hits = max(scores.items(), key=lambda item: item[1])
    return category if hits else "CULTURE / GENERAL"


def derive_coin_identity(alert: NewsAlert) -> tuple[str, str, str]:
    text = f"{alert.headline}\n{alert.summary}"
    tagged = re.findall(r"[$#]([A-Za-z][A-Za-z0-9_]{2,20})", text)
    quoted = re.findall(r"[\"“]([^\"”]{3,32})[\"”]", text)
    candidates = tagged + quoted + list(alert.narrative_terms)
    scored: list[tuple[int, int, str]] = []
    for index, raw in enumerate(candidates):
        cleaned = re.sub(r"\s+", " ", raw).strip(" .,:;!?-_/@#\"")
        words = cleaned.split()
        if not cleaned or len(cleaned) > 32:
            continue
        if cleaned.casefold() in IDENTITY_STOP:
            continue
        if all(word.casefold() in IDENTITY_STOP for word in words):
            continue
        score = 0
        if raw in tagged:
            score += 9
        if raw in quoted:
            score += 8
        if 1 <= len(words) <= 3:
            score += 4
        if 4 <= len(cleaned) <= 18:
            score += 4
        if any(char.isupper() for char in cleaned):
            score += 2
        if cleaned.casefold() in text.casefold():
            score += 1
        scored.append((score, -index, cleaned))

    primary = max(scored, default=(0, 0, ""))[2]
    if not primary:
        words = re.findall(r"[A-Za-z][A-Za-z0-9'’-]{2,}", alert.headline)
        primary = next(
            (word for word in words if word.casefold() not in IDENTITY_STOP),
            "News Meme",
        )

    name = re.sub(r"[^A-Za-z0-9 &'’.-]", "", primary).strip()[:32] or "News Meme"
    name_words = re.findall(r"[A-Za-z0-9]+", name)
    if len(name_words) >= 2:
        symbol = "".join(word[0] for word in name_words).upper()[:10]
    else:
        symbol = re.sub(r"[^A-Za-z0-9]", "", name).upper()[:10]
    if len(symbol) < 2:
        symbol = "MEME"
    return name, symbol, primary


def score_launch_opportunity(
    alert: NewsAlert,
    *,
    x_evidence: XSocialSnapshot | None = None,
    competition: NarrativeCompetition | None = None,
    cross_source_count: int = 0,
    now: int | None = None,
    watch_score: int = 45,
    launch_ready_score: int = 72,
    no_x_candidates_enabled: bool = True,
    no_x_launch_min_score: int = 78,
) -> LaunchOpportunity:
    now = now or int(time.time())
    x_evidence = x_evidence or XSocialSnapshot(available=False, error="not checked yet")
    name, symbol, primary = derive_coin_identity(alert)
    competition = competition or NarrativeCompetition(query=primary, error="not checked yet")
    text = f"{alert.headline}\n{alert.summary}"
    lowered = text.casefold()
    category = classify_event(text)
    positives: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []

    source_score = _source_score(alert)
    if source_score >= 12:
        positives.append("high-credibility source")
    elif source_score < 7:
        warnings.append("source proof is limited")

    age = max(0, now - alert.created_at) if alert.created_at else None
    speed_score = _speed_score(age)
    if age is not None and age <= 120:
        positives.append(f"detected {age}s after publication")
    elif age is not None and age > 3600:
        blockers.append("story is over one hour old")
    elif age is None:
        warnings.append("publication time is unavailable")

    event_hits = sum(1 for word in EVENT_ACTION_WORDS if word in lowered)
    dry_hits = sum(1 for phrase in DRY_TECHNICAL_PHRASES if phrase in lowered)
    viral_score = min(25, 5 + min(8, len(alert.narrative_terms) * 2) + min(8, event_hits * 3))
    if category != "CULTURE / GENERAL":
        viral_score = min(25, viral_score + 4)
    if any(marker in text for marker in ("#", '"', "“", "$")):
        viral_score = min(25, viral_score + 3)
    if 3 <= len(primary) <= 20:
        viral_score = min(25, viral_score + 2)
    if dry_hits:
        viral_score = max(0, viral_score - dry_hits * 7)
        warnings.append("story is technical and has weak meme clarity")
    if viral_score >= 18:
        positives.append(f"strong {category.lower()} meme identity")

    x_score = _x_score(alert, x_evidence)
    if x_evidence.available:
        if x_evidence.unique_authors >= 5:
            positives.append(f"X has {x_evidence.unique_authors} independent authors")
        if x_evidence.duplicate_percent >= Decimal("70"):
            warnings.append("most X posts repeat the same text")
    elif alert.source == "X filtered stream":
        warnings.append("X trend search did not provide wider confirmation")

    confirmation_score = min(10, max(0, cross_source_count) * 4)
    if cross_source_count >= 1:
        positives.append(f"confirmed by {cross_source_count} additional source(s)")

    competition_score = _competition_score(competition)
    if competition.error:
        warnings.append("existing-coin competition could not be checked")
    elif competition.matching_pairs == 0:
        positives.append("no matching Solana pair found yet")
    elif competition.strongest_liquidity_usd is not None:
        warnings.append(
            f"{competition.matching_pairs} matching pair(s) already exist; strongest has "
            f"${competition.strongest_liquidity_usd:,.0f} liquidity"
        )

    identity_score = _identity_score(primary, name, symbol, alert)
    if identity_score >= 8:
        positives.append(f"clear coin identity: {name} (${symbol})")
    elif identity_score <= 4:
        warnings.append("coin name/ticker identity is weak")

    rumor = any(word in lowered for word in RUMOR_WORDS)
    if rumor:
        blockers.append("story is framed as a rumor or unconfirmed claim")
    if alert.token_mints:
        blockers.append("a Solana contract already appears in the source")

    crypto_native = _is_crypto_native(alert, lowered)
    us_relevant = _is_us_relevant(alert, lowered)
    exceptional_event = _is_exceptional_event(lowered, category=category)
    contract_promotion_ready = bool(alert.token_mints) and _crypto_attention_ready(
        x_evidence,
        require_contract=True,
    )
    narrative_crypto_ready = not alert.token_mints and _crypto_attention_ready(
        x_evidence,
        require_contract=False,
    )
    major_breakout_ready = (
        exceptional_event
        and us_relevant
        and cross_source_count >= 2
        and _major_breakout_ready(x_evidence)
    )
    crypto_attention_ready = contract_promotion_ready or narrative_crypto_ready
    x_verified_ready = bool(
        not alert.token_mints
        and x_evidence.available
        and (
            (crypto_native and narrative_crypto_ready)
            or major_breakout_ready
        )
    )
    lane = (
        "EXISTING COIN PROMOTION"
        if alert.token_mints
        else "CRYPTO TREND"
        if crypto_native
        else "MAJOR U.S. BREAKING"
        if exceptional_event and us_relevant
        else "GENERAL NEWS — NOT ELIGIBLE"
    )

    if alert.token_mints and not contract_promotion_ready:
        warnings.append(
            "exact contract is not yet being promoted by enough credible crypto accounts"
        )
    elif crypto_native and x_evidence.available and not narrative_crypto_ready:
        warnings.append("not enough credible crypto accounts are actively pushing this narrative")
    elif exceptional_event and us_relevant and x_evidence.available and not major_breakout_ready:
        if cross_source_count < 2:
            warnings.append("major-event lane needs two additional independent news confirmations")
        warnings.append("major-event lane still lacks explosive crypto-community pickup")
    elif not crypto_native:
        warnings.append("routine or non-U.S. general news is not eligible for one-click launch")

    score = max(
        0,
        min(
            100,
            source_score
            + speed_score
            + viral_score
            + x_score
            + confirmation_score
            + competition_score
            + identity_score,
        ),
    )
    if dry_hits and event_hits == 0 and not alert.token_mints:
        score = min(score, watch_score - 1)
    if (
        not alert.token_mints
        and not crypto_native
        and not (exceptional_event and us_relevant)
    ):
        score = min(score, watch_score - 1)
    if alert.token_mints and not contract_promotion_ready:
        score = min(score, watch_score - 1)

    free_confirmation_ready = bool(
        (crypto_native and cross_source_count >= 1)
        or (
            exceptional_event
            and us_relevant
            and cross_source_count >= 2
        )
    )
    competition_clear = bool(
        competition.error is None
        and competition_score >= 8
    )
    no_x_candidate_ready = bool(
        no_x_candidates_enabled
        and not x_evidence.available
        and not alert.token_mints
        and score >= no_x_launch_min_score
        and source_score >= 12
        and speed_score >= 11
        and viral_score >= 18
        and identity_score >= 8
        and competition_clear
        and free_confirmation_ready
        and not blockers
    )

    if not x_evidence.available:
        warnings.append("X/social velocity was not verified.")
        if not no_x_candidate_ready:
            warnings.append("the stricter free launch-candidate gate did not pass")

    if alert.token_mints and contract_promotion_ready and score >= watch_score:
        verdict = "COIN FOUND"
    elif (
        score >= launch_ready_score
        and source_score >= 8
        and speed_score >= 6
        and viral_score >= 13
        and identity_score >= 6
        and competition_score >= 4
        and (
            (crypto_native and narrative_crypto_ready)
            or major_breakout_ready
        )
        and not blockers
    ):
        verdict = X_VERIFIED_LAUNCH_VERDICT
    elif no_x_candidate_ready:
        verdict = NO_X_LAUNCH_VERDICT
    elif score >= watch_score:
        verdict = "WATCH"
    else:
        verdict = "SKIP"

    confidence = (
        "HIGH"
        if verdict in MANUAL_LAUNCH_VERDICTS | {"COIN FOUND"} and score >= 80
        else "MEDIUM"
        if score >= 55
        else "LOW"
    )
    return LaunchOpportunity(
        alert=replace(alert, score=score, urgency=confidence),
        score=score,
        verdict=verdict,
        confidence=confidence,
        category=category,
        coin_name=name,
        coin_symbol=symbol,
        primary_narrative=primary,
        source_score=source_score,
        speed_score=speed_score,
        viral_score=viral_score,
        x_score=x_score,
        confirmation_score=confirmation_score,
        competition_score=competition_score,
        identity_score=identity_score,
        lane=lane,
        crypto_attention_ready=crypto_attention_ready or major_breakout_ready,
        x_verified=x_verified_ready,
        no_x_candidate_ready=no_x_candidate_ready,
        exceptional_event=exceptional_event,
        us_relevant=us_relevant,
        cross_source_count=cross_source_count,
        competition=competition,
        x_evidence=x_evidence,
        positives=tuple(dict.fromkeys(positives)),
        warnings=tuple(dict.fromkeys(warnings)),
        blockers=tuple(dict.fromkeys(blockers)),
        generated_at=now,
    )


def _keyword_present(text: str, keyword: str) -> bool:
    escaped = re.escape(keyword.casefold())
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text))


def _source_score(alert: NewsAlert) -> int:
    host = urlparse(alert.url).netloc.casefold().removeprefix("www.")
    if host.endswith(".gov"):
        score = 15
    elif any(host == domain or host.endswith(f".{domain}") for domain in KNOWN_NEWS_DOMAINS):
        score = 13
    elif alert.url.startswith("https://") and alert.source != "X filtered stream":
        score = 9
    else:
        score = 3
    if alert.author_verified:
        score += 4
    if alert.author_followers >= 1_000_000:
        score += 8
    elif alert.author_followers >= 100_000:
        score += 6
    elif alert.author_followers >= 10_000:
        score += 3
    return min(15, score)


def _speed_score(age_seconds: int | None) -> int:
    if age_seconds is None:
        return 3
    if age_seconds <= 30:
        return 15
    if age_seconds <= 120:
        return 13
    if age_seconds <= 300:
        return 11
    if age_seconds <= 900:
        return 8
    if age_seconds <= 1800:
        return 5
    if age_seconds <= 3600:
        return 2
    return 0


def _x_score(alert: NewsAlert, snapshot: XSocialSnapshot) -> int:
    score = 0
    text = f"{alert.headline} {alert.summary}".casefold()
    if alert.source == "X filtered stream" and _is_crypto_native(alert, text):
        score += 2
    if snapshot.available:
        score += min(3, snapshot.crypto_authors)
        score += min(3, snapshot.credible_crypto_authors)
        score += min(3, snapshot.promoter_posts)
        score += min(2, snapshot.contract_posts)
        if snapshot.trusted_crypto_authors or snapshot.million_follower_authors:
            score += 1
        if snapshot.unique_authors >= 5:
            score += 1
        if snapshot.posts_per_minute >= Decimal("0.25"):
            score += 1
        if snapshot.engagements >= 100:
            score += 1
        if snapshot.duplicate_percent >= Decimal("50"):
            score -= 3
        if snapshot.unique_authors and snapshot.suspicious_authors * 3 >= snapshot.unique_authors:
            score -= 2
    return max(0, min(15, score))


def _is_crypto_native(alert: NewsAlert, lowered_text: str) -> bool:
    return (
        bool(alert.token_mints)
        or alert.author.casefold() in CRYPTO_SOURCE_ACCOUNTS
        or any(_keyword_present(lowered_text, term) for term in CRYPTO_NATIVE_TERMS)
    )


def _is_us_relevant(alert: NewsAlert, lowered_text: str) -> bool:
    host = urlparse(alert.url).netloc.casefold().removeprefix("www.")
    return (
        alert.author.casefold() in US_SOURCE_ACCOUNTS
        or host.endswith(".gov")
        or host in {"espn.com", "whitehouse.gov"}
        or any(_keyword_present(lowered_text, term) for term in US_RELEVANCE_TERMS)
    )


def _is_exceptional_event(lowered_text: str, *, category: str) -> bool:
    if any(_keyword_present(lowered_text, phrase) for phrase in EXCEPTIONAL_EVENT_PHRASES):
        return True
    if category == "INTERNET / MEME" and "viral" in lowered_text:
        return True
    if (
        _keyword_present(lowered_text, "breaking")
        and any(
            _keyword_present(lowered_text, subject)
            for subject in {"trump", "white house", "president", "elon musk"}
        )
        and any(
            _keyword_present(lowered_text, action)
            for action in {"announce", "announces", "ban", "bans", "sign", "signs"}
        )
    ):
        return True
    return _keyword_present(lowered_text, "breaking") and sum(
        _keyword_present(lowered_text, word)
        for word in {"arrest", "ban", "collapse", "crash", "fires", "suspended"}
    ) >= 1


def _crypto_attention_ready(
    snapshot: XSocialSnapshot,
    *,
    require_contract: bool,
) -> bool:
    if not snapshot.available or snapshot.duplicate_percent >= Decimal("35"):
        return False
    if snapshot.unique_authors and snapshot.suspicious_authors * 3 >= snapshot.unique_authors:
        return False
    if require_contract:
        return (
            snapshot.unique_authors >= 4
            and snapshot.contract_posts >= 3
            and snapshot.contract_authors >= 3
            and snapshot.crypto_authors >= 3
            and snapshot.credible_contract_authors >= 2
            and snapshot.promoter_posts >= 3
        )
    return (
        snapshot.unique_authors >= 5
        and snapshot.established_authors >= 3
        and snapshot.crypto_authors >= 4
        and snapshot.credible_crypto_authors >= 2
        and snapshot.promoter_posts >= 3
        and (
            snapshot.influential_authors >= 1
            or snapshot.trusted_crypto_authors >= 1
            or snapshot.million_follower_authors >= 1
        )
        and (
            snapshot.posts_per_minute >= Decimal("0.25")
            or snapshot.engagements >= 50
        )
    )


def _major_breakout_ready(snapshot: XSocialSnapshot) -> bool:
    return (
        snapshot.available
        and snapshot.unique_authors >= 8
        and snapshot.established_authors >= 4
        and snapshot.influential_authors >= 2
        and snapshot.crypto_authors >= 3
        and snapshot.credible_crypto_authors >= 2
        and snapshot.promoter_posts >= 2
        and snapshot.posts_per_minute >= Decimal("0.50")
        and snapshot.engagements >= 100
        and snapshot.duplicate_percent < Decimal("35")
        and snapshot.suspicious_authors * 3 < snapshot.unique_authors
    )


def _competition_score(snapshot: NarrativeCompetition) -> int:
    if snapshot.error:
        return 2
    strongest = snapshot.strongest_liquidity_usd or Decimal("0")
    if snapshot.matching_pairs == 0:
        return 10
    if snapshot.liquid_pairs == 0 and strongest < Decimal("2000"):
        return 8
    if snapshot.liquid_pairs <= 1 and strongest < Decimal("10000"):
        return 6
    if snapshot.liquid_pairs <= 3 and strongest < Decimal("50000"):
        return 3
    return 0


def _identity_score(primary: str, name: str, symbol: str, alert: NewsAlert) -> int:
    score = 0
    if primary and primary.casefold() not in IDENTITY_STOP:
        score += 3
    if 3 <= len(name) <= 24:
        score += 2
    if 2 <= len(symbol) <= 10:
        score += 2
    if len(primary.split()) <= 3:
        score += 1
    if primary.casefold() in f"{alert.headline} {alert.summary}".casefold():
        score += 2
    return min(10, score)


def should_publish_news_opportunity(opportunity: LaunchOpportunity) -> bool:
    """Public news alerts must be immediately actionable through the launch button."""

    if opportunity.alert.token_mints or opportunity.blockers:
        return False
    if opportunity.verdict == X_VERIFIED_LAUNCH_VERDICT:
        return opportunity.x_verified and opportunity.crypto_attention_ready
    if opportunity.verdict == NO_X_LAUNCH_VERDICT:
        return opportunity.no_x_candidate_ready and not opportunity.x_verified
    return False


def is_manual_launch_opportunity(opportunity: LaunchOpportunity) -> bool:
    """Allow only explicit manual launch tiers; WATCH/SKIP/COIN FOUND stay blocked."""

    return bool(
        opportunity.verdict in MANUAL_LAUNCH_VERDICTS
        and should_publish_news_opportunity(opportunity)
    )


def is_launch_lab_eligible(
    opportunity: LaunchOpportunity,
    *,
    minimum_score: int,
    max_age_seconds: int,
    now: int | None = None,
) -> bool:
    """Keep manual browsing separate from the stricter automatic alert threshold."""

    now = now or int(time.time())
    age = max(0, now - (opportunity.alert.created_at or opportunity.generated_at or now))
    return bool(
        opportunity.score >= minimum_score
        and age <= max_age_seconds
        and not opportunity.alert.token_mints
        and not opportunity.blockers
        and opportunity.source_score >= 8
        and opportunity.speed_score >= 5
        and opportunity.viral_score >= 13
        and opportunity.identity_score >= 6
        and opportunity.competition.error is None
        and opportunity.competition_score >= 4
        and opportunity.alert.url.startswith("https://")
    )


def default_launch_draft(opportunity: LaunchOpportunity, creator_buy_sol: Decimal) -> LaunchDraft:
    source_url = opportunity.alert.url
    return LaunchDraft(
        opportunity=opportunity,
        name=opportunity.coin_name,
        symbol=opportunity.coin_symbol,
        description=(
            f"Community-created meme inspired by public news: "
            f"{opportunity.alert.headline[:280]}. Not official or affiliated with the "
            "people, brands, publisher, or event named in the source."
        ),
        creator_buy_sol=creator_buy_sol,
        website_url=(
            "" if "x.com/" in source_url or "twitter.com/" in source_url else source_url
        ),
        x_url=(source_url if "x.com/" in source_url or "twitter.com/" in source_url else ""),
    )


def validate_launch_draft(draft: LaunchDraft, *, maximum_buy_sol: Decimal) -> LaunchDraft:
    name = re.sub(r"\s+", " ", draft.name).strip()
    symbol = re.sub(r"[^A-Za-z0-9]", "", draft.symbol).upper()
    description = re.sub(r"\s+", " ", draft.description).strip()
    if not 2 <= len(name) <= 32:
        raise PumpLaunchError("Name must contain 2 to 32 characters.")
    if not 2 <= len(symbol) <= 10:
        raise PumpLaunchError("Ticker must contain 2 to 10 letters or numbers.")
    if not 10 <= len(description) <= 500:
        raise PumpLaunchError("Description must contain 10 to 500 characters.")
    if draft.creator_buy_sol <= 0 or draft.creator_buy_sol > maximum_buy_sol:
        raise PumpLaunchError(
            f"Creator buy must be above 0 and no more than {maximum_buy_sol} SOL."
        )
    website_url = _validated_optional_public_url(draft.website_url, field="Website")
    x_url = _validated_optional_public_url(draft.x_url, field="X/social URL")
    if x_url and urlparse(x_url).netloc.casefold().removeprefix("www.") not in {
        "x.com",
        "twitter.com",
    }:
        raise PumpLaunchError("X/social URL must use x.com or twitter.com.")
    return replace(
        draft,
        name=name,
        symbol=symbol,
        description=description,
        website_url=website_url,
        x_url=x_url,
        art_variant=max(0, draft.art_variant),
    )


def _validated_optional_public_url(value: str, *, field: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not is_safe_launch_image_url(value):
        raise PumpLaunchError(f"{field} must be a public HTTPS URL.")
    return value


def launch_opportunity_to_json(opportunity: LaunchOpportunity) -> str:
    return json.dumps(asdict(opportunity), default=str, separators=(",", ":"))


def launch_opportunity_from_json(payload_json: str) -> LaunchOpportunity:
    payload = json.loads(payload_json)
    alert_data = dict(payload.pop("alert"))
    for key in ("narrative_terms", "token_mints", "image_urls"):
        alert_data[key] = tuple(alert_data.get(key) or ())
    competition_data = dict(payload.pop("competition"))
    for key in (
        "strongest_liquidity_usd",
        "strongest_market_cap_usd",
    ):
        value = competition_data.get(key)
        competition_data[key] = Decimal(str(value)) if value is not None else None
    x_data = dict(payload.pop("x_evidence"))
    for key in ("duplicate_percent", "posts_per_minute"):
        x_data[key] = Decimal(str(x_data.get(key) or 0))
    for key in ("notable_accounts", "notable_posts"):
        x_data[key] = tuple(x_data.get(key) or ())
    for key in ("positives", "warnings", "blockers"):
        payload[key] = tuple(payload.get(key) or ())
    return LaunchOpportunity(
        alert=NewsAlert(**alert_data),
        competition=NarrativeCompetition(**competition_data),
        x_evidence=XSocialSnapshot(**x_data),
        **payload,
    )


def opportunity_with_draft(draft: LaunchDraft) -> LaunchOpportunity:
    return replace(
        draft.opportunity,
        coin_name=draft.name,
        coin_symbol=draft.symbol,
    )


def render_opportunity_image(
    opportunity: LaunchOpportunity,
    source_image: bytes | None = None,
) -> bytes:
    """Create original topic-aware coin art without an external image-generation bill."""

    if source_image:
        try:
            return _render_source_photo_art(opportunity, source_image)
        except (OSError, UnidentifiedImageError, ValueError):
            pass

    digest = hashlib.sha256(opportunity.primary_narrative.encode("utf-8")).digest()
    background = (max(18, digest[0] // 2), max(18, digest[1] // 2), max(24, digest[2] // 2))
    image = Image.new("RGB", (1024, 1024), background)
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.load_default(size=92)
        symbol_font = ImageFont.load_default(size=58)
        small_font = ImageFont.load_default(size=30)
    except TypeError:  # Pillow versions before scalable load_default
        title_font = symbol_font = small_font = ImageFont.load_default()

    accent = (min(255, digest[3] + 80), min(255, digest[4] + 80), min(255, digest[5] + 80))
    muted = tuple(max(45, channel // 2) for channel in accent)
    draw.polygon(((0, 0), (1024, 0), (1024, 420), (0, 610)), fill=muted)
    draw.ellipse((670, -130, 1130, 330), outline=accent, width=22)
    draw.ellipse((-180, 760, 280, 1220), outline=accent, width=18)
    draw.rounded_rectangle((55, 55, 969, 969), radius=52, outline=accent, width=10)
    draw.rounded_rectangle((85, 82, 520, 145), radius=26, fill=background)
    draw.text((110, 98), opportunity.category, fill=accent, font=small_font)

    _draw_topic_mark(draw, opportunity.category, accent, background)

    title_lines = textwrap.wrap(opportunity.coin_name.upper(), width=16)[:2]
    y = 555
    for line in title_lines:
        draw.text((90, y), line, fill="white", font=title_font)
        y += 102
    draw.rounded_rectangle((80, 790, 600, 885), radius=34, fill=accent)
    draw.text((110, 807), f"${opportunity.coin_symbol}", fill=background, font=symbol_font)
    draw.text((90, 916), "COMMUNITY MEME | NOT OFFICIAL", fill=(225, 225, 225), font=small_font)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _render_source_photo_art(opportunity: LaunchOpportunity, source_image: bytes) -> bytes:
    with Image.open(io.BytesIO(source_image)) as original:
        original.load()
        if original.width < 240 or original.height < 240:
            raise ValueError("source image is too small")
        image = ImageOps.fit(
            original.convert("RGB"),
            (1024, 1024),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.45),
        ).convert("RGBA")

    shade = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    for y in range(380, 1024):
        alpha = min(220, 35 + int((y - 380) * 0.31))
        shade_draw.line((0, y, 1024, y), fill=(5, 7, 12, alpha))
    shade_draw.rounded_rectangle(
        (52, 52, 972, 972),
        radius=56,
        outline=(255, 255, 255, 230),
        width=12,
    )
    image = Image.alpha_composite(image, shade)
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.load_default(size=86)
        symbol_font = ImageFont.load_default(size=56)
        small_font = ImageFont.load_default(size=29)
    except TypeError:
        title_font = symbol_font = small_font = ImageFont.load_default()

    digest = hashlib.sha256(opportunity.primary_narrative.encode("utf-8")).digest()
    accent = (min(255, digest[3] + 80), min(255, digest[4] + 80), min(255, digest[5] + 80))
    draw.rounded_rectangle((80, 70, 525, 137), radius=28, fill=(5, 7, 12, 205))
    draw.text((108, 87), "SOURCE-LED COIN ART", fill=accent, font=small_font)
    title_lines = textwrap.wrap(opportunity.coin_name.upper(), width=16)[:2]
    y = 640
    for line in title_lines:
        draw.text((82, y), line, fill="white", font=title_font, stroke_width=3, stroke_fill="black")
        y += 98
    draw.rounded_rectangle((78, 850, 585, 936), radius=34, fill=accent)
    draw.text((108, 863), f"${opportunity.coin_symbol}", fill=(5, 7, 12), font=symbol_font)
    draw.text((610, 881), "UNOFFICIAL MEME", fill="white", font=small_font)

    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def _draw_topic_mark(
    draw: ImageDraw.ImageDraw,
    category: str,
    accent: tuple[int, int, int],
    background: tuple[int, int, int],
) -> None:
    """Draw a bold category motif so every launch is more than a generic text card."""

    category = category.upper()
    if category == "SPORTS":
        draw.arc((350, 190, 675, 480), 0, 180, fill=accent, width=34)
        draw.rectangle((445, 305, 580, 455), fill=accent)
        draw.rectangle((495, 445, 530, 505), fill=accent)
        draw.rounded_rectangle((420, 495, 605, 535), radius=18, fill=accent)
    elif category == "POLITICS":
        draw.polygon(((320, 290), (512, 175), (705, 290)), fill=accent)
        draw.rectangle((330, 300, 695, 340), fill=accent)
        for x in (365, 455, 545, 635):
            draw.rectangle((x, 340, x + 35, 485), fill=accent)
        draw.rectangle((300, 485, 725, 525), fill=accent)
    elif category == "CELEBRITY":
        points: list[tuple[float, float]] = []
        for index in range(10):
            radius = 170 if index % 2 == 0 else 72
            angle = -math.pi / 2 + index * math.pi / 5
            points.append((512 + radius * math.cos(angle), 350 + radius * math.sin(angle)))
        draw.polygon(points, fill=accent)
    elif category == "BRAND / PRODUCT":
        draw.rounded_rectangle((330, 205, 695, 520), radius=58, fill=accent)
        draw.rectangle((390, 170, 635, 245), fill=accent)
        draw.rounded_rectangle((390, 305, 635, 430), radius=28, fill=background)
    elif category == "INTERNET / MEME":
        draw.rounded_rectangle((285, 205, 650, 425), radius=62, fill=accent)
        draw.polygon(((390, 410), (335, 505), (475, 425)), fill=accent)
        draw.ellipse((350, 295, 390, 335), fill=background)
        draw.ellipse((455, 295, 495, 335), fill=background)
        draw.ellipse((560, 295, 600, 335), fill=background)
    elif category == "GAMING / TECH":
        draw.rounded_rectangle((295, 245, 730, 475), radius=100, fill=accent)
        draw.rectangle((380, 320, 500, 360), fill=background)
        draw.rectangle((420, 280, 460, 400), fill=background)
        draw.ellipse((590, 295, 635, 340), fill=background)
        draw.ellipse((650, 360, 695, 405), fill=background)
    elif category == "CRYPTO":
        draw.ellipse((330, 165, 695, 530), fill=accent, outline="white", width=12)
        draw.ellipse((385, 220, 640, 475), fill=background)
        for x, top, bottom in ((430, 350, 435), (485, 280, 430), (540, 315, 440), (595, 240, 420)):
            draw.rectangle((x, top, x + 25, bottom), fill=accent)
    elif category == "WORLD EVENT":
        draw.ellipse((350, 185, 675, 510), outline=accent, width=28)
        draw.ellipse((410, 245, 615, 450), outline=accent, width=22)
        draw.ellipse((475, 310, 550, 385), fill=accent)
    else:
        draw.polygon(
            ((535, 165), (350, 385), (485, 385), (445, 535), (680, 300), (535, 300)),
            fill=accent,
        )


def is_safe_launch_image_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


class PumpLaunchClient:
    BUILD_URL = "https://fun-block.pump.fun/agents/create-coin"
    PINATA_UPLOAD_URL = "https://uploads.pinata.cloud/v3/files"
    PINATA_TEST_URL = "https://api.pinata.cloud/data/testAuthentication"
    MAX_SOURCE_IMAGE_BYTES = 8 * 1024 * 1024

    def __init__(self, settings: Settings, rpc: SolanaRPC) -> None:
        self.settings = settings
        self.rpc = rpc
        self._session: aiohttp.ClientSession | None = None
        self.last_pinata_success_at: int | None = None

    @property
    def configured(self) -> bool:
        return self.settings.pump_launch_is_unlocked and self.wallet_address is not None

    @property
    def wallet_address(self) -> str | None:
        if not self.settings.pump_launch_private_key:
            return None
        try:
            return str(load_keypair(self.settings.pump_launch_private_key).pubkey())
        except ValueError:
            return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _render_launch_art(
        self,
        opportunity: LaunchOpportunity,
        *,
        variant: int = 0,
    ) -> bytes:
        session = await self._get_session()
        candidates = (
            opportunity.alert.image_urls[:3]
            if self.settings.news_source_image_enabled
            else ()
        )
        ordered_candidates = candidates[variant:] + candidates[:variant] if candidates else ()
        if variant >= len(candidates):
            ordered_candidates = ()
        for image_url in ordered_candidates:
            if not is_safe_launch_image_url(image_url):
                continue
            try:
                async with session.get(image_url, allow_redirects=True) as response:
                    if response.status >= 400 or not is_safe_launch_image_url(str(response.url)):
                        continue
                    content_type = str(response.headers.get("Content-Type") or "").casefold()
                    if not content_type.startswith("image/"):
                        continue
                    if (
                        response.content_length is not None
                        and response.content_length > self.MAX_SOURCE_IMAGE_BYTES
                    ):
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > self.MAX_SOURCE_IMAGE_BYTES:
                            chunks = []
                            break
                        chunks.append(chunk)
                    if chunks:
                        return render_opportunity_image(opportunity, b"".join(chunks))
            except (TimeoutError, aiohttp.ClientError, ValueError):
                continue
        fallback_opportunity = (
            replace(
                opportunity,
                primary_narrative=f"{opportunity.primary_narrative} art variant {variant}",
            )
            if variant
            else opportunity
        )
        return render_opportunity_image(fallback_opportunity)

    async def render_draft_art(self, draft: LaunchDraft) -> bytes:
        return await self._render_launch_art(
            opportunity_with_draft(draft),
            variant=draft.art_variant,
        )

    async def pinata_health(self) -> tuple[bool, str]:
        if not self.settings.pinata_jwt:
            return False, "PINATA_JWT is not configured"
        session = await self._get_session()
        try:
            async with session.get(
                self.PINATA_TEST_URL,
                headers={"Authorization": f"Bearer {self.settings.pinata_jwt}"},
            ) as response:
                await response.read()
                if response.status == 200:
                    return True, "READY"
                if response.status in {401, 403}:
                    return False, "PINATA AUTH FAILED"
                return False, f"PINATA HEALTH HTTP {response.status}"
        except (TimeoutError, aiohttp.ClientError):
            return False, "PINATA HEALTH NETWORK FAILURE"

    async def launch(self, opportunity: LaunchOpportunity) -> PumpLaunchResult:
        created_at = int(time.time())
        key = alert_key(opportunity.alert)
        if not self.configured:
            raise PumpLaunchError("one-click Pump launch is locked or missing required secrets")
        if not is_manual_launch_opportunity(opportunity):
            raise PumpLaunchError("only a manual launch candidate or X-verified alert can launch")
        if opportunity.score < self.settings.pump_launch_min_score:
            raise PumpLaunchError("opportunity score is below PUMP_LAUNCH_MIN_SCORE")
        if opportunity.alert.token_mints:
            raise PumpLaunchError("a source contract already exists; launch was blocked")

        private_key = self.settings.pump_launch_private_key or ""
        try:
            keypair = load_keypair(private_key, variable_name="PUMP_LAUNCH_PRIVATE_KEY")
        except ValueError as exc:
            raise PumpLaunchError(str(exc)) from exc
        wallet = str(keypair.pubkey())
        image_cid = await self._pin_file(
            filename=f"{opportunity.coin_symbol.lower()}-launch.png",
            content=await self._render_launch_art(opportunity),
            content_type="image/png",
        )
        image_uri = f"https://ipfs.io/ipfs/{image_cid}"
        source_url = opportunity.alert.url
        metadata = {
            "name": opportunity.coin_name,
            "symbol": opportunity.coin_symbol,
            "description": (
                f"Community-created meme inspired by public news: "
                f"{opportunity.alert.headline[:280]}. Not official or affiliated with the "
                "people, brands, publisher, or event named in the source."
            ),
            "image": image_uri,
            "showName": True,
            "createdOn": "https://pump.fun",
        }
        if source_url:
            if "x.com/" in source_url or "twitter.com/" in source_url:
                metadata["twitter"] = source_url
            else:
                metadata["website"] = source_url
        metadata_cid = await self._pin_file(
            filename=f"{opportunity.coin_symbol.lower()}-metadata.json",
            content=json.dumps(metadata, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
        )
        metadata_uri = f"https://ipfs.io/ipfs/{metadata_cid}"
        lamports = int(self.settings.pump_launch_initial_buy_sol * Decimal("1000000000"))
        payload = {
            "user": wallet,
            "name": opportunity.coin_name,
            "symbol": opportunity.coin_symbol,
            "uri": metadata_uri,
            "solLamports": str(lamports),
            "mayhemMode": self.settings.pump_launch_mayhem_mode,
            "cashback": self.settings.pump_launch_cashback,
            "tokenizedAgent": self.settings.pump_launch_tokenized_agent,
            "buybackBps": self.settings.pump_launch_buyback_bps,
            "frontRunningProtection": False,
            "tipAmount": 0,
            "encoding": "base64",
            "feePayer": wallet,
            "creator": wallet,
        }
        session = await self._get_session()
        try:
            async with session.post(self.BUILD_URL, json=payload) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    detail = _error_detail(body)
                    raise PumpLaunchError(
                        f"Pump build API HTTP {response.status}: {detail or 'request rejected'}"
                    )
        except PumpLaunchError:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise PumpLaunchError(f"Pump build request failed: {exc}") from exc

        if not isinstance(body, dict):
            raise PumpLaunchError("Pump build API returned an invalid response")
        encoded = str(body.get("transaction") or "")
        mint = str(body.get("mintPublicKey") or "")
        if not encoded or not mint:
            raise PumpLaunchError("Pump build API omitted transaction or mintPublicKey")
        try:
            signed = sign_versioned_transaction(
                encoded,
                keypair,
                provider="Pump.fun",
            )
        except Exception as exc:
            raise PumpLaunchError(str(exc)) from exc

        try:
            signature = await self.rpc.call(
                "sendTransaction",
                [
                    signed,
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed",
                        "maxRetries": 3,
                    },
                ],
            )
        except Exception as exc:
            raise PumpLaunchError(f"signed Pump transaction could not be sent: {exc}") from exc
        signature = str(signature or "")
        if not signature:
            raise PumpLaunchError("Solana RPC returned no transaction signature")
        return PumpLaunchResult(
            success=True,
            status="SUBMITTED",
            message="Pump.fun create + initial buy transaction submitted",
            alert_key=key,
            name=opportunity.coin_name,
            symbol=opportunity.coin_symbol,
            mint=mint,
            signature=signature,
            metadata_uri=metadata_uri,
            explorer_url=f"https://solscan.io/tx/{signature}",
            created_at=created_at,
        )

    async def _pin_file(self, *, filename: str, content: bytes, content_type: str) -> str:
        jwt = self.settings.pinata_jwt or ""
        if not jwt:
            raise PumpLaunchError("PINATA_JWT is required for the public launch image")
        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type=content_type)
        form.add_field("network", "public")
        form.add_field("name", filename)
        try:
            async with session.post(
                self.PINATA_UPLOAD_URL,
                data=form,
                headers={"Authorization": f"Bearer {jwt}"},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise PumpLaunchError(f"PINATA UPLOAD FAILED — HTTP {response.status}")
        except PumpLaunchError:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise PumpLaunchError("PINATA UPLOAD FAILED — network failure") from exc
        data = body.get("data") if isinstance(body, dict) else None
        cid = str((data or {}).get("cid") or "") if isinstance(data, dict) else ""
        if not cid:
            raise PumpLaunchError("PINATA UPLOAD FAILED — response omitted a CID")
        self.last_pinata_success_at = int(time.time())
        return cid


J7_REGION_BASES = {
    "europe": "https://eu.j7tracker.io/deploy",
    "na-east": "https://nyc.j7tracker.io/deploy",
    "na-west": "https://lax.j7tracker.io/deploy",
    "asia": "https://sgp.j7tracker.io/deploy",
    "australia": "https://aus.j7tracker.io/deploy",
}


def build_j7_launch_payload(
    opportunity: LaunchOpportunity,
    settings: Settings,
    *,
    image_url: str,
    draft: LaunchDraft | None = None,
) -> dict[str, Any]:
    """Build only documented J7 external-deploy fields; credentials stay in headers/body."""

    draft = draft or default_launch_draft(
        opportunity,
        settings.pump_launch_initial_buy_sol,
    )
    source_url = draft.website_url or draft.x_url or opportunity.alert.url
    payload: dict[str, Any] = {
        "type": "create_token",
        "external": True,
        "api_key": settings.j7_launch_api_key or "",
        "mode": "pump",
        "name": draft.name,
        "ticker": draft.symbol,
        "buy_amount": float(draft.creator_buy_sol),
        "image_url": image_url,
        "image_type": "url",
        "sell_panel_enabled": True,
        "private_desc": False,
        "description": draft.description,
        "mayhem_mode": settings.pump_launch_mayhem_mode,
        "cashback_mode": settings.pump_launch_cashback,
        "agent_mode": settings.pump_launch_tokenized_agent,
        "buyback_bps": settings.pump_launch_buyback_bps,
    }
    if draft.website_url:
        payload["website"] = draft.website_url
    if draft.x_url:
        payload["twitter"] = draft.x_url
    if source_url and "website" not in payload and "twitter" not in payload:
        payload["website"] = source_url
    return payload


class J7LaunchClient(PumpLaunchClient):
    """Launch through J7's documented API using its encrypted per-wallet key."""

    @property
    def configured(self) -> bool:
        return self.settings.j7_launch_is_unlocked

    @property
    def wallet_address(self) -> str | None:
        value = self.settings.j7_launch_wallet_address
        if not value:
            return None
        try:
            return str(Pubkey.from_string(value))
        except ValueError:
            return None

    async def health_check(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "J7 configuration is incomplete"
        base_url = J7_REGION_BASES[self.settings.j7_launch_region]
        session = await self._get_session()
        try:
            async with session.get(
                f"{base_url}/ping",
                headers={"Authorization": f"Bearer {self.settings.j7_launch_session_token}"},
            ) as response:
                await response.read()
                if 200 <= response.status < 300:
                    return True, "HEALTHY"
                return False, _j7_http_error(response.status, health=True)
        except (TimeoutError, aiohttp.ClientError):
            return False, "NETWORK FAILURE"

    async def wallet_balance(self) -> Decimal:
        wallet = self.wallet_address
        if not self.settings.j7_launch_wallet_address:
            raise PumpLaunchError("J7_LAUNCH_WALLET_ADDRESS is not configured")
        if wallet is None:
            raise PumpLaunchError("J7_LAUNCH_WALLET_ADDRESS is not a valid Solana address")
        try:
            result = await self.rpc.call("getBalance", [wallet, {"commitment": "confirmed"}])
        except Exception as exc:
            raise PumpLaunchError("PUBLIC WALLET BALANCE LOOKUP FAILED") from exc
        lamports = result.get("value") if isinstance(result, dict) else None
        if not isinstance(lamports, int) or lamports < 0:
            raise PumpLaunchError("PUBLIC WALLET BALANCE LOOKUP RETURNED INVALID DATA")
        return Decimal(lamports) / Decimal("1000000000")

    async def launch(
        self,
        opportunity: LaunchOpportunity,
        *,
        draft: LaunchDraft | None = None,
        allow_launch_lab: bool = False,
    ) -> PumpLaunchResult:
        created_at = int(time.time())
        key = alert_key(opportunity.alert)
        if not self.configured:
            raise PumpLaunchError("J7 launch is locked or missing required credentials")
        draft = validate_launch_draft(
            draft or default_launch_draft(
                opportunity,
                self.settings.pump_launch_initial_buy_sol,
            ),
            maximum_buy_sol=self.settings.pump_launch_initial_buy_sol,
        )
        lab_eligible = allow_launch_lab and is_launch_lab_eligible(
            opportunity,
            minimum_score=self.settings.launch_lab_min_score,
            max_age_seconds=self.settings.launch_lab_max_age_seconds,
        )
        if not is_manual_launch_opportunity(opportunity) and not lab_eligible:
            raise PumpLaunchError("only a manual launch candidate or X-verified alert can launch")
        if not lab_eligible and opportunity.score < self.settings.pump_launch_min_score:
            raise PumpLaunchError("opportunity score is below PUMP_LAUNCH_MIN_SCORE")
        if opportunity.alert.token_mints:
            raise PumpLaunchError("a source contract already exists; launch was blocked")
        if lab_eligible:
            key = launch_draft_key(draft)

        image_cid = await self._pin_file(
            filename=f"{draft.symbol.lower()}-launch.png",
            content=await self.render_draft_art(draft),
            content_type="image/png",
        )
        image_url = f"https://ipfs.io/ipfs/{image_cid}"
        payload = build_j7_launch_payload(
            opportunity,
            self.settings,
            image_url=image_url,
            draft=draft,
        )
        base_url = J7_REGION_BASES[self.settings.j7_launch_region]
        session = await self._get_session()

        # Warm the same regional path immediately before a time-sensitive deploy.
        try:
            async with session.get(f"{base_url}/ping") as response:
                await response.read()
        except (TimeoutError, aiohttp.ClientError):
            pass

        try:
            async with session.post(
                f"{base_url}/submit",
                data=json.dumps(payload, separators=(",", ":")),
                headers={
                    "Authorization": f"Bearer {self.settings.j7_launch_session_token}",
                    "Content-Type": "text/plain;charset=UTF-8",
                },
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise PumpLaunchError(_j7_http_error(response.status))
        except PumpLaunchError:
            raise
        except TimeoutError as exc:
            raise UnknownLaunchResultError(
                "UNKNOWN SUBMISSION STATE — J7 timed out after submission; do not retry"
            ) from exc
        except aiohttp.ClientError as exc:
            raise UnknownLaunchResultError(
                "UNKNOWN SUBMISSION STATE — network failed after submission; do not retry"
            ) from exc
        except ValueError as exc:
            raise PumpLaunchError("J7 REQUEST REJECTED — invalid response") from exc

        if not isinstance(body, dict):
            raise UnknownLaunchResultError("J7 RESPONSE MALFORMED — submission state unknown")
        if body.get("type") != "token_create_success":
            detail = _error_detail(body)
            raise PumpLaunchError(f"J7 REQUEST REJECTED — {detail or 'provider rejected it'}")
        mint = str(body.get("mint_address") or body.get("address") or "")
        signature = str(body.get("signature") or body.get("tx_hash") or "")
        if not mint:
            raise UnknownLaunchResultError("J7 RESPONSE MISSING MINT — submission state unknown")
        return PumpLaunchResult(
            success=True,
            status="SUBMITTED",
            message="J7 Tracker created the Pump.fun coin and submitted the initial buy",
            alert_key=key,
            name=draft.name,
            symbol=draft.symbol,
            mint=mint,
            signature=signature,
            metadata_uri=image_url,
            explorer_url=(
                f"https://solscan.io/tx/{signature}"
                if signature
                else f"https://pump.fun/coin/{mint}"
            ),
            created_at=created_at,
            provider="J7 Tracker",
        )


class OneClickLaunchClient:
    """Prefer J7's encrypted-key route and retain direct Pump signing as a fallback."""

    def __init__(self, settings: Settings, rpc: SolanaRPC) -> None:
        self.j7 = J7LaunchClient(settings, rpc)
        self.pump = PumpLaunchClient(settings, rpc)

    @property
    def configured(self) -> bool:
        return self.j7.configured or self.pump.configured

    @property
    def provider(self) -> str:
        if self.j7.configured:
            return "J7 Tracker"
        if self.pump.configured:
            return "Pump.fun direct"
        return "none"

    @property
    def wallet_address(self) -> str | None:
        if self.j7.configured:
            return self.j7.wallet_address
        return self.pump.wallet_address if self.pump.configured else None

    async def launch(self, opportunity: LaunchOpportunity) -> PumpLaunchResult:
        if self.j7.configured:
            return await self.j7.launch(opportunity)
        return await self.pump.launch(opportunity)

    async def close(self) -> None:
        await self.j7.close()
        await self.pump.close()


def _error_detail(body: Any) -> str:
    if not isinstance(body, dict):
        return str(body)[:200]
    return str(
        body.get("detail")
        or body.get("message")
        or body.get("error")
        or body.get("errors")
        or ""
    )[:200]


def _j7_http_error(status: int, *, health: bool = False) -> str:
    if status == 401:
        return "J7 SESSION EXPIRED / AUTH FAILED"
    if status == 403:
        return "J7 AUTH FAILED"
    if status == 429:
        return "J7 RATE LIMITED"
    if status >= 500:
        return "J7 ENDPOINT UNHEALTHY" if health else "J7 SERVER ERROR"
    return f"J7 REQUEST REJECTED — HTTP {status}"
