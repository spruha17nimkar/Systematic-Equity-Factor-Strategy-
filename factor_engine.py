"""Cross-sectional equity factor engine: data, factor signals, portfolio construction, backtest and metrics.

Pipeline
    panel (date, ticker, ret, mktcap, book_to_market, roe)
      -> raw signals: Value, Momentum, Quality, Low-Vol, Size
      -> cross-sectional winsorise (1st/99th pct) and z-score each month
      -> composite score (equal-weighted average of available z-scores)
      -> quantile sort at month end t, hold over month t+1
      -> long-short (top minus bottom quantile, dollar-neutral) and long-only (top quantile)
      -> transaction costs = cost_bps x one-way turnover, charged per leg

Data sources
    load_data("synthetic")  simulated panel with known factor premia (default; see make_synthetic_panel)
    load_data("yfinance")   real prices for a list of tickers; only the price-based factors are available
    load_french_factors()   Kenneth French data library factor returns, for a real-world reference
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

FACTORS = ["value", "momentum", "quality", "low_vol", "size"]
PANEL_COLUMNS = ["date", "ticker", "ret", "mktcap", "book_to_market", "roe"]


# --------------------------------------------------------------------------------------------- data
@dataclass
class SyntheticParams:
    """Calibration of the simulated market.

    ``premia`` are monthly returns per 1 s.d. of each true characteristic. With a top-minus-bottom quintile
    spread of about 2.8 s.d. they give long-short premia of roughly value 4%, momentum 7%, quality 3.4%,
    low-vol 3.4% and size 1.7% a year. ``premium_vol`` gives factor volatilities of roughly 10%, 15%, 7%, 12%
    and 10% a year. Both are in the range of long-run US estimates (Fama-French / AQR factor data), so a single
    factor has a Sharpe ratio of about 0.3-0.5 before measurement noise and costs.
    """
    n_stocks: int = 1000
    n_months: int = 300
    burn_in: int = 24
    seed: int = 7
    premia: dict = field(default_factory=lambda: {"value": 0.0012, "momentum": 0.0021, "quality": 0.0010,
                                                  "low_vol": 0.0010, "size": 0.0005})
    premium_vol: dict = field(default_factory=lambda: {"value": 0.010, "momentum": 0.015, "quality": 0.007,
                                                       "low_vol": 0.012, "size": 0.010})
    persistence: dict = field(default_factory=lambda: {"value": 0.98, "quality": 0.99, "size": 0.995, "log_vol": 0.98})
    proxy_noise: float = 1.0          # s.d. of measurement error in observed value / quality / size proxies
    market_mean: float = 0.006
    market_vol: float = 0.045
    idio_vol: float = 0.08            # median monthly idiosyncratic volatility
    idio_vol_dispersion: float = 0.35


def _ar1(rng, shape, rho, n_burn):
    n, t = shape
    x = np.empty((n, t + n_burn))
    x[:, 0] = rng.standard_normal(n)
    innov = np.sqrt(1 - rho ** 2)
    for k in range(1, t + n_burn):
        x[:, k] = rho * x[:, k - 1] + innov * rng.standard_normal(n)
    return x[:, n_burn:]


def make_synthetic_panel(p: SyntheticParams = SyntheticParams()) -> pd.DataFrame:
    """Simulated monthly panel of excess-of-cash returns with known factor premia.

    Each stock has slowly moving latent characteristics (value, quality, smallness, idiosyncratic volatility).
    Next month's return loads on them, and on the stock's own standardised 12-1 month past return (momentum
    as return continuation), with time-varying premia:
        r_{i,t+1} = beta_i MKT_{t+1} + sum_k x_{k,i,t} (lambda_k + s_k eta_{k,t+1}) + sigma_{i,t} eps_{i,t+1}
    The engine never sees the latents. It sees noisy proxies (book-to-market, ROE, market cap) and computes
    momentum and volatility from the simulated returns, so the factors predict returns, but not perfectly.
    Betas are independent of volatility, so low-vol is not offset by market beta.
    """
    rng = np.random.default_rng(p.seed)
    n, T = p.n_stocks, p.n_months + p.burn_in
    lat = {k: _ar1(rng, (n, T), p.persistence[k], 100) for k in ("value", "quality", "size")}
    log_vol = _ar1(rng, (n, T), p.persistence["log_vol"], 100)
    sigma = p.idio_vol * np.exp(p.idio_vol_dispersion * log_vol)
    lat["low_vol"] = -log_vol
    beta = 1 + 0.25 * rng.standard_normal(n)

    mkt = p.market_mean + p.market_vol * rng.standard_normal(T)
    ret = np.zeros((n, T))
    log_ret = np.zeros((n, T))
    for t in range(1, T):
        r = beta * mkt[t] + sigma[:, t - 1] * rng.standard_normal(n)
        for k in ("value", "quality", "size", "low_vol"):
            r += lat[k][:, t - 1] * (p.premia[k] + p.premium_vol[k] * rng.standard_normal())
        if t >= 13:   # 12-1 momentum known at t-1: returns t-12 .. t-2
            mom = log_ret[:, t - 12:t - 1].sum(axis=1)
            r += (mom - mom.mean()) / mom.std() * (p.premia["momentum"] + p.premium_vol["momentum"] * rng.standard_normal())
        ret[:, t] = np.clip(r, -0.95, None)
        log_ret[:, t] = np.log1p(ret[:, t])

    noise = lambda: _ar1(rng, (n, T), 0.9, 50)  # noqa: E731  persistent measurement error
    book_to_market = np.exp(-0.7 + 0.45 * (lat["value"] + p.proxy_noise * noise()))
    roe = 0.12 + 0.06 * (lat["quality"] + p.proxy_noise * noise())
    mktcap = np.exp(8.0 - 1.3 * (lat["size"] + 0.5 * p.proxy_noise * noise()))   # $m

    dates = pd.date_range(end="2025-12-31", periods=T, freq="ME")   # labels only; the data are simulated
    tickers = [f"S{i:04d}" for i in range(n)]
    panel = pd.DataFrame({
        "date": np.tile(dates, n), "ticker": np.repeat(tickers, T), "ret": ret.ravel(),
        "mktcap": mktcap.ravel(), "book_to_market": book_to_market.ravel(), "roe": roe.ravel(),
    })
    return panel[panel["date"] > dates[0]].reset_index(drop=True)


def load_yfinance(tickers: list[str], start: str = "2005-01-01", end: str | None = None) -> pd.DataFrame:
    """Real monthly returns for ``tickers``. Only price-based factors (Momentum, Low-Vol) can be computed:
    yfinance has no point-in-time book equity, ROE or share-count history, so those columns are NaN and the
    composite uses the factors that are available. The universe is whatever list is passed in, so a list of
    today's index members carries survivorship bias."""
    import yfinance as yf

    px = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)["Close"]
    monthly = px.resample("ME").last()
    ret = monthly.pct_change().iloc[1:]
    long = ret.stack().rename("ret").reset_index()
    long.columns = ["date", "ticker", "ret"]
    for c in ("mktcap", "book_to_market", "roe"):
        long[c] = np.nan
    return long[PANEL_COLUMNS]


def load_data(source: str = "synthetic", **kwargs) -> pd.DataFrame:
    """Return a long panel with columns PANEL_COLUMNS. Replace or extend this to plug in real data."""
    if source == "synthetic":
        return make_synthetic_panel(SyntheticParams(**kwargs))
    if source == "yfinance":
        return load_yfinance(**kwargs)
    raise ValueError(f"unknown source {source!r}")


FRENCH = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/{}_CSV.zip"


def _french_table(name: str) -> pd.DataFrame:
    import requests

    raw = requests.get(FRENCH.format(name), timeout=60).content
    text = zipfile.ZipFile(io.BytesIO(raw)).read(zipfile.ZipFile(io.BytesIO(raw)).namelist()[0]).decode("latin-1")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith(",") and i + 1 < len(lines)
                 and lines[i + 1].strip()[:6].isdigit())
    rows = []
    for ln in lines[start + 1:]:
        cells = [c.strip() for c in ln.split(",")]
        if not cells[0].isdigit() or len(cells[0]) != 6:
            break
        rows.append(cells)
    cols = ["date"] + [c.strip() for c in lines[start].split(",")[1:]]
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m") + pd.offsets.MonthEnd(0)
    return df.set_index("date").apply(pd.to_numeric, errors="coerce") / 100


def load_french_factors() -> pd.DataFrame:
    """Monthly long-short factor returns from the Kenneth French data library (real US data, 1963 onward):
    SMB (size), HML (value), RMW (profitability / quality), UMD (momentum) and a low-volatility spread
    (lowest minus highest variance quintile, value-weighted), plus Mkt-RF and RF."""
    ff5 = _french_table("F-F_Research_Data_5_Factors_2x3")
    mom = _french_table("F-F_Momentum_Factor")
    var = _french_table("Portfolios_Formed_on_VAR")
    out = ff5[["Mkt-RF", "SMB", "HML", "RMW", "RF"]].join(mom.iloc[:, [0]].rename(columns=lambda _: "UMD"))
    out["LowVol"] = var["Lo 20"] - var["Hi 20"]
    return out.dropna()


# ------------------------------------------------------------------------------------------ signals
def to_wide(panel: pd.DataFrame, col: str) -> pd.DataFrame:
    return panel.pivot(index="date", columns="ticker", values=col).sort_index()


def raw_signals(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Raw factor signals observed at each month end (higher = more attractive)."""
    ret = to_wide(panel, "ret")
    log1p = np.log1p(ret)
    sig = {
        # 12-1 momentum: cumulative return from t-11 to t-1, skipping the most recent month
        "momentum": np.expm1(log1p.shift(1).rolling(11, min_periods=11).sum()),
        # low volatility: minus trailing 12-month volatility of monthly returns
        "low_vol": -ret.rolling(12, min_periods=12).std(),
    }
    if panel["book_to_market"].notna().any():
        sig["value"] = np.log(to_wide(panel, "book_to_market"))
    if panel["roe"].notna().any():
        sig["quality"] = to_wide(panel, "roe")
    if panel["mktcap"].notna().any():
        sig["size"] = -np.log(to_wide(panel, "mktcap"))
    return {k: sig[k] for k in FACTORS if k in sig}


def winsorize_cs(df: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    lo = df.quantile(lower, axis=1)
    hi = df.quantile(upper, axis=1)
    return df.clip(lo, hi, axis=0)


def zscore_cs(df: pd.DataFrame) -> pd.DataFrame:
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


def factor_scores(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {k: zscore_cs(winsorize_cs(v)) for k, v in raw_signals(panel).items()}


def composite(scores: dict[str, pd.DataFrame], factors: list[str] | None = None,
              weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Weighted average of factor z-scores over the factors a stock has data for."""
    factors = factors or list(scores)
    weights = weights or {f: 1.0 for f in factors}
    num = sum(scores[f].fillna(0) * weights[f] for f in factors)
    den = sum(scores[f].notna() * weights[f] for f in factors)
    return (num / den.replace(0, np.nan)).where(den >= 0.6 * sum(weights[f] for f in factors))


# ------------------------------------------------------------------------------------------ portfolios
def quantile_weights(score: pd.Series, n_q: int, construction: str) -> pd.Series:
    s = score.dropna()
    if len(s) < 2 * n_q:
        return pd.Series(dtype=float)
    q = pd.qcut(s.rank(method="first"), n_q, labels=False)
    top, bottom = q == n_q - 1, q == 0
    w = pd.Series(0.0, index=s.index)
    w[top] = 1.0 / top.sum()
    if construction == "long_short":
        w[bottom] = -1.0 / bottom.sum()
    elif construction != "long_only":
        raise ValueError(construction)
    return w[w != 0]


def _drift(w: pd.Series, r: pd.Series) -> pd.Series:
    """Weights after one month of returns, each leg re-normalised to its pre-drift gross exposure, so that
    turnover measures only the trading needed to get back to the new target weights."""
    if w.empty:
        return w
    grown = w * (1 + r.reindex(w.index).fillna(0))
    out = grown.copy()
    for leg in (w > 0, w < 0):
        if leg.any():
            out[leg] = grown[leg] / grown[leg].abs().sum() * w[leg].abs().sum()
    return out


def backtest(panel: pd.DataFrame, score: pd.DataFrame, n_q: int = 5, construction: str = "long_short",
             cost_bps: float = 10.0, lag: int = 0) -> pd.DataFrame:
    """Monthly rebalanced quantile portfolio. ``score`` at month end t (shifted by ``lag`` extra months) sets
    weights held over month t+1. Costs: cost_bps per unit of one-way turnover, charged on each leg."""
    ret = to_wide(panel, "ret")
    score = score.shift(lag).reindex(ret.index)
    dates = ret.index
    prev = pd.Series(dtype=float)
    rows = []
    for t in range(len(dates) - 1):
        w = quantile_weights(score.iloc[t], n_q, construction)
        if w.empty:
            continue
        drifted = _drift(prev, ret.iloc[t]) if not prev.empty else pd.Series(dtype=float)
        idx = w.index.union(drifted.index)
        turnover = (w.reindex(idx, fill_value=0) - drifted.reindex(idx, fill_value=0)).abs().sum()
        r_next = ret.iloc[t + 1].reindex(w.index).fillna(0)
        gross = float((w * r_next).sum())
        cost = cost_bps / 1e4 * turnover
        rows.append({"date": dates[t + 1], "gross": gross, "cost": cost, "net": gross - cost, "turnover": turnover,
                     "n_long": int((w > 0).sum()), "n_short": int((w < 0).sum())})
        prev = w
    cols = ["gross", "cost", "net", "turnover", "n_long", "n_short"]
    return pd.DataFrame(rows, columns=["date", *cols]).set_index("date")


def benchmark_returns(panel: pd.DataFrame) -> pd.Series:
    """Cap-weighted universe (equal-weighted if market caps are unavailable)."""
    ret = to_wide(panel, "ret")
    if panel["mktcap"].notna().any():
        cap = to_wide(panel, "mktcap").shift(1)
        w = cap.div(cap.sum(axis=1), axis=0)
        return (w * ret).sum(axis=1, min_count=1).dropna().rename("benchmark")
    return ret.mean(axis=1).rename("benchmark")


# ------------------------------------------------------------------------------------------ metrics
def newey_west_t(x: pd.Series | np.ndarray, lags: int = 6) -> float:
    """t-statistic of the mean with Newey-West (Bartlett) standard errors."""
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    e = x - x.mean()
    s = e @ e / n
    for k in range(1, lags + 1):
        s += 2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / n
    return float(x.mean() / np.sqrt(s / n)) if s > 0 else float("nan")


def max_drawdown(r: pd.Series) -> float:
    wealth = (1 + r).cumprod()
    return float((wealth / wealth.cummax() - 1).min())


def metrics(r: pd.Series, turnover: pd.Series | None = None, periods: int = 12) -> dict:
    r = r.dropna()
    ann_ret = (1 + r).prod() ** (periods / len(r)) - 1
    vol = r.std(ddof=1) * np.sqrt(periods)
    downside = r[r < 0].std(ddof=1) * np.sqrt(periods)
    out = {
        "ann_return": float(ann_ret), "ann_vol": float(vol),
        "sharpe": float(r.mean() / r.std(ddof=1) * np.sqrt(periods)),
        "sortino": float(r.mean() * periods / downside) if downside > 0 else np.nan,
        "max_drawdown": max_drawdown(r), "nw_t_stat": newey_west_t(r),
        "hit_rate": float((r > 0).mean()), "skew": float(stats.skew(r)), "excess_kurtosis": float(stats.kurtosis(r)),
        "months": int(len(r)), "start": str(r.index[0].date()), "end": str(r.index[-1].date()),
    }
    if turnover is not None:
        out["avg_monthly_turnover"] = float(turnover.mean())
    return out


def run_strategy(panel: pd.DataFrame, n_q: int = 5, cost_bps: float = 10.0, factors: list[str] | None = None,
                 lag: int = 0, scores: dict | None = None) -> dict:
    """Composite long-short and long-only backtests plus the benchmark, with metrics."""
    scores = scores or factor_scores(panel)
    comp = composite(scores, factors)
    ls = backtest(panel, comp, n_q, "long_short", cost_bps, lag)
    lo = backtest(panel, comp, n_q, "long_only", cost_bps, lag)
    bench = benchmark_returns(panel).reindex(lo.index)
    return {
        "long_short": ls, "long_only": lo, "benchmark": bench,
        "metrics": {"Long-Short": metrics(ls["net"], ls["turnover"]), "Long-Only": metrics(lo["net"], lo["turnover"]),
                    "Benchmark": metrics(bench)},
    }
