import pandas as pd
import numpy as np
import itertools, os

TICKERS = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "QQQ", "SPY", "BTC-USD", "ETH-USD"]

def wilder_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_indicators(df):
    df['SMA20'] = df['Close'].rolling(20).mean()
    df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['RSI14'] = wilder_rsi(df['Close'])
    df['2Red'] = (df['Close'] < df['Open']) & (df['Close'].shift(1) < df['Open'].shift(1))
    df['2Green'] = (df['Close'] > df['Open']) & (df['Close'].shift(1) > df['Open'].shift(1))
    return df.dropna()

def backtest_winrate(df, signal, side='LONG'):
    SL, TP = 0.003, 0.006 # 1m ke liye 0.3% SL 0.6% TP rakha
    trades = []
    for i in range(len(df)-20):
        if not signal.iloc[i]: continue
        entry = df['Close'].iloc[i]
        win = False
        for j in range(1, 20): # next 20 candles me dekho (20 min)
            h = df['High'].iloc[i+j]
            l = df['Low'].iloc[i+j]
            if side == 'LONG':
                if l <= entry*(1-SL): win=False; break
                if h >= entry*(1+TP): win=True; break
            else:
                if h >= entry*(1+SL): win=False; break
                if l <= entry*(1-TP): win=True; break
        trades.append(win)
    if len(trades) < 30: return 0,0
    return round(sum(trades)/len(trades)*100,2), len(trades)

# --- MAIN MINING ---
for t in TICKERS:
    path = f"data/minute_{t}_1m.csv"
    if not os.path.exists(path): continue
    df = pd.read_csv(path)
    # yfinance csv me Date/Datetime column hota hai
    time_col = 'Datetime' if 'Datetime' in df.columns else 'Date'
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.set_index(time_col).sort_index()
    df = compute_indicators(df)

    unique_days = sorted(df.index.normalize().unique())[-6:]
    if len(unique_days) < 6:
        print(f"{t} me 6 din nahi mile")
        continue
    learn_days = unique_days[:3]
    test_days = unique_days[3:]

    learn_df = df[df.index.normalize().isin(learn_days)]
    test_df = df[df.index.normalize().isin(test_days)]

    print(f"\n{t} LEARN {learn_days[0].date()}->{learn_days[-1].date()} ({len(learn_df)}) | TEST {test_days[0].date()}->{test_days[-1].date()} ({len(test_df)})")

    conds = {
        'c>SMA20': learn_df['Close'] > learn_df['SMA20'],
        'c<SMA20': learn_df['Close'] < learn_df['SMA20'],
        'EMA9>EMA20': learn_df['EMA9'] > learn_df['EMA20'],
        'EMA9<EMA20': learn_df['EMA9'] < learn_df['EMA20'],
        'RSI<35': learn_df['RSI14'] < 35,
        'RSI>65': learn_df['RSI14'] > 65,
        '2Red': learn_df['2Red'],
        '2Green': learn_df['2Green'],
    }
    # same conds for test
    conds_test = {
        'c>SMA20': test_df['Close'] > test_df['SMA20'],
        'c<SMA20': test_df['Close'] < test_df['SMA20'],
        'EMA9>EMA20': test_df['EMA9'] > test_df['EMA20'],
        'EMA9<EMA20': test_df['EMA9'] < test_df['EMA20'],
        'RSI<35': test_df['RSI14'] < 35,
        'RSI>65': test_df['RSI14'] > 65,
        '2Red': test_df['2Red'],
        '2Green': test_df['2Green'],
    }

    for r in [2,3,4]: # 2-4 factor combo as you said
        for combo in itertools.combinations(conds.keys(), r):
            sig_learn = pd.Series(True, index=learn_df.index)
            for c in combo: sig_learn &= conds[c]

            wr_learn, tr_learn = backtest_winrate(learn_df, sig_learn, 'LONG')
            if wr_learn < 60: continue

            # ab same combo TEST pe check
            sig_test = pd.Series(True, index=test_df.index)
            for c in combo: sig_test &= conds_test[c]
            wr_test, tr_test = backtest_winrate(test_df, sig_test, 'LONG')

            if wr_test >= 60:
                print(f" FOUND LONG {combo} | LEARN {wr_learn}% ({tr_learn}) -> TEST {wr_test}% ({tr_test})")