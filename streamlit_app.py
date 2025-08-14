import os
import streamlit as st
import pandas as pd
import yfinance as yf
import concurrent.futures
import time
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from io import BytesIO

# load .env if present
load_dotenv()

# ----------------------------
# Configuration / Defaults
# ----------------------------
DEFAULT_PRICE_RANGE = (2.0, 20.0)
DEFAULT_REL_VOLUME = (3.0, 10.0)
DEFAULT_PERCENT_GAIN = (4.0, 15.0)
DEFAULT_FLOAT_LIMIT = 10_000_000

MAX_WORKERS = 10
RATE_LIMIT_DELAY = 0.1
EOD_API_KEY = os.getenv("EOD_API_KEY", "")

EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]

# Store skipped symbols and reasons
SKIPPED_SYMBOLS_LOG = []

# ----------------------------
# Symbol loading helpers
# ----------------------------
@st.cache_data(show_spinner=False)
def get_index_symbols(source: str):
    """Load symbols for NASDAQ/NYSE/AMEX from EOD API, S&P500 from Wikipedia."""
    try:
        if source in ("NASDAQ", "NYSE", "AMEX"):
            if not EOD_API_KEY:
                st.error("EOD_API_KEY not set in environment. Can't load exchange list.")
                return []
            url = f"https://eodhistoricaldata.com/api/exchange-symbol-list/{source}?api_token={EOD_API_KEY}&fmt=json"
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            data = r.json()
            return [item["Code"] for item in data if item.get("Code")]
        elif source == "S&P500":
            wiki_url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            r = requests.get(wiki_url, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.find("table", {"id": "constituents"})
            if table is None:
                return []
            df = pd.read_html(str(table))[0]
            if "Symbol" in df.columns:
                return df["Symbol"].astype(str).str.strip().tolist()
            return df.iloc[:, 0].astype(str).str.strip().tolist()
    except Exception as e:
        st.error(f"Error loading symbols for {source}: {e}")
        return []

def load_symbols_from_file(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file)
        symbol_col = next((c for c in df.columns if c.strip().lower() in ("symbol", "ticker", "sym")), None)
        if not symbol_col:
            st.error("Uploaded CSV must contain a 'Symbol' or 'Ticker' column.")
            return []
        return df[symbol_col].dropna().astype(str).str.strip().unique().tolist()
    except Exception as e:
        st.error(f"Error reading uploaded file: {e}")
        return []

# ----------------------------
# Data fetchers
# ----------------------------
def fetch_basic_info(symbol):
    try:
        t = yf.Ticker(symbol)
        info = t.info
        return {
            "Symbol": symbol,
            "Company Name": info.get("shortName", ""),
            "Latest Price": info.get("regularMarketPrice", None),
            "Industry": info.get("industry", ""),
            "Market Cap": info.get("marketCap", None),
            "Volume": info.get("volume", None)
        }
    except Exception:
        return None

def fetch_float_from_eod(symbol: str):
    if not EOD_API_KEY:
        return None
    try:
        url = f"https://eodhistoricaldata.com/api/fundamentals/{symbol}.US?api_token={EOD_API_KEY}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        shares_float = data.get("SharesStats", {}).get("SharesFloat")
        return int(shares_float) if shares_float is not None else None
    except Exception:
        return None

def has_news_event(symbol: str) -> bool:
    try:
        url = f"https://finance.yahoo.com/quote/{symbol}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        headlines = soup.find_all("h3")
        return len(headlines) > 0
    except Exception:
        return False

def fetch_info(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        hist = None
        try:
            hist = ticker.history(period="2d", interval="1d")
        except Exception:
            pass

        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            pass

        price = info.get("regularMarketPrice")
        if price is None and hist is not None and not hist.empty:
            price = hist["Close"].iloc[-1]

        prev_close = info.get("regularMarketPreviousClose")
        if prev_close is None and hist is not None and len(hist) >= 2:
            prev_close = hist["Close"].iloc[-2]
        elif prev_close is None and hist is not None and len(hist) == 1:
            prev_close = hist["Close"].iloc[0]

        volume = info.get("volume")
        avg_volume = info.get("averageVolume")
        if (volume is None or avg_volume is None) and hist is not None:
            try:
                intraday = ticker.history(period="1d", interval="1m")
                if not intraday.empty:
                    volume = volume or int(intraday["Volume"].iloc[-1])
                    avg_volume = avg_volume or int(intraday["Volume"].mean())
            except Exception:
                pass

        if price is None or prev_close is None or volume is None or avg_volume is None:
            SKIPPED_SYMBOLS_LOG.append((symbol, "Missing price/volume data"))
            return None

        float_shares = info.get("floatShares") or fetch_float_from_eod(symbol)

        rel_volume = (float(volume) / float(avg_volume)) if avg_volume else 0.0
        percent_gain = ((float(price) - float(prev_close)) / float(prev_close) * 100) if prev_close else 0.0

        return {
            "Symbol": symbol,
            "Price": float(price),
            "Volume": int(volume),
            "Avg Volume": int(avg_volume),
            "Rel Volume": rel_volume,
            "Float": int(float_shares) if float_shares is not None else None,
            "% Gain": percent_gain,
        }
    except Exception as e:
        SKIPPED_SYMBOLS_LOG.append((symbol, f"Exception: {e}"))
        return None

# ----------------------------
# Cached symbol detail loader
# ----------------------------
@st.cache_data(show_spinner=False)
def load_symbol_details(symbol_source, uploaded_file=None):
    if symbol_source == "Upload File" and uploaded_file:
        symbols = load_symbols_from_file(uploaded_file)
    else:
        symbols = get_index_symbols(symbol_source)

    details = []
    for sym in symbols:
        info = fetch_basic_info(sym)
        if info:
            details.append(info)
    return pd.DataFrame(details)

# ----------------------------
# Scanner
# ----------------------------
def scan_symbols(symbols, price_range, rel_volume_range, percent_gain_range,
                 news_required, float_limit, enable_price, enable_rel_vol,
                 enable_pct_gain, enable_news, enable_float, top_n):
    results = []
    total = len(symbols)
    if total == 0:
        return results

    status_placeholder = st.empty()
    progress_bar = st.progress(0.0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(fetch_info, s): s for s in symbols}

        completed = 0
        for fut in concurrent.futures.as_completed(future_to_symbol):
            symbol = future_to_symbol[fut]
            try:
                info = fut.result()
            except Exception as e:
                SKIPPED_SYMBOLS_LOG.append((symbol, f"Error: {e}"))
                info = None

            completed += 1
            progress_bar.progress(completed / total)
            status_placeholder.text(f"Scanned {completed}/{total} — latest: {symbol}")
            time.sleep(RATE_LIMIT_DELAY)

            if not info:
                continue

            if enable_float and (info.get("Float") is None):
                SKIPPED_SYMBOLS_LOG.append((symbol, "Missing float"))
                continue

            price = info.get("Price")
            rel_vol = info.get("Rel Volume", 0.0)
            pct = info.get("% Gain", 0.0)
            flt = info.get("Float", float("inf"))

            pass_filters = True
            if enable_price and not (price_range[0] <= price <= price_range[1]):
                pass_filters = False
            if pass_filters and enable_rel_vol and not (rel_volume_range[0] <= rel_vol <= rel_volume_range[1]):
                pass_filters = False
            if pass_filters and enable_pct_gain and not (percent_gain_range[0] <= pct <= percent_gain_range[1]):
                pass_filters = False
            if pass_filters and enable_float and (flt is None or flt > float_limit):
                pass_filters = False
            if pass_filters and enable_news and news_required and not has_news_event(symbol):
                pass_filters = False

            if pass_filters:
                results.append(info)

    progress_bar.empty()
    status_placeholder.empty()

    if results:
        df = pd.DataFrame(results).sort_values(by="% Gain", ascending=False)
        if top_n:
            df = df.head(top_n)
        return df.to_dict(orient="records")
    return []

# ----------------------------
# App UI
# ----------------------------
def app():
    global MAX_WORKERS, RATE_LIMIT_DELAY

    st.set_page_config(page_title="Top Daily Stocks Scanner", layout="wide")
    st.title("📈 Top Daily Stocks Scanner")

    with st.sidebar:
        st.header("Universe")
        symbol_source = st.radio("Symbols source", ["S&P500"] + EXCHANGES + ["Upload File"])
        uploaded_file = None
        if symbol_source == "Upload File":
            uploaded_file = st.file_uploader("Upload CSV", type=["csv"])

        st.markdown("---")
        st.header("Demand Filters")
        enable_price = st.checkbox("Enable price filter", value=True)
        price_range = st.slider("Price range ($)", 0.01, 100.0, DEFAULT_PRICE_RANGE, 0.01)

        enable_rel_vol = st.checkbox("Enable relative volume filter", value=True)
        rel_volume_range = st.slider("Relative Volume (x)", 1.0, 50.0, DEFAULT_REL_VOLUME, 0.1)

        enable_pct_gain = st.checkbox("Enable % gain filter", value=True)
        percent_gain_range = st.slider("% Gain Today", -50.0, 200.0, DEFAULT_PERCENT_GAIN, 0.1)

        enable_news = st.checkbox("Enable news filter", value=False)
        news_required = st.checkbox("Require news event", value=False) if enable_news else False

        st.markdown("---")
        st.header("Supply Filter")
        enable_float = st.checkbox("Enable float filter", value=True)
        float_limit = st.number_input("Max float", 1_000, 500_000_000, DEFAULT_FLOAT_LIMIT, 100_000)

        st.markdown("---")
        st.header("Other")
        top_n = st.slider("Top N results", 1, 50, 10)
        MAX_WORKERS = st.slider("Max Workers", 1, 50, MAX_WORKERS)
        RATE_LIMIT_DELAY = st.number_input("Rate Limit Delay (s)", 0.0, 2.0, RATE_LIMIT_DELAY)

    # Load and show symbol details
    symbols_df = load_symbol_details(symbol_source, uploaded_file)
    st.write(f"Loaded {len(symbols_df)} symbols from `{symbol_source}`")

    if not symbols_df.empty:
        csv_buffer = BytesIO()
        symbols_df.to_csv(csv_buffer, index=False)
        st.download_button(
            label="📥 Download Symbols List CSV",
            data=csv_buffer.getvalue(),
            file_name=f"{symbol_source}_symbols.csv",
            mime="text/csv"
        )

    # Run scanner
    run_scan = st.button("🚀 Run Scanner", disabled=symbols_df.empty)
    if run_scan:
        SKIPPED_SYMBOLS_LOG.clear()
        symbols = symbols_df["Symbol"].tolist()
        with st.spinner("Scanning..."):
            results = scan_symbols(symbols, price_range, rel_volume_range, percent_gain_range,
                                   news_required, float_limit, enable_price, enable_rel_vol,
                                   enable_pct_gain, enable_news, enable_float, top_n)

        if results:
            df = pd.DataFrame(results)
            display_df = df.copy()
            if "Volume" in display_df:
                display_df["Volume"] = display_df["Volume"].apply(lambda x: f"{int(x):,}")
            if "Float" in display_df:
                display_df["Float"] = display_df["Float"].apply(lambda x: f"{int(x):,}" if pd.notna(x) else "")
            st.success(f"✅ Found {len(display_df)} matches")
            st.dataframe(display_df, use_container_width=True)
            st.download_button("📥 Download Results CSV", df.to_csv(index=False).encode(), "scan_results.csv", "text/csv")
        else:
            st.warning("No matches found.")

        if SKIPPED_SYMBOLS_LOG:
            skipped_df = pd.DataFrame(SKIPPED_SYMBOLS_LOG, columns=["Symbol", "Reason"])
            st.subheader("⚠️ Skipped Symbols")
            st.dataframe(skipped_df)
            st.download_button("📥 Download Skipped Log", skipped_df.to_csv(index=False).encode(), "skipped_log.csv", "text/csv")

if __name__ == "__main__":
    app()
