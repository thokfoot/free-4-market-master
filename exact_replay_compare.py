"""Event-level no-look-ahead comparison for bot swing/intraday signals."""
import argparse
import os
from collections import Counter

import pandas as pd

import market_data
from scanner import (
    compute_indicators,
    get_best_entries,
    get_yf_ticker,
    load_strategies,
    scan_strategies,
)
from scanner_intraday import (
    compute_indicators_1h,
    get_best_intraday_entries,
    get_yf_ticker as get_intraday_ticker,
    load_intraday_strategies,
    scan_intraday_strategies,
)

START = pd.Timestamp("2026-07-27", tz="UTC")
END = pd.Timestamp("2026-08-21 23:59:59", tz="UTC")


def _prepare(df):
    if df is None or df.empty:
        return df
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    index = pd.DatetimeIndex(df.index)
    df.index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def _event_key(row):
    rank = str(row.get("Pattern_Rank", "")).strip()
    try:
        rank = str(int(float(rank)))
    except (TypeError, ValueError):
        pass
    return (
        str(row.get("Date", ""))[:10],
        str(row.get("Ticker", "")),
        rank,
        str(row.get("Direction", "")).upper(),
        str(row.get("TimeFrame", "")),
    )


def _load_bot_entries(path):
    df = pd.read_csv(path, on_bad_lines="warn")
    entries = df.copy()
    entries = entries[entries["TimeFrame"].astype(str).isin(["SWING_1d", "INTRADAY_1h"])]
    return Counter(_event_key(row) for _, row in entries.iterrows())


def _download_history(tickers, interval, period):
    data = {}
    for ticker in tickers:
        try:
            data[ticker] = _prepare(
                market_data.download(
                    ticker, interval=interval, period=period
                )
            )
        except Exception as exc:
            print(f"[Replay] fetch failed {ticker}: {exc}")
            data[ticker] = None
    return data


def _events_for_day(strategies, history, intraday, scan_time):
    ticker_fn = get_intraday_ticker if intraday else get_yf_ticker
    ticker_data = {}
    minimum = 200 if intraday else 60
    for ticker, df in history.items():
        if df is None or len(df) < minimum:
            continue
        completed = df[df.index + (pd.Timedelta(hours=1) if intraday else pd.Timedelta(days=1)) <= scan_time]
        if len(completed) < minimum:
            continue
        ticker_data[ticker] = compute_indicators_1h(completed) if intraday else compute_indicators(completed)
    signals = scan_intraday_strategies(strategies, ticker_data) if intraday else scan_strategies(strategies, ticker_data)
    best = get_best_intraday_entries(signals) if intraday else get_best_entries(signals)
    tf = "INTRADAY_1h" if intraday else "SWING_1d"
    events = []
    for signal in best:
        events.append({
            "Date": scan_time.tz_convert("Asia/Kolkata").strftime("%Y-%m-%d"),
            "Signal_Time_UTC": scan_time.isoformat(),
            "Ticker": signal["ticker"],
            "Pattern_Rank": signal["rank"],
            "Direction": signal["direction"],
            "TimeFrame": tf,
            "Entry": signal.get("close", 0),
            "Factors": signal.get("factors", ""),
        })
    return events


def _scan_times(interval):
    dates = pd.date_range(START.normalize(), END.normalize(), freq="D", tz="UTC")
    if interval == "1d":
        return [d + pd.Timedelta(hours=1) for d in dates if d.weekday() < 5]
    return [
        d + pd.Timedelta(hours=h, minutes=30)
        for d in dates
        if d.weekday() < 5
        for h in range(13, 21)
    ]


def run(output, ledger):
    swing = load_strategies()
    intraday = load_intraday_strategies()
    swing_tickers = sorted({get_yf_ticker(str(x)) for x in swing["Market"] if get_yf_ticker(str(x))})
    intraday_tickers = sorted({get_intraday_ticker(str(x)) for x in intraday["Market"] if get_intraday_ticker(str(x))})
    history = {
        "swing": _download_history(swing_tickers, "1d", "2y"),
        "intraday": _download_history(intraday_tickers, "1h", "60d"),
    }
    events = []
    for scan_time in _scan_times("1d"):
        if START <= scan_time <= END:
            events.extend(_events_for_day(swing, history["swing"], False, scan_time))
    for scan_time in _scan_times("1h"):
        if START <= scan_time <= END:
            events.extend(_events_for_day(intraday, history["intraday"], True, scan_time))
    event_df = pd.DataFrame(events)
    event_df.to_csv(output, index=False)
    bot_keys = _load_bot_entries(ledger)
    replay = Counter(_event_key(row) for _, row in event_df.iterrows())
    matched = replay & bot_keys
    comparison = pd.DataFrame([
        {"result": "MATCH", "key": repr(key)} for key in matched.elements()
    ] + [
        {"result": "REPLAY_ONLY", "key": repr(key)} for key in (replay - bot_keys).elements()
    ] + [
        {"result": "BOT_ONLY", "key": repr(key)} for key in (bot_keys - replay).elements()
    ])
    compare_path = os.path.splitext(output)[0] + "_comparison.csv"
    comparison.to_csv(compare_path, index=False)
    print(f"EVENTS_REPLAYED {len(event_df)}")
    print(f"BOT_GENERIC_ENTRIES {len(bot_keys)}")
    print(f"MATCHES {sum(matched.values())}")
    print(f"REPLAY_ONLY {sum((replay - bot_keys).values())}")
    print(f"BOT_ONLY {sum((bot_keys - replay).values())}")
    print(f"EVENT_FILE {output}")
    print(f"COMPARISON_FILE {compare_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=r"C:\Users\Mind\Downloads\exact_replay_events_2026-07-27_to_2026-08-21.csv")
    parser.add_argument("--ledger", default=r"C:\Users\Mind\free-4-market-master\logs\paper_trades.csv")
    args = parser.parse_args()
    run(args.output, args.ledger)
