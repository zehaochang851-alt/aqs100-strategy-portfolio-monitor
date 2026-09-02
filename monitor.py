import json
import math
import os
from html import escape
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
INITIAL_CAPITAL_USD = float(CONFIG.get("initial_capital_usd", 10000.0))


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


def discover_private_chat_id():
    token = env_required("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    response = requests.get(url, timeout=30)
    if not response.ok:
        try:
            detail = response.json().get("description", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"Telegram API 讀取更新失敗：HTTP {response.status_code}；{detail}")
    payload = response.json() or {}
    for update in reversed(payload.get("result", []) or []):
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        if chat.get("type") == "private" and sender.get("is_bot") is not True and chat.get("id") is not None:
            return str(chat["id"])
    raise RuntimeError("找不到私人聊天 Chat ID。請先在新的 AQS100 Bot 私人聊天傳送 /start，再重新手動執行 workflow。")


def send_telegram(text, chat_id=None):
    token = env_required("TELEGRAM_BOT_TOKEN")
    chat_id = str(chat_id or env_required("TELEGRAM_CHAT_ID"))
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=30)
    if not response.ok:
        try:
            detail = response.json().get("description", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"Telegram API 發送失敗：HTTP {response.status_code}；{detail}")


def pct(value):
    return f"{value * 100:+.2f}%"


def pnl_line(value, dollars):
    icon = "🟢" if dollars >= 0 else "🔴"
    return f"{icon} ${dollars:+,.2f} ({pct(value)})"


def send_telegram_preview(chat_id):
    signal_preview = """🔔 <b>AQS100｜新訊號</b>
🟢 <b>ENTRY｜CRWD</b>
📅 2026-09-01　🕐 13:00

━━━━━━━━━━━━━━
🎯 <b>這次訊號</b>
━━━━━━━━━━━━━━

訊號價格　<b>$214.15</b>
參考股數　<b>6 股</b>

策略　　　<code>min_max</code>
參數　　　<code>L60</code>
門檻　　　<code>0.95</code>

━━━━━━━━━━━━━━
💰 <b>資金配置</b>
━━━━━━━━━━━━━━

策略預算　　$1,428.57
實際持倉　　<b>$1,284.90</b>
未使用預算　$143.67
預算使用率　<b>89.9%</b>

━━━━━━━━━━━━━━
📊 <b>訊號後 Portfolio</b>
━━━━━━━━━━━━━━

目前資金　<b>$10,026.64</b>
累積損益　🟢 <b>+$26.64（+0.27%）</b>
今日損益　🔴 <b>-$10.29（-0.10%）</b>

<i>以上為模擬訊號，不會下單。</i>"""
    daily_preview = """📊 <b>AQS100 每日摘要｜2026-09-01</b>

━━━━━━━━━━━━━━
💰 <b>帳戶今天怎麼樣？</b>
━━━━━━━━━━━━━━

目前總資產　<b>$10,026.64</b>

今日損益　🔴 <b>-$5.88（-0.06%）</b>
累積損益　🟢 <b>+$31.05（+0.31%）</b>

💵 現金　　$7,363.75
📈 已進場　$2,636.25（26.3%）

━━━━━━━━━━━━━━
🚀 <b>目前已進場</b>
━━━━━━━━━━━━━━

共 <b>2 檔股票</b>
投入資金 <b>$2,636.25</b>

🥤 <b>KO</b>
目前 🔴 <b>虧損 $26.18（-1.83%）</b>

策略　　　trend_reverse_long｜robust_scaler
持有　　　15 股
買入價　　$90.1200
目前價　　$90.0900
持倉市值　$1,351.35

──────────────

🛡️ <b>CRWD</b>
目前 🟢 <b>獲利 $5.25（+0.37%）</b>

策略　　　trend_long｜min_max
持有　　　6 股
買入價　　$214.1500
目前價　　$214.1500
持倉市值　$1,284.90

<i>以上為模擬摘要，不會下單。</i>"""
    send_telegram(signal_preview, chat_id=chat_id)
    send_telegram(daily_preview, chat_id=chat_id)


def main():
    now = pd.Timestamp.now(tz="America/New_York")
    state = load_state()
    manual_test = os.environ.get("TELEGRAM_TEST", "false").strip().lower() == "true"
    if manual_test:
        telegram_chat_id = discover_private_chat_id()
        state["telegram_chat_id"] = telegram_chat_id
        if os.environ.get("TELEGRAM_PREVIEW", "false").strip().lower() == "true":
            send_telegram_preview(telegram_chat_id)
            print("TELEGRAM_PREVIEW_SENT")
            return
        send_telegram("[AQS100 Telegram 測試成功]\\n時間：" + str(now) + "\\n這只是通知連線測試，不會下單。", chat_id=telegram_chat_id)
        print("TELEGRAM_CHAT_ID_DISCOVERED")
        print("TELEGRAM_TEST_SENT")
    else:
        telegram_chat_id = str(state.get("telegram_chat_id") or env_required("TELEGRAM_CHAT_ID"))
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

    state.setdefault("strategies", {})
    state.setdefault("started", False)
    state.setdefault("last_daily_summary", None)
    state.setdefault("daily_date", None)
    state.setdefault("daily_anchor_pnl", 0.0)
    events = []
    strategy_pnl = {}
    strategy_dollar_pnl = {}
    strategy_allocations = {}
    latest_bar = None
    fee = 0.0006
    total_weight = sum(float(s.get("weight", 0.0)) for s in STRATEGIES) or 1.0

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
        allocated_capital = INITIAL_CAPITAL_USD * float(spec.get("weight", 0.0)) / total_weight
        current_shares = int(old.get("reference_shares", 0))
        entry_price = old.get("entry_price")
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
                    event_price = float(frame["close"].iloc[j])
                    is_entry = float(signal[j]) != 0.0
                    if is_entry:
                        event_shares = int(math.floor(allocated_capital / event_price)) if event_price > 0 else 0
                        current_shares = event_shares
                        entry_price = event_price
                    else:
                        event_shares = current_shares
                        exit_entry_price = entry_price
                        current_shares = 0
                        entry_price = None
                    events.append({"target": target, "action": "ENTRY" if is_entry else "EXIT", "price": event_price, "entry_price": event_price if is_entry else exit_entry_price, "shares": event_shares, "allocated_capital": allocated_capital, "bar_time": str(frame.index[j]), "strategy_id": spec["strategy_id"], "signal": float(signal[j])})
        current = float(signal[-1])
        close = float(frame["close"].iloc[-1])
        if current != 0.0 and current_shares <= 0:
            current_shares = int(math.floor(allocated_capital / close)) if close > 0 else 0
        if current == 0.0:
            current_shares = 0
            entry_price = None
        strategy_pnl[spec["strategy_id"]] = cumulative
        strategy_dollar_pnl[spec["strategy_id"]] = cumulative * allocated_capital
        strategy_allocations[spec["strategy_id"]] = allocated_capital
        state["strategies"][spec["strategy_id"]] = {"signal": current, "last_bar": str(bar_time), "last_price": close, "reference_shares": current_shares, "entry_price": entry_price, "position_value": current_shares * close, "allocated_capital": allocated_capital, "cumulative_pnl": cumulative, "dollar_pnl": cumulative * allocated_capital}

    total_weight = sum(float(s["weight"]) for s in STRATEGIES) or 1.0
    portfolio_pnl = sum(float(s["weight"]) * strategy_pnl[s["strategy_id"]] for s in STRATEGIES) / total_weight
    portfolio_dollar_pnl = portfolio_pnl * INITIAL_CAPITAL_USD
    current_portfolio_value = INITIAL_CAPITAL_USD + portfolio_dollar_pnl
    invested_value = sum(float(state["strategies"][s["strategy_id"]].get("position_value", 0.0)) for s in STRATEGIES)
    uninvested_cash = INITIAL_CAPITAL_USD - invested_value
    today = str(now.date())
    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_anchor_pnl"] = portfolio_pnl
        state["last_daily_summary"] = None
    daily_pnl = portfolio_pnl - float(state.get("daily_anchor_pnl", portfolio_pnl))
    daily_dollar_pnl = daily_pnl * INITIAL_CAPITAL_USD

    spec_by_id = {spec["strategy_id"]: spec for spec in STRATEGIES}

    def format_number(value):
        return f"{float(value):g}"

    def strategy_label(spec):
        return f"{spec['model']} / {spec['strategy']} / L{spec['length']} / T{format_number(spec['entry_threshold'])}"

    active_ids = {event["strategy_id"] for event in events}
    active_ids.update(sid for sid, item in state["strategies"].items() if float(item.get("signal", 0.0)) != 0.0)

    def append_portfolio_status(lines, include_holdings=True):
        lines.extend([
            "",
            "【Portfolio 狀態】",
            f"起始資金       ：${INITIAL_CAPITAL_USD:,.2f}",
            f"目前資金       ：${current_portfolio_value:,.2f}",
            f"已投入市值     ：${invested_value:,.2f}",
            f"未使用現金     ：${uninvested_cash:,.2f}",
            f"累積損益       ：{pct(portfolio_pnl)}（${portfolio_dollar_pnl:+,.2f}）",
            f"今日損益       ：{pct(daily_pnl)}（${daily_dollar_pnl:+,.2f}）",
        ])
        if not include_holdings:
            return
        lines.extend(["", "【目前持有策略】"])
        for spec in STRATEGIES:
            sid = spec["strategy_id"]
            if sid not in active_ids:
                continue
            current_state = state["strategies"][sid]
            shares = int(current_state.get("reference_shares", 0))
            entry = current_state.get("entry_price")
            current_price = float(current_state.get("last_price", 0.0))
            position_value = shares * current_price
            status = "持有中" if shares > 0 else "剛出場"
            lines.extend([
                f"{spec['target']}｜{status}｜{spec['strategy']}｜{spec['model']}",
                f"  進場價格：${float(entry):,.4f}" if entry is not None else "  進場價格：—（剛出場或尚未持有）",
                f"  目前價格：${current_price:,.4f}｜參考股數：{shares} 股",
                f"  持倉市值：${position_value:,.2f}｜策略損益：{pct(strategy_pnl[sid])}（${strategy_dollar_pnl[sid]:+,.2f}）",
            ])

    if events:
        lines = ["🔔 <b>AQS100｜新訊號</b>"]
        for event in events:
            spec = spec_by_id.get(event["strategy_id"], {})
            action_icon = "🟢" if event["action"] == "ENTRY" else "🔴"
            actual_value = event["shares"] * event["price"]
            unused_budget = max(event["allocated_capital"] - actual_value, 0.0)
            usage = actual_value / event["allocated_capital"] * 100 if event["allocated_capital"] else 0.0
            lines.extend([
                "",
                f"{action_icon} <b>{event['action']}｜{escape(str(event['target']))}</b>",
                f"📅 {escape(str(event['bar_time']))}",
                "",
                "━━━━━━━━━━━━━━",
                "🎯 <b>這次訊號</b>",
                "━━━━━━━━━━━━━━",
                "",
                f"訊號價格　<b>${event['price']:,.2f}</b>",
                f"進場價格　<b>${event['entry_price']:,.2f}</b>" if event.get('entry_price') is not None else "進場價格　—",
                f"參考股數　<b>{event['shares']} 股</b>",
                f"策略　　　<code>{escape(str(spec.get('model', '')))}</code>",
                f"參數　　　<code>L{spec.get('length', '')}</code>",
                f"門檻　　　<code>{format_number(spec.get('entry_threshold', 0))}</code>",
                "",
                "━━━━━━━━━━━━━━",
                "💰 <b>資金配置</b>",
                "━━━━━━━━━━━━━━",
                "",
                f"策略預算　　${event['allocated_capital']:,.2f}",
                f"實際持倉　　<b>${actual_value:,.2f}</b>",
                f"未使用預算　${unused_budget:,.2f}",
                f"預算使用率　<b>{usage:.1f}%</b>",
            ])
        lines.extend(["", "━━━━━━━━━━━━━━", "📊 <b>訊號後 Portfolio</b>", "━━━━━━━━━━━━━━"])
        append_portfolio_status(lines, include_holdings=False)
        send_telegram("\n".join(lines), chat_id=telegram_chat_id)

    is_trading_day = now.weekday() < 5 and latest_bar is not None and latest_bar.date() == now.date()
    if is_trading_day and now.hour >= 16 and state.get("last_daily_summary") != today:
        lines = [f"📊 <b>AQS100 每日摘要｜{today}</b>", "", "━━━━━━━━━━━━━━", "💰 <b>帳戶今天怎麼樣？</b>", "━━━━━━━━━━━━━━"]
        append_portfolio_status(lines)
        send_telegram("\n".join(lines), chat_id=telegram_chat_id)
        state["last_daily_summary"] = today

    state["last_bar"] = str(latest_bar)
    state["started"] = True
    save_state(state)
    print(json.dumps({"status": "OK", "bar": str(latest_bar), "events": events, "initial_capital_usd": INITIAL_CAPITAL_USD, "portfolio_cumulative_pnl": portfolio_pnl, "portfolio_cumulative_pnl_usd": portfolio_dollar_pnl, "portfolio_daily_pnl": daily_pnl, "portfolio_daily_pnl_usd": daily_dollar_pnl}, ensure_ascii=False))


if __name__ == "__main__":
    main()
