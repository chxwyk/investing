"""A factory address is not trusted because someone wrote it down.

The specification is blunt: *never guess a contract address*, and *if LONG
cannot be independently verified, keep the LONG adapter disabled with a clear
status instead of inventing an address*.

It would be incoherent to reject a coin for asserting an anchor by name while
accepting a factory because a developer pasted it into a source file. So an
adapter is disabled until it proves itself on chain against a digest the
operator supplied from an independent source, and every other outcome — no
code, wrong code, unreachable chain, no digest configured — leaves it off with
a reason a human can act on.
"""

from __future__ import annotations

import inspect

from smart_money_bot.stocks.launchpads import (
    DOCUMENTED,
    MISMATCH,
    NO_CODE,
    NOT_CONFIGURED,
    UNREACHABLE,
    UNVERIFIED,
    VERIFIED,
    AdapterStatus,
    FactoryVerification,
    admissible,
    digest_of,
    verify_factory,
)

NOW = 1_700_000_000
CODE = "0x6080604052348015600f57600080fd5b5060043610603c5760003560e01c"
GOOD = digest_of(CODE)


def _verify(**overrides) -> FactoryVerification:
    values = dict(
        address="0xFactory0000000000000000000000000000000001",
        expected_digest=GOOD,
        observed_bytecode=CODE,
        checked_at=NOW,
    )
    values.update(overrides)
    return verify_factory("Pons", **values)


def test_a_matching_digest_is_the_only_thing_that_enables_an_adapter() -> None:
    assert _verify().state == VERIFIED
    assert _verify().enabled is True


def test_every_other_outcome_leaves_the_adapter_disabled() -> None:
    for label, kwargs, expected in (
        ("no digest to compare against", {"expected_digest": ""}, UNVERIFIED),
        ("different code deployed", {"observed_bytecode": "0xdeadbeef"}, MISMATCH),
        ("nothing deployed there", {"observed_bytecode": "0x"}, NO_CODE),
        ("chain unreachable", {"observed_bytecode": None}, UNREACHABLE),
        ("no address configured", {"address": ""}, NOT_CONFIGURED),
    ):
        result = _verify(**kwargs)
        assert result.state == expected, label
        assert result.enabled is False, f"{label} must not enable the adapter"


def test_code_being_present_is_not_evidence_that_it_is_the_right_code() -> None:
    """The subtle one.

    "Something is deployed at this address" is not the same claim as "the
    contract we mean is deployed at this address", and only the second one
    justifies believing its events.
    """

    result = _verify(expected_digest="")
    assert result.observed_digest, "the digest was still computed"
    assert result.state == UNVERIFIED
    assert result.enabled is False


def test_a_mismatch_reports_an_upgraded_proxy_or_the_wrong_contract() -> None:
    result = _verify(observed_bytecode="0x60806040FFFF")
    assert result.state == MISMATCH
    assert "proxy was upgraded" in result.human()


def test_an_unreachable_chain_is_not_a_verdict_about_the_contract() -> None:
    # A failure to look is different from a failure of the thing looked at,
    # and neither of them is a pass.
    unreachable = _verify(observed_bytecode=None, reachable=False)
    assert unreachable.state == UNREACHABLE
    assert unreachable.enabled is False


def test_the_digest_ignores_prefix_and_case_but_nothing_else() -> None:
    assert digest_of(CODE) == digest_of(CODE.upper())
    assert digest_of(CODE) == digest_of(CODE.removeprefix("0x"))
    assert digest_of(CODE) == digest_of(bytes.fromhex(CODE.removeprefix("0x")))
    assert digest_of(CODE) != digest_of(CODE + "00")


def test_an_unverified_adapter_produces_nothing_however_good_its_logs_are() -> None:
    logs = [{"looks": "perfectly well formed"}]
    assert admissible(logs, _verify()) is True
    assert admissible(logs, _verify(expected_digest="")) is False
    assert admissible(logs, _verify(observed_bytecode=None)) is False


# --- specification test 7 ----------------------------------------------------


def test_no_factory_address_is_hardcoded_as_an_enabled_default() -> None:
    """Spec test 7, and the rule behind it.

    What ships in the source is a *documented candidate* with its provenance —
    a starting point for an operator — carrying no authority. Nothing in this
    package enables an adapter from one.
    """

    for candidate in DOCUMENTED:
        assert isinstance(candidate.address, str)
        # A candidate is inert: it has no digest, so it can never verify.
        result = verify_factory(
            candidate.launchpad,
            address=candidate.address,
            expected_digest="",
            observed_bytecode=CODE,
            checked_at=NOW,
        )
        assert result.enabled is False


def test_long_ships_disabled_with_a_stated_reason_rather_than_a_guess() -> None:
    long = next(item for item in DOCUMENTED if item.launchpad == "LONG")
    assert long.address == "", "an address was invented for LONG"
    assert "disabled rather than guessing" in long.note


def test_pons_records_the_disagreement_rather_than_picking_a_side() -> None:
    pons = next(item for item in DOCUMENTED if item.launchpad == "Pons")
    assert pons.address == ""
    assert "disagree" in pons.note
    assert "ponsfamily" in pons.source


def test_pair_records_that_a_proxy_needs_its_implementation_checked() -> None:
    pair = next(item for item in DOCUMENTED if item.launchpad == "Pair")
    assert pair.address.lower().startswith("0x")
    assert "proxy" in pair.note
    assert "implementation can change" in pair.note
    # Documented, and still not enabled by anything here.
    assert pair.source


def test_the_module_contains_no_expected_digest_for_any_factory() -> None:
    # A digest committed to source would be the same trust-me claim as a
    # committed address, one step removed.
    import smart_money_bot.stocks.launchpads as module

    source = inspect.getsource(module)
    import re

    assert not re.search(r"[0-9a-f]{64}", source), "a bytecode digest was hardcoded"


def test_an_adapter_status_reports_its_cursor_for_restart_safety() -> None:
    status = AdapterStatus(
        launchpad="Pons", verification=_verify(), cursor_block=1_234, confirmations=3
    )
    payload = status.to_json()
    assert payload["enabled"] is True
    assert payload["cursor_block"] == 1_234
    assert payload["verification"]["state"] == VERIFIED


def test_a_disabled_adapter_status_still_explains_itself() -> None:
    status = AdapterStatus(launchpad="LONG", verification=_verify(address=""))
    assert status.enabled is False
    assert "no factory address configured" in status.verification.human()


def test_the_module_holds_no_provider_database_or_signer() -> None:
    import smart_money_bot.stocks.launchpads as module

    source = inspect.getsource(module)
    for forbidden in ("import aiohttp", "aiosqlite", "private_key", "cookies=", "eth_getCode("):
        assert forbidden not in source, "this module decides; it does not fetch"


# --- configuration (spec sections 6 and 9) -----------------------------------


def test_the_whole_subsystem_is_off_by_default(settings) -> None:
    # A lane that can alert must be switched on deliberately, not arrive
    # enabled because it was merged.
    assert settings.stonks_enabled is False
    assert settings.stonks_research_only is True


def test_no_channel_is_hardcoded(settings) -> None:
    assert settings.stonks_channel_id is None


def test_every_adapter_ships_without_an_address_or_a_digest(settings) -> None:
    for field in (
        "stonks_pons_factory", "stonks_pons_digest",
        "stonks_pair_factory", "stonks_pair_digest",
        "stonks_long_factory", "stonks_long_digest",
    ):
        assert getattr(settings, field) == "", f"{field} ships with a value"


def test_the_chain_id_is_pinned_to_robinhood_chain(monkeypatch, settings) -> None:
    import dataclasses

    import pytest

    assert settings.stonks_chain_id == 4663
    # Reusing one chain's assumptions for another is the mistake this pin
    # exists to prevent: the registry and the indexers would read different
    # chains and every anchor proof would be meaningless.
    with pytest.raises(ValueError, match="4663"):
        dataclasses.replace(settings, stonks_chain_id=1).validate()


def test_research_only_cannot_be_switched_off(settings) -> None:
    import dataclasses

    import pytest

    # There is no order path behind this lane. Refusing the flag is cheaper
    # than letting an operator believe setting it did something.
    with pytest.raises(ValueError, match="no execution path"):
        dataclasses.replace(settings, stonks_research_only=False).validate()


def test_a_backfill_is_bounded_rather_than_from_genesis(settings) -> None:
    assert 0 < settings.stonks_backfill_blocks <= 5_000_000


# --- the production verification script --------------------------------------


def test_the_verification_script_is_read_only_by_construction() -> None:
    """The operator runs this where the network works; it must never write.

    Its whole job is to print a bytecode digest and a chain id so an adapter
    can be configured. Printing a digest is not the same as trusting one — the
    script enables nothing, and the operator still compares what it prints
    against an independent source.
    """

    import ast
    import pathlib

    source = pathlib.Path("scripts/stonks_verify.py").read_text()
    tree = ast.parse(source)
    # Strip docstrings: the module explains at length that it signs nothing,
    # and grepping prose would flag the promise rather than a violation.
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    code = ast.unparse(tree)

    for forbidden in (
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "eth_sign",
        "personal_",
        "private_key",
        "sqlite",
        ".write(",
        "json.dump(",
    ):
        assert forbidden not in code, f"the diagnostic must stay read-only: {forbidden}"

    # And it imports nothing that could persist anything. (`urlopen` legitimately
    # contains the substring "open(", so the check above is deliberately about
    # write *methods* rather than any name containing "open".)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, ast.Import) else [])
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= {"json", "sys", "urllib", "hashlib", "__future__"}, imported

    # Only these RPC methods, all of them reads.
    for method in ("eth_chainId", "eth_blockNumber", "eth_getCode"):
        assert method in code
