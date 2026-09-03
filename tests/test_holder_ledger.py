"""Holders reconstructed from Transfer logs, not read off a card.

A provider's holder number is a claim. The operator has been burned by one
twice — a card silent about holders for a token FOMO showed as "No holders
yet", and a board figure nothing independent could confirm. The only auditable
answer is a ledger.

Covers specification tests 9-18.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from smart_money_bot.lab.holderledger import (
    DEAD_ADDRESS,
    EX_BURN,
    EX_EMPTY,
    EX_POOL,
    EX_SELF,
    EX_SYSTEM,
    HOLDER_DATA_CONFLICT,
    TAG_BUNDLER,
    TAG_CREATOR,
    TAG_INSIDER,
    TAG_SNIPER,
    ZERO_ADDRESS,
    AddressRole,
    HolderLedger,
    LedgerConfig,
    TransferLog,
    apply_logs,
    compare_with_provider,
    rollback_to,
)

TOKEN = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"  # Artificial Inu
POOL = "0xpoo1000000000000000000000000000000000001"
ROUTER = "0xr0u7er0000000000000000000000000000000002"
DEV = "0xdev0000000000000000000000000000000000003"
MYSTERY = "0xmystery00000000000000000000000000000004"


def _log(index: int, sender: str, recipient: str, value: str, **overrides) -> TransferLog:
    values = dict(
        transaction_hash=f"0x{index:064x}",
        log_index=index,
        block_number=100 + index,
        from_address=sender,
        to_address=recipient,
        value=Decimal(value),
    )
    values.update(overrides)
    return TransferLog(**values)


def _roles(**extra) -> dict[str, AddressRole]:
    roles = {
        POOL: AddressRole(address=POOL, is_pool=True, is_contract=True),
        ROUTER: AddressRole(address=ROUTER, is_system=True, is_contract=True),
        DEV: AddressRole(address=DEV, tags=(TAG_CREATOR,)),
        MYSTERY: AddressRole(address=MYSTERY, is_contract=True),
    }
    roles.update(extra)
    return roles


def _launch_logs() -> list[TransferLog]:
    return [
        _log(1, ZERO_ADDRESS, DEV, "1000000"),
        _log(2, DEV, POOL, "400000"),
        _log(3, POOL, "0xalice", "5000"),
        _log(4, POOL, "0xbob", "3000"),
        _log(5, POOL, ROUTER, "900"),
        _log(6, POOL, MYSTERY, "700"),
        _log(7, "0xalice", DEAD_ADDRESS, "5000"),
    ]


def _ledger(logs=None, roles=None) -> HolderLedger:
    return apply_logs(
        HolderLedger(token=TOKEN),
        logs if logs is not None else _launch_logs(),
        roles=roles if roles is not None else _roles(),
        observed_at=1_700_000_000,
    )


# ===========================================================================
# 9, 10, 11 — reconstruction and the two counts
# ===========================================================================


def test_9_balances_reconstruct_exactly_from_mints_transfers_and_burns() -> None:
    ledger = _ledger()

    assert ledger.balance_of(DEV) == Decimal("600000")
    assert ledger.balance_of(POOL) == Decimal("390400")
    assert ledger.balance_of("0xbob") == Decimal("3000")
    assert ledger.balance_of("0xalice") == Decimal("0")
    assert ledger.total_minted == Decimal("1000000")
    assert ledger.total_burned == Decimal("5000")
    assert ledger.circulating == Decimal("995000")


def test_10_machinery_is_excluded_from_economic_holders_but_kept_in_raw() -> None:
    """An explorer counts wallets. This has to count people.

    Nothing is dropped silently: raw keeps everything with a balance, which is
    what makes the exclusions auditable rather than a matter of trust.
    """

    ledger = _ledger()

    assert POOL in ledger.raw_holders
    assert ROUTER in ledger.raw_holders
    assert ledger.exclusion_for(POOL) == EX_POOL
    assert ledger.exclusion_for(ROUTER) == EX_SYSTEM
    assert ledger.exclusion_for(TOKEN) == EX_SELF
    assert ledger.exclusion_for("0xalice") == EX_EMPTY
    assert ledger.exclusion_for(ZERO_ADDRESS) == EX_BURN

    economic = ledger.economic_holders()
    assert POOL not in economic
    assert ROUTER not in economic
    assert "0xalice" not in economic
    assert "0xbob" in economic


def test_10b_an_unclassified_contract_is_flagged_rather_than_dropped() -> None:
    # An exclusion nobody can explain and a bug look the same from outside.
    ledger = _ledger()
    assert MYSTERY in ledger.unclassified_contracts
    assert MYSTERY in ledger.raw_holders
    assert MYSTERY in ledger.economic_holders()


def test_11_excluding_the_pool_does_not_erase_creator_or_insider_exposure() -> None:
    """The fix quietly undoing the protection, prevented.

    Removing the LP vault from the holder count is right. Letting that also
    remove the creator's stack from the risk picture would be the whole point
    of the exclusion turned against itself.
    """

    ledger = _ledger()
    assert ledger.tagged_balance(TAG_CREATOR) == Decimal("600000")

    tagged = _ledger(
        roles=_roles(
            **{
                "0xbob": AddressRole(address="0xbob", tags=(TAG_SNIPER, TAG_BUNDLER)),
                MYSTERY: AddressRole(address=MYSTERY, is_contract=True, tags=(TAG_INSIDER,)),
            }
        )
    )
    assert tagged.tagged_balance(TAG_SNIPER) == Decimal("3000")
    assert tagged.tagged_balance(TAG_INSIDER) == Decimal("700")
    payload = tagged.to_json()
    for tag in ("creator", "insider", "sniper", "bundler"):
        assert f"{tag}_balance" in payload


# ===========================================================================
# 12, 13 — reorgs and restarts are ordinary, not edge cases
# ===========================================================================


def test_12_a_reorg_rolls_balances_and_the_cursor_back() -> None:
    ledger = _ledger()
    assert ledger.last_block == 107

    rolled = rollback_to(ledger, block_number=104, logs=_launch_logs())

    assert rolled.last_block == 104
    # Logs 5, 6 and 7 are undone: the router and mystery holdings never
    # happened, and alice's burn is reversed so she holds again.
    assert rolled.balance_of(ROUTER) == Decimal("0")
    assert rolled.balance_of(MYSTERY) == Decimal("0")
    assert rolled.balance_of("0xalice") == Decimal("5000")
    assert rolled.total_burned == Decimal("0")


def test_12b_a_removed_log_is_reversed_rather_than_left_in_place() -> None:
    ledger = _ledger()
    removed = apply_logs(ledger, [_log(4, POOL, "0xbob", "3000", removed=True)])
    assert removed.balance_of("0xbob") == Decimal("0")
    assert removed.balance_of(POOL) == Decimal("393400")


def test_12c_reversing_a_log_never_applied_changes_nothing() -> None:
    # A ledger that reversed a transfer it never saw would corrupt itself worse
    # than the reorg did.
    ledger = _ledger()
    phantom = _log(99, POOL, "0xnobody", "12345", removed=True)
    assert apply_logs(ledger, [phantom]).balances == ledger.balances


def test_13_replaying_overlapping_ranges_is_idempotent() -> None:
    logs = _launch_logs()
    once = _ledger(logs)
    twice = apply_logs(once, logs)
    thrice = apply_logs(twice, logs[3:] + logs)

    assert twice.balances == once.balances
    assert thrice.balances == once.balances
    assert thrice.total_minted == once.total_minted


def test_13b_logs_apply_in_block_and_index_order_whatever_order_they_arrive() -> None:
    logs = _launch_logs()
    forwards = _ledger(logs)
    backwards = _ledger(list(reversed(logs)))
    assert forwards.balances == backwards.balances


# ===========================================================================
# 14 — a provider may corroborate; it may never overrule
# ===========================================================================


def test_14_a_provider_claiming_far_more_holders_is_a_conflict() -> None:
    ledger = _ledger()
    comparison = compare_with_provider(ledger, 265)

    assert comparison.conflict is True
    assert comparison.to_json()["code"] == HOLDER_DATA_CONFLICT
    assert "265" in comparison.detail
    assert str(comparison.reconstructed) in comparison.detail
    # Both figures survive into the payload, with the gap stated.
    payload = comparison.to_json()
    assert payload["provider_reported_holder_count"] == 265
    assert payload["provider_vs_onchain_difference"] == 265 - comparison.reconstructed


def test_14b_a_provider_claiming_far_fewer_is_equally_a_conflict() -> None:
    many = [_log(index + 20, POOL, f"0xh{index:036x}", "100") for index in range(60)]
    ledger = _ledger(_launch_logs() + many)
    assert compare_with_provider(ledger, 3).conflict is True


def test_14c_a_modest_gap_is_not_worth_arguing_about() -> None:
    ledger = _ledger()
    reconstructed = len(ledger.economic_holders())
    assert compare_with_provider(ledger, reconstructed + 2).conflict is False
    assert compare_with_provider(ledger, None).conflict is False


def test_14d_a_provider_reporting_holders_where_the_chain_has_none_conflicts() -> None:
    empty = apply_logs(HolderLedger(token=TOKEN), [], roles=_roles())
    result = compare_with_provider(empty, 265)
    assert result.conflict is True
    assert "reconstructs none" in result.detail


# ===========================================================================
# 16, 17 — wallets are not actors
# ===========================================================================


def test_16_fifty_wallets_on_one_funder_are_one_economic_holder() -> None:
    wallets = [f"0xw{index:038x}" for index in range(50)]
    logs = _launch_logs() + [
        _log(20 + index, POOL, wallet, "100") for index, wallet in enumerate(wallets)
    ]
    roles = _roles(
        **{
            wallet: AddressRole(address=wallet, cluster_id="FUNDER-A")
            for wallet in wallets
        }
    )
    ledger = _ledger(logs, roles)

    assert len(ledger.raw_holders) >= 50
    economic = ledger.economic_holders()
    clustered = [item for item in economic if item in wallets]
    assert len(clustered) == 1, "fifty envelopes, one person"


def test_16b_unclustered_wallets_each_count() -> None:
    wallets = [f"0xu{index:038x}" for index in range(20)]
    logs = _launch_logs() + [
        _log(20 + index, POOL, wallet, "100") for index, wallet in enumerate(wallets)
    ]
    ledger = _ledger(logs, _roles())
    assert len([item for item in ledger.economic_holders() if item in wallets]) == 20


def test_17_raw_and_cluster_adjusted_concentration_are_both_stored() -> None:
    """One person spread across twenty wallets looks broadly held until you add
    them back up. Both numbers are kept because both are true answers to
    different questions."""

    spread = [f"0xs{index:038x}" for index in range(20)]
    others = [f"0xo{index:038x}" for index in range(20)]
    logs = [_log(1, ZERO_ADDRESS, DEV, "1000000"), _log(2, DEV, POOL, "100000")]
    logs += [_log(10 + i, POOL, w, "20000") for i, w in enumerate(spread)]
    logs += [_log(40 + i, POOL, w, "5000") for i, w in enumerate(others)]
    roles = _roles(
        **{w: AddressRole(address=w, cluster_id="ONE-WHALE") for w in spread}
    )
    ledger = _ledger(logs, roles)

    raw = ledger.concentration(10, cluster_adjusted=False)
    adjusted = ledger.concentration(10, cluster_adjusted=True)
    assert raw is not None and adjusted is not None
    assert adjusted > raw, "collapsing the cluster must raise measured concentration"
    payload = ledger.to_json()
    assert payload["top_10"] is not None
    assert payload["cluster_adjusted_top_10"] is not None


def test_the_pool_is_not_counted_as_supply_somebody_holds() -> None:
    ledger = _ledger()
    # The LP vault holds most of the float; counting it as concentration would
    # make every healthy token look owned by one address.
    assert ledger.concentration(1, cluster_adjusted=False) is not None
    assert ledger.balance_of(POOL) > ledger.balance_of("0xbob")
    top1 = ledger.concentration(1, cluster_adjusted=False)
    assert top1 < Decimal("1"), "the pool must not be the top holder"


# ===========================================================================
# 18 — the pattern the operator already lost money to
# ===========================================================================


def test_18_a_thin_top_heavy_token_reads_as_top_heavy() -> None:
    """79 holders, ~86.5% top-10, ~$3K liquidity — still unsafe.

    The ledger's job here is only to report the concentration honestly; the
    stage ladder is what refuses it. This proves the number reaches the gate.
    """

    whales = [f"0xwh{index:037x}" for index in range(5)]
    tail = [f"0xt{index:038x}" for index in range(74)]
    logs = [_log(1, ZERO_ADDRESS, DEV, "1000000"), _log(2, DEV, POOL, "50000")]
    logs += [_log(10 + i, DEV, w, "160000") for i, w in enumerate(whales)]
    logs += [_log(40 + i, DEV, w, "200") for i, w in enumerate(tail)]
    ledger = _ledger(logs, _roles())

    assert len(ledger.economic_holders()) == 79 + 1  # the tail, the whales, dev
    top10 = ledger.concentration(10, cluster_adjusted=False)
    assert top10 is not None and top10 > Decimal("0.8")


# ===========================================================================
# Standing rules
# ===========================================================================


def test_the_ledger_is_immutable_so_a_replay_answers_historically() -> None:
    before = _ledger()
    snapshot = dict(before.balances)
    apply_logs(before, [_log(50, POOL, "0xlater", "999")])
    assert before.balances == snapshot


def test_the_module_holds_no_provider_rpc_database_or_signer() -> None:
    import smart_money_bot.lab.holderledger as module

    source = inspect.getsource(module)
    for forbidden in (
        "import aiohttp", "aiosqlite", "eth_getLogs", "web3", "private_key",
        "send_transaction", "requests.",
    ):
        assert forbidden not in source, (
            f"this module is handed logs; it does not fetch: {forbidden}"
        )


def test_dust_balances_can_be_configured_out_of_the_holder_count() -> None:
    logs = _launch_logs() + [_log(30, POOL, "0xdust", "0.0000001")]
    ledger = _ledger(logs, _roles())
    assert "0xdust" in ledger.economic_holders()
    strict = LedgerConfig(dust_balance=Decimal("0.001"))
    assert "0xdust" not in ledger.economic_holders(config=strict)
