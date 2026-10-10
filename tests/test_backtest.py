import numpy as np
import pandas as pd
import pytest

from trader.backtest import simulate, tilt_shares

BOT = {"base": 0.8, "up": 0.2, "down": 0.8, "lookback": 11, "vol_months": 36}


def prices(months=80, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("1990-01-31", periods=months, freq="ME")
    walk = lambda: 100 * np.exp(np.cumsum(0.005 + 0.04 * rng.normal(size=months)))
    return pd.DataFrame({"stocks": walk(), "gold": walk()}, index=idx)


def test_all_stocks_earns_the_stock_return_without_trading():
    px = prices()
    v, traded = simulate(px, pd.Series(1.0, index=px.index), band=0.05, cost=0.001)
    assert traded == 0
    assert v.iloc[-1] == pytest.approx(px["stocks"].iloc[-1] / px["stocks"].iloc[0])


def test_a_fixed_mix_drifts_until_the_band_then_trades_back_and_pays_costs():
    idx = pd.date_range("2000-01-31", periods=4, freq="ME")
    px = pd.DataFrame({"stocks": [100, 150, 150, 150], "gold": [100, 100, 100, 100]}, index=idx, dtype=float)
    target = pd.Series(0.5, index=idx)
    free, _ = simulate(px, target, band=0.05, cost=0.0)
    costly, traded = simulate(px, target, band=0.05, cost=0.01)
    assert free.iloc[0] == pytest.approx(1.25)                       # 50/50 and stocks +50%: drifted to 60/40
    assert traded > 0 and costly.iloc[-1] == pytest.approx(1.25 * (1 - 0.01 * 0.2))   # traded 0.2 back once


def test_the_tilt_never_uses_prices_from_the_month_it_is_applied_to():
    px = prices(seed=1)
    before = tilt_shares(px, BOT)
    changed = px.copy()
    changed.iloc[60:, 0] *= 3                                         # stocks jump from month 60 on
    after = tilt_shares(changed, BOT)
    # The share applied to month t+1 (decided at t, from data up to t-1) ignores month 60 until t = 61
    pd.testing.assert_series_equal(before.iloc[:61], after.iloc[:61])
    assert not np.allclose(before.iloc[61:].dropna(), after.iloc[61:].dropna())
    assert before.dropna().between(0.0, 1.0).all()
