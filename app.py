import os, json
from datetime import datetime, date
import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

# ----------------------------
# CONFIG
# ----------------------------
st.set_page_config(page_title="TEA Paper Trading Long Terme", layout="wide")

DATA_DIR = "data"
TX_PATH = os.path.join(DATA_DIR, "transactions.csv")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

PORTFOLIOS = ["Conservateur", "Modéré", "Agressif"]
DEFAULT_BENCHMARKS = ["SPY", "QQQ", "VT"]  # S&P 500, Nasdaq 100, World (VT). (Alternative: "AGG" pour obligations)

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "initial_cash": {pf: 10000.0 for pf in PORTFOLIOS},
    "benchmarks": DEFAULT_BENCHMARKS,
    "drip": False,     # si True: dividendes réinvestis (option future)
    "fees": 0.0        # frais fixes par transaction (option)
}

# ----------------------------
# LOAD / SAVE
# ----------------------------
def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2, ensure_ascii=False)
        return DEFAULT_SETTINGS
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        s = json.load(f)
    # fallback keys
    for k, v in DEFAULT_SETTINGS.items():
        if k not in s:
            s[k] = v
    return s

def save_settings(s):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

def load_transactions():
    if not os.path.exists(TX_PATH):
        df = pd.DataFrame(columns=["date", "portfolio", "ticker", "side", "quantity", "price", "fee"])
        df.to_csv(TX_PATH, index=False)
        return df
    df = pd.read_csv(TX_PATH)
    if df.empty:
        return pd.DataFrame(columns=["date", "portfolio", "ticker", "side", "quantity", "price", "fee"])
    # types
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["ticker"] = df["ticker"].str.upper().str.strip()
    df["side"] = df["side"].str.upper().str.strip()
    df["quantity"] = df["quantity"].astype(float)
    df["price"] = df["price"].astype(float)
    if "fee" not in df.columns:
        df["fee"] = 0.0
    df["fee"] = df["fee"].astype(float)
    return df.sort_values("date").reset_index(drop=True)

def append_transaction(tx_row: dict):
    df = load_transactions()
    df = pd.concat([df, pd.DataFrame([tx_row])], ignore_index=True)
    df.to_csv(TX_PATH, index=False)

# ----------------------------
# YFINANCE HELPERS
# ----------------------------
@st.cache_data(ttl=60*60*6)
def fetch_history(tickers, start):
    """
    Returns a dict[ticker] -> dataframe with columns:
    Close, Dividends, Stock Splits
    """
    out = {}
    for t in tickers:
        hist = yf.Ticker(t).history(start=start, auto_adjust=False, actions=True)
        if hist is None or hist.empty:
            continue
        hist = hist[["Close", "Dividends", "Stock Splits"]].copy()
        hist.index = pd.to_datetime(hist.index).date
        out[t] = hist
    return out

@st.cache_data(ttl=60*30)
def fetch_last_price(ticker):
    h = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
    if h is None or h.empty:
        return None
    return float(h["Close"].iloc[-1])

# ----------------------------
# PORTFOLIO ENGINE
# ----------------------------
def build_signed_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Signed qty (+buy, -sell) and signed cash flows."""
    if df.empty:
        return df
    d = df.copy()
    d["signed_qty"] = np.where(d["side"] == "BUY", d["quantity"], -d["quantity"])
    # cash: buy -> -, sell -> +, minus fee
    d["cash_flow"] = np.where(d["side"] == "BUY", -d["quantity"]*d["price"], d["quantity"]*d["price"]) - d["fee"]
    return d

def daily_positions_from_trades(trades: pd.DataFrame, all_days: pd.Index) -> pd.DataFrame:
    """
    Returns multiindex columns: (portfolio, ticker) -> shares held each day (pre-splits adjustment).
    """
    if trades.empty:
        return pd.DataFrame(index=all_days)

    # aggregate daily signed quantities
    g = trades.groupby(["date", "portfolio", "ticker"])["signed_qty"].sum().reset_index()
    # pivot to daily grid
    piv = g.pivot_table(index="date", columns=["portfolio", "ticker"], values="signed_qty", aggfunc="sum").fillna(0.0)
    piv = piv.reindex(all_days, fill_value=0.0)
    shares = piv.cumsum()
    return shares

def apply_splits_to_shares(shares: pd.DataFrame, histories: dict) -> pd.DataFrame:
    """
    Apply stock splits to share counts over time.
    If split factor is 4.0 on date D, then shares from D onward multiply by 4.
    """
    if shares.empty:
        return shares

    adj = shares.copy()
    for (pf, tkr) in adj.columns:
        if tkr not in histories:
            continue
        splits = histories[tkr]["Stock Splits"]
        splits = splits[splits > 0]
        if splits.empty:
            continue

        # Build multiplier series over the full index
        mult = pd.Series(1.0, index=adj.index)
        for d, factor in splits.items():
            if d in mult.index:
                mult.loc[d:] *= float(factor)
        adj[(pf, tkr)] = adj[(pf, tkr)] * mult.values
    return adj

def daily_cash_series(trades: pd.DataFrame, all_days: pd.Index, initial_cash: dict) -> pd.DataFrame:
    """
    Returns cash balance per portfolio by day (without dividends yet).
    """
    cash = pd.DataFrame(index=all_days, columns=PORTFOLIOS, data=0.0)
    for pf in PORTFOLIOS:
        cash.loc[:, pf] = float(initial_cash.get(pf, 0.0))

    if trades.empty:
        return cash

    cf = trades.groupby(["date", "portfolio"])["cash_flow"].sum().unstack("portfolio").fillna(0.0)
    cf = cf.reindex(all_days, fill_value=0.0)
    cash = cash + cf.cumsum()
    return cash

def dividends_cashflows(shares_adj: pd.DataFrame, histories: dict, all_days: pd.Index) -> pd.DataFrame:
    """
    Compute dividends cashflows per portfolio by day using shares held on dividend dates.
    Adds dividends to CASH (not reinvested by default).
    """
    div_cf = pd.DataFrame(index=all_days, columns=PORTFOLIOS, data=0.0)
    if shares_adj.empty:
        return div_cf

    # For each ticker: on dividend date, dividend_per_share * shares_held_that_day
    for (pf, tkr) in shares_adj.columns:
        if tkr not in histories:
            continue
        divs = histories[tkr]["Dividends"]
        divs = divs[divs > 0]
        if divs.empty:
            continue
        for d, div_per_share in divs.items():
            if d in div_cf.index and d in shares_adj.index:
                qty = float(shares_adj.loc[d, (pf, tkr)])
                if qty > 0:
                    div_cf.loc[d, pf] += qty * float(div_per_share)

    return div_cf

def portfolio_value_series(shares_adj: pd.DataFrame, histories: dict, all_days: pd.Index) -> pd.DataFrame:
    """
    Compute daily market value of holdings (without cash) per portfolio.
    """
    val = pd.DataFrame(index=all_days, columns=PORTFOLIOS, data=0.0)
    if shares_adj.empty:
        return val

    # Build price panel aligned on all_days
    price = {}
    for tkr, hist in histories.items():
        # align close to all_days (ffill)
        s = pd.Series(hist["Close"].copy())
        s = s.reindex(all_days).ffill()
        price[tkr] = s

    for (pf, tkr) in shares_adj.columns:
        if tkr not in price:
            continue
        qty = shares_adj[(pf, tkr)]
        val[pf] += qty * price[tkr].values

    return val

def benchmark_series(histories: dict, bench: list, all_days: pd.Index) -> pd.DataFrame:
    """
    Price series for benchmarks; normalized to 100 at start day.
    """
    bdf = pd.DataFrame(index=all_days)
    for b in bench:
        if b not in histories:
            continue
        s = pd.Series(histories[b]["Close"].copy()).reindex(all_days).ffill()
        if s.dropna().empty:
            continue
        base = float(s.dropna().iloc[0])
        bdf[b] = (s / base) * 100.0
    return bdf

# ----------------------------
# UI
# ----------------------------
# Header image (optional)
if os.path.exists("header.png"):
    st.image("header.png", use_container_width=True)

settings = load_settings()
tx = load_transactions()
trades = build_signed_trades(tx)

# Sidebar settings
st.sidebar.header("⚙️ Paramètres")
st.sidebar.subheader("Cash initial")
for pf in PORTFOLIOS:
    settings["initial_cash"][pf] = float(st.sidebar.number_input(
        f"{pf} – cash initial",
        min_value=0.0,
        value=float(settings["initial_cash"].get(pf, 10000.0)),
        step=500.0
    ))
st.sidebar.subheader("Benchmarks")
benchmarks = st.sidebar.multiselect(
    "Comparer contre",
    options=["SPY", "QQQ", "VT", "AGG", "DIA", "IWM"],
    default=settings.get("benchmarks", DEFAULT_BENCHMARKS)
)
settings["benchmarks"] = benchmarks if benchmarks else DEFAULT_BENCHMARKS
settings["fees"] = float(st.sidebar.number_input("Frais par transaction (option)", min_value=0.0, value=float(settings.get("fees", 0.0)), step=0.25))
save_settings(settings)

st.title("📈 TEA – Mini Paper Trading Long Terme")
st.caption("Paper trading éducatif – les transactions sont enregistrées localement (CSV) et les prix viennent de Yahoo Finance.")

tab_trade, tab_dashboard, tab_tx = st.tabs(["🧾 Trader", "📊 Dashboard", "📜 Transactions"])

# ----------------------------
# TRADE TAB
# ----------------------------
with tab_trade:
    st.subheader("Acheter / Vendre (paper trading)")
    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1, 1])

    with st.form("trade_form"):
        portfolio = c1.selectbox("Portefeuille", PORTFOLIOS)
        ticker = c2.text_input("Ticker (ex: AAPL)", value="AAPL").upper().strip()
        side = c3.selectbox("Sens", ["BUY", "SELL"])
        qty = c4.number_input("Quantité", min_value=0.01, value=1.0, step=1.0)

        # auto price helper
        last = fetch_last_price(ticker)
        colp1, colp2 = st.columns([1, 1])
        price = colp1.number_input("Prix (tu peux le changer)", min_value=0.01, value=float(last) if last else 1.0, step=0.01)
        fee = colp2.number_input("Frais", min_value=0.0, value=float(settings.get("fees", 0.0)), step=0.25)

        trade_date = st.date_input("Date", value=date.today())
        submitted = st.form_submit_button("Enregistrer la transaction")

        if submitted:
            if ticker == "":
                st.error("Ticker obligatoire.")
            else:
                append_transaction({
                    "date": trade_date.isoformat(),
                    "portfolio": portfolio,
                    "ticker": ticker,
                    "side": side,
                    "quantity": float(qty),
                    "price": float(price),
                    "fee": float(fee)
                })
                st.success("✅ Transaction enregistrée. Va voir le Dashboard.")
                st.rerun()

    st.divider()
    st.markdown("**Astuce** : tu peux vendre partiellement. Les positions fermées restent dans l’historique et le rendement total tient compte du réalisé + latent.")

# ----------------------------
# DASHBOARD TAB
# ----------------------------
with tab_dashboard:
    st.subheader("Résumé & évolution")
    if tx.empty:
        st.info("Aucune transaction pour l’instant. Ajoute un trade dans l’onglet Trader.")
    else:
        # date range
        start = pd.to_datetime(tx["date"]).min()
        start = start if pd.notna(start) else pd.Timestamp.today()
        start_date = start.date()

        all_days = pd.date_range(start=start_date, end=date.today(), freq="B").date  # jours de bourse
        all_days = pd.Index(all_days, name="date")

        # tickers needed (assets + benchmarks)
        asset_tickers = sorted(tx["ticker"].unique().tolist())
        all_tickers = sorted(list(set(asset_tickers + settings["benchmarks"])))

        histories = fetch_history(all_tickers, start=start_date.isoformat())

        shares = daily_positions_from_trades(trades, all_days)
        shares_adj = apply_splits_to_shares(shares, histories)

        cash = daily_cash_series(trades, all_days, settings["initial_cash"])
        div_cf = dividends_cashflows(shares_adj, histories, all_days)
        cash_with_div = cash + div_cf.cumsum()

        holdings_value = portfolio_value_series(shares_adj, histories, all_days)
        total_value = holdings_value + cash_with_div

        # KPIs today
        today = total_value.index[-1]
        total_all = float(total_value.loc[today].sum())
        cash_all = float(cash_with_div.loc[today].sum())
        invested_flow = trades.groupby("portfolio")["cash_flow"].sum()  # net cash impact of trades (sell positive, buy negative)
        # "Contributions" approx = initial_cash - current_cash + market_value? (not perfect). We'll show P&L instead:
        # P&L total = current_total - sum(initial_cash) - net_external? Here external is none.
        pnl_total = total_all - sum(settings["initial_cash"].values())

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Valeur totale (3 portefeuilles)", f"${total_all:,.2f}")
        k2.metric("Cash total", f"${cash_all:,.2f}")
        k3.metric("P&L total vs cash initial", f"${pnl_total:,.2f}")
        best_pf = total_value.loc[today].idxmax()
        k4.metric("Meilleur profil (valeur)", best_pf)

        st.divider()

        # Per-portfolio cards
        st.markdown("### Portefeuilles (aujourd’hui)")
        cols = st.columns(3)
        for col, pf in zip(cols, PORTFOLIOS):
            with col:
                v = float(total_value.loc[today, pf])
                c = float(cash_with_div.loc[today, pf])
                hv = float(holdings_value.loc[today, pf])
                div_total = float(div_cf[pf].sum())
                st.markdown(f"#### {pf}")
                st.metric("Valeur totale", f"${v:,.2f}")
                st.metric("Valeur positions", f"${hv:,.2f}")
                st.metric("Cash", f"${c:,.2f}")
                st.metric("Dividendes cumulés", f"${div_total:,.2f}")

        st.divider()

        # Evolution chart (normalized to 100)
        st.markdown("### Courbe d’évolution (normalisée)")
        base_total = total_value.iloc[0].sum()
        norm_port = (total_value.sum(axis=1) / float(base_total)) * 100.0
        curve = pd.DataFrame({"TEA (Total)": norm_port}, index=total_value.index)

        bench_norm = benchmark_series(histories, settings["benchmarks"], all_days)
        plot_df = curve.join(bench_norm, how="left")

        st.line_chart(plot_df)

        st.caption("Normalisé à 100 au départ. TEA inclut cash + dividendes crédités au cash + valeur des positions.")

        st.divider()

        # Table holdings snapshot
        st.markdown("### Positions ouvertes (aujourd’hui)")
        rows = []
        for (pf, tkr) in shares_adj.columns:
            qty_today = float(shares_adj.loc[today, (pf, tkr)])
            if qty_today <= 0:
                continue
            last_px = histories.get(tkr, pd.DataFrame()).get("Close", pd.Series(dtype=float))
            if isinstance(last_px, pd.Series) and not last_px.empty:
                px = float(pd.Series(last_px).reindex([today]).ffill().iloc[-1]) if today in pd.Series(last_px).index else float(pd.Series(last_px).iloc[-1])
            else:
                px = None
            rows.append({
                "Portefeuille": pf,
                "Ticker": tkr,
                "Quantité": round(qty_today, 4),
                "Prix (approx)": round(px, 2) if px else np.nan,
                "Valeur": round(qty_today * px, 2) if px else np.nan
            })
        st.dataframe(pd.DataFrame(rows).sort_values(["Portefeuille","Ticker"]), use_container_width=True)

        # Dividends log
        st.markdown("### Dividendes (journal)")
        div_log = []
        for pf in PORTFOLIOS:
            s = div_cf[pf]
            s = s[s > 0]
            for d, amt in s.items():
                div_log.append({"Date": d, "Portefeuille": pf, "Dividendes crédités": round(float(amt), 2)})
        st.dataframe(pd.DataFrame(div_log).sort_values("Date") if div_log else pd.DataFrame(), use_container_width=True)

# ----------------------------
# TRANSACTIONS TAB
# ----------------------------
with tab_tx:
    st.subheader("Historique des transactions")
    st.dataframe(load_transactions(), use_container_width=True)

    st.divider()
    st.subheader("⚠️ Supprimer une transaction (option)")
    st.caption("Pour l’instant, suppression manuelle : ouvre data/transactions.csv et retire la ligne. (On peut ajouter un module de suppression sécurisée.)")

st.caption("Note: Ceci est un outil éducatif / paper trading, pas un conseil financier.")
