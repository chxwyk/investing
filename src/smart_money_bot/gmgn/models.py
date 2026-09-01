"""Parsed GMGN rows, with identity enforced at the door.

Every row that enters this codebase from GMGN passes through here, and the
parsers share one rule: **a row without a usable exact mint is dropped, not
guessed at.**  A symbol is display metadata; it never identifies a token, and
nothing downstream may resolve one from it (v2.43.1).

The second rule is about absence.  Provider payloads omit fields all the time,
and ``0`` is a measurement while ``None`` is the absence of one.  Every numeric
parser here returns ``None`` for a missing field, so "no holder count" can never
read as "no holders" — that conflation is what makes a degraded provider look
like a dangerous token (section 41).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..token_identity import is_valid_mint
from .signals import classify_signal

ZERO = Decimal("0")


def _d(value: Any) -> Decimal | None:
    """A number, or ``None``.  Never a zero standing in for a missing field."""

    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _mint_of(row: dict[str, Any]) -> str:
    """Pull the exact mint out of a row, accepting only a valid address.

    GMGN uses ``address`` for the token across the market endpoints and
    ``token_address`` in a few nested shapes.  Both are exact addresses; neither
    is a name, and anything that does not look like a mint is refused rather
    than passed along as a maybe.
    """

    for key in ("address", "token_address", "mint", "base_address"):
        candidate = _text(row.get(key)).strip()
        if candidate and is_valid_mint(candidate):
            return candidate
    return ""


# --- trending / hot search rows ---------------------------------------------


@dataclass(frozen=True, slots=True)
class GmgnToken:
    """One token row from a ranked GMGN feed, identified by its exact mint.

    Field names mirror GMGN's documented metrics so a reader can check them
    against `docs/cli-usage.md`; the values are ours only in the sense of being
    typed and made honest about absence.
    """

    mint: str
    symbol: str = ""
    name: str = ""
    rank: int | None = None
    interval: str = ""
    source: str = ""

    market_cap_usd: Decimal | None = None
    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    swaps: int | None = None
    holder_count: int | None = None
    price_change_percent: Decimal | None = None
    created_at: int | None = None
    history_highest_market_cap_usd: Decimal | None = None

    # ---- provider participant counts (evidence, not verdicts) ------------
    smart_degen_count: int | None = None
    renowned_count: int | None = None
    bot_degen_count: int | None = None
    visiting_count: int | None = None

    # ---- provider risk rates (section 16) --------------------------------
    insider_rate: Decimal | None = None
    bundler_rate: Decimal | None = None
    top10_holder_rate: Decimal | None = None
    sniper_hold_rate: Decimal | None = None
    dev_team_hold_rate: Decimal | None = None

    raw_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def age_seconds_at(self) -> int | None:
        return None if self.created_at is None else self.created_at

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "name": self.name,
            "rank": self.rank,
            "interval": self.interval,
            "source": self.source,
            "market_cap_usd": _s(self.market_cap_usd),
            "price_usd": _s(self.price_usd),
            "liquidity_usd": _s(self.liquidity_usd),
            "volume_usd": _s(self.volume_usd),
            "swaps": self.swaps,
            "holder_count": self.holder_count,
            "price_change_percent": _s(self.price_change_percent),
            "created_at": self.created_at,
            "history_highest_market_cap_usd": _s(self.history_highest_market_cap_usd),
            "smart_degen_count": self.smart_degen_count,
            "renowned_count": self.renowned_count,
            "bot_degen_count": self.bot_degen_count,
            "visiting_count": self.visiting_count,
            "insider_rate": _s(self.insider_rate),
            "bundler_rate": _s(self.bundler_rate),
            "top10_holder_rate": _s(self.top10_holder_rate),
            "sniper_hold_rate": _s(self.sniper_hold_rate),
            "dev_team_hold_rate": _s(self.dev_team_hold_rate),
        }


def parse_tokens(
    rows: Iterable[Any],
    *,
    interval: str = "",
    source: str = "",
) -> tuple[GmgnToken, ...]:
    """Parse a ranked list.  Rows without a valid exact mint are dropped.

    Rank is taken from the row when the provider supplies one and otherwise from
    position in the list, because the list *is* the ranking — but it is never
    invented for a row we could not identify.
    """

    parsed: list[GmgnToken] = []
    position = 0
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        mint = _mint_of(row)
        if not mint:
            continue
        position += 1
        parsed.append(
            GmgnToken(
                mint=mint,
                symbol=_text(row.get("symbol")),
                name=_text(row.get("name")),
                rank=_i(row.get("rank")) or position,
                interval=interval,
                source=source,
                market_cap_usd=_d(row.get("market_cap") or row.get("marketcap")),
                price_usd=_d(row.get("price")),
                liquidity_usd=_d(row.get("liquidity")),
                volume_usd=_d(row.get("volume")),
                swaps=_i(row.get("swaps")),
                holder_count=_i(row.get("holder_count") or row.get("holders")),
                price_change_percent=_d(row.get("price_change_percent")),
                created_at=_i(row.get("creation_timestamp") or row.get("created_timestamp")),
                history_highest_market_cap_usd=_d(row.get("history_highest_market_cap")),
                smart_degen_count=_i(row.get("smart_degen_count")),
                renowned_count=_i(row.get("renowned_count")),
                bot_degen_count=_i(row.get("bot_degen_count")),
                visiting_count=_i(row.get("visiting_count")),
                insider_rate=_d(row.get("insider_rate")),
                bundler_rate=_d(row.get("bundler_rate")),
                top10_holder_rate=_d(row.get("top10_holder_rate")),
                sniper_hold_rate=_d(row.get("top70_sniper_hold_rate")),
                dev_team_hold_rate=_d(row.get("dev_team_hold_rate")),
                raw_keys=tuple(sorted(row)),
            )
        )
    return tuple(parsed)


def parse_rank_response(payload: Any, *, interval: str) -> tuple[GmgnToken, ...]:
    """``/v1/market/rank`` — a list, or an object wrapping one."""

    rows = _rows_of(payload, ("rank", "tokens", "list", "data"))
    return parse_tokens(rows, interval=interval, source="gmgn_rank")


def parse_hot_searches_response(payload: Any) -> tuple[GmgnToken, ...]:
    """``/v1/market/hot_searches`` — blocks of ``(interval, chain, tokens)``."""

    blocks = payload if isinstance(payload, list) else _rows_of(payload, ("data", "list"))
    parsed: list[GmgnToken] = []
    for block in blocks or ():
        if not isinstance(block, dict):
            continue
        parsed.extend(
            parse_tokens(
                block.get("tokens") or (),
                interval=_text(block.get("interval")),
                source="gmgn_hot_search",
            )
        )
    return tuple(parsed)


def parse_trenches_response(payload: Any) -> dict[str, tuple[GmgnToken, ...]]:
    """``/v1/trenches`` — one keyed section per requested type.

    Unknown section names are kept rather than dropped: a new GMGN section is
    something to notice, and silently discarding it would hide a feed we are
    already paying for.
    """

    if not isinstance(payload, dict):
        return {}
    sections: dict[str, tuple[GmgnToken, ...]] = {}
    for name, value in payload.items():
        rows = _rows_of(value, ("tokens", "list", "data"))
        if not rows:
            continue
        sections[str(name)] = parse_tokens(rows, source=f"gmgn_trench_{name}")
    return sections


def _rows_of(payload: Any, keys: Sequence[str]) -> list[Any]:
    if isinstance(payload, list):
        return list(payload)
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            nested = _rows_of(value, keys)
            if nested:
                return nested
    return []


# --- market signals ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GmgnSignal:
    """One provider signal, named from the documented table (section 17)."""

    mint: str
    signal_type: object
    signal_name: str
    known: bool
    demand: bool
    triggered_at: int | None = None
    market_cap_usd: Decimal | None = None
    trigger_market_cap_usd: Decimal | None = None
    symbol: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "signal_type": self.signal_type,
            "signal_name": self.signal_name,
            "known": self.known,
            "demand": self.demand,
            "triggered_at": self.triggered_at,
            "market_cap_usd": _s(self.market_cap_usd),
            "trigger_market_cap_usd": _s(self.trigger_market_cap_usd),
        }


def parse_signals(payload: Any) -> tuple[GmgnSignal, ...]:
    rows = _rows_of(payload, ("signals", "list", "data", "items"))
    parsed: list[GmgnSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mint = _mint_of(row)
        if not mint:
            continue
        code = row.get("signal_type")
        classification = classify_signal(code)
        parsed.append(
            GmgnSignal(
                mint=mint,
                symbol=_text(row.get("symbol")),
                signal_type=code,
                signal_name=classification.name,
                known=classification.known,
                demand=classification.demand,
                triggered_at=_i(row.get("trigger_at") or row.get("triggered_at")),
                market_cap_usd=_d(row.get("market_cap")),
                trigger_market_cap_usd=_d(row.get("trigger_mc") or row.get("trigger_market_cap")),
            )
        )
    return tuple(parsed)


# --- participants: holders, traders, smart money, KOLs -----------------------


@dataclass(frozen=True, slots=True)
class GmgnParticipant:
    """A wallet GMGN has something to say about, for one exact mint.

    ``provider_tags`` records what GMGN called this wallet.  It is kept distinct
    from anything this bot concluded on its own, because a provider label is
    evidence about a classification, not evidence about future returns.
    """

    wallet: str
    mint: str = ""
    label: str = ""
    provider_tags: tuple[str, ...] = field(default_factory=tuple)
    holding_percent: Decimal | None = None
    holding_usd: Decimal | None = None
    cost_usd: Decimal | None = None
    realized_pnl_usd: Decimal | None = None
    unrealized_pnl_usd: Decimal | None = None
    bought_usd: Decimal | None = None
    sold_usd: Decimal | None = None
    buys: int | None = None
    sells: int | None = None
    last_active_at: int | None = None
    is_smart_money: bool = False
    is_kol: bool = False
    fresh_wallet: bool = False
    sniper: bool = False
    bundler: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "mint": self.mint,
            "label": self.label,
            "provider_tags": list(self.provider_tags),
            "holding_percent": _s(self.holding_percent),
            "holding_usd": _s(self.holding_usd),
            "cost_usd": _s(self.cost_usd),
            "realized_pnl_usd": _s(self.realized_pnl_usd),
            "unrealized_pnl_usd": _s(self.unrealized_pnl_usd),
            "bought_usd": _s(self.bought_usd),
            "sold_usd": _s(self.sold_usd),
            "buys": self.buys,
            "sells": self.sells,
            "last_active_at": self.last_active_at,
            "is_smart_money": self.is_smart_money,
            "is_kol": self.is_kol,
            "fresh_wallet": self.fresh_wallet,
            "sniper": self.sniper,
            "bundler": self.bundler,
        }


# ``True == 1`` in Python, so the integer form is already covered.
_TRUTHY = {True, "1", "true", "True", "yes"}


def _flag(row: dict[str, Any], *keys: str) -> bool:
    return any(row.get(key) in _TRUTHY for key in keys)


def parse_participants(payload: Any, *, mint: str) -> tuple[GmgnParticipant, ...]:
    """Top holders or top traders for one exact mint.

    ``mint`` is passed in rather than read from the rows: the request was for a
    specific address and the answer belongs to that address, so a row that
    happens to name a different token is a provider bug, not a discovery.
    """

    rows = _rows_of(payload, ("holders", "traders", "list", "data", "items"))
    parsed: list[GmgnParticipant] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        wallet = _text(row.get("address") or row.get("wallet_address") or row.get("wallet"))
        if not wallet:
            continue
        row_mint = _mint_of(row)
        if row_mint and row_mint != mint:
            # The answer must be about the token we asked about.
            continue
        tags = tuple(
            sorted({_text(item) for item in (row.get("tags") or ()) if _text(item)})
        )
        smart = _flag(row, "is_smart_money", "smart_money", "is_smart_degen") or (
            "smart_money" in tags or "smart_degen" in tags
        )
        kol = _flag(row, "is_kol", "kol", "is_renowned") or "kol" in tags
        parsed.append(
            GmgnParticipant(
                wallet=wallet,
                mint=mint,
                label=_text(row.get("name") or row.get("twitter_username") or row.get("label")),
                provider_tags=tags,
                holding_percent=_d(row.get("holding_percentage") or row.get("percentage")),
                holding_usd=_d(row.get("usd_value") or row.get("holding_usd")),
                cost_usd=_d(row.get("cost") or row.get("history_bought_cost")),
                realized_pnl_usd=_d(row.get("realized_profit")),
                unrealized_pnl_usd=_d(row.get("unrealized_profit")),
                bought_usd=_d(row.get("buy_amount_cur") or row.get("bought_usd")),
                sold_usd=_d(row.get("sell_amount_cur") or row.get("sold_usd")),
                buys=_i(row.get("buy_tx_count_cur") or row.get("buys")),
                sells=_i(row.get("sell_tx_count_cur") or row.get("sells")),
                last_active_at=_i(row.get("last_active_timestamp")),
                is_smart_money=bool(smart),
                is_kol=bool(kol),
                fresh_wallet=_flag(row, "is_fresh_wallet", "fresh_wallet"),
                sniper=_flag(row, "is_sniper", "sniper", "is_snipe"),
                bundler=_flag(row, "is_bundler", "bundler"),
            )
        )
    return tuple(parsed)


def parse_wallet_directory(payload: Any, *, kind: str) -> tuple[GmgnParticipant, ...]:
    """``/v1/user/smartmoney`` or ``/v1/user/kol`` — a wallet list, not a token list."""

    rows = _rows_of(payload, ("list", "data", "items", "wallets"))
    parsed: list[GmgnParticipant] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        wallet = _text(row.get("wallet_address") or row.get("address") or row.get("wallet"))
        if not wallet:
            continue
        parsed.append(
            GmgnParticipant(
                wallet=wallet,
                label=_text(row.get("name") or row.get("twitter_username") or row.get("nickname")),
                provider_tags=(kind,),
                is_smart_money=kind == "GMGN_SMART_MONEY",
                is_kol=kind == "GMGN_KOL",
                realized_pnl_usd=_d(row.get("realized_profit")),
            )
        )
    return tuple(parsed)


# --- security ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GmgnSecurity:
    """GMGN's read on a token's risk.  One input to safety, never the verdict.

    Every field is tri-state.  ``None`` means GMGN did not say, which is not the
    same as "no" — and a provider that is down produces an all-``None`` record
    that the safety engine must treat as unknown rather than as clean
    (sections 40, 41).
    """

    mint: str
    honeypot: bool | None = None
    can_sell: bool | None = None
    renounced: bool | None = None
    mint_authority_disabled: bool | None = None
    freeze_authority_disabled: bool | None = None
    lp_burned: bool | None = None
    wash_trading: bool | None = None
    risk_score: Decimal | None = None
    findings: tuple[str, ...] = field(default_factory=tuple)
    provider_available: bool = True

    @property
    def hard_fail(self) -> bool:
        """Only an explicit provider statement is a failure, never a silence."""

        return self.honeypot is True or self.can_sell is False

    @property
    def unknown(self) -> bool:
        return not self.provider_available or all(
            value is None
            for value in (
                self.honeypot,
                self.can_sell,
                self.renounced,
                self.mint_authority_disabled,
                self.freeze_authority_disabled,
                self.lp_burned,
                self.wash_trading,
                self.risk_score,
            )
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "honeypot": self.honeypot,
            "can_sell": self.can_sell,
            "renounced": self.renounced,
            "mint_authority_disabled": self.mint_authority_disabled,
            "freeze_authority_disabled": self.freeze_authority_disabled,
            "lp_burned": self.lp_burned,
            "wash_trading": self.wash_trading,
            "risk_score": _s(self.risk_score),
            "findings": list(self.findings),
            "provider_available": self.provider_available,
            "hard_fail": self.hard_fail,
            "unknown": self.unknown,
        }


def _tri(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def parse_security(payload: Any, *, mint: str) -> GmgnSecurity:
    if not isinstance(payload, dict):
        return GmgnSecurity(mint=mint, provider_available=False)
    row = payload.get("security") if isinstance(payload.get("security"), dict) else payload
    findings = tuple(
        sorted({_text(item) for item in (row.get("risk_items") or row.get("risks") or ()) if item})
    )
    return GmgnSecurity(
        mint=mint,
        honeypot=_tri(row.get("is_honeypot") or row.get("honeypot")),
        can_sell=_tri(row.get("can_sell")),
        renounced=_tri(row.get("renounced") or row.get("is_renounced")),
        mint_authority_disabled=_tri(
            row.get("mint_authority_disabled")
            if row.get("mint_authority_disabled") is not None
            else _invert(row.get("is_mintable"))
        ),
        freeze_authority_disabled=_tri(
            row.get("freeze_authority_disabled")
            if row.get("freeze_authority_disabled") is not None
            else _invert(row.get("is_freezable"))
        ),
        lp_burned=_tri(row.get("lp_burned") or row.get("is_lp_burnt") or row.get("burn_status")),
        wash_trading=_tri(row.get("is_wash_trading") or row.get("wash_trading")),
        risk_score=_d(row.get("risk_score")),
        findings=findings,
    )


def _invert(value: Any) -> Any:
    tri = _tri(value)
    return None if tri is None else (not tri)
