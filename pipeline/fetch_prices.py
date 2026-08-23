"""일봉 가격 수집 (yfinance, 수정주가).

- 저장 구조: data/prices/{YEAR}.parquet — 해당 연도 전체 티커의 일봉
  (매일 갱신 시 올해 파일만 다시 쓰므로 저장소 증가량이 작음)
- 최초 실행(백필): START_YEAR부터 전체 다운로드 (Actions에서 1회, ~10분)
- 이후 실행(증분): 마지막 저장일 이후만 받아 병합
- 컬럼: date, ticker, open, high, low, close, volume  (auto_adjust=True → 배당·분할 반영)
"""
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
PRICES_DIR = ROOT / "data" / "prices"
UNIVERSE_FILE = ROOT / "data" / "universe" / "current_universe.csv"

START_YEAR = 2005
BATCH = 100  # 티커 배치 크기 (야후 요청 안정성)


def load_universe() -> list[str]:
    return pd.read_csv(UNIVERSE_FILE)["ticker"].tolist()


def last_stored_date() -> pd.Timestamp | None:
    files = sorted(PRICES_DIR.glob("*.parquet"))
    if not files:
        return None
    df = pd.read_parquet(files[-1], columns=["date"])
    return pd.to_datetime(df["date"]).max()


def download(tickers: list[str], start: str) -> pd.DataFrame:
    """배치 다운로드 → long format 정규화."""
    frames = []
    for i in range(0, len(tickers), BATCH):
        batch = tickers[i : i + BATCH]
        for attempt in range(3):
            try:
                raw = yf.download(
                    batch, start=start, auto_adjust=True,
                    group_by="ticker", progress=False, threads=True,
                )
                break
            except Exception as e:
                print(f"[warn] batch {i} attempt {attempt+1} 실패: {e}", file=sys.stderr)
                time.sleep(15 * (attempt + 1))
        else:
            continue
        if raw is None or raw.empty:
            continue
        if not isinstance(raw.columns, pd.MultiIndex):  # 단일 티커 케이스
            raw = pd.concat({batch[0]: raw}, axis=1)
        long = (
            raw.stack(level=0, future_stack=True)
            .rename_axis(["date", "ticker"])
            .reset_index()
        )
        long.columns = [str(c).lower() for c in long.columns]
        frames.append(long[["date", "ticker", "open", "high", "low", "close", "volume"]])
        time.sleep(2)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).dropna(subset=["close"])
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df


def merge_and_save(new: pd.DataFrame) -> None:
    if new.empty:
        print("신규 데이터 없음")
        return
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    for year, g in new.groupby(new["date"].dt.year):
        path = PRICES_DIR / f"{year}.parquet"
        if path.exists():
            old = pd.read_parquet(path)
            g = (
                pd.concat([old, g], ignore_index=True)
                .drop_duplicates(subset=["date", "ticker"], keep="last")
            )
        g.sort_values(["date", "ticker"]).reset_index(drop=True).to_parquet(path, index=False)
        print(f"{path.name}: {len(g):,} rows")


def stored_tickers() -> set[str]:
    """이미 데이터가 있는 티커 집합 (신규 편입 티커 감지용)."""
    files = sorted(PRICES_DIR.glob("*.parquet"))
    if not files:
        return set()
    return set(pd.read_parquet(files[-1], columns=["ticker"])["ticker"].unique())


def main() -> None:
    tickers = load_universe()
    last = last_stored_date()
    if last is None:
        start = f"{START_YEAR}-01-01"
        print(f"백필 모드: {start}부터 {len(tickers)}개 티커")
        merge_and_save(download(tickers, start))
        return
    # 유니버스에 새로 들어온 티커(ETF 추가, 지수 편입 등)는 전체 히스토리 백필
    new_tickers = sorted(set(tickers) - stored_tickers())
    if new_tickers:
        print(f"신규 티커 {len(new_tickers)}개 전체 백필: {new_tickers[:10]}")
        merge_and_save(download(new_tickers, f"{START_YEAR}-01-01"))
    start = (last - pd.Timedelta(days=5)).strftime("%Y-%m-%d")  # 겹침 구간은 dedup
    print(f"증분 모드: {start}부터 (마지막 저장일 {last.date()})")
    merge_and_save(download([t for t in tickers if t not in new_tickers], start))


if __name__ == "__main__":
    main()
