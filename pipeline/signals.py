"""F1 조합의 오늘자 목표 비중 산출 (백테스트 검증 로직의 현시점 버전).

조합: AAA 자산배분 45% + 앙상블 듀얼모멘텀 20% + 채권 로테이션 25% + 평균회귀 10%
출력: signals/target_weights.json  {"date": ..., "weights": {티커: 비중}, "sleeves": {...}}
검증 근거: OOS(2020~2026) 샤프 0.75, MDD -14.8% (프로젝트 05_백테스트_최종보고)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision as D  # 위원회 결정 브리지 단일 소스 (2026-09-28, US·KR 공용)
PRICES_DIR = ROOT / "data" / "prices"
OUT = ROOT / "signals" / "target_weights.json"

ASSETS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "GLD", "DBC",
          "TLT", "IEF", "LQD", "HYG", "TIP"]
BONDS = ["TLT", "IEF", "SHY", "TIP", "LQD", "HYG"]
DM_RISK = ["SPY", "EFA", "EEM"]
DM_SAFE = ["TLT", "IEF", "BIL"]


def load_close() -> pd.DataFrame:
    files = sorted(PRICES_DIR.glob("*.parquet"))[-3:]  # 최근 3개년이면 룩백 충분
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")


def blended_mom(close, tickers, lookbacks=(21, 63, 126, 252)):
    d = close.index[-1]
    out = {}
    for t in tickers:
        vals = []
        for lb in lookbacks:
            s = close[t].dropna()
            if len(s) > lb:
                vals.append(s.iloc[-1] / s.iloc[-1 - lb] - 1)
        if vals:
            out[t] = float(np.mean(vals))
    return pd.Series(out)


def aaa_weights(close, top_n=5):
    m = blended_mom(close, ASSETS)
    cash_m = blended_mom(close, ["BIL"]).get("BIL", 0.0)
    vol = close[ASSETS].pct_change(fill_method=None).rolling(63).std().iloc[-1]
    top = m.sort_values(ascending=False).head(top_n)
    iv = (1.0 / vol[top.index].replace(0, np.nan)).fillna(1.0)
    base = iv / iv.sum()
    safe_m = blended_mom(close, ["IEF"]).get("IEF", -1)
    fallback = "IEF" if safe_m > cash_m else "BIL"
    w = {}
    for a, bw in base.items():
        tgt = a if m[a] > cash_m else fallback
        w[tgt] = w.get(tgt, 0.0) + float(bw)
    return w


def dual_momentum_weights(close, lookback):
    d = close.index[-1]
    def mom(t):
        s = close[t].dropna()
        return s.iloc[-1] / s.iloc[-1 - lookback] - 1 if len(s) > lookback else np.nan
    cash_m = mom("BIL")
    cash_m = 0.0 if pd.isna(cash_m) else cash_m
    risk = pd.Series({t: mom(t) for t in DM_RISK}).dropna().sort_values(ascending=False)
    if len(risk) and risk.iloc[0] > cash_m:
        return {risk.index[0]: 1.0}
    safe = pd.Series({t: mom(t) for t in DM_SAFE}).dropna().sort_values(ascending=False)
    return {safe.index[0]: 1.0} if len(safe) else {}


def ensemble_dm_weights(close):
    total = {}
    for lb in (63, 126, 189, 252):
        for t, w in dual_momentum_weights(close, lb).items():
            total[t] = total.get(t, 0.0) + w / 4
    return total


def bond_weights(close, top_n=2):
    m = blended_mom(close, BONDS, lookbacks=(63, 126, 252))
    cash_m = blended_mom(close, ["BIL"], lookbacks=(63, 126, 252)).get("BIL", 0.0)
    top = m.sort_values(ascending=False).head(top_n)
    w = {}
    for b in top.index:
        tgt = b if top[b] > cash_m else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 1.0 / top_n
    return w


def mean_reversion_weights(close, n_slots=10):
    """보유상태 없는 근사: 오늘 신호 기준 진입 후보만 (RSI2<5, 200MA 위, 유동성 상위)."""
    spy = close["SPY"].dropna()
    if spy.iloc[-1] < spy.rolling(200).mean().iloc[-1]:
        return {}
    etfs = set(ASSETS + BONDS + ["BIL", "XLB","XLE","XLF","XLI","XLK","XLP","XLU","XLV","XLY"])
    stocks = [c for c in close.columns if c not in etfs]
    sub = close[stocks]
    delta = sub.diff()
    up = delta.clip(lower=0).rolling(2).mean().iloc[-1]
    dn = (-delta.clip(upper=0)).rolling(2).mean().iloc[-1]
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    ma200 = sub.rolling(200).mean().iloc[-1]
    px = sub.iloc[-1]
    cand = rsi[(rsi < 5) & (px > ma200)].dropna().sort_values().head(n_slots)
    return {t: 1.0 / n_slots for t in cand.index}


def load_decision():
    """호환 래퍼 — 판정은 decision.py 가 한다 (C-21 fail-closed, R-26 ISO 검증, override cap).

    반환: (유효한 결정 dict 또는 None, 소스 라벨, 상태). 상태가 'valid' 가 아니면 이 신호로는
    집행하지 않는다 — preflight·execute 가 decision_state 를 보고 독립적으로 정지한다.
    (구버전 docstring 은 만료 시 F1 복귀를 'fail-safe' 라 불렀으나 실제로는 fail-open 이었다: 결함 L)"""
    dec, state, note = D.load_decision("us", ROOT / "decisions")
    return dec, D.source_label(state, note, "F1 원신호"), state


def main():
    close = load_close()
    d = close.index[-1]
    dec, source, dstate = load_decision()
    # MR 슬리브 스칼라 (2026-08-28 위원회 스키마 확장 → 2026-09-02 구현):
    # approved.json 최상위 "mr_scale" (0.0~1.0). 0.0 = 결함 C 동결(#2026-0826-01) —
    # MR 신규 진입·교체 차단. 비운 비중은 아래 "MR 슬리브 미충족분은 현금(BIL)"이 자동 흡수.
    # "mr_policy": "hold"(기존 MR 보유 유지, 집행기가 매도 생략) | "liquidate"(목표 0% → 청산)
    mr_scale, mr_policy = 1.0, "liquidate"
    if dec is not None:
        try:
            mr_scale = min(1.0, max(0.0, float(dec.get("mr_scale", 1.0))))
        except (TypeError, ValueError):
            print("[warn] mr_scale 해석 불가 → 1.0 사용")
        mr_policy = "hold" if str(dec.get("mr_policy", "liquidate")).lower() == "hold" else "liquidate"
    mr_raw = mean_reversion_weights(close)
    sleeves = {
        "AAA_45": aaa_weights(close),
        "DUAL_20": ensemble_dm_weights(close),
        "BOND_25": bond_weights(close),
        "MR_10": {t: w * mr_scale for t, w in mr_raw.items()},
    }
    mix = {"AAA_45": 0.45, "DUAL_20": 0.20, "BOND_25": 0.25, "MR_10": 0.10}
    total: dict[str, float] = {}
    for k, ws in sleeves.items():
        for t, w in ws.items():
            total[t] = total.get(t, 0.0) + mix[k] * w
    # MR 슬리브 미충족분은 현금(BIL)
    ssum = sum(total.values())
    if ssum < 0.999:
        total["BIL"] = total.get("BIL", 0.0) + (1.0 - ssum)
    # 동일기업 다중클래스 통합 (시범회의 개선과제 반영)
    for dup, keep in [("GOOG", "GOOGL"), ("BRK-A", "BRK-B")]:
        if dup in total:
            total[keep] = total.get(keep, 0.0) + total.pop(dup)
    # 정규화 (합계 100% 보장 — 헌법 1조)
    s = sum(total.values())
    total = {t: round(w / s, 6) for t, w in total.items() if w / s > 0.001}
    excess = round(sum(total.values()) - 1.0, 6)
    if excess > 0:  # 반올림 오차로 합>1이면 현금성(BIL)에서 차감 (2026-08-26 결함 A 수정)
        big = "BIL" if "BIL" in total else max(total, key=total.get)
        total[big] = round(total[big] - excess, 6)

    # 위원회 결정 브리지 (2026-08-28 → 2026-09-28 개정): 유효한 결정이면 overrides 적용.
    # 기본 의미론은 cap(리스크 축소 방향만). 결정이 valid 가 아니면 비중은 기록용 F1 원신호이며
    # decision_state 가 집행을 막는다 (만료 = 정지, C-21).
    if dec is not None:
        total = D.apply_overrides(total, dec, "BIL")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"date": str(d.date()), "source": source, "decision_state": dstate,
                               "weights": total,
                               "mr_scale": mr_scale, "mr_policy": mr_policy,
                               "mr_raw": {t: round(w, 4) for t, w in mr_raw.items()},  # 스케일 전 MR 원신호 (기록용)
                               "sleeves": {k: {t: round(w, 4) for t, w in v.items()} for k, v in sleeves.items()}},
                              ensure_ascii=False, indent=1))
    print(f"{d.date()} 목표비중 {len(total)}종목, 합계 {sum(total.values()):.4f}, 소스: {source}, "
          f"mr_scale={mr_scale} ({mr_policy}), 결정상태={dstate} -> {OUT}")


if __name__ == "__main__":
    main()
