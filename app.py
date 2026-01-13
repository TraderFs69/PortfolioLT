import streamlit as st
import yfinance as yf
import pandas as pd
import json, os
from datetime import date
from fpdf import FPDF

# ================= CONFIG =================
st.set_page_config(page_title="Portefeuilles Trading en Action", layout="wide")
st.title("📊 Portefeuilles Trading en Action")

STATE_FILE = "portfolios_state.json"

# ================= STATE =================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)

    return {
        "portfolios": {
            "ETF simple": {"cash": 100000, "positions": {}, "transactions": []},
            "Croissance": {"cash": 100000, "positions": {}, "transactions": []},
            "Risqué": {"cash": 100000, "positions": {}, "transactions": []}
        }
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

state = load_state()

# ================= SIDEBAR =================
st.sidebar.header("⚙️ Paramètres")

start_date = st.sidebar.date_input("Date de départ des graphiques", date(2018,1,1))

portfolio_names = list(state["portfolios"].keys())
selected_portfolio = st.sidebar.selectbox("Portefeuille actif", portfolio_names)

view_mode = st.sidebar.radio(
    "Vue",
    ["Gestion du portefeuille", "Comparatif des portefeuilles"]
)

# ================= DATA =================
all_tickers = set()
for p in state["portfolios"].values():
    all_tickers.update(p["positions"].keys())

@st.cache_data(show_spinner=False)
def load_prices(tickers, start):
    if not tickers:
        return pd.DataFrame()
    raw = yf.download(list(tickers), start=start, auto_adjust=True, progress=False)
    prices = raw["Close"] if "Close" in raw else raw
    prices = prices.dropna(axis=1, how="all").dropna()
    return prices

prices = load_prices(all_tickers, start_date)

# ================= TRANSACTION MODE =================
if view_mode == "Gestion du portefeuille":
    pf = state["portfolios"][selected_portfolio]

    st.subheader(f"📁 {selected_portfolio}")

    # ----- Transactions -----
    st.markdown("### 🔁 Nouvelle transaction")

    col1, col2, col3, col4 = st.columns(4)
    action = col1.selectbox("Action", ["Acheter", "Vendre"])
    ticker = col2.text_input("Ticker").upper()
    shares = col3.number_input("Actions", min_value=0.0)
    price = col4.number_input("Prix ($)", min_value=0.0)

    if st.button("Exécuter la transaction"):
        if action == "Acheter":
            cost = shares * price
            if pf["cash"] >= cost:
                pos = pf["positions"].get(ticker, {"shares":0,"avg":0})
                new_shares = pos["shares"] + shares
                pos["avg"] = (pos["shares"]*pos["avg"] + shares*price) / new_shares
                pos["shares"] = new_shares
                pf["positions"][ticker] = pos
                pf["cash"] -= cost

        else:
            if ticker in pf["positions"] and pf["positions"][ticker]["shares"] >= shares:
                pf["cash"] += shares * price
                pf["positions"][ticker]["shares"] -= shares
                if pf["positions"][ticker]["shares"] == 0:
                    del pf["positions"][ticker]

        pf["transactions"].append({
            "date": str(date.today()),
            "ticker": ticker,
            "type": action,
            "shares": shares,
            "price": price
        })
        save_state(state)

    st.metric("💰 Cash disponible", f"${pf['cash']:,.0f}")

    # ----- Positions -----
    st.markdown("### 📋 Positions")

    rows = []
    for t,p in pf["positions"].items():
        if t in prices.columns:
            m = prices[t].iloc[-1]
            val = p["shares"] * m
            cost = p["shares"] * p["avg"]
            pnl = val - cost
            rows.append({
                "Ticker": t,
                "Actions": p["shares"],
                "Prix moyen": round(p["avg"],2),
                "Prix actuel": round(m,2),
                "Valeur": round(val,2),
                "P&L $": round(pnl,2),
                "P&L %": round((pnl/cost)*100,2)
            })

    st.dataframe(pd.DataFrame(rows))

    st.markdown("### 🧾 Historique des transactions")
    st.dataframe(pd.DataFrame(pf["transactions"]))

# ================= COMPARISON MODE =================
else:
    st.subheader("📈 Comparatif des portefeuilles")

    series = {}

    for name,pf in state["portfolios"].items():
        if not pf["positions"]:
            continue

        values = []
        for d in prices.index:
            total = pf["cash"]
            for t,p in pf["positions"].items():
                if t in prices.columns:
                    total += p["shares"] * prices.loc[d,t]
            values.append(total)

        series[name] = pd.Series(values, index=prices.index)

    if series:
        st.line_chart(pd.DataFrame(series))
    else:
        st.info("Aucune donnée à comparer pour l’instant.")

# ================= FOOTER =================
st.markdown("""
---
### 🎓 Philosophie Trading en Action
Chaque portefeuille a son **objectif**,  
son **niveau de risque**,  
et sa **discipline propre**.

👉 Ce n’est pas la performance qui compte.  
👉 C’est la **cohérence dans le temps**.
""")
