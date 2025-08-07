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
from io import StringIO
from dotenv import load_dotenv

load_dotenv()
# ------------------------------
# Configuration
# ------------------------------
EOD_API_KEY = os.getenv("EOD_API_KEY", "")

# Constants
DEFAULT_PRICE_RANGE = (2, 20)
DEFAULT_REL_VOLUME = 5
MAX_WORKERS = 5
RATE_LIMIT_DELAY = 1  # seconds
EOD_API_KEY = os.getenv("EOD_API_KEY")

SKIPPED_SYMBOLS_LOG = []

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

def fetch_info(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        if info is None:
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

            # Rate limit handling
            time.sleep(RATE_LIMIT_DELAY)
            st.progress((i + 1) / total)

    return results
    
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
            reasons.append("% change below threshold", pct_change)

        volume = data["Volume"].iloc[-1]
        avg_volume = info.get("averageVolume") or 0
        if avg_volume == 0 or volume / avg_volume < rel_vol_filter:
            reasons.append("Low relative volume", avg_volume)

        if not (price_min <= current <= price_max):
            reasons.append("Price out of range ", current)

        free_float = info.get("floatShares") or 0
        if free_float > max_float:
            reasons.append("Free float too high", free_float)

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
    st.set_page_config(page_title="Top Daily Stocks Scanner", layout="wide")
    st.title("📈 Top Daily Stocks Scanner")

    # Sidebar - Inputs
    with st.sidebar:
        symbol_source = st.radio("Select Symbols Source", ["S&P500", "NASDAQ", "NYSE", "AMEX", "Upload File"])

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
        else:
            st.warning("No stocks matched the criteria.")

        if SKIPPED_SYMBOLS_LOG:
            st.markdown("---")
            st.subheader("⚠️ Skipped Symbols")
            for symbol, reason in SKIPPED_SYMBOLS_LOG:
                st.write(f"{symbol}: {reason}")

if __name__ == "__main__":
    app()

