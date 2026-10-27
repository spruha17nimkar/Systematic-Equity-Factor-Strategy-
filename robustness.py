"""Seven robustness tests for the composite factor strategy.

1. Sub-periods            Sharpe in four equal, non-overlapping sub-samples
2. Parameter sensitivity  3 / 5 / 10 quantiles x long-short / long-only
3. Cost sensitivity       0-50 bps per leg
4. Block bootstrap        circular block bootstrap of monthly net returns (12-month blocks)
5. Factor decomposition   standalone factors, leave-one-out composites, regression on factor returns
6. Signal decay           implementation lag of 0-6 months
7. Deflated Sharpe        Bailey & Lopez de Prado (2014) haircut for the number of configurations tried
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

import factor_engine as fe


def sharpe(r: pd.Series, periods: int = 12) -> float:
    r = pd.Series(r).dropna()
    return float(r.mean() / r.std(ddof=1) * np.sqrt(periods)) if r.std(ddof=1) > 0 else np.nan


# 1 -------------------------------------------------------------------------------------------------
def subperiods(ls_net: pd.Series, n: int = 4) -> pd.DataFrame:
    r = ls_net.dropna()
    parts = [r.iloc[idx] for idx in np.array_split(np.arange(len(r)), n)]
    return pd.DataFrame([{"period": f"{p.index[0]:%Y-%m} to {p.index[-1]:%Y-%m}", "months": len(p),
                          "ann_return": (1 + p).prod() ** (12 / len(p)) - 1, "sharpe": sharpe(p),
                          "max_drawdown": fe.max_drawdown(p)} for p in parts])


# 2 -------------------------------------------------------------------------------------------------
def parameter_sensitivity(panel, comp, quantiles=(3, 5, 10), constructions=("long_short", "long_only"), cost_bps=10.0):
    rows = []
    for c in constructions:
        for q in quantiles:
            bt = fe.backtest(panel, comp, q, c, cost_bps)
            m = fe.metrics(bt["net"], bt["turnover"])
            rows.append({"construction": c, "quantiles": q, **{k: m[k] for k in
                         ("ann_return", "ann_vol", "sharpe", "max_drawdown", "nw_t_stat", "avg_monthly_turnover")}})
    return pd.DataFrame(rows)


# 3 -------------------------------------------------------------------------------------------------
def cost_sensitivity(bt: pd.DataFrame, costs=(0, 5, 10, 20, 30, 50)) -> pd.DataFrame:
    """Re-prices the same trades at different cost levels (turnover does not depend on costs)."""
    rows = []
    for c in costs:
        net = bt["gross"] - c / 1e4 * bt["turnover"]
        rows.append({"cost_bps_per_leg": c, "ann_return": (1 + net).prod() ** (12 / len(net)) - 1,
                     "sharpe": sharpe(net), "nw_t_stat": fe.newey_west_t(net)})
    out = pd.DataFrame(rows)
    g = bt["gross"].mean()
    tc = bt["turnover"].mean() / 1e4
    out.attrs["breakeven_cost_bps"] = float(g / tc) if tc > 0 else np.inf
    return out


# 4 -------------------------------------------------------------------------------------------------
def block_bootstrap(r: pd.Series, n_boot: int = 2000, block: int = 12, seed: int = 0) -> dict:
    """Circular block bootstrap of the Sharpe ratio (keeps autocorrelation within 12-month blocks)."""
    x = pd.Series(r).dropna().to_numpy()
    n = len(x)
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    samples = x[idx.reshape(n_boot, -1)[:, :n]]
    sr = samples.mean(axis=1) / samples.std(axis=1, ddof=1) * np.sqrt(12)
    return {"n_boot": n_boot, "block_months": block, "sharpe_point": sharpe(x), "p_sharpe_positive": float((sr > 0).mean()),
            "p05": float(np.percentile(sr, 5)), "p50": float(np.percentile(sr, 50)), "p95": float(np.percentile(sr, 95)),
            "distribution": sr}


# 5 -------------------------------------------------------------------------------------------------
def factor_decomposition(panel, scores: dict, n_q: int = 5, cost_bps: float = 10.0) -> dict:
    factors = list(scores)
    standalone, rets = [], {}
    for f in factors:
        bt = fe.backtest(panel, scores[f], n_q, "long_short", cost_bps)
        rets[f] = bt["net"]
        m = fe.metrics(bt["net"], bt["turnover"])
        standalone.append({"portfolio": f, "ann_return": m["ann_return"], "sharpe": m["sharpe"], "nw_t_stat": m["nw_t_stat"],
                           "avg_monthly_turnover": m["avg_monthly_turnover"]})
    full = fe.backtest(panel, fe.composite(scores, factors), n_q, "long_short", cost_bps)
    m = fe.metrics(full["net"], full["turnover"])
    standalone.append({"portfolio": "composite", "ann_return": m["ann_return"], "sharpe": m["sharpe"],
                       "nw_t_stat": m["nw_t_stat"], "avg_monthly_turnover": m["avg_monthly_turnover"]})
    loo = []
    for f in factors:
        bt = fe.backtest(panel, fe.composite(scores, [g for g in factors if g != f]), n_q, "long_short", cost_bps)
        loo.append({"dropped": f, "sharpe": sharpe(bt["net"]), "sharpe_change": sharpe(bt["net"]) - m["sharpe"]})

    # Return attribution: composite on the standalone factor portfolios (OLS); contribution = beta x mean.
    F = pd.DataFrame(rets).dropna()
    y = full["net"].reindex(F.index)
    X = np.column_stack([np.ones(len(F)), F.to_numpy()])
    beta, *_ = np.linalg.lstsq(X, y.to_numpy(), rcond=None)
    resid = y.to_numpy() - X @ beta
    attrib = pd.DataFrame({"factor": factors, "beta": beta[1:], "contribution_ann": beta[1:] * F.mean().to_numpy() * 12})
    attrib.loc[len(attrib)] = ["alpha (diversification / interaction)", np.nan, beta[0] * 12]
    attrib["share_of_return"] = attrib["contribution_ann"] / (y.mean() * 12)
    corr = F.corr()
    return {"standalone": pd.DataFrame(standalone), "leave_one_out": pd.DataFrame(loo), "attribution": attrib,
            "r2": float(1 - resid.var() / y.var()), "factor_correlation": corr,
            "composite_beats_all": bool(m["sharpe"] > max(s["sharpe"] for s in standalone[:-1]))}


# 6 -------------------------------------------------------------------------------------------------
def signal_decay(panel, comp, lags=(0, 1, 2, 3, 6), n_q: int = 5, cost_bps: float = 10.0) -> pd.DataFrame:
    rows = []
    for L in lags:
        bt = fe.backtest(panel, comp, n_q, "long_short", cost_bps, lag=L)
        m = fe.metrics(bt["net"], bt["turnover"])
        rows.append({"lag_months": L, "ann_return": m["ann_return"], "sharpe": m["sharpe"], "nw_t_stat": m["nw_t_stat"]})
    return pd.DataFrame(rows)


# 7 -------------------------------------------------------------------------------------------------
def deflated_sharpe(r: pd.Series, trial_sharpes: list[float]) -> dict:
    """Probability that the true Sharpe exceeds the maximum expected from ``len(trial_sharpes)`` unskilled trials
    (Bailey & Lopez de Prado 2014). Sharpe ratios are per period (monthly) inside the formula."""
    x = pd.Series(r).dropna()
    T = len(x)
    sr = x.mean() / x.std(ddof=1)
    g3, g4 = stats.skew(x), stats.kurtosis(x, fisher=False)
    trials = np.asarray(trial_sharpes, float) / np.sqrt(12)
    N = len(trials)
    var_sr = trials.var(ddof=1) if N > 1 else 0.0
    emc = 0.5772156649
    sr0 = np.sqrt(var_sr) * ((1 - emc) * stats.norm.ppf(1 - 1 / N) + emc * stats.norm.ppf(1 - 1 / (N * np.e))) if N > 1 else 0.0
    z = (sr - sr0) * np.sqrt(T - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    psr0 = (sr) * np.sqrt(T - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    return {"n_trials": N, "observed_sharpe_annual": float(sr * np.sqrt(12)), "expected_max_sharpe_annual": float(sr0 * np.sqrt(12)),
            "deflated_sharpe_prob": float(stats.norm.cdf(z)), "probabilistic_sharpe_prob": float(stats.norm.cdf(psr0)),
            "passes_95": bool(stats.norm.cdf(z) > 0.95)}


def run_suite(panel: pd.DataFrame, scores: dict | None = None, n_q: int = 5, cost_bps: float = 10.0,
              n_boot: int = 2000) -> dict:
    scores = scores or fe.factor_scores(panel)
    comp = fe.composite(scores)
    ls = fe.backtest(panel, comp, n_q, "long_short", cost_bps)
    out = {
        "subperiods": subperiods(ls["net"]),
        "parameters": parameter_sensitivity(panel, comp, cost_bps=cost_bps),
        "costs": cost_sensitivity(ls),
        "bootstrap": block_bootstrap(ls["net"], n_boot),
        "decomposition": factor_decomposition(panel, scores, n_q, cost_bps),
        "decay": signal_decay(panel, comp, n_q=n_q, cost_bps=cost_bps),
    }
    # Every distinct configuration evaluated above counts as a trial for the deflated Sharpe ratio.
    trials = (list(out["parameters"]["sharpe"]) + list(out["costs"]["sharpe"]) + list(out["decay"]["sharpe"])
              + list(out["decomposition"]["standalone"]["sharpe"]) + list(out["decomposition"]["leave_one_out"]["sharpe"]))
    trials = list(dict.fromkeys(round(s, 10) for s in trials))   # the base configuration appears in several tests
    out["deflated"] = deflated_sharpe(ls["net"], trials)
    return out
