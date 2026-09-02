"""Which coin owns a stock anchor, and when that genuinely changes.

Stonks Onchain ranks by ``credible value``:

    credible_value = min(FDV, 150 x volume_24h, 500 x liquidity_usd)

The shape of that formula is the interesting part.  FDV alone is trivially
inflated — it is a supply number multiplied by whatever the last trade was, and
the last trade can be a dollar.  The two caps make the headline figure *earn*
itself: a token cannot be credibly worth more than a multiple of what has
actually traded through it, or a multiple of what is actually pooled behind it.
A billion-dollar FDV on $200 of liquidity is worth $100,000 by this measure, and
that is the correct answer.

The other half of this module is knowing when the crown has actually moved.  Two
coins within noise of each other will trade places on every refresh, and alerting
on each swap would produce a stream of notifications describing nothing.  So a
challenger has to beat the incumbent by a configured multiple, and hold it — a
lead that appears and vanishes inside one poll was never a lead.

Grouping is by the anchor's exact contract address, never by ticker text, for
the same reason everything else in this package is: two tokens can print the
same symbol.

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")

#: The published multipliers.  Named rather than inlined so the card can show
#: which cap was binding, which is usually the most informative thing about a
#: candidate: "capped by liquidity" says more than any score.
VOLUME_MULTIPLE = Decimal("150")
LIQUIDITY_MULTIPLE = Decimal("500")

CAP_FDV = "FDV"
CAP_VOLUME = "VOLUME"
CAP_LIQUIDITY = "LIQUIDITY"

HUMAN_CAP: dict[str, str] = {
    CAP_FDV: "its own fully diluted value",
    CAP_VOLUME: f"{VOLUME_MULTIPLE}x its 24h volume — little has actually traded",
    CAP_LIQUIDITY: f"{LIQUIDITY_MULTIPLE}x its liquidity — little is actually pooled",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One verified stock-linked meme, with the market data behind it."""

    mint: str
    anchor_key: str
    symbol: str = ""
    name: str = ""
    launchpad: str = ""
    fdv_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    launched_at: int | None = None
    observed_at: int | None = None

    @property
    def measurable(self) -> bool:
        """Whether there is enough data to rank this at all."""

        return all(
            value is not None
            for value in (self.fdv_usd, self.volume_24h_usd, self.liquidity_usd)
        )


@dataclass(frozen=True, slots=True)
class CredibleValue:
    """The ranking figure, and which cap decided it."""

    mint: str
    value: Decimal | None = None
    binding_cap: str = ""
    fdv_usd: Decimal | None = None
    volume_component: Decimal | None = None
    liquidity_component: Decimal | None = None

    @property
    def credible(self) -> bool:
        return self.value is not None and self.value > ZERO

    def why(self) -> str:
        if not self.credible:
            return "not enough market data to value this"
        return f"capped by {HUMAN_CAP.get(self.binding_cap, self.binding_cap)}"

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "value": _s(self.value),
            "binding_cap": self.binding_cap,
            "fdv_usd": _s(self.fdv_usd),
            "volume_component": _s(self.volume_component),
            "liquidity_component": _s(self.liquidity_component),
            "why": self.why(),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def credible_value(candidate: Candidate) -> CredibleValue:
    """``min(FDV, 150 x volume, 500 x liquidity)``, with the binding cap named.

    Any missing or negative input makes the whole figure unavailable rather
    than zero: a token we could not measure must not sort below one we measured
    as worthless, because those are different findings.
    """

    if not candidate.measurable:
        return CredibleValue(mint=candidate.mint)
    fdv = candidate.fdv_usd or ZERO
    volume = candidate.volume_24h_usd or ZERO
    liquidity = candidate.liquidity_usd or ZERO
    if min(fdv, volume, liquidity) < ZERO:
        return CredibleValue(mint=candidate.mint)

    volume_cap = volume * VOLUME_MULTIPLE
    liquidity_cap = liquidity * LIQUIDITY_MULTIPLE
    options = ((fdv, CAP_FDV), (volume_cap, CAP_VOLUME), (liquidity_cap, CAP_LIQUIDITY))
    value, binding = min(options, key=lambda item: item[0])
    return CredibleValue(
        mint=candidate.mint,
        value=value.quantize(CENT),
        binding_cap=binding,
        fdv_usd=fdv,
        volume_component=volume_cap,
        liquidity_component=liquidity_cap,
    )


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: Candidate
    value: CredibleValue
    rank: int = 0


def rank_anchor(
    candidates: Sequence[Candidate],
    *,
    anchor_key: str,
) -> tuple[RankedCandidate, ...]:
    """Rank one anchor's coins, best first, with explicit tie breakers.

    Ties resolve by the earlier launch and then by mint, so the order is
    deterministic across restarts — a ranking that reshuffles on equal inputs
    would manufacture crown changes out of nothing.
    """

    scoped = [item for item in candidates if item.anchor_key == anchor_key]
    scored = [(item, credible_value(item)) for item in scoped]
    scored.sort(
        key=lambda pair: (
            -(pair[1].value or ZERO),
            pair[0].launched_at if pair[0].launched_at is not None else 1 << 62,
            pair[0].mint,
        )
    )
    return tuple(
        RankedCandidate(candidate=item, value=value, rank=index + 1)
        for index, (item, value) in enumerate(scored)
    )


# --- crown changes ------------------------------------------------------------
CROWN_UNCHANGED = "UNCHANGED"
CROWN_ESTABLISHED = "FIRST_LEADER"
CROWN_CHANGED = "CROWN_CHANGED"
CROWN_CONTESTED = "CONTESTED_NO_CLEAR_LEADER"


@dataclass(frozen=True, slots=True)
class CrownState:
    """Who holds an anchor, since when, and who is closest behind."""

    anchor_key: str
    leader_mint: str = ""
    leader_value: Decimal | None = None
    leader_since: int | None = None
    previous_leader_mint: str = ""
    challengers: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, object]:
        return {
            "anchor_key": self.anchor_key,
            "leader_mint": self.leader_mint,
            "leader_value": _s(self.leader_value),
            "leader_since": self.leader_since,
            "previous_leader_mint": self.previous_leader_mint,
            "challengers": list(self.challengers),
        }


@dataclass(frozen=True, slots=True)
class CrownEvent:
    """What, if anything, to say about this anchor."""

    anchor_key: str
    outcome: str = CROWN_UNCHANGED
    state: CrownState | None = None
    reason: str = ""

    @property
    def should_alert(self) -> bool:
        """Only a genuine, decisive change earns an interruption."""

        return self.outcome == CROWN_CHANGED

    def to_json(self) -> dict[str, object]:
        return {
            "anchor_key": self.anchor_key,
            "outcome": self.outcome,
            "should_alert": self.should_alert,
            "reason": self.reason,
            "state": None if self.state is None else self.state.to_json(),
        }


def evaluate_crown(
    anchor_key: str,
    ranked: Sequence[RankedCandidate],
    previous: CrownState | None,
    *,
    hysteresis: Decimal = Decimal("1.15"),
    now: int,
) -> CrownEvent:
    """Decide whether the crown moved, with hysteresis against flapping.

    A challenger must beat the incumbent by ``hysteresis`` before it takes the
    crown.  Without that, two coins within a percent of each other trade places
    on every refresh and the operator gets a stream of alerts describing noise.
    """

    live = [item for item in ranked if item.value.credible]
    if not live:
        return CrownEvent(
            anchor_key=anchor_key,
            outcome=CROWN_CONTESTED,
            state=previous,
            reason="no candidate on this anchor has enough market data to rank",
        )

    top = live[0]
    state = CrownState(
        anchor_key=anchor_key,
        leader_mint=top.candidate.mint,
        leader_value=top.value.value,
        leader_since=now,
        challengers=tuple(item.candidate.mint for item in live[1:4]),
    )

    if previous is None or not previous.leader_mint:
        return CrownEvent(
            anchor_key=anchor_key,
            outcome=CROWN_ESTABLISHED,
            state=state,
            reason=f"first ranked leader on this anchor ({top.value.why()})",
        )

    if top.candidate.mint == previous.leader_mint:
        # Same holder.  Keep the original leader_since: how long it has held
        # the anchor is information, and resetting it every poll would erase it.
        return CrownEvent(
            anchor_key=anchor_key,
            outcome=CROWN_UNCHANGED,
            state=CrownState(
                anchor_key=anchor_key,
                leader_mint=previous.leader_mint,
                leader_value=top.value.value,
                leader_since=previous.leader_since,
                previous_leader_mint=previous.previous_leader_mint,
                challengers=state.challengers,
            ),
            reason="the same coin still leads this anchor",
        )

    incumbent = next(
        (item for item in live if item.candidate.mint == previous.leader_mint), None
    )
    incumbent_value = (
        incumbent.value.value
        if incumbent is not None and incumbent.value.value is not None
        else previous.leader_value
    )
    challenger_value = top.value.value or ZERO

    if incumbent_value and incumbent_value > ZERO:
        margin = challenger_value / incumbent_value
        if margin < hysteresis:
            return CrownEvent(
                anchor_key=anchor_key,
                outcome=CROWN_CONTESTED,
                state=previous,
                reason=(
                    f"{top.candidate.mint[:8]}… leads by {margin.quantize(CENT)}x, "
                    f"below the {hysteresis}x needed to call it a change"
                ),
            )

    return CrownEvent(
        anchor_key=anchor_key,
        outcome=CROWN_CHANGED,
        state=CrownState(
            anchor_key=anchor_key,
            leader_mint=top.candidate.mint,
            leader_value=challenger_value,
            leader_since=now,
            previous_leader_mint=previous.leader_mint,
            challengers=state.challengers,
        ),
        reason=(
            f"{top.candidate.symbol or top.candidate.mint[:8]} took this anchor from "
            f"{previous.leader_mint[:8]}… ({top.value.why()})"
        ),
    )


@dataclass(frozen=True, slots=True)
class AnchorComplex:
    """The aggregate picture for one stock, when the data supports it."""

    anchor_key: str
    coin_count: int = 0
    total_liquidity_usd: Decimal | None = None
    total_volume_24h_usd: Decimal | None = None
    leader_mint: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "anchor_key": self.anchor_key,
            "coin_count": self.coin_count,
            "total_liquidity_usd": _s(self.total_liquidity_usd),
            "total_volume_24h_usd": _s(self.total_volume_24h_usd),
            "leader_mint": self.leader_mint,
        }


def summarise_anchor(
    anchor_key: str, ranked: Sequence[RankedCandidate]
) -> AnchorComplex:
    """Totals across an anchor, summing only what was actually measured."""

    liquidity = [
        item.candidate.liquidity_usd
        for item in ranked
        if item.candidate.liquidity_usd is not None
    ]
    volume = [
        item.candidate.volume_24h_usd
        for item in ranked
        if item.candidate.volume_24h_usd is not None
    ]
    return AnchorComplex(
        anchor_key=anchor_key,
        coin_count=len(ranked),
        total_liquidity_usd=sum(liquidity, ZERO) if liquidity else None,
        total_volume_24h_usd=sum(volume, ZERO) if volume else None,
        leader_mint=ranked[0].candidate.mint if ranked else "",
    )
