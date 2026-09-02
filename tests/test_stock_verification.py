"""A name is never an anchor.  Only an address is.

The rule the whole stock lane rests on, and the one the operator has been asking
for since the first fake coin. A meme is linked to a stock only when an
address-level proof succeeds:

1. a verified launchpad factory's launch record names an anchor address that is
   an active chain-4663 deployment in Robinhood's ``/rhj/assets``, or
2. the canonical pool from that verified launch holds the meme on one side and
   an active Robinhood Stock Token on the other.

Ticker, name, description, logo, website, socials, creator claims and terminal
cards are never sufficient. The case that matters most is the one the operator
kept being shown: **a costume token with more liquidity, more volume and a
higher FDV than the genuine one is still rejected**, because depth is not
evidence of a relationship that does not exist.

Fixtures only — no network, no database, no wallet. The addresses below are the
ones named in the specification; none of them is treated as verified by this
suite, they are fixture inputs.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from smart_money_bot.stocks import (
    NOT_STOCK_LINKED,
    PROOF_LAUNCH_RECORD,
    PROOF_POOL_PAIRING,
    REASON_ANCHOR_NOT_A_STOCK,
    REASON_POOL_MISMATCH,
    REASON_REGISTRY_UNUSABLE,
    REASON_UNVERIFIED_FACTORY,
    ROBINHOOD_CHAIN_ID,
    LaunchRecord,
    StockRegistry,
    build_registry,
    verify_anchor,
)

NOW = 1_700_000_000

# The meme contract named in the specification.
ARTIFICIAL_INU = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
# Fixture stock-token addresses. Real values come from /rhj/assets at runtime.
NVDA_TOKEN = "0xNVDA000000000000000000000000000000000001".lower()
TSLA_TOKEN = "0xTSLA000000000000000000000000000000000002".lower()
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
IMPOSTOR_NVDA = "0xFAKE000000000000000000000000000000000009"


def _assets() -> list[dict]:
    """A ``/rhj/assets`` payload in the documented shape."""

    return [
        {
            "tokenSymbol": "NVDA",
            "tokenName": "NVIDIA Corporation",
            "id": "asset-nvda",
            "status": "active",
            "multiplier": "1",
            "deployments": [
                {"chainId": ROBINHOOD_CHAIN_ID, "contractAddress": NVDA_TOKEN},
                # Same asset on another chain — a different token, dropped.
                {"chainId": 1, "contractAddress": "0xdeadbeef" + "0" * 32},
            ],
        },
        {
            "tokenSymbol": "TSLA",
            "tokenName": "Tesla, Inc.",
            "id": "asset-tsla",
            "status": "active",
            "deployments": [{"chainId": ROBINHOOD_CHAIN_ID, "contractAddress": TSLA_TOKEN}],
        },
        {
            "tokenSymbol": "DELISTED",
            "tokenName": "No Longer Listed",
            "status": "inactive",
            "deployments": [
                {"chainId": ROBINHOOD_CHAIN_ID, "contractAddress": "0x" + "b" * 40}
            ],
        },
    ]


def _registry(**overrides) -> StockRegistry:
    values = dict(payload=_assets(), fetched_at=NOW, ttl_seconds=3_600)
    values.update(overrides)
    return build_registry(**values)


def _launch(**overrides) -> LaunchRecord:
    values = dict(
        meme_address=ARTIFICIAL_INU,
        launchpad="Pons",
        factory_address="0xfactory",
        factory_verified=True,
        anchor_addresses=(NVDA_TOKEN,),
        pool_address="0xpool",
        transaction_hash="0xtx",
        log_index=3,
        block_number=1_000,
        launched_at=NOW,
    )
    values.update(overrides)
    return LaunchRecord(**values)


# ===========================================================================
# Spec test 1 — Artificial Inu positive case
# ===========================================================================


def test_artificial_inu_is_accepted_only_on_address_level_proof() -> None:
    proof = verify_anchor(_launch(), _registry(), now=NOW)

    assert proof.verified is True
    assert proof.proof == PROOF_LAUNCH_RECORD
    assert proof.primary is not None
    assert proof.primary.symbol == "NVDA"
    assert proof.primary.address == NVDA_TOKEN
    assert proof.meme_address == ARTIFICIAL_INU


def test_the_same_launch_without_the_registry_entry_is_refused() -> None:
    # The proof is the registry match, not the launch record. Strip NVDA from
    # /rhj/assets and the identical launch stops being anchored.
    without_nvda = build_registry(
        [row for row in _assets() if row["tokenSymbol"] != "NVDA"], fetched_at=NOW
    )
    proof = verify_anchor(_launch(), without_nvda, now=NOW)
    assert proof.verified is False
    assert proof.proof == NOT_STOCK_LINKED
    assert REASON_ANCHOR_NOT_A_STOCK in proof.reasons


def test_a_pool_pairing_is_the_second_admissible_proof() -> None:
    proof = verify_anchor(
        _launch(anchor_addresses=(), pool_token_addresses=(ARTIFICIAL_INU, NVDA_TOKEN)),
        _registry(),
        now=NOW,
    )
    assert proof.proof == PROOF_POOL_PAIRING
    assert proof.primary.symbol == "NVDA"


# ===========================================================================
# Spec test 2 — the NVDA costume, with MORE liquidity
# ===========================================================================


def test_a_costume_token_is_rejected_however_deep_it_is() -> None:
    """The case the operator kept being shown.

    Same symbol, same name, same branding, and deeper than the genuine one —
    paired with WETH rather than a stock token. Depth is not evidence of a
    relationship that does not exist.
    """

    costume = _launch(
        meme_address="0xC05TUME00000000000000000000000000000001".lower(),
        anchor_addresses=(),
        pool_token_addresses=("0xC05TUME00000000000000000000000000000001".lower(), WETH),
    )
    proof = verify_anchor(costume, _registry(), now=NOW)

    assert proof.verified is False
    assert proof.proof == NOT_STOCK_LINKED
    assert proof.primary is None
    # And the card can say why in words, not codes.
    assert any("not an active chain-4663" in item for item in proof.to_json()["reasons"])


def test_liquidity_and_fdv_are_absent_from_the_verification_path_entirely() -> None:
    # Structural: there is no number the costume could have raised to win.
    import smart_money_bot.stocks.verification as verification

    source = inspect.getsource(verification)
    for forbidden in ("liquidity", "fdv", "volume", "market_cap"):
        assert forbidden not in source.lower().split('"""')[-1], (
            "market data must not reach the anchor proof"
        )


def test_a_pool_that_does_not_hold_the_meme_proves_nothing() -> None:
    # An unrelated NVDA/WETH pool is not this meme's anchor, however real it is.
    proof = verify_anchor(
        _launch(anchor_addresses=(), pool_token_addresses=(NVDA_TOKEN, WETH)),
        _registry(),
        now=NOW,
    )
    assert proof.verified is False
    assert REASON_POOL_MISMATCH in proof.reasons


# ===========================================================================
# Spec test 3 — a fake stock token wearing the NVDA symbol
# ===========================================================================


def test_a_fake_stock_token_using_the_nvda_symbol_is_rejected() -> None:
    proof = verify_anchor(_launch(anchor_addresses=(IMPOSTOR_NVDA,)), _registry(), now=NOW)
    assert proof.verified is False
    assert REASON_ANCHOR_NOT_A_STOCK in proof.reasons


def test_there_is_no_way_to_look_a_stock_token_up_by_symbol() -> None:
    """The core rule, made structural rather than documented.

    Address in, token out. A symbol-keyed lookup is the substitution bug of
    v2.43.1 rebuilt on a new chain, so the reverse mapping simply does not
    exist anywhere in the package.
    """

    import smart_money_bot.stocks.registry as registry_module

    assert not hasattr(StockRegistry, "address_for_symbol")
    assert not hasattr(StockRegistry, "by_symbol")
    assert not hasattr(StockRegistry, "resolve_symbol")
    source = inspect.getsource(registry_module)
    assert "def address_for_symbol" not in source


def test_an_inactive_stock_token_cannot_back_a_proof() -> None:
    delisted = "0x" + "b" * 40
    proof = verify_anchor(_launch(anchor_addresses=(delisted,)), _registry(), now=NOW)
    assert proof.verified is False


def test_a_deployment_on_another_chain_is_a_different_asset() -> None:
    # Reusing one chain's assumptions for another is the mistake the chain-id
    # filter exists to prevent.
    registry = _registry()
    assert registry.resolve("0xdeadbeef" + "0" * 32, now=NOW) is None
    assert registry.resolve(NVDA_TOKEN, now=NOW) is not None


# ===========================================================================
# Spec test 5 — Pair multi-anchor, and deduplication
# ===========================================================================


def test_a_multi_anchor_launch_verifies_every_stock_token() -> None:
    # Pair V5 pairs a meme with one to five eligible stock tokens. Dropping the
    # extras would misreport the relationship.
    proof = verify_anchor(
        _launch(anchor_addresses=(NVDA_TOKEN, TSLA_TOKEN, IMPOSTOR_NVDA)),
        _registry(),
        now=NOW,
    )
    assert proof.verified is True
    assert {token.symbol for token in proof.anchors} == {"NVDA", "TSLA"}
    assert len(proof.anchors) == 2, "the impostor must not appear among the anchors"


def test_repeated_anchor_addresses_are_deduplicated() -> None:
    proof = verify_anchor(
        _launch(anchor_addresses=(NVDA_TOKEN, NVDA_TOKEN.upper(), NVDA_TOKEN)),
        _registry(),
        now=NOW,
    )
    assert len(proof.anchors) == 1


def test_a_launch_identity_is_unique_per_log() -> None:
    # Deduplication across restarts, retries and reorgs (spec test 13).
    a = _launch(transaction_hash="0xAA", log_index=1)
    b = _launch(transaction_hash="0xAA", log_index=2)
    assert a.identity != b.identity
    assert a.identity == _launch(transaction_hash="0xaa", log_index=1).identity


# ===========================================================================
# Spec test 6 — an unverified factory can emit anything it likes
# ===========================================================================


def test_an_unverified_factory_proves_nothing_even_with_a_perfect_event() -> None:
    """A lookalike contract can emit a flawless event naming a real stock token.

    Trusting it because it parsed would hand anybody a way to mint proofs, so
    the factory is checked before anything it said is read.
    """

    proof = verify_anchor(_launch(factory_verified=False), _registry(), now=NOW)
    assert proof.verified is False
    assert REASON_UNVERIFIED_FACTORY in proof.reasons


def test_the_factory_is_checked_before_its_event_is_read() -> None:
    import smart_money_bot.stocks.verification as verification

    source = inspect.getsource(verification.verify_anchor)
    assert source.index("factory_verified") < source.index("anchor_addresses")


# ===========================================================================
# Spec test 15 — registry outage, last-known-good, then fail closed
# ===========================================================================


def test_a_degraded_registry_inside_its_ttl_still_vouches() -> None:
    stale_but_usable = _registry(fetched_at=NOW - 600, degraded=True)
    assert stale_but_usable.usable(NOW) is True
    assert verify_anchor(_launch(), stale_but_usable, now=NOW).verified is True


def test_past_its_ttl_the_registry_stops_vouching_for_anything() -> None:
    expired = _registry(fetched_at=NOW - 7_200, ttl_seconds=3_600)
    assert expired.usable(NOW) is False
    proof = verify_anchor(_launch(), expired, now=NOW)
    assert proof.verified is False
    assert REASON_REGISTRY_UNUSABLE in proof.reasons


def test_a_registry_that_never_loaded_vouches_for_nothing() -> None:
    empty = StockRegistry()
    assert empty.usable(NOW) is False
    assert verify_anchor(_launch(), empty, now=NOW).verified is False


def test_the_registry_describes_its_own_state_for_diagnostics() -> None:
    assert "EXPIRED" in _registry(fetched_at=NOW - 99_999).describe(NOW)
    assert "DEGRADED" in _registry(degraded=True).describe(NOW)
    assert StockRegistry().describe(NOW) == "registry never loaded"


# ===========================================================================
# Standing rules
# ===========================================================================


def test_addresses_are_case_folded_in_both_directions() -> None:
    registry = _registry()
    assert registry.resolve(NVDA_TOKEN.upper(), now=NOW) is not None
    assert verify_anchor(
        _launch(anchor_addresses=(NVDA_TOKEN.upper(),)), registry, now=NOW
    ).verified is True


def test_the_meme_cannot_be_its_own_anchor() -> None:
    """A token cannot vouch for itself, even if it *is* in the registry.

    The pathological case the exclusion guards: a launch whose meme address is
    itself a listed stock token, naming itself as its own anchor. Using a meme
    address absent from the registry would make this pass for the wrong reason
    — the lookup would miss regardless — so the fixture is a real entry.
    """

    self_anchored = _launch(meme_address=NVDA_TOKEN, anchor_addresses=(NVDA_TOKEN,))
    proof = verify_anchor(self_anchored, _registry(), now=NOW)
    assert proof.verified is False
    assert proof.anchors == ()

    # Same for the pool path: meme on both sides is not a pairing.
    both_sides = _launch(
        meme_address=NVDA_TOKEN,
        anchor_addresses=(),
        pool_token_addresses=(NVDA_TOKEN, NVDA_TOKEN),
    )
    assert verify_anchor(both_sides, _registry(), now=NOW).verified is False


def test_the_verification_package_holds_no_provider_database_or_signer() -> None:
    import smart_money_bot.stocks.registry as registry_module
    import smart_money_bot.stocks.verification as verification

    for module in (registry_module, verification):
        source = inspect.getsource(module)
        for forbidden in (
            "import aiohttp", "import requests", "aiosqlite", "private_key", "cookies=",
        ):
            assert forbidden not in source, f"{module.__name__} must stay pure logic"


def test_market_data_cannot_be_confused_with_proof() -> None:
    # A high-FDV costume and a genuine anchor differ only in the proof, and the
    # proof object carries no market figure at all.
    proof = verify_anchor(_launch(), _registry(), now=NOW)
    payload = proof.to_json()
    assert "liquidity" not in payload
    assert "credible_value" not in payload
    assert Decimal  # market types are simply not part of this decision
