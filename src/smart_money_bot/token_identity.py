"""Token identity: chain + exact mint, and nothing else.

This module exists because of a specific production failure.  The bot alerted on
mint ``7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump`` as an "ORGANIC RUNNER —
LOOK NOW" when the operator was looking at a *different* token that merely shared
the ``GPRO`` symbol.  A brand-new clone replaced the real thing.

The rule, stated once and enforced mechanically:

    **Identity is chain + exact mint address.  Name and ticker are display
    metadata and may never resolve, substitute, merge, dedupe, enrich or choose
    a token.**

The dangerous pattern is not fuzzy matching itself — it is fuzzy matching that
*returns one winner*.  A text search over a symbol yields a set of unrelated
tokens; picking "the best" one from that set silently asserts an identity the
search never established.  Worse, the ranking that picked it here preferred the
*youngest* pair, which is precisely the clone.

So:

* :class:`ResolutionProvenance` records where a mint came from and how, and
  :meth:`ResolutionProvenance.verify` hard-fails when a source mint and a
  resolved mint disagree.
* A symbol-derived mint is ``identity_verified = False`` by construction, and
  callers must treat it as a lead to check, never as an answer.
* When exact enrichment fails, the honest result is
  :data:`UNRESOLVED_EXACT_MINT` — failure is preferable to substitution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Solana mints are base58 and 32-44 characters.  Anything else is not a mint,
#: and must never be treated as one.
_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

#: The only chain this deployment resolves.  Identity is chain + mint, so the
#: chain travels with the address rather than being assumed.
CHAIN_SOLANA = "solana"

# --- how a mint was arrived at ------------------------------------------------
#: The source handed us the exact address.  The only trustworthy method.
METHOD_EXACT_MINT = "EXACT_MINT"
#: Read directly from on-chain program state keyed by the mint.
METHOD_ONCHAIN = "ONCHAIN_ACCOUNT"
#: Derived from a text/symbol/name search.  **Never** an identity on its own.
METHOD_SYMBOL_SEARCH = "SYMBOL_SEARCH"
#: A narrative or story term produced the candidate.  Same caveat.
METHOD_NARRATIVE_SEARCH = "NARRATIVE_SEARCH"
#: We could not establish the exact mint at all.
METHOD_UNRESOLVED = "UNRESOLVED"

RESOLUTION_METHODS: tuple[str, ...] = (
    METHOD_EXACT_MINT,
    METHOD_ONCHAIN,
    METHOD_SYMBOL_SEARCH,
    METHOD_NARRATIVE_SEARCH,
    METHOD_UNRESOLVED,
)

#: Methods that establish identity on their own.
VERIFIED_METHODS: frozenset[str] = frozenset({METHOD_EXACT_MINT, METHOD_ONCHAIN})

#: Methods that produce a *lead*, not an identity.  A candidate resolved this way
#: can be researched but can never be promoted or made actionable.
UNVERIFIED_METHODS: frozenset[str] = frozenset(
    {METHOD_SYMBOL_SEARCH, METHOD_NARRATIVE_SEARCH, METHOD_UNRESOLVED}
)

#: The honest answer when exact enrichment fails.  Never a substituted mint.
UNRESOLVED_EXACT_MINT = "UNRESOLVED_EXACT_MINT"

#: Recorded when a source and its resolution disagree.  This is a hard failure.
SOURCE_RESOLVED_MISMATCH = "SOURCE_RESOLVED_MINT_MISMATCH"


class TokenIdentityError(ValueError):
    """Raised when a pipeline would substitute one token for another."""


def is_valid_mint(value: object) -> bool:
    return isinstance(value, str) and bool(_MINT_RE.match(value))


def normalise_symbol(value: object) -> str:
    """Fold a symbol for *collision detection only*.

    The output of this function must never be used to look a token up.  It
    exists so that "two live tokens call themselves GPRO" is detectable, which
    is risk context — not a way to choose between them.
    """

    text = str(value or "").strip().casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


@dataclass(frozen=True, slots=True)
class ResolutionProvenance:
    """Where a candidate's mint came from, and whether that establishes identity.

    ``source_mint`` is what the discovery source handed us; ``resolved_mint`` is
    what the pipeline ended up using.  When a source supplied an exact address,
    those two must be equal at every downstream stage — that equality is the
    whole guarantee, and :meth:`verify` is what enforces it.
    """

    source: str = ""
    source_chain: str = CHAIN_SOLANA
    source_mint: str = ""
    resolved_chain: str = CHAIN_SOLANA
    resolved_mint: str = ""
    resolution_method: str = METHOD_UNRESOLVED
    symbol_collision: bool = False
    collision_mints: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def identity_verified(self) -> bool:
        """True only for an exact address that survived the whole pipeline."""

        if self.resolution_method not in VERIFIED_METHODS:
            return False
        if not is_valid_mint(self.resolved_mint):
            return False
        if self.source_mint and self.source_mint != self.resolved_mint:
            return False
        return self.source_chain == self.resolved_chain

    @property
    def substituted(self) -> bool:
        """True when a source address was replaced by a different one."""

        return bool(
            self.source_mint
            and self.resolved_mint
            and self.source_mint != self.resolved_mint
        )

    @property
    def unresolved(self) -> bool:
        return self.resolution_method == METHOD_UNRESOLVED or not is_valid_mint(
            self.resolved_mint
        )

    def failure_reason(self) -> str:
        if self.substituted:
            return SOURCE_RESOLVED_MISMATCH
        if self.unresolved:
            return UNRESOLVED_EXACT_MINT
        return ""

    def verify(self) -> None:
        """Hard-fail rather than let a substitution reach an operator.

        Called at every promotion boundary.  A mismatch is a programming error,
        not a market condition, so it raises rather than degrading quietly.
        """

        if self.substituted:
            raise TokenIdentityError(
                f"{SOURCE_RESOLVED_MISMATCH}: source {self.source_mint[:8]}… was "
                f"replaced by {self.resolved_mint[:8]}… via {self.resolution_method}; "
                "a same-symbol token is not the same token"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_chain": self.source_chain,
            "source_mint": self.source_mint,
            "resolved_chain": self.resolved_chain,
            "resolved_mint": self.resolved_mint,
            "resolution_method": self.resolution_method,
            "symbol_collision": self.symbol_collision,
            "collision_mints": list(self.collision_mints),
            "identity_verified": self.identity_verified,
            "substituted": self.substituted,
            "unresolved": self.unresolved,
            "failure_reason": self.failure_reason(),
            "notes": list(self.notes),
        }


def exact(mint: str, *, source: str, chain: str = CHAIN_SOLANA) -> ResolutionProvenance:
    """The good case: a source handed us the exact address and we kept it."""

    return ResolutionProvenance(
        source=source,
        source_chain=chain,
        source_mint=mint,
        resolved_chain=chain,
        resolved_mint=mint,
        resolution_method=METHOD_EXACT_MINT,
    )


def onchain(mint: str, *, source: str = "solana_rpc") -> ResolutionProvenance:
    return ResolutionProvenance(
        source=source,
        source_mint=mint,
        resolved_mint=mint,
        resolution_method=METHOD_ONCHAIN,
    )


def unresolved(
    source_mint: str,
    *,
    source: str,
    note: str = "",
) -> ResolutionProvenance:
    """Exact enrichment failed.  Say so; never fall back to a symbol search."""

    return ResolutionProvenance(
        source=source,
        source_mint=source_mint,
        resolved_mint="",
        resolution_method=METHOD_UNRESOLVED,
        notes=(note,) if note else (),
    )


def from_symbol_search(
    mint: str,
    *,
    source: str,
    query: str,
    collision_mints: tuple[str, ...] = (),
) -> ResolutionProvenance:
    """A text search produced this mint.  It is a lead, never an identity.

    ``source_mint`` is deliberately left empty: the search did not start from an
    address, so there is nothing to have preserved.  ``identity_verified`` is
    therefore ``False``, and every promotion gate refuses it.
    """

    return ResolutionProvenance(
        source=source,
        source_mint="",
        resolved_mint=mint,
        resolution_method=METHOD_SYMBOL_SEARCH,
        symbol_collision=len(collision_mints) > 1,
        collision_mints=tuple(collision_mints),
        notes=(f"resolved from the text query {query!r}, not from an address",),
    )


def assert_exact_propagation(
    source_mint: str,
    resolved_mint: str,
    *,
    stage: str,
) -> None:
    """Assert a pipeline stage did not swap the mint underneath us.

    Called at each hand-off — enrichment, scoring, persistence, rendering — so a
    substitution is caught at the stage that introduced it rather than surfacing
    as a wrong card.
    """

    if source_mint and resolved_mint and source_mint != resolved_mint:
        raise TokenIdentityError(
            f"{stage}: candidate entered as {source_mint[:8]}… but resolved to "
            f"{resolved_mint[:8]}…; exact mint must propagate unchanged"
        )


@dataclass(frozen=True, slots=True)
class SymbolCollision:
    """Two or more live tokens calling themselves the same thing.

    Purely informational.  It raises the bar for promotion and it is shown to
    the operator; it never picks a winner, because there is no basis on which to
    pick one.
    """

    symbol: str
    mints: tuple[str, ...] = ()

    @property
    def detected(self) -> bool:
        return len(set(self.mints)) > 1

    @property
    def count(self) -> int:
        return len(set(self.mints))

    def warning_line(self, subject_mint: str) -> str:
        if not self.detected:
            return ""
        others = sorted(set(self.mints) - {subject_mint})
        return (
            f"⚠ SYMBOL COLLISION: {self.count} live tokens use `{self.symbol}`. "
            f"This card is for `{subject_mint}` and no other."
            + (f" Others: {', '.join(item[:8] + '…' for item in others[:4])}" if others else "")
        )

    def to_json(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "mints": list(self.mints),
            "detected": self.detected,
            "count": self.count,
        }


def detect_symbol_collision(
    symbol: str,
    known: dict[str, str],
    *,
    subject_mint: str = "",
) -> SymbolCollision:
    """Find every known mint sharing this normalised symbol.

    ``known`` maps mint → symbol.  The result groups them; it does not rank
    them, and no caller may use it to choose one.
    """

    wanted = normalise_symbol(symbol)
    if not wanted:
        return SymbolCollision(symbol=str(symbol or ""))
    mints = sorted(
        {mint for mint, value in known.items() if normalise_symbol(value) == wanted}
        | ({subject_mint} if subject_mint else set())
    )
    return SymbolCollision(symbol=str(symbol or ""), mints=tuple(mints))
