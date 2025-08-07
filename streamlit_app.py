import os
import streamlit as st
import pandas as pd
import yfinance as yf
import concurrent.futures
from concurrent import futures  # fallback
import time
from datetime import datetime
from io import StringIO
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup

load_dotenv()

# Constants
DEFAULT_PRICE_RANGE = (2, 20)
DEFAULT_REL_VOLUME = 5
MAX_WORKERS = 5
RATE_LIMIT_DELAY = 1  # seconds
EOD_API_KEY = os.getenv("EOD_API_KEY")

SKIPPED_SYMBOLS_LOG = []

EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]

def load_symbols_from_file(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file)
        if 'Symbol' not in df.columns:
            st.error("Uploaded CSV must contain a 'Symbol' column.")
            return []
        return df['Symbol'].dropna().unique().tolist()
    except Exception as e:
        st.error(f"Failed to load symbols from file: {e}")
        return []

def fetch_info(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info

        if not info:
            SKIPPED_SYMBOLS_LOG.append((ticker_symbol, "Missing Yahoo info"))
            return None

        price = info.get("regularMarketPrice")
        avg_volume = info.get("averageVolume")
        volume = info.get("volume")
        float_shares = info.get("floatShares")

        if not all([price, avg_volume, volume]):
            SKIPPED_SYMBOLS_LOG.append((ticker_symbol, "Missing volume/price data"))
            return None

        if float_shares is None:
            float_shares = fetch_float_from_eod(ticker_symbol)
            if float_shares is None:
                SKIPPED_SYMBOLS_LOG.append((ticker_symbol, "Missing float data"))
                return None

        rel_volume = volume / avg_volume if avg_volume else 0

        return {
            "Symbol": ticker_symbol,
            "Price": price,
            "Volume": volume,
            "Rel Volume": rel_volume,
            "Float": float_shares,
        }
    except Exception as e:
        SKIPPED_SYMBOLS_LOG.append((ticker_symbol, f"Exception: {e}"))
        return None

def fetch_float_from_eod(symbol):
    try:
        url = f"https://eodhistoricaldata.com/api/fundamentals/{symbol}.US?api_token={EOD_API_KEY}"
        response = requests.get(url)
        if response.status_code != 200:
            return None
        data = response.json()
        shares_float = data.get("SharesStats", {}).get("SharesFloat")
        return shares_float
    except:
        return None

def scan_symbols(symbols, price_range, min_rel_volume):
    results = []
    total = len(symbols)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_info, symbol): symbol for symbol in symbols
        }

        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            result = future.result()
            if result:
                if price_range[0] <= result["Price"] <= price_range[1] and result["Rel Volume"] >= min_rel_volume:
                    results.append(result)

            time.sleep(RATE_LIMIT_DELAY)
            st.progress((i + 1) / total)

    return results

@st.cache_data(show_spinner=False)
def get_index_symbols(source):
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

def app():
    st.set_page_config(page_title="Top Daily Stocks Scanner", layout="wide")
    st.title("📈 Top Daily Stocks Scanner")

    # Sidebar - Inputs
    with st.sidebar:
        symbol_source = st.radio("Select Symbols Source", ["S&P500"] + EXCHANGES + ["Upload File"])

        if symbol_source == "Upload File":
            uploaded_file = st.file_uploader("Upload CSV File", type=["csv"])
            if uploaded_file:
                symbol_list = load_symbols_from_file(uploaded_file)
            else:
                symbol_list = []
        else:
            symbol_list = get_index_symbols(symbol_source)

        st.markdown("---")
        price_range = st.slider("Price Range ($)", 0.01, 100.0, DEFAULT_PRICE_RANGE, 0.01)
        min_rel_volume = st.slider("Min Relative Volume", 1.0, 20.0, float(DEFAULT_REL_VOLUME), 0.5)
        run_scan = st.button("🚀 Run Scanner", disabled=(len(symbol_list) == 0))

    if run_scan:
        with st.spinner("Scanning symbols, please wait..."):
            results = scan_symbols(symbol_list, price_range, min_rel_volume)

        if results:
            df = pd.DataFrame(results)
            df["Volume"] = df["Volume"].apply(lambda x: f"{int(x):,}")
            df["Float"] = df["Float"].apply(lambda x: f"{int(x):,}")
            df = df.sort_values(by="Rel Volume", ascending=False)
            st.success(f"✅ Found {len(df)} matching stocks")
            st.dataframe(df, use_container_width=True)

            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Download CSV", csv, "top_stocks.csv", "text/csv")
        else:
            st.warning("No stocks matched the criteria.")

        if SKIPPED_SYMBOLS_LOG:
            st.markdown("---")
            st.subheader("⚠️ Skipped Symbols")
            for symbol, reason in SKIPPED_SYMBOLS_LOG:
                st.write(f"{symbol}: {reason}")

if __name__ == "__main__":
    app()
