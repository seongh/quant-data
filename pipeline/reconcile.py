"""집행 후 대사(Reconciliation) — "조용한 실패" 봉쇄 (계층 3 인터록).

집행 직후 실행되어, 문제가 있으면 exit 1로 워크플로를 실패 처리 → 실패 메일 경보.
사용: python pipeline/reconcile.py us|kr
검사: 오늘자 집행 로그 존재 / FAIL 주문 0건 / (미국) 계좌 현금 ≥ 0 (헌법 1조)
주의: 이 스크립트는 어떤 주문도 내지 않는다. 읽기와 판정만 한다.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = {"us": "reports/trade_log.md", "kr": "reports/kr_trade_log.md"}


def main():
    mkt = sys.argv[1] if len(sys.argv) > 1 else "us"
    log_path = ROOT / LOGS[mkt]
    problems = []

    if not log_path.exists():
        sys.exit("[RECONCILE FAIL] 집행 로그 파일 없음")
    today_block = log_path.read_text().split("\n---\n")[0]
    header = today_block.splitlines()[0] if today_block.splitlines() else ""
    if str(date.today()) not in header:
        sys.exit(f"[RECONCILE FAIL] 오늘자 집행 로그 없음 (최신: {header[:40]})")

    fails = [l.strip() for l in today_block.splitlines() if l.strip().startswith("- FAIL")]
    if fails:
        problems.append(f"실패 주문 {len(fails)}건 — " + " / ".join(f[:70] for f in fails[:3]))

    if mkt == "us":
        key = os.environ.get("ALPACA_KEY", "")
        sec = os.environ.get("ALPACA_SECRET", "")
        if key and sec:
            time.sleep(30)  # 시장가 체결·현금 반영 대기
            req = urllib.request.Request(
                "https://paper-api.alpaca.markets/v2/account",
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
            with urllib.request.urlopen(req, timeout=30) as r:
                acct = json.loads(r.read())
            cash = float(acct["cash"])
            print(f"[reconcile] 계좌 현금 ${cash:,.2f}")
            if cash < -1.0:
                problems.append(f"현금 음수 ${cash:,.2f} — 헌법 1조(무레버리지) 위반 상태. "
                                f"execute.py는 현금 범위 내에서만 매수하므로 다음 집행에서 추가 매수는 자동 차단됨")
        else:
            print("[reconcile] ALPACA 키 없음 — 현금 검사 생략")

    if problems:
        sys.exit("[RECONCILE FAIL] " + " | ".join(problems))
    print(f"[reconcile] PASS — {mkt} 대사 이상 없음 (실패 주문 0건)")


if __name__ == "__main__":
    main()
