from types import SimpleNamespace

from quant_platform_kit.common.models import OrderIntent

from application.ibkr_order_execution import probe_order_write_access, submit_order_intent


class FakeMarketOrder:
    def __init__(self, side, quantity):
        self.action = side
        self.totalQuantity = quantity
        self.tif = ""


class FakeLimitOrder:
    def __init__(self, side, quantity, limit_price):
        self.action = side
        self.totalQuantity = quantity
        self.lmtPrice = limit_price
        self.tif = ""


class FakeIB:
    def __init__(self):
        self.placed_contract = None
        self.placed_order = None

    def qualifyContracts(self, _contract):
        return None

    def placeOrder(self, contract, order):
        self.placed_contract = contract
        self.placed_order = order
        return SimpleNamespace(
            order=SimpleNamespace(orderId=42),
            orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=0),
        )


class FakeWhatIfIB:
    def __init__(self):
        self.probed_contract = None
        self.probed_order = None

    def whatIfOrder(self, contract, order):
        self.probed_contract = contract
        self.probed_order = order
        return SimpleNamespace(warningText="")


def fake_stock(symbol, exchange, currency):
    return SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency)


def fake_option(symbol, expiration, strike, right, exchange, currency):
    return SimpleNamespace(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiration,
        strike=strike,
        right=right,
        exchange=exchange,
        currency=currency,
    )


def test_probe_order_write_access_uses_explicit_what_if_order():
    ib = FakeWhatIfIB()

    probe_order_write_access(
        ib,
        symbol="AAPL",
        account_id="U1234567",
        stock_factory=fake_stock,
        market_order_factory=FakeMarketOrder,
    )

    assert ib.probed_contract.symbol == "AAPL"
    assert ib.probed_order.account == "U1234567"
    assert ib.probed_order.whatIf is True
    assert ib.probed_order.transmit is True


def test_submit_order_intent_sets_default_day_tif_on_market_orders():
    ib = FakeIB()

    report = submit_order_intent(
        ib,
        OrderIntent(symbol="AAPL", side="sell", quantity=3),
        wait_seconds=0,
        stock_factory=fake_stock,
        market_order_factory=FakeMarketOrder,
    )

    assert ib.placed_order.tif == "DAY"
    assert report.status == "Submitted"
    assert report.raw_payload["time_in_force"] == "DAY"


def test_submit_order_intent_preserves_explicit_tif_on_market_orders():
    ib = FakeIB()

    submit_order_intent(
        ib,
        OrderIntent(symbol="AAPL", side="sell", quantity=3, time_in_force="GTC"),
        wait_seconds=0,
        stock_factory=fake_stock,
        market_order_factory=FakeMarketOrder,
    )

    assert ib.placed_order.tif == "GTC"


def test_submit_order_intent_preserves_account_id():
    ib = FakeIB()

    report = submit_order_intent(
        ib,
        OrderIntent(symbol="AAPL", side="buy", quantity=3, account_id="U1234567"),
        wait_seconds=0,
        stock_factory=fake_stock,
        market_order_factory=FakeMarketOrder,
    )

    assert ib.placed_order.account == "U1234567"
    assert report.raw_payload["account_id"] == "U1234567"


def test_submit_order_intent_can_target_hk_stock_exchange_and_currency():
    ib = FakeIB()

    submit_order_intent(
        ib,
        OrderIntent(symbol="00700", side="buy", quantity=100),
        wait_seconds=0,
        stock_factory=fake_stock,
        market_order_factory=FakeMarketOrder,
        stock_exchange="SEHK",
        stock_currency="HKD",
    )

    assert ib.placed_contract.symbol == "00700"
    assert ib.placed_contract.exchange == "SEHK"
    assert ib.placed_contract.currency == "HKD"


def test_submit_order_intent_passes_option_factory_and_default_tif():
    ib = FakeIB()

    report = submit_order_intent(
        ib,
        OrderIntent(
            symbol="QQQ",
            side="buy_to_open",
            quantity=1,
            order_type="limit",
            limit_price=150.0,
            metadata={
                "asset_class": "option",
                "intent_type": "single_leg_option",
                "underlier": "QQQ",
                "right": "C",
                "expiration": "2028-01-21",
                "strike": 520.0,
            },
        ),
        wait_seconds=0,
        option_factory=fake_option,
        limit_order_factory=FakeLimitOrder,
    )

    assert ib.placed_order.tif == "DAY"
    assert report.raw_payload["asset_class"] == "option"
