import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "strategies.json").read_text(encoding="utf-8"))
STRATEGIES = CONFIG["strategies"]
INTERVAL = CONFIG.get("interval", "1h")
FEED = CONFIG.get("feed", "iex")
ADJUSTMENT = CONFIG.get("adjustment", "all")
VIX_TICKER = CONFIG.get("vix_proxy", "VIXY")
STATE_PATH = ROOT / "state.json"
ALPACA_URL = "https://data.alpaca.markets/v2/stocks/bars"
TIMEFRAME_MAP = {"1d": "1Day", "1h": "1Hour", "30m": "30Min", "15m": "15Min", "5m": "5Min", "1m": "1Min"}
ANNUALIZATION = {"1d": 252, "1h": 2016, "30m": 4032, "15m": 8064, "5m": 24192, "1m": 120960}[INTERVAL]


def env_required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少 GitHub Secret：{name}")
    return value


def iso_utc(ts):
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York")
    return ts.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def fetch_bars(symbols, start, end):
    symbols = list(dict.fromkeys(symbols))
    headers = {"APCA-API-KEY-ID": env_required("ALPACA_API_KEY"), "APCA-API-SECRET-KEY": env_required("ALPACA_SECRET_KEY")}
    params = {"symbols": ",".join(symbols), "timeframe": TIMEFRAME_MAP[INTERVAL], "start": iso_utc(start), "end": iso_utc(end), "limit": 10000, "adjustment": ADJUSTMENT, "feed": FEED, "sort": "asc"}
    records = {s: [] for s in symbols}
    while True:
        response = requests.get(ALPACA_URL, headers=headers, params=params, timeout=90)
        if response.status_code in (401, 403):
            raise RuntimeError(f"Alpaca 認證或資料權限錯誤（{response.status_code}）")
        if response.status_code == 429:
            raise RuntimeError("Alpaca API 呼叫太頻繁（429），請稍後重試")
        response.raise_for_status()
        payload = response.json() or {}
        for symbol in symbols:
            records[symbol].extend((payload.get("bars", {}) or {}).get(symbol, []) or [])
        token = payload.get("next_page_token")
        if not token:
            return records
        params["page_token"] = token


def series_from(records, field, name):
    if not records:
        return pd.Series(dtype=float, name=name)
    idx = pd.to_datetime([r["t"] for r in records], utc=True).tz_convert("America/New_York").tz_localize(None)
    values = pd.to_numeric([r.get(field, np.nan) for r in records], errors="coerce")
    result = pd.Series(values, index=idx, name=name).sort_index()
    return result[~result.index.duplicated(keep="last")]


def build_frame(records, targets, benchmarks):
    frames = []
    for target in targets:
        base = pd.concat([series_from(records[target], "o", "open"), series_from(records[target], "h", "high"), series_from(records[target], "l", "low"), series_from(records[target], "c", "close"), series_from(records[target], "v", "volume")], axis=1)
        base.columns = ["open", "high", "low", "close", "volume"]
        base[f"{target}_close"] = base["close"]
        base[f"{target}_returns"] = base["close"].pct_change(fill_method=None)
        base["VIX_close"] = series_from(records[VIX_TICKER], "c", VIX_TICKER)
        base["VIX_returns"] = base["VIX_close"].pct_change(fill_method=None)
        for benchmark in benchmarks[target]:
            close = series_from(records[benchmark], "c", benchmark)
            base[f"{benchmark}_close"] = close
            base[f"{benchmark}_returns"] = close.pct_change(fill_method=None)
            base[f"{target}_{benchmark}_spread"] = base[f"{target}_close"] - close
        if CONFIG.get("regular_trading_hours_only", True) and INTERVAL != "1d":
            base = base[(base.index.strftime("%H:%M") >= "09:30") & (base.index.strftime("%H:%M") <= "16:00")]
        frames.append(base.dropna().sort_index())
    return frames


def model_score(frame, feature, model, length):
    s = pd.to_numeric(frame[feature], errors="coerce").astype(float)
    if model == "zscore":
        return ((s - s.rolling(length).mean()) / s.rolling(length).std()).to_numpy()
    if model == "min_max":
        lo, hi = s.rolling(length).min(), s.rolling(length).max()
        return (2 * ((s - lo) / (hi - lo)) - 1).to_numpy()
    if model == "sma_diff":
        return (s / s.rolling(length).mean() - 1).to_numpy()
    if model == "robust_scaler":
        med = s.rolling(length).median()
        return ((s - med) / (s.rolling(length).quantile(.75) - s.rolling(length).quantile(.25))).to_numpy()
    if model == "maxabs_norm":
        return (s / s.rolling(length).apply(lambda x: np.abs(x).max(), raw=True)).to_numpy()
    if model == "rsi":
        delta = s.diff()
        gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
        avg_gain = gain.rolling(length, min_periods=length).mean()
        avg_loss = loss.rolling(length, min_periods=length).mean()
        return (100 - 100 / (1 + avg_gain / avg_loss)).to_numpy()
    raise ValueError(f"未知 Model：{model}")


def signal_from_score(score, strategy, threshold, exit_threshold=0.0):
    x = np.asarray(score, dtype=float)
    prev = np.roll(x, 1)
    prev[0] = np.nan
    if strategy == "trend_long": out = np.where(prev >= threshold, 1, np.where(prev <= exit_threshold, 0, np.nan))
    elif strategy == "trend_short": out = np.where(prev >= threshold, -1, np.where(prev <= exit_threshold, 0, np.nan))
    elif strategy == "trend_reverse_long": out = np.where(prev <= threshold, 1, np.where(prev >= exit_threshold, 0, np.nan))
    elif strategy == "trend_reverse_short": out = np.where(prev <= threshold, -1, np.where(prev >= exit_threshold, 0, np.nan))
    elif strategy == "trend": out = np.where(prev >= threshold, 1, np.where(prev <= -threshold, -1, np.nan))
    elif strategy == "trend_reverse": out = np.where(prev >= threshold, -1, np.where(prev <= -threshold, 1, np.nan))
    elif strategy in ("mr", "mr_reverse"):
        reverse = strategy == "mr_reverse"
        a = np.where(prev > threshold, -1 if reverse else 1, np.where(prev < 0, 0, np.nan))
        b = np.where(prev < -threshold, 1 if reverse else -1, np.where(prev > 0, 0, np.nan))
        out = pd.Series(a).ffill().to_numpy() + pd.Series(b).ffill().to_numpy()
    else: raise ValueError(f"未知 Strategy：{strategy}")
    return pd.Series(out).ffill().fillna(0).to_numpy(dtype=float)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"strategies": {}, "last_bar": None, "last_daily_summary": None, "started": False}


def save_state(state):
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def send_telegram(text):
    token = env_required("TELEGRAM_BOT_TOKEN")
    chat_id = env_required("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
    response.raise_for_status()


def pct(value):
    return f"{value * 100:+.2f}%"


def main():
    now = pd.Timestamp.now(tz="America/New_York")
    # Alpaca bar 的時間標籤是該根 K 線的開始時間；因此只取已經完整結束的 bar。
    end = now.floor("h") - pd.Timedelta(hours=1) if INTERVAL != "1d" else now.normalize()
    # 1h 最長 rolling length 是 180，20 天不夠，使用 120 天保留安全暖身區。
    start = end - pd.Timedelta(days=120 if INTERVAL != "1d" else 500)
    targets = list(dict.fromkeys(s["target"] for s in STRATEGIES))
    benchmark_map = {target: [] for target in targets}
    for s in STRATEGIES:
        feature = s["feature"]
        if feature.endswith("_returns") and feature not in (f"{s['target']}_returns", "VIX_returns"):
            benchmark_map[s["target"]].append(feature[:-8])
        elif feature.endswith("_close") and feature not in (f"{s['target']}_close", "VIX_close"):
            benchmark_map[s["target"]].append(feature[:-6])
        elif feature.startswith(s["target"] + "_") and feature.endswith("_spread"):
            benchmark_map[s["target"]].append(feature[len(s["target"]) + 1:-7])
    for target in benchmark_map:
        benchmark_map[target] = list(dict.fromkeys(benchmark_map[target]))
    symbols = targets + [VIX_TICKER] + [b for values in benchmark_map.values() for b in values]
    records = fetch_bars(symbols, start, end)
    frames = {target: frame for target, frame in zip(targets, build_frame(records, targets, benchmark_map))}

    state = load_state()
    state.setdefault("strategies", {})
    state.setdefault("started", False)
    state.setdefault("last_daily_summary", None)
    state.setdefault("daily_date", None)
    state.setdefault("daily_anchor_pnl", 0.0)
    events = []
    strategy_pnl = {}
    latest_bar = None
    fee = 0.0006

    for spec in STRATEGIES:
        target = spec["target"]
        frame = frames[target]
        if len(frame) < int(spec["length"]) + 2:
            raise RuntimeError(f"{target} 資料不足以計算 length={spec['length']}")
        score = model_score(frame, spec["feature"], spec["model"], int(spec["length"]))
        signal = signal_from_score(score, spec["strategy"], float(spec["entry_threshold"]), float(spec.get("exit_threshold", 0.0)))
        ret = frame["close"].pct_change(fill_method=None).fillna(0.0).to_numpy(dtype=float)
        bar_time = frame.index[-1]
        latest_bar = max(latest_bar, bar_time) if latest_bar is not None else bar_time
        old = state["strategies"].get(spec["strategy_id"], {})
        old_signal = float(old.get("signal", 0.0))
        cumulative = float(old.get("cumulative_pnl", 0.0))
        last_bar_text = old.get("last_bar")
        previous_pos = None
        if last_bar_text:
            previous_ts = pd.Timestamp(last_bar_text)
            prior = np.flatnonzero(frame.index <= previous_ts)
            if len(prior):
                previous_pos = int(prior[-1])
        if not state.get("started") or previous_pos is None:
            # 第一次啟動只建立基準狀態，不把歷史資料冒充成即時監控損益，也不發假訊號。
            first_new_pos = len(frame) - 1
        else:
            first_new_pos = max(previous_pos + 1, 0)
            for j in range(first_new_pos, len(frame)):
                trade = abs(float(signal[j]) - float(signal[j - 1])) if j > 0 else 0.0
                bar_pnl = float(signal[j - 1] * ret[j] - trade * fee) if j > 0 else 0.0
                cumulative += bar_pnl
                if float(signal[j]) != float(signal[j - 1]):
                    events.append({"target": target, "action": "ENTRY" if signal[j] != 0 else "EXIT", "price": float(frame["close"].iloc[j]), "bar_time": str(frame.index[j]), "strategy_id": spec["strategy_id"], "signal": float(signal[j])})
        current = float(signal[-1])
        close = float(frame["close"].iloc[-1])
        strategy_pnl[spec["strategy_id"]] = cumulative
        state["strategies"][spec["strategy_id"]] = {"signal": current, "last_bar": str(bar_time), "last_price": close, "cumulative_pnl": cumulative}

    total_weight = sum(float(s["weight"]) for s in STRATEGIES) or 1.0
    portfolio_pnl = sum(float(s["weight"]) * strategy_pnl[s["strategy_id"]] for s in STRATEGIES) / total_weight
    today = str(now.date())
    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_anchor_pnl"] = portfolio_pnl
        state["last_daily_summary"] = None
    daily_pnl = portfolio_pnl - float(state.get("daily_anchor_pnl", portfolio_pnl))

    if events:
        lines = ["[AQS100 Portfolio 訊號]", f"已完成 K 線：{latest_bar}"]
        for event in events:
            lines.append(f"{event['action']}｜{event['target']}｜參考價格 {event['price']:.4f}｜{event['strategy_id']}")
        lines.append(f"目前 Portfolio 累積損益：{pct(portfolio_pnl)}")
        lines.append(f"今日 Portfolio 損益：{pct(daily_pnl)}")
        lines.append("各策略累積損益：")
        for spec in STRATEGIES:
            lines.append(f"{spec['target']}｜{pct(strategy_pnl[spec['strategy_id']])}｜{spec['strategy_id']}")
        send_telegram("\n".join(lines))

    is_trading_day = now.weekday() < 5 and latest_bar is not None and latest_bar.date() == now.date()
    if is_trading_day and now.hour >= 16 and state.get("last_daily_summary") != today:
        lines = ["[AQS100 每日 Portfolio 摘要]", f"日期：{today}", f"Portfolio 累積損益：{pct(portfolio_pnl)}", f"今日損益：{pct(daily_pnl)}"]
        for spec in STRATEGIES:
            lines.append(f"{spec['target']}｜{pct(strategy_pnl[spec['strategy_id']])}｜{spec['strategy_id']}")
        send_telegram("\n".join(lines))
        state["last_daily_summary"] = today

    state["last_bar"] = str(latest_bar)
    state["started"] = True
    save_state(state)
    print(json.dumps({"status": "OK", "bar": str(latest_bar), "events": events, "portfolio_cumulative_pnl": portfolio_pnl, "portfolio_daily_pnl": daily_pnl}, ensure_ascii=False))


if __name__ == "__main__":
    main()
