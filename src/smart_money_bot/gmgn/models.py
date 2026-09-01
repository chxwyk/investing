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

    #: The exact mint's logo, straight from the row that found it.  Dropping
    #: this and re-fetching it later is what produced blank card thumbnails.
    image_url: str = ""
    creator: str = ""
    launchpad_platform: str = ""

    market_cap_usd: Decimal | None = None
    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    swaps: int | None = None
    buys: int | None = None
    sells: int | None = None
    holder_count: int | None = None
    price_change_percent: Decimal | None = None
    price_change_1m_percent: Decimal | None = None
    price_change_5m_percent: Decimal | None = None
    price_change_1h_percent: Decimal | None = None
    created_at: int | None = None
    open_at: int | None = None
    hot_level: int | None = None
    #: Cumulative fees paid on this token.  Activity, never rug safety (§37).
    total_fee: Decimal | None = None
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
            "image_url": self.image_url,
            "creator": self.creator,
            "launchpad_platform": self.launchpad_platform,
            "rank": self.rank,
            "interval": self.interval,
            "source": self.source,
            "market_cap_usd": _s(self.market_cap_usd),
            "price_usd": _s(self.price_usd),
            "liquidity_usd": _s(self.liquidity_usd),
            "volume_usd": _s(self.volume_usd),
            "swaps": self.swaps,
            "buys": self.buys,
            "sells": self.sells,
            "holder_count": self.holder_count,
            "price_change_percent": _s(self.price_change_percent),
            "price_change_1m_percent": _s(self.price_change_1m_percent),
            "price_change_5m_percent": _s(self.price_change_5m_percent),
            "price_change_1h_percent": _s(self.price_change_1h_percent),
            "created_at": self.created_at,
            "open_at": self.open_at,
            "hot_level": self.hot_level,
            "total_fee": _s(self.total_fee),
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
                # ``logo`` is the documented RankItem field for the token image.
                image_url=_text(row.get("logo")),
                creator=_text(row.get("creator")),
                launchpad_platform=_text(row.get("launchpad_platform")),
                rank=_i(row.get("rank")) or position,
                interval=interval,
                source=source,
                market_cap_usd=_d(row.get("market_cap")),
                price_usd=_d(row.get("price")),
                liquidity_usd=_d(row.get("liquidity")),
                volume_usd=_d(row.get("volume")),
                swaps=_i(row.get("swaps")),
                buys=_i(row.get("buys")),
                sells=_i(row.get("sells")),
                holder_count=_i(row.get("holder_count")),
                price_change_percent=_d(row.get("price_change_percent")),
                price_change_1m_percent=_d(row.get("price_change_percent1m")),
                price_change_5m_percent=_d(row.get("price_change_percent5m")),
                price_change_1h_percent=_d(row.get("price_change_percent1h")),
                created_at=_i(row.get("creation_timestamp")),
                open_at=_i(row.get("open_timestamp")),
                hot_level=_i(row.get("hot_level")),
                total_fee=_d(row.get("total_fee")),
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


#: The trenches response does not echo the request's section names.  The
#: official docs are explicit: *"`data.pump` in the response corresponds to
#: `--type near_completion` in the request. The API always returns this category
#: under the key `pump`, not `near_completion`."*  Reading the response with the
#: request's vocabulary silently loses the entire near-completion section, which
#: is the one the operator's workflow calls FINAL STRETCH.
RESPONSE_SECTION_ALIASES: dict[str, str] = {"pump": "near_completion"}


def parse_trenches_response(payload: Any) -> dict[str, tuple[GmgnToken, ...]]:
    """``/v1/trenches`` — ``data.new_creation`` / ``data.pump`` / ``data.completed``.

    Sections are normalised to the request vocabulary so callers can map one set
    of names.  Unknown section names are kept rather than dropped: a new GMGN
    category is something to go and read about, and silently discarding it would
    hide a feed we are already paying for.
    """

    if not isinstance(payload, dict):
        return {}
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    sections: dict[str, tuple[GmgnToken, ...]] = {}
    for raw_name, value in body.items():
        rows = _rows_of(value, ("tokens", "list", "data"))
        if not rows:
            continue
        name = RESPONSE_SECTION_ALIASES.get(str(raw_name), str(raw_name))
        sections[name] = parse_tokens(rows, source=f"gmgn_trench_{name}")
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
    #: Documented as ``sell_amount_percentage`` — the share of what the wallet
    #: bought that it has since sold.  This is the accumulating/distributing
    #: verdict, and it comes from the provider rather than being inferred.
    sold_fraction: Decimal | None = None
    buys: int | None = None
    sells: int | None = None
    first_buy_at: int | None = None
    last_active_at: int | None = None
    #: 0 normal wallet, 1 burn/dead, 2 DEX pool.  A pool is not a holder.
    address_type: int | None = None
    #: ``native_transfer.from_address`` — who funded this wallet.  Real cluster
    #: evidence, and free: it arrives with the holder row.
    funded_by: str = ""
    is_smart_money: bool = False
    is_kol: bool = False
    fresh_wallet: bool = False
    sniper: bool = False
    bundler: bool = False
    rat_trader: bool = False
    wash_trader: bool = False
    dev_team: bool = False

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
            "sold_fraction": _s(self.sold_fraction),
            "buys": self.buys,
            "sells": self.sells,
            "first_buy_at": self.first_buy_at,
            "last_active_at": self.last_active_at,
            "address_type": self.address_type,
            "funded_by": self.funded_by,
            "is_smart_money": self.is_smart_money,
            "is_kol": self.is_kol,
            "fresh_wallet": self.fresh_wallet,
            "sniper": self.sniper,
            "bundler": self.bundler,
            "rat_trader": self.rat_trader,
            "wash_trader": self.wash_trader,
            "dev_team": self.dev_team,
            "is_pool": self.address_type == ADDR_TYPE_POOL,
        }


# ``True == 1`` in Python, so the integer form is already covered.
# --- documented wallet tags (gmgn-holder-analysis SKILL.md) -------------------
#: ``tags`` on a holder row.  These are the platform's wallet labels.
TAG_SMART_DEGEN = "smart_degen"
TAG_PUMP_SMART = "pump_smart"
TAG_RENOWNED = "renowned"
TAG_FRESH_WALLET = "fresh_wallet"
TAG_WASH_TRADER = "wash_trader"
TAG_KOL = "kol"
#: ``maker_token_tags`` — what the wallet did to *this* token.
TAG_BUNDLER = "bundler"
TAG_RAT_TRADER = "rat_trader"
TAG_SNIPER = "sniper"
TAG_WHALE = "whale"
TAG_DEV_TEAM = "dev_team"
TAG_CREATOR = "creator"

#: Tags meaning "GMGN considers this wallet skilled".  ``renowned`` is
#: deliberately absent: that is the KOL label, and fame is not expectancy.
SMART_MONEY_TAGS: frozenset[str] = frozenset({TAG_SMART_DEGEN, TAG_PUMP_SMART})
KOL_TAGS: frozenset[str] = frozenset({TAG_KOL, TAG_RENOWNED})

#: ``addr_type``: 0 = normal wallet, 1 = burn/dead, 2 = DEX/pool.  A liquidity
#: pool is not a holder, and counting one as a whale makes every token look
#: dangerously concentrated.
ADDR_TYPE_NORMAL = 0
ADDR_TYPE_BURN = 1
ADDR_TYPE_POOL = 2


def _tags_of(row: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = row.get(key)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({_text(item).strip().lower() for item in raw if _text(item)}))


def _fraction_to_percent(value: Any) -> Decimal | None:
    """``amount_percentage`` is documented as a 0–1 fraction, not a percent.

    Reading it as a percent turns an 8% holder into "0.08%", which is the
    difference between a concentrated token and a healthy one.
    """

    parsed = _d(value)
    return None if parsed is None else (parsed * Decimal("100")).quantize(Decimal("0.01"))


def parse_participants(payload: Any, *, mint: str) -> tuple[GmgnParticipant, ...]:
    """Top holders or top traders for one exact mint.

    Field names follow the documented holder object: ``amount_percentage`` (a
    0–1 fraction), ``usd_value``, ``realized_profit``, ``unrealized_profit``,
    ``buy_tx_count_cur`` / ``sell_tx_count_cur``, ``sell_volume_cur``, and the
    two tag arrays.  v2.45 guessed at boolean flags such as ``is_smart_money``
    and ``is_sniper``; those fields do not exist, which is why nothing was ever
    tagged at token level.

    ``mint`` is passed in rather than read from the rows: the request was for a
    specific address, so a row naming a different token is a provider bug rather
    than a discovery.
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
            continue
        wallet_tags = _tags_of(row, "tags")
        token_tags = _tags_of(row, "maker_token_tags")
        every_tag = tuple(dict.fromkeys((*wallet_tags, *token_tags)))
        funder = ""
        transfer = row.get("native_transfer")
        if isinstance(transfer, dict):
            funder = _text(transfer.get("from_address"))
        parsed.append(
            GmgnParticipant(
                wallet=wallet,
                mint=mint,
                label=_text(row.get("name") or row.get("twitter_name")),
                provider_tags=every_tag,
                holding_percent=_fraction_to_percent(row.get("amount_percentage")),
                holding_usd=_d(row.get("usd_value")),
                cost_usd=_d(row.get("avg_cost")),
                realized_pnl_usd=_d(row.get("realized_profit")),
                unrealized_pnl_usd=_d(row.get("unrealized_profit")),
                # ``sell_volume_cur`` is USD; ``sell_amount_cur`` is a token
                # amount.  v2.45 conflated them, which would have reported a
                # token count as dollars.
                sold_usd=_d(row.get("sell_volume_cur")),
                sold_fraction=_d(row.get("sell_amount_percentage")),
                buys=_i(row.get("buy_tx_count_cur")),
                sells=_i(row.get("sell_tx_count_cur")),
                first_buy_at=_i(row.get("start_holding_at")),
                address_type=_i(row.get("addr_type")),
                funded_by=funder,
                is_smart_money=bool(set(every_tag) & SMART_MONEY_TAGS),
                is_kol=bool(set(every_tag) & KOL_TAGS),
                fresh_wallet=TAG_FRESH_WALLET in every_tag,
                sniper=TAG_SNIPER in every_tag,
                bundler=TAG_BUNDLER in every_tag,
                rat_trader=TAG_RAT_TRADER in every_tag,
                wash_trader=TAG_WASH_TRADER in every_tag,
                dev_team=bool({TAG_DEV_TEAM, TAG_CREATOR} & set(every_tag)),
            )
        )
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class GmgnWalletTrade:
    """One trade from ``/v1/user/smartmoney`` or ``/v1/user/kol``.

    These endpoints are **trade feeds**, not wallet directories — the official
    skill is explicit that they "return trades from platform-tagged public
    wallet lists".  v2.45 parsed them as a directory and looked for a top-level
    ``wallet_address``, found none, and reported zero wallets forever.  That is
    the whole reason production showed ``Smart-money wallets: 0``.

    The correction is also an upgrade: a trade feed is exactly the *event* the
    fast smart-money card wants, and it carries the token's symbol and logo.
    """

    wallet: str
    mint: str
    side: str = ""
    tag: str = ""
    label: str = ""
    twitter: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    amount_usd: Decimal | None = None
    price_usd: Decimal | None = None
    price_now_usd: Decimal | None = None
    at: int | None = None
    symbol: str = ""
    image_url: str = ""
    full_position: bool = False

    @property
    def is_buy(self) -> bool:
        return self.side.lower() == "buy"

    @property
    def is_smart_money(self) -> bool:
        return bool(set(self.tags) & SMART_MONEY_TAGS)

    @property
    def is_kol(self) -> bool:
        return bool(set(self.tags) & KOL_TAGS)

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "mint": self.mint,
            "side": self.side,
            "tag": self.tag,
            "label": self.label,
            "twitter": self.twitter,
            "tags": list(self.tags),
            "amount_usd": _s(self.amount_usd),
            "price_usd": _s(self.price_usd),
            "price_now_usd": _s(self.price_now_usd),
            "at": self.at,
            "symbol": self.symbol,
            "image_url": self.image_url,
            "full_position": self.full_position,
            "is_buy": self.is_buy,
            "is_smart_money": self.is_smart_money,
            "is_kol": self.is_kol,
        }


def parse_wallet_trades(payload: Any, *, tag: str) -> tuple[GmgnWalletTrade, ...]:
    """Parse the smart-money / KOL trade feed against its documented shape.

    Rows without a usable exact mint are dropped, exactly as everywhere else:
    a trade we cannot attribute to an address is not evidence about a token.
    """

    rows = _rows_of(payload, ("list", "data", "items"))
    parsed: list[GmgnWalletTrade] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mint = _text(row.get("base_address"))
        if not mint or not is_valid_mint(mint):
            continue
        maker_info = row.get("maker_info") if isinstance(row.get("maker_info"), dict) else {}
        base_token = row.get("base_token") if isinstance(row.get("base_token"), dict) else {}
        wallet = _text(row.get("maker") or maker_info.get("address"))
        if not wallet:
            continue
        tags = _tags_of(maker_info, "tags") or (tag.lower(),)
        parsed.append(
            GmgnWalletTrade(
                wallet=wallet,
                mint=mint,
                side=_text(row.get("side")),
                tag=tag,
                label=_text(maker_info.get("name") or maker_info.get("twitter_name")),
                twitter=_text(maker_info.get("twitter_username")),
                tags=tags,
                amount_usd=_d(row.get("amount_usd") or row.get("cost_usd")),
                price_usd=_d(row.get("price_usd")),
                price_now_usd=_d(row.get("price_now")),
                at=_i(row.get("timestamp")),
                symbol=_text(base_token.get("symbol")),
                image_url=_text(base_token.get("logo")),
                full_position=row.get("is_open_or_close") in (1, "1"),
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
