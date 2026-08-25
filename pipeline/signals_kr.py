"""한국 슬리브(K조합+변동성타게팅)의 오늘자 목표 비중 산출.

조합: K1 자산배분(ETF 6종 블렌디드 모멘텀 top3, 역변동성, 절대모멘텀→단기채) 50%
    + K2 종목모멘텀(시총상위 종목 12-1 모멘텀 top20, 역변동성, KODEX200 200MA 레짐) 50%
오버레이: 변동성 타게팅 연 10% (현 목표비중을 과거 63일에 적용한 실현변동성 기준, 축소만)
검증: OOS(2021~2026) 샤프 0.92, MDD -14.0% (07_한국확장_백테스트)
출력: signals/kr_target_weights.json
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
KRX_DIR = ROOT / "data" / "krx"
OUT = ROOT / "signals" / "kr_target_weights.json"

ETF = ["069500", "229200", "132030", "148070", "153130", "133690"]
CASH = "153130"   # 단기채 = 현금성
KODEX = "069500"
VT_TARGET = 0.10


def load_close():
    files = sorted(KRX_DIR.glob("*.parquet"))
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    names = df.groupby("ticker")["name"].last().to_dict()
    close = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    return close, names


def blended_mom(sub, lookbacks=(21, 63, 126, 252)):
    moms = [sub.pct_change(lb, fill_method=None).iloc[-1] for lb in lookbacks]
    return pd.concat(moms, axis=1).mean(axis=1)


def k1_allocation(close, top_n=3):
    sub = close[ETF]
    m = blended_mom(sub).dropna()
    vol = sub.pct_change(fill_method=None).rolling(63).std().iloc[-1]
    cm = m.get(CASH, 0.0)
    cm = 0.0 if pd.isna(cm) else cm
    top = m.drop(CASH, errors="ignore").sort_values(ascending=False).head(top_n)
    iv = (1.0 / vol[top.index].replace(0, np.nan)).fillna(1.0)
    base = iv / iv.sum()
    w = {}
    for a, bw in base.items():
        tgt = a if m[a] > cm else CASH
        w[tgt] = w.get(tgt, 0.0) + float(bw)
    return w


def k2_momentum(close, top_n=20):
    stocks = [c for c in close.columns if c not in ETF]
    sub = close[stocks]
    kodex = close[KODEX].dropna()
    if kodex.iloc[-1] < kodex.rolling(200).mean().iloc[-1]:
        return {}  # 약세 레짐 → 전량 현금
    mom = sub.shift(21).pct_change(231, fill_method=None).iloc[-1].dropna()
    vol = sub.pct_change(fill_method=None).rolling(63).std().iloc[-1]
    top = mom.sort_values(ascending=False).head(top_n)
    if not len(top):
        return {}
    iv = 1.0 / vol[top.index].replace(0, np.nan)
    iv = iv.fillna(iv.mean() if iv.notna().any() else 1.0)
    return (iv / iv.sum()).clip(upper=0.10).to_dict()


def main():
    close, names = load_close()
    d = close.index[-1]
    w1, w2 = k1_allocation(close), k2_momentum(close)
    total: dict[str, float] = {}
    for ws, mix in ((w1, 0.5), (w2, 0.5)):
        for t, w in ws.items():
            total[t] = total.get(t, 0.0) + mix * w
    ssum = sum(total.values())
    if ssum < 0.999:  # K2 레짐 오프 등 미충족분은 현금성
        total[CASH] = total.get(CASH, 0.0) + (1.0 - ssum)

    # 변동성 타게팅: 현 목표비중을 과거 63일 수익률에 적용한 실현변동성으로 스케일 (축소만)
    rets = close.pct_change(fill_method=None).iloc[-63:]
    wvec = pd.Series(total).reindex(close.columns).fillna(0.0)
    port = (rets * wvec).sum(axis=1, min_count=1).dropna()
    rv = float(port.std() * np.sqrt(252)) if len(port) > 20 else VT_TARGET
    scale = min(1.0, VT_TARGET / rv) if rv > 0 else 1.0
    scaled = {t: w * scale for t, w in total.items()}
    scaled[CASH] = scaled.get(CASH, 0.0) + (1.0 - sum(scaled.values()))  # 잔여 → 현금성

    s = sum(scaled.values())
    final = {t: round(w / s, 6) for t, w in scaled.items() if w / s > 0.001}
    excess = round(sum(final.values()) - 1.0, 6)
    if excess > 0:  # 반올림 오차로 합>1이면 현금성에서 차감 (헌법 1조 보장)
        big = CASH if CASH in final else max(final, key=final.get)
        final[big] = round(final[big] - excess, 6)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "date": str(d.date()), "vt_scale": round(scale, 3),
        "weights": final,
        "names": {t: names.get(t, t) for t in final},
    }, ensure_ascii=False, indent=1))
    print(f"{d.date()} KR 목표비중 {len(final)}종목, VT스케일 {scale:.2f}, 합계 {sum(final.values()):.4f}")


if __name__ == "__main__":
    main()
