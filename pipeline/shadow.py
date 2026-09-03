"""pipeline/shadow.py — 그림자 포트폴리오 기록 (거래 없음, 계산·기록만)

목적 (03_조직설계 보완장치 1): 위원회 실계좌 vs F1 원신호(룰) vs SPY 3자 병행 기록.
F1 원신호는 오버레이·mr_scale 없이(=위원회가 없었을 때의 룰 그대로) 산출하며,
무비용·익일 종가 리밸런스 근사로 가상 운용한다. 첫 실행 시 2026-08-21부터 소급 백필.

출력:
  signals/shadow_state.json  — {last, eq, w, series} 누적 상태
  reports/shadow_log.md      — 날짜별 3자 비교 한 줄 추가 (append-only)

주의: 이 스크립트는 주문을 내지 않는다. 실패해도 집행에 영향 없도록
워크플로에서 continue-on-error 로 실행할 것.
"""
import json
from pathlib import Path

import pandas as pd

import signals as S  # 검증된 신호 로직 단일 소스 재사용

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "signals" / "shadow_state.json"
LOG = ROOT / "reports" / "shadow_log.md"
START = "2026-08-21"  # F1 첫 신호일 = 기준점 1.0

LOG_HEADER = (
    "# 그림자 3자 비교 로그 (기준 2026-08-21 = 1.0000)\n\n"
    "F1 원신호: 오버레이 없음·무비용·익일 종가 리밸런스 근사 / 위원회: 실계좌 실측(비용·슬리피지 포함)\n\n"
    "| 날짜 | F1 원신호 | 위원회 실계좌 | SPY |\n|---|---|---|---|\n"
)


def f1_raw_weights(close: pd.DataFrame) -> dict:
    """F1 원신호 목표비중 (위원회 오버레이·mr_scale 미적용 = 순수 룰)."""
    mr = S.mean_reversion_weights(close)
    sleeves = {
        "AAA_45": S.aaa_weights(close),
        "DUAL_20": S.ensemble_dm_weights(close),
        "BOND_25": S.bond_weights(close),
        "MR_10": mr,
    }
    mix = {"AAA_45": 0.45, "DUAL_20": 0.20, "BOND_25": 0.25, "MR_10": 0.10}
    total: dict[str, float] = {}
    for k, ws in sleeves.items():
        for t, w in ws.items():
            total[t] = total.get(t, 0.0) + mix[k] * w
    if sum(total.values()) < 0.999:  # MR 미충족분은 현금(BIL) — signals.py와 동일 규칙
        total["BIL"] = total.get("BIL", 0.0) + (1.0 - sum(total.values()))
    s = sum(total.values())
    return {t: w / s for t, w in total.items() if w / s > 0.001}


def main():
    close = S.load_close()
    rets = close.pct_change(fill_method=None)

    if STATE.exists():
        state = json.loads(STATE.read_text())
    else:
        state = {"last": None, "eq": 1.0, "w": {}, "series": {}}

    dates = [d for d in close.index
             if str(d.date()) >= START
             and (state["last"] is None or str(d.date()) > state["last"])]

    updated = []
    for d in dates:
        if state["w"]:  # 전일 신호 비중에 당일 수익률 적용
            row = rets.loc[d]
            r = 0.0
            for t, w in state["w"].items():
                v = row.get(t)
                if v is not None and pd.notna(v):
                    r += w * float(v)
            state["eq"] *= (1.0 + r)
        state["w"] = f1_raw_weights(close.loc[:d])  # 당일 종가 신호 → 익일 적용
        state["last"] = str(d.date())
        state["series"][state["last"]] = round(state["eq"], 6)
        updated.append(state["last"])

    if not updated:
        print("그림자: 갱신할 신규 거래일 없음")
        return

    # 실계좌·SPY 비교치
    acct_file = ROOT / "signals" / "account_state.json"
    acct = json.loads(acct_file.read_text()) if acct_file.exists() else {}
    spy = close["SPY"].dropna()
    spy_base = float(spy.loc[spy.index >= pd.Timestamp(START)].iloc[0])
    spy_rel = float(spy.iloc[-1]) / spy_base

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    acct_txt = (f"${acct['last_equity']:,.0f} (dd {acct['dd']:.2%})"
                if acct else "미기록")
    line = f"| {state['last']} | {state['eq']:.4f} | {acct_txt} | {spy_rel:.4f} |\n"
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text((LOG.read_text() if LOG.exists() else LOG_HEADER) + line)
    print(f"그림자 갱신 {updated[0]}~{updated[-1]} ({len(updated)}일): "
          f"eq {state['eq']:.4f}, SPY {spy_rel:.4f}")


if __name__ == "__main__":
    main()
