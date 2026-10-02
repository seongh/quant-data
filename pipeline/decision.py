"""위원회 결정 브리지 — US·KR 공용 단일 소스 (2026-09-28 패치).

decisions/approved.json (US) / decisions/approved_kr.json (KR) 을 읽어 4개 상태 중 하나로 판정한다.

    valid    유효기한 내, 형식 정상  → 승인안 오버레이 적용, 집행 허용
    expired  유효기한 경과            → 집행 정지 (C-21: 만료 = 정지)
    missing  파일 없음                → 집행 정지 (헌법 3조: 기록된 결정 없이 매매 금지)
    invalid  JSON·형식·시장 불일치    → 집행 정지

이전 코드는 만료·오류 시 F1 원신호로 "자동 복귀"하며 이를 fail-safe 라 불렀으나(결함 L),
실제로는 mr_scale 1.0 복귀·override 해제로 리스크가 커지는 fail-open 이었다.
이 모듈은 fail-closed 다: valid 가 아니면 signals 가 decision_state 를 기록하고,
preflight 와 execute 가 각각 독립적으로 집행을 막는다(이중 인터록).

형식 규칙 (R-26): valid_until 은 ISO-8601 절대일자(YYYY-MM-DD)만 허용. 상대일자·자연어는 invalid.
오버라이드 의미 (#21): override_semantics = "cap"(기본값) | "set".
    cap: 목표비중 = min(시스템 신호, 승인값) — 리스크를 줄이는 방향으로만 작동, 신규 편입 불가
    set: 목표비중 = 승인값 (구버전 동작, 명시해야만 사용)
"""
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TZ = {"us": ZoneInfo("America/New_York"), "kr": ZoneInfo("Asia/Seoul")}
FILES = {"us": "approved.json", "kr": "approved_kr.json"}
WARN_DAYS = 3


def local_today(market: str) -> date:
    return datetime.now(TZ[market]).date()


def load_decision(market: str, decisions_dir: Path, today: date | None = None):
    """(결정 dict 또는 None, 상태, 설명) 반환. 상태가 'valid' 일 때만 dict 를 돌려준다."""
    today = today or local_today(market)
    f = Path(decisions_dir) / FILES[market]
    if not f.exists():
        return None, "missing", f"{f.name} 없음"
    try:
        dec = json.loads(f.read_text())
    except Exception as e:
        return None, "invalid", f"{f.name} JSON 해석 실패: {e}"
    if not isinstance(dec, dict):
        return None, "invalid", f"{f.name} 최상위가 객체가 아님"
    mkt = str(dec.get("market", market)).lower()
    if mkt != market:
        return None, "invalid", f"{f.name} market={mkt!r} ≠ {market!r}"
    vu = str(dec.get("valid_until", ""))
    if not ISO_DATE.match(vu):
        return None, "invalid", f"valid_until={vu!r} — ISO 절대일자(YYYY-MM-DD)만 허용 (R-26)"
    try:
        until = date.fromisoformat(vu)
    except ValueError:
        return None, "invalid", f"valid_until={vu!r} — 존재하지 않는 날짜"
    sem = str(dec.get("override_semantics", "cap")).lower()
    if sem not in ("cap", "set"):
        return None, "invalid", f"override_semantics={sem!r} — cap|set 만 허용"
    ov = dec.get("overrides", {})
    if not isinstance(ov, dict):
        return None, "invalid", "overrides 가 객체가 아님"
    try:
        if any(float(w) < 0 or float(w) > 1 for w in ov.values()):
            return None, "invalid", "overrides 비중이 0~1 범위 밖"
    except (TypeError, ValueError):
        return None, "invalid", "overrides 비중이 숫자가 아님"
    if today > until:
        return None, "expired", f"결정 만료: {vu} (오늘 {today})"
    left = (until - today).days
    note = f"{dec.get('meeting_id', '?')}"
    if left <= WARN_DAYS:
        print(f"[warn] 위원회 결정 유효기한 {vu}까지 {left}일 — 갱신 필요 (만료 시 집행 정지)")
    return dec, "valid", note


def source_label(state: str, note: str, base: str) -> str:
    if state == "valid":
        return f"위원회 결정 오버레이 ({note})"
    return f"{base} ({note}) — 집행 정지(C-21)"


def apply_overrides(total: dict, dec: dict, cash: str) -> dict:
    """승인안 overrides 를 적용하고 잔여·초과분을 현금성(cash)으로 흡수. 합계 1.0, 음수 없음 보장."""
    sem = str(dec.get("override_semantics", "cap")).lower()
    total = dict(total)
    for t, w in dec.get("overrides", {}).items():
        w = float(w)
        if t == cash:
            continue  # 현금성은 잔여 흡수 계정 — 직접 지정 불가 (합계 보정과 충돌 방지)
        total[t] = min(total.get(t, 0.0), w) if sem == "cap" else w
    diff = round(1.0 - sum(v for k, v in total.items() if k != cash), 6)
    total[cash] = round(diff, 6)
    if total[cash] < 0:  # set 의미론에서만 가능: 위험자산 전체를 비례 축소
        s2 = sum(v for k, v in total.items() if k != cash)
        total = {k: v / s2 for k, v in total.items() if k != cash}
        total[cash] = 0.0
    total = {t: round(w, 6) for t, w in total.items() if w > 0.0005}
    excess = round(sum(total.values()) - 1.0, 6)
    if excess > 0:
        big = cash if cash in total else max(total, key=total.get)
        total[big] = round(total[big] - excess, 6)
    return total
