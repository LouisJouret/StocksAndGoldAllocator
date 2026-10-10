"""Command line: `uv run trader signal | rebalance | backtest`."""

import argparse

import pandas as pd
from rich.console import Console
from rich.table import Table

from .config import load_config, resolve

console = Console()


def short(path) -> str:
    """A path relative to the project if it is inside it."""
    try:
        return str(path.relative_to(resolve(".")))
    except ValueError:
        return str(path)


def _signals(cfg):
    """Monthly signals of the tilt, from Yahoo prices of the signal funds."""
    from .data import fetch_yahoo
    from .tilt import month_end_closes, signals

    b = cfg["bot"]
    today = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    start = f"{today.year - 8}-01-01"
    daily = pd.concat({k: fetch_yahoo(s, start)["close"] for k, s in b["signal"].items()}, axis=1)
    daily.index = daily.index.tz_localize(None)
    closes = month_end_closes(daily, today)
    if closes.index[-1] < (today.to_period("M") - 1).to_timestamp(how="end").normalize():
        raise SystemExit(f"Prices end {closes.index[-1]:%Y-%m-%d}: last month is missing, not trading on stale data.")
    return signals(closes, b)


def cmd_signal(args):
    cfg = load_config(args.config)
    b = cfg["bot"]
    sig = _signals(cfg)
    t = Table("month end", f"{b['signal']['stocks']} 12 mo", f"{b['signal']['gold']} 12 mo", "z",
              f"target {b['stocks']}", f"target {b['gold']}")
    for s in sig[-13:]:
        t.add_row(f"{s.month:%Y-%m}", f"{s.stocks_12m:+.1%}", f"{s.gold_12m:+.1%}", f"{s.z:+.2f}",
                  f"{s.stock_share:.0%}", f"{1 - s.stock_share:.0%}")
    console.print(t)
    s = sig[-1]
    console.print(f"Target for this month (from prices up to {s.month:%Y-%m-%d}): {s.stock_share:.1%} {b['stocks']}, "
                  f"{1 - s.stock_share:.1%} {b['gold']}. Trades only if {b['stocks']} is more than "
                  f"{b['band']:.0%} away from it.")


def cmd_rebalance(args):
    import csv
    from datetime import datetime, timezone

    from .broker import Broker, month_start_utc
    from .data import fetch_yahoo
    from .tilt import plan

    cfg = load_config(args.config)
    b = cfg["bot"]
    if args.live and not args.execute:
        console.print("--live only matters together with --execute; showing a dry run against the live account.")
    sig = _signals(cfg)[-1]
    targets = {b["stocks"]: sig.stock_share, b["gold"]: 1 - sig.stock_share}
    broker = Broker(live=args.live)
    mode = "LIVE" if args.live else "paper"
    fractional = {s: broker.fractionable(s) for s in targets}
    cash, equity = broker.account()
    held = broker.positions()
    others = sorted(set(held) - set(targets))
    if others:
        console.print(f"[yellow]Also holds {others}: left alone and not counted in the strategy's budget.[/yellow]")
    holdings = {s: held.get(s, (0.0, 0.0))[1] for s in targets}
    price = {s: held[s][1] / held[s][0] if s in held and held[s][0] else float(fetch_yahoo(s, "2020-01-01")["close"].iloc[-1])
             for s in targets}

    console.print(f"{mode} account: cash {cash:,.2f}, equity {equity:,.2f} | signal from {sig.month:%Y-%m}: "
                  f"z {sig.z:+.2f} → target {sig.stock_share:.1%} {b['stocks']} / {1 - sig.stock_share:.1%} {b['gold']}")
    orders, reason = plan(cash, holdings, targets, b["band"], b["cash_buffer"], b["min_order"])
    done = broker.orders_since(month_start_utc(), list(targets))
    still_open = [o for o in done if str(o.status).split(".")[-1].lower() not in ("filled", "partially_filled")]
    if still_open and not args.force:
        console.print(f"{len(still_open)} order(s) from earlier are still open: waiting for them (--force to override).")
        return
    if orders and done and any(o.side == "sell" for o in orders) and not args.force:
        console.print(f"Already rebalanced this month ({len(done)} order(s)); the next rebalance can happen next month "
                      "(--force to override). New cash is still invested on any day.")
        return
    console.print(reason)
    t = Table("fund", "now", "now %", "target %", "order")
    invested = sum(holdings.values())
    for s in targets:
        o = [f"{x.side} {x.amount:,.2f}" for x in orders if x.symbol == s]
        t.add_row(s, f"{holdings[s]:,.2f}", f"{holdings[s] / invested:.1%}" if invested else "–", f"{targets[s]:.1%}",
                  ", ".join(o) or "–")
    console.print(t)

    status = "dry run"
    if orders and not args.execute:
        console.print(f"Dry run: nothing sent. Add --execute to place these orders in the {mode} account.")
    elif orders and not broker.market_open():
        status = "market closed: will try again on the next run"
        console.print("The US market is closed (open 15:30–22:00 in Zurich/Berlin): nothing sent, will try again next run.")
    elif orders:
        results = []
        for o in [x for x in orders if x.side == "sell"]:
            r = broker.submit(o.symbol, "sell", o.amount, price[o.symbol], held.get(o.symbol, (0.0, 0.0))[0], fractional[o.symbol])
            results.append(f"sell {o.symbol}: {broker.wait(r.id) if r else 'skipped'}")
        cash, _ = broker.account()
        spendable = cash - b["cash_buffer"] * (cash + sum(v for _, v in broker.positions().values()))
        buys = [x for x in orders if x.side == "buy"]
        scale = min(1.0, spendable / sum(x.amount for x in buys)) if buys and spendable > 0 else 0.0
        for o in buys:
            r = broker.submit(o.symbol, "buy", o.amount * scale, price[o.symbol], 0.0, fractional[o.symbol])
            results.append(f"buy {o.symbol}: {broker.wait(r.id) if r else 'skipped'}")
        status = "; ".join(results)
        console.print(status)
    log = resolve(b["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    new = not log.exists()
    with log.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "mode", "signal_month", "z", "target_stocks", "holdings", "orders", "result"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), mode, f"{sig.month:%Y-%m}", f"{sig.z:.3f}",
                    f"{sig.stock_share:.4f}", {s: round(v, 2) for s, v in holdings.items()},
                    [f"{o.side} {o.symbol} {o.amount:.2f}" for o in orders], status if orders else reason])
    console.print(f"Logged to {short(log)}")


def cmd_backtest(args):
    from . import backtest as bt

    cfg = load_config(args.config)
    out = resolve(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    console.print("Getting prices (Yahoo, Kenneth French's data library, monthly gold) ...")
    px, cash = bt.history(cfg)
    console.print(f"Monthly data {px.index[0]:%b %Y} – {px.index[-1]:%b %Y} ({len(px)} months)")
    pct = lambda x: f"{x:+.1%}"

    targets = bt.strategies(px, cfg)
    mixes = bt.chart_mixes(px, cash, cfg, out / "mixes.png", tilt=targets["the bot's tilt"])
    t = Table("stocks/gold", "avg gold", "per year", "volatility", "sharpe", "worst drop", "worst 10 yrs",
              "median 10 yrs", "10 yrs beating 100% stocks")
    for name, r in mixes.iterrows():
        t.add_row(name, f"{r['average gold']:.0%}",
                  pct(r["per year"]), f"{r['volatility']:.1%}", f"{r['sharpe']:.2f}", f"{r['worst drop']:.0%}",
                  pct(r["worst 10 years"]), pct(r["median 10 years"]), f"{r['10 years beating 100% stocks']:.0%}")
    console.print(t)

    table, decades = bt.chart_tilt(px, cash, cfg, targets, out / "tilt.png")
    t = Table("strategy", "per year", "volatility", "sharpe", "worst drop", "traded/yr", *decades.index)
    for name, r in table.iterrows():
        t.add_row(name, pct(r["per year"]), f"{r['volatility']:.1%}", f"{r['sharpe']:.2f}", f"{r['worst drop']:.0%}",
                  f"{r['traded/yr']:.0%}", *[pct(decades.loc[d, name]) for d in decades.index])
    console.print(t)

    console.print("Every start month (takes a minute) ...")
    res = bt.chart_start_dates(px, cfg, targets, out / "start_dates.png")
    t = Table("from every start month", "beats 100% stocks", "beats fixed 80/20", "worst 10 years")
    for name in targets:
        today, ten = res["until today"], res["next 10 years"]
        t.add_row(name, f"{(today[name] > today['100% stocks']).mean():.0%} until today, "
                        f"{(ten[name] > ten['100% stocks']).mean():.0%} over 10 yrs",
                  f"{(today[name] > today['fixed 80/20']).mean():.0%} until today, "
                  f"{(ten[name] > ten['fixed 80/20']).mean():.0%} over 10 yrs", pct(ten[name].min()))
    console.print(t)
    console.print(f"Charts: {short(out / 'mixes.png')}, {short(out / 'tilt.png')}, {short(out / 'start_dates.png')}")


def main():
    p = argparse.ArgumentParser(prog="trader", description="Momentum tilt between world stocks and gold: bot and backtest")
    p.add_argument("--config", help="config file (default: config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("signal", help="this month's target mix (no broker needed)")
    s.set_defaults(func=cmd_signal)

    s = sub.add_parser("rebalance", help="bring the Alpaca account to this month's target (dry run unless --execute)")
    s.add_argument("--execute", action="store_true", help="really place the orders (paper account unless --live)")
    s.add_argument("--live", action="store_true", help="use the live account (ALPACA_LIVE_API_KEY / _SECRET_KEY)")
    s.add_argument("--force", action="store_true", help="trade even if a rebalance already happened this month")
    s.set_defaults(func=cmd_rebalance)

    s = sub.add_parser("backtest", help="backtest the rule and fixed mixes monthly since 1972; charts in results/")
    s.set_defaults(func=cmd_backtest)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
