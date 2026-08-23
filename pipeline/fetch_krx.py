"""한국(KRX) 일봉 수집 — pykrx 기반.

- 유니버스: KOSPI200 현재 구성종목 + 자산배분용 국내 ETF
  (KOSPI200 시점별 히스토리는 미확보 → 생존편향 잔존, 문서화된 한계.
   미국과 동일하게 수정주가(adjusted) 사용)
- 저장: data/krx/{YEAR}.parquet  (date, ticker, name, open, high, low, close, volume)
- 최초 실행: START부터 전체 백필 (~20분), 이후 증분
"""
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
from pykrx import stock

ROOT = Path(__file__).resolve().parent.parent
KRX_DIR = ROOT / "data" / "krx"

START = "20050101"
KOSPI200_INDEX = "1028"
# 자산배분용 ETF (상장일이 짧은 것은 있는 구간만 수집)
ETFS = {
    "069500": "KODEX200",
    "229200": "KODEX코스닥150",
    "132030": "KODEX골드선물",
    "148070": "KOSEF국고채10년",
    "153130": "KODEX단기채권",
    "133690": "TIGER나스닥100",
}


def get_universe() -> dict[str, str]:
    tickers = stock.get_index_portfolio_deposit_file(KOSPI200_INDEX)
    uni = {}
    for t in tickers:
        try:
            uni[t] = stock.get_market_ticker_name(t)
        except Exception:
            uni[t] = t
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


def fetch_one(ticker: str, name: str, start: str, end: str) -> pd.DataFrame | None:
    for fn in (stock.get_market_ohlcv, getattr(stock, "get_etf_ohlcv_by_date", None)):
        if fn is None:
            continue
        try:
            if fn is stock.get_market_ohlcv:
                raw = fn(start, end, ticker, adjusted=True)
            else:
                raw = fn(start, end, ticker)
            if raw is None or raw.empty:
                continue
            raw = raw.reset_index()
            ren = {"날짜": "date", "시가": "open", "고가": "high", "저가": "low",
                   "종가": "close", "거래량": "volume"}
            raw = raw.rename(columns=ren)
            keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in raw.columns]
            df = raw[keep].copy()
            df["ticker"] = ticker
            df["name"] = name
            df = df[df["close"] > 0]
            return df
        except Exception as e:
            print(f"[warn] {ticker} {name}: {type(e).__name__} {str(e)[:80]}", file=sys.stderr)
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
    print(f"KRX 유니버스: {len(uni)}개 (KOSPI200 + ETF {len(ETFS)})")
    last = last_stored_date()
    end = date.today().strftime("%Y%m%d")
    known = stored_tickers()
    frames = []
    for i, (t, name) in enumerate(uni.items()):
        # 신규 티커는 전체 백필, 기존 티커는 증분
        if last is None or t not in known:
            start = START
        else:
            start = (last - pd.Timedelta(days=5)).strftime("%Y%m%d")
        df = fetch_one(t, name, start, end)
        if df is not None:
            frames.append(df)
        if i % 20 == 19:
            print(f"  {i+1}/{len(uni)}")
        time.sleep(0.4)  # KRX 서버 예의
    merge_and_save(pd.concat(frames, ignore_index=True) if frames else pd.DataFrame())


if __name__ == "__main__":
    main()
