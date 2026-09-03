"""The four coins the operator named, and the copycats that must not pass.

These are regression evidence, not permission to hardcode a bullish answer.
Every fixture supplies the *proof* and lets the code decide; none of them
asserts that a real-world token is good. Market values are fixed snapshot
numbers from the brief, used only to exercise thresholds — no test here reads a
live value, so none of them can go stale into a false pass.

Covers specification tests 1-8, plus HARDER (35).
"""

from __future__ import annotations

from decimal import Decimal

from smart_money_bot.stocks.registry import ROBINHOOD_CHAIN_ID, build_registry
from smart_money_bot.stocks.traction import (
    STAGE_0_VERIFIED,
    STAGE_1_SPARK,
    STAGE_3_ENTRY,
    HolderSnapshot,
    climb,
    measure,
)
from smart_money_bot.stocks.verification import (
    NOT_STOCK_LINKED,
    PROOF_LAUNCH_RECORD,
    PROOF_POOL_PAIRING,
    REASON_ANCHOR_NOT_A_STOCK,
    REASON_POOL_MISMATCH,
    REASON_UNVERIFIED_FACTORY,
    LaunchRecord,
    verify_anchor,
)

NOW = 1_700_000_000

# Meme contracts exactly as the brief names them.
ARTIFICIAL_INU = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
BONER = "0x98096d17e191b3da1d5f99a6d7b3584351b11e18"
SOL_ON_INDA = "0x208092689248d96aa7f30aab09ff6a7e05b41e18"

# Stock-token addresses are FIXTURES. The real ones come from /rhj/assets at
# runtime; inventing them here would be the exact thing this lane refuses.
NVDA = "0xnvda00000000000000000000000000000000a001"
HIMS = "0xhims00000000000000000000000000000000a002"
INDA = "0xinda00000000000000000000000000000000a003"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
IMPOSTOR = "0xfake00000000000000000000000000000000a009"


def _assets(*symbols: str) -> list[dict]:
    known = {"NVDA": NVDA, "HIMS": HIMS, "INDA": INDA}
    return [
        {
            "tokenSymbol": symbol,
            "tokenName": f"{symbol} Inc.",
            "status": "active",
            "deployments": [
                {"chainId": ROBINHOOD_CHAIN_ID, "contractAddress": known[symbol]}
            ],
        }
        for symbol in symbols
    ]


def _registry(*symbols: str):
    return build_registry(_assets(*(symbols or ("NVDA", "HIMS", "INDA"))), fetched_at=NOW)


def _launch(meme: str, **overrides) -> LaunchRecord:
    values = dict(
        meme_address=meme,
        launchpad="Pons",
        factory_address="0xfactory",
        factory_verified=True,
        transaction_hash=f"0xtx{meme[-6:]}",
        log_index=1,
        block_number=1_000,
        launched_at=NOW,
    )
    values.update(overrides)
    return LaunchRecord(**values)


# ===========================================================================
# 1, 2, 3 — the positives, accepted ONLY on address-level proof
# ===========================================================================


def test_1_artificial_inu_needs_the_exact_nvda_contract() -> None:
    accepted = verify_anchor(
        _launch(ARTIFICIAL_INU, anchor_addresses=(NVDA,)), _registry(), now=NOW
    )
    assert accepted.verified is True
    assert accepted.proof == PROOF_LAUNCH_RECORD
    assert accepted.primary.symbol == "NVDA"

    # The same launch with NVDA absent from /rhj/assets is refused: the proof
    # is the registry match, not the launch record's own claim.
    without = verify_anchor(
        _launch(ARTIFICIAL_INU, anchor_addresses=(NVDA,)), _registry("HIMS", "INDA"), now=NOW
    )
    assert without.verified is False
    assert REASON_ANCHOR_NOT_A_STOCK in without.reasons


def test_2_boner_needs_the_exact_hims_contract() -> None:
    accepted = verify_anchor(_launch(BONER, anchor_addresses=(HIMS,)), _registry(), now=NOW)
    assert accepted.verified is True
    assert accepted.primary.symbol == "HIMS"

    wrong_stock = verify_anchor(
        _launch(BONER, anchor_addresses=(NVDA,)), _registry(), now=NOW
    )
    # Still verified — but to NVDA, not HIMS. The anchor is whatever the chain
    # says, never what the name implies.
    assert wrong_stock.primary.symbol == "NVDA"


def test_3_sol_on_inda_is_accepted_via_the_canonical_pool() -> None:
    accepted = verify_anchor(
        _launch(SOL_ON_INDA, anchor_addresses=(), pool_token_addresses=(SOL_ON_INDA, INDA)),
        _registry(),
        now=NOW,
    )
    assert accepted.verified is True
    assert accepted.proof == PROOF_POOL_PAIRING
    assert accepted.primary.symbol == "INDA"


# ===========================================================================
# 4, 5, 6, 7, 8 — the negatives
# ===========================================================================


def test_4_same_name_and_more_liquidity_but_the_wrong_anchor_is_not_stock_linked() -> None:
    """A costume with deeper pockets is still a costume.

    No market figure appears anywhere in the proof path, so there is no number
    this token could have raised to win.
    """

    costume = verify_anchor(
        _launch(
            "0xc05tume0000000000000000000000000000a010",
            anchor_addresses=(),
            pool_token_addresses=("0xc05tume0000000000000000000000000000a010", WETH),
        ),
        _registry(),
        now=NOW,
    )
    assert costume.verified is False
    assert costume.proof == NOT_STOCK_LINKED
    assert costume.primary is None


def test_5_a_fake_stock_token_wearing_a_real_ticker_is_rejected() -> None:
    # Symbol NVDA, address absent from /rhj/assets.
    result = verify_anchor(
        _launch(ARTIFICIAL_INU, anchor_addresses=(IMPOSTOR,)), _registry(), now=NOW
    )
    assert result.verified is False
    assert REASON_ANCHOR_NOT_A_STOCK in result.reasons


def test_6_the_site_saying_on_inda_loses_to_what_the_pool_actually_holds() -> None:
    """Stonks displaying "$SOL on INDA" is discovery, not proof.

    Here the launch pairs the meme with WETH. The page's label is not consulted
    at all — there is no parameter for it — so the answer comes from the pool.
    """

    result = verify_anchor(
        _launch(SOL_ON_INDA, anchor_addresses=(), pool_token_addresses=(SOL_ON_INDA, WETH)),
        _registry(),
        now=NOW,
    )
    assert result.verified is False
    assert result.primary is None

    # And a pool that does not even contain the meme proves nothing about it.
    unrelated = verify_anchor(
        _launch(SOL_ON_INDA, anchor_addresses=(), pool_token_addresses=(INDA, WETH)),
        _registry(),
        now=NOW,
    )
    assert REASON_POOL_MISMATCH in unrelated.reasons


def test_7_an_unverified_factory_proves_nothing_with_a_perfect_event() -> None:
    result = verify_anchor(
        _launch(ARTIFICIAL_INU, anchor_addresses=(NVDA,), factory_verified=False),
        _registry(),
        now=NOW,
    )
    assert result.verified is False
    assert REASON_UNVERIFIED_FACTORY in result.reasons


def test_8_an_unresolved_launch_stays_out_of_the_operators_channel() -> None:
    from smart_money_bot.stocks.stages import STAGE_DIAGNOSTIC, classify_stage

    unresolved = verify_anchor(_launch(ARTIFICIAL_INU), _registry(), now=NOW)
    assert unresolved.verified is False
    decision = classify_stage(unresolved)
    assert decision.stage == STAGE_DIAGNOSTIC
    assert decision.publishable is False


# ===========================================================================
# 35 — HARDER: generic traction, never a stock claim
# ===========================================================================


def test_35_harder_like_traction_never_implies_a_stock_link() -> None:
    """Strong metrics do not create an anchor.

    The brief is explicit that HARDER is a traction example and not a verified
    stock-linked one, so the traction ladder may evaluate it while the
    verification path still refuses to name an anchor.
    """

    harder = "0xharder000000000000000000000000000000a020"
    # 29 minutes old, 1K holders, $103.7K liquidity, +3,329% — the numbers from
    # the brief, as a fixed snapshot.
    launch = NOW - 1_740
    series = [
        HolderSnapshot(
            at=launch + 300, economic_holders=180, raw_holders=190,
            independent_buyers=60, independent_sellers=14,
            liquidity_usd=Decimal("60000"), cluster_adjusted_top10=Decimal("0.20"),
            volume_usd=Decimal("400000"),
        ),
        HolderSnapshot(
            at=NOW, economic_holders=1_000, raw_holders=1_040,
            independent_buyers=310, independent_sellers=90,
            liquidity_usd=Decimal("103700"), cluster_adjusted_top10=Decimal("0.1697"),
            volume_usd=Decimal("1800000"),
        ),
    ]
    metrics = measure(series, launched_at=launch, now=NOW)
    ladder = climb(series[-1], metrics, age_seconds=1_740)

    # The traction evidence is genuinely strong.
    assert ladder.stage == STAGE_3_ENTRY

    # And it is still not stock-linked, because nothing proved an anchor.
    proof = verify_anchor(_launch(harder), _registry(), now=NOW)
    assert proof.verified is False
    assert proof.proof == NOT_STOCK_LINKED


# ===========================================================================
# The SOL-on-INDA miss, end to end
# ===========================================================================


def test_the_sol_on_inda_miss_would_now_surface_during_the_climb() -> None:
    """The operator saw it near $30K and could not tell whether it was real.

    Verified at launch, spoken about at twenty-five holders — not held silent
    until the board showed +7,081%.
    """

    proof = verify_anchor(
        _launch(SOL_ON_INDA, anchor_addresses=(INDA,)), _registry(), now=NOW
    )
    assert proof.verified is True

    launch = NOW - 2_880  # the ~48 minutes the brief describes
    early = [
        HolderSnapshot(
            at=launch + 120, economic_holders=8, raw_holders=11,
            independent_buyers=5, independent_sellers=0,
            liquidity_usd=Decimal("4000"), cluster_adjusted_top10=Decimal("0.35"),
            volume_usd=Decimal("6000"),
        ),
        HolderSnapshot(
            at=launch + 420, economic_holders=31, raw_holders=36,
            independent_buyers=14, independent_sellers=3,
            liquidity_usd=Decimal("12500"), cluster_adjusted_top10=Decimal("0.33"),
            volume_usd=Decimal("48000"),
        ),
    ]
    first = climb(
        early[0], measure(early[:1], launched_at=launch, now=early[0].at), age_seconds=120
    )
    second = climb(
        early[1], measure(early, launched_at=launch, now=early[1].at), age_seconds=420
    )

    assert first.stage == STAGE_0_VERIFIED, "verified and honest that nobody is in it yet"
    assert second.stage == STAGE_1_SPARK, "spoken about at 31 holders, seven minutes in"
    assert second.stage != STAGE_3_ENTRY, "and never mislabelled as an entry"


def test_no_test_in_this_file_asserts_a_live_market_value() -> None:
    """Fixtures are snapshots, not live reads.

    A test that called a current value would become flaky tomorrow and, worse,
    could turn into a false pass the day the market moved.
    """

    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(__file__).read_text())
    # Drop this function before scanning: its own pattern list would otherwise
    # be the only match, which is the check failing on itself.
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.FunctionDef)
            and node.name == "test_no_test_in_this_file_asserts_a_live_market_value"
        )
    ]
    source = ast.unparse(tree)
    for forbidden in ("requests.", "urlopen", "aiohttp", "http://", "https://api."):
        assert forbidden not in source, f"a live read crept into the fixtures: {forbidden}"
