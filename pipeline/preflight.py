"""집행 전 비행검사(Pre-flight) — 신호를 집행기와 독립된 코드로 검증.

하나라도 실패하면 exit 1 → 워크플로의 집행 단계가 시작되지 않음 (계층 1 인터록).
사용: python pipeline/preflight.py us|kr
검사: 비중합=1.0 / 음수 없음 / 개별종목≤10% / 종목이 당일 데이터에 존재 /
      신호일=데이터 최신일 / **신호일이 현지 달력 기준 직전 거래일보다 오래되지 않음** (사고 #8 봉합:
      수집이 멈추면 신호도 멈추고 집행도 멈춘다. 예외: PREFLIGHT_MAX_STALE_TDAYS=N) /
      비현금 매수 회전율 ≤30% (직전 커밋 신호 대비 — 인공물성 대량 교체 감지.
      현금성으로의 이동(위험 축소)은 무제한 허용)
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
US_ETFS = {"SPY","QQQ","IWM","EFA","EEM","VNQ","GLD","DBC","TLT","IEF","SHY","TIP","LQD","HYG","BIL",
           "XLB","XLE","XLF","XLI","XLK","XLP","XLU","XLV","XLY"}
KR_ETFS = {"069500","229200","132030","148070","153130","133690"}
CFG = {
    "us": {"sig": "signals/target_weights.json", "data": "data/prices", "etfs": US_ETFS, "cash": {"BIL"}},
    "kr": {"sig": "signals/kr_target_weights.json", "data": "data/krx", "etfs": KR_ETFS, "cash": {"153130"}},
}
MAX_STOCK_W = 0.10
MAX_BUY_TURNOVER = 0.30

# 휴장일 (2026 하반기~2027 초). 누락 시 오탐은 "집행 1일 스킵"이라 안전 방향.
HOLIDAYS = {
    "kr": {"2026-08-15", "2026-08-17", "2026-09-24", "2026-09-25", "2026-10-03", "2026-10-05",
           "2026-10-09", "2026-12-25", "2026-12-31", "2027-01-01"},
    "us": {"2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18"},
}
TZ = {"kr": ZoneInfo("Asia/Seoul"), "us": ZoneInfo("America/New_York")}


def is_trading_day(mkt, d: date) -> bool:
    return d.weekday() < 5 and str(d) not in HOLIDAYS[mkt]


def prev_trading_day(mkt, today: date) -> date:
    d = today - timedelta(days=1)
    while not is_trading_day(mkt, d):
        d -= timedelta(days=1)
    return d


def trading_days_between(mkt, a: date, b: date) -> int:
    """a < b 구간의 거래일 수 (a 제외, b 포함)."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += is_trading_day(mkt, d)
    return n


def check_freshness(mkt, sig_date: str, today: date | None = None) -> str | None:
    """신호일이 현지 달력 기준 직전 거래일보다 오래되면 실패 메시지, 아니면 None."""
    today = today or datetime.now(TZ[mkt]).date()
    ref = prev_trading_day(mkt, today)
    sd = date.fromisoformat(sig_date)
    max_stale = int(os.environ.get("PREFLIGHT_MAX_STALE_TDAYS", "0"))
    stale = trading_days_between(mkt, sd, ref)  # 신호일 이후 ref까지 거래일 수 (정상=0)
    if stale > max_stale:
        return (f"신호일 {sd}이 직전 거래일 {ref}보다 오래됨 ({stale}거래일 경과, 오늘 {today} {mkt.upper()} 현지)"
                f" — 수집 중단 의심, 묵은 신호 집행 차단")
    return None


def fail(msg):
    sys.exit(f"[PREFLIGHT FAIL] {msg}")


def main():
    mkt = sys.argv[1] if len(sys.argv) > 1 else "us"
    c = CFG[mkt]
    sig = json.loads((ROOT / c["sig"]).read_text())
    w = sig["weights"]

    s = sum(w.values())
    if not (0.995 <= s <= 1.0 + 1e-9):
        fail(f"비중합 {s!r} — 1.0이 아님")
    if any(v < 0 for v in w.values()):
        fail("음수 비중 존재")
    for t, v in w.items():
        if t not in c["etfs"] and v > MAX_STOCK_W + 1e-9:
            fail(f"헌법 4조: {t} {v:.2%} > 10%")

    files = sorted((ROOT / c["data"]).glob("*.parquet"))
    if not files:
        fail("가격 데이터 없음")
    df = pd.read_parquet(files[-1])
    last = str(pd.to_datetime(df["date"]).max().date())
    if sig.get("date") != last:
        fail(f"신호일 {sig.get('date')} != 데이터 최신일 {last} (신선도 불량)")
    msg = check_freshness(mkt, sig["date"])   # 외부 기준(달력) 대비 — 데이터·신호가 함께 멈춘 경우 검출
    if msg:
        fail(msg)
    tickers = set(df["ticker"].unique())
    missing = [t for t in w if t not in tickers]
    if missing:
        fail(f"당일 데이터에 없는 종목: {missing[:5]}")

    # 회전율 급변 감지 — 직전 커밋(HEAD) 신호 대비, 현금성 매수는 제외
    prev = None
    try:
        out = subprocess.run(["git", "show", f"HEAD:{c['sig']}"],
                             capture_output=True, text=True, check=True, cwd=ROOT)
        prev = json.loads(out.stdout)["weights"]
    except Exception:
        print("[preflight] 직전 커밋 신호 없음 — 회전율 검사 생략")
    if prev is not None:
        buy_turn = sum(max(0.0, w.get(t, 0.0) - prev.get(t, 0.0))
                       for t in set(w) | set(prev) if t not in c["cash"])
        if buy_turn > MAX_BUY_TURNOVER:
            fail(f"비현금 매수 회전율 {buy_turn:.1%} > {MAX_BUY_TURNOVER:.0%} — 신호 급변(인공물 의심), 집행 차단")
        print(f"[preflight] 매수 회전율 {buy_turn:.1%} OK")

    print(f"[preflight] PASS — {mkt} {sig['date']} {len(w)}종목, 합계 {s:.6f}")


if __name__ == "__main__":
    main()
