import streamlit as st
import pandas as pd
import yfinance as yf
import datetime
import requests
import io
from bs4 import BeautifulSoup
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import os
from threading import Lock
from stqdm import stqdm

# ------------------------------
# Configuration
# ------------------------------
EOD_API_KEY = os.getenv("EOD_API_KEY", "")

# ------------------------------
# Utility Functions
# ------------------------------
def load_symbols_from_file(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file)
        if 'Symbol' not in df.columns:
            st.error("Uploaded file must contain a 'Symbol' column.")
            return []
        return df['Symbol'].dropna().unique().tolist()
    except Exception as e:
        st.error(f"Error reading file: {e}")
        return []

def load_index_symbols(source):
    try:
        if source == "NASDAQ":
            url = f"https://eodhistoricaldata.com/api/exchange-symbol-list/NASDAQ?api_token={EOD_API_KEY}&fmt=json"
            response = requests.get(url)
            data = response.json()
            return [item['Code'] for item in data if item.get('Code')]

        elif source == "NYSE":
            url = f"https://eodhistoricaldata.com/api/exchange-symbol-list/NYSE?api_token={EOD_API_KEY}&fmt=json"
            response = requests.get(url)
            data = response.json()
            return [item['Code'] for item in data if item.get('Code')]

        elif source == "AMEX":
            url = f"https://eodhistoricaldata.com/api/exchange-symbol-list/AMEX?api_token={EOD_API_KEY}&fmt=json"
            response = requests.get(url)
            data = response.json()
            return [item['Code'] for item in data if item.get('Code')]

        elif source == "S&P500":
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            html = requests.get(url).text
            soup = BeautifulSoup(html, "lxml")
            table = soup.find("table", {"id": "constituents"})
            df = pd.read_html(str(table))[0]
            return df['Symbol'].tolist()

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
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d")
        if data.empty:
            return None, ["No intraday data"]

        info = ticker.info

        reasons = []
        current = data["Close"].iloc[-1]
        prev_close = data["Close"].iloc[0]
        pct_change = ((current - prev_close) / prev_close) * 100

        if pct_change < price_change_filter:
            reasons.append("% change below threshold")

        volume = data["Volume"].iloc[-1]
        avg_volume = info.get("averageVolume") or 0
        if avg_volume == 0 or volume / avg_volume < rel_vol_filter:
            reasons.append("Low relative volume")

        if not (price_min <= current <= price_max):
            reasons.append("Price out of range")

        free_float = info.get("floatShares") or 0
        if free_float > max_float:
            reasons.append("Free float too high")

        if reasons:
            return None, reasons

        return {
            "Symbol": symbol,
            "Price": round(current, 2),
            "% Change": round(pct_change, 2),
            "Volume": f"{volume:,}",
            "Avg Volume": f"{avg_volume:,}",
            "Float": f"{free_float:,}"
        }, None

    except Exception as e:
        return None, [str(e)]

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
    max_float = st.sidebar.number_input("Max Free Float", value=100_000_000)

    source = st.radio("Choose Symbol Source", ["NASDAQ", "NYSE", "AMEX", "S&P500", "Upload File"])
    symbol_list = []

    if source == "Upload File":
        uploaded_file = st.file_uploader("Upload CSV with 'Symbol' column")
        if uploaded_file:
            symbol_list = load_symbols_from_file(uploaded_file)
    else:
        with st.spinner("Loading symbols..."):
            symbol_list = load_index_symbols(source)

    run_scan = st.button("Run Scanner", disabled=(source == "Upload File" and not symbol_list))

    if run_scan:
        if not symbol_list:
            st.warning("No symbols to scan. Please check your selection or upload.")
            return

        st.info(f"Scanning {len(symbol_list)} symbols. Please wait...")

        results = []
        skipped = {}

        def worker(symbol):
            result, reasons = process_symbol(symbol)
            if result:
                results.append(result)
            elif reasons:
                skipped[symbol] = reasons

        with ThreadPoolExecutor(max_workers=20) as executor:
            list(stqdm(executor.map(worker, symbol_list), total=len(symbol_list)))

        if results:
            df = pd.DataFrame(results)
            st.success(f"Found {len(df)} matching stocks")
            st.dataframe(df)

            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button("Download Results as CSV", csv, "scan_results.csv", "text/csv")

        if skipped:
            st.write("### Skipped Symbols Log")
            skip_log = pd.DataFrame([{"Symbol": k, "Reasons": ", ".join(v)} for k, v in skipped.items()])
            st.dataframe(skip_log)

            log_csv = skip_log.to_csv(index=False).encode('utf-8')
            st.download_button("Download Skipped Log", log_csv, "skipped_symbols.csv", "text/csv")

# Call app
if __name__ == '__main__':
    app()
