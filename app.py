import streamlit as st
import pandas as pd
import sqlite3
import yfinance as yf
import requests
from datetime import date, timedelta
import numpy as np

# ================= CONFIG =================
DB_NAME = "portfolio.db"
POLYGON_KEY = st.secrets["POLYGON_API_KEY"]

st.set_page_config(page_title="📊 Portfolio Tracker + Benchmark", layout="wide")

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

# ================= FX =================
@st.cache_data(ttl=3600)
def fx_rate():
    try:
        df = yf.download("USDCAD=X", period="5d", progress=False)
        return float(df["Close"].dropna().iloc[-1])
    except Exception:
        return 1.35  # fallback

FX = fx_rate()

# ================= HELPERS =================
def normalize_ticker(ticker, market):
    if market == "CA" and not ticker.upper().endswith(".TO"):
        return ticker.upper() + ".TO"
    return ticker.upper()

# ================= OHLC =================
def get_ohlc(ticker, market, d):
    if not ticker:
        return None
    if market == "US":
        url = f"https://api.polygon.io/v1/open-close/{ticker}/{d}?adjusted=true&apiKey={POLYGON_KEY}"
        r = requests.get(url).json()
        if r.get("status") != "OK":
            return None
        return {"Open": r["open"], "Close": r["close"]}
    else:
        t = normalize_ticker(ticker, "CA")
        df = yf.download(t, start=d, end=d + timedelta(days=1), progress=False)
        if df.empty:
            return None
        r = df.iloc[0]
        return {"Open": float(r["Open"]), "Close": float(r["Close"])}

# ================= PRIX ACTUELS =================
@st.cache_data(ttl=900)
def get_last_close_us(ticker):
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={POLYGON_KEY}"
    r = requests.get(url).json()
    if "results" in r:
        return r["results"][0]["c"]
    return None

@st.cache_data(ttl=900)
def get_last_closes_ca(tickers):
    if not tickers:
        return {}
    tickers = list(set(tickers))
    data = yf.download(tickers=tickers, period="5d", progress=False)
    prices = {}

    if len(tickers) == 1:
        try:
            prices[tickers[0]] = float(data["Close"].dropna().iloc[-1])
        except Exception:
            prices[tickers[0]] = None
        return prices

    close_df = data["Close"]
    for t in tickers:
        try:
            prices[t] = float(close_df[t].dropna().iloc[-1])
        except Exception:
            prices[t] = None
    return prices

# ================= POSITIONS =================
def load_positions(portfolio):
    df = pd.read_sql(
        "SELECT * FROM transactions WHERE portfolio=?",
        conn, params=(portfolio,)
    )
    if df.empty:
        return pd.DataFrame(), df

    df["signed"] = np.where(df["action"] == "BUY", df["quantity"], -df["quantity"])

    pos = df.groupby(["ticker", "market", "currency"]).agg(
        quantity=("signed", "sum"),
        avg_price=("price", "mean")
    ).reset_index()

    pos = pos[pos["quantity"] > 0]

    ca_tickers = pos.loc[pos["market"] == "CA", "ticker"].tolist()
    ca_prices = get_last_closes_ca(ca_tickers)

    prices, values, costs = [], [], []

    for _, r in pos.iterrows():
        price = ca_prices.get(r.ticker) if r.market == "CA" else get_last_close_us(r.ticker)
        prices.append(price)

        if price:
            val = price * r.quantity
            val_cad = val if r.currency == "CAD" else val * FX
            cost = r.avg_price * r.quantity
            cost_cad = cost if r.currency == "CAD" else cost * FX
        else:
            val_cad, cost_cad = None, None

        values.append(val_cad)
        costs.append(cost_cad)

    pos["Prix actuel"] = prices
    pos["Valeur (CAD)"] = values
    pos["Coût (CAD)"] = costs
    pos["Gain %"] = (pos["Valeur (CAD)"] - pos["Coût (CAD)"]) / pos["Coût (CAD)"] * 100

    return pos, df

# ================= METRICS =================
def portfolio_metrics(pos, df):
    total_value = pos["Valeur (CAD)"].sum()
    total_cost = pos["Coût (CAD)"].sum()

    total_return = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0.0

    start_date = pd.to_datetime(df["date"]).min()
    years = (pd.Timestamp.today() - start_date).days / 365.25

    cagr = (total_value / total_cost) ** (1 / years) - 1 if total_cost > 0 and years > 0 else 0.0
    return total_value, total_return, cagr

# ================= BENCHMARK =================
@st.cache_data(ttl=3600)
def load_benchmark(symbol, start):
    df = yf.download(symbol, start=start, progress=False)
    if df.empty:
        return None
    df = df[["Close"]].dropna()
    df["Norm"] = df["Close"] / df["Close"].iloc[0]
    return df

# ================= UI =================
st.title("📊 Portfolio Tracker + Benchmark")

portfolio = st.selectbox("📁 Portefeuille", ["ETF", "CROISSANCE", "RISQUE"])

tab1, tab2, tab3 = st.tabs(["📦 Composition", "📈 Benchmark", "📒 Journal"])

# ---------- TAB 1 : COMPOSITION ----------
with tab1:
    pos, df_port = load_positions(portfolio)

    if not pos.empty:
        total_value, total_return, cagr = portfolio_metrics(pos, df_port)

        st.metric("Valeur totale (CAD)", f"{total_value:,.2f}")
        st.metric("Rendement total", f"{total_return:.2f} %")
        st.metric("CAGR", f"{cagr*100:.2f} %")

        st.dataframe(
            pos.fillna(0).style.format({
                "quantity": "{:.2f}",
                "avg_price": "{:.2f}",
                "Prix actuel": "{:.2f}",
                "Valeur (CAD)": "{:,.2f}",
                "Coût (CAD)": "{:,.2f}",
                "Gain %": "{:.2f}%"
            })
        )
    else:
        st.info("Aucune position.")

# ---------- TAB 2 : BENCHMARK ----------
with tab2:
    if df_port.empty:
        st.info("Aucune donnée pour le benchmark.")
    else:
        start = pd.to_datetime(df_port["date"]).min()

        bench_us = load_benchmark("^GSPC", start)
        bench_ca = load_benchmark("^GSPTSE", start)

        total_value, _, _ = portfolio_metrics(pos, df_port)

        # portefeuille normalisé
        port_series = (
            df_port.groupby("date")["price"]
            .sum()
            .sort_index()
        )
        port_norm = port_series / port_series.iloc[0]

        df_plot = pd.DataFrame({"Portefeuille": port_norm})

        if bench_us is not None:
            df_plot["S&P 500"] = bench_us["Norm"]

        if bench_ca is not None:
            df_plot["TSX"] = bench_ca["Norm"]

        st.line_chart(df_plot)

# ---------- TAB 3 : JOURNAL ----------
with tab3:
    journal = pd.read_sql(
        """
        SELECT rowid AS id, date, portfolio, ticker, market,
               action, quantity, price, currency
        FROM transactions
        ORDER BY date DESC
        """,
        conn
    )

    if journal.empty:
        st.info("Aucune transaction.")
    else:
        st.dataframe(journal)
