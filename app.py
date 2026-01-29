import streamlit as st
import pandas as pd
import sqlite3
import requests
import yfinance as yf
from datetime import date, timedelta

# ================= CONFIG =================
DB_NAME = "portfolio.db"
POLYGON_KEY = st.secrets["POLYGON_API_KEY"]

st.set_page_config(page_title="📊 Portfolio Tracker Pro", layout="wide")

# ================= DB =================
conn = sqlite3.connect(DB_NAME, check_same_thread=False)
c = conn.cursor()

# ---- CREATE TABLE (nouvelle install) ----
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

# ---- MIGRATION SAFE (ancienne table) ----
def migrate_db():
    cols = [row[1] for row in c.execute("PRAGMA table_info(transactions)").fetchall()]

    if "currency" not in cols:
        c.execute("ALTER TABLE transactions ADD COLUMN currency TEXT DEFAULT 'CAD'")

    conn.commit()

migrate_db()

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
    return {
        "Open": r["open"],
        "High": r["high"],
        "Low": r["low"],
        "Close": r["close"],
        "VWAP": r.get("vwap")
    }

def get_ohlc_ca(ticker, d):
    df = yf.Ticker(ticker).history(start=d, end=d + timedelta(days=1))
    if df.empty:
        return None
    row = df.iloc[0]
    return {
        "Open": float(row["Open"]),
        "High": float(row["High"]),
        "Low": float(row["Low"]),
        "Close": float(row["Close"]),
        "VWAP": float((row["High"] + row["Low"] + row["Close"]) / 3)
    }

def get_ohlc(ticker, market, d):
    return get_ohlc_us(ticker, d) if market == "US" else get_ohlc_ca(ticker, d)

# ================= TRANSACTIONS =================
def add_tx(d, portfolio, ticker, market, action, qty, price, currency):
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
        f"""
        SELECT * FROM transactions
        WHERE portfolio='{portfolio}'
        AND action IN ('CASH_DEPOSIT','CASH_WITHDRAW','DIVIDEND')
        """,
        conn
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
        f"""
        SELECT * FROM transactions
        WHERE portfolio='{portfolio}'
        AND action IN ('BUY','SELL')
        """,
        conn
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
def portfolio_value(portfolio):
    pos = load_positions(portfolio)
    cash = get_cash(portfolio)

    total = cash.get("CAD",0) + cash.get("USD",0) * FX

    for _, r in pos.iterrows():
        ohlc = get_ohlc(r.ticker, r.market, date.today())
        if not ohlc:
            continue
        price = ohlc["Close"]
        val = price * r.quantity
        total += val if r.currency == "CAD" else val * FX

    return total

# ================= UI =================
st.title("📊 Portfolio Tracker Pro")

portfolio = st.selectbox("📁 Portefeuille", ["ETF","CROISSANCE","RISQUE"])

# -------- CASH / DIVIDENDES --------
st.subheader("💰 Cash & Dividendes")

c1,c2,c3 = st.columns(3)
with c1:
    cash_action = st.selectbox("Action", ["CASH_DEPOSIT","CASH_WITHDRAW","DIVIDEND"])
with c2:
    cash_amount = st.number_input("Montant", min_value=0.0)
with c3:
    cash_currency = st.selectbox("Devise", ["CAD","USD"])

if st.button("💾 Enregistrer cash/dividende"):
    add_tx(
        date.today().strftime("%Y-%m-%d"),
        portfolio,
        "CASH",
        "N/A",
        cash_action,
        cash_amount,
        1,
        cash_currency
    )
    st.success("Transaction enregistrée")

cash = get_cash(portfolio)
st.info(f"💵 Cash CAD : {cash.get('CAD',0):.2f} | 💲 Cash USD : {cash.get('USD',0):.2f}")

# -------- TICKET DE TRADE --------
st.divider()
st.subheader("➕ Ticket de trade avancé")

t1,t2,t3,t4 = st.columns([2,2,2,4])

with t1:
    ticker = st.text_input("Ticker")
    market = st.selectbox("Marché", ["US","CA"])
    price_mode = st.selectbox("Prix de référence", ["Open","Close","VWAP"])

with t2:
    target_amount = st.number_input("💰 Montant ($)", min_value=0.0)
    risk_pct = st.number_input("⚠️ Risk % portefeuille", min_value=0.0, max_value=10.0, value=1.0)

with t3:
    stop_pct = st.number_input("📉 Stop (%)", min_value=0.0, value=5.0)
    rounding = st.selectbox("Arrondi quantité", ["Entier","2 décimales"])

with t4:
    tx_date = st.date_input("Date", value=date.today())

ohlc = get_ohlc(ticker.upper(), market, tx_date) if ticker else None

if ohlc:
    st.markdown(
        f"""
        **📊 OHLC**
        - Open : **{ohlc['Open']:.2f}**
        - High : **{ohlc['High']:.2f}**
        - Low : **{ohlc['Low']:.2f}**
        - Close : **{ohlc['Close']:.2f}**
        - VWAP : **{ohlc['VWAP']:.2f}**
        """
    )

ref_price = ohlc[price_mode] if ohlc else None
portfolio_val = portfolio_value(portfolio)

colA, colB = st.columns(2)

with colA:
    if st.button("⚡ Auto-prix"):
        if ref_price:
            st.session_state.price = round(ref_price, 2)

with colB:
    if st.button("🧮 Taille auto (Risk %)"):
        if ref_price and portfolio_val > 0:
            risk_dollars = portfolio_val * (risk_pct / 100)
            stop_distance = ref_price * (stop_pct / 100)
            qty = risk_dollars / stop_distance
            qty = int(qty) if rounding == "Entier" else round(qty, 2)
            st.session_state.qty = qty
            st.session_state.price = round(ref_price, 2)

price = st.number_input("Prix exécuté", min_value=0.0, key="price")
qty = st.number_input("Quantité", min_value=0.0, key="qty")

currency = "USD" if market == "US" else "CAD"

if st.button("💾 Enregistrer trade"):
    add_tx(tx_date.strftime("%Y-%m-%d"), portfolio, ticker.upper(), market, "BUY", qty, price, currency)
    add_tx(tx_date.strftime("%Y-%m-%d"), portfolio, "CASH", "N/A", "CASH_WITHDRAW", qty * price, 1, currency)
    st.success("Trade enregistré")

# -------- PERFORMANCE --------
st.divider()
st.subheader("📊 Performance globale")

st.metric("Valeur totale (CAD)", f"{portfolio_value(portfolio):,.2f}")

# -------- JOURNAL --------
st.divider()
st.subheader("📒 Journal de transactions")

journal = pd.read_sql("SELECT rowid,* FROM transactions ORDER BY date DESC", conn)
st.dataframe(journal)

tx_id = st.number_input("rowid à supprimer", min_value=1, step=1)
if st.button("🗑️ Supprimer transaction"):
    delete_tx(tx_id)
    st.warning("Transaction supprimée")
