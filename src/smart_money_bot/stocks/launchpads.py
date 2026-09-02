"""A factory address is not trusted because it was written down.

The specification is blunt about this and it is right: *never guess a contract
address*, and *if LONG cannot be independently verified, keep the LONG adapter
disabled with a clear status instead of inventing an address*.

That constraint shapes the whole module.  A hardcoded address in a source file
is a claim by whoever typed it, and a claim is exactly what this lane refuses to
accept everywhere else — it would be incoherent to reject a coin for asserting
an anchor by name while accepting a factory because a developer pasted it.

So an adapter is **disabled until it proves itself at runtime**:

* The address comes from configuration, not from this file.  What lives here is
  the *documented candidate* and where it was documented, carrying no authority
  of its own.
* Before an adapter may emit a single launch, its factory must be verified on
  chain: the address must hold code, and the hash of that code must match a
  digest the operator supplied from an independent source (the launchpad's
  official repository, its documentation, or the Robinhood Chain explorer).
* Anything else — no code at the address, a digest mismatch, an unreachable RPC,
  no digest configured at all — leaves the adapter ``DISABLED`` with a reason a
  human can act on.

A mismatch is treated as *more* alarming than an absence, because an address
that holds different code than documented is either a proxy that has been
upgraded underneath us or the wrong contract entirely, and both of those emit
plausible events.

This module decides; it does not fetch.  The RPC call that reads the bytecode
lives in the runtime, which hands the result here.  Pure logic: no provider, no
database, no signer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256

# --- verification states ------------------------------------------------------
#: Verified on chain against an independently supplied digest.  Only this state
#: may produce launch records.
VERIFIED = "VERIFIED"
#: No digest was configured, so there is nothing to verify against.  The
#: default, and the state every adapter ships in.
UNVERIFIED = "UNVERIFIED"
#: The address holds code, and it is not the code we were told to expect.
MISMATCH = "BYTECODE_MISMATCH"
#: The address holds no code at all.
NO_CODE = "NO_CODE_AT_ADDRESS"
#: The chain could not be reached to check.  Not a pass, and not a failure of
#: the contract — a failure to look.
UNREACHABLE = "RPC_UNREACHABLE"
#: The operator has not supplied an address for this launchpad.
NOT_CONFIGURED = "NOT_CONFIGURED"

STATES: tuple[str, ...] = (
    VERIFIED,
    UNVERIFIED,
    MISMATCH,
    NO_CODE,
    UNREACHABLE,
    NOT_CONFIGURED,
)

HUMAN_STATE: dict[str, str] = {
    VERIFIED: "factory bytecode matches the independently supplied digest",
    UNVERIFIED: (
        "no expected bytecode digest configured — supply one from the launchpad's "
        "official repository, its documentation, or the Robinhood Chain explorer"
    ),
    MISMATCH: (
        "the address holds code that does not match the expected digest — either "
        "a proxy was upgraded or this is the wrong contract; both emit plausible "
        "events, so the adapter stays off"
    ),
    NO_CODE: "there is no contract at this address on chain 4663",
    UNREACHABLE: "the chain could not be reached to verify this factory",
    NOT_CONFIGURED: "no factory address configured for this launchpad",
}


@dataclass(frozen=True, slots=True)
class DocumentedCandidate:
    """An address a public source documents, carrying no authority of its own.

    Recorded so an operator knows where to start looking and can compare what
    they find against what the specification said.  Nothing in this codebase
    enables an adapter from one of these: it still has to be configured and
    then verified on chain.
    """

    launchpad: str
    address: str = ""
    source: str = ""
    note: str = ""


#: What public documentation says, as of this release, with its provenance.
#: These are starting points for an operator, not defaults — none of them is
#: enabled by anything in this package.
DOCUMENTED: tuple[DocumentedCandidate, ...] = (
    DocumentedCandidate(
        launchpad="Pair",
        address="0x8660A7F019C7943b0b0A91B8E39AFf3b6DB6Ae62",
        source="pair.fund/docs — PairLaunchpadV5Upgradeable proxy",
        note=(
            "a proxy: verify the current implementation and its event ABI, not "
            "only the proxy address, because the implementation can change "
            "under a stable proxy address"
        ),
    ),
    DocumentedCandidate(
        launchpad="Pons",
        address="",
        source="docs.ponsfamily.com and github.com/ponsdotdev/ponsfamily",
        note=(
            "V1 and V2 both exist and public sources disagree on the V2 address; "
            "resolve against current official documentation and verified "
            "explorer bytecode rather than picking one"
        ),
    ),
    DocumentedCandidate(
        launchpad="LONG",
        address="",
        source="",
        note=(
            "no official or independently verified production factory address "
            "was obtainable — this adapter stays disabled rather than guessing"
        ),
    ),
)


def digest_of(bytecode: str | bytes) -> str:
    """The digest an operator compares against.  Plain sha256 of the runtime code.

    Normalised so that ``0x`` prefixes and letter case cannot make identical
    code look different.
    """

    raw = bytecode.hex() if isinstance(bytecode, bytes) else str(bytecode or "")
    raw = raw.lower().removeprefix("0x").strip()
    return sha256(raw.encode("ascii", "ignore")).hexdigest()


@dataclass(frozen=True, slots=True)
class FactoryVerification:
    """Whether one launchpad's factory may be believed, and why."""

    launchpad: str
    address: str = ""
    state: str = NOT_CONFIGURED
    expected_digest: str = ""
    observed_digest: str = ""
    checked_at: int | None = None
    detail: str = ""

    @property
    def enabled(self) -> bool:
        """Only a verified factory may produce launch records."""

        return self.state == VERIFIED

    def human(self) -> str:
        return HUMAN_STATE.get(self.state, self.state)

    def to_json(self) -> dict[str, object]:
        return {
            "launchpad": self.launchpad,
            "address": self.address,
            "state": self.state,
            "enabled": self.enabled,
            "human": self.human(),
            "expected_digest": self.expected_digest[:16] or "",
            "observed_digest": self.observed_digest[:16] or "",
            "checked_at": self.checked_at,
            "detail": self.detail,
        }


def verify_factory(
    launchpad: str,
    *,
    address: str,
    expected_digest: str,
    observed_bytecode: str | bytes | None,
    reachable: bool = True,
    checked_at: int | None = None,
) -> FactoryVerification:
    """Decide whether this factory may be believed.

    ``observed_bytecode`` is whatever ``eth_getCode`` returned; ``None`` means
    the call did not succeed, which is distinct from the call succeeding and
    finding nothing there.
    """

    def result(state: str, **kw: object) -> FactoryVerification:
        return FactoryVerification(
            launchpad=launchpad,
            address=address,
            state=state,
            expected_digest=expected_digest,
            checked_at=checked_at,
            **kw,  # type: ignore[arg-type]
        )

    if not address:
        return result(NOT_CONFIGURED)
    if not reachable or observed_bytecode is None:
        return result(UNREACHABLE)

    observed = digest_of(observed_bytecode)
    empty = str(observed_bytecode or "").lower().removeprefix("0x").strip() in {"", "0"}
    if empty:
        return result(NO_CODE, observed_digest=observed)
    if not expected_digest:
        # There is code, but nothing independent to compare it against.  That
        # is not a pass: "something is deployed here" is not evidence that the
        # something is the contract we mean.
        return result(UNVERIFIED, observed_digest=observed)
    if observed.lower() != expected_digest.strip().lower():
        return result(
            MISMATCH,
            observed_digest=observed,
            detail="expected and observed runtime bytecode differ",
        )
    return result(VERIFIED, observed_digest=observed)


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    """One launchpad indexer's health, for ``/stonks status``."""

    launchpad: str
    verification: FactoryVerification = field(
        default_factory=lambda: FactoryVerification(launchpad="")
    )
    #: Last block this adapter has fully processed.  Persisted so a restart
    #: resumes rather than rescanning or skipping.
    cursor_block: int | None = None
    #: Blocks behind head before a launch is considered settled.
    confirmations: int = 3
    last_scan_at: int | None = None
    launches_seen: int = 0
    last_error: str = ""

    @property
    def enabled(self) -> bool:
        return self.verification.enabled

    def to_json(self) -> dict[str, object]:
        return {
            "launchpad": self.launchpad,
            "enabled": self.enabled,
            "verification": self.verification.to_json(),
            "cursor_block": self.cursor_block,
            "confirmations": self.confirmations,
            "last_scan_at": self.last_scan_at,
            "launches_seen": self.launches_seen,
            "last_error": self.last_error,
        }


def admissible(records: object, verification: FactoryVerification) -> bool:
    """Whether launches from this adapter may be acted on at all.

    Deliberately blunt and deliberately called at the boundary: an adapter that
    is not verified produces nothing, however well-formed its logs are.
    """

    return bool(records) and verification.enabled
