import streamlit as st
import pandas as pd
import sqlite3
import yfinance as yf
from datetime import date, timedelta
import numpy as np

# ================= CONFIG =================
DB_NAME = "portfolio.db"
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

def migrate_db():
    cols = [r[1] for r in c.execute("PRAGMA table_info(transactions)")]
    if "currency" not in cols:
        c.execute("ALTER TABLE transactions ADD COLUMN currency TEXT DEFAULT 'CAD'")
    conn.commit()

migrate_db()

# ================= HELPERS =================
def normalize_ticker(ticker, market):
    if market == "CA" and not ticker.upper().endswith(".TO"):
        return ticker.upper() + ".TO"
    return ticker.upper()

@st.cache_data(ttl=3600)
def get_fx():
    return float(yf.Ticker("USDCAD=X").history(period="1d")["Close"].iloc[-1])

FX = get_fx()

# ================= OHLC =================
def get_ohlc(ticker, market, d):
    ticker = normalize_ticker(ticker, market)
    df = yf.Ticker(ticker).history(start=d, end=d + timedelta(days=1))
    if df.empty:
        return None
    r = df.iloc[0]
    return {
        "Open": float(r["Open"]),
        "High": float(r["High"]),
        "Low": float(r["Low"]),
        "Close": float(r["Close"])
    }

# ================= TRANSACTIONS =================
def add_tx(d, portfolio, ticker, market, action, qty, price, currency):
    ticker = normalize_ticker(ticker, market)
    c.execute("""
        INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)
    """, (d, portfolio, ticker, market, action, qty, price, currency))
    conn.commit()

def delete_tx(rowid):
    c.execute("DELETE FROM transactions WHERE rowid=?", (rowid,))
    conn.commit()

# ================= CASH =================
def get_cash(portfolio):
    df = pd.read_sql(
        "SELECT * FROM transactions WHERE portfolio=? AND action IN ('CASH_DEPOSIT','CASH_WITHDRAW','DIVIDEND')",
        conn, params=(portfolio,)
    )
    cash = {"CAD": 0.0, "USD": 0.0}
    for _, r in df.iterrows():
        sign = 1 if r["action"] in ("CASH_DEPOSIT","DIVIDEND") else -1
        cash[r["currency"]] += sign * r["quantity"]
    return cash

# ================= POSITIONS =================
def load_positions(portfolio):
    df = pd.read_sql(
        "SELECT * FROM transactions WHERE portfolio=? AND action IN ('BUY','SELL')",
        conn, params=(portfolio,)
    )
    if df.empty:
        return pd.DataFrame()

    df["signed"] = df.apply(lambda r: r["quantity"] if r["action"]=="BUY" else -r["quantity"], axis=1)
    pos = df.groupby(["ticker","market","currency"]).agg(
        quantity=("signed","sum"),
        avg_price=("price","mean")
    ).reset_index()
    return pos[pos["quantity"]>0]

# ================= PORTFOLIO VALUE =================
def portfolio_value_at_date(portfolio, d):
    df = pd.read_sql(
        "SELECT * FROM transactions WHERE portfolio=? AND date<=?",
        conn, params=(portfolio, d.strftime("%Y-%m-%d"))
    )
    cash = {"CAD":0.0,"USD":0.0}
    pos = {}
    for _, r in df.iterrows():
        if r["action"]=="CASH_DEPOSIT": cash[r["currency"]] += r["quantity"]
        if r["action"]=="CASH_WITHDRAW": cash[r["currency"]] -= r["quantity"]
        if r["action"]=="BUY": pos[r["ticker"]] = pos.get(r["ticker"],0)+r["quantity"]
        if r["action"]=="SELL": pos[r["ticker"]] = pos.get(r["ticker"],0)-r["quantity"]

    total = cash["CAD"] + cash["USD"]*FX
    for t, q in pos.items():
        if q<=0: continue
        ohlc = get_ohlc(t,"CA" if t.endswith(".TO") else "US", d)
        if ohlc:
            total += ohlc["Close"]*q*(1 if t.endswith(".TO") else FX)
    return total

def portfolio_timeseries(portfolio):
    df = pd.read_sql(
        "SELECT MIN(date) start, MAX(date) end FROM transactions WHERE portfolio=?",
        conn, params=(portfolio,)
    )
    if df.iloc[0]["start"] is None:
        return pd.DataFrame()
    dates = pd.date_range(df.iloc[0]["start"], date.today(), freq="D")
    return pd.DataFrame({
        "Date": dates,
        "Valeur": [portfolio_value_at_date(portfolio,d) for d in dates]
    })

# ================= UI =================
st.title("📊 Portfolio Tracker")

portfolio = st.selectbox("📁 Portefeuille", ["ETF","CROISSANCE","RISQUE"])

# -------- CASH --------
st.subheader("💰 Cash")
c1,c2,c3 = st.columns(3)
with c1: action = st.selectbox("Action",["CASH_DEPOSIT","CASH_WITHDRAW","DIVIDEND"])
with c2: amount = st.number_input("Montant",min_value=0.0)
with c3: currency = st.selectbox("Devise",["CAD","USD"])

if st.button("Enregistrer cash"):
    add_tx(date.today().strftime("%Y-%m-%d"),portfolio,"CASH","N/A",action,amount,1,currency)

cash = get_cash(portfolio)
st.info(f"CAD: {cash['CAD']:.2f} | USD: {cash['USD']:.2f}")

# -------- TRADE --------
st.subheader("➕ Achat / Vente")
t1,t2,t3,t4 = st.columns(4)
with t1: ticker = st.text_input("Ticker")
with t2: market = st.selectbox("Marché",["US","CA"])
with t3: tx_date = st.date_input("Date",date.today())
with t4: price_mode = st.selectbox("Prix",["Open","Close"])

ohlc = get_ohlc(ticker,market,tx_date) if ticker else None
if ohlc:
    st.caption(f"O:{ohlc['Open']} H:{ohlc['High']} L:{ohlc['Low']} C:{ohlc['Close']}")

ref = ohlc[price_mode] if ohlc else None
amount = st.number_input("Montant à investir",min_value=0.0)
if st.button("Auto"):
    if ref:
        st.session_state.price = ref
        st.session_state.qty = amount/ref if ref>0 else 0

price = st.number_input("Prix",key="price")
qty = st.number_input("Quantité",key="qty")

if st.button("Acheter"):
    add_tx(tx_date.strftime("%Y-%m-%d"),portfolio,ticker,market,"BUY",qty,price,"USD" if market=="US" else "CAD")
    add_tx(tx_date.strftime("%Y-%m-%d"),portfolio,"CASH","N/A","CASH_WITHDRAW",qty*price,1,"USD" if market=="US" else "CAD")

# -------- SUMMARY --------
st.divider()
st.subheader("📊 Rendement des portefeuilles")

rows=[]
for p in ["ETF","CROISSANCE","RISQUE"]:
    ts = portfolio_timeseries(p)
    if ts.empty: continue
    invested = ts["Valeur"].iloc[0]
    current = ts["Valeur"].iloc[-1]
    years = (ts["Date"].iloc[-1]-ts["Date"].iloc[0]).days/365.25
    cagr = (current/invested)**(1/years)-1 if years>0 else 0
    rows.append({"Portefeuille":p,"Valeur actuelle":current,"CAGR %":cagr*100})

st.dataframe(pd.DataFrame(rows))

# -------- BENCHMARK --------
st.subheader("📈 Évolution comparée + Benchmark")

series=[]
start=None
for p in ["ETF","CROISSANCE","RISQUE"]:
    ts = portfolio_timeseries(p)
    if not ts.empty:
        ts["Valeur"]=ts["Valeur"]/ts["Valeur"].iloc[0]*100
        ts["Label"]=p
        series.append(ts)
        start = ts["Date"].iloc[0] if start is None else min(start,ts["Date"].iloc[0])

if series:
    sp = yf.Ticker("^GSPC").history(start=start)["Close"]
    tsx = yf.Ticker("^GSPTSE").history(start=start)["Close"]
    df = pd.concat(series)
    chart = df.pivot(index="Date",columns="Label",values="Valeur")
    chart["S&P 500"]=sp/sp.iloc[0]*100
    chart["TSX"]=tsx/tsx.iloc[0]*100
    st.line_chart(chart)
