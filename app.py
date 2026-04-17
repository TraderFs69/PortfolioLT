import streamlit as st
import pandas as pd
import psycopg2
import requests
import numpy as np
from datetime import date

# ================= CONFIG =================
st.set_page_config(page_title="📊 TEA Portfolio", layout="wide")

POLYGON_KEY = st.secrets["POLYGON_API_KEY"]

# ================= DB CONNECTION =================
@st.cache_resource
def get_conn():
    return psycopg2.connect(
        st.secrets["SUPABASE_DB_URL"],
        sslmode="require"  # 🔥 IMPORTANT pour Supabase
    )

conn = get_conn()

def run_query(query, params=None, fetch=False):
    with conn.cursor() as c:
        c.execute(query, params)
        if fetch:
            return c.fetchall()
        conn.commit()

# ================= INIT TABLES =================
run_query("""
CREATE TABLE IF NOT EXISTS portfolios (
    name TEXT PRIMARY KEY
)
""")

run_query("""
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    date DATE,
    portfolio TEXT,
    ticker TEXT,
    action TEXT,
    quantity FLOAT,
    price FLOAT
)
""")

run_query("""
CREATE TABLE IF NOT EXISTS history (
    date DATE,
    portfolio TEXT,
    value FLOAT
)
""")

# ================= PRIX =================
@st.cache_data(ttl=60)
def get_price(ticker):
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={POLYGON_KEY}"
        r = requests.get(url).json()
        if "results" in r:
            return r["results"][0]["c"]
    except:
        return None
    return None

# ================= SIDEBAR =================
st.sidebar.title("📁 STRATÉGIES")

ports = pd.read_sql("SELECT name FROM portfolios", conn)["name"].tolist()

new_port = st.sidebar.text_input("➕ Nouvelle stratégie")

if st.sidebar.button("Créer stratégie"):
    if new_port:
        run_query(
            "INSERT INTO portfolios (name) VALUES (%s) ON CONFLICT DO NOTHING",
            (new_port,)
        )
        st.rerun()

if ports:
    portfolio = st.sidebar.selectbox("Choisir", ports)
else:
    st.warning("Crée un portefeuille pour commencer")
    st.stop()

# ================= UI =================
st.title("📊 TEA Portfolio Tracker")

col1, col2 = st.columns(2)

with col1:
    ticker = st.text_input("Ticker")
    action = st.selectbox("Action", ["BUY", "SELL"])
    price = st.number_input("Prix", min_value=0.0)

with col2:
    qty = st.number_input("Quantité", min_value=0.0)
    tx_date = st.date_input("Date", value=date.today())

if st.button("💾 Ajouter transaction"):
    if ticker and qty > 0 and price > 0:
        run_query(
            """INSERT INTO transactions 
            (date, portfolio, ticker, action, quantity, price)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (tx_date, portfolio, ticker.upper(), action, qty, price)
        )
        st.success("Transaction ajoutée")
        st.rerun()
    else:
        st.error("Entrée invalide")

# ================= LOAD DATA =================
df = pd.read_sql(
    "SELECT * FROM transactions WHERE portfolio=%s",
    conn,
    params=(portfolio,)
)

if not df.empty:

    df["signed"] = np.where(df["action"] == "BUY", df["quantity"], -df["quantity"])
    df["cost"] = df["price"] * df["quantity"]

    pos = df.groupby("ticker").agg(
        quantity=("signed", "sum"),
        total_cost=("cost", "sum")
    ).reset_index()

    pos = pos[pos["quantity"] > 0]

    pos["avg_price"] = pos["total_cost"] / pos["quantity"]

    prices, values = [], []

    for _, r in pos.iterrows():
        p = get_price(r.ticker)
        prices.append(p)

        if p:
            val = p * r.quantity
        else:
            val = None

        values.append(val)

    pos["Prix"] = prices
    pos["Valeur"] = values

    pos["Gain %"] = (
        (pos["Valeur"] - pos["total_cost"]) / pos["total_cost"]
    ) * 100

    pos["Poids %"] = pos["Valeur"] / pos["Valeur"].sum() * 100

    # ================= METRICS =================
    total_value = pos["Valeur"].sum()
    total_cost = pos["total_cost"].sum()

    total_return = (
        (total_value / total_cost - 1) * 100
        if total_cost > 0 else 0
    )

    colA, colB = st.columns(2)
    colA.metric("💰 Valeur", f"{total_value:,.0f} $")
    colB.metric("📈 Rendement", f"{total_return:.2f} %")

    # ================= SAVE HISTORY =================
    today = date.today()

    exists = run_query(
        "SELECT 1 FROM history WHERE date=%s AND portfolio=%s",
        (today, portfolio),
        fetch=True
    )

    if not exists:
        run_query(
            "INSERT INTO history (date, portfolio, value) VALUES (%s,%s,%s)",
            (today, portfolio, total_value)
        )

    # ================= TABLE =================
    st.subheader("📦 Positions")
    st.dataframe(pos)

    # ================= GRAPH =================
    st.subheader("📈 Évolution")

    hist = pd.read_sql(
        "SELECT * FROM history WHERE portfolio=%s ORDER BY date",
        conn,
        params=(portfolio,)
    )

    if not hist.empty:
        hist["date"] = pd.to_datetime(hist["date"])
        st.line_chart(hist.set_index("date")["value"])

else:
    st.info("Aucune transaction pour ce portefeuille")
