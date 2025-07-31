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

# ------------------------------
# Configuration
# ------------------------------
EOD_API_KEY = "your_eod_api_key_here"  # Replace with your actual API key

# ------------------------------
# Helper functions
# ------------------------------
@lru_cache(maxsize=5)
def get_exchange_symbols(exchange):
    try:
        exchange_map = {
            "NYSE": "US",
            "AMEX": "US",
            "NASDAQ": "US"
        }
        country_code = exchange_map.get(exchange.upper())
        if not country_code:
            raise ValueError("Unsupported exchange")

        url = f"https://eodhistoricaldata.com/api/exchange-symbol-list/{country_code}?api_token={EOD_API_KEY}&fmt=json"
        response = requests.get(url)
        if response.status_code != 200:
            raise ValueError(f"EOD API error: {response.status_code}")

        symbols_data = response.json()
        filtered = [item['Code'] for item in symbols_data if item.get("Exchange", "").upper() == exchange.upper()]
        return filtered
    except Exception as e:
        st.error(f"Failed to load {exchange} symbols: {e}")
        return []

@lru_cache(maxsize=5)
def load_index_symbols(index):
    try:
        if index == "NASDAQ":
            return get_exchange_symbols("NASDAQ")
        elif index == "S&P500":
            url = 'https://datahub.io/core/s-and-p-500-companies/r/constituents.csv'
            df = pd.read_csv(url)
            symbol_col = next((col for col in df.columns if col.strip().lower() == "symbol"), None)
            if not symbol_col:
                return []
            return df[symbol_col].dropna().unique().tolist()
        elif index in ["NYSE", "AMEX"]:
            return get_exchange_symbols(index)
        else:
            return []
    except Exception as e:
        st.error(f"Failed to load symbols for {index}: {e}")
        return []

def fetch_latest_news(symbol):
    try:
        url = f"https://finviz.com/quote.ashx?t={symbol}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        news_table = soup.find('table', class_='fullview-news-outer')
        rows = news_table.findAll('tr') if news_table else []
        headlines = [row.a.text for row in rows[:3] if row.a]
        return " | ".join(headlines)
    except:
        return ""

def process_symbol(symbol):
    reason_skipped = []
    try:
        stock = yf.Ticker(symbol)
        hist = stock.history(period="2d")
        info = stock.info

        if len(hist) < 2:
            reason_skipped.append("Not enough historical data")
            return None, reason_skipped

        price_today = hist['Close'].iloc[-1]
        price_yesterday = hist['Close'].iloc[-2]
        percent_change = ((price_today - price_yesterday) / price_yesterday) * 100

        volume_today = hist['Volume'].iloc[-1]
        avg_volume = info.get('averageVolume', 0)
        rel_volume = volume_today / avg_volume if avg_volume else 0

        if percent_change < price_change_filter:
            reason_skipped.append(f"% Change {percent_change:.2f} < {price_change_filter}")
        if rel_volume < rel_vol_filter:
            reason_skipped.append(f"Rel Vol {rel_volume:.2f} < {rel_vol_filter}")
        if not (price_min <= price_today <= price_max):
            reason_skipped.append(f"Price {price_today:.2f} not in range {price_min}-{price_max}")

        float_shares = info.get('floatShares') or 0
        if float_shares > max_float:
            reason_skipped.append(f"Float {float_shares} > {max_float}")

        if reason_skipped:
            return None, reason_skipped

        headlines = fetch_latest_news(symbol)

        return {
            'Symbol': symbol,
            'Name': info.get('shortName'),
            'Price': round(price_today, 2),
            '% Change': round(percent_change, 2),
            'Volume': volume_today,
            'Float': float_shares,
            'Market Cap': info.get('marketCap'),
            'P/E': info.get('trailingPE'),
            'Sector': info.get('sector'),
            'Analyst Rating': info.get('recommendationKey'),
            'Volatility': info.get('beta'),
            'Pre-Market Price': info.get('preMarketPrice'),
            'Recent News': headlines
        }, None

    except Exception as e:
        return None, [str(e)]

def fetch_stock_data(symbols):
    data = []
    debug_log = []
    total = len(symbols)
    progress_text = st.empty()
    progress_bar = st.progress(0.0)

    def update_progress(i):
        progress = float(i + 1) / float(total)
        progress_bar.progress(progress)
        progress_text.text(f"Scanning ({i+1}/{total})...")

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(process_symbol, symbol): symbol for symbol in symbols}
        for i, future in enumerate(futures):
            result, reason_skipped = future.result()
            if result:
                data.append(result)
            elif debug_mode:
                debug_log.append(f"{futures[future]}: SKIPPED => {', '.join(reason_skipped)}")
            update_progress(i)

    progress_text.text("Scan complete.")
    if debug_mode and debug_log:
        with open("skipped_symbols_log.txt", "w") as f:
            for log in debug_log:
                f.write(log + "\n")
        with open("skipped_symbols_log.txt", "rb") as f:
            st.download_button("Download Skipped Symbols Log", f, file_name="skipped_symbols_log.txt")

    return pd.DataFrame(data)

# ------------------------------
# Streamlit UI
# ------------------------------
st.title("📈 Market Demand & Supply Stock Scanner")

with st.sidebar:
    st.header("🔧 Scanner Settings")
    source = st.radio("Select Symbol Source", ["NASDAQ", "S&P500", "NYSE", "AMEX", "Upload File"])

    price_min, price_max = st.slider("Price Range ($)", 0.01, 100.0, (2.0, 20.0))
    max_float = st.slider("Max Float (shares)", 200_000, 10_000_000, 10_000_000)
    top_n = st.slider("Top N Stocks to Show", 1, 20, 10)

    price_change_filter = st.number_input("Min % Price Increase Today", min_value=0.0, max_value=100.0, value=10.0)
    rel_vol_filter = st.number_input("Min Relative Volume (x Avg)", min_value=0.0, max_value=20.0, value=5.0)
    debug_mode = st.checkbox("Enable Debug Log")

if st.button("🔍 Run Scanner"):
    with st.spinner("Preparing symbol list..."):
        if source == "Upload File":
            uploaded_file = st.file_uploader("Upload CSV with 'Symbol' column")
            if uploaded_file:
                symbols = load_symbols_from_file(uploaded_file)
            else:
                st.warning("Please upload a file.")
                symbols = []
        else:
            symbols = load_index_symbols(source)

    if symbols:
        with st.spinner("Scanning market..."):
            df = fetch_stock_data(symbols)
            if not df.empty:
                df = df.sort_values(by='% Change', ascending=False).head(top_n)
                st.success(f"Found {len(df)} stocks matching criteria")
                st.dataframe(df)
            else:
                st.warning("No stocks matched the criteria.")
    else:
        st.warning("No symbols loaded. Please check the selected source or uploaded file.")
