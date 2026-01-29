import streamlit as st
import pandas as pd
import sqlite3
import yfinance as yf
import requests
from datetime import date, timedelta

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

# ================= HELPERS =================
def normalize_ticker(ticker, market):
    if market == "CA" and not ticker.upper().endswith(".TO"):
        return ticker.upper() + ".TO"
    return ticker.upper()

@st.cache_data(ttl=3600)
def fx_rate():
    return yf.Ticker("USDCAD=X").history(period="1d")["Close"].iloc[-1]

FX = fx_rate()

# ================= OHLC =================
def get_ohlc(ticker, market, d):
    if market == "US":
        url = f"https://api.polygon.io/v1/open-close/{ticker}/{d}?adjusted=true&apiKey={POLYGON_KEY}"
        r = requests.get(url).json()
        if r.get("status") != "OK":
            return None
        return {"Open": r["open"], "High": r["high"], "Low": r["low"], "Close": r["close"]}
    else:
        ticker = normalize_ticker(ticker, "CA")
        df = yf.Ticker(ticker).history(start=d, end=d + timedelta(days=1))
        if df.empty:
            return None
        r = df.iloc[0]
        return {"Open": r["Open"], "High": r["High"], "Low": r["Low"], "Close": r["Close"]}

# ================= TRANSACTIONS =================
def add_tx(d, portfolio, ticker, market, action, qty, price, currency):
    c.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",
        (d, portfolio, ticker, market, action, qty, price, currency)
    )
    conn.commit()

def delete_tx(rowid):
    c.execute("DELETE FROM transactions WHERE rowid=?", (rowid,))
    conn.commit()

# ================= POSITIONS =================
def load_positions(portfolio):
    df = pd.read_sql(
        "SELECT * FROM transactions WHERE portfolio=? AND action IN ('BUY','SELL')",
        conn, params=(portfolio,)
    )
    if df.empty:
        return pd.DataFrame()

    df["signed"] = df.apply(lambda x: x["quantity"] if x["action"]=="BUY" else -x["quantity"], axis=1)
    pos = df.groupby(["ticker","market","currency"]).agg(
        quantity=("signed","sum"),
        avg_price=("price","mean")
    ).reset_index()

    pos = pos[pos["quantity"] > 0]

    prices = []
    values = []

    for _, r in pos.iterrows():
        ohlc = get_ohlc(r.ticker, r.market, date.today())
        if not ohlc:
            prices.append(0)
            values.append(0)
        else:
            p = ohlc["Close"]
            prices.append(p)
            v = p * r.quantity
            values.append(v if r.currency=="CAD" else v*FX)

    pos["Prix actuel"] = prices
    pos["Valeur (CAD)"] = values
    pos["Gain %"] = (pos["Prix actuel"] - pos["avg_price"]) / pos["avg_price"] * 100

    return pos

# ================= PORTFOLIO VALUE =================
def portfolio_value(portfolio):
    pos = load_positions(portfolio)
    if pos.empty:
        return 0
    return pos["Valeur (CAD)"].sum()

# ================= UI =================
st.title("📊 Portfolio Tracker Complet")

portfolio = st.selectbox("📁 Portefeuille", ["ETF","CROISSANCE","RISQUE"])

# ---------- ACHAT / VENTE ----------
st.subheader("➕ Achat / Vente")

c1,c2,c3,c4 = st.columns(4)

with c1:
    ticker = st.text_input("Ticker")
    market = st.selectbox("Marché", ["US","CA"])
    price_mode = st.selectbox("Prix", ["Open","Close"])

with c2:
    tx_date = st.date_input("Date", value=date.today())
    montant = st.number_input("Montant $", min_value=0.0)

with c3:
    rounding = st.selectbox("Arrondi", ["Entier","2 décimales"])
    action = st.selectbox("Action", ["BUY","SELL"])

with c4:
    ohlc = get_ohlc(ticker, market, tx_date) if ticker else None
    if ohlc:
        st.markdown(
            f"""
            **OHLC**
            Open: {ohlc['Open']:.2f}
            High: {ohlc['High']:.2f}
            Low: {ohlc['Low']:.2f}
            Close: {ohlc['Close']:.2f}
            """
        )

ref_price = ohlc[price_mode] if ohlc else None

if st.button("⚡ Auto-calcul quantité") and ref_price and montant>0:
    qty = montant / ref_price
    qty = int(qty) if rounding=="Entier" else round(qty,2)
    st.session_state.qty = qty
    st.session_state.price = round(ref_price,2)

price = st.number_input("Prix", key="price")
qty = st.number_input("Quantité", key="qty")

currency = "USD" if market=="US" else "CAD"

if st.button("💾 Enregistrer"):
    add_tx(tx_date.strftime("%Y-%m-%d"), portfolio, normalize_ticker(ticker, market),
           market, action, qty, price, currency)
    st.success("Transaction enregistrée")

# ---------- COMPOSITION ----------
st.divider()
st.subheader(f"📦 Composition du portefeuille {portfolio}")
pos = load_positions(portfolio)
st.dataframe(pos)

# ---------- JOURNAL ----------
st.divider()
st.subheader("📒 Journal des transactions")

journal = pd.read_sql(
    "SELECT rowid as id, * FROM transactions ORDER BY date DESC",
    conn
)
st.dataframe(journal)

tx_id = st.number_input("ID à supprimer", min_value=1, step=1)
if st.button("🗑️ Supprimer"):
    delete_tx(tx_id)
    st.warning("Transaction supprimée")

# ---------- RENDEMENT ----------
st.divider()
st.subheader("📊 Valeur des portefeuilles")

rows=[]
for p in ["ETF","CROISSANCE","RISQUE"]:
    rows.append({"Portefeuille":p,"Valeur (CAD)":portfolio_value(p)})

st.dataframe(pd.DataFrame(rows))

# ---------- BENCHMARK ----------
st.divider()
st.subheader("📈 Évolution comparée & benchmark")

series=[]
start=date.today()-timedelta(days=365)

for p in ["ETF","CROISSANCE","RISQUE"]:
    ts=[]
    for d in pd.date_range(start,date.today()):
        ts.append(portfolio_value(p))
    series.append(pd.Series(ts,name=p))

sp500=yf.Ticker("^GSPC").history(start=start)["Close"]
tsx=yf.Ticker("^GSPTSE").history(start=start)["Close"]

df=pd.concat(series+[sp500/sp500.iloc[0]*100, tsx/tsx.iloc[0]*100],axis=1)
df.columns=["ETF","CROISSANCE","RISQUE","S&P500","TSX"]

st.line_chart(df)
