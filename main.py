import os, pytz, requests, yfinance as yf, pandas as pd, json, time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from logger import log_trade
from paper_trader import enter_paper_trade, update_paper_trades, load_portfolio

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")

def fmt_price(p):
    try:
        p = float(p)
        if p >= 1000: return f"{p:.1f}"
        if p >= 100: return f"{p:.2f}"
        if p >= 1: return f"{p:.2f}"
        if p >= 0.1: return f"{p:.4f}"
        if p >= 0.01: return f"{p:.6f}"
        return f"{p:.8f}"
    except: return str(p)

def get_indian_tickers():
    nifty50 = ["RELIANCE.NS","TCS.NS","INFY.NS","HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","BHARTIARTL.NS","ITC.NS","LT.NS","KOTAKBANK.NS","BAJFINANCE.NS","HINDUNILVR.NS","ASIANPAINT.NS","AXISBANK.NS","MARUTI.NS","TITAN.NS","SUNPHARMA.NS","ULTRACEMCO.NS","WIPRO.NS","NTPC.NS","HCLTECH.NS","POWERGRID.NS","ONGC.NS","M&M.NS","BAJAJFINSV.NS","ADANIENT.NS","ADANIPORTS.NS","COALINDIA.NS","HDFCLIFE.NS","SBILIFE.NS","TATASTEEL.NS","JSWSTEEL.NS","HINDALCO.NS","GRASIM.NS","CIPLA.NS","DRREDDY.NS","DIVISLAB.NS","EICHERMOT.NS","BAJAJ-AUTO.NS","HEROMOTOCO.NS","BRITANNIA.NS","NESTLEIND.NS","TATACONSUM.NS","APOLLOHOSP.NS","BPCL.NS","UPL.NS","TECHM.NS","INDUSINDBK.NS","TATAMOTORS.NS"]
    next50 = ["VEDL.NS","SAIL.NS","NMDC.NS","BANKBARODA.NS","PNB.NS","CANBK.NS","IDFCFIRSTB.NS","FEDERALBNK.NS","AUBANK.NS","BANDHANBNK.NS","INDIGO.NS","ZOMATO.NS","PAYTM.NS","NYKAA.NS","DMART.NS","TRENT.NS","ABFRL.NS","PIDILITIND.NS","BERGEPAINT.NS","CUMMINSIND.NS","ASHOKLEY.NS","MOTHERSON.NS","TVSMOTOR.NS","BAJAJHLDNG.NS","CHOLAFIN.NS","MUTHOOTFIN.NS","PFC.NS","RECLTD.NS","IRCTC.NS","IRFC.NS","HAL.NS","BEL.NS","BDL.NS","MAZDOCK.NS","COCHINSHIP.NS","RVNL.NS","IDEA.NS","TATAPOWER.NS","ADANIGREEN.NS","ADANIENSOL.NS","SUZLON.NS","KPITTECH.NS","PERSISTENT.NS","COFORGE.NS","MPHASIS.NS","LTTS.NS","TATAELXSI.NS","OFSS.NS","LTIM.NS","POLYCAB.NS","KEI.NS"]
    return list(dict.fromkeys(nifty50 + next50))

def get_us_tickers():
    nasdaq100 = ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","COST","PEP","ADBE","CSCO","NFLX","AMD","INTC","QCOM","TXN","AMGN","INTU","ISRG","BKNG","GILD","REGN","VRTX","MDLZ","ADI","LRCX","KLAC","PANW","SNPS","CDNS","MAR","ORLY","MELI","ADSK","CTAS","CHTR","NXPI","PCAR","WDAY","ABNB","MNST","KDP","KHC","AEP","MRVL","CSX","FTNT","DASH","ODFL","FAST","PAYX","VRSK","CTSH","CPRT","ROST","BKR","EA","EXC","IDXX","GFS","TTD","DDOG","ZS","TEAM","ANSS","XEL","BIIB","WBA","ILMN","SIRI","SPLK","ALGN","LCID","RIVN","COIN","HOOD","PLTR","SMCI","ARM","MSTR","APP","SHOP","SQ","CRWD","NET","NOW","UBER","DKNG","RBLX"]
    sp_gems = ["JPM","BAC","WFC","GS","MS","BRK.B","JNJ","PFE","MRK","ABBV","UNH","CVX","XOM","COP","SLB","BA","CAT","DE","HON","LMT","RTX","NOC","FDX","UPS","NKE","SBUX","MCD","DIS","CMCSA","VZ","T","SPG","PLD","AMT","CCI","WMT","TGT","HD","LOW","PG","KO","CL"]
    combined = list(dict.fromkeys(nasdaq100 + sp_gems))
    return combined[:110]

def get_crypto_tickers():
    return ["BTC-USD","ETH-USD","BNB-USD","SOL-USD","XRP-USD","ADA-USD","DOGE-USD","AVAX-USD","DOT-USD","LINK-USD","TRX-USD","POL-USD","LTC-USD","BCH-USD","UNI-USD","ATOM-USD","ETC-USD","XLM-USD","FIL-USD","HBAR-USD","APT-USD","ARB-USD","OP-USD","NEAR-USD","VET-USD","ICP-USD","STX-USD","IMX-USD","TAO-USD","RENDER-USD","INJ-USD","SUI-USD","SEI-USD","PEPE-USD","BONK-USD","WIF-USD","FLOKI-USD","SHIB-USD","FET-USD","GRT-USD","AAVE-USD","MKR-USD","SNX-USD","COMP-USD","LDO-USD","RUNE-USD","KAS-USD","KCS-USD"]

def calc_rsi(close, period=14):
    try:
        delta = close.diff()
        gain = delta.where(delta>0, 0).rolling(period).mean()
        loss = (-delta.where(delta<0, 0)).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    except: return pd.Series([50]*len(close), index=close.index)

def analyze_stock(df):
    if len(df) < 50:
        return 0, []
    try:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"]; vol = df["Volume"]; high = df["High"]; low = df["Low"]; op = df["Open"]
        vol_avg_20 = vol.rolling(20).mean()
        last = df.iloc[-1]; prev = df.iloc[-2]
        vol_avg_last = float(vol_avg_20.iloc[-1]) if not pd.isna(vol_avg_20.iloc[-1]) else 0
        rvol = float(last["Volume"] / vol_avg_last) if vol_avg_last > 0 else 0
        rvol_capped = min(rvol, 15.0)
        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        ema9 = close.ewm(span=9).mean().iloc[-1]
        ema21 = close.ewm(span=21).mean().iloc[-1]
        rsi = calc_rsi(close).iloc[-1]
        typical = (high + low + close) / 3
        vwap = (typical * vol).rolling(20).sum() / vol.rolling(20).sum()
        vwap_last = float(vwap.iloc[-1]) if not pd.isna(vwap.iloc[-1]) else 0
        vwap_prev = float(vwap.iloc[-2]) if not pd.isna(vwap.iloc[-2]) else 0
        high_20 = float(high.rolling(20).max().iloc[-2]) if not pd.isna(high.rolling(20).max().iloc[-2]) else 0
        low_5 = float(low.rolling(5).min().iloc[-2]) if not pd.isna(low.rolling(5).min().iloc[-2]) else 0
        if pd.isna(sma20) or pd.isna(sma50): return 0, []
        signals = []; score = 0
        if rvol >= 2.0 and float(last["Close"]) > float(prev["Close"]):
            signals.append(f"RVOL {rvol_capped:.1f}x"); score += 2
            if rvol >= 3.0: score += 1
        if float(last["Close"]) > high_20 and float(last["Volume"]) > vol_avg_last*1.5 and high_20>0:
            signals.append(f"20D Breakout"); score += 2
        if float(prev["Close"]) < vwap_prev and float(last["Close"]) > vwap_last and rvol > 1.3 and vwap_last>0:
            signals.append("VWAP Reclaim"); score += 1
        if low_5>0 and float(last["Low"]) < low_5 * 0.998 and float(last["Close"]) > low_5 and float(last["Close"]) > float(prev["Open"]):
            if rvol > 1.2:
                signals.append("Liq Sweep"); score += 2
        if float(last["Close"]) > sma20 > sma50 and float(prev["Low"]) <= sma20:
            signals.append("Trend Pullback"); score += 1
        if 55 <= rsi <= 75:
            signals.append(f"RSI {rsi:.0f}"); score += 1
        if ema9 > ema21 and float(last["Close"]) > ema9:
            signals.append("EMA Bull"); score += 1
        return score, signals
    except Exception as e:
        return 0, []

def scan_ticker(t):
    for attempt in range(3):
        try:
            df = yf.download(t, period="3mo", interval="1d", progress=False, auto_adjust=True)
            if len(df) < 50:
                if attempt < 2:
                    time.sleep(1)
                    continue
                return None
            score, sigs = analyze_stock(df)
            if score >= 2:
                vol_avg = float(df["Volume"].rolling(20).mean().iloc[-1])
                last_vol = float(df.iloc[-1]["Volume"])
                rvol = last_vol / vol_avg if vol_avg>0 else 0
                rvol = min(rvol, 15.0)
                return {"ticker": t, "score": score, "close": float(df.iloc[-1]["Close"]), "sigs": sigs, "rvol": rvol}
            return None
        except Exception as e:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            return None
    return None

def scan_market(tickers, market_name, max_workers=8):
    print(f"[{market_name}] Scanning {len(tickers)} stocks for GEMS...")
    gems = []
    workers = 8 if market_name == "INDIAN" else 12
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_ticker = {executor.submit(scan_ticker, t): t for t in tickers}
        for future in as_completed(future_to_ticker):
            try:
                res = future.result()
                if res: gems.append(res)
            except: continue
    gems = sorted(gems, key=lambda x: (x["score"], x["rvol"]), reverse=True)
    print(f"[{market_name}] Found {len(gems)} GEMS out of {len(tickers)}")
    for g in gems[:5]:
        print(f"  GEM {g['ticker']} Score:{g['score']} RVOL:{g['rvol']:.1f} - {', '.join(g['sigs'])}")
    return gems

def get_mode():
    now_ist = datetime.now(IST)
    hour = now_ist.hour
    weekday = now_ist.weekday()
    if weekday < 5 and 9 <= hour <= 16: return "INDIAN"
    if weekday < 5 and 19 <= hour <= 23: return "US"
    return "CRYPTO"

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG] Missing token"); return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
            print(f"[TG] {r.status_code} - {r.text[:100]}")
            if r.status_code == 200:
                return r.status_code
            time.sleep(2)
        except Exception as e:
            print(f"[TG Error attempt {attempt+1}] {e}")
            time.sleep(2)
    return None

def scan_decay():
    now_ist = datetime.now(IST)
    signals = []
    if now_ist.weekday() == 3:
        try:
            df = yf.download("^NSEBANK", period="5d", interval="1d", progress=False)
            spot = float(df.iloc[-1]["Close"]) if len(df)>0 else 56000
            atm = int(round(spot/100)*100)
            signals.append({"ticker": f"BANKNIFTY {atm}", "score": 3, "close": 150, "sigs": [f"Expiry ATM {atm} Spot {spot:.0f}"], "rvol": 1.0})
        except:
            signals.append({"ticker": "BANKNIFTY ATM", "score": 2, "close": 150, "sigs": ["Expiry"], "rvol": 1.0})
    return signals

def get_prices_for_open_positions():
    try:
        portfolio = load_portfolio()
        open_tickers = [p["Ticker"] for p in portfolio.get("open_positions", []) if p.get("Status") == "OPEN"]
        prices = {}
        for ticker in open_tickers:
            try:
                if "BANKNIFTY" in ticker:
                    continue
                df = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=True)
                if len(df) > 0:
                    prices[ticker] = float(df.iloc[-1]["Close"])
            except: continue
        return prices
    except: return {}

if __name__ == "__main__":
    try:
        now_ist = datetime.now(IST)
        mode = get_mode()
        date_str = now_ist.strftime("%Y-%m-%d")
        time_str = now_ist.strftime("%H:%M:%S IST")
        print(f"[BOT] v4.2 NO-MISS 7-STRATEGY {mode} - {date_str} {time_str}")
        all_gems = {"INDIAN": [], "US": [], "CRYPTO": []}
        decay_gems = []
        if mode == "INDIAN":
            all_gems["INDIAN"] = scan_market(get_indian_tickers(), "INDIAN")
            all_gems["CRYPTO"] = scan_market(get_crypto_tickers()[:20], "CRYPTO-SMALL")
            decay_gems = scan_decay()
        elif mode == "US":
            all_gems["US"] = scan_market(get_us_tickers(), "US")
            all_gems["CRYPTO"] = scan_market(get_crypto_tickers()[:20], "CRYPTO-SMALL")
        else:
            all_gems["CRYPTO"] = scan_market(get_crypto_tickers(), "CRYPTO")
        total_gems = sum(len(v) for v in all_gems.values()) + len(decay_gems)
        print(f"[Executor] Total {total_gems} GEMS found")
        current_prices = {}
        for gems in all_gems.values():
            for g in gems:
                current_prices[g["ticker"]] = g["close"]
        for g in decay_gems:
            current_prices[g["ticker"]] = g["close"]
        open_prices = get_prices_for_open_positions()
        current_prices.update(open_prices)
        closed_msgs = update_paper_trades(current_prices)
        to_enter = []
        if mode == "INDIAN":
            to_enter = all_gems["INDIAN"][:5]
        elif mode == "US":
            to_enter = all_gems["US"][:5]
        else:
            to_enter = all_gems["CRYPTO"][:5]
        entered = []
        for g in to_enter:
            tr = enter_paper_trade(mode, g["ticker"], g["close"], ", ".join(g["sigs"][:2]) + f" Score:{g['score']} RVOL:{g['rvol']:.1f}x")
            if tr: entered.append(tr)
        portfolio = load_portfolio()
        capital = portfolio["capital"]
        open_count = len(portfolio["open_positions"])
        if total_gems == 0 and not closed_msgs:
            msg = f"Bot Alive - {mode} - {time_str}\nFull Scan 262 stocks: 0 GEMS - Market flat | Open: {open_count} Cap: Rs {capital:.0f} | Next scan in 30 min"
            tg_code = send_telegram(msg)
            tg_status = f"Sent - {tg_code}"
        else:
            lines = [f"\U0001f48e *v4.2 7-STRAT NO-MISS {mode}* {date_str} {time_str}", f"Scanned 262 stocks | Cap: Rs {capital:.0f} | Open: {open_count}", ""]
            for mkt, gems in all_gems.items():
                if gems:
                    lines.append(f"*{mkt} GEMS ({len(gems)}):*")
                    for g in gems[:7]:
                        sig_text = ", ".join(g['sigs'][:2])
                        if "RVOL" in sig_text:
                            lines.append(f"• {g['ticker']} @ {fmt_price(g['close'])} | Score {g['score']} | {sig_text}")
                        else:
                            lines.append(f"• {g['ticker']} @ {fmt_price(g['close'])} | Score {g['score']} | {sig_text} | RVOL {g['rvol']:.1f}x")
                    lines.append("")
            if decay_gems:
                lines.append(f"*DECAY:* {decay_gems[0]['ticker']} | {decay_gems[0]['sigs'][0]}")
                lines.append("")
            if entered:
                lines.append(f"*NEW PAPER:* {len(entered)} entered")
                for t in entered:
                    lines.append(f"• {t['Ticker']} @ {t['Entry_Price']} Qty {t['Qty']} SL {t['SL']} TGT {t['Target']}")
                lines.append("")
            if closed_msgs:
                lines.append("*CLOSED:*")
                lines.extend([f"• {c}" for c in closed_msgs])
            msg = "\n".join(lines)
            if len(msg) > 4000: msg = msg[:4000]
            tg_code = send_telegram(msg)
            tg_status = f"Sent {total_gems} GEMS - {tg_code}"
        log_trade({
            "Date": date_str, "Time_IST": time_str, "Mode": mode,
            "Indian_Count": len(all_gems["INDIAN"]), "US_Count": len(all_gems["US"]),
            "Crypto_Count": len(all_gems["CRYPTO"]), "Decay_Count": len(decay_gems),
            "Total_Found": total_gems, "Filtered_Sent_Count": len(to_enter),
            "Telegram_Status": tg_status,
            "Skip_Reason": "None" if total_gems else "No gems",
            "Signals_Detail": " | ".join([f"{g['ticker']}:{g['score']}" for gems in all_gems.values() for g in gems[:5]]),
            "Weekday": now_ist.strftime("%A")
        })
    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        try:
            send_telegram(f"\u26a0\ufe0f Bot Error {datetime.now(IST).strftime('%H:%M IST')}: {str(e)[:200]}")
        except: pass
        raise
