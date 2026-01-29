import streamlit as st
import pandas as pd
import sqlite3
import requests
import yfinance as yf
from datetime import date

# ================= CONFIG =================
DB_NAME = "portfolio.db"
POLYGON_KEY = st.secrets["POLYGON_API_KEY"]

st.set_page_config(page_title="📊 Portfolio Tracker Pro", layout="wide")

# ================= DB =================
conn = sqlite3.connect(DB_NAME, check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

# ================= FX =================
@st.cache_data(ttl=3600)
def get_fx():
    fx = yf.Ticker("USDCAD=X").history(period="1d")
    return float(fx["Close"].iloc[-1])

FX = get_fx()

# ================= PRICES =================
def get_price_us(ticker):
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={POLYGON_KEY}"
    r = requests.get(url).json()
    return r["results"][0]["c"] if "results" in r else None

def get_price_ca(ticker):
    data = yf.Ticker(ticker).history(period="1d")
    return float(data["Close"].iloc[-1])

def get_live_price(ticker, market):
    return get_price_us(ticker) if market == "US" else get_price_ca(ticker)

# ================= TRANSACTIONS =================
def add_tx(d, portfolio, ticker, market, action, qty, price, currency):
    c.execute(
        "INSERT INTO transactions VALUES (NULL,?,?,?,?,?,?,?)",
        (d, portfolio, ticker, market, action, qty, price, currency)
    )
    conn.commit()

def delete_tx(tx_id):
    c.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()

# ================= CASH =================
def get_cash(portfolio):
    df = pd.read_sql(
        f"SELECT * FROM transactions WHERE portfolio='{portfolio}' AND action IN ('CASH_DEPOSIT','CASH_WITHDRAW','DIVIDEND')",
        conn
    )
    if df.empty:
        return {"CAD": 0, "USD": 0}

    df["signed"] = df.apply(
        lambda x: x["quantity"] if x["action"] in ("CASH_DEPOSIT", "DIVIDEND") else -x["quantity"],
        axis=1
    )
    return df.groupby("currency")["signed"].sum().to_dict()

# ================= POSITIONS =================
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

    pos = df.groupby(["ticker","market","currency"]).agg(
        quantity=("signed_qty","sum"),
        avg_price=("price","mean")
    ).reset_index()

    return pos[pos["quantity"] > 0]

# ================= PERFORMANCE =================
def portfolio_value(portfolio):
    pos = load_positions(portfolio)
    cash = get_cash(portfolio)

    total = cash.get("CAD",0) + cash.get("USD",0) * FX

    for _, r in pos.iterrows():
        live = get_live_price(r.ticker, r.market)
        val = live * r.quantity
        total += val if r.currency=="CAD" else val * FX

    return total

# ================= UI =================
st.title("📊 Portfolio Tracker Pro")

portfolio = st.selectbox("📁 Portefeuille", ["ETF","CROISSANCE","RISQUE"])

# -------- CASH --------
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
    st.success("Enregistré")

cash = get_cash(portfolio)
st.info(f"Cash CAD: {cash.get('CAD',0):.2f} | Cash USD: {cash.get('USD',0):.2f}")

# -------- TRADE --------
st.subheader("➕ Achat / Vente")

t1,t2,t3 = st.columns(3)
with t1:
    ticker = st.text_input("Ticker")
    market = st.selectbox("Marché", ["US","CA"])
with t2:
    action = st.selectbox("Action", ["BUY","SELL"])
    qty = st.number_input("Quantité", min_value=0.0)
with t3:
    price = st.number_input("Prix", min_value=0.0)
    tx_date = st.date_input("Date", value=date.today())

currency = "USD" if market=="US" else "CAD"

if st.button("💾 Enregistrer trade"):
    add_tx(tx_date.strftime("%Y-%m-%d"), portfolio, ticker.upper(), market, action, qty, price, currency)

  

    cash_flow = qty * price
    add_tx(
        tx_date.strftime("%Y-%m-%d"),
        portfolio,
        "CASH",
        "N/A",
        "CASH_WITHDRAW" if action=="BUY" else "CASH_DEPOSIT",
        cash_flow,
        1,
        currency
    )
    st.success("Trade enregistré")

# -------- POSITIONS --------
st.divider()
st.subheader("📈 Positions")

pos = load_positions(portfolio)
rows = []

for _, r in pos.iterrows():
    live = get_live_price(r.ticker, r.market)
    val = live * r.quantity
    val_cad = val if r.currency=="CAD" else val * FX
    cost = r.avg_price * r.quantity
    cost_cad = cost if r.currency=="CAD" else cost * FX

    rows.append({
        "Ticker": r.ticker,
        "Qté": r.quantity,
        "Valeur CAD": val_cad,
        "Gain CAD": val_cad - cost_cad
    })

df_pos = pd.DataFrame(rows)
st.dataframe(df_pos.style.format({"Valeur CAD":"{:.2f}","Gain CAD":"{:.2f}"}))

# -------- PERFORMANCE --------
st.divider()
st.subheader("📊 Performance globale")

total_value = portfolio_value(portfolio)
st.metric("Valeur totale (CAD)", f"{total_value:,.2f}")

# -------- JOURNAL --------
st.divider()
st.subheader("📒 Journal (modifiable)")

journal = pd.read_sql("SELECT * FROM transactions ORDER BY date DESC", conn)
st.dataframe(journal)

tx_id = st.number_input("ID de la transaction à supprimer", min_value=1, step=1)
if st.button("🗑️ Supprimer la transaction"):
    delete_tx(tx_id)
    st.warning("Transaction supprimée")
