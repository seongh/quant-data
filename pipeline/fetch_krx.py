"""한국(KRX) 일봉 수집 — FinanceDataReader(네이버 금융 기반, 로그인 불필요).

- 배경: KRX 정보데이터시스템이 로그인 필수로 변경되어 pykrx 사용 불가 → fdr로 교체 (2026-08-23)
- 유니버스: KOSPI 시가총액 상위 200 (KOSPI200 근사) + 자산배분용 국내 ETF
  (시점별 구성 히스토리 미확보 → 생존편향 잔존, 문서화된 한계. 수정주가 사용)
- 저장: data/krx/{YEAR}.parquet  (date, ticker, name, open, high, low, close, volume)
"""
import sys
import time
from datetime import date
from pathlib import Path

import FinanceDataReader as fdr
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
KRX_DIR = ROOT / "data" / "krx"

START = "2005-01-01"
TOP_N = 200
ETFS = {
    "069500": "KODEX200",
    "229200": "KODEX코스닥150",
    "132030": "KODEX골드선물",
    "148070": "KOSEF국고채10년",
    "153130": "KODEX단기채권",
    "133690": "TIGER나스닥100",
}


def get_universe() -> dict[str, str]:
    """KOSPI 시총 상위 TOP_N + ETF. 리스팅 소스는 순차 폴백."""
    listing = None
    for market in ("KOSPI", "KRX"):
        try:
            df = fdr.StockListing(market)
            if df is not None and len(df) > 100:
                listing = df
                break
        except Exception as e:
            print(f"[warn] StockListing({market}) 실패: {str(e)[:80]}", file=sys.stderr)
    if listing is None:
        raise RuntimeError("종목 리스팅 소스 전부 실패")
    cols = {c.lower(): c for c in listing.columns}
    code_col = cols.get("code") or cols.get("symbol")
    name_col = cols.get("name")
    cap_col = cols.get("marcap")
    if cap_col:
        listing = listing.sort_values(cap_col, ascending=False)
    top = listing.head(TOP_N)
    uni = {str(r[code_col]).zfill(6): str(r[name_col]) for _, r in top.iterrows()}
    uni.update(ETFS)
    return uni


def last_stored_date():
    files = sorted(KRX_DIR.glob("*.parquet"))
    if not files:
        return None
    df = pd.read_parquet(files[-1], columns=["date"])
    return pd.to_datetime(df["date"]).max()


def stored_tickers() -> set:
    files = sorted(KRX_DIR.glob("*.parquet"))
    if not files:
        return set()
    return set(pd.read_parquet(files[-1], columns=["ticker"])["ticker"].unique())


def fetch_one(ticker: str, name: str, start: str) -> pd.DataFrame | None:
    for attempt in range(2):
        try:
            raw = fdr.DataReader(ticker, start)
            if raw is None or raw.empty:
                return None
            df = raw.reset_index().rename(columns={
                "Date": "date", "Open": "open", "High": "high",
                "Low": "low", "Close": "close", "Volume": "volume"})
            keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
            df = df[keep].copy()
            df["ticker"] = ticker
            df["name"] = name
            return df[df["close"] > 0]
        except Exception as e:
            print(f"[warn] {ticker} {name} 시도{attempt+1}: {str(e)[:80]}", file=sys.stderr)
            time.sleep(3)
    return None


def merge_and_save(new: pd.DataFrame) -> None:
    if new is None or new.empty:
        print("신규 데이터 없음")
        return
    new["date"] = pd.to_datetime(new["date"])
    KRX_DIR.mkdir(parents=True, exist_ok=True)
    for year, g in new.groupby(new["date"].dt.year):
        path = KRX_DIR / f"{year}.parquet"
        if path.exists():
            old = pd.read_parquet(path)
            g = pd.concat([old, g], ignore_index=True).drop_duplicates(
                subset=["date", "ticker"], keep="last")
        g.sort_values(["date", "ticker"]).reset_index(drop=True).to_parquet(path, index=False)
        print(f"krx/{path.name}: {len(g):,} rows")


def main() -> None:
    uni = get_universe()
    print(f"KRX 유니버스: {len(uni)}개 (KOSPI 시총상위 {TOP_N} + ETF {len(ETFS)})")
    last = last_stored_date()
    known = stored_tickers()
    frames, fail = [], 0
    for i, (t, name) in enumerate(uni.items()):
        start = START if (last is None or t not in known) else (
            (last - pd.Timedelta(days=5)).strftime("%Y-%m-%d"))
        df = fetch_one(t, name, start)
        if df is not None:
            frames.append(df)
        else:
            fail += 1
        if i % 20 == 19:
            print(f"  {i+1}/{len(uni)} (실패 {fail})")
        time.sleep(0.3)
    merge_and_save(pd.concat(frames, ignore_index=True) if frames else pd.DataFrame())
    if fail > len(uni) * 0.2:
        sys.exit(f"실패 비율 과다: {fail}/{len(uni)}")


if __name__ == "__main__":
    main()
