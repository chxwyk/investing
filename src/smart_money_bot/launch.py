from __future__ import annotations

import hashlib
import io
import json
import re
import textwrap
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .errors import PumpLaunchError
from .market import load_keypair, sign_versioned_transaction
from .models import (
    LaunchOpportunity,
    NarrativeCompetition,
    NewsAlert,
    PumpLaunchResult,
    XSocialSnapshot,
)
from .rpc import SolanaRPC

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
    "espn.com",
    "reuters.com",
    "sec.gov",
    "whitehouse.gov",
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
    if alert.token_mints and score >= watch_score:
        verdict = "COIN FOUND"
    elif (
        score >= launch_ready_score
        and source_score >= 8
        and speed_score >= 6
        and viral_score >= 13
        and identity_score >= 6
        and competition_score >= 4
        and not blockers
    ):
        verdict = "LAUNCH READY"
    elif score >= watch_score:
        verdict = "WATCH"
    else:
        verdict = "SKIP"

    confidence = "HIGH" if score >= 80 else "MEDIUM" if score >= 55 else "LOW"
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
    if alert.source == "X filtered stream":
        score += 2
        if alert.author_verified:
            score += 2
        if alert.author_followers >= 100_000:
            score += 2
    if snapshot.available:
        score += min(4, snapshot.unique_authors // 2)
        score += min(3, snapshot.established_authors)
        score += min(2, snapshot.influential_authors)
        if snapshot.posts_per_minute >= Decimal("1"):
            score += 2
        elif snapshot.posts_per_minute >= Decimal("0.25"):
            score += 1
        if snapshot.engagements >= 1_000:
            score += 2
        elif snapshot.engagements >= 100:
            score += 1
        if snapshot.duplicate_percent >= Decimal("70"):
            score -= 3
        if snapshot.suspicious_authors > snapshot.established_authors:
            score -= 2
    return max(0, min(15, score))


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


def render_opportunity_image(opportunity: LaunchOpportunity) -> bytes:
    """Create a deterministic news-card PNG for Pump metadata without external AI calls."""

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
    draw.rounded_rectangle((55, 55, 969, 969), radius=52, outline=accent, width=10)
    draw.text((90, 90), opportunity.category, fill=accent, font=small_font)
    title_lines = textwrap.wrap(opportunity.coin_name.upper(), width=14)[:3]
    y = 270
    for line in title_lines:
        draw.text((90, y), line, fill="white", font=title_font)
        y += 105
    draw.text((90, 710), f"${opportunity.coin_symbol}", fill=accent, font=symbol_font)
    draw.text((90, 880), "COMMUNITY MEME • NOT OFFICIAL", fill=(220, 220, 220), font=small_font)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


class PumpLaunchClient:
    BUILD_URL = "https://fun-block.pump.fun/agents/create-coin"
    PINATA_UPLOAD_URL = "https://uploads.pinata.cloud/v3/files"

    def __init__(self, settings: Settings, rpc: SolanaRPC) -> None:
        self.settings = settings
        self.rpc = rpc
        self._session: aiohttp.ClientSession | None = None

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

    async def launch(self, opportunity: LaunchOpportunity) -> PumpLaunchResult:
        created_at = int(time.time())
        key = alert_key(opportunity.alert)
        if not self.configured:
            raise PumpLaunchError("one-click Pump launch is locked or missing required secrets")
        if opportunity.verdict != "LAUNCH READY":
            raise PumpLaunchError("only a LAUNCH READY alert can use one-click launch")
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
            content=render_opportunity_image(opportunity),
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
            raise PumpLaunchError("PINATA_JWT is required for Pump metadata")
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
                    detail = _error_detail(body)
                    raise PumpLaunchError(
                        f"Pinata upload HTTP {response.status}: {detail or 'request rejected'}"
                    )
        except PumpLaunchError:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise PumpLaunchError(f"Pinata upload failed: {exc}") from exc
        data = body.get("data") if isinstance(body, dict) else None
        cid = str((data or {}).get("cid") or "") if isinstance(data, dict) else ""
        if not cid:
            raise PumpLaunchError("Pinata upload response omitted a CID")
        return cid


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
