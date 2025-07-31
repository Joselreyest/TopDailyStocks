import streamlit as st
import pandas as pd
import yfinance as yf
import datetime
import requests
import io
from bs4 import BeautifulSoup
import time
from concurrent.futures import ThreadPoolExecutor
import os

# ------------------------------
# Config
# ------------------------------
EOD_API_KEY = os.getenv("EOD_API_KEY", "")

# ------------------------------
# Utility Functions
# ------------------------------
def load_symbols_from_file(uploaded_file):
    df = pd.read_csv(uploaded_file)
    return df['Symbol'].dropna().unique().tolist() if 'Symbol' in df.columns else []

def load_index_symbols(source):
    try:
        if source == "NASDAQ":
            url = "https://eodhistoricaldata.com/api/exchange-symbol-list/NASDAQ?api_token={}&fmt=csv".format(EOD_API_KEY)
        elif source == "NYSE":
            url = "https://eodhistoricaldata.com/api/exchange-symbol-list/NYSE?api_token={}&fmt=csv".format(EOD_API_KEY)
        elif source == "AMEX":
            url = "https://eodhistoricaldata.com/api/exchange-symbol-list/AMEX?api_token={}&fmt=csv".format(EOD_API_KEY)
        elif source == "S&P500":
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            html = requests.get(url).text
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table", {"id": "constituents"})
            df = pd.read_html(str(table))[0]
            return df['Symbol'].tolist()

        content = requests.get(url).content
        df = pd.read_csv(io.StringIO(content.decode("utf-8")))
        return df['Code'].dropna().tolist()
    except Exception as e:
        st.error(f"Error loading symbols from {source}: {e}")
        return []

def fetch_data_yf(symbol):
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        hist = ticker.history(period="1d")
        if hist.empty or 'Open' not in hist.columns:
            return None
        today = hist.iloc[-1]
        return {
            'symbol': symbol,
            'name': info.get("shortName", ""),
            'price': today["Close"],
            'percent_change': (today["Close"] - today["Open"]) / today["Open"] * 100,
            'volume': today["Volume"],
            'avg_volume': info.get("averageVolume", 0),
            'float': info.get("floatShares", 0),
            'market_cap': info.get("marketCap", 0),
            'pe_ratio': info.get("trailingPE", None),
            'sector': info.get("sector", ""),
            'analyst_rating': info.get("recommendationKey", ""),
            'volatility': info.get("beta", None),
            'pre_market_price': info.get("preMarketPrice", None)
        }
    except:
        return None

def fetch_data_eod(symbol):
    try:
        url = f"https://eodhistoricaldata.com/api/fundamentals/{symbol}.US?api_token={EOD_API_KEY}"
        r = requests.get(url)
        if r.status_code != 200:
            return None
        data = r.json()
        quote = data.get("General", {})
        return {
            'symbol': symbol,
            'name': quote.get("Name", ""),
            'price': quote.get("PreviousClose", 0),
            'percent_change': 0,  # EOD doesn't give today's change
            'volume': data.get("Highlights", {}).get("VolAvg", 0),
            'avg_volume': data.get("Highlights", {}).get("VolAvg", 0),
            'float': data.get("SharesStats", {}).get("SharesFloat", 0),
            'market_cap': data.get("Highlights", {}).get("MarketCapitalization", 0),
            'pe_ratio': data.get("Valuation", {}).get("TrailingPE", None),
            'sector': quote.get("Sector", ""),
            'analyst_rating': "",
            'volatility': data.get("Technicals", {}).get("Beta", None),
            'pre_market_price': None
        }
    except:
        return None

def process_symbol(symbol):
    reasons = []
    data = fetch_data_yf(symbol)
    if not data and EOD_API_KEY:
        data = fetch_data_eod(symbol)

    if not data:
        return None, ["Failed to fetch data"]

    if data["price"] is None or data["percent_change"] is None:
        return None, ["Incomplete data"]

    if data["percent_change"] < price_change_filter:
        reasons.append("Price change below threshold")

    rel_vol = data["volume"] / data["avg_volume"] if data["avg_volume"] else 0
    if rel_vol < rel_vol_filter:
        reasons.append("Low relative volume")

    if not (price_min <= data["price"] <= price_max):
        reasons.append("Price outside range")

    if data["float"] > max_float:
        reasons.append("Float too high")

    return data if not reasons else None, reasons

# ------------------------------
# App Function
# ------------------------------
def app():
    st.title("📈 Real-Time Stock Scanner")

    st.sidebar.header("Filter Criteria")
    global price_change_filter, rel_vol_filter, price_min, price_max, max_float

    price_change_filter = st.sidebar.slider("Min % Change Today", 0, 100, 10)
    rel_vol_filter = st.sidebar.slider("Min Relative Volume", 0.0, 10.0, 5.0)
    price_min, price_max = st.sidebar.slider("Price Range ($)", 0.01, 100.0, (2.0, 20.0))
    max_float = st.sidebar.number_input("Max Free Float", value=10_000_000)

    st.sidebar.header("Universe")
    source = st.sidebar.radio("Select Source", ["NASDAQ", "NYSE", "AMEX", "S&P500", "Upload File"])
    symbol_list = []

    if source == "Upload File":
        uploaded_file = st.sidebar.file_uploader("Upload CSV with 'Symbol' column", type=['csv'])
        if uploaded_file:
            symbol_list = load_symbols_from_file(uploaded_file)
    else:
        with st.spinner("Loading symbols..."):
            symbol_list = load_index_symbols(source)

    num_results = st.sidebar.slider("Number of Top Matches", 1, 20, 10)

    run = st.button("Run Scanner", disabled=(not symbol_list))
    if run:
        st.info(f"Scanning {len(symbol_list)} symbols. Please wait...")

        results = []
        skipped = {}

        progress_bar = st.progress(0)
        total = len(symbol_list)

        def worker(symbol):
            result, reasons = process_symbol(symbol)
            if result:
                results.append(result)
            else:
                skipped[symbol] = reasons
            progress_bar.progress(min(len(results) + len(skipped), total) / total)

        with ThreadPoolExecutor(max_workers=20) as executor:
            executor.map(worker, symbol_list)

        progress_bar.empty()

        if results:
            df = pd.DataFrame(results)
            df = df.sort_values(by="percent_change", ascending=False).head(num_results)
            st.success(f"Found {len(df)} matching stocks")
            st.dataframe(df)

            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button("📥 Download Results", csv, "scan_results.csv", "text/csv")

        if skipped:
            st.write("### ❌ Skipped Symbols Log")
            skip_log = pd.DataFrame([{"Symbol": k, "Reasons": ", ".join(v)} for k, v in skipped.items()])
            st.dataframe(skip_log)

            log_csv = skip_log.to_csv(index=False).encode('utf-8')
            st.download_button("📥 Download Skipped Log", log_csv, "skipped_symbols.csv", "text/csv")

# ------------------------------
# Run App
# ------------------------------
if __name__ == '__main__':
    app()
