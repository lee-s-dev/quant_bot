#!/usr/bin/env python3
"""
backtest.py — QuantBot V4.0 파라미터 최적화 백테스트
═══════════════════════════════════════════════════════
시드    : 100,000 KRW (10만원)
종목    : KRW-BTC (Upbit)
데이터  : 15분봉 + 1시간봉 (pyupbit)
방식    : 풀 그리드서치 → Sharpe/수익률/MDD/승률/PF 기준 TOP 랭킹
═══════════════════════════════════════════════════════
"""

import time
import json
import warnings
import itertools
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyupbit
import ta

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════
# ① 상수
# ══════════════════════════════════════════════════════════════
TICKER          = "KRW-BTC"
KST             = ZoneInfo("Asia/Seoul")
INITIAL_CAPITAL = 100_000       # 10만원
FEE_RATE        = 0.0005        # 업비트 수수료 0.05% (단방향)
SLIPPAGE        = 0.0003        # 시장가 슬리피지 추정 (단방향)
COST            = FEE_RATE + SLIPPAGE   # 왕복 0.16%
DATA_CACHE      = Path("btc_15m_cache.pkl")
RESULT_FILE     = Path("backtest_results.json")

# ══════════════════════════════════════════════════════════════
# ② 최적화할 파라미터 그리드
#    총 조합 수 = 각 리스트 길이의 곱
# ══════════════════════════════════════════════════════════════
PARAM_GRID = {
    # ── 진입 조건 ───────────────────────────────────────────
    "adx_threshold": [25, 28, 30],              # 현재 28
    "highest_n":     [40, 60, 80],              # 현재 40
    "atr_min_pct":   [0.0015, 0.0025],          # 현재 0.15%
    "rsi_max":       [72, 76, 9999],            # 현재 미사용
    "vol_mult":      [1.3, 9999],               # 현재 미사용
    "time_filter":   [True, False],             # KST 01~04시 차단 여부 ★신규
    # ── 포지션 관리 ─────────────────────────────────────────
    "sl_mult":       [1.0, 1.5, 2.0],           # 손절 ATR 배수
    "tp1_mult":      [2.0, 2.5],                # 1차 익절
    "tp1_pct":       [0.30, 0.40],              # 1차 익절 비율
    "tp2_mult":      [4.0, 6.0, 8.0],           # 2차 익절
    "trail_mult":    [0.5, 0.8, 1.2],           # TP1 이후 트레일
}
# 총 3×3×2×3×2×2×3×2×2×3×3 = 11,664 조합 (시간필터 옵션 포함)


# ══════════════════════════════════════════════════════════════
# ③ 데이터 수집 (캐시 우선)
# ══════════════════════════════════════════════════════════════
def fetch_data(months: int = 12) -> pd.DataFrame:
    """Upbit 15분봉 데이터를 다운로드하거나 캐시에서 로드."""
    if DATA_CACHE.exists():
        df = pd.read_pickle(DATA_CACHE)
        print(f"✅ 캐시 로드: {len(df):,}개 캔들  "
              f"({df.index[0].strftime('%Y-%m-%d')} ~ "
              f"{df.index[-1].strftime('%Y-%m-%d')})")
        return df

    print(f"📡 Upbit에서 {months}개월치 15분봉 다운로드 중...")
    frames = []
    to_dt  = datetime.now()
    target = months * 30 * 24 * 4      # 15분봉 개수

    while True:
        chunk = pyupbit.get_ohlcv(
            TICKER, interval="minute15", count=200,
            to=to_dt.strftime("%Y%m%d %H%M%S")
        )
        if chunk is None or chunk.empty:
            break
        frames.append(chunk)
        to_dt = chunk.index[0]
        collected = len(frames) * 200
        print(f"  {collected:,} / ~{target:,} 캔들 수집...", end="\r")
        if collected >= target:
            break
        time.sleep(0.35)

    df = pd.concat(frames).sort_index().drop_duplicates()
    df.to_pickle(DATA_CACHE)
    print(f"\n✅ 저장 완료: {len(df):,}개 캔들")
    return df


# ══════════════════════════════════════════════════════════════
# ④ 지표 사전 계산 (모든 조합에서 공유)
# ══════════════════════════════════════════════════════════════
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    모든 파라미터 조합에서 공통으로 쓰이는 지표를 한 번에 계산.
    - 1h: EMA20, EMA50, ADX14
    - 15m: ATR14, RSI14, Volume MA20
    - highest_n: 40 / 60 / 80 캔들 롤링 최고가 (모두 pre-compute)
    - KST 시간 (시간대 필터용)
    """
    print("📊 지표 계산 중...", end=" ")

    # ── 1시간봉 리샘플 → 1h 지표 ─────────────────────────────
    df_1h = df.resample("1h").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"),   close=("close", "last"),
        volume=("volume", "sum")
    ).dropna()

    ema20_1h = ta.trend.EMAIndicator(df_1h["close"], window=20).ema_indicator()
    ema50_1h = ta.trend.EMAIndicator(df_1h["close"], window=50).ema_indicator()
    adx_1h   = ta.trend.ADXIndicator(
        df_1h["high"], df_1h["low"], df_1h["close"], window=14
    ).adx()

    # 15분봉 인덱스로 forward-fill.
    # shift(1): 라벨 H의 1h 봉은 H시 *종가*까지 포함하므로 그대로 ffill하면
    # 같은 시간대의 15분봉이 미래 종가를 보게 됨 → 직전 완결 봉 값만 사용
    df["ema20"] = ema20_1h.shift(1).reindex(df.index, method="ffill")
    df["ema50"] = ema50_1h.shift(1).reindex(df.index, method="ffill")
    df["adx"]   = adx_1h.shift(1).reindex(df.index, method="ffill")

    # ── 15분봉 지표 ──────────────────────────────────────────
    df["atr"] = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=14
    ).average_true_range()

    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    # ── 여러 highest_n 사전 계산 ─────────────────────────────
    for n in [40, 60, 80]:
        # shift(1): 현재 봉 제외, 직전 봉까지의 최고가
        df[f"h{n}"] = df["high"].shift(1).rolling(n).max()

    # ── KST 시간 (pyupbit naive 인덱스는 KST로 간주) ─────────────
    if df.index.tz is None:
        kst_index = df.index.tz_localize(KST)
    else:
        kst_index = df.index.tz_convert(KST)
    df["kst_hour"] = kst_index.hour

    df = df.dropna()
    print(f"완료 ({len(df):,}개 유효 행)")
    return df


# ══════════════════════════════════════════════════════════════
# ⑤ 단일 백테스트 엔진 (NumPy 배열로 속도 최적화)
# ══════════════════════════════════════════════════════════════
def run_backtest(arrays: dict, params: dict, collect_pnls: bool = False) -> dict:
    """
    하나의 파라미터 조합으로 백테스트를 실행하고 성과지표를 반환.

    상태 머신 (라이브 봇과 정합):
        0 = IDLE     → 확정봉 종가 기준 진입 조건 스캔, 충족 시 그 봉 종가에 체결
        2 = POSITION → TP1 / TP2 / 손절 모니터링
    ※ 진입은 "확정봉이 직전 highest_n 위에서 마감 + 같은 봉의 RSI/거래량/ATR 필터
      통과"의 단일봉 판정. 라이브 봇도 15분봉 마감 직후 동일 조건으로 시장가 매수.
    """
    p = params

    # 배열 언패킹
    close    = arrays["close"]
    high     = arrays["high"]       # noqa (사용 예비)
    ema20    = arrays["ema20"]
    ema50    = arrays["ema50"]
    adx      = arrays["adx"]
    atr      = arrays["atr"]
    rsi      = arrays["rsi"]
    highest  = arrays[f"h{p['highest_n']}"]
    vol      = arrays["volume"]
    vol_ma   = arrays["vol_ma20"]
    kst_hour = arrays["kst_hour"]
    n        = len(close)

    # 파라미터 언패킹
    adx_thr  = p["adx_threshold"]
    atr_min  = p["atr_min_pct"]
    rsi_max  = p["rsi_max"]
    vol_mult = p["vol_mult"]
    sl_mult  = p["sl_mult"]
    tp1_mult = p["tp1_mult"]
    tp1_pct  = p["tp1_pct"]
    tp2_mult = p["tp2_mult"]
    trail    = p["trail_mult"]

    # 상태 변수
    capital   = float(INITIAL_CAPITAL)
    btc_qty   = 0.0
    state     = 0
    entry_px  = 0.0
    atr_entry = 0.0
    tp1_done  = False
    cooldown  = -1        # cooldown 종료 인덱스

    # 결과 누적
    equity    = np.empty(n)
    equity[0] = capital
    trades    = []        # (type, pnl)

    for i in range(1, n):
        px = close[i]
        equity[i] = capital + btc_qty * px

        # ── 쿨다운 중 ────────────────────────────────────────
        if i <= cooldown:
            continue

        # ── IDLE: 확정봉 진입 판정 → 충족 시 그 봉 종가에 체결 ──
        if state == 0:
            # 1h 상위 추세 필터 (직전 완결 1h 봉 기준)
            if not (adx[i] >= adx_thr
                    and ema20[i] > ema50[i]
                    and px > ema20[i] * 1.005):
                continue
            # 종가 돌파 확정 (확정봉 종가 > 직전 highest_n 최고가)
            if px <= highest[i]:
                continue
            # RSI 과매수 필터 (돌파봉 자신의 값)
            if rsi_max < 9999 and rsi[i] > rsi_max:
                continue
            # 거래량 필터 (돌파봉 자신의 값)
            if vol_mult < 9999 and vol[i] < vol_ma[i] * vol_mult:
                continue
            # ATR 변동성 필터
            if atr[i] / px < atr_min:
                continue
            # 시간 필터: KST 01~04시 제외 (옵션)
            if p["time_filter"] and 1 <= kst_hour[i] <= 4:
                continue

            # ── 포지션 사이즈 계산 (1% 리스크 룰) ───────────
            sl_pct  = sl_mult * atr[i] / px
            if sl_pct <= 0:
                continue
            budget  = min(
                capital * 0.01 / sl_pct,   # 1% 리스크
                capital * 0.50             # 최대 50%
            )
            if budget < 5_000 or capital < 5_000:
                continue

            # ── 매수 체결 (돌파봉 마감 직후 시장가 ≈ 그 봉 종가) ──
            exec_px   = px * (1 + COST)
            qty       = budget / exec_px
            capital  -= budget
            btc_qty   = qty
            entry_px  = exec_px
            atr_entry = atr[i]
            tp1_done  = False
            state     = 2
            trades.append({"type": "BUY", "pnl": 0.0})

        # ── POSITION: 청산 조건 모니터링 ─────────────────────
        elif state == 2:
            safe_atr = atr_entry if atr_entry > 0 else entry_px * 0.02

            # 손절가 / 트레일 손절가
            stop_px = (
                entry_px + trail * safe_atr  # TP1 이후: 트레일
                if tp1_done
                else entry_px - sl_mult * safe_atr
            )
            tp1_px = entry_px + tp1_mult * safe_atr
            tp2_px = entry_px + tp2_mult * safe_atr

            # ── 손절 / 트레일 컷 ────────────────────────────
            if px <= stop_px:
                exec_px   = px * (1 - COST)
                realized  = (exec_px - entry_px) * btc_qty
                capital  += btc_qty * exec_px
                btc_qty   = 0.0
                trades.append({"type": "SELL_SL", "pnl": realized})
                state = 0
                if not tp1_done:
                    cooldown = i + 12   # 3시간 쿨다운 (15분봉 × 12)
                tp1_done = False

            # ── 1차 익절 ─────────────────────────────────────
            elif not tp1_done and px >= tp1_px:
                qty_sell  = btc_qty * tp1_pct
                exec_px   = px * (1 - COST)
                realized  = (exec_px - entry_px) * qty_sell
                capital  += qty_sell * exec_px
                btc_qty  -= qty_sell
                trades.append({"type": "SELL_TP1", "pnl": realized})
                tp1_done  = True

            # ── 2차 전량 익절 ────────────────────────────────
            elif tp1_done and px >= tp2_px:
                exec_px   = px * (1 - COST)
                realized  = (exec_px - entry_px) * btc_qty
                capital  += btc_qty * exec_px
                btc_qty   = 0.0
                trades.append({"type": "SELL_TP2", "pnl": realized})
                state    = 0
                tp1_done = False

    # 마지막 미결 포지션 강제 청산
    if state == 2 and btc_qty > 0:
        exec_px  = close[-1] * (1 - COST)
        realized = (exec_px - entry_px) * btc_qty
        capital += btc_qty * exec_px
        btc_qty  = 0.0
        trades.append({"type": "SELL_EOD", "pnl": realized})
        equity[-1] = capital

    # ── 성과 지표 계산 ───────────────────────────────────────
    final_eq     = capital + btc_qty * close[-1]
    total_ret    = (final_eq / INITIAL_CAPITAL - 1) * 100

    # 일간 수익률 (Sharpe)
    daily_eq     = equity[::96]   # 96봉 = 24시간
    daily_ret    = np.diff(daily_eq) / np.where(daily_eq[:-1] == 0, 1, daily_eq[:-1])
    valid_ret    = daily_ret[np.isfinite(daily_ret)]
    sharpe       = 0.0
    if len(valid_ret) > 10 and valid_ret.std() > 1e-9:
        sharpe   = float(valid_ret.mean() / valid_ret.std() * np.sqrt(365))

    # MDD
    roll_max     = np.maximum.accumulate(equity)
    mdd          = float(np.min((equity - roll_max) / np.where(roll_max == 0, 1, roll_max)) * 100)

    # 거래 통계
    sell_trades  = [t for t in trades if t["type"] != "BUY"]
    pnls         = [t["pnl"] for t in sell_trades]
    wins         = [p for p in pnls if p > 0]
    losses       = [p for p in pnls if p <= 0]
    n_trades     = sum(1 for t in trades if t["type"] == "BUY")
    win_rate     = len(wins) / len(pnls) * 100 if pnls else 0.0
    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    pf           = gross_profit / gross_loss if gross_loss > 0 else 0.0

    out = {
        "total_return_pct": round(total_ret, 2),
        "sharpe":           round(sharpe, 3),
        "mdd_pct":          round(mdd, 2),
        "n_trades":         n_trades,
        "win_rate":         round(win_rate, 1),
        "profit_factor":    round(pf, 3),
        "final_equity":     round(final_eq, 0),
        "params":           params,
    }
    if collect_pnls:
        out["pnls"] = pnls
    return out


# ══════════════════════════════════════════════════════════════
# ⑥ 결과 출력 헬퍼
# ══════════════════════════════════════════════════════════════
def print_result(rank: int, r: dict, label: str = "") -> None:
    p = r["params"]
    tag = f"#{rank}" if not label else label
    print(
        f"\n{tag}  "
        f"Sharpe={r['sharpe']:+.3f}  "
        f"수익률={r['total_return_pct']:+.1f}%  "
        f"MDD={r['mdd_pct']:.1f}%  "
        f"승률={r['win_rate']:.0f}%  "
        f"PF={r['profit_factor']:.2f}  "
        f"거래수={r['n_trades']}"
    )
    print(
        f"     ADX≥{p['adx_threshold']}  "
        f"H{p['highest_n']}  "
        f"ATR≥{p['atr_min_pct']*100:.2f}%  "
        f"RSI<{'∞' if p['rsi_max']>=9999 else p['rsi_max']}  "
        f"Vol×{'∞' if p['vol_mult']>=9999 else p['vol_mult']}  "
        f"SL={p['sl_mult']}  "
        f"TP1={p['tp1_mult']}×{int(p['tp1_pct']*100)}%  "
        f"TP2={p['tp2_mult']}  "
        f"Trail={p['trail_mult']}"
    )


# ══════════════════════════════════════════════════════════════
# ⑦ 메인 — 그리드 서치
# ══════════════════════════════════════════════════════════════
def main():
    print("=" * 64)
    print("  QuantBot V4.0  파라미터 최적화 백테스트")
    print(f"  시드: {INITIAL_CAPITAL:,}원   종목: {TICKER}")
    print("=" * 64)

    # 데이터 준비
    df_raw = fetch_data(months=12)
    df     = compute_indicators(df_raw)

    # NumPy 배열 사전 변환 (루프 내 .values 호출 제거 → 속도↑)
    arrays = {
        col: df[col].values
        for col in ["close", "high", "volume",
                    "ema20", "ema50", "adx",
                    "atr", "rsi", "vol_ma20",
                    "h40", "h60", "h80", "kst_hour"]
    }

    # 파라미터 조합 생성
    keys    = list(PARAM_GRID.keys())
    combos  = list(itertools.product(*PARAM_GRID.values()))
    total   = len(combos)
    print(f"\n🔍 총 {total:,}개 파라미터 조합 백테스트 시작")
    print(f"   데이터: {len(df):,}개 캔들\n")

    results   = []
    t0        = time.time()
    report_at = set(range(500, total, 500))

    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        r      = run_backtest(arrays, params)
        results.append(r)

        if idx in report_at or idx == total - 1:
            elapsed = time.time() - t0
            rate    = (idx + 1) / elapsed
            eta     = (total - idx - 1) / rate
            best    = max(results, key=lambda x: x["sharpe"])
            print(
                f"  [{idx+1:,}/{total:,}]  "
                f"경과 {elapsed:.0f}s  ETA {eta:.0f}s  "
                f"현재 최고 Sharpe={best['sharpe']:.3f}"
            )

    elapsed_total = time.time() - t0
    print(f"\n✅ 완료: {elapsed_total:.1f}초 ({elapsed_total/60:.1f}분)")

    # ── 현재 기본값 성과 ──────────────────────────────────────
    baseline_params = {
        "adx_threshold": 28, "highest_n": 40,  "atr_min_pct": 0.0015,
        "rsi_max": 9999,      "vol_mult": 9999, "sl_mult": 1.5,
        "tp1_mult": 2.0,      "tp1_pct": 0.30,  "tp2_mult": 8.0,
        "trail_mult": 0.5,    "time_filter": False,
    }
    baseline = run_backtest(arrays, baseline_params)

    # ── 거래 수 구간별 best ────────────────────────────────────
    # 데이터 기간이 약 12개월이므로:
    # 30+ ≈ 월 2.5회, 50+ ≈ 월 4회, 100+ ≈ 월 8회
    by_sharpe = sorted(results, key=lambda x: x["sharpe"],           reverse=True)
    by_return = sorted(results, key=lambda x: x["total_return_pct"], reverse=True)

    bucket_30  = [r for r in results if r["n_trades"] >= 30  and r["mdd_pct"] >= -50]
    bucket_50  = [r for r in results if r["n_trades"] >= 50  and r["mdd_pct"] >= -50]
    bucket_100 = [r for r in results if r["n_trades"] >= 100 and r["mdd_pct"] >= -50]

    bucket_30_top  = sorted(bucket_30,  key=lambda x: x["sharpe"], reverse=True)
    bucket_50_top  = sorted(bucket_50,  key=lambda x: x["sharpe"], reverse=True)
    bucket_100_top = sorted(bucket_100, key=lambda x: x["sharpe"], reverse=True)

    safe        = [r for r in results if r["n_trades"] >= 5 and r["mdd_pct"] >= -80]
    safe_sharpe = sorted(safe, key=lambda x: x["sharpe"], reverse=True)

    # ── 출력 ─────────────────────────────────────────────────
    sep = "\n" + "─" * 64

    print(sep)
    print("  📊 현재 봇 기본값 성과")
    print_result(0, baseline, label="[현재값]")

    print(sep)
    print(f"  🟢 거래 30+회 (월 2.5회+, n={len(bucket_30):,}) Sharpe TOP 10")
    for i, r in enumerate(bucket_30_top[:10], 1):
        print_result(i, r)

    print(sep)
    print(f"  🟡 거래 50+회 (월 4회+, n={len(bucket_50):,}) Sharpe TOP 10")
    for i, r in enumerate(bucket_50_top[:10], 1):
        print_result(i, r)

    print(sep)
    print(f"  🔵 거래 100+회 (월 8회+, n={len(bucket_100):,}) Sharpe TOP 5")
    for i, r in enumerate(bucket_100_top[:5], 1):
        print_result(i, r)

    print(sep)
    print("  🏆 단순 수익률 TOP 5 (필터 없음)")
    for i, r in enumerate(by_return[:5], 1):
        print_result(i, r)

    # ── 파라미터별 민감도 분석 ────────────────────────────────
    print(sep)
    print("  📈 파라미터별 평균 Sharpe (민감도 분석)")
    df_res = pd.DataFrame([
        {**r["params"], "sharpe": r["sharpe"], "ret": r["total_return_pct"]}
        for r in results
    ])
    for key in keys:
        group = df_res.groupby(key)["sharpe"].mean().sort_values(ascending=False)
        best_val = group.index[0]
        print(f"   {key:>18}: ", end="")
        for val, sh in group.items():
            marker = " ◀" if val == best_val else ""
            print(f"  {val}→{sh:.3f}{marker}", end="")
        print()

    # ── 결과 저장 ──────────────────────────────────────────────
    save_data = {
        "run_at":          datetime.now().isoformat(),
        "total_combos":    total,
        "elapsed_sec":     round(elapsed_total, 1),
        "data_candles":    len(df),
        "data_start":      str(df.index[0]),
        "data_end":        str(df.index[-1]),
        "baseline":        baseline,
        "bucket_30_top10":  bucket_30_top[:10],
        "bucket_50_top10":  bucket_50_top[:10],
        "bucket_100_top10": bucket_100_top[:10],
        "top10_overall":    safe_sharpe[:10],
        "top5_return":      by_return[:5],
    }
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n💾 전체 결과 저장 완료: {RESULT_FILE}")
    print("=" * 64)

    # ── 최종 추천 파라미터 출력 (거래수 30+ 기준 = 통계적 유의) ──
    print("\n" + "=" * 64)
    print("  🎯 추천 파라미터 (거래수 30+회 = 통계적 유의 구간 1위)")
    print("=" * 64)

    if bucket_30_top:
        best = bucket_30_top[0]
        p = best["params"]
        print(f"""
    # ── 진입 조건
    ADX_THRESHOLD  = {p['adx_threshold']}            # (기존: 28)
    HIGHEST_N      = {p['highest_n']}            # (기존: 40)
    ATR_MIN_PCT    = {p['atr_min_pct']}        # (기존: 0.0015)
    RSI_MAX        = {p['rsi_max'] if p['rsi_max'] < 9999 else 'None'}            # (기존: None)
    VOL_MULT       = {p['vol_mult'] if p['vol_mult'] < 9999 else 'None'}           # (기존: None)
    TIME_FILTER    = {p['time_filter']}        # KST 01~04시 차단 여부 (신규)
    # ── 포지션 관리
    SL_MULT        = {p['sl_mult']}           # (기존: 1.5)
    TP1_MULT       = {p['tp1_mult']}           # (기존: 2.0)
    TP1_PCT        = {p['tp1_pct']}          # (기존: 0.30)
    TP2_MULT       = {p['tp2_mult']}           # (기존: 8.0)
    TRAIL_MULT     = {p['trail_mult']}           # (기존: 0.5)

  성과: Sharpe={best['sharpe']:.3f}  수익률={best['total_return_pct']:+.1f}%  "
              f"MDD={best['mdd_pct']:.1f}%  승률={best['win_rate']:.0f}%  "
              f"PF={best['profit_factor']:.2f}  거래수={best['n_trades']}회/년
""")

    # ── Robustness 체크: TOP10 파라미터 분포 ──────────────────
    print("─" * 64)
    print("  🔬 Robustness 체크 (거래 30+ TOP 10의 파라미터 빈도)")
    print("─" * 64)
    if len(bucket_30_top) >= 10:
        top10 = bucket_30_top[:10]
        for key in keys:
            vals = [t["params"][key] for t in top10]
            counts = pd.Series(vals).value_counts().to_dict()
            counts_str = "  ".join([f"{k}×{v}" for k, v in counts.items()])
            print(f"   {key:>15}: {counts_str}")


if __name__ == "__main__":
    main()
