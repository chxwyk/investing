"""Where Trending data actually came from, and what it is honestly allowed to be called.

This module exists because of one specific failure mode the operator asked us to
avoid: quietly relabelling an *approximation* as the real thing.  The bot has no
authorised Fomo Trending feed by default.  What it has is a public DEX Screener
nomination lane, which correlates with attention but is not Fomo's Trending
board and must never be printed as though it were.

So provenance is a first-class, persisted value rather than a naming convention:

``FOMO_TRENDING``
    An administrator-configured, authorised Fomo Trending feed.  Only a
    deployment that supplies :envvar:`FOMO_TRENDING_API_URL` can ever produce
    this label, and the label is attached by the source adapter itself.

``TRENDING_PROXY``
    A public approximation (today: DEX Screener token profiles and boosts).
    Every card, every ledger row and every alert built from it says so.

``NO_SOURCE_CONFIGURED``
    Nothing legitimate is connected.  The lane reports this rather than showing
    an empty green "ACTIVE", because "connected and quiet" and "not connected"
    are different problems for a human to fix.

Nothing in this module scrapes, reuses a session, replays a cookie or reverses a
private endpoint.  An authorised feed is supplied by configuration or it does
not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# --- provenance (sections 4, 11) ---------------------------------------------
#: An authorised, administrator-configured Fomo Trending feed.
SOURCE_FOMO_TRENDING = "FOMO_TRENDING"
#: A public approximation of attention.  Never call this Fomo Trending.
SOURCE_TRENDING_PROXY = "TRENDING_PROXY"
#: Nothing is connected.
SOURCE_NONE = "NO_SOURCE_CONFIGURED"

SOURCE_KINDS: tuple[str, ...] = (
    SOURCE_FOMO_TRENDING,
    SOURCE_TRENDING_PROXY,
    SOURCE_NONE,
)

#: Human labels.  The proxy label is deliberately unglamorous.
SOURCE_LABELS: dict[str, str] = {
    SOURCE_FOMO_TRENDING: "FOMO TRENDING (authorised feed)",
    SOURCE_TRENDING_PROXY: "TRENDING PROXY (public approximation — not Fomo Trending)",
    SOURCE_NONE: "NO SOURCE CONFIGURED",
}

#: Only the authorised feed may claim to be Fomo's own ranking.
EXACT_SOURCES: frozenset[str] = frozenset({SOURCE_FOMO_TRENDING})


# --- change-window semantics (section 6) -------------------------------------
#: Fomo shows a percentage.  We do not know what window it covers unless the
#: source says so, and guessing "24h" would silently corrupt every derived
#: number.  An unknown window stays unknown.
CHANGE_WINDOW_UNKNOWN = "CHANGE_WINDOW_UNKNOWN"
CHANGE_WINDOW_5M = "5M"
CHANGE_WINDOW_1H = "1H"
CHANGE_WINDOW_6H = "6H"
CHANGE_WINDOW_24H = "24H"
CHANGE_WINDOW_SINCE_LAUNCH = "SINCE_LAUNCH"

KNOWN_CHANGE_WINDOWS: frozenset[str] = frozenset(
    {
        CHANGE_WINDOW_5M,
        CHANGE_WINDOW_1H,
        CHANGE_WINDOW_6H,
        CHANGE_WINDOW_24H,
        CHANGE_WINDOW_SINCE_LAUNCH,
    }
)

#: Strings a source may legitimately use for each window.  Anything not listed
#: resolves to ``CHANGE_WINDOW_UNKNOWN`` — an unrecognised label is not a licence
#: to pick the nearest guess.
_WINDOW_ALIASES: dict[str, str] = {
    "5m": CHANGE_WINDOW_5M,
    "m5": CHANGE_WINDOW_5M,
    "5min": CHANGE_WINDOW_5M,
    "1h": CHANGE_WINDOW_1H,
    "h1": CHANGE_WINDOW_1H,
    "60m": CHANGE_WINDOW_1H,
    "6h": CHANGE_WINDOW_6H,
    "h6": CHANGE_WINDOW_6H,
    "24h": CHANGE_WINDOW_24H,
    "h24": CHANGE_WINDOW_24H,
    "1d": CHANGE_WINDOW_24H,
    "day": CHANGE_WINDOW_24H,
    "since_launch": CHANGE_WINDOW_SINCE_LAUNCH,
    "since-launch": CHANGE_WINDOW_SINCE_LAUNCH,
    "launch": CHANGE_WINDOW_SINCE_LAUNCH,
    "all": CHANGE_WINDOW_SINCE_LAUNCH,
}


def normalise_change_window(value: object) -> str:
    """Map a source's window label onto a known window, or admit we do not know."""

    if value is None:
        return CHANGE_WINDOW_UNKNOWN
    text = str(value).strip().casefold().replace(" ", "")
    if not text:
        return CHANGE_WINDOW_UNKNOWN
    if text.upper() in KNOWN_CHANGE_WINDOWS:
        return text.upper()
    return _WINDOW_ALIASES.get(text, CHANGE_WINDOW_UNKNOWN)


def describe_change(percent: Decimal | None, window: str) -> str:
    """Render a displayed change without inventing its timeframe."""

    if percent is None:
        return "unknown"
    sign = "+" if percent >= 0 else ""
    if window == CHANGE_WINDOW_UNKNOWN:
        return f"{sign}{percent:.1f}% (window unknown)"
    return f"{sign}{percent:.1f}% / {window}"


# --- the source descriptor ---------------------------------------------------
@dataclass(frozen=True, slots=True)
class TrendingSourceInfo:
    """What produced a snapshot, and what may honestly be claimed about it."""

    kind: str = SOURCE_NONE
    #: Free-text provider name, e.g. ``"dexscreener_profiles_boosts"``.
    provider: str = ""
    #: True only for an administrator-configured, authorised feed.
    authorised: bool = False
    #: Whether the source documents its own percentage window.
    change_window: str = CHANGE_WINDOW_UNKNOWN
    #: Whether the source publishes an actual ordinal rank.  A proxy that only
    #: supplies an ordering derives rank from its own list position, which is
    #: weaker evidence and is labelled as such.
    publishes_rank: bool = False
    detail: str = ""

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.kind, self.kind)

    @property
    def is_exact_fomo(self) -> bool:
        """True only when the rank really is Fomo's own Trending rank."""

        return self.kind in EXACT_SOURCES and self.authorised

    @property
    def configured(self) -> bool:
        return self.kind != SOURCE_NONE

    def rank_caveat(self) -> str:
        """One line an operator surface can print next to any rank."""

        if self.is_exact_fomo:
            return "Rank is the authorised Fomo Trending rank."
        if self.kind == SOURCE_TRENDING_PROXY:
            return (
                "Rank is a PROXY ordering from public attention data, "
                "not Fomo's Trending rank."
            )
        return "No Trending source is configured; no rank is available."

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "authorised": self.authorised,
            "change_window": self.change_window,
            "publishes_rank": self.publishes_rank,
            "detail": self.detail,
        }


def source_from_settings(
    *,
    api_url: str | None,
    api_key: str | None,
    proxy_enabled: bool,
    proxy_provider: str = "dexscreener_profiles_boosts",
    change_window: object = None,
) -> TrendingSourceInfo:
    """Resolve provenance from configuration alone.

    An authorised feed requires an explicitly configured URL.  There is no code
    path that promotes the proxy to ``FOMO_TRENDING`` — not on a heuristic, not
    on a hostname, not on a response shape.
    """

    url = (api_url or "").strip()
    if url:
        return TrendingSourceInfo(
            kind=SOURCE_FOMO_TRENDING,
            provider=url,
            authorised=True,
            change_window=normalise_change_window(change_window),
            publishes_rank=True,
            detail=(
                "Administrator-configured Fomo Trending feed"
                f"{' with credentials' if api_key else ' without credentials'}."
            ),
        )
    if proxy_enabled:
        return TrendingSourceInfo(
            kind=SOURCE_TRENDING_PROXY,
            provider=proxy_provider,
            authorised=False,
            # The proxy's percentages come from DEX Screener price changes, which
            # *are* documented per window; the Trending ordering itself is not a
            # Fomo rank and says so.
            change_window=normalise_change_window(change_window),
            publishes_rank=False,
            detail=(
                "Public DEX Screener attention data used as an approximation. "
                "No authorised Fomo Trending feed is configured."
            ),
        )
    return TrendingSourceInfo(
        kind=SOURCE_NONE,
        provider="",
        authorised=False,
        change_window=CHANGE_WINDOW_UNKNOWN,
        publishes_rank=False,
        detail="No Trending source is configured.",
    )


# --- lane health (section 34) ------------------------------------------------
HEALTH_NO_SOURCE = "NO_SOURCE_CONFIGURED"
HEALTH_DISABLED = "DISABLED_BY_CONFIG"
HEALTH_ACTIVE_NO_EVENTS = "ACTIVE_NO_EVENTS"
HEALTH_ACTIVE = "ACTIVE"
HEALTH_DEGRADED = "PROVIDER_DEGRADED"
HEALTH_STALE = "STALE"

TRENDING_HEALTH_STATES: tuple[str, ...] = (
    HEALTH_NO_SOURCE,
    HEALTH_DISABLED,
    HEALTH_ACTIVE_NO_EVENTS,
    HEALTH_ACTIVE,
    HEALTH_DEGRADED,
    HEALTH_STALE,
)


@dataclass(frozen=True, slots=True)
class TrendingLaneHealth:
    """The honest state of the Trending discovery lane."""

    state: str = HEALTH_NO_SOURCE
    source: TrendingSourceInfo = field(default_factory=TrendingSourceInfo)
    snapshots: int = 0
    last_snapshot_at: int | None = None
    last_error: str = ""
    tracked: int = 0

    @property
    def healthy(self) -> bool:
        return self.state == HEALTH_ACTIVE

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "source": self.source.to_json(),
            "snapshots": self.snapshots,
            "last_snapshot_at": self.last_snapshot_at,
            "last_error": self.last_error,
            "tracked": self.tracked,
        }


def assess_lane_health(
    *,
    enabled: bool,
    source: TrendingSourceInfo,
    snapshots: int,
    last_snapshot_at: int | None,
    last_error: str = "",
    tracked: int = 0,
    now: int,
    stale_after_seconds: int = 600,
) -> TrendingLaneHealth:
    """Never report ACTIVE just because a class was constructed (section 34)."""

    def build(state: str) -> TrendingLaneHealth:
        return TrendingLaneHealth(
            state=state,
            source=source,
            snapshots=snapshots,
            last_snapshot_at=last_snapshot_at,
            last_error=last_error,
            tracked=tracked,
        )

    if not source.configured:
        return build(HEALTH_NO_SOURCE)
    if not enabled:
        return build(HEALTH_DISABLED)
    if snapshots <= 0:
        return build(HEALTH_DEGRADED if last_error else HEALTH_ACTIVE_NO_EVENTS)
    if last_snapshot_at is not None and now - last_snapshot_at > stale_after_seconds:
        return build(HEALTH_STALE)
    if last_error:
        return build(HEALTH_DEGRADED)
    if tracked <= 0:
        return build(HEALTH_ACTIVE_NO_EVENTS)
    return build(HEALTH_ACTIVE)
