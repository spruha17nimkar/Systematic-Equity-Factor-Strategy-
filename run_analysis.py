"""Runs the strategy and the robustness suite, then writes charts, an Excel workbook, a JSON summary and a
Markdown report to output/.

    python run_analysis.py                 # synthetic panel (default) + Kenneth French real-data check
    python run_analysis.py --no-french     # skip the download of the real-data check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import factor_engine as fe  # noqa: E402
import robustness as rb  # noqa: E402

OUT = Path("output")
FIG = OUT / "figures"
COLORS = {"Long-Short": "#1f4e79", "Long-Only": "#2e7d32", "Benchmark": "#7f8c8d"}


def french_check() -> dict:
    """Same idea on real data: an equal-weighted composite of published US long-short factor returns."""
    ff = fe.load_french_factors()
    factors = {"Value (HML)": "HML", "Momentum (UMD)": "UMD", "Quality (RMW)": "RMW", "Low-Vol (Lo20-Hi20)": "LowVol",
               "Size (SMB)": "SMB"}
    rets = pd.DataFrame({k: ff[v] for k, v in factors.items()})
    rets["Composite (equal weight)"] = rets.mean(axis=1)
    rets["Market (Mkt-RF)"] = ff["Mkt-RF"]
    table = pd.DataFrame({k: fe.metrics(rets[k]) for k in rets}).T
    recent = rets[rets.index >= "2000-01-01"]
    table_recent = pd.DataFrame({k: fe.metrics(recent[k]) for k in recent}).T
    return {"returns": rets, "metrics": table, "metrics_since_2000": table_recent,
            "correlation": rets.drop(columns=["Composite (equal weight)", "Market (Mkt-RF)"]).corr(),
            "subperiods": rb.subperiods(rets["Composite (equal weight)"])}


def charts(res: dict, rob: dict, french: dict | None) -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    series = {"Long-Short": res["long_short"]["net"], "Long-Only": res["long_only"]["net"], "Benchmark": res["benchmark"]}

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    for k, r in series.items():
        w = (1 + r.fillna(0)).cumprod()
        a1.plot(w.index, w, label=k, color=COLORS[k], lw=1.8)
        a2.plot(w.index, w / w.cummax() - 1, color=COLORS[k], lw=1.2)
    a1.set_yscale("log"); a1.set_ylabel("growth of $1 (log)"); a1.legend()
    a1.set_title("Composite five-factor strategy, net of 10bp/leg costs (synthetic panel)", fontsize=11)
    a2.set_ylabel("drawdown"); a2.yaxis.set_major_formatter(lambda x, _: f"{x:.0%}")
    fig.tight_layout(); fig.savefig(FIG / "equity_curves.png", dpi=150); plt.close(fig)

    ls = res["long_short"]["net"]
    roll = ls.rolling(36).mean() / ls.rolling(36).std() * np.sqrt(12)
    fig, ax = plt.subplots(figsize=(10, 3.2))
    ax.plot(roll.index, roll, color=COLORS["Long-Short"]); ax.axhline(0, color="black", lw=0.6)
    ax.set_title("Rolling 36-month Sharpe ratio, long-short", fontsize=11)
    fig.tight_layout(); fig.savefig(FIG / "rolling_sharpe.png", dpi=150); plt.close(fig)

    dec = rob["decomposition"]["standalone"].set_index("portfolio")
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.5))
    axes[0].bar(dec.index, dec["sharpe"], color=["#95a5a6"] * (len(dec) - 1) + [COLORS["Long-Short"]])
    axes[0].set_title("Standalone vs composite Sharpe", fontsize=10); axes[0].tick_params(axis="x", rotation=45)
    c = rob["costs"]
    axes[1].plot(c["cost_bps_per_leg"], c["sharpe"], marker="o", color=COLORS["Long-Short"])
    axes[1].axhline(0, color="black", lw=0.6); axes[1].set_xlabel("bps per leg"); axes[1].set_title("Cost sensitivity", fontsize=10)
    d = rob["decay"]
    axes[2].bar(d["lag_months"].astype(str), d["sharpe"], color=COLORS["Long-Short"])
    axes[2].set_xlabel("implementation lag (months)"); axes[2].set_title("Signal decay", fontsize=10)
    fig.tight_layout(); fig.savefig(FIG / "robustness.png", dpi=150); plt.close(fig)

    b = rob["bootstrap"]
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.hist(b["distribution"], bins=60, color=COLORS["Long-Short"], alpha=0.7)
    for v, ls_ in [(b["p05"], "--"), (b["p95"], "--"), (b["sharpe_point"], "-")]:
        ax.axvline(v, color="black", ls=ls_, lw=1)
    ax.axvline(0, color="#c0392b", lw=1)
    ax.set_title(f"Block bootstrap of the long-short Sharpe ({b['n_boot']:,} resamples, {b['block_months']}-month blocks)", fontsize=10)
    fig.tight_layout(); fig.savefig(FIG / "bootstrap.png", dpi=150); plt.close(fig)

    if french:
        r = french["returns"]
        fig, ax = plt.subplots(figsize=(10, 4))
        for col in r.columns:
            w = (1 + r[col]).cumprod()
            lw, color = (2.4, COLORS["Long-Short"]) if col.startswith("Composite") else (1.0, None)
            ax.plot(w.index, w, label=col, lw=lw, color=color)
        ax.set_yscale("log"); ax.legend(fontsize=7, ncol=2)
        ax.set_title("Real data: Kenneth French US factor returns (gross, long-short)", fontsize=11)
        fig.tight_layout(); fig.savefig(FIG / "french_factors.png", dpi=150); plt.close(fig)


def md_table(df: pd.DataFrame, fmt: dict | None = None, index: bool = True) -> str:
    fmt = fmt or {}
    d = df.reset_index() if index else df.copy()
    lines = ["| " + " | ".join(map(str, d.columns)) + " |", "|" + "|".join("---:" if pd.api.types.is_numeric_dtype(d[c]) else "---" for c in d.columns) + "|"]
    for _, row in d.iterrows():
        cells = []
        for c in d.columns:
            v = row[c]
            f = fmt.get(c)
            if pd.isna(v) if not isinstance(v, str) else False:
                cells.append("–")
            elif f:
                cells.append(f.format(v))
            else:
                cells.append(f"{v:.3f}" if isinstance(v, (float, np.floating)) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


PCT = "{:.1%}"


def report(res: dict, rob: dict, french: dict | None, panel: pd.DataFrame) -> str:
    m = pd.DataFrame(res["metrics"]).T
    head = m[["ann_return", "ann_vol", "sharpe", "max_drawdown", "nw_t_stat", "avg_monthly_turnover"]]
    b, dsr, cs = rob["bootstrap"], rob["deflated"], rob["costs"]
    dec = rob["decomposition"]
    L = [
        "# Results", "",
        f"Synthetic panel: {panel['ticker'].nunique():,} stocks, {panel['date'].nunique()} months "
        f"({panel['date'].min():%Y-%m} to {panel['date'].max():%Y-%m}). Quintile sorts, monthly rebalancing, 10bp per leg.", "",
        "## Headline (net of costs)", "",
        md_table(head, {"ann_return": PCT, "ann_vol": PCT, "max_drawdown": PCT, "sharpe": "{:.2f}", "nw_t_stat": "{:.2f}",
                        "avg_monthly_turnover": "{:.2f}"}), "",
        "![equity](figures/equity_curves.png)", "",
        "## 1. Sub-periods", "", md_table(rob["subperiods"], {"ann_return": PCT, "max_drawdown": PCT, "sharpe": "{:.2f}"}, index=False), "",
        "## 2. Parameter sensitivity", "",
        md_table(rob["parameters"], {"ann_return": PCT, "ann_vol": PCT, "max_drawdown": PCT, "sharpe": "{:.2f}", "nw_t_stat": "{:.2f}",
                                     "avg_monthly_turnover": "{:.2f}"}, index=False), "",
        "## 3. Cost sensitivity (long-short)", "", md_table(cs, {"ann_return": PCT, "sharpe": "{:.2f}", "nw_t_stat": "{:.2f}"}, index=False), "",
        f"Break-even cost: {cs.attrs['breakeven_cost_bps']:.0f}bp per leg.", "",
        "## 4. Block bootstrap", "",
        f"{b['n_boot']:,} circular block resamples ({b['block_months']}-month blocks): P(Sharpe > 0) = {b['p_sharpe_positive']:.3f}; "
        f"5th-95th percentile [{b['p05']:.2f}, {b['p95']:.2f}]; median {b['p50']:.2f}.", "",
        "![bootstrap](figures/bootstrap.png)", "",
        "## 5. Factor decomposition", "",
        md_table(dec["standalone"], {"ann_return": PCT, "sharpe": "{:.2f}", "nw_t_stat": "{:.2f}", "avg_monthly_turnover": "{:.2f}"}, index=False), "",
        f"Composite beats every standalone factor: {'yes' if dec['composite_beats_all'] else 'no'}.", "",
        "Leave-one-out:", "", md_table(dec["leave_one_out"], {"sharpe": "{:.2f}", "sharpe_change": "{:+.2f}"}, index=False), "",
        f"Return attribution (composite regressed on the standalone factor portfolios, R² {dec['r2']:.2f}):", "",
        md_table(dec["attribution"], {"beta": "{:.2f}", "contribution_ann": PCT, "share_of_return": PCT}, index=False), "",
        "## 6. Signal decay", "", md_table(rob["decay"], {"ann_return": PCT, "sharpe": "{:.2f}", "nw_t_stat": "{:.2f}"}, index=False), "",
        "![robustness](figures/robustness.png)", "",
        "## 7. Deflated Sharpe ratio", "",
        f"{dsr['n_trials']} distinct configurations evaluated. Expected maximum Sharpe from that many unskilled trials: "
        f"{dsr['expected_max_sharpe_annual']:.2f}. Observed {dsr['observed_sharpe_annual']:.2f}. "
        f"Deflated Sharpe probability {dsr['deflated_sharpe_prob']:.3f} → {'passes' if dsr['passes_95'] else 'fails'} at 95%.", "",
    ]
    if french:
        cols = ["ann_return", "ann_vol", "sharpe", "max_drawdown", "nw_t_stat", "start", "end"]
        f = {"ann_return": PCT, "ann_vol": PCT, "max_drawdown": PCT, "sharpe": "{:.2f}", "nw_t_stat": "{:.2f}"}
        L += ["## Real-data check: Kenneth French US factors (gross of costs)", "",
              md_table(french["metrics"][cols], f), "", "Since 2000:", "", md_table(french["metrics_since_2000"][cols], f), "",
              "Composite sub-periods:", "", md_table(french["subperiods"], {"ann_return": PCT, "max_drawdown": PCT, "sharpe": "{:.2f}"}, index=False), "",
              "![french](figures/french_factors.png)", ""]
    return "\n".join(L)


def excel(res: dict, rob: dict, french: dict | None, path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(res["metrics"]).T.to_excel(xw, sheet_name="Summary")
        pd.DataFrame({"long_short_net": res["long_short"]["net"], "long_short_turnover": res["long_short"]["turnover"],
                      "long_only_net": res["long_only"]["net"], "long_only_turnover": res["long_only"]["turnover"],
                      "benchmark": res["benchmark"]}).to_excel(xw, sheet_name="Monthly returns")
        rob["subperiods"].to_excel(xw, sheet_name="1 Sub-periods", index=False)
        rob["parameters"].to_excel(xw, sheet_name="2 Parameters", index=False)
        rob["costs"].to_excel(xw, sheet_name="3 Costs", index=False)
        pd.Series({k: v for k, v in rob["bootstrap"].items() if k != "distribution"}).to_excel(xw, sheet_name="4 Bootstrap")
        rob["decomposition"]["standalone"].to_excel(xw, sheet_name="5 Decomposition", index=False)
        rob["decomposition"]["attribution"].to_excel(xw, sheet_name="5 Decomposition", index=False, startrow=10)
        rob["decomposition"]["leave_one_out"].to_excel(xw, sheet_name="5 Decomposition", index=False, startrow=20)
        rob["decay"].to_excel(xw, sheet_name="6 Signal decay", index=False)
        pd.Series(rob["deflated"]).to_excel(xw, sheet_name="7 Deflated Sharpe")
        if french:
            french["metrics"].to_excel(xw, sheet_name="Real data (French)")
            french["returns"].to_excel(xw, sheet_name="French monthly")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-french", action="store_true")
    args = ap.parse_args()

    panel = fe.load_data("synthetic")
    scores = fe.factor_scores(panel)
    res = fe.run_strategy(panel, scores=scores)
    rob = rb.run_suite(panel, scores)
    french = None if args.no_french else french_check()

    OUT.mkdir(exist_ok=True)
    charts(res, rob, french)
    (OUT / "REPORT.md").write_text(report(res, rob, french, panel))
    excel(res, rob, french, OUT / "factor_strategy_results.xlsx")
    summary = {"headline": res["metrics"],
               "robustness": {"subperiod_sharpes": rob["subperiods"]["sharpe"].round(3).tolist(),
                              "costs": rob["costs"].round(4).to_dict(orient="records"),
                              "breakeven_cost_bps": rob["costs"].attrs["breakeven_cost_bps"],
                              "bootstrap": {k: v for k, v in rob["bootstrap"].items() if k != "distribution"},
                              "composite_beats_all": rob["decomposition"]["composite_beats_all"],
                              "decay_sharpes": rob["decay"].set_index("lag_months")["sharpe"].round(3).to_dict(),
                              "deflated": rob["deflated"]}}
    if french:
        summary["real_data_french"] = french["metrics"][["ann_return", "ann_vol", "sharpe", "max_drawdown", "nw_t_stat", "start", "end"]].to_dict(orient="index")
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=float))

    m = pd.DataFrame(res["metrics"]).T
    print(m[["ann_return", "sharpe", "max_drawdown", "nw_t_stat"]].round(3).to_string())
    print(f"Deflated Sharpe probability {rob['deflated']['deflated_sharpe_prob']:.3f} ({rob['deflated']['n_trials']} trials)")
    print("Outputs written to output/")


if __name__ == "__main__":
    main()
