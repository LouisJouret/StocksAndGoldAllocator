"""Backtest of the bot's rule and of fixed stock/gold mixes, monthly from 1972 to last month.

Data (all free): world stocks = MSCI World index until 1987, then MSCI ACWI (Yahoo), plus the US
dividend yield (Kenneth French's library minus the S&P 500 price index); gold = average monthly
price (public series) minus a fund fee; cash = US T-bills. Monthly averages only exist for gold
before 2000, so everything uses monthly averages, and the tilt's signal is taken one month older
than the bot would: averaging links neighbouring months, which would otherwise flatter momentum.
"""

import numpy as np
import pandas as pd

from .data import fetch_yahoo, gold_monthly, us_market_monthly
from .tilt import signals


def _monthly(daily: pd.Series, how: str) -> pd.Series:
    s = daily.copy()
    s.index = s.index.tz_localize(None)
    return getattr(s.resample("ME"), how)()


def history(cfg: dict) -> tuple[pd.DataFrame, pd.Series]:
    """(monthly prices with columns stocks and gold, monthly cash return), complete months only."""
    bt = cfg["backtest"]
    us = us_market_monthly()
    spx = _monthly(fetch_yahoo("^GSPC", "1960-01-01")["close"], "last")
    dividends = (us["total"] - spx.pct_change()).dropna().rolling(12).mean()
    world = _monthly(fetch_yahoo("^990100-USD-STRD", "1971-01-01")["close"], "mean")
    acwi = _monthly(fetch_yahoo("^892400-USD-STRD", "1985-01-01")["close"], "mean")
    price = pd.concat([world.loc[:"1987-12"] * acwi.loc["1988-01"].iloc[0] / world.loc["1988-01"].iloc[0],
                       acwi.loc["1988-01":]])
    this_month = pd.Timestamp.now().to_period("M").to_timestamp()
    gold = gold_monthly()
    months = price.index[(price.index >= pd.Timestamp(bt["start"])) & (price.index < this_month)].intersection(gold.index)
    d = dividends.reindex(months).ffill().bfill()
    stocks = price.loc[months] * np.cumprod(1 + d)
    gold = gold.loc[months] * np.exp(-bt["gold_fee"] / 12 * np.arange(len(months)))
    px = pd.DataFrame({"stocks": stocks, "gold": gold}).dropna()
    return px, us["cash"].reindex(px.index).ffill()


def tilt_shares(px: pd.DataFrame, bot: dict) -> pd.Series:
    """The bot's stock share for each month end, from data one month older (see the module docstring)."""
    s = pd.Series({x.month: x.stock_share for x in signals(px, bot)}, dtype=float)
    return s.reindex(px.index).shift(1)


def simulate(px: pd.DataFrame, target: pd.Series, band: float, cost: float, start=None) -> tuple[pd.Series, float]:
    """Monthly portfolio value. The share decided at a month end applies to the next month; holdings drift
    with prices and are traded back to target when stocks are more than `band` off. Returns (values, traded/yr)."""
    r = px.pct_change()
    tgt = target.reindex(px.index).shift(1)
    ok = r.notna().all(axis=1) & tgt.notna()
    if start is not None:
        ok &= r.index >= pd.Timestamp(start)
    rs, gs, goals = r.loc[ok, "stocks"].to_numpy(), r.loc[ok, "gold"].to_numpy(), tgt[ok].to_numpy()
    w, v, values, traded = goals[0], 1.0, np.empty(len(goals)), 0.0
    for i, (a, g, goal) in enumerate(zip(rs, gs, goals)):
        if abs(w - goal) > band:
            traded += 2 * abs(w - goal)
            v *= 1 - cost * 2 * abs(w - goal)
            w = goal
        sa, sg = w * (1 + a), (1 - w) * (1 + g)
        v *= sa + sg
        w = sa / (sa + sg)
        values[i] = v
    return pd.Series(values, index=r.index[ok]), traded / (len(values) / 12)


def stats(v: pd.Series, cash: pd.Series) -> dict:
    lr = np.log(v).diff().fillna(np.log(v.iloc[0]))
    excess = lr - np.log1p(cash.reindex(v.index))
    return {"per year": v.iloc[-1] ** (12 / len(v)) - 1, "volatility": lr.std() * np.sqrt(12),
            "sharpe": excess.mean() / excess.std() * np.sqrt(12), "worst drop": (v / v.cummax().clip(lower=1.0) - 1).min()}


def strategies(px: pd.DataFrame, cfg: dict) -> dict[str, pd.Series]:
    tilt = tilt_shares(px, cfg["bot"])
    const = lambda s: pd.Series(s, index=px.index).where(tilt.notna())
    return {"100% stocks": const(1.0), "fixed 80/20": const(0.8), "the bot's tilt": tilt}


def per_start(px, targets, band, cost, starts) -> dict[str, pd.DataFrame]:
    """For each start month: return per year until the end, over the next 10 years, and worst drop."""
    out = {k: {} for k in ("until today", "next 10 years", "worst drop")}
    for name, t in targets.items():
        rows = {k: [] for k in out}
        for s in starts:
            v, _ = simulate(px, t, band, cost, start=s)
            rows["until today"].append(v.iloc[-1] ** (12 / len(v)) - 1)
            rows["next 10 years"].append(v.iloc[119] ** 0.1 - 1 if len(v) >= 120 else np.nan)
            rows["worst drop"].append((v / v.cummax().clip(lower=1.0) - 1).min())
        for k in out:
            out[k][name] = rows[k]
    return {k: pd.DataFrame(v, index=starts) for k, v in out.items()}


# ----------------------------------------------------------------------------------------------- charts
STYLE = {"100% stocks": ("#9e9e9e", 1.6), "fixed 80/20": ("k", 1.6), "the bot's tilt": ("C4", 2.2)}
ERAS = [("1972", "1980", "gold boom,\nstagflation"), ("1980", "2001", "stocks boom, gold falls"),
        ("2001", "2011", "gold boom,\nstocks' lost decade"), ("2011", "2015", "gold\nbust"), ("2015", "2027", "both rise")]


def from_zero(v: pd.Series) -> tuple[pd.DatetimeIndex, pd.Series]:
    """A value curve with its starting point (1.0 = 0%) added one month before its first value."""
    s = pd.concat([pd.Series([1.0], index=[v.index[0] - pd.offsets.MonthEnd(1)]), v])
    return s.index, s


def _log_axis(ax):
    import matplotlib.ticker as mticker

    ax.set_yscale("log")
    ax.yaxis.set_major_locator(mticker.LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{(v - 1) * 100:+,.0f}%"))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.grid(alpha=0.3)


def chart_mixes(px, cash, cfg, path, tilt: pd.Series | None = None) -> pd.DataFrame:
    """Growth of fixed mixes from 0% to 100% gold (the table also has every 10-year period). With `tilt`
    (the bot's stock shares), the bot is added, and every mix starts with it (it needs a few years of
    history for its signal), so all numbers cover the same months."""
    import matplotlib.pyplot as plt

    bt = cfg["backtest"]
    shares = np.round(np.arange(1.0, -0.01, -0.1), 1)
    live = tilt.notna() if tilt is not None else pd.Series(True, index=px.index)
    runs = {f"{s:.0%}/{1 - s:.0%}": simulate(px, pd.Series(s, index=px.index).where(live), bt["band"], bt["cost"])[0]
            for s in shares}
    gold_share = {f"{s:.0%}/{1 - s:.0%}": 1 - s for s in shares}
    BOT, AVG = "the bot's tilt", "the bot's average mix, fixed"
    if tilt is not None:
        runs[BOT] = simulate(px, tilt, bt["band"], bt["cost"])[0]
        gold_share[BOT] = gold_share[AVG] = 1 - tilt.dropna().mean()
        # the same average split held all the time: the gap to the bot is what its timing adds
        runs[AVG] = simulate(px, pd.Series(1 - gold_share[AVG], index=px.index).where(live), bt["band"], bt["cost"])[0]
    tens = {n: np.exp(np.log(v).diff(120).dropna() / 10) - 1 for n, v in runs.items()}   # by end month
    stocks10 = tens["100%/0%"]
    table = pd.DataFrame({n: {**stats(v, cash), "average gold": gold_share[n], "worst 10 years": tens[n].min(),
                              "median 10 years": tens[n].median(),
                              "10 years beating 100% stocks": (tens[n] > stocks10.reindex(tens[n].index)).mean()}
                          for n, v in runs.items()}).T
    fixed = [n for n in runs if n not in (BOT, AVG)]
    fig, ax = plt.subplots(figsize=(15, 7))
    colors = dict(zip(fixed, plt.cm.coolwarm_r(np.linspace(0, 1, len(fixed)))))
    for n in fixed:
        if n in ("100%/0%", "80%/20%", "60%/40%", "40%/60%", "20%/80%", "0%/100%"):
            st = table.loc[n]
            s, g = n.split("/")
            ax.plot(*from_zero(runs[n]), color=colors[n], lw=2.4 if n in ("100%/0%", "0%/100%") else 1.4,
                    label=f"{s} stocks / {g} gold: {st['per year']:+.1%}/yr, worst drop {st['worst drop']:.0%}")
    if tilt is not None:
        v = runs[BOT]
        st = table.loc[BOT]
        ax.plot(*from_zero(v), color=STYLE[BOT][0], lw=2.6,
                label=f"{BOT} (about {gold_share[BOT]:.0%} gold on average): {st['per year']:+.1%}/yr, "
                      f"worst drop {st['worst drop']:.0%}")
        st = table.loc[AVG]
        ax.plot(*from_zero(runs[AVG]), color=STYLE[BOT][0], lw=1.8, ls="--",
                label=f"{AVG} ({1 - gold_share[AVG]:.0%}/{gold_share[AVG]:.0%}, no timing): "
                      f"{st['per year']:+.1%}/yr, worst drop {st['worst drop']:.0%}")
    _log_axis(ax)
    ax.legend(loc="lower right", fontsize=9)
    top = ax.get_ylim()[1] * 0.6
    first, last = from_zero(runs["100%/0%"])[0][0], px.index[-1]
    for a, b, label in ERAS:
        a, b = max(pd.Timestamp(a), first), min(pd.Timestamp(b), last)
        if a < b:
            ax.text(a + (b - a) / 2, top, label, ha="center", va="top", fontsize=8.5, color="#555")
            ax.axvline(a, color="#999", lw=0.6, ls=":")
    ax.set_title(f"World stocks / gold mixes, {first:%b %Y} – {last:%b %Y}, rebalanced when >"
                 f"{bt['band']:.0%} off (USD, monthly averages, {bt['cost']:.1%} per trade)", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return table


def chart_tilt(px, cash, cfg, targets, path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The bot's tilt against 100% stocks and fixed 80/20: share chosen, growth, decades."""
    import matplotlib.pyplot as plt

    bt = cfg["backtest"]
    runs = {n: simulate(px, t, bt["band"], bt["cost"]) for n, t in targets.items()}
    table = pd.DataFrame({n: {**stats(v, cash), "traded/yr": to} for n, (v, to) in runs.items()}).T
    start = runs["fixed 80/20"][0].index[0]
    fig = plt.figure(figsize=(15, 11))
    ax = fig.add_subplot(2, 1, 1)
    for name in [n for n in targets if n not in ("100% stocks", "fixed 80/20")]:
        t = targets[name].dropna()
        ax.step(t.index, t * 100, where="post", color=STYLE[name][0], lw=1.8 if name == "the bot's tilt" else 1.0,
                alpha=1.0 if name == "the bot's tilt" else 0.8, label=name)
    ax.axhline(80, color="k", lw=0.8, label="fixed 80/20")
    ax.axhline(100, color="#9e9e9e", lw=1.5, ls=":", label="100% stocks")
    ax.set_ylim(-3, 103)
    ax.set_ylabel("stock share for the next month (%)")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower left", fontsize=9, ncol=3)
    ax.set_title(f"A. Stock share chosen each month (the rest is gold): stocks' {cfg['bot']['lookback']}-month lead "
                 "over gold, scaled by its usual size", fontsize=10.5)
    ax2 = fig.add_subplot(2, 2, 3)
    for n, (v, _) in runs.items():
        st = table.loc[n]
        ax2.plot(v.index, v, color=STYLE[n][0], lw=STYLE[n][1],
                 label=f"{n}: {st['per year']:+.1%}/yr, worst {st['worst drop']:.0%}, sharpe {st['sharpe']:.2f}")
    _log_axis(ax2)
    ax2.legend(fontsize=8.5, loc="upper left")
    ax2.set_title(f"B. Growth, {start:%b %Y} – {px.index[-1]:%b %Y}", fontsize=10.5)
    edges = [start.year] + [y for y in range(1980, px.index[-1].year + 1, 10) if y > start.year]
    spans = [(a, (edges[i + 1] - 1) if i + 1 < len(edges) else px.index[-1].year) for i, a in enumerate(edges)]
    decades = pd.DataFrame({n: {f"{a}–{str(b)[2:]}": (v.loc[:f"{b}"].iloc[-1] / (v.loc[:f"{a - 1}"].iloc[-1] if (v.index.year < a).any() else 1.0))
                                ** (12 / len(v.loc[f"{a}":f"{b}"])) - 1 for a, b in spans}
                            for n, (v, _) in runs.items()})
    ax3 = fig.add_subplot(2, 2, 4)
    xs = np.arange(len(decades))
    width = 0.8 / len(decades.columns)
    for k, n in enumerate(decades.columns):
        ax3.bar(xs + (k - (len(decades.columns) - 1) / 2) * width, decades[n] * 100, width * 0.95, color=STYLE[n][0], label=n)
    ax3.set_xticks(xs, decades.index)
    ax3.axhline(0, color="k", lw=0.6)
    ax3.grid(alpha=0.3, axis="y")
    ax3.set_ylabel("% per year")
    ax3.legend(fontsize=8.5)
    ax3.set_title("C. Return per year by decade", fontsize=10.5)
    fig.suptitle(f"The bot's rule, backtested monthly (signals one month old, {bt['cost']:.1%} per trade)", fontsize=11.5)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return table, decades


def chart_start_dates(px, cfg, targets, path) -> dict[str, pd.DataFrame]:
    """Every start month: return until today, over the next 10 years, worst drop lived through."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    bt = cfg["backtest"]
    first = simulate(px, targets["fixed 80/20"], bt["band"], bt["cost"])[0].index
    starts = first[first <= px.index[-1] - pd.DateOffset(years=1)]
    res = per_start(px, targets, bt["band"], bt["cost"], starts)
    fig, axs = plt.subplots(3, 1, figsize=(15, 13), sharex=True)
    enough = res["until today"].index <= px.index[-1] - pd.DateOffset(years=3)
    panels = ((res["until today"][enough], "A. Return per year from the start date until today (at least 3 years)", "% per year"),
              (res["next 10 years"], "B. Return per year over the 10 years after the start date", "% per year"),
              (res["worst drop"], "C. Worst drop lived through, from the start date until today "
                                  "(monthly averages: real drops were deeper)", "worst drop (%)"))
    for ax, (df, title, ylabel) in zip(axs, panels):
        for n, (col, lw) in [(n, STYLE[n]) for n in targets]:
            ax.plot(df.index, df[n] * 100, color=col, lw=lw, label=n)
        ax.set_title(title, fontsize=11)
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.3)
        ax.axhline(0, color="k", lw=0.6)
        ax.xaxis.set_major_locator(mdates.YearLocator(4))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axs[0].legend(fontsize=9.5, loc="upper right", ncol=3)
    ten = res["next 10 years"].dropna()
    if len(ten):
        axs[1].axvspan(ten.index[-1], starts[-1], color="#eee", zorder=0)
    axs[1].set_xlim(starts[0], starts[-1])
    axs[2].set_xlabel("start date (everything invested at the end of that month)")
    fig.suptitle("Does the starting date change the answer?", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return {"until today": res["until today"][enough], "next 10 years": ten, "worst drop": res["worst drop"]}


DECADES = [("1975–79", 1975, 1979), ("1980s", 1980, 1989), ("1990s", 1990, 1999), ("2000s", 2000, 2009),
           ("2010s", 2010, 2019), ("2020s", 2020, 2029)]


def per_decade(v: pd.Series) -> dict[str, float]:
    """Return per year in each decade (or part of it) covered by the value curve `v`."""
    out = {}
    for name, a, b in DECADES:
        seg, before = v.loc[f"{a}":f"{b}"], v.loc[:f"{a - 1}"]
        if len(seg):
            out[name] = (seg.iloc[-1] / (before.iloc[-1] if len(before) else 1.0)) ** (12 / len(seg)) - 1
    return out


def chart_horizon(px, cash, cfg, path, horizons=range(1, 37)) -> pd.DataFrame:
    """Every momentum horizon with the bot's other settings: average return per decade over the whole period
    and over each half, so you can see whether the best horizon is a broad zone or a lucky spike."""
    import matplotlib.pyplot as plt

    bt = cfg["backtest"]
    shares = {h: tilt_shares(px, {**cfg["bot"], "lookback": h}) for h in horizons}
    common = np.logical_and.reduce([s.notna() for s in shares.values()])     # same start for every horizon
    rows = {}
    for h, s in shares.items():
        v, traded = simulate(px, s.where(common), bt["band"], bt["cost"])
        d = per_decade(v)
        names = list(d)
        rows[h] = {"whole period": np.mean(list(d.values())), "first half": np.mean([d[n] for n in names[:3]]),
                   "second half": np.mean([d[n] for n in names[3:]]), **stats(v, cash), "traded/yr": traded}
    df = pd.DataFrame(rows).T
    fixed, _ = simulate(px, pd.Series(0.8, index=px.index).where(common), bt["band"], bt["cost"])
    ref = np.mean(list(per_decade(fixed).values()))
    first, half = fixed.index[0], DECADES[3][1]
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(df.index, df["whole period"] * 100, "o-", color=STYLE["the bot's tilt"][0], lw=2.4,
            label=f"average per decade, {first:%Y}–{px.index[-1]:%Y}")
    ax.plot(df.index, df["first half"] * 100, "--", color="C1", label=f"average per decade, {first:%Y}–{half - 1} only")
    ax.plot(df.index, df["second half"] * 100, "--", color="C0", label=f"average per decade, {half}–{px.index[-1]:%Y} only")
    ax.axhline(ref * 100, color="k", lw=1, ls=":", label=f"fixed 80/20 ({ref:.1%})")
    chosen = cfg["bot"]["lookback"]
    ax.annotate(f"the bot: {chosen} months", (chosen, df.loc[chosen, "whole period"] * 100),
                xytext=(chosen + 2, df.loc[chosen, "whole period"] * 100 + 0.5), arrowprops=dict(arrowstyle="->"))
    ax.set_xlabel("momentum horizon (months)")
    ax.set_ylabel("% per year")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    hi = cfg["bot"]["base"] + cfg["bot"]["up"]
    ax.set_title(f"Choosing the momentum horizon: average return per decade (stock share "
                 f"{cfg['bot']['base'] - cfg['bot']['down']:.0%}–{hi:.0%}, neutral {cfg['bot']['base']:.0%})", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return df


def chart_rule(px, cfg, path):
    """How z maps to the stock share, over a histogram of the z values that actually occurred."""
    import matplotlib.pyplot as plt

    from .tilt import target_share

    bot = cfg["bot"]
    share = lambda z: target_share(z, bot["base"], bot["up"], bot["down"]) * 100
    occurred = np.array([s.z for s in signals(px, bot)])
    z = np.linspace(-3.5, 3.5, 400)
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax2 = ax.twinx()
    ax2.hist(occurred, bins=40, range=(-3.5, 3.5), color="#bbb", alpha=0.5)
    ax2.set_yticks([])
    ax2.set_ylabel(f"how often each z occurred, {px.index[0]:%Y}–{px.index[-1]:%Y}", color="#888")
    ax.set_zorder(ax2.get_zorder() + 1)
    ax.patch.set_visible(False)
    ax.plot(z, [share(v) for v in z], color=STYLE["the bot's tilt"][0], lw=3)
    ax.axhline(bot["base"] * 100, color="k", lw=0.8, ls=":")
    ax.axvline(0, color="k", lw=0.6)
    for v in (-2, -1, 1, 2):
        s = share(v)
        ax.plot(v, s, "o", color=STYLE["the bot's tilt"][0])
        ax.annotate(f"z = {v:+d}: {s:.0f}/{100 - s:.0f}", (v, s), textcoords="offset points",
                    xytext=(8, -14 if v > 0 else 6), fontsize=9)
    lo, hi = (bot["base"] - bot["down"]) * 100, (bot["base"] + bot["up"]) * 100
    ax.text(-3.35, lo + 0.45 * (bot["base"] * 100 - lo), f"gold has led\n→ down to {lo:.0f}/{100 - lo:.0f}", fontsize=10, color="#a0812c")
    ax.text(1.9, bot["base"] * 100 - 0.3 * (bot["base"] * 100 - lo), f"stocks have led\n→ up to {hi:.0f}/{100 - hi:.0f}",
            fontsize=10, color="#2c5ba0")
    ax.text(0.08, bot["base"] * 100 - 4, f"no lead: {bot['base']:.0%}/{1 - bot['base']:.0%}", fontsize=9, color="#333")
    ax.set_xlim(-3.5, 3.5)
    ax.set_ylim(lo - 3, hi + 3)
    ax.set_xlabel(f"z = stocks' {bot['lookback']}-month lead over gold ÷ its usual size")
    ax.set_ylabel("stock share (%), the rest in gold")
    ax.set_title(f"The rule: a smooth tilt between {lo:.0f}/{100 - lo:.0f} and {hi:.0f}/{100 - hi:.0f}, "
                 f"centred on {bot['base']:.0%}/{1 - bot['base']:.0%}", fontsize=11)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
