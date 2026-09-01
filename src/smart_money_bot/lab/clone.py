"""Telling an original from a copy of it, before the operator is interrupted.

The production failure, exactly.  Two cards arrived minutes apart, both titled
**Sock and Pussy 500 · $SNP500**, both "DISCOVERED VIA GMGN Trending", both with
``Symbol collision: NO`` printed on them:

    J8GLnJ…pump   first seen $14.41K → alerted $27.15K   liq $12.08K   399/334
    3DV5zV…fXUjp  first seen  $9.87K → alerted $40.71K   liq $15.18K   450/438

One of those went on to $789K with 3,000 holders.  The other was a copy riding
its name.  The operator had no way to tell which was which, because the bot did
not tell them there were two.

Two things had gone wrong and only one of them is about detection.  The symbol
collision check read tables that GMGN-discovered tokens are never written to, so
it answered "NO" to a question it could not see.  That is fixed elsewhere.  This
module is the second half: **once you know two live mints share a name, which
one is the original?**

The honest answer at sixty seconds old is: market data barely separates them.
Liquidity $15.18K against $12.08K, 450 buys against 399 — nothing there is a
verdict.  What *does* separate them is order and provenance:

* **Who was here first.**  A copy is a copy because it came after.  Chain time,
  not the time we happened to look.
* **Whose money is deeper.**  Liquidity and real volume follow the original,
  usually within minutes, because that is where the attention already is.
* **Whose fees are being paid.**  Fee velocity is money actually moving now,
  which is much harder to fake than a market cap.

So the classification is deliberately conservative: it names an **original**
only when the evidence is ordered and material, names a **suspected clone** on
the same basis, and returns UNKNOWN whenever the two are genuinely too close to
call — because an operator told "we cannot tell these apart, here are both" is
better served than one told a coin flip with confidence.

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")

# --- verdicts ----------------------------------------------------------------
#: This mint is the earliest and deepest of the group.  Safe to treat normally.
ORIGINAL = "ORIGINAL"
#: A later arrival trading on someone else's name.  It never pings.
SUSPECTED_CLONE = "SUSPECTED_CLONE"
#: More than one live token, and no honest basis to rank them.  Both are shown,
#: neither is promoted.  This is a real answer, not a failure to produce one.
AMBIGUOUS = "AMBIGUOUS_COLLISION"
#: Nothing else answers to this name.
UNIQUE = "UNIQUE"

VERDICTS: tuple[str, ...] = (ORIGINAL, SUSPECTED_CLONE, AMBIGUOUS, UNIQUE)

#: Verdicts that may interrupt a human.  A suspected clone and an unresolvable
#: collision both stay on the radar where they can be read, not pinged.
PINGABLE_VERDICTS: frozenset[str] = frozenset({ORIGINAL, UNIQUE})

HUMAN_VERDICT: dict[str, str] = {
    ORIGINAL: "earliest and deepest token using this name",
    SUSPECTED_CLONE: "a later token trading on an existing name",
    AMBIGUOUS: "several live tokens share this name and none is clearly the original",
    UNIQUE: "no other live token uses this name",
}


def normalise(value: object) -> str:
    """Fold a name or ticker for comparison only.  Never for resolution.

    Copies rarely reuse a name byte for byte — ``Sock and Pussy 500`` against
    ``Sock And Pussy 500``, ``$SNP500`` against ``$SNP-500``.  Case, spacing and
    punctuation are stripped so those collapse together, and the result is used
    *only* to group mints for comparison.  Nothing in this codebase ever
    resolves a token from one (v2.43.1).
    """

    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


@dataclass(frozen=True, slots=True)
class TokenFacts:
    """What we know about one candidate in a same-name group.

    Every measurement is optional, because a provider that did not answer must
    not read as a token with no liquidity — that conflation is how a degraded
    feed starts looking like a rug.
    """

    mint: str
    name: str = ""
    symbol: str = ""
    #: Chain creation time where known, else first observation.  Order matters
    #: more than any single measurement here.
    created_at: int | None = None
    first_seen_at: int | None = None
    age_seconds: int | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    holder_count: int | None = None
    buys: int | None = None
    sells: int | None = None
    #: Cumulative fees paid, in SOL.  Money actually moving, and much harder to
    #: fake than a market cap.
    total_fee_sol: Decimal | None = None

    # ---- direction (v2.48) ------------------------------------------------
    # v2.47 scored levels only, and levels cannot tell a run from a rug: a
    # token down 99.8% with 3,400 sells against 252 buys earned *full* marks
    # on volume, depth ratio and transactions, because a dump generates all
    # three.  These are the fields that say which way it is going.
    #:
    #: Percent change over the last minute / five minutes, signed.
    price_change_1m_percent: Decimal | None = None
    price_change_5m_percent: Decimal | None = None
    #: The highest market cap this token has ever held.  A mint at $61K whose
    #: high was $222K is not an entry, it is someone else's exit — this is the
    #: "ATH MC" column the operator reads on every board they use.
    ath_market_cap_usd: Decimal | None = None
    #: Provider risk rates, 0..1 where supplied.
    top10_holder_rate: Decimal | None = None
    dev_hold_rate: Decimal | None = None
    bundler_rate: Decimal | None = None
    sniper_hold_rate: Decimal | None = None
    insider_rate: Decimal | None = None

    @property
    def identity_key(self) -> str:
        """Name and ticker folded together, for grouping only."""

        return f"{normalise(self.name)}|{normalise(self.symbol)}"

    @property
    def birth(self) -> int | None:
        """The earliest moment we can defend as this token's start."""

        if self.created_at is not None:
            return self.created_at
        return self.first_seen_at

    @property
    def fee_velocity_sol_per_minute(self) -> Decimal | None:
        """Fees per minute of life (section 37).

        0.5 SOL in two minutes and 0.5 SOL in four hours are different tokens
        wearing the same number, so the rate is what gets compared — never the
        total.
        """

        if self.total_fee_sol is None or not self.age_seconds or self.age_seconds <= 0:
            return None
        return (self.total_fee_sol * Decimal(60) / Decimal(self.age_seconds)).quantize(
            Decimal("0.0001")
        )

    @property
    def drawdown_from_ath(self) -> Decimal | None:
        """How far below its own high this token is trading, as 0..1.

        The single cheapest way to tell "this is running" from "this already
        ran".  ``None`` when we have no high to compare against — unknown, not
        zero, because zero would read as a token sitting at its peak.
        """

        if (
            self.ath_market_cap_usd is None
            or self.market_cap_usd is None
            or self.ath_market_cap_usd <= ZERO
        ):
            return None
        if self.market_cap_usd >= self.ath_market_cap_usd:
            return ZERO
        return (
            (self.ath_market_cap_usd - self.market_cap_usd) / self.ath_market_cap_usd
        ).quantize(Decimal("0.0001"))

    @property
    def sell_pressure(self) -> Decimal | None:
        """Sells per buy.  Above 1 means more people leaving than arriving.

        Counted rather than inferred from price, because price can be held up
        by one buyer while everyone else is getting out.
        """

        if self.buys is None or self.sells is None:
            return None
        if self.buys <= 0:
            return Decimal(self.sells) if self.sells else None
        return (Decimal(self.sells) / Decimal(self.buys)).quantize(CENT)

    @property
    def momentum_percent(self) -> Decimal | None:
        """The freshest signed move we have.  Minute first, then five minutes.

        The operator's words: *"if a new coin that just came out is moving and
        it's actually real"*.  Movement is a rate, and the shortest window we
        hold is the one closest to now.
        """

        if self.price_change_1m_percent is not None:
            return self.price_change_1m_percent
        return self.price_change_5m_percent

    @property
    def volume_to_liquidity(self) -> Decimal | None:
        if self.volume_usd is None or not self.liquidity_usd or self.liquidity_usd <= ZERO:
            return None
        return (self.volume_usd / self.liquidity_usd).quantize(CENT)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "created_at": self.created_at,
            "first_seen_at": self.first_seen_at,
            "age_seconds": self.age_seconds,
            "liquidity_usd": _s(self.liquidity_usd),
            "volume_usd": _s(self.volume_usd),
            "market_cap_usd": _s(self.market_cap_usd),
            "holder_count": self.holder_count,
            "buys": self.buys,
            "sells": self.sells,
            "total_fee_sol": _s(self.total_fee_sol),
            "fee_velocity_sol_per_minute": _s(self.fee_velocity_sol_per_minute),
            "volume_to_liquidity": _s(self.volume_to_liquidity),
            "price_change_1m_percent": _s(self.price_change_1m_percent),
            "price_change_5m_percent": _s(self.price_change_5m_percent),
            "ath_market_cap_usd": _s(self.ath_market_cap_usd),
            "drawdown_from_ath": _s(self.drawdown_from_ath),
            "sell_pressure": _s(self.sell_pressure),
            "momentum_percent": _s(self.momentum_percent),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class CloneConfig:
    """How much earlier, and how much deeper, before we will call it."""

    #: A token this many seconds older than the rest is meaningfully first.
    #: Below it, two launches are simply close together and order proves little.
    older_by_seconds: int = 45
    #: The leader must hold this multiple of the runner-up's liquidity before
    #: depth counts as evidence rather than noise.
    liquidity_multiple: Decimal = Decimal("1.5")
    #: Same, for real traded volume.
    volume_multiple: Decimal = Decimal("1.5")
    #: And for fee velocity, which is the hardest of the three to fake.
    fee_velocity_multiple: Decimal = Decimal("1.5")
    #: Independent lines of evidence needed to name an original at all.
    min_evidence: int = 2


DEFAULT_CLONE_CONFIG = CloneConfig()


@dataclass(frozen=True, slots=True)
class CloneVerdict:
    """What this mint is, relative to everything else using its name."""

    mint: str
    verdict: str = UNIQUE
    peers: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)
    #: The mint this module believes came first, when it believes anything.
    leader_mint: str = ""
    evidence_count: int = 0

    @property
    def collision(self) -> bool:
        return bool(self.peers)

    @property
    def may_ping(self) -> bool:
        """Only an original, or a token nobody is imitating, interrupts anyone."""

        return self.verdict in PINGABLE_VERDICTS

    @property
    def suspected_clone(self) -> bool:
        return self.verdict == SUSPECTED_CLONE

    def human(self) -> str:
        return HUMAN_VERDICT.get(self.verdict, self.verdict)

    def warning_line(self) -> str:
        if not self.collision:
            return ""
        others = ", ".join(item[:8] + "…" for item in self.peers[:3])
        if self.verdict == SUSPECTED_CLONE:
            return (
                f"⚠ SUSPECTED COPY — {len(self.peers)} other live token(s) use this "
                f"name and one of them came first ({self.leader_mint[:8]}…). "
                f"Others: {others}"
            )
        if self.verdict == AMBIGUOUS:
            return (
                f"⚠ NAME COLLISION — {len(self.peers) + 1} live tokens share this "
                f"name and none is clearly the original. Others: {others}"
            )
        return (
            f"⚠ {len(self.peers)} copy/copies of this name exist. This card is the "
            f"earliest and deepest of them. Others: {others}"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "verdict": self.verdict,
            "human": self.human(),
            "peers": list(self.peers),
            "reasons": list(self.reasons),
            "leader_mint": self.leader_mint,
            "evidence_count": self.evidence_count,
            "collision": self.collision,
            "may_ping": self.may_ping,
            "suspected_clone": self.suspected_clone,
            "warning": self.warning_line(),
        }


def _leads(
    left: Decimal | None,
    right: Decimal | None,
    *,
    multiple: Decimal,
) -> bool:
    """Whether ``left`` beats ``right`` by enough to mean something."""

    if left is None or right is None:
        return False
    if right <= ZERO:
        return left > ZERO
    return left >= right * multiple


def classify_clone(
    subject: TokenFacts,
    peers: Sequence[TokenFacts],
    *,
    config: CloneConfig = DEFAULT_CLONE_CONFIG,
) -> CloneVerdict:
    """Decide what ``subject`` is, relative to other tokens sharing its name.

    Peers are matched on the folded name/ticker and never on the mint, because
    the whole point is that the addresses differ.  The subject's own mint is
    excluded even if a caller passes it in.
    """

    key = subject.identity_key
    if key in {"|", ""}:
        # No name and no ticker: nothing to collide with, and nothing to compare.
        return CloneVerdict(mint=subject.mint, verdict=UNIQUE)

    group = [
        item
        for item in peers
        if item.mint and item.mint != subject.mint and item.identity_key == key
    ]
    if not group:
        return CloneVerdict(mint=subject.mint, verdict=UNIQUE)

    peer_mints = tuple(sorted({item.mint for item in group}))
    everyone = [subject, *group]

    # --- who was first ----------------------------------------------------
    births = {item.mint: item.birth for item in everyone if item.birth is not None}
    leader_mint = ""
    ordered = False
    if len(births) == len(everyone) and len(set(births.values())) > 1:
        earliest_mint = min(births, key=lambda mint: births[mint])
        others = [value for mint, value in births.items() if mint != earliest_mint]
        if others and min(others) - births[earliest_mint] >= config.older_by_seconds:
            leader_mint = earliest_mint
            ordered = True

    # --- who is deepest ---------------------------------------------------
    best_peer_liquidity = _best(item.liquidity_usd for item in group)
    best_peer_volume = _best(item.volume_usd for item in group)
    best_peer_fee = _best(item.fee_velocity_sol_per_minute for item in group)

    reasons: list[str] = []
    evidence = 0
    if ordered and leader_mint == subject.mint:
        evidence += 1
        reasons.append("this mint existed before the others using the name")
    if _leads(subject.liquidity_usd, best_peer_liquidity, multiple=config.liquidity_multiple):
        evidence += 1
        reasons.append("materially deeper liquidity than the copies")
    if _leads(subject.volume_usd, best_peer_volume, multiple=config.volume_multiple):
        evidence += 1
        reasons.append("materially more traded volume than the copies")
    if _leads(
        subject.fee_velocity_sol_per_minute,
        best_peer_fee,
        multiple=config.fee_velocity_multiple,
    ):
        evidence += 1
        reasons.append("fees are being paid here faster than on the copies")

    if ordered and leader_mint and leader_mint != subject.mint:
        # Someone else demonstrably came first.  That alone is enough to stop
        # this one interrupting anybody: being second with the same name is the
        # definition of the thing we are protecting against.
        return CloneVerdict(
            mint=subject.mint,
            verdict=SUSPECTED_CLONE,
            peers=peer_mints,
            leader_mint=leader_mint,
            evidence_count=evidence,
            reasons=(
                f"{leader_mint[:8]}… was using this name first",
                *reasons,
            ),
        )

    if evidence >= config.min_evidence:
        return CloneVerdict(
            mint=subject.mint,
            verdict=ORIGINAL,
            peers=peer_mints,
            leader_mint=subject.mint,
            evidence_count=evidence,
            reasons=tuple(reasons),
        )

    # Genuinely too close to call.  Say so — at sixty seconds old two launches
    # really can be indistinguishable, and pretending otherwise is worse than
    # admitting it.
    return CloneVerdict(
        mint=subject.mint,
        verdict=AMBIGUOUS,
        peers=peer_mints,
        leader_mint=leader_mint,
        evidence_count=evidence,
        reasons=(
            *reasons,
            "not enough separation to name an original yet",
        ),
    )


def _best(values) -> Decimal | None:
    present = [item for item in values if item is not None]
    return max(present) if present else None


def group_by_identity(tokens: Sequence[TokenFacts]) -> dict[str, tuple[TokenFacts, ...]]:
    """Bucket tokens by folded name/ticker, for the collision panel."""

    grouped: dict[str, list[TokenFacts]] = {}
    for item in tokens:
        key = item.identity_key
        if key in {"|", ""}:
            continue
        grouped.setdefault(key, []).append(item)
    return {
        key: tuple(sorted(rows, key=lambda row: (row.birth or 0, row.mint)))
        for key, rows in grouped.items()
        if len(rows) > 1
    }
