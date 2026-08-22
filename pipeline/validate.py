"""데이터 품질 검증 — 매 실행 후 reports/data_quality.md 갱신.

체크 항목:
1. 최신성: 마지막 데이터 날짜가 최근 영업일 기준 3일 이내인가
2. 커버리지: 유니버스 대비 데이터가 있는 티커 비율
3. 무결성: 음수/0 가격, high<low 같은 불량 행
4. 이상 수익률: 하루 ±50% 초과 (분할 미반영 의심 사례) 카운트
검증 실패 시 exit code 1 → Actions 실행이 실패로 표시되어 바로 인지 가능.
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PRICES_DIR = ROOT / "data" / "prices"
UNIVERSE_FILE = ROOT / "data" / "universe" / "current_universe.csv"
REPORT = ROOT / "reports" / "data_quality.md"


def main() -> None:
    files = sorted(PRICES_DIR.glob("*.parquet"))
    if not files:
        print("가격 데이터 없음 — 백필 전이면 정상")
        return
    df = pd.concat([pd.read_parquet(f) for f in files[-2:]], ignore_index=True)  # 최근 2개년만 검사
    df["date"] = pd.to_datetime(df["date"])
    universe = set(pd.read_csv(UNIVERSE_FILE)["ticker"])

    problems: list[str] = []

    last_date = df["date"].max()
    staleness = (pd.Timestamp(date.today()) - last_date).days
    if staleness > 5:
        problems.append(f"최신성 실패: 마지막 데이터가 {last_date.date()} ({staleness}일 전)")

    latest = df[df["date"] == last_date]
    coverage = len(set(latest["ticker"]) & universe) / len(universe)
    if coverage < 0.95:
        problems.append(f"커버리지 실패: 최신일 데이터 보유 비율 {coverage:.1%} (< 95%)")

    bad = df[(df["close"] <= 0) | (df["high"] < df["low"])]
    if len(bad) > 0:
        problems.append(f"무결성 실패: 불량 행 {len(bad)}건")

    df = df.sort_values(["ticker", "date"])
    rets = df.groupby("ticker")["close"].pct_change()
    extreme = int((rets.abs() > 0.5).sum())

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 데이터 품질 리포트",
        f"- 검증 시각: {date.today()}",
        f"- 마지막 데이터 날짜: {last_date.date()} (staleness {staleness}일)",
        f"- 최신일 커버리지: {coverage:.1%} / 유니버스 {len(universe)} 티커",
        f"- 불량 행(음수가·high<low): {len(bad)}건",
        f"- 이상 수익률(일 ±50% 초과): {extreme}건 (급등락·분할 확인 필요 시 수동 점검)",
        f"- 판정: {'FAIL — ' + '; '.join(problems) if problems else 'PASS'}",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
