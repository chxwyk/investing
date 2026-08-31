"""Reading the theses people post about a token, and deciding what they are worth.

A thesis is an *opinion with a timestamp*.  That makes it useful in exactly two
ways: it can be specific enough to check, and it can have been posted before the
move rather than after it.  Everything else about it — how confident it sounds,
how many likes it has, how smart the author seems — is decoration.

So this module grades theses on things that are actually falsifiable:

* **Specificity** — a checkable claim beats "this is going to run" (section 21).
* **Timing** — posted before the move, or explaining one that already happened
  (sections 21, 24).  A hindsight thesis is not alpha.
* **Exact-mint provenance** — a thesis about a *different* mint with the same
  name carries no weight here at all (sections 13, 99).
* **Independence** — three copies of one post are one information source; three
  analysts reaching the same conclusion separately are three (section 26).
* **Author forward record** — measured from what happened after their past
  theses, never from their follower count (section 25).

And it penalises the specific failure patterns the operator named: generic moon
posts, copy-paste, developer self-promotion, circular sourcing, claims lifted
from another mint, unsupported insider assertions, and post-move hindsight
(section 22).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- what kind of thesis is this? (section 20) -------------------------------
THESIS_AI_PROJECT = "AI_PROJECT"
THESIS_REAL_WORLD = "REAL_WORLD_STORY"
THESIS_MEME = "MEME_CULTURE"
THESIS_CATALYST = "CATALYST"
THESIS_PRODUCT = "PRODUCT"
THESIS_COMMUNITY = "COMMUNITY"
THESIS_SMART_MONEY = "SMART_MONEY"
THESIS_TECHNICAL = "TECHNICAL_MARKET"
THESIS_PURE_HYPE = "PURE_HYPE"
THESIS_INSIDER_CLAIM = "UNSUPPORTED_INSIDER_CLAIM"
THESIS_OTHER = "OTHER"

THESIS_CATEGORIES: tuple[str, ...] = (
    THESIS_AI_PROJECT,
    THESIS_REAL_WORLD,
    THESIS_MEME,
    THESIS_CATALYST,
    THESIS_PRODUCT,
    THESIS_COMMUNITY,
    THESIS_SMART_MONEY,
    THESIS_TECHNICAL,
    THESIS_PURE_HYPE,
    THESIS_INSIDER_CLAIM,
    THESIS_OTHER,
)

# --- how good is it? (section 21) --------------------------------------------
QUALITY_NOISE = "NOISE"
QUALITY_SPECULATIVE = "SPECULATIVE"
QUALITY_PLAUSIBLE = "PLAUSIBLE"
QUALITY_SUPPORTED = "SUPPORTED"
QUALITY_STRONG = "STRONG"

QUALITY_GRADES: tuple[str, ...] = (
    QUALITY_NOISE,
    QUALITY_SPECULATIVE,
    QUALITY_PLAUSIBLE,
    QUALITY_SUPPORTED,
    QUALITY_STRONG,
)

QUALITY_RANK: dict[str, int] = {grade: index for index, grade in enumerate(QUALITY_GRADES)}

#: Grades that may contribute to an urgent alert's named reason.
SERIOUS_QUALITIES: frozenset[str] = frozenset({QUALITY_SUPPORTED, QUALITY_STRONG})

# --- when was it posted, relative to the move? (section 24) ------------------
TIMING_EARLY = "EARLY"
TIMING_TIMELY = "TIMELY"
TIMING_LATE = "LATE"
TIMING_EDGE_CONSUMED = "EDGE_CONSUMED"

# --- penalties (section 22) --------------------------------------------------
PENALTY_GENERIC = "GENERIC_HYPE"
PENALTY_COPIED = "COPIED_TEXT"
PENALTY_DEV_PROMO = "DEVELOPER_SELF_PROMOTION"
PENALTY_CIRCULAR = "CIRCULAR_SOURCING"
PENALTY_WRONG_MINT = "CLAIM_COPIED_FROM_ANOTHER_MINT"
PENALTY_INSIDER = "UNSUPPORTED_INSIDER_CLAIM"
PENALTY_HINDSIGHT = "POST_MOVE_HINDSIGHT"
PENALTY_FAKE_OFFICIAL = "FAKE_OFFICIAL_CLAIM"

_GENERIC_PHRASES: tuple[str, ...] = (
    "to the moon",
    "moon",
    "lfg",
    "send it",
    "ape in",
    "next 100x",
    "100x",
    "1000x",
    "easy money",
    "free money",
    "dont miss",
    "don't miss",
    "last chance",
    "gem",
    "early gem",
    "buy now",
    "this is the one",
    "trust me",
    "wagmi",
)

_INSIDER_PHRASES: tuple[str, ...] = (
    "insider",
    "i have inside",
    "my source",
    "leaked",
    "private group",
    "alpha group said",
    "dev told me",
    "team told me",
    "confirmed by a friend",
)

_DEV_PROMO_PHRASES: tuple[str, ...] = (
    "our token",
    "we are launching",
    "we just launched",
    "join our",
    "our community",
    "buy our",
    "dev here",
    "team here",
)

_OFFICIAL_PHRASES: tuple[str, ...] = (
    "officially partnered",
    "official partnership",
    "backed by",
    "endorsed by",
    "official token of",
)

_CATEGORY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (THESIS_SMART_MONEY, ("wallet", "whale", "smart money", "accumulating", "bought",
                          "insider wallet", "top trader")),
    (THESIS_AI_PROJECT, ("ai ", " ai", "agent", "llm", "model", "neural", "machine learning",
                         "artificial intelligence")),
    (THESIS_CATALYST, ("listing", "listed on", "announcement", "launch date", "airdrop",
                       "partnership", "cex", "binance", "coinbase", "upgrade")),
    (THESIS_PRODUCT, ("product", "app", "beta", "shipped", "release", "demo", "protocol",
                      "testnet", "mainnet")),
    (THESIS_REAL_WORLD, ("news", "story", "rescue", "viral video", "event", "real world",
                         "happened", "reported")),
    (THESIS_MEME, ("meme", "culture", "dog", "cat", "frog", "pepe", "funny", "vibe")),
    (THESIS_COMMUNITY, ("community", "cto", "holders", "discord", "telegram", "grassroots")),
    (THESIS_TECHNICAL, ("chart", "support", "resistance", "breakout", "consolidat",
                        "volume", "liquidity", "market cap", "retrace")),
)

_SPECIFIC_MARKERS = re.compile(
    r"(https?://\S+)"           # a link to check
    r"|(\b\d{1,2}[:/]\d{2}\b)"  # a time
    r"|(\b\d+(?:\.\d+)?\s?[km]\b)"  # a size
    r"|(\b\d{1,3}(?:,\d{3})+\b)"    # a formatted number
    r"|(\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}\b)",  # a date
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"[a-z0-9']+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ThesisRecord:
    """One publicly posted thesis about one exact mint.

    ``mint`` is the exact mint the thesis was recorded against.  If a thesis was
    written about a different mint that merely shares a name, it does not belong
    in this record at all — see :func:`reject_cross_mint`.
    """

    mint: str
    author: str
    posted_at: int
    text: str
    #: Where it came from, e.g. ``"fomo_theses"`` or ``"public_social"``.
    source: str = ""
    direction: str = ""
    market_cap_at_thesis_usd: Decimal | None = None
    #: Engagement is recorded only when the source honestly supplies it
    #: (section 33).  ``None`` means "not available", never zero.
    likes: int | None = None
    views: int | None = None
    #: True when the author is the token's creator or a known promoter of it.
    author_is_creator: bool = False
    #: Set when the same text was seen on another mint first (section 22).
    seen_on_other_mint: str = ""

    @property
    def thesis_id(self) -> str:
        return f"{self.mint}:{self.author}:{self.posted_at}"


@dataclass(frozen=True, slots=True)
class AuthorReputation:
    """A forward-measured record.  Popularity is not skill (section 25)."""

    author: str
    sample: int = 0
    avg_forward_move_percent: Decimal | None = None
    avg_mfe_percent: Decimal | None = None
    avg_mae_percent: Decimal | None = None
    severe_failures: int = 0
    rug_exposures: int = 0
    late_theses: int = 0

    @property
    def credible(self) -> bool:
        """Only a real forward sample with a positive record counts."""

        return (
            self.sample >= 5
            and self.avg_forward_move_percent is not None
            and self.avg_forward_move_percent > ZERO
            and self.severe_failure_rate < Decimal("0.35")
        )

    @property
    def severe_failure_rate(self) -> Decimal:
        if self.sample <= 0:
            return ZERO
        return Decimal(self.severe_failures) / Decimal(self.sample)

    @property
    def lateness_rate(self) -> Decimal:
        if self.sample <= 0:
            return ZERO
        return Decimal(self.late_theses) / Decimal(self.sample)

    def to_json(self) -> dict[str, object]:
        return {
            "author": self.author,
            "sample": self.sample,
            "avg_forward_move_percent": _s(self.avg_forward_move_percent),
            "avg_mfe_percent": _s(self.avg_mfe_percent),
            "avg_mae_percent": _s(self.avg_mae_percent),
            "severe_failures": self.severe_failures,
            "rug_exposures": self.rug_exposures,
            "late_theses": self.late_theses,
            "credible": self.credible,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class ThesisAssessment:
    """A graded thesis: what it claims, how good it is, and when it arrived."""

    record: ThesisRecord
    category: str = THESIS_OTHER
    quality: str = QUALITY_NOISE
    timing: str = TIMING_TIMELY
    specificity: int = 0
    penalties: tuple[str, ...] = ()
    corroborated: bool = False
    #: Identifier of the cluster of near-identical theses this belongs to.
    cluster_id: str = ""
    #: True when this is the first (earliest) member of its cluster.
    cluster_leader: bool = True
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def serious(self) -> bool:
        """May this thesis be a named reason for interrupting a human?"""

        return (
            self.quality in SERIOUS_QUALITIES
            and self.timing in {TIMING_EARLY, TIMING_TIMELY}
            and not self.penalties
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.record.mint,
            "author": self.record.author,
            "posted_at": self.record.posted_at,
            "source": self.record.source,
            "category": self.category,
            "quality": self.quality,
            "timing": self.timing,
            "specificity": self.specificity,
            "penalties": list(self.penalties),
            "corroborated": self.corroborated,
            "cluster_id": self.cluster_id,
            "cluster_leader": self.cluster_leader,
            "reasons": list(self.reasons),
            "market_cap_at_thesis_usd": _s(self.record.market_cap_at_thesis_usd),
            "likes": self.record.likes,
            "views": self.record.views,
        }


def reject_cross_mint(record: ThesisRecord, mint: str) -> bool:
    """True when this thesis must not be attached to ``mint`` (sections 13, 99).

    Evidence never crosses mints.  A same-name, same-story, different-mint token
    inherits nothing — not a thesis, not a story, not a wallet event.
    """

    return record.mint != mint


def classify_thesis(text: str) -> str:
    folded = f" {(text or '').casefold()} "
    if any(phrase in folded for phrase in _INSIDER_PHRASES):
        return THESIS_INSIDER_CLAIM
    for category, terms in _CATEGORY_TERMS:
        if any(term in folded for term in terms):
            return category
    if any(phrase in folded for phrase in _GENERIC_PHRASES):
        return THESIS_PURE_HYPE
    return THESIS_OTHER


def _normalise(text: str) -> tuple[str, ...]:
    stripped = _URL_RE.sub(" ", (text or "").casefold())
    return tuple(_WORD_RE.findall(stripped))


def _similarity(left: Sequence[str], right: Sequence[str]) -> Decimal:
    if not left or not right:
        return ZERO
    left_set, right_set = set(left), set(right)
    overlap = len(left_set & right_set)
    union = len(left_set | right_set)
    if union == 0:
        return ZERO
    return (Decimal(overlap) / Decimal(union)).quantize(Decimal("0.001"))


def score_specificity(text: str) -> int:
    """How checkable is this?  0-4.  Length is not specificity."""

    body = text or ""
    score = 0
    matches = _SPECIFIC_MARKERS.findall(body)
    score += min(2, len(matches))
    words = _normalise(body)
    if len(words) >= 25:
        score += 1
    folded = f" {body.casefold()} "
    if not any(phrase in folded for phrase in _GENERIC_PHRASES):
        score += 1
    return min(4, score)


def classify_timing(
    record: ThesisRecord,
    *,
    current_market_cap_usd: Decimal | None,
    edge_narrowing_percent: Decimal = Decimal("35"),
    edge_consumed_percent: Decimal = Decimal("80"),
) -> str:
    """Was the thesis posted before the move, during it, or after it?"""

    thesis_mc = record.market_cap_at_thesis_usd
    if thesis_mc is None or current_market_cap_usd is None or thesis_mc <= ZERO:
        return TIMING_TIMELY
    move = (current_market_cap_usd - thesis_mc) / thesis_mc * HUNDRED
    if move >= edge_consumed_percent:
        # The thesis called it and the move has since run: the *thesis* was
        # early, but acting on it now is not.
        return TIMING_EDGE_CONSUMED
    if move >= edge_narrowing_percent:
        return TIMING_EARLY
    if move <= -edge_narrowing_percent:
        return TIMING_LATE
    return TIMING_TIMELY


def detect_penalties(
    record: ThesisRecord,
    *,
    market_cap_at_move_start_usd: Decimal | None = None,
    corroborating_sources: int = 0,
    duplicate_of: str = "",
) -> tuple[str, ...]:
    """The bullshit filter (section 22)."""

    penalties: list[str] = []
    folded = f" {(record.text or '').casefold()} "

    if any(phrase in folded for phrase in _GENERIC_PHRASES) and score_specificity(record.text) <= 1:
        penalties.append(PENALTY_GENERIC)
    if duplicate_of:
        penalties.append(PENALTY_COPIED)
    if record.author_is_creator or any(phrase in folded for phrase in _DEV_PROMO_PHRASES):
        penalties.append(PENALTY_DEV_PROMO)
    if record.seen_on_other_mint:
        penalties.append(PENALTY_WRONG_MINT)
    if any(phrase in folded for phrase in _INSIDER_PHRASES) and corroborating_sources <= 0:
        penalties.append(PENALTY_INSIDER)
    if any(phrase in folded for phrase in _OFFICIAL_PHRASES) and corroborating_sources <= 0:
        penalties.append(PENALTY_FAKE_OFFICIAL)
    # A thesis posted after the move had already happened is an explanation, not
    # a prediction.
    if (
        market_cap_at_move_start_usd is not None
        and record.market_cap_at_thesis_usd is not None
        and market_cap_at_move_start_usd > ZERO
        and record.market_cap_at_thesis_usd
        >= market_cap_at_move_start_usd * Decimal("1.8")
    ):
        penalties.append(PENALTY_HINDSIGHT)
    # A source that only cites other posts about the same token is circular.
    if record.source and record.source.casefold() in {"repost", "aggregator", "echo"}:
        penalties.append(PENALTY_CIRCULAR)
    return tuple(dict.fromkeys(penalties))


def grade_thesis(
    record: ThesisRecord,
    *,
    current_market_cap_usd: Decimal | None = None,
    market_cap_at_move_start_usd: Decimal | None = None,
    corroborating_sources: int = 0,
    externally_supported: bool = False,
    author: AuthorReputation | None = None,
    duplicate_of: str = "",
    cluster_id: str = "",
    cluster_leader: bool = True,
) -> ThesisAssessment:
    """Grade one thesis.  Sounding smart is not evidence."""

    reasons: list[str] = []
    category = classify_thesis(record.text)
    specificity = score_specificity(record.text)
    timing = classify_timing(record, current_market_cap_usd=current_market_cap_usd)
    penalties = detect_penalties(
        record,
        market_cap_at_move_start_usd=market_cap_at_move_start_usd,
        corroborating_sources=corroborating_sources,
        duplicate_of=duplicate_of,
    )

    # Start from what is checkable, then apply corroboration, then penalise.
    if specificity >= 3:
        grade = QUALITY_PLAUSIBLE
        reasons.append("specific, checkable claim")
    elif specificity == 2:
        grade = QUALITY_SPECULATIVE
        reasons.append("some specifics, mostly unverified")
    else:
        grade = QUALITY_NOISE
        reasons.append("no checkable content")

    if externally_supported and specificity >= 2:
        grade = QUALITY_SUPPORTED
        reasons.append("corroborated by an independent external source")
    if corroborating_sources >= 2 and grade == QUALITY_SUPPORTED:
        grade = QUALITY_STRONG
        reasons.append(f"{corroborating_sources} independent sources agree")

    if author is not None and author.credible and grade != QUALITY_NOISE:
        grade = QUALITY_GRADES[min(len(QUALITY_GRADES) - 1, QUALITY_RANK[grade] + 1)]
        reasons.append(
            f"author has a positive forward record over {author.sample} prior theses"
        )
    elif author is not None and author.sample >= 5 and not author.credible:
        grade = QUALITY_GRADES[max(0, QUALITY_RANK[grade] - 1)]
        reasons.append(f"author's prior {author.sample} theses did not work out")

    # A copy of somebody else's post is not a second data point.
    if not cluster_leader:
        grade = QUALITY_GRADES[max(0, QUALITY_RANK[grade] - 1)]
        reasons.append("repeats an earlier thesis — one information source, not two")

    for penalty in penalties:
        grade = QUALITY_GRADES[max(0, QUALITY_RANK[grade] - 1)]
        reasons.append(f"penalty: {penalty}")

    if category in {THESIS_PURE_HYPE, THESIS_INSIDER_CLAIM}:
        grade = QUALITY_GRADES[min(QUALITY_RANK[grade], QUALITY_RANK[QUALITY_SPECULATIVE])]
        reasons.append(f"{category} can never grade above SPECULATIVE on its own")

    if timing == TIMING_EDGE_CONSUMED:
        reasons.append("the move this thesis called has already happened")

    return ThesisAssessment(
        record=record,
        category=category,
        quality=grade,
        timing=timing,
        specificity=specificity,
        penalties=penalties,
        corroborated=externally_supported or corroborating_sources > 0,
        cluster_id=cluster_id or record.thesis_id,
        cluster_leader=cluster_leader,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class ThesisCluster:
    cluster_id: str
    members: tuple[str, ...]
    leader: str


def cluster_theses(
    records: Sequence[ThesisRecord],
    *,
    similarity_threshold: Decimal = Decimal("0.7"),
) -> tuple[ThesisCluster, ...]:
    """Group near-identical theses so copies count once (section 26).

    The earliest post in a cluster is its leader; every later near-duplicate is a
    follower and is graded down, because agreement by copy-paste is not
    independent agreement.
    """

    ordered = sorted(records, key=lambda item: (item.posted_at, item.author))
    clusters: list[list[ThesisRecord]] = []
    tokenised: list[tuple[str, ...]] = []
    for record in ordered:
        words = _normalise(record.text)
        placed = False
        for index, cluster in enumerate(clusters):
            if _similarity(words, tokenised[index]) >= similarity_threshold:
                cluster.append(record)
                placed = True
                break
        if not placed:
            clusters.append([record])
            tokenised.append(words)
    return tuple(
        ThesisCluster(
            cluster_id=cluster[0].thesis_id,
            members=tuple(item.thesis_id for item in cluster),
            leader=cluster[0].thesis_id,
        )
        for cluster in clusters
    )


@dataclass(frozen=True, slots=True)
class ThesisPanel:
    """Everything the operator card needs to say about a token's theses."""

    mint: str
    total: int = 0
    independent_sources: int = 0
    supported: int = 0
    speculative: int = 0
    noise: int = 0
    strongest: ThesisAssessment | None = None
    assessments: tuple[ThesisAssessment, ...] = ()

    @property
    def has_serious_thesis(self) -> bool:
        return any(item.serious for item in self.assessments)

    def summary_line(self) -> str:
        if not self.total:
            return "no theses found"
        return (
            f"{self.total} total • {self.independent_sources} independent • "
            f"{self.supported} supported • {self.speculative} speculative • {self.noise} noise"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "total": self.total,
            "independent_sources": self.independent_sources,
            "supported": self.supported,
            "speculative": self.speculative,
            "noise": self.noise,
            "has_serious_thesis": self.has_serious_thesis,
            "strongest": self.strongest.to_json() if self.strongest else None,
            "assessments": [item.to_json() for item in self.assessments],
        }


def build_thesis_panel(
    mint: str,
    records: Sequence[ThesisRecord],
    *,
    current_market_cap_usd: Decimal | None = None,
    market_cap_at_move_start_usd: Decimal | None = None,
    externally_supported_ids: frozenset[str] = frozenset(),
    corroborating_sources: dict[str, int] | None = None,
    authors: dict[str, AuthorReputation] | None = None,
) -> ThesisPanel:
    """Grade every thesis for one exact mint and summarise the result.

    Records for any other mint are dropped outright rather than "matched
    loosely" — a same-name token's thesis is not this token's evidence.
    """

    own = [record for record in records if not reject_cross_mint(record, mint)]
    if not own:
        return ThesisPanel(mint=mint)

    clusters = cluster_theses(own)
    leader_of: dict[str, str] = {}
    cluster_of: dict[str, str] = {}
    for cluster in clusters:
        for member in cluster.members:
            cluster_of[member] = cluster.cluster_id
            leader_of[member] = cluster.leader

    sources = corroborating_sources or {}
    reputations = authors or {}
    assessments: list[ThesisAssessment] = []
    for record in own:
        identifier = record.thesis_id
        is_leader = leader_of.get(identifier) == identifier
        assessments.append(
            grade_thesis(
                record,
                current_market_cap_usd=current_market_cap_usd,
                market_cap_at_move_start_usd=market_cap_at_move_start_usd,
                corroborating_sources=sources.get(identifier, 0),
                externally_supported=identifier in externally_supported_ids,
                author=reputations.get(record.author),
                duplicate_of="" if is_leader else leader_of.get(identifier, ""),
                cluster_id=cluster_of.get(identifier, identifier),
                cluster_leader=is_leader,
            )
        )

    assessments.sort(
        key=lambda item: (QUALITY_RANK[item.quality], item.specificity),
        reverse=True,
    )
    supported = sum(1 for item in assessments if item.quality in SERIOUS_QUALITIES)
    speculative = sum(
        1 for item in assessments if item.quality in {QUALITY_SPECULATIVE, QUALITY_PLAUSIBLE}
    )
    noise = sum(1 for item in assessments if item.quality == QUALITY_NOISE)
    return ThesisPanel(
        mint=mint,
        total=len(assessments),
        independent_sources=len({item.cluster_id for item in assessments}),
        supported=supported,
        speculative=speculative,
        noise=noise,
        strongest=assessments[0] if assessments else None,
        assessments=tuple(assessments),
    )


def update_author_reputation(
    current: AuthorReputation,
    *,
    forward_move_percent: Decimal,
    mfe_percent: Decimal | None = None,
    mae_percent: Decimal | None = None,
    severe_failure: bool = False,
    rug_exposure: bool = False,
    late: bool = False,
) -> AuthorReputation:
    """Fold one resolved forward outcome into an author's record (section 25)."""

    sample = current.sample + 1

    def blend(previous: Decimal | None, value: Decimal | None) -> Decimal | None:
        if value is None:
            return previous
        if previous is None:
            return value
        return ((previous * Decimal(current.sample)) + value) / Decimal(sample)

    return AuthorReputation(
        author=current.author,
        sample=sample,
        avg_forward_move_percent=blend(current.avg_forward_move_percent, forward_move_percent),
        avg_mfe_percent=blend(current.avg_mfe_percent, mfe_percent),
        avg_mae_percent=blend(current.avg_mae_percent, mae_percent),
        severe_failures=current.severe_failures + (1 if severe_failure else 0),
        rug_exposures=current.rug_exposures + (1 if rug_exposure else 0),
        late_theses=current.late_theses + (1 if late else 0),
    )
