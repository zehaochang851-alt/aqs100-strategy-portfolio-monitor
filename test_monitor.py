import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import monitor

os.environ["ALPACA_API_KEY"] = "test"
os.environ["ALPACA_SECRET_KEY"] = "test"
os.environ["TELEGRAM_BOT_TOKEN"] = "test"
os.environ["TELEGRAM_CHAT_ID"] = "test"

monitor.STATE_PATH = ROOT / "test_state.json"
if monitor.STATE_PATH.exists():
    monitor.STATE_PATH.unlink()

calls = []
run_count = {"n": 0}

def fake_fetch(symbols, start, end):
    run_count["n"] += 1
    periods = 1200 + run_count["n"] - 1
    idx = pd.date_range("2026-08-01 09:30", periods=periods, freq="h")
    result = {}
    for symbol in symbols:
        result[symbol] = [{"t": ts.tz_localize("America/New_York").tz_convert("UTC").isoformat().replace("+00:00", "Z"), "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0 + i * 0.01, "v": 1000.0} for i, ts in enumerate(idx)]
    return result

def fake_score(frame, feature, model, length):
    return pd.Series(range(len(frame)), index=frame.index, dtype=float).to_numpy()

def fake_signal(score, strategy, threshold, exit_threshold=0.0):
    out = [0.0] * len(score)
    out[-1] = 1.0
    return out

def fake_send(text):
    calls.append(text)

monitor.fetch_bars = fake_fetch
monitor.model_score = fake_score
monitor.signal_from_score = fake_signal
monitor.send_telegram = fake_send

monitor.main()
assert calls == [], "首次執行不應發送假訊號"
monitor.main()
assert len(calls) >= 1, "新 K 線及訊號變化應發送 Telegram"
assert "ENTRY" in calls[0]
assert "目前 Portfolio 累積損益" in calls[0]
state = json.loads(monitor.STATE_PATH.read_text())
assert state["started"] is True
assert len(state["strategies"]) == 7
print("MONITOR_SMOKE_OK")
print("first_run_no_alert=validated")
print("new_bar_entry_alert=validated")
print("portfolio_state_persistence=validated")
