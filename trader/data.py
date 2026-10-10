"""Price data: daily from Yahoo Finance, plus two free monthly sources for the long backtest. No keys needed."""

import io
import time
import urllib.request
import zipfile

import pandas as pd

from .config import resolve

NY = "America/New_York"
COLUMNS = ["open", "high", "low", "close", "volume"]
FRENCH_FACTORS = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip"
GOLD_MONTHLY = "https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv"


def fetch_yahoo(symbol: str, start: str) -> pd.DataFrame:
    """Daily bars, adjusted for dividends and splits, indexed by New York date."""
    import yfinance as yf

    df = yf.Ticker(symbol).history(start=start, interval="1d", auto_adjust=True, actions=False)
    if df.empty:
        raise RuntimeError(f"Yahoo returned no data for {symbol}")
    df = df.rename(columns=str.lower)[COLUMNS].astype("float64")
    df.index = pd.DatetimeIndex(df.index).tz_convert(NY).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    # Drop today's bar until the market has closed: it is still changing
    return df[df.index + pd.Timedelta(hours=16) <= pd.Timestamp.now(tz=NY)]


def _cached(url: str, name: str, max_age_days: float = 7) -> bytes:
    path = resolve("data") / name
    if not path.exists() or time.time() - path.stat().st_mtime > max_age_days * 86400:
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as r:
            path.write_bytes(r.read())
    return path.read_bytes()


def us_market_monthly() -> pd.DataFrame:
    """Monthly US stock market total return and 1-month T-bill return since 1926 (Kenneth French's library).

    Columns: total, cash. Index: month ends.
    """
    raw = _cached(FRENCH_FACTORS, "french_factors.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    rows = [line.split(",") for line in text.splitlines()]
    rows = [r for r in rows if r[0].strip().isdigit() and len(r[0].strip()) == 6]   # monthly rows only (YYYYMM)
    df = pd.DataFrame({"total": [float(r[1]) + float(r[4]) for r in rows], "cash": [float(r[4]) for r in rows]},
                      index=pd.PeriodIndex([r[0].strip() for r in rows], freq="M").to_timestamp(how="end").normalize())
    return df / 100


def gold_monthly() -> pd.Series:
    """Average gold price per month in USD since 1833 (free since 1971, before that fixed by governments)."""
    df = pd.read_csv(io.BytesIO(_cached(GOLD_MONTHLY, "gold_monthly.csv")))
    return pd.Series(df["Price"].to_numpy(), index=pd.PeriodIndex(df["Date"], freq="M").to_timestamp(how="end").normalize())
