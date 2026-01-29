import streamlit as st
import pandas as pd
import sqlite3
import requests
import yfinance as yf
from datetime import date, timedelta
import numpy as np

# ================= CONFIG =================
DB_NAME = "portfolio.db"
POLYGON_KEY = st.secrets["POLYGON_API_KEY"]

st.set_page_config(page_title="📊 Portfolio Tracker", layout="wide")

# ================= DB =================
conn = sqlite3.connect(DB_NAME, check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    date TEXT,
    portfolio TEXT,
    ticker TEXT,
    market TEXT,
    action TEXT,
    quantity REAL,
    price REAL,
    currency TEXT
)
""")
conn.commit()

# ---- MIGRATION SAFE ----
def migrate_db():
    cols = [row[1] for row in c.execute("PRAGMA table_info(transactions)").fetchall()]
    if "currency" not in cols:
        c.execute("ALTER TABLE transactions ADD COLUMN currency TEXT DEFAULT 'CAD'")
    conn.commit()

migrate_db()

# ================= HELPERS =================
def normalize_ticker(ticker, market):
    if market == "CA" and not ticker.upper().endswith(".TO"):
        return ticker.upper() + ".TO"
    return ticker.upper()

# ================= FX =================
@st.cache_data(ttl=3600)
def get_fx():
    fx = yf.Ticker("USDCAD=X").history(period="1d")
    return float(fx["Close"].iloc[-1])

FX = get_fx()

# ================= OHLC =================
def get_ohlc_us(ticker, d):
    ds = d.strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v1/open-close/{ticker}/{ds}?adjusted=true&apiKey={POLYGON_KEY}"
    r = requests.get(url).json()
    if r.get("status") != "OK":
        return None
    return {"Close": r["close"]}

def get_ohlc_ca(ticker, d):
    ticker = normalize_ticker(ticker, "CA")
    df = yf.Ticker(ticker).history(start=d, end=d + timedelta(days=1))
    if df.empty:
        return None
    return {"Close": float(df.iloc[0]["Close"])}

def get_ohlc(ticker, market, d):
    return get_ohlc_us(ticker, d) if market == "US" else get_ohlc_ca(ticker, d)

# ================= TRANSACTIONS =================
def add_tx(d, portfolio, ticker, market, action, qty, price, currency):
    ticker = normalize_ticker(ticker, market)
    c.execute("""
        INSERT INTO transactions (
            date, portfolio, ticker, market, action, quantity, price, currency
        ) VALUES (?,?,?,?,?,?,?,?)
    """, (d, portfolio, ticker, market, action, qty, price, currency))
    conn.commit()

def delete_tx(rowid):
    c.execute("DELETE FROM transactions WHERE rowid=?", (rowid,))
    conn.commit()

# ================= CASH =================
def get_cash(portfolio):
    df = pd.read_sql(
        """
        SELECT * FROM transactions
        WHERE portfolio = ?
        AND action IN ('CASH_DEPOSIT','CASH_WITHDRAW','DIVIDEND')
        """,
        conn,
        params=(portfolio,)
    )

    if df.empty:
        return {"CAD": 0.0, "USD": 0.0}

    df["signed"] = df.apply(
        lambda x: x["quantity"] if x["action"] in ("CASH_DEPOSIT","DIVIDEND") else -x["quantity"],
        axis=1
    )
    return df.groupby("currency")["signed"].sum().to_dict()

# ================= POSITIONS =================
def load_positions(portfolio):
    df = pd.read_sql(
        """
        SELECT * FROM transactions
        WHERE portfolio = ?
        AND action IN ('BUY','SELL')
        """,
        conn,
        params=(portfolio,)
    )

    if df.empty:
        return pd.DataFrame()

    df["signed_qty"] = df.apply(
        lambda x: x["quantity"] if x["action"] == "BUY" else -x["quantity"],
        axis=1
    )

    pos = df.groupby(["ticker","market","currency"]).agg(
        quantity=("signed_qty","sum"),
        avg_price=("price","mean")
    ).reset_index()

    return pos[pos["quantity"] > 0]

# ================= PORTFOLIO VALUE =================
def portfolio_value_at_date(portfolio, d):
    df = pd.read_sql(
        "SELECT * FROM transactions WHERE portfolio = ? AND date <= ?",
        conn,
        params=(portfolio, d.strftime("%Y-%m-%d"))
    )

    cash = {"CAD": 0.0, "USD": 0.0}
    positions = {}

    for _, r in df.iterrows():
        if r["action"] == "CASH_DEPOSIT":
            cash[r["currency"]] += r["quantity"]
        elif r["action"] == "CASH_WITHDRAW":
            cash[r["currency"]] -= r["quantity"]
        elif r["action"] == "BUY":
            positions.setdefault((r["ticker"], r["market"], r["currency"]), 0)
            positions[(r["ticker"], r["market"], r["currency"])] += r["quantity"]
        elif r["action"] == "SELL":
            positions.setdefault((r["ticker"], r["market"], r["currency"]), 0)
            positions[(r["ticker"], r["market"], r["currency"])] -= r["quantity"]

    total = cash["CAD"] + cash["USD"] * FX

    for (ticker, market, currency), qty in positions.items():
        if qty <= 0:
            continue
        ohlc = get_ohlc(ticker, market, d)
        if not ohlc:
            continue
        value = ohlc["Close"] * qty
        total += value if currency == "CAD" else value * FX

    return total

def portfolio_timeseries(portfolio):
    df = pd.read_sql(
        "SELECT MIN(date) as start, MAX(date) as end FROM transactions WHERE portfolio = ?",
        conn,
        params=(portfolio,)
    )

    if df.iloc[0]["start"] is None:
        return pd.DataFrame()

    dates = pd.date_range(df.iloc[0]["start"], df.iloc[0]["end"], freq="D")
    return pd.DataFrame({
        "Date": dates,
        "Valeur": [portfolio_value_at_date(portfolio, d) for d in dates]
    })

# ================= BENCHMARK =================
@st.cache_data(ttl=3600)
def benchmark_series(symbol, start, end):
    df = yf.Ticker(symbol).history(start=start, end=end)
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index()[["Date", "Close"]]
    df["Valeur"] = df["Close"] / df["Close"].iloc[0] * 100
    return df[["Date", "Valeur"]]

# ================= UI =================
st.title("📊 Portfolio Tracker")

st.subheader("📈 Évolution comparée + Benchmark")

series = []
start_dates = []

for p in ["ETF","CROISSANCE","RISQUE"]:
    ts = portfolio_timeseries(p)
    if not ts.empty:
        ts["Valeur"] = ts["Valeur"] / ts["Valeur"].iloc[0] * 100
        ts["Label"] = p
        series.append(ts)
        start_dates.append(ts["Date"].min())

if series:
    start = min(start_dates)
    end = date.today()

    sp500 = benchmark_series("^GSPC", start, end)
    tsx = benchmark_series("^GSPTSE", start, end)

    if not sp500.empty:
        sp500["Label"] = "S&P 500"
        series.append(sp500)

    if not tsx.empty:
        tsx["Label"] = "TSX Composite"
        series.append(tsx)

    df_all = pd.concat(series)
    chart_df = df_all.pivot(index="Date", columns="Label", values="Valeur")

    st.line_chart(chart_df)
else:
    st.info("Pas assez de données pour afficher les courbes.")
