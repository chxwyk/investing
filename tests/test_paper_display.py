from decimal import Decimal

from smart_money_bot.bot import _return_percent


def test_return_percent_handles_profit_loss_and_zero_basis() -> None:
    assert _return_percent(Decimal("130"), Decimal("100")) == Decimal("30.0")
    assert _return_percent(Decimal("88"), Decimal("100")) == Decimal("-12.00")
    assert _return_percent(Decimal("10"), Decimal("0")) == Decimal("0")
