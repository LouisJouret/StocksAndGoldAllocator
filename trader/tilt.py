"""The smooth momentum tilt between world stocks and gold, and the trades that follow from it.

Once a month, from month-end prices (defaults in config.yaml):
    lead = log return of stocks − log return of gold over the last `lookback` months (11)
    z    = lead / its usual size (std of monthly return differences over 36 months × √12)
    stock share = 50% + 50% × tanh(z + s), with s = atanh(2 × centre − 1)
an S-curve from 100% gold to 100% stocks, shifted so that it gives `centre` (80%) when neither leads.
Trades happen only when the stock share is more than `band` away from its target.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Signal:
    month: pd.Timestamp       # the month-end the decision is based on
    stocks_return: float      # return of the stock fund over the lookback
    gold_return: float        # return of the gold fund over the lookback
    z: float
    stock_share: float        # target share of stocks; the rest is gold


def month_end_closes(daily: pd.DataFrame, today: pd.Timestamp) -> pd.DataFrame:
    """Last close of every complete month before `today`'s month (columns: stocks, gold)."""
    closes = daily.dropna().resample("ME").last()
    return closes[closes.index < today.to_period("M").to_timestamp()]


def target_share(z: float, center: float = 0.8) -> float:
    """Stock share for a lead of size z: smooth from 0% to 100%, and `center` when z = 0."""
    return float(0.5 + 0.5 * np.tanh(z + np.arctanh(2 * center - 1)))


def signals(closes: pd.DataFrame, cfg: dict) -> list[Signal]:
    """One signal per month for which there is enough history (`lookback` + `vol_months` months)."""
    log = np.log(closes[["stocks", "gold"]])
    lead = log.diff(cfg["lookback"])
    diff = log["stocks"].diff() - log["gold"].diff()
    size = diff.rolling(cfg["vol_months"]).std() * np.sqrt(12)
    z = (lead["stocks"] - lead["gold"]) / size
    out = []
    for month in z.dropna().index:
        out.append(Signal(month, float(np.expm1(lead.loc[month, "stocks"])), float(np.expm1(lead.loc[month, "gold"])),
                          float(z[month]), target_share(z[month], cfg["center"])))
    return out


@dataclass
class Order:
    symbol: str
    side: str                 # "buy" or "sell"
    amount: float             # in account currency


def plan(cash: float, holdings: dict[str, float], targets: dict[str, float], band: float,
         cash_buffer: float, min_order: float) -> tuple[list[Order], str]:
    """Orders that bring `holdings` (market values of the strategy's funds) to `targets` (weights).

    Full rebalance only when the first fund's share is more than `band` off target. Otherwise idle
    cash above the buffer is only used to buy, the underweight fund first, so nothing is sold.
    Returns (orders, a one-line reason). Sells come before buys.
    """
    symbols = list(targets)
    budget = cash + sum(holdings.get(s, 0.0) for s in symbols)
    if budget <= 0:
        return [], "nothing to invest"
    investable = budget * (1 - cash_buffer)
    want = {s: targets[s] * investable for s in symbols}
    invested = sum(holdings.get(s, 0.0) for s in symbols)
    share = holdings.get(symbols[0], 0.0) / invested if invested > 0 else None
    if share is None or abs(share - targets[symbols[0]]) > band:
        reason = ("first investment" if share is None else
                  f"{symbols[0]} is {share:.1%} of the funds, target {targets[symbols[0]]:.1%}: more than "
                  f"{band:.0%} off, rebalance")
        sells = [Order(s, "sell", holdings.get(s, 0.0) - want[s]) for s in symbols if holdings.get(s, 0.0) - want[s] >= min_order]
        free = cash + sum(o.amount for o in sells) - budget * cash_buffer
        gaps = {s: want[s] - holdings.get(s, 0.0) for s in symbols if want[s] - holdings.get(s, 0.0) >= min_order}
        scale = min(1.0, free / sum(gaps.values())) if gaps and free > 0 else 0.0
        buys = [Order(s, "buy", g * scale) for s, g in gaps.items() if g * scale >= min_order]
        return sells + buys, reason
    idle = cash - budget * cash_buffer
    if idle < max(min_order, 0.01 * budget):
        return [], f"{symbols[0]} is {share:.1%} of the funds, target {targets[symbols[0]]:.1%}: within {band:.0%}, no trade"
    gaps = {s: max(want[s] - holdings.get(s, 0.0), 0.0) for s in symbols}
    weights = gaps if sum(gaps.values()) > 0 else {s: targets[s] for s in symbols}
    total = sum(weights.values())
    buys = [Order(s, "buy", idle * w / total) for s, w in weights.items() if idle * w / total >= min_order]
    return buys, f"within {band:.0%} of target; investing idle cash ({idle:,.2f}) without selling"
