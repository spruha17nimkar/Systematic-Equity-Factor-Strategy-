import numpy as np
import pandas as pd
import pytest

import factor_engine as fe
import robustness as rb


@pytest.fixture(scope="module")
def small_panel():
    return fe.make_synthetic_panel(fe.SyntheticParams(n_stocks=200, n_months=96, seed=3))


def toy_panel(rets: dict[str, list[float]]) -> pd.DataFrame:
    dates = pd.date_range("2020-01-31", periods=len(next(iter(rets.values()))), freq="ME")
    rows = [{"date": d, "ticker": t, "ret": r, "mktcap": 1.0, "book_to_market": np.nan, "roe": np.nan}
            for t, series in rets.items() for d, r in zip(dates, series)]
    return pd.DataFrame(rows)


def test_panel_schema(small_panel):
    assert list(small_panel.columns) == fe.PANEL_COLUMNS
    assert small_panel.groupby("date")["ticker"].nunique().eq(200).all()


def test_winsorize_and_zscore():
    df = pd.DataFrame([list(range(100)) + [10_000]], dtype=float)
    w = fe.winsorize_cs(df)
    assert w.iloc[0].max() < 10_000
    z = fe.zscore_cs(w)
    assert z.iloc[0].mean() == pytest.approx(0, abs=1e-12)
    assert z.iloc[0].std() == pytest.approx(1)


def test_momentum_skips_most_recent_month():
    # 13 months of returns; the 12-1 signal at the last date must ignore the final month's return.
    a = [0.01] * 12 + [0.50]
    b = [0.02] * 12 + [-0.50]
    sig = fe.raw_signals(toy_panel({"A": a, "B": b}))["momentum"]
    last = sig.iloc[-1]
    assert last["B"] > last["A"]
    assert last["A"] == pytest.approx(1.01 ** 11 - 1)


def test_composite_averages_available_scores():
    s = {"value": pd.DataFrame({"x": [1.0], "y": [-1.0]}), "momentum": pd.DataFrame({"x": [3.0], "y": [np.nan]})}
    c = fe.composite(s)
    assert c.loc[0, "x"] == pytest.approx(2.0)
    assert np.isnan(c.loc[0, "y"])          # only half its factors observed: below the 60% coverage rule


def test_quantile_weights_are_dollar_neutral_and_long_only_sums_to_one():
    score = pd.Series(np.arange(50, dtype=float), index=[f"s{i}" for i in range(50)])
    ls = fe.quantile_weights(score, 5, "long_short")
    assert ls.sum() == pytest.approx(0) and ls[ls > 0].sum() == pytest.approx(1)
    assert set(ls[ls > 0].index) == {f"s{i}" for i in range(40, 50)}
    lo = fe.quantile_weights(score, 5, "long_only")
    assert lo.sum() == pytest.approx(1) and (lo > 0).all()


def test_backtest_uses_next_month_returns_and_charges_costs():
    # Two stocks; the score always prefers A. Long-short return each month = r_A - r_B of the NEXT month.
    # Four stocks, two quantiles: long A and C, short B and D (equal weights of 0.5 in each leg).
    panel = toy_panel({"A": [0.0, 0.05, 0.02, 0.03], "B": [0.0, -0.01, 0.00, 0.01],
                       "C": [0.0, 0.05, 0.02, 0.03], "D": [0.0, -0.01, 0.00, 0.01]})
    score = pd.DataFrame({"A": 1.0, "B": 0.0, "C": 1.0, "D": 0.0}, index=sorted(panel["date"].unique()))
    bt = fe.backtest(panel, score, n_q=2, construction="long_short", cost_bps=10)
    assert list(bt["gross"].round(10)) == [0.06, 0.02, 0.02]
    assert bt["turnover"].iloc[0] == pytest.approx(2.0)          # initial build of both legs
    assert bt["cost"].iloc[0] == pytest.approx(2.0 * 10 / 1e4)
    assert bt["turnover"].iloc[1:].max() < 1e-12                 # same names, legs re-normalised: no trading


def test_metrics_and_newey_west():
    r = pd.Series([0.01, -0.005] * 60, index=pd.date_range("2010-01-31", periods=120, freq="ME"))
    m = fe.metrics(r)
    assert m["sharpe"] == pytest.approx(r.mean() / r.std() * np.sqrt(12))
    assert m["max_drawdown"] == pytest.approx(-0.005)
    iid = np.random.default_rng(0).normal(0.01, 0.02, 5000)
    assert fe.newey_west_t(iid, lags=0) == pytest.approx(iid.mean() / (iid.std() / np.sqrt(len(iid))), rel=1e-3)


def test_strategy_recovers_built_in_premia(small_panel):
    res = fe.run_strategy(small_panel)
    assert res["metrics"]["Long-Short"]["ann_return"] > 0
    # The composite needs 60% factor coverage, so it starts in month 1 with the three fundamental factors.
    assert len(res["long_short"]) == small_panel["date"].nunique() - 1


def test_cost_sensitivity_is_monotonic_and_matches_breakeven():
    bt = pd.DataFrame({"gross": [0.01] * 24, "turnover": [1.0] * 24}, index=pd.date_range("2020-01-31", periods=24, freq="ME"))
    out = rb.cost_sensitivity(bt, costs=(0, 50, 100))
    assert out["ann_return"].is_monotonic_decreasing
    assert out.attrs["breakeven_cost_bps"] == pytest.approx(100)


def test_block_bootstrap_centres_on_sample_sharpe():
    r = pd.Series(np.random.default_rng(1).normal(0.01, 0.03, 300))
    b = rb.block_bootstrap(r, n_boot=500)
    assert b["p05"] < b["sharpe_point"] < b["p95"]
    assert 0 <= b["p_sharpe_positive"] <= 1


def test_deflated_sharpe_penalises_more_trials():
    r = pd.Series(np.random.default_rng(2).normal(0.008, 0.03, 240))
    few = rb.deflated_sharpe(r, [0.2, 0.4, 0.6])
    many = rb.deflated_sharpe(r, list(np.linspace(-0.5, 1.5, 100)))
    assert many["expected_max_sharpe_annual"] > few["expected_max_sharpe_annual"]
    assert many["deflated_sharpe_prob"] < few["deflated_sharpe_prob"]


def test_subperiods_cover_the_sample():
    r = pd.Series(np.zeros(100), index=pd.date_range("2000-01-31", periods=100, freq="ME"))
    sp = rb.subperiods(r)
    assert sp["months"].sum() == 100 and len(sp) == 4


def test_backtest_returns_empty_frame_when_universe_too_small():
    panel = toy_panel({"A": [0.0, 0.01], "B": [0.0, 0.02]})
    score = pd.DataFrame({"A": 1.0, "B": 0.0}, index=sorted(panel["date"].unique()))
    assert fe.backtest(panel, score, n_q=2).empty
