"""유니버스 정의: S&P500(시점별) + NASDAQ100(현재).

- S&P500: data/universe/sp500_pit_constituents.csv (1996~, point-in-time)
  -> 생존 편향 없는 백테스트용 멤버십 조회 제공
- NASDAQ100: 위키피디아에서 현재 구성종목 수집 (히스토리 없음 -> 문서화된 한계)
- 출력: data/universe/current_universe.csv (오늘 기준 수집 대상 티커 전체)
"""
import io
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_DIR = ROOT / "data" / "universe"
PIT_FILE = UNIVERSE_DIR / "sp500_pit_constituents.csv"

WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"
HEADERS = {"User-Agent": "Mozilla/5.0 (quant-pipeline; personal research)"}

# 전략·벤치마크용 ETF (자산배분·섹터로테이션·벤치마크·레짐 필터)
ETFS = [
    # 광역 자산군
    "SPY", "QQQ", "IWM", "EFA", "EEM",          # 주식 (미국 대/소형, 선진, 신흥)
    "TLT", "IEF", "SHY", "TIP", "LQD", "HYG",   # 채권 (장기/중기/단기/물가/회사채/하이일드)
    "GLD", "DBC",                                # 금, 원자재
    "VNQ",                                       # 리츠
    "BIL",                                       # 현금성
    # 섹터 (SPDR)
    "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY",
]


PIT_SOURCE_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)


def ensure_pit_file() -> None:
    """PIT CSV가 없으면 공개 저장소에서 자동 다운로드 (저장소 부트스트랩용)."""
    if PIT_FILE.exists():
        return
    print(f"PIT 파일 없음 → 다운로드: {PIT_SOURCE_URL}")
    resp = requests.get(PIT_SOURCE_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    PIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIT_FILE.write_bytes(resp.content)


def load_sp500_pit() -> pd.DataFrame:
    """시점별 S&P500 구성종목 (date, tickers 콤마구분)."""
    ensure_pit_file()
    df = pd.read_csv(PIT_FILE)
    df["date"] = pd.to_datetime(df["date"])
    return df


def sp500_members_on(date: str) -> list[str]:
    """특정 날짜의 S&P500 구성종목 리스트 (백테스트에서 사용)."""
    df = load_sp500_pit()
    d = pd.to_datetime(date)
    rows = df[df["date"] <= d]
    if rows.empty:
        raise ValueError(f"{date} 이전 구성종목 데이터가 없습니다 (최초: {df['date'].min().date()})")
    return sorted(rows.iloc[-1]["tickers"].split(","))


def fetch_nasdaq100_current() -> list[str]:
    """위키피디아에서 현재 NASDAQ100 티커 수집."""
    resp = requests.get(WIKI_NDX, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    for t in tables:
        cols = [str(c).lower() for c in t.columns]
        if any("ticker" in c or "symbol" in c for c in cols) and len(t) >= 90:
            col = t.columns[[("ticker" in str(c).lower() or "symbol" in str(c).lower()) for c in t.columns].index(True)]
            return sorted(t[col].astype(str).str.strip().tolist())
    raise RuntimeError("NASDAQ100 테이블을 찾지 못했습니다 — 위키피디아 구조 변경 여부 확인 필요")


def normalize_ticker(t: str) -> str:
    """야후 파이낸스 형식으로 변환 (BRK.B -> BRK-B)."""
    return t.replace(".", "-").strip().upper()


def build_current_universe() -> pd.DataFrame:
    sp500 = sp500_members_on(pd.Timestamp.today().strftime("%Y-%m-%d"))
    try:
        ndx = fetch_nasdaq100_current()
    except Exception as e:  # NDX 수집 실패해도 S&P500만으로 진행
        print(f"[warn] NASDAQ100 수집 실패: {e}", file=sys.stderr)
        ndx = []
    rows = []
    seen = set()
    for t, src in [(t, "SP500") for t in sp500] + [(t, "NDX100") for t in ndx] + [(t, "ETF") for t in ETFS]:
        yt = normalize_ticker(t)
        if yt in seen:
            # 중복(양쪽 소속)은 소스 병합
            for r in rows:
                if r["ticker"] == yt:
                    r["source"] = r["source"] + "+NDX100"
            continue
        seen.add(yt)
        rows.append({"ticker": yt, "source": src})
    df = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
    out = UNIVERSE_DIR / "current_universe.csv"
    df.to_csv(out, index=False)
    print(f"universe: {len(df)} tickers -> {out}")
    return df


if __name__ == "__main__":
    build_current_universe()
