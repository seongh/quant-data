"""재무 지표 스냅샷 수집 (멀티팩터 전략용).

- 매월 1회 실행 (워크플로에서 매월 첫 영업일 가드)
- 저장: data/fundamentals/{YYYY-MM}.parquet — 그 시점의 팩터 지표 스냅샷
- 주의(문서화된 한계): 야후 재무 데이터는 과거 시점 조회가 얕아서,
  멀티팩터 백테스트는 "지금부터 쌓는 스냅샷"이 축적된 구간 + 제한된 과거만 검증 가능.
  (마스터플랜에서 후보4를 제한적 검증으로 명시한 이유)
"""
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
FUND_DIR = ROOT / "data" / "fundamentals"
UNIVERSE_FILE = ROOT / "data" / "universe" / "current_universe.csv"

FIELDS = [
    "returnOnEquity", "profitMargins", "grossMargins", "operatingMargins",
    "debtToEquity", "currentRatio", "earningsGrowth", "revenueGrowth",
    "trailingPE", "forwardPE", "priceToBook", "enterpriseToEbitda",
    "beta", "marketCap", "freeCashflow", "totalRevenue",
]


def main() -> None:
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    snap_name = date.today().strftime("%Y-%m")
    out = FUND_DIR / f"{snap_name}.parquet"
    if out.exists():
        print(f"{out.name} 이미 존재 — 이번 달 스냅샷 완료됨")
        return
    tickers = pd.read_csv(UNIVERSE_FILE)["ticker"].tolist()
    rows = []
    for i, t in enumerate(tickers):
        try:
            info = yf.Ticker(t).info
            row = {"ticker": t, "snapshot": snap_name}
            row.update({f: info.get(f) for f in FIELDS})
            rows.append(row)
        except Exception as e:
            print(f"[warn] {t}: {e}", file=sys.stderr)
        if i % 50 == 49:
            print(f"  {i+1}/{len(tickers)}")
            time.sleep(3)
    df = pd.DataFrame(rows)
    df.to_parquet(out, index=False)
    print(f"{out.name}: {len(df)} tickers, 필드 커버리지 {df[FIELDS].notna().mean().mean():.0%}")


if __name__ == "__main__":
    main()
