import numpy as np
import pandas as pd
import pytest

from trader.tilt import month_end_closes, plan, signals, target_share

CFG = {"base": 0.8, "up": 0.2, "down": 0.3, "lookback": 12, "vol_months": 36}


def monthly(stock_drift, gold_drift, months=60, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2015-01-31", periods=months, freq="ME")
    walk = lambda drift: 100 * np.exp(np.cumsum(drift + 0.03 * rng.normal(size=months)))
    return pd.DataFrame({"stocks": walk(stock_drift), "gold": walk(gold_drift)}, index=idx)


def test_target_share_moves_smoothly_between_all_gold_and_all_stocks():
    assert target_share(0.0) == pytest.approx(0.8)
    assert target_share(5.0) == pytest.approx(1.0, abs=1e-3)
    assert target_share(-5.0) == pytest.approx(0.0, abs=1e-3)
    zs = np.linspace(-4, 4, 81)
    shares = [target_share(z) for z in zs]
    assert all(0.0 <= s <= 1.0 for s in shares) and np.all(np.diff(shares) > 0)


def test_signal_follows_the_leader_and_matches_the_backtest_formula():
    up = signals(monthly(0.02, 0.0), CFG)
    down = signals(monthly(0.0, 0.02), CFG)
    assert up[-1].stock_share > 0.9 and down[-1].stock_share < 0.6
    assert len(up) == 60 - 12 - 24        # needs 12 months of lead and 36 months of differences
    closes = monthly(0.01, 0.005, seed=3)
    lr = np.log(closes).diff()
    z = (np.log(closes / closes.shift(12))["stocks"] - np.log(closes / closes.shift(12))["gold"]) / (
        (lr["stocks"] - lr["gold"]).rolling(36).std() * np.sqrt(12))
    expected = 0.8 + np.where(z > 0, 0.2, 0.3) * np.tanh(z)
    got = pd.Series({s.month: s.stock_share for s in signals(closes, CFG)})
    np.testing.assert_allclose(got.to_numpy(), pd.Series(expected, index=closes.index).dropna().to_numpy())


def test_only_complete_months_are_used():
    days = pd.bdate_range("2024-01-01", "2024-03-14")
    daily = pd.DataFrame({"stocks": np.arange(len(days)) + 100.0, "gold": 50.0}, index=days)
    closes = month_end_closes(daily, pd.Timestamp("2024-03-14"))
    assert list(closes.index.month) == [1, 2]
    assert closes.loc["2024-02", "stocks"].iloc[0] == daily.loc["2024-02-29", "stocks"]


def test_first_investment_buys_the_target_and_keeps_a_buffer():
    orders, _ = plan(10_000, {}, {"ACWI": 0.8, "GLDM": 0.2}, 0.05, 0.005, 5)
    buys = {o.symbol: o.amount for o in orders}
    assert all(o.side == "buy" for o in orders)
    assert buys["ACWI"] == pytest.approx(7_960) and buys["GLDM"] == pytest.approx(1_990)


def test_no_trade_inside_the_band():
    orders, reason = plan(20, {"ACWI": 8_000, "GLDM": 2_000}, {"ACWI": 0.82, "GLDM": 0.18}, 0.05, 0.005, 5)
    assert orders == [] and "no trade" in reason


def test_rebalance_sells_first_and_never_spends_more_than_it_has():
    cash, holdings, targets = 50.0, {"ACWI": 9_000, "GLDM": 1_000}, {"ACWI": 0.6, "GLDM": 0.4}
    orders, _ = plan(cash, holdings, targets, 0.05, 0.005, 5)
    assert [o.side for o in orders] == ["sell", "buy"]
    sold, bought = orders[0].amount, orders[1].amount
    assert orders[0].symbol == "ACWI" and orders[1].symbol == "GLDM"
    assert bought <= cash + sold - 0.005 * (cash + 10_000) + 1e-9
    after = {"ACWI": 9_000 - sold, "GLDM": 1_000 + bought}
    assert after["ACWI"] / sum(after.values()) == pytest.approx(0.6, abs=0.001)


def test_idle_cash_is_invested_without_selling_underweight_first():
    orders, _ = plan(1_000, {"ACWI": 8_200, "GLDM": 1_800}, {"ACWI": 0.8, "GLDM": 0.2}, 0.05, 0.005, 5)
    assert all(o.side == "buy" for o in orders)
    amounts = {o.symbol: o.amount for o in orders}
    assert amounts["GLDM"] / sum(amounts.values()) > 0.2      # gold is below target: more than its 20% of new money
    assert sum(amounts.values()) == pytest.approx(1_000 - 0.005 * 11_000)
    after = {s: v + amounts.get(s, 0) for s, v in {"ACWI": 8_200, "GLDM": 1_800}.items()}
    assert after["ACWI"] / sum(after.values()) == pytest.approx(0.8, abs=0.001)
