import streamlit as st
import pandas as pd
import yfinance as yf
import datetime
import requests
import io
from bs4 import BeautifulSoup
import time

# ------------------------------
# Helper functions
# ------------------------------
def load_symbols_from_file(uploaded_file):
    df = pd.read_csv(uploaded_file)
    symbol_col = next((col for col in df.columns if col.strip().lower() == "symbol"), None)
    if not symbol_col:
        st.error("Uploaded file must contain a 'Symbol' column (case-insensitive).")
        return []
    return df[symbol_col].dropna().unique().tolist()

def load_index_symbols(index):
    try:
        if index == "NASDAQ":
            url = 'https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt'
            response = requests.get(url)
            lines = response.text.splitlines()
            data = [line.split('|') for line in lines[1:-1]]  # Skip header and footer
            df = pd.DataFrame(data, columns=lines[0].split('|'))
            return df['Symbol'].dropna().unique().tolist()
        elif index == "S&P500":
            url = 'https://datahub.io/core/s-and-p-500-companies/r/constituents.csv'
            df = pd.read_csv(url)
            symbol_col = next((col for col in df.columns if col.strip().lower() == "symbol"), None)
            if not symbol_col:
                return []
            return df[symbol_col].dropna().unique().tolist()
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

def fetch_stock_data(symbols):
    data = []
    total = len(symbols)
    progress_text = st.empty()
    progress_bar = st.progress(0.0)

    for i, symbol in enumerate(symbols):
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period="2d")
            info = stock.info

            if len(hist) < 2:
                continue

            price_today = hist['Close'].iloc[-1]
            price_yesterday = hist['Close'].iloc[-2]
            percent_change = ((price_today - price_yesterday) / price_yesterday) * 100

            volume_today = hist['Volume'].iloc[-1]
            avg_volume = info.get('averageVolume', 0)
            rel_volume = volume_today / avg_volume if avg_volume else 0

            if percent_change < price_change_filter or rel_volume < rel_vol_filter:
                continue

            if not (price_min <= price_today <= price_max):
                continue

            float_shares = info.get('floatShares') or 0
            if float_shares > max_float:
                continue

            headlines = fetch_latest_news(symbol)

            data.append({
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
            })
        except:
            continue

        progress = float(i + 1) / float(total)
        progress_bar.progress(progress)
        progress_text.text(f"Scanning {symbol} ({i+1}/{total})...")
        time.sleep(0.1)

    progress_text.text("Scan complete.")
    return pd.DataFrame(data)

# ------------------------------
# Streamlit UI
# ------------------------------
st.title("📈 Market Demand & Supply Stock Scanner")

with st.sidebar:
    st.header("🔧 Scanner Settings")
    source = st.radio("Select Symbol Source", ["NASDAQ", "S&P500", "Upload File"])

    symbols = []
    if source == "Upload File":
        uploaded_file = st.file_uploader("Upload CSV with 'Symbol' column")
        if uploaded_file:
            symbols = load_symbols_from_file(uploaded_file)
    else:
        symbols = load_index_symbols(source)

    price_min, price_max = st.slider("Price Range ($)", 0.01, 100.0, (2.0, 20.0))
    max_float = st.slider("Max Float (shares)", 200_000, 10_000_000, 10_000_000)
    top_n = st.slider("Top N Stocks to Show", 1, 20, 10)

    price_change_filter = st.number_input("Min % Price Increase Today", min_value=0.0, max_value=100.0, value=10.0)
    rel_vol_filter = st.number_input("Min Relative Volume (x Avg)", min_value=0.0, max_value=20.0, value=5.0)

scanner_ready = symbols is not None and len(symbols) > 0

if st.button("🔍 Run Scanner", disabled=not scanner_ready):
    with st.spinner("Scanning market..."):
        df = fetch_stock_data(symbols)
        if not df.empty:
            df = df.sort_values(by='% Change', ascending=False).head(top_n)
            st.success(f"Found {len(df)} stocks matching criteria")
            st.dataframe(df)
        else:
            st.warning("No stocks matched the criteria.")
