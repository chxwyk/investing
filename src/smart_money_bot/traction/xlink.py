"""Is that X link a real account, or is it furniture?

A launchpad metadata field called "twitter" accepts any string, so "has an X
link" is a much weaker fact than it looks.  In practice the field contains, in
roughly descending order of usefulness:

* a real account page — the thing the operator's filter is actually asking for,
* a **community** page (``/i/communities/123…``), which is not an account and
  has no owner, follower count or history to judge,
* a **single tweet** (``/status/123…``) — very often someone else's tweet, reused
  to borrow its engagement, which is the specific abuse the operator asked to be
  flagged,
* a search or intent URL, which is a link to a query rather than to anybody,
* a dead or malformed URL.

This module grades that structurally and for free.  It makes **no network call**:
everything below is decided from the URL string plus our own database, which is
what lets it run on the fast path.  Whether a structurally-valid account actually
exists is a different question, costs an X API call, and is answered later by the
enrichment step under the existing X budget — so a valid-looking handle is
reported as ``ACCOUNT_UNVERIFIED``, never as ``ACCOUNT_VERIFIED``.

**Reuse across tokens is the highest-signal check here and it is free.**  One X
account or one tweet attached to six different mints inside an hour is a much
stronger statement about intent than any single token's metadata, and we can see
it because we keep every link we have ever observed.  Reuse is reported, not
filtered on — consistent with the profile's no-safety-gates rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- link classes ------------------------------------------------------------
#: A plausible account page.  Structure only; existence is unverified.
ACCOUNT_UNVERIFIED = "ACCOUNT_UNVERIFIED"
#: An account page confirmed to exist by a live lookup.
ACCOUNT_VERIFIED = "ACCOUNT_VERIFIED"
#: A live lookup said this account does not exist or is suspended.
ACCOUNT_MISSING = "ACCOUNT_MISSING"
#: An ``/i/communities/...`` page: not an account.
COMMUNITY = "COMMUNITY"
#: A link to a single tweet rather than to an account.
SINGLE_TWEET = "SINGLE_TWEET"
#: A search, intent, hashtag or share URL.
NOT_AN_ACCOUNT = "NOT_AN_ACCOUNT"
#: Present but unparseable, or not an X/Twitter host at all.
MALFORMED = "MALFORMED"
#: The field was empty.
ABSENT = "ABSENT"

LINK_CLASSES: tuple[str, ...] = (
    ACCOUNT_UNVERIFIED,
    ACCOUNT_VERIFIED,
    ACCOUNT_MISSING,
    COMMUNITY,
    SINGLE_TWEET,
    NOT_AN_ACCOUNT,
    MALFORMED,
    ABSENT,
)

#: Classes that represent "there is an account behind this link".
ACCOUNT_CLASSES: frozenset[str] = frozenset({ACCOUNT_UNVERIFIED, ACCOUNT_VERIFIED})

#: Reserved paths that are never a user handle.
_RESERVED_HANDLES: frozenset[str] = frozenset(
    {
        "i",
        "home",
        "search",
        "explore",
        "intent",
        "share",
        "hashtag",
        "notifications",
        "messages",
        "settings",
        "login",
        "signup",
        "compose",
        "about",
        "tos",
        "privacy",
        "download",
    }
)

_X_HOSTS: frozenset[str] = frozenset(
    {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
)

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_COMMUNITY_RE = re.compile(r"/i/communities/(\d+)")
_STATUS_RE = re.compile(r"/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)")


@dataclass(frozen=True, slots=True)
class XLinkAssessment:
    """What we can say about one X link without asking X."""

    raw: str
    link_class: str = ABSENT
    #: Lowercased handle, when the link names one.  The reuse join key.
    handle: str = ""
    community_id: str = ""
    tweet_id: str = ""
    detail: str = ""

    @property
    def is_account(self) -> bool:
        return self.link_class in ACCOUNT_CLASSES

    @property
    def usable(self) -> bool:
        """Whether this link satisfies "has a Twitter/X link" honestly.

        A community page and a borrowed tweet are both *present* and neither is
        an account, so they are reported as present-but-not-an-account rather
        than counted as satisfying the filter.
        """

        return self.is_account

    def to_json(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "link_class": self.link_class,
            "handle": self.handle,
            "community_id": self.community_id,
            "tweet_id": self.tweet_id,
            "is_account": self.is_account,
            "usable": self.usable,
            "detail": self.detail,
        }


def _normalise(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("@") and "/" not in text:
        # Bare handle, which several launchpads store instead of a URL.
        return f"https://x.com/{text[1:]}"
    if not text.lower().startswith(("http://", "https://")):
        text = f"https://{text}"
    return text


def assess_link(raw: str) -> XLinkAssessment:
    """Classify an X link from its structure alone.  No network, no database."""

    if not (raw or "").strip():
        return XLinkAssessment(raw="", link_class=ABSENT, detail="no X link supplied")

    normalised = _normalise(raw)
    from urllib.parse import urlparse

    try:
        parsed = urlparse(normalised)
    except ValueError:
        return XLinkAssessment(raw=raw, link_class=MALFORMED, detail="URL did not parse")

    host = (parsed.netloc or "").lower()
    if host not in _X_HOSTS:
        return XLinkAssessment(
            raw=raw,
            link_class=MALFORMED,
            detail=f"host {host or 'missing'!r} is not an X/Twitter host",
        )

    path = parsed.path or "/"

    community = _COMMUNITY_RE.search(path)
    if community:
        return XLinkAssessment(
            raw=raw,
            link_class=COMMUNITY,
            community_id=community.group(1),
            detail=(
                "community page, not an account: it has no owner, follower count "
                "or posting history to judge"
            ),
        )

    status = _STATUS_RE.search(path)
    if status:
        return XLinkAssessment(
            raw=raw,
            link_class=SINGLE_TWEET,
            handle=status.group(1).lower(),
            tweet_id=status.group(2),
            detail=(
                "links to one tweet rather than to an account; check whether the "
                "tweet belongs to this project or was reused for its engagement"
            ),
        )

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return XLinkAssessment(
            raw=raw, link_class=NOT_AN_ACCOUNT, detail="links to the X home page"
        )

    first = segments[0]
    if first.lower() in _RESERVED_HANDLES:
        return XLinkAssessment(
            raw=raw,
            link_class=NOT_AN_ACCOUNT,
            detail=f"/{first} is a reserved X path, not a user handle",
        )
    if not _HANDLE_RE.match(first):
        return XLinkAssessment(
            raw=raw,
            link_class=MALFORMED,
            detail=f"{first!r} is not a valid X handle",
        )

    return XLinkAssessment(
        raw=raw,
        link_class=ACCOUNT_UNVERIFIED,
        handle=first.lower(),
        detail=(
            "structurally a real account page; existence not yet checked "
            "(a live lookup costs X-API budget and runs in enrichment)"
        ),
    )


@dataclass(frozen=True, slots=True)
class ReuseReport:
    """How many other mints carry this same X identity."""

    handle: str = ""
    #: Other mints sharing the handle, most recent first.
    other_mints: tuple[str, ...] = ()
    #: Other mints sharing the exact tweet, when the link is a tweet.
    tweet_other_mints: tuple[str, ...] = ()
    window_seconds: int = 0

    @property
    def reused(self) -> bool:
        return bool(self.other_mints or self.tweet_other_mints)

    @property
    def distinct_mints(self) -> int:
        return len(set(self.other_mints) | set(self.tweet_other_mints))

    @property
    def severity(self) -> str:
        """A plain label.  Observed reuse, not an accusation of anything."""

        count = self.distinct_mints
        if count == 0:
            return "NONE"
        if count == 1:
            return "SEEN_ON_ONE_OTHER_MINT"
        if count < 5:
            return "SEEN_ON_SEVERAL_MINTS"
        return "SEEN_ON_MANY_MINTS"

    def to_json(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "reused": self.reused,
            "distinct_mints": self.distinct_mints,
            "severity": self.severity,
            "other_mints": list(self.other_mints[:10]),
            "tweet_other_mints": list(self.tweet_other_mints[:10]),
            "window_seconds": self.window_seconds,
        }


@dataclass(frozen=True, slots=True)
class XLinkVerdict:
    """The assessment plus reuse, which is how the card renders it."""

    assessment: XLinkAssessment
    reuse: ReuseReport = field(default_factory=ReuseReport)
    #: Set once a live lookup has run.
    followers: int | None = None
    account_age_days: int | None = None
    live_checked: bool = False

    @property
    def summary(self) -> str:
        """One line for the card.  Never claims more than was actually checked."""

        assessment = self.assessment
        if assessment.link_class == ABSENT:
            return "no X link"
        if assessment.link_class == COMMUNITY:
            base = "community page (not an account)"
        elif assessment.link_class == SINGLE_TWEET:
            base = f"single tweet by @{assessment.handle or 'unknown'} (not an account page)"
        elif assessment.link_class == MALFORMED:
            base = "malformed / not an X link"
        elif assessment.link_class == NOT_AN_ACCOUNT:
            base = "not an account link"
        elif assessment.link_class == ACCOUNT_MISSING:
            base = f"@{assessment.handle} — account not found or suspended"
        elif assessment.link_class == ACCOUNT_VERIFIED:
            parts = [f"@{assessment.handle} — exists"]
            if self.followers is not None:
                parts.append(f"{self.followers} followers")
            if self.account_age_days is not None:
                parts.append(f"{self.account_age_days}d old")
            base = ", ".join(parts)
        else:
            base = f"@{assessment.handle} — structure OK, existence unchecked"

        if self.reuse.reused:
            base += (
                f" · ⚠ same X identity seen on {self.reuse.distinct_mints} "
                f"other mint(s)"
            )
        return base

    def to_json(self) -> dict[str, Any]:
        return {
            "assessment": self.assessment.to_json(),
            "reuse": self.reuse.to_json(),
            "followers": self.followers,
            "account_age_days": self.account_age_days,
            "live_checked": self.live_checked,
            "summary": self.summary,
        }
