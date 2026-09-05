"""일봉 가격 수집 (yfinance, 수정주가).

- 저장 구조: data/prices/{YEAR}.parquet — 해당 연도 전체 티커의 일봉
  (매일 갱신 시 올해 파일만 다시 쓰므로 저장소 증가량이 작음)
- 최초 실행(백필): START_YEAR부터 전체 다운로드 (Actions에서 1회, ~10분)
- 이후 실행(증분): 마지막 저장일 이후만 받아 병합
- 컬럼: date, ticker, open, high, low, close, volume  (auto_adjust=True → 배당·분할 반영)

[패치 15 — 2026-09-05] 완전성 가드 추가:
- 장중 가드: 미국 정규장 마감+정산 여유(21:00 UTC) 전에는 당일 행을 저장하지 않음
  (9/3 사고 원인: 장중 push 트리거 실행이 09:55 ET 스냅샷을 종가 자리에 고착시킴)
- 거래량 가드: 최신일 SPY 거래량 < MIN_SPY_VOLUME 이면 해당 일자 전체를 저장 보류
  (부분/장중 데이터가 파일에 들어가는 것을 원천 차단 — 다음 실행이 정상분으로 재수집)
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
MIN_SPY_VOLUME = 20_000_000  # 정규 종가 완전성 하한 (SPY 평소 일 30~40M주)
SESSION_CLOSE_UTC_HOUR = 21  # 미국 정규장 마감(20:00 UTC, DST) + 정산 여유 1시간


def drop_incomplete_latest(df: pd.DataFrame) -> pd.DataFrame:
    """장중/부분 데이터가 파일에 저장되는 것을 차단 (패치 15)."""
    if df.empty:
        return df
    # 1) 장중 가드: 오늘 세션이 아직 안 끝났으면 오늘 날짜 행은 저장하지 않음
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    cutoff = now.normalize() + (
        pd.Timedelta(days=1) if now.hour >= SESSION_CLOSE_UTC_HOUR else pd.Timedelta(0)
    )
    dropped = df[df["date"] >= cutoff]
    if not dropped.empty:
        print(f"[guard] 장중 가드: {sorted(dropped['date'].dt.date.unique())} "
              f"{len(dropped)}행 저장 보류 (세션 미종료)")
        df = df[df["date"] < cutoff]
    # 2) 거래량 가드: SPY 거래량이 하한 미달인 날짜는 전체 보류
    #    (SPY가 다운로드에 포함된 경우에만 — 신규 티커 백필 등 SPY 없는 호출은 그대로 통과)
    if "SPY" in set(df["ticker"]):
        spy = df[df["ticker"] == "SPY"].set_index("date")["volume"]
        bad_dates = sorted(spy[spy < MIN_SPY_VOLUME].index)
        if bad_dates:
            for d in bad_dates:
                print(f"[guard] 거래량 가드: {d.date()} SPY 거래량 {spy[d]:,.0f} < "
                      f"{MIN_SPY_VOLUME:,} → 해당 일자 저장 보류 (장중/부분 데이터 의심)")
            df = df[~df["date"].isin(bad_dates)]
    return df


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
    new = drop_incomplete_latest(new)  # 패치 15: 장중/부분 데이터 차단
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
