import os
import re
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
DEFAULT_PRICE_RANGE = (2.0, 20.0)         # floats required by st.slider for range
DEFAULT_REL_VOLUME = (3.0, 10.0)
DEFAULT_PERCENT_GAIN = (4.0, 15.0)
DEFAULT_FLOAT_LIMIT = 10_000_000

MAX_WORKERS = 10           # keep moderate to reduce rate-limit issues
RATE_LIMIT_DELAY = 0.1     # seconds between processed symbols (main thread)

EOD_API_KEY = os.getenv("EOD_API_KEY", "")
EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]
FILE_MAP = {
    "NASDAQ": "data/NASDAQ.csv",
    "NYSE": "data/NYSE.csv",
    "AMEX": "data/AMEX.csv",
    "S&P500": "data/SP500.csv",   # if you have a local file for S&P500, place it here
}

# Store skipped symbols and reasons
SKIPPED_SYMBOLS_LOG = []

# ----------------------------
# Symbol loading helpers
# ----------------------------
def _extract_symbols_from_df(df: pd.DataFrame) -> list:
    """Find a symbol-like column and return a clean list."""
    # try typical column names first
    for col in df.columns:
        if col.strip().lower() in ("symbol", "ticker", "sym"):
            return df[col].dropna().astype(str).tolist()
    # else, use the first column
    return df.iloc[:, 0].dropna().astype(str).tolist()

@st.cache_data(show_spinner=False)
def load_local_symbols(source: str) -> list:
    """Load list of symbols from a local CSV file only (no web)."""
    path = FILE_MAP.get(source)
    if not path:
        return []
    try:
        df = pd.read_csv(path)
        return _extract_symbols_from_df(df)
    except Exception as e:
        st.error(f"Error loading symbols for {source} from {path}: {e}")
        return []

@st.cache_data(show_spinner=False)
def get_index_symbols(source: str) -> list:
    """(Unused by default now) Load symbols via EOD/Wikipedia if you decide to re-enable web."""
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

def load_symbols_from_file(uploaded_file) -> list:
    """Read CSV and return values in a 'Symbol' (case-insensitive) column."""
    try:
        df = pd.read_csv(uploaded_file)
        return _extract_symbols_from_df(df)
    except Exception as e:
        st.error(f"Error reading uploaded file: {e}")
        return []

def clean_symbols(symbols: list) -> list:
    """
    Clean before scanning:
      - strip whitespace
      - uppercase
      - drop invalid characters (allow A-Z, 0-9, dot, hyphen)
      - de-duplicate (preserve order)
      - drop empty
    """
    out = []
    seen = set()
    pattern = re.compile(r"^[A-Z0-9.\-]+$")
    for s in symbols:
        t = str(s).strip().upper()
        if not t:
            continue
        if not pattern.match(t):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

# ----------------------------
# Data fetchers and helpers
# ----------------------------
def fetch_float_from_eod(symbol: str):
    """Try to fetch float from EOD fundamentals. Returns int or None."""
    if not EOD_API_KEY:
        return None
    try:
        url = f"https://eodhistoricaldata.com/api/fundamentals/{symbol}.US?api_token={EOD_API_KEY}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        shares_float = data.get("SharesStats", {}).get("SharesFloat")
        if shares_float is None:
            return None
        return int(shares_float)
    except Exception:
        return None

def has_news_event(symbol: str) -> bool:
    """Best-effort scrape of Yahoo Finance headlines as a proxy for 'has news'."""
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
    """
    Primary data fetch using yfinance. Returns dict with required fields or None.
    If float missing, attempts EOD fallback for float only.
    """
    try:
        ticker = yf.Ticker(symbol)

        # Try to get 2-day history (to compute percent change from previous close)
        hist = None
        try:
            hist = ticker.history(period="2d", interval="1d")
        except Exception:
            hist = None

        # Use ticker.info for fields
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

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

        # fallback to intraday for volume/avg
        if (volume is None or avg_volume is None) and hist is not None:
            try:
                intraday = ticker.history(period="1d", interval="1m")
                if not intraday.empty:
                    if volume is None:
                        volume = int(intraday["Volume"].iloc[-1])
                    if avg_volume is None:
                        avg_volume = int(intraday["Volume"].mean())
            except Exception:
                pass

        # if key numbers missing -> skip
        if price is None or prev_close is None or volume is None or avg_volume is None:
            SKIPPED_SYMBOLS_LOG.append((symbol, "Missing price/volume data from yfinance"))
            return None

        float_shares = info.get("floatShares")
        if float_shares is None:
            float_shares = fetch_float_from_eod(symbol)

        try:
            rel_volume = float(volume) / float(avg_volume) if avg_volume else 0.0
        except Exception:
            rel_volume = 0.0
        try:
            percent_gain = ((float(price) - float(prev_close)) / float(prev_close)) * 100 if prev_close else 0.0
        except Exception:
            percent_gain = 0.0

        return {
            "Symbol": symbol,
            "Price": float(price),
            "Volume": int(volume),
            "Avg Volume": int(avg_volume) if avg_volume is not None else None,
            "Rel Volume": float(rel_volume),
            "Float": int(float_shares) if float_shares is not None else None,
            "% Gain": float(percent_gain),
        }

    except Exception as e:
        SKIPPED_SYMBOLS_LOG.append((symbol, f"Exception fetching: {e}"))
        return None

# ----------------------------
# Scanner
# ----------------------------
def scan_symbols(symbols,
                 price_range,
                 rel_volume_range,
                 percent_gain_range,
                 news_required,
                 float_limit,
                 enable_price,
                 enable_rel_vol,
                 enable_pct_gain,
                 enable_news,
                 enable_float,
                 top_n):
    """
    Concurrently fetch symbols, apply enabled filters, and return results list.
    UI updates (progress) happen in the main thread while collecting completed futures.
    """
    results = []
    total = len(symbols)
    if total == 0:
        return results

    status_placeholder = st.empty()
    progress_bar = st.progress(0.0)

    # submit all futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(fetch_info, s): s for s in symbols}

        completed = 0
        for fut in concurrent.futures.as_completed(future_to_symbol):
            symbol = future_to_symbol[fut]
            try:
                info = fut.result()
            except Exception as e:
                SKIPPED_SYMBOLS_LOG.append((symbol, f"Worker exception: {e}"))
                info = None

            # update progress BEFORE sleeping so UI is responsive
            completed += 1
            progress_bar.progress(completed / total)
            status_placeholder.text(f"Scanned {completed}/{total} — latest: {symbol}")

            # rate limiting delay (main thread)
            time.sleep(RATE_LIMIT_DELAY)

            if info is None:
                continue

            # If float is missing and float filter enabled -> skip and log
            if enable_float and (info.get("Float") is None):
                SKIPPED_SYMBOLS_LOG.append((symbol, "Missing float data"))
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

    # post processing: sort by % Gain descending and take top_n
    progress_bar.empty()
    status_placeholder.empty()

    if results:
        df = pd.DataFrame(results).sort_values(by="% Gain", ascending=False)
        if top_n and top_n > 0:
            df = df.head(top_n)
        return df.to_dict(orient="records")
    else:
        return []

# ----------------------------
# Streamlit App UI
# ----------------------------
def app():
    global MAX_WORKERS, RATE_LIMIT_DELAY  # must be before you use them
    
    st.set_page_config(page_title="Top Daily Stocks Scanner", layout="wide")
    st.title("📈 Top Daily Stocks Scanner")

    # Sidebar inputs
    with st.sidebar:
        st.header("Universe")
        symbol_source = st.radio("Symbols source", ["S&P500"] + EXCHANGES + ["Upload File"])

        # load symbols based on selection (from local files or upload ONLY)
        raw_symbols = []
        if symbol_source == "Upload File":
            uploaded_file = st.file_uploader(
                "Upload CSV (must contain Symbol/Ticker column)",
                type=["csv"]
            )
            if uploaded_file:
                raw_symbols = load_symbols_from_file(uploaded_file)
        else:
            # Use local files only as requested
            raw_symbols = load_local_symbols(symbol_source)

        st.markdown("---")
        st.header("Demand (turn ON/OFF each)")

        enable_price = st.checkbox("Enable price range filter", value=True)
        price_range = st.slider("Price range ($)", 0.01, 100.0, value=DEFAULT_PRICE_RANGE, step=0.01)

        enable_rel_vol = st.checkbox("Enable relative volume filter", value=True)
        rel_volume_range = st.slider("Relative Volume (x)", 1.0, 50.0, value=DEFAULT_REL_VOLUME, step=0.1)

        enable_pct_gain = st.checkbox("Enable % gain filter", value=True)
        percent_gain_range = st.slider("% Gain Today", -50.0, 200.0, value=DEFAULT_PERCENT_GAIN, step=0.1)

        enable_news = st.checkbox("Enable news filter (requires internet scrape)", value=False)
        news_required = st.checkbox("Require news event (when enabled)", value=False) if enable_news else False

        st.markdown("---")
        st.header("Supply")
        enable_float = st.checkbox("Enable float (supply) filter", value=True)
        float_limit = st.number_input(
            "Max float (shares)",
            min_value=1_000,
            max_value=500_000_000,
            value=int(DEFAULT_FLOAT_LIMIT),
            step=100_000
        )

        st.markdown("---")
        st.header("Performance / Limits")
        MAX_WORKERS = st.slider("Concurrency (max workers)", 1, 20, MAX_WORKERS)
        RATE_LIMIT_DELAY = st.slider("Delay between symbols (s)", 0.0, 5.0, RATE_LIMIT_DELAY, 0.1)

    # --- Clean symbols BEFORE scanning ---
    cleaned_symbols = clean_symbols(raw_symbols)
    st.write(f"Loaded {len(raw_symbols)} raw symbols from `{symbol_source}` → **{len(cleaned_symbols)}** after cleaning (trim/uppercase/dedup/filter invalid).")

    # Allow download of the CLEANED list
    if cleaned_symbols:
        symbols_df = pd.DataFrame({"Symbol": cleaned_symbols})
        buf = BytesIO()
        symbols_df.to_csv(buf, index=False)
        st.download_button(
            label="📥 Download Cleaned Symbols CSV",
            data=buf.getvalue(),
            file_name=f"{symbol_source}_symbols_cleaned.csv",
            mime="text/csv"
        )

    st.markdown("---")

    run_scan = st.button("🚀 Run Scanner", disabled=(len(cleaned_symbols) == 0))
    if run_scan:
        # clear previous skipped log for this run
        SKIPPED_SYMBOLS_LOG.clear()

        with st.spinner("Scanning symbols — this may take a while for large universes..."):
            results = scan_symbols(
                cleaned_symbols,
                price_range,
                rel_volume_range,
                percent_gain_range,
                news_required,
                float_limit,
                enable_price,
                enable_rel_vol,
                enable_pct_gain,
                enable_news,
                enable_float,
                top_n=st.slider("Top N results to show", 1, 50, 10, key="topn_main")
            )

        # present results
        if results:
            df = pd.DataFrame(results)
            display_df = df.copy()
            if "Volume" in display_df.columns:
                display_df["Volume"] = display_df["Volume"].apply(lambda x: f"{int(x):,}")
            if "Float" in display_df.columns:
                display_df["Float"] = display_df["Float"].apply(lambda x: f"{int(x):,}" if pd.notna(x) else "")
            st.success(f"✅ Found {len(display_df)} matching stocks")
            st.dataframe(display_df, use_container_width=True)

            csv_bytes = df.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Download Results CSV", csv_bytes, file_name="scan_results.csv", mime="text/csv")
        else:
            st.warning("No symbols matched the enabled criteria.")

        # skipped log
        if SKIPPED_SYMBOLS_LOG:
            st.markdown("---")
            st.subheader("⚠️ Skipped / Problematic Symbols")
            skipped_df = pd.DataFrame(SKIPPED_SYMBOLS_LOG, columns=["Symbol", "Reason"])
            st.dataframe(skipped_df, use_container_width=True)
            log_csv = skipped_df.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Download Skipped Log", log_csv, file_name="skipped_log.csv", mime="text/csv")

if __name__ == "__main__":
    app()
