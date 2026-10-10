"""Alpaca: account, positions and orders. Paper trading unless `live=True` (which needs separate live keys)."""

import math
import os
import time
from datetime import datetime, timezone

from .config import resolve


def load_env(path=".env") -> None:
    """Read KEY=VALUE lines from .env into the environment (existing variables win)."""
    p = resolve(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Broker:
    def __init__(self, live: bool = False):
        from alpaca.trading.client import TradingClient

        load_env()
        prefix = "ALPACA_LIVE_" if live else "ALPACA_"
        key, secret = os.environ.get(prefix + "API_KEY"), os.environ.get(prefix + "SECRET_KEY")
        if not key or not secret:
            raise SystemExit(f"Missing {prefix}API_KEY / {prefix}SECRET_KEY in .env "
                             + ("(live trading uses its own keys, separate from the paper keys)." if live else "."))
        self.live = live
        self.client = TradingClient(key, secret, paper=not live)

    def account(self) -> tuple[float, float]:
        """(cash, equity). Refuses to continue if the account cannot trade."""
        a = self.client.get_account()
        if a.trading_blocked or a.account_blocked:
            raise SystemExit("The Alpaca account is blocked from trading.")
        return float(a.cash), float(a.equity)

    def positions(self) -> dict[str, tuple[float, float]]:
        """{symbol: (quantity, market value)} for everything held."""
        return {p.symbol: (float(p.qty), float(p.market_value)) for p in self.client.get_all_positions()}

    def market_open(self) -> bool:
        return bool(self.client.get_clock().is_open)

    def fractionable(self, symbol: str) -> bool:
        asset = self.client.get_asset(symbol)
        if not asset.tradable:
            raise SystemExit(f"{symbol} is not tradable on Alpaca.")
        return bool(asset.fractionable)

    def orders_since(self, since: datetime, symbols: list[str]) -> list:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=since, symbols=symbols))
        return [o for o in orders if str(o.status).split(".")[-1].lower() not in ("canceled", "expired", "rejected")]

    def submit(self, symbol: str, side: str, amount: float, price: float, held_qty: float, fractional: bool):
        """Market order for about `amount`: buys by notional, sells by quantity (whole position if nearly all)."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if side == "buy":
            if fractional:
                req = MarketOrderRequest(symbol=symbol, notional=math.floor(amount * 100) / 100, side=OrderSide.BUY,
                                         time_in_force=TimeInForce.DAY)
            else:
                req = MarketOrderRequest(symbol=symbol, qty=math.floor(amount / price), side=OrderSide.BUY,
                                         time_in_force=TimeInForce.DAY)
        else:
            qty = amount / price
            qty = held_qty if qty >= 0.995 * held_qty else (math.floor(qty * 1e6) / 1e6 if fractional else math.floor(qty))
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        if (req.qty is not None and float(req.qty) <= 0) or (req.notional is not None and float(req.notional) < 1):
            return None
        return self.client.submit_order(req)

    def wait(self, order_id, timeout: float = 90) -> str:
        """Wait until the order is filled (or gives up); returns the final status."""
        deadline = time.time() + timeout
        while True:
            status = str(self.client.get_order_by_id(order_id).status).split(".")[-1].lower()
            if status in ("filled", "canceled", "expired", "rejected") or time.time() > deadline:
                return status
            time.sleep(2)


def month_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
