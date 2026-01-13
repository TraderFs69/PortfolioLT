import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import json, os
from datetime import date
from fpdf import FPDF

# ================= CONFIG =================
st.set_page_config(page_title="Trading en Action – Long Terme", layout="wide")
st.title("📈 Simulateur Long Terme – Trading en Action")

STATE_FILE = "state_transactions.json"

# ================= STATE =================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"cash": 100000, "positions": {}, "transactions": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

state = load_state()

# ================= PORTFEUILLES =================
ETF_SIMPLE = {"VTI": 0.6, "VXUS": 0.25, "BND": 0.15}

GROWTH = {
    "AAPL":0.09,"MSFT":0.09,"V":0.09,"BRK-B":0.09,"COST":0.09,
    "NVDA":0.06,"AMZN":0.06,"GOOGL":0.06,"ADBE":0.06,"ASML":0.06,"CRM":0.06,
    "TSLA":0.05,"CRWD":0.05,"SHOP":0.05,"MELI":0.05
}

RISKY = {
    "NVDA":0.17,"AMZN":0.17,"MSFT":0.16,
    "PLTR":0.10,"SNOW":0.10,"COIN":0.10,
    "SQ":0.10,"ROKU":0.10
}

# ================= SIDEBAR =================
st.sidebar.header("⚙️ Paramètres globaux")

start_date = st.sidebar.date_input("Date de départ", date(2018,1,1))
monthly = st.sidebar.number_input("Contribution mensuelle ($)", 0, step=100)

benchmark_name = st.sidebar.selectbox("Benchmark", ["S&P 500","Nasdaq"])
benchmark = "^GSPC" if benchmark_name=="S&P 500" else "^IXIC"

mode = st.sidebar.radio("Mode", ["Comparaison éducative","Portefeuille transactionnel"])

# ================= DATA =================
tickers = list(set(
    list(ETF_SIMPLE.keys())+
    list(GROWTH.keys())+
    list(RISKY.keys())+
    list(state["positions"].keys())
))

prices = yf.download(tickers+[benchmark], start=start_date)["Adj Close"].dropna()

# ================= FUNCTIONS =================
def simulate(weights):
    base = 100000
    pos = {t:(base*w)/prices.iloc[0][t] for t,w in weights.items()}
    cash = 0
    values=[]
    for i,d in enumerate(prices.index):
        if i%21==0:
            cash+=monthly
        total=cash+sum(pos[t]*prices.loc[d,t] for t in pos)
        values.append(total)
    return pd.Series(values,index=prices.index)

def cagr(series):
    years=(series.index[-1]-series.index[0]).days/365
    return (series.iloc[-1]/series.iloc[0])**(1/years)-1

# ================= MODE COMPARAISON =================
if mode=="Comparaison éducative":

    etf = simulate(ETF_SIMPLE)
    growth = simulate(GROWTH)
    risky = simulate(RISKY)
    bench = prices[benchmark]/prices[benchmark].iloc[0]*etf.iloc[0]

    st.subheader("📊 Comparaison des portefeuilles")
    st.line_chart(pd.DataFrame({
        "ETF simple":etf,
        "Croissance":growth,
        "Risqué":risky,
        benchmark_name:bench
    }))

    summary = pd.DataFrame({
        "Valeur finale ($)":[etf.iloc[-1],growth.iloc[-1],risky.iloc[-1]],
        "CAGR (%)":[cagr(etf)*100,cagr(growth)*100,cagr(risky)*100]
    },index=["ETF simple","Croissance","Risqué"])

    st.subheader("📈 Résumé")
    st.dataframe(summary.style.format({"Valeur finale ($)":"{:,.0f}","CAGR (%)":"{:.2f}"}))

# ================= MODE TRANSACTIONNEL =================
else:
    st.subheader("🔁 Gestion des positions")

    action = st.selectbox("Action",["Acheter","Vendre"])
    ticker = st.text_input("Ticker").upper()
    shares = st.number_input("Actions",0.0)
    price = st.number_input("Prix ($)",0.0)

    if st.button("Exécuter"):
        if action=="Acheter":
            cost=shares*price
            if state["cash"]>=cost:
                pos=state["positions"].get(ticker,{"shares":0,"avg":0})
                new_sh=pos["shares"]+shares
                pos["avg"]=(pos["shares"]*pos["avg"]+shares*price)/new_sh
                pos["shares"]=new_sh
                state["positions"][ticker]=pos
                state["cash"]-=cost
        else:
            if ticker in state["positions"] and state["positions"][ticker]["shares"]>=shares:
                state["cash"]+=shares*price
                state["positions"][ticker]["shares"]-=shares
                if state["positions"][ticker]["shares"]==0:
                    del state["positions"][ticker]

        state["transactions"].append({
            "date":str(date.today()),
            "ticker":ticker,
            "type":action,
            "shares":shares,
            "price":price
        })
        save_state(state)

    st.metric("💰 Cash disponible",f"${state['cash']:,.0f}")

    rows=[]
    for t,p in state["positions"].items():
        if t in prices.columns:
            m=prices[t].iloc[-1]
            val=p["shares"]*m
            cost=p["shares"]*p["avg"]
            pnl=val-cost
            rows.append({
                "Ticker":t,"Actions":p["shares"],
                "Prix moyen":round(p["avg"],2),
                "Prix actuel":round(m,2),
                "Valeur":round(val,2),
                "P&L $":round(pnl,2),
                "P&L %":round(pnl/cost*100,2)
            })

    st.subheader("📋 Positions")
    st.dataframe(pd.DataFrame(rows))

    st.subheader("🧾 Historique")
    st.dataframe(pd.DataFrame(state["transactions"]))

# ================= PDF =================
def export_pdf(title, df):
    pdf=FPDF()
    pdf.add_page()
    pdf.set_font("Arial","B",16)
    pdf.cell(0,10,title,ln=True)
    pdf.ln(5)
    pdf.set_font("Arial",size=12)
    for _,row in df.iterrows():
        pdf.cell(0,8," | ".join(str(x) for x in row.values),ln=True)
    return pdf

if st.button("📄 Export PDF"):
    if mode=="Comparaison éducative":
        pdf=export_pdf("Comparaison des portefeuilles",summary)
    else:
        pdf=export_pdf("Portefeuille transactionnel",pd.DataFrame(state["transactions"]))
    pdf.output("rapport_trading_en_action.pdf")
    st.download_button("⬇️ Télécharger",
        open("rapport_trading_en_action.pdf","rb"),
        file_name="rapport_trading_en_action.pdf",
        mime="application/pdf"
    )

st.markdown("""
---
### 🎓 Message clé
Même horizon long terme.  
Différentes structures → différentes expériences émotionnelles.  

👉 La performance est un résultat.  
👉 **La discipline est la stratégie.**
""")
