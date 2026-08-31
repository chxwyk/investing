"""Where every piece of intelligence came from, and what may honestly be claimed.

v2.42 established this discipline for the Trending board.  v2.43 extends it to
*every* source the research engine reads, because the same failure mode applies
everywhere: an approximation quietly relabelled as the real thing.

The specific claim this module exists to prevent is "TERMINAL TRENDING".  Terminal
(formerly Padre) publishes documentation describing the *kinds* of signal active
memecoin traders care about — multi-timeframe momentum, bonding progress, dev
holding, bundles, fresh wallets, holder concentration.  That documentation is a
legitimate design reference.  Their ranking algorithm is proprietary and their
feed is not something this deployment can legitimately read, so anything we
compute from public data is ours and is labelled ``PUBLIC_TRENDING_MODEL`` —
never Terminal's, never Fomo's.

Nothing in this codebase reads a logged-in Terminal session, reuses its cookies
or auth tokens, calls a private endpoint, or reverse-engineers a proprietary
ranking.  A Terminal-sourced value exists only when an administrator explicitly
supplies one, and then it is labelled ``TERMINAL_AUTHORIZED``.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- source kinds (section 3) -------------------------------------------------
#: Read directly from the Pump.fun program's own on-chain accounts.
PUMP_ONCHAIN = "PUMP_ONCHAIN"
#: Read from PumpSwap pool state on chain.
PUMPSWAP_ONCHAIN = "PUMPSWAP_ONCHAIN"
#: Any public Solana RPC method — balances, holders, signatures, slots.
SOLANA_RPC = "SOLANA_RPC"
#: DEX Screener's documented public endpoints.
DEXSCREENER_PUBLIC = "DEXSCREENER_PUBLIC"
#: A social source an administrator authorised and configured.
AUTHORIZED_SOCIAL = "AUTHORIZED_SOCIAL"
#: The configured J7 feed.
J7_AUTHORIZED = "J7_AUTHORIZED"
#: An administrator-configured Fomo Trending feed.
FOMO_AUTHORIZED = "FOMO_AUTHORIZED"
#: An administrator-supplied Terminal observation.  Never auto-fetched.
TERMINAL_AUTHORIZED = "TERMINAL_AUTHORIZED"
#: Ordinary public web content.
PUBLIC_WEB = "PUBLIC_WEB"
#: Our own ranking, computed from the sources above.  Ours, not anyone else's.
DERIVED_PUBLIC_MODEL = "DERIVED_PUBLIC_MODEL"

SOURCE_KINDS: tuple[str, ...] = (
    PUMP_ONCHAIN,
    PUMPSWAP_ONCHAIN,
    SOLANA_RPC,
    DEXSCREENER_PUBLIC,
    AUTHORIZED_SOCIAL,
    J7_AUTHORIZED,
    FOMO_AUTHORIZED,
    TERMINAL_AUTHORIZED,
    PUBLIC_WEB,
    DERIVED_PUBLIC_MODEL,
)

SOURCE_LABELS: dict[str, str] = {
    PUMP_ONCHAIN: "Pump.fun program state (on-chain)",
    PUMPSWAP_ONCHAIN: "PumpSwap pool state (on-chain)",
    SOLANA_RPC: "public Solana RPC",
    DEXSCREENER_PUBLIC: "DEX Screener public API",
    AUTHORIZED_SOCIAL: "authorised social source",
    J7_AUTHORIZED: "authorised J7 feed",
    FOMO_AUTHORIZED: "authorised Fomo feed",
    TERMINAL_AUTHORIZED: "administrator-supplied Terminal observation",
    PUBLIC_WEB: "public web",
    DERIVED_PUBLIC_MODEL: "our own model over public data",
}

#: Sources that are on-chain facts rather than a vendor's opinion.  These keep
#: working when every vendor is down, which is why the engine is built on them.
ONCHAIN_SOURCES: frozenset[str] = frozenset({PUMP_ONCHAIN, PUMPSWAP_ONCHAIN, SOLANA_RPC})

#: Sources that only exist when an administrator configured them.
ADMIN_CONFIGURED_SOURCES: frozenset[str] = frozenset(
    {AUTHORIZED_SOCIAL, J7_AUTHORIZED, FOMO_AUTHORIZED, TERMINAL_AUTHORIZED}
)


# --- the honest name for our own ranking (sections 3, 32, 97) ----------------
#: Our independent ranking over public data.
PUBLIC_TRENDING_MODEL = "PUBLIC_TRENDING_MODEL"
#: The same thing, named to acknowledge the design reference without claiming
#: to be it.  Either label is honest; neither is "Terminal Trending".
TERMINAL_STYLE_PUBLIC_MODEL = "TERMINAL_STYLE_PUBLIC_MODEL"

#: Names no surface may ever produce for a model we computed ourselves.
FORBIDDEN_RANKING_CLAIMS: frozenset[str] = frozenset(
    {
        "TERMINAL_TRENDING",
        "PADRE_TRENDING",
        "TERMINAL TRENDING",
        "PADRE TRENDING",
    }
)


def assert_honest_ranking_name(name: str) -> None:
    """Fail loudly rather than let a card claim someone else's ranking.

    Called by the model itself, so the guarantee holds even if a future caller
    passes a label through from configuration.
    """

    if name.strip().upper().replace("-", "_") in FORBIDDEN_RANKING_CLAIMS:
        raise ValueError(
            f"{name!r} claims a proprietary third-party ranking this deployment "
            "cannot legitimately read; use PUBLIC_TRENDING_MODEL instead"
        )


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One attributed observation: what we learned and where it came from."""

    kind: str
    detail: str = ""
    #: True only for on-chain facts and administrator-authorised feeds.
    authoritative: bool = False

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"unknown intelligence source: {self.kind}")

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.kind, self.kind)

    @property
    def onchain(self) -> bool:
        return self.kind in ONCHAIN_SOURCES

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "authoritative": self.authoritative,
            "onchain": self.onchain,
        }


def onchain(detail: str = "") -> SourceRef:
    """Shorthand for the source we trust most, because it is not an opinion."""

    return SourceRef(kind=SOLANA_RPC, detail=detail, authoritative=True)


def pump_onchain(detail: str = "") -> SourceRef:
    return SourceRef(kind=PUMP_ONCHAIN, detail=detail, authoritative=True)


# --- independence for confluence (section 34) --------------------------------
#: Which *evidence family* each source belongs to.  Two feeds of the same market
#: are one piece of evidence, however many vendors relay it — counting them
#: twice is how a single data point gets mistaken for agreement.
SOURCE_FAMILIES: dict[str, str] = {
    PUMP_ONCHAIN: "ONCHAIN_MARKET",
    PUMPSWAP_ONCHAIN: "ONCHAIN_MARKET",
    SOLANA_RPC: "ONCHAIN_MARKET",
    DEXSCREENER_PUBLIC: "ONCHAIN_MARKET",
    FOMO_AUTHORIZED: "THIRD_PARTY_ATTENTION",
    TERMINAL_AUTHORIZED: "THIRD_PARTY_ATTENTION",
    DERIVED_PUBLIC_MODEL: "OUR_MODEL",
    AUTHORIZED_SOCIAL: "SOCIAL",
    J7_AUTHORIZED: "SOCIAL",
    PUBLIC_WEB: "SOCIAL",
}


def independent_families(sources: tuple[SourceRef, ...]) -> frozenset[str]:
    """Collapse sources to the distinct evidence families they represent."""

    return frozenset(
        SOURCE_FAMILIES.get(source.kind, source.kind) for source in sources
    )


def count_independent(sources: tuple[SourceRef, ...]) -> int:
    """How many genuinely independent things agree (section 34)."""

    return len(independent_families(sources))
