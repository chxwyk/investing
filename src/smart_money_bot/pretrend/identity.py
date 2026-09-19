"""Exact-mint identity for every cross-provider join in the pre-trend engine.

The whole engine rests on one claim: *this* observation and *that* Trending
entry describe the same token.  If that claim is ever wrong, every downstream
number — lead time, affinity, precision, base rate — is measuring noise and
reporting it as alpha.

So the join key is the Solana mint and nothing else.  A ticker, a name, a logo,
a website, a description and a social handle are all attributes that unrelated
tokens routinely share; a mint is not.  Two tokens called ``$CAT`` are two
tokens, and the only honest answer when a provider hands us a ticker with no
mint is that we could not resolve it.

Match states are explicit rather than boolean because "we are not sure" is a
real and common answer that must not silently collapse into "yes":

``EXACT_CA_MATCH``
    Both sides supplied the same valid mint.  This is the only state that may
    carry evidence from one provider to another.

``STRONG_CA_MATCH``
    One side supplied the mint; the other supplied a reference that the *first
    side itself published for that mint* (an id, a canonical URL).  Strong, but
    still derived, so it is recorded separately and can be audited later.

``AMBIGUOUS``
    Attribute-level agreement only (same ticker, same name).  This is never
    treated as confirmed — it exists so the ambiguity can be counted and
    reported, not so it can be used.

``NO_MATCH``
    Nothing lined up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..token_identity import is_valid_mint, normalise_symbol

#: Both sides named the same exact mint.
EXACT_CA_MATCH = "EXACT_CA_MATCH"
#: One side named the mint; the other named a reference that side published for it.
STRONG_CA_MATCH = "STRONG_CA_MATCH"
#: Attributes agree but the mint does not (or is absent).  Never confirmed.
AMBIGUOUS = "AMBIGUOUS"
#: Nothing lined up.
NO_MATCH = "NO_MATCH"

MATCH_STATES: tuple[str, ...] = (
    EXACT_CA_MATCH,
    STRONG_CA_MATCH,
    AMBIGUOUS,
    NO_MATCH,
)

#: The only states that may carry evidence across a provider boundary.
CONFIRMED_STATES: frozenset[str] = frozenset({EXACT_CA_MATCH, STRONG_CA_MATCH})

#: A base58 run long enough to be a mint, for pulling a CA out of free text.
_MINT_IN_TEXT = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def extract_mint(value: Any) -> str | None:
    """Return the exact mint in ``value``, or ``None``.

    A string that *is* a mint resolves to itself.  A longer string (a URL, a
    thesis body) resolves only if it contains exactly one distinct valid mint —
    two candidates is an ambiguity, and picking the first would be a guess.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if is_valid_mint(text):
        return text
    found = {token for token in _MINT_IN_TEXT.findall(text) if is_valid_mint(token)}
    if len(found) == 1:
        return found.pop()
    return None


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Why two records were, or were not, judged to be the same token."""

    state: str
    mint: str | None = None
    reason: str = ""

    @property
    def confirmed(self) -> bool:
        """True only for states that may carry evidence between providers."""

        return self.state in CONFIRMED_STATES

    @property
    def exact(self) -> bool:
        return self.state == EXACT_CA_MATCH

    def to_json(self) -> dict[str, Any]:
        return {"state": self.state, "mint": self.mint, "reason": self.reason}


def match_records(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    mint_keys: tuple[str, ...] = ("mint", "address", "tokenAddress", "token_address", "ca"),
    reference_keys: tuple[str, ...] = ("token_id", "tokenId", "id", "url", "fomo_url"),
    symbol_keys: tuple[str, ...] = ("symbol", "ticker"),
    name_keys: tuple[str, ...] = ("name",),
) -> MatchResult:
    """Decide how two provider records relate, preferring the weakest honest claim."""

    left_mint = _first_mint(left, mint_keys)
    right_mint = _first_mint(right, mint_keys)

    if left_mint and right_mint:
        if left_mint == right_mint:
            return MatchResult(EXACT_CA_MATCH, left_mint, "both records named the same mint")
        return MatchResult(
            NO_MATCH,
            None,
            "both records named a mint and the mints differ",
        )

    # Exactly one side has a mint: a shared published reference is strong
    # evidence, because the reference was issued *by a provider for that mint*
    # rather than chosen by us.
    known = left_mint or right_mint
    if known:
        other = right if left_mint else left
        owner = left if left_mint else right
        shared = _shared_reference(owner, other, reference_keys)
        if shared:
            return MatchResult(
                STRONG_CA_MATCH,
                known,
                f"shared provider reference {shared!r} published for this mint",
            )
        # A mint embedded in the other record's free text is exact, not strong.
        for key in ("url", "link", "text", "body", "thesis", "description"):
            embedded = extract_mint(other.get(key))
            if embedded == known:
                return MatchResult(
                    EXACT_CA_MATCH, known, f"exact mint present in {key!r}"
                )
            if embedded and embedded != known:
                return MatchResult(
                    NO_MATCH, None, f"{key!r} names a different mint"
                )

    if _attributes_agree(left, right, symbol_keys) or _attributes_agree(left, right, name_keys):
        return MatchResult(
            AMBIGUOUS,
            known,
            "only ticker/name agree; a shared ticker is not a shared token",
        )
    return MatchResult(NO_MATCH, known, "no mint and no attribute agreement")


def _first_mint(record: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in record:
            mint = extract_mint(record.get(key))
            if mint:
                return mint
    return None


def _shared_reference(
    owner: dict[str, Any], other: dict[str, Any], keys: tuple[str, ...]
) -> str | None:
    for key in keys:
        left_value = str(owner.get(key) or "").strip()
        if not left_value:
            continue
        for other_key in keys:
            if str(other.get(other_key) or "").strip() == left_value:
                return left_value
    return None


def _attributes_agree(
    left: dict[str, Any], right: dict[str, Any], keys: tuple[str, ...]
) -> bool:
    for key in keys:
        left_value = normalise_symbol(left.get(key))
        right_value = normalise_symbol(right.get(key))
        if left_value and left_value == right_value:
            return True
    return False
