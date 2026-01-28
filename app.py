import streamlit as st
import pandas as pd
import sqlite3
import requests
import yfinance as yf
from datetime import date

# ---------------- CONFIG ----------------
DB_NAME = "portfolio.db"
POLYGON_KEY = st.secrets["POLYGON_API_KEY"]

st.set_page_config(page_title="📊 Portefeuilles Long Terme", layout="wide")

# ---------------- DB ----------------
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

# ---------------- FX ----------------
@st.cache_data(ttl=3600)
def get_fx():
    fx = yf.Ticker("USDCAD=X").history(period="1d")
    return float(fx["Close"].iloc[-1])

FX_USD_CAD = get_fx()

# ---------------- PRICES ----------------
def get_price_us(ticker):
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={POLYGON_KEY}"
    r = requests.get(url).json()
    return r["results"][0]["c"] if "results" in r else None

def get_price_ca(ticker):
    data = yf.Ticker(ticker).history(period="1d")
    return float(data["Close"].iloc[-1])

def get_live_price(ticker, market):
    return get_price_us(ticker) if market == "US" else get_price_ca(ticker)

# ---------------- TRANSACTIONS ----------------
def add_transaction(d, portfolio, ticker, market, action, qty, price, currency):
    c.execute(
        "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (d, portfolio, ticker, market, action, qty, price, currency)
    )
    conn.commit()

# ---------------- CASH ----------------
def get_cash(portfolio):
    df = pd.read_sql(
        f"SELECT * FROM transactions WHERE portfolio='{portfolio}' AND action LIKE 'CASH%'",
        conn
    )
    if df.empty:
        return {"CAD": 0.0, "USD": 0.0}

    df["signed"] = df.apply(
        lambda x: x["quantity"] if x["action"] == "CASH_DEPOSIT" else -x["quantity"],
        axis=1
    )
    return df.groupby("currency")["signed"].sum().to_dict()

# ---------------- POSITIONS ----------------
def load_positions(portfolio):
    df = pd.read_sql(
        f"SELECT * FROM transactions WHERE portfolio='{portfolio}' AND action IN ('BUY','SELL')",
        conn
    )
    if df.empty:
        return pd.DataFrame()

    df["signed_qty"] = df.apply(
        lambda x: x["quantity"] if x["action"] == "BUY" else -x["quantity"],
        axis=1
    )

    pos = df.groupby(["ticker", "market", "currency"]).agg(
        quantity=("signed_qty", "sum"),
        avg_price=("price", "mean")
    ).reset_index()

    return pos[pos["quantity"] > 0]

# ---------------- UI ----------------
st.title("📊 Gestionnaire de Portefeuilles (Persistant)")

portfolio = st.selectbox("📁 Portefeuille", ["ETF", "CROISSANCE", "RISQUE"])

# ---------------- CASH UI ----------------
st.subheader("💰 Gestion du cash")

col1, col2, col3 = st.columns(3)
with col1:
    cash_action = st.selectbox("Action", ["CASH_DEPOSIT", "CASH_WITHDRAW"])
with col2:
    cash_amount = st.number_input("Montant", min_value=0.0)
with col3:
    cash_currency = st.selectbox("Devise", ["CAD", "USD"])

if st.button("💾 Enregistrer cash"):
    add_transaction(
        date.today().strftime("%Y-%m-%d"),
        portfolio,
        "CASH",
        "N/A",
        cash_action,
        cash_amount,
        1,
        cash_currency
    )
    st.success("Cash enregistré")

cash = get_cash(portfolio)
st.info(f"💵 Cash CAD: {cash.get('CAD',0):.2f} | 💲 Cash USD: {cash.get('USD',0):.2f}")

# ---------------- TRADE UI ----------------
st.subheader("➕ Achat / Vente")

c1, c2, c3 = st.columns(3)
with c1:
    ticker = st.text_input("Ticker (AAPL / CNQ.TO)")
    market = st.selectbox("Marché", ["US", "CA"])
with c2:
    action = st.selectbox("Action", ["BUY", "SELL"])
    qty = st.number_input("Quantité", min_value=0.0)
with c3:
    price = st.number_input("Prix", min_value=0.0)
    trans_date = st.date_input("Date", value=date.today())

currency = "USD" if market == "US" else "CAD"

if st.button("💾 Enregistrer trade"):
    add_transaction(
        trans_date.strftime("%Y-%m-%d"),
        portfolio,
        ticker.upper(),
        market,
        action,
        qty,
        price,
        currency
    )

    # impact cash
    cash_flow = qty * price
    cash_action = "CASH_WITHDRAW" if action == "BUY" else "CASH_DEPOSIT"
    add_transaction(
        trans_date.strftime("%Y-%m-%d"),
        portfolio,
        "CASH",
        "N/A",
        cash_action,
        cash_flow,
        1,
        currency
    )
    st.success("Trade enregistré")

# ---------------- PORTFOLIO VIEW ----------------
st.divider()
st.subheader(f"📈 Positions – {portfolio}")

pos = load_positions(portfolio)

if pos.empty:
    st.info("Aucune position")
else:
    rows = []
    for _, r in pos.iterrows():
        live = get_live_price(r.ticker, r.market)
        value = live * r.quantity
        cost = r.avg_price * r.quantity
        value_cad = value if r.currency == "CAD" else value * FX_USD_CAD

        rows.append({
            "Ticker": r.ticker,
            "Qté": r.quantity,
            "Devise": r.currency,
            "Prix moyen": r.avg_price,
            "Prix actuel": live,
            "Valeur CAD": value_cad,
            "Gain CAD": value_cad - (cost if r.currency == "CAD" else cost * FX_USD_CAD)
        })

    df = pd.DataFrame(rows)
    st.dataframe(df.style.format({
        "Prix moyen": "{:.2f}",
        "Prix actuel": "{:.2f}",
        "Valeur CAD": "{:.2f}",
        "Gain CAD": "{:.2f}"
    }))

# ---------------- JOURNAL ----------------
st.divider()
st.subheader("📒 Journal de transactions (permanent)")
journal = pd.read_sql("SELECT * FROM transactions ORDER BY date DESC", conn)
st.dataframe(journal)
