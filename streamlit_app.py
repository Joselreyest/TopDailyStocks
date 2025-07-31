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

# ------------------------------
# Configuration
# ------------------------------
EOD_API_KEY = os.getenv("EOD_API_KEY", "")

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

def load_symbols_from_file(file):
    try:
        df = pd.read_csv(file)
        symbol_col = next((col for col in df.columns if col.strip().lower() == "symbol"), None)
        if not symbol_col:
            st.error("No 'Symbol' column found in the uploaded file.")
            return []
        return df[symbol_col].dropna().unique().tolist()
    except Exception as e:
        st.error(f"Error reading file: {e}")
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

    def fallback_to_yfinance():
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

    try:
        url = f"https://eodhistoricaldata.com/api/real-time/{symbol}.US?api_token={EOD_API_KEY}&fmt=json"
        r = requests.get(url)
        if r.status_code != 200:
            return fallback_to_yfinance()

        data = r.json()
        price_today = data['close']
        price_yesterday = data['previousClose']
        percent_change = ((price_today - price_yesterday) / price_yesterday) * 100
        volume_today = data['volume']
        rel_volume = volume_today / data.get('avgVolume', 1)
        float_shares = data.get('sharesFloat', 0)

        if percent_change < price_change_filter:
            reason_skipped.append(f"% Change {percent_change:.2f} < {price_change_filter}")
        if rel_volume < rel_vol_filter:
            reason_skipped.append(f"Rel Vol {rel_volume:.2f} < {rel_vol_filter}")
        if not (price_min <= price_today <= price_max):
            reason_skipped.append(f"Price {price_today:.2f} not in range {price_min}-{price_max}")
        if float_shares > max_float:
            reason_skipped.append(f"Float {float_shares} > {max_float}")

        if reason_skipped:
            return None, reason_skipped

        headlines = fetch_latest_news(symbol)

        return {
            'Symbol': symbol,
            'Name': data.get('name'),
            'Price': round(price_today, 2),
            '% Change': round(percent_change, 2),
            'Volume': volume_today,
            'Float': float_shares,
            'Market Cap': data.get('marketCapitalization'),
            'P/E': data.get('pe'),
            'Sector': data.get('sector'),
            'Analyst Rating': data.get('recommendation'),
            'Volatility': data.get('beta'),
            'Pre-Market Price': data.get('previousClose'),
            'Recent News': headlines
        }, None

    except Exception:
        return fallback_to_yfinance()

# Rest of your code remains the same
# This hybrid implementation tries EOD API first, then falls back to yfinance
