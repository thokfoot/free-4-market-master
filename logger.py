
import os, pandas as pd
LOG_DIR = "logs"
CSV_FILE = os.path.join(LOG_DIR, "trade_log.csv")
EXCEL_FILE = os.path.join(LOG_DIR, "trade_log.xlsx")
COLUMNS = ["Date","Time_IST","Mode","Indian_Count","US_Count","Crypto_Count","Decay_Count","Total_Found","Filtered_Sent_Count","Telegram_Status","Skip_Reason","Signals_Detail","Weekday"]

def log_trade(row_dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    df_new = pd.DataFrame([row_dict])[COLUMNS]
    if os.path.exists(CSV_FILE):
        df_old = pd.read_csv(CSV_FILE)
        df_combined = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_combined = df_new
    df_combined.to_csv(CSV_FILE, index=False)
    print(f"[Logger] CSV rows {len(df_combined)}")
    try:
        summary = df_combined.groupby("Date").agg({
            "Total_Found":"sum","Filtered_Sent_Count":"sum",
            "Indian_Count":"sum","US_Count":"sum","Crypto_Count":"sum","Decay_Count":"sum"
        }).reset_index()
        with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as writer:
            df_combined.to_excel(writer, sheet_name="All Logs", index=False)
            summary.to_excel(writer, sheet_name="Daily Summary", index=False)
            pf = os.path.join(LOG_DIR, "paper_trades.csv")
            if os.path.exists(pf):
                pd.read_csv(pf).to_excel(writer, sheet_name="Paper Trades", index=False)
        print(f"[Logger] Excel updated")
    except Exception as e:
        print(f"[Logger Excel Error] {e}")
