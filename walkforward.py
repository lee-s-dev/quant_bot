#!/usr/bin/env python3
"""
walkforward.py — QuantBot 워크포워드 검증 + Plateau 파라미터 선택
═══════════════════════════════════════════════════════════════
목적: "그리드서치 1등 뽑기"의 과최적화를 측정하고,
      OOS(검증 구간)에서도 살아남는 파라미터를 고르는 것.

방식:
  1. 캐시를 최신 캔들까지 연장 (최근 구간 = 순수 홀드아웃)
  2. [IS 4개월 최적화 → OOS 2개월 검증] 을 2개월씩 슬라이드
  3. 각 폴드에서 세 가지 선택법을 OOS로 비교:
       plateau — IS 상위 50개 조합의 파라미터 최빈값 (강건 선택)
       top1    — IS Sharpe 1등 그대로 (기존 방식 = 과최적화 대조군)
       live    — 현재 라이브 봇 파라미터 고정
  4. plateau OOS 거래들로 몬테카를로 부트스트랩 → 손실 확률 추정
═══════════════════════════════════════════════════════════════
"""
import json
import time
import warnings
import itertools
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyupbit

from backtest import (TICKER, INITIAL_CAPITAL, PARAM_GRID, DATA_CACHE,
                      fetch_data, compute_indicators, run_backtest)

warnings.filterwarnings("ignore")

RESULT_FILE   = Path("walkforward_results.json")
IS_CANDLES    = 4 * 30 * 96     # 최적화 구간 ≈ 4개월 (15분봉 96개/일)
OOS_CANDLES   = 2 * 30 * 96     # 검증 구간 ≈ 2개월
MIN_IS_TRADES = 10              # IS 거래수 미달 조합은 plateau 후보 제외
TOP_N_PLATEAU = 50              # plateau 선택에 쓰는 IS 상위 조합 수

# 현재 라이브 봇 파라미터 (quant_bot_4.0.py)
LIVE_PARAMS = {
    "adx_threshold": 28, "highest_n": 60, "atr_min_pct": 0.0015,
    "rsi_max": 76, "vol_mult": 1.3, "time_filter": True,
    "sl_mult": 1.5, "tp1_mult": 2.0, "tp1_pct": 0.40,
    "tp2_mult": 6.0, "trail_mult": 1.2,
}


def update_cache():
    """캐시 마지막 시점 이후의 신규 15분봉을 받아 캐시를 연장."""
    if not DATA_CACHE.exists():
        return
    df = pd.read_pickle(DATA_CACHE)
    last = df.index[-1]
    frames = []
    to_dt = datetime.now()
    while True:
        chunk = pyupbit.get_ohlcv(TICKER, interval="minute15", count=200,
                                  to=to_dt.strftime("%Y%m%d %H%M%S"))
        if chunk is None or chunk.empty:
            break
        frames.append(chunk)
        to_dt = chunk.index[0]
        if chunk.index[0] <= last:
            break
        time.sleep(0.25)
    if not frames:
        return
    merged = pd.concat([df] + frames)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if len(merged) > len(df):
        merged.to_pickle(DATA_CACHE)
        print(f"📡 캐시 연장: ~{last} → ~{merged.index[-1]} "
              f"(+{len(merged) - len(df):,} 캔들)")


def slice_arrays(arrays, s, e):
    return {k: v[s:e] for k, v in arrays.items()}


def grid_search(arrays):
    keys = list(PARAM_GRID.keys())
    return [run_backtest(arrays, dict(zip(keys, combo)))
            for combo in itertools.product(*PARAM_GRID.values())]


def select_plateau(is_results):
    """IS 상위 조합들의 파라미터 최빈값으로 plateau 중심 조합 구성."""
    cands = [r for r in is_results
             if r["n_trades"] >= MIN_IS_TRADES and r["mdd_pct"] >= -50]
    cands.sort(key=lambda r: r["sharpe"], reverse=True)
    top = cands[:TOP_N_PLATEAU]
    if not top:
        return None, []
    chosen = {}
    for k in PARAM_GRID:
        mode_val = pd.Series([t["params"][k] for t in top]).mode().iloc[0]
        chosen[k] = mode_val.item() if hasattr(mode_val, "item") else mode_val
    return chosen, top


def fmt(r):
    return (f"수익률 {r['total_return_pct']:+6.2f}%  Sharpe {r['sharpe']:+6.3f}  "
            f"MDD {r['mdd_pct']:6.2f}%  승률 {r['win_rate']:4.1f}%  "
            f"PF {r['profit_factor']:.2f}  거래 {r['n_trades']}회")


def main():
    update_cache()
    df = compute_indicators(fetch_data())
    arrays = {col: df[col].values for col in
              ["close", "high", "volume", "ema20", "ema50", "adx",
               "atr", "rsi", "vol_ma20", "h40", "h60", "h80", "kst_hour"]}
    n = len(df)

    # 폴드 구성: [IS][OOS] 를 OOS 길이만큼 슬라이드, 마지막 폴드는 끝까지
    folds, s = [], 0
    while s + IS_CANDLES + OOS_CANDLES <= n:
        folds.append((s, s + IS_CANDLES, s + IS_CANDLES + OOS_CANDLES))
        s += OOS_CANDLES
    if folds:
        a, b, _ = folds[-1]
        folds[-1] = (a, b, n)

    print(f"총 {n:,} 캔들 ({df.index[0].date()} ~ {df.index[-1].date()}), "
          f"폴드 {len(folds)}개")

    methods = ["plateau", "top1", "live"]
    agg = {m: [] for m in methods}
    fold_reports = []

    for fi, (s, m_, e) in enumerate(folds, 1):
        print(f"\n── 폴드 {fi}: IS {df.index[s].date()}~{df.index[m_-1].date()}"
              f"  /  OOS {df.index[m_].date()}~{df.index[e-1].date()} ──")
        is_arr, oos_arr = slice_arrays(arrays, s, m_), slice_arrays(arrays, m_, e)

        t0 = time.time()
        is_results = grid_search(is_arr)
        print(f"   IS 그리드서치 {len(is_results):,}조합 완료 ({time.time()-t0:.0f}s)")

        plateau, top = select_plateau(is_results)
        top1 = top[0]["params"] if top else None

        rep = {"fold": fi,
               "is_period":  [str(df.index[s]), str(df.index[m_ - 1])],
               "oos_period": [str(df.index[m_]), str(df.index[e - 1])]}
        for name, params in [("plateau", plateau), ("top1", top1), ("live", LIVE_PARAMS)]:
            if params is None:
                continue
            is_r  = run_backtest(is_arr, params)
            oos_r = run_backtest(oos_arr, params, collect_pnls=True)
            agg[name].append(oos_r)
            rep[name] = {
                "params": params,
                "is":  {k: is_r[k] for k in ("total_return_pct", "sharpe", "n_trades")},
                "oos": {k: oos_r[k] for k in ("total_return_pct", "sharpe", "mdd_pct",
                                              "win_rate", "profit_factor", "n_trades")},
            }
            print(f"   [{name:7}] IS {is_r['total_return_pct']:+6.2f}%  →  OOS {fmt(oos_r)}")
        fold_reports.append(rep)

    # ── OOS 종합 ─────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("  📊 OOS 종합 (모든 폴드의 검증 구간 합산)")
    print("=" * 64)
    summary = {}
    for name in methods:
        rs = agg[name]
        if not rs:
            continue
        rets = [r["total_return_pct"] for r in rs]
        comp = (np.prod([1 + x / 100 for x in rets]) - 1) * 100
        pos  = sum(1 for x in rets if x > 0)
        ntr  = sum(r["n_trades"] for r in rs)
        print(f"  {name:7}: OOS 복리 {comp:+.2f}%   평균 {np.mean(rets):+.2f}%/폴드   "
              f"양(+)폴드 {pos}/{len(rs)}   총 거래 {ntr}회")
        summary[name] = {
            "oos_compound_pct": round(float(comp), 2),
            "oos_mean_pct": round(float(np.mean(rets)), 2),
            "positive_folds": f"{pos}/{len(rs)}",
            "total_trades": ntr,
        }

    # ── 몬테카를로: 방식별 OOS 거래 부트스트랩 (방식 라벨 명시) ──
    mc = {}
    rng = np.random.default_rng(42)
    for name in methods:
        pnls = [p for r in agg[name] for p in r.get("pnls", [])]
        if len(pnls) < 15:
            continue
        sims = np.array([rng.choice(pnls, size=len(pnls), replace=True).sum()
                         for _ in range(3000)]) / INITIAL_CAPITAL * 100
        mc[name] = {"n_trades": len(pnls),
                    "p5":  round(float(np.percentile(sims, 5)), 2),
                    "p50": round(float(np.percentile(sims, 50)), 2),
                    "p95": round(float(np.percentile(sims, 95)), 2),
                    "prob_loss_pct": round(float((sims < 0).mean() * 100), 1)}
        m = mc[name]
        print(f"\n  🎲 몬테카를로 [{name}] (OOS {len(pnls)}건 부트스트랩 3,000회)")
        print(f"     총수익률 분포: p5 {m['p5']:+.2f}% / p50 {m['p50']:+.2f}% / "
              f"p95 {m['p95']:+.2f}%   손실 확률 {m['prob_loss_pct']:.0f}%")

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump({"run_at": datetime.now().isoformat(),
                   "data_range": [str(df.index[0]), str(df.index[-1])],
                   "folds": fold_reports, "summary": summary,
                   "monte_carlo": mc},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"\n💾 저장: {RESULT_FILE}")


if __name__ == "__main__":
    main()
