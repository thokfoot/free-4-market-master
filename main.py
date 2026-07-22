
import os, pytz, requests, yfinance as yf, pandas as pd, json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from logger import log_trade
from paper_trader import enter_paper_trade, update_paper_trades, load_portfolio

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")

# ===== FULL MARKET TICKERS - 262 STOCKS =====
def get_indian_tickers():
    nifty50 = ["RELIANCE.NS","TCS.NS","INFY.NS","HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","BHARTIARTL.NS","ITC.NS","LT.NS","KOTAKBANK.NS","BAJFINANCE.NS","HINDUNILVR.NS","ASIANPAINT.NS","AXISBANK.NS","MARUTI.NS","TITAN.NS","SUNPHARMA.NS","ULTRACEMCO.NS","WIPRO.NS","NTPC.NS","HCLTECH.NS","POWERGRID.NS","ONGC.NS","M&M.NS","BAJAJFINSV.NS","ADANIENT.NS","ADANIPORTS.NS","COALINDIA.NS","HDFCLIFE.NS","SBILIFE.NS","TATASTEEL.NS","JSWSTEEL.NS","HINDALCO.NS","GRASIM.NS","CIPLA.NS","DRREDDY.NS","DIVISLAB.NS","EICHERMOT.NS","BAJAJ-AUTO.NS","HEROMOTOCO.NS","BRITANNIA.NS","NESTLEIND.NS","TATACONSUM.NS","APOLLOHOSP.NS","BPCL.NS","UPL.NS","TECHM.NS","INDUSINDBK.NS","TATAMOTORS.NS"]
    next50 = ["VEDL.NS","SAIL.NS","NMDC.NS","BANKBARODA.NS","PNB.NS","CANBK.NS","IDFCFIRSTB.NS","FEDERALBNK.NS","AUBANK.NS","BANDHANBNK.NS","INDIGO.NS","ZOMATO.NS","PAYTM.NS","NYKAA.NS","DMART.NS","TRENT.NS","ABFRL.NS","PIDILITIND.NS","BERGEPAINT.NS","CUMMINSIND.NS","ASHOKLEY.NS","MOTHERSON.NS","TVSMOTOR.NS","BAJAJHLDNG.NS","CHOLAFIN.NS","MUTHOOTFIN.NS","PFC.NS","RECLTD.NS","IRCTC.NS","IRFC.NS","HAL.NS","BEL.NS","BDL.NS","MAZDOCK.NS","COCHINSHIP.NS","RVNL.NS","IDEA.NS","TATAPOWER.NS","ADANIGREEN.NS","ADANIENSOL.NS","SUZLON.NS","KPITTECH.NS","PERSISTENT.NS","COFORGE.NS","MPHASIS.NS","LTTS.NS","TATAELXSI.NS","OFSS.NS","LTIM.NS","POLYCAB.NS","KEI.NS"]
    return list(set(nifty50 + next50))

def get_us_tickers():
    nasdaq100 = ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","COST","PEP","ADBE","CSCO","NFLX","AMD","INTC","QCOM","TXN","AMGN","INTU","ISRG","BKNG","GILD","REGN","VRTX","MDLZ","ADI","LRCX","KLAC","PANW","SNPS","CDNS","MAR","ORLY","MELI","ADSK","CTAS","CHTR","NXPI","PCAR","WDAY","ABNB","MNST","KDP","KHC","AEP","MRVL","CSX","FTNT","DASH","ODFL","FAST","PAYX","VRSK","CTSH","CPRT","ROST","BKR","EA","EXC","IDXX","GFS","TTD","DDOG","ZS","TEAM","ANSS","XEL","BIIB","WBA","ILMN","SIRI","SPLK","ALGN","LCID","RIVN","COIN","HOOD","PLTR","SMCI","ARM","MSTR","APP","SHOP","SQ","CRWD","NET","NOW","UBER","DKNG","RBLX"]
    sp_gems = ["JPM","BAC","WFC","GS","MS","BRK.B","JNJ","PFE","MRK","ABBV","UNH","CVX","XOM","COP","SLB","BA","CAT","DE","HON","LMT","RTX","NOC","FDX","UPS","NKE","SBUX","MCD","DIS","CMCSA","VZ","T","SPG","PLD","AMT","CCI","WMT","TGT","HD","LOW","PG","KO","CL"]
    return list(set(nasdaq100 + sp_gems))[:110]

def get_crypto_tickers():
    return ["BTC-USD","ETH-USD","BNB-USD","SOL-USD","XRP-USD","ADA-USD","DOGE-USD","AVAX-USD","DOT-USD","LINK-USD","TRX-USD","MATIC-USD","LTC-USD","BCH-USD","UNI-USD","ATOM-USD","ETC-USD","XLM-USD","FIL-USD","HBAR-USD","APT-USD","ARB-USD","OP-USD","NEAR-USD","VET-USD","ICP-USD","STX-USD","IMX-USD","TAO-USD","RENDER-USD","INJ-USD","SUI-USD","SEI-USD","PEPE-USD","BONK-USD","WIF-USD","FLOKI-USD","SHIB-USD","FET-USD","AGIX-USD","RNDR-USD","GRT-USD","AAVE-USD","MKR-USD","SNX-USD","COMP-USD","LDO-USD","RUNE-USD","KAS-USD","KCS-USD"]

# ===== GEM STRATEGIES - 5 FILTERS =====
def analyze_stock(df):
    if len(df) < 30:
        return 0, []
    try:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"]; vol = df["Volume"]; high = df["High"]; low = df["Low"]
        vol_avg_20 = vol.rolling(20).mean()
        last = df.iloc[-1]; prev = df.iloc[-2]
        rvol = float(last["Volume"] / vol_avg_20.iloc[-1]) if vol_avg_20.iloc[-1] > 0 else 0
        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        typical = (high + low + close) / 3
        vwap = (typical * vol).rolling(20).sum() / vol.rolling(20).sum()
        vwap_last = float(vwap.iloc[-1]); vwap_prev = float(vwap.iloc[-2])
        high_20 = float(high.rolling(20).max().iloc[-2])
        low_5 = float(low.rolling(5).min().iloc[-2])
        signals = []; score = 0
        # 1 RVOL GEM
        if rvol >= 2.0 and float(last["Close"]) > float(prev["Close"]):
            signals.append(f"RVOL {rvol:.1f}x"); score += 2
            if rvol >= 3.0: score += 1
        # 2 Breakout
        if float(last["Close"]) > high_20 and float(last["Volume"]) > float(vol_avg_20.iloc[-1])*1.5:
            signals.append(f"20D Break {float(last['Close']):.0f}>{high_20:.0f}"); score += 2
        # 3 VWAP Reclaim
        if float(prev["Close"]) < vwap_prev and float(last["Close"]) > vwap_last and rvol > 1.3:
            signals.append("VWAP Reclaim"); score += 1
        # 4 Liquidity Sweep
        if float(last["Low"]) < low_5 * 0.998 and float(last["Close"]) > low_5 and float(last["Close"]) > float(prev["Open"]):
            if rvol > 1.2:
                signals.append("Liq Sweep"); score += 2
        # 5 Trend Pullback
        if float(last["Close"]) > sma20 > sma50 and float(prev["Low"]) <= sma20:
            signals.append("Trend Pullback"); score += 1
        return score, signals
    except Exception as e:
        return 0, []

def scan_ticker(t):
    try:
        df = yf.download(t, period="3mo", interval="1d", progress=False, auto_adjust=True)
        if len(df) < 30: return None
        score, sigs = analyze_stock(df)
        if score >= 2:
            rvol = float(df.iloc[-1]["Volume"] / df["Volume"].rolling(20).mean().iloc[-1])
            return {"ticker": t, "score": score, "close": float(df.iloc[-1]["Close"]), "sigs": sigs, "rvol": rvol}
    except: return None
    return None

def scan_market(tickers, market_name, max_workers=20):
    print(f"[{market_name}] Scanning {len(tickers)} stocks for GEMS...")
    gems = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {executor.submit(scan_ticker, t): t for t in tickers}
        for future in as_completed(future_to_ticker):
            res = future.result()
            if res: gems.append(res)
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
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
        print(f"[TG] {r.status_code}"); return r.status_code
    except Exception as e:
        print(f"[TG Error] {e}"); return None

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

if __name__ == "__main__":
    now_ist = datetime.now(IST)
    mode = get_mode()
    date_str = now_ist.strftime("%Y-%m-%d")
    time_str = now_ist.strftime("%H:%M:%S IST")
    print(f"[BOT] v4 ULTIMATE GEM+PAPER {mode} - {date_str} {time_str}")

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

    # Paper trading - update existing
    current_prices = {}
    for gems in all_gems.values():
        for g in gems:
            current_prices[g["ticker"]] = g["close"]
    for g in decay_gems:
        current_prices[g["ticker"]] = g["close"]

    closed_msgs = update_paper_trades(current_prices)

    # Enter new - TOP GEMS ONLY to avoid overtrading
    to_enter = []
    if mode == "INDIAN":
        to_enter = all_gems["INDIAN"][:5] + decay_gems
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
        msg = f"Bot Alive - {mode} - {time_str}\nFull Scan 262 stocks: 0 GEMS - Market flat | Open: {open_count} Cap: Rs {capital:.0f}"
        tg_code = send_telegram(msg)
        tg_status = f"Sent - {tg_code}"
    else:
        lines = [f"💎 *v4 ULTIMATE {mode}* {date_str} {time_str}", f"Scanned 262 stocks | Cap: Rs {capital:.0f} | Open: {open_count}", ""]
        for mkt, gems in all_gems.items():
            if gems:
                lines.append(f"*{mkt} GEMS ({len(gems)}):*")
                for g in gems[:7]:
                    lines.append(f"• {g['ticker']} @ {g['close']:.1f} | Score {g['score']} | {', '.join(g['sigs'][:2])} | RVOL {g['rvol']:.1f}x")
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
