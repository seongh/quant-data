"""execute.py / execute_kr.py 를 임시 디렉터리 + 가짜 브로커로 실행하는 공용 헬퍼."""
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PIPE = Path(__file__).resolve().parent.parent / "pipeline"
sys.path.insert(0, str(PIPE))

OPEN_US = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))
OPEN_KR = datetime(2026, 9, 28, 9, 14, tzinfo=ZoneInfo("Asia/Seoul"))


def load_us(tmp, monkeypatch, fake, weights, extra_sig=None, env=None):
    monkeypatch.setenv("ALPACA_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET", "s")
    monkeypatch.delenv("DECISION_URL", raising=False)
    monkeypatch.delenv("EXECUTE_FORCE", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    ex = importlib.reload(importlib.import_module("execute"))
    for name in ("SIGNALS", "STATE", "LOG", "HALT", "MARKER"):
        p = tmp / getattr(ex, name).relative_to(ex.ROOT)
        p.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ex, name, p)
    for name in ("LAST_TARGET", "SKIP_MARKER"):
        if hasattr(ex, name):
            p = tmp / getattr(ex, name).relative_to(ex.ROOT)
            p.parent.mkdir(parents=True, exist_ok=True)
            monkeypatch.setattr(ex, name, p)
    sig = {"date": "2026-09-25", "source": "위원회 결정 오버레이 (test)", "decision_state": "valid",
           "weights": weights, "mr_scale": 1.0, "mr_policy": "liquidate"}
    sig.update(extra_sig or {})
    ex.SIGNALS.write_text(json.dumps(sig))
    monkeypatch.setattr(ex, "api", fake.api)
    monkeypatch.setattr(ex, "market_open_now", lambda now=None: (True, "NY test"))
    if hasattr(ex, "time"):
        monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    return ex


def load_kr(tmp, monkeypatch, fake, weights, extra_sig=None, env=None, decision=None):
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678")
    monkeypatch.delenv("EXECUTE_FORCE", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    ex = importlib.reload(importlib.import_module("execute_kr"))
    for name in ("SIGNALS", "STATE", "LOG", "HALT", "MARKER", "SKIP_MARKER", "LAST_TARGET"):
        if hasattr(ex, name):
            p = tmp / getattr(ex, name).relative_to(ex.ROOT)
            p.parent.mkdir(parents=True, exist_ok=True)
            monkeypatch.setattr(ex, name, p)
    sig = {"date": "2026-09-25", "vt_scale": 0.2, "weights": weights, "names": {},
           "source": "위원회 결정 오버레이 (test)", "decision_state": "valid"}
    sig.update(extra_sig or {})
    ex.SIGNALS.write_text(json.dumps(sig))
    monkeypatch.setattr(ex, "get_token", lambda: None)
    monkeypatch.setattr(ex, "get_balance", fake.get_balance)
    monkeypatch.setattr(ex, "get_buying_power", fake.get_buying_power)
    monkeypatch.setattr(ex, "get_price", fake.get_price)
    monkeypatch.setattr(ex, "order", fake.order)
    monkeypatch.setattr(ex, "market_open_now", lambda now=None: (True, "KST test"))
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    return ex
