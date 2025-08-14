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

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/json",
}

# Store skipped symbols and reasons
SKIPPED_SYMBOLS_LOG = []

# ----------------------------
# Utilities
# ----------------------------
def normalize_for_yf(sym: str) -> str:
    """Convert tickers like BRK.B -> BRK-B for yfinance."""
    return sym.replace(".", "-").upper().strip()

def _safe_json_get(url: str, timeout: int = 20):
    r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ----------------------------
# Symbol loading helpers
# ----------------------------
@st.cache_data(show_spinner=False)
def get_index_symbols(source: str):
    """
    Load symbols for:
      - S&P500 from Wikipedia
      - NASDAQ, NYSE, AMEX from EOD Historical Data.
    Adds robust fallbacks and normalization for yfinance.
    Returns a simple Python list of ticker strings.
    """
    try:
        if source in ("NASDAQ", "NYSE", "AMEX"):
            if not EOD_API_KEY:
                st.error("EOD_API_KEY not set in environment. Can't load exchange list.")
                return []

            # Primary: direct exchange list (works for many exchanges)
            url = f"https://eodhistoricaldata.com/api/exchange-symbol-list/{source}?api_token={EOD_API_KEY}&fmt=json"
            try:
                data = _safe_json_get(url)
                if isinstance(data, list) and len(data) > 0:
                    symbols = [item["Code"] for item in data if item.get("Code")]
                    return sorted(set(normalize_for_yf(s) for s in symbols))
            except Exception:
                # fall back below
                pass

            # Fallback: get US list and filter by Exchange == source
            us_url = f"https://eodhistoricaldata.com/api/exchange-symbol-list/US?api_token={EOD_API_KEY}&fmt=json"
            try:
                data = _safe_json_get(us_url)
                if isinstance(data, list) and len(data) > 0:
                    symbols = [
                        item["Code"]
                        for item in data
                        if item.get("Code") and item.get("Exchange") == source
                    ]
                    return sorted(set(normalize_for_yf(s) for s in symbols))
                else:
                    st.error(f"EOD returned no data for US. Check API key/plan.")
                    return []
            except Exception as e:
                st.error(f"Error loading {source} from EOD: {e}")
                return []

        elif source == "S&P500":
            wiki_url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            try:
                r = requests.get(wiki_url, headers=HTTP_HEADERS, timeout=20)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                table = soup.find("table", {"id": "constituents"})
                if table is None:
                    st.error("Could not find S&P 500 table on Wikipedia.")
                    return []
                df = pd.read_html(str(table))[0]
                col = "Symbol" if "Symbol" in df.columns else df.columns[0]
                syms = df[col].astype(str).str.strip().tolist()
                # Normalize for yfinance (BRK.B -> BRK-B, BF.B -> BF-B, etc.)
                return sorted(set(normalize_for_yf(s) for s in syms))
            except Exception as e:
                st.error(f"Error loading S&P500 from Wikipedia: {e}")
                return []

        else:
            return []
    except Exception as e:
        st.error(f"Error loading symbols for {source}: {e}")
        return []

def load_symbols_from_file(uploaded_file):
    """Read CSV and return values in a 'Symbol'/'Ticker'/'Sym' column (normalized for yfinance)."""
    try:
        df = pd.read_csv(uploaded_file)
        symbol_col = next((c for c in df.columns if c.strip().lower() in ("symbol", "ticker", "sym")), None)
        if not symbol_col:
            st.error("Uploaded CSV must contain a 'Symbol' or 'Ticker' column.")
            return []
        return sorted(set(normalize_for_yf(s) for s in df[symbol_col].dropna().astype(str)))
    except Exception as e:
        st.error(f"Error reading uploaded file: {e}")
        return []

# ----------------------------
# Data fetchers and helpers
# ----------------------------
def fetch_basic_info(symbol: str):
    """Lightweight info for the pre-scan CSV."""
    try:
        t = yf.Ticker(symbol)
        info = t.info
        return {
            "Symbol": symbol,
            "Company Name": info.get("shortName", ""),
            "Latest Price": info.get("regularMarketPrice", None),
            "Industry": info.get("industry", ""),
            "Market Cap": info.get("marketCap", None),
            "Volume": info.get("volume", None),
        }
    except Exception:
        return None

def fetch_float_from_eod(symbol: str):
    """Try to fetch float from EOD fundamentals. Returns int or None."""
    if not EOD_API_KEY:
        return None
    try:
        url = f"https://eodhistoricaldata.com/api/fundamentals/{symbol}.US?api_token={EOD_API_KEY}"
        r = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        shares_float = data.get("SharesStats", {}).get("SharesFloat")
        return int(shares_float) if shares_float is not None else None
    except Exception:
        return None

def has_news_event(symbol: str) -> bool:
    """Simple news check via scraping Yahoo Finance headlines (best-effort)."""
    try:
        url = f"https://finance.yahoo.com/quote/{symbol}"
        r = requests.get(url, headers=HTTP_HEADERS, timeout=10)
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
            pass

        # Use ticker.info for many fields
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            pass

        # price determination: prefer real market price or last close
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

        # fallback to intraday history for volume
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

        # if we still miss key numbers -> record skipped
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
            percent_gain = (
                ((float(price) - float(prev_close)) / float(prev_close)) * 100
                if prev_close else 0.0
            )
        except Exception:
            percent_gain = 0.0

        return {
            "Symbol": symbol,
            "Price": float(price),
            "Volume": int(volume),
            "Avg Volume": int(avg_volume),
            "Rel Volume": float(rel_volume),
            "Float": int(float_shares) if float_shares is not None else None,
            "% Gain": float(percent_gain),
        }
    except Exception as e:
        SKIPPED_SYMBOLS_LOG.append((symbol, f"Exception fetching: {e}"))
        return None

# ----------------------------
# Cached SYMBOL LIST loader (NOT the details)
# ----------------------------
@st.cache_data(show_spinner=False)
def load_symbol_list(symbol_source: str):
    """Returns pure list of tickers for the chosen source."""
    if symbol_source in ("S&P500",) + tuple(EXCHANGES):
        return get_index_symbols(symbol_source)
    return []

# ----------------------------
# Pre-scan details builder (fast, concurrent)
# ----------------------------
@st.cache_data(show_spinner=True)
def build_basic_info_df(symbols: list):
    """Concurrent pre-scan to prepare the downloadable symbols CSV."""
    if not symbols:
        return pd.DataFrame(columns=["Symbol", "Company Name", "Latest Price", "Industry", "Market Cap", "Volume"])

    results = []
    max_workers = min(20, max(1, len(symbols)//50 or 4))  # reasonable concurrency
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_basic_info, s): s for s in symbols}
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            if rec:
                results.append(rec)
    if not results:
        return pd.DataFrame(columns=["Symbol", "Company Name", "Latest Price", "Industry", "Market Cap", "Volume"])
    df = pd.DataFrame(results).drop_duplicates(subset=["Symbol"])
    # Sort for nicer UX
    return df.sort_values(by="Symbol").reset_index(drop=True)

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

            completed += 1
            progress_bar.progress(completed / total)
            status_placeholder.text(f"Scanned {completed}/{total} — latest: {symbol}")

            time.sleep(RATE_LIMIT_DELAY)

            if info is None:
                continue

            # Float is required if float filter enabled
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
        symbol_list = []

        if symbol_source == "Upload File":
            uploaded_file = st.file_uploader("Upload CSV (must contain Symbol or Ticker column)", type=["csv"])
            if uploaded_file:
                symbol_list = load_symbols_from_file(uploaded_file)
        else:
            symbol_list = load_symbol_list(symbol_source)

        st.markdown("---")
        st.header("Demand (turn ON/OFF each)")
        enable_price = st.checkbox("Enable price range filter", value=True)
        price_range = st.slider("Price range ($)", 0.01, 100.0, DEFAULT_PRICE_RANGE, 0.01)

        enable_rel_vol = st.checkbox("Enable relative volume filter", value=True)
        rel_volume_range = st.slider("Relative Volume (x)", 1.0, 50.0, DEFAULT_REL_VOLUME, 0.1)

        enable_pct_gain = st.checkbox("Enable % gain filter", value=True)
        percent_gain_range = st.slider("% Gain Today", -50.0, 200.0, DEFAULT_PERCENT_GAIN, 0.1)

        enable_news = st.checkbox("Enable news filter (requires internet scrape)", value=False)
        news_required = st.checkbox("Require news event (when enabled)", value=False) if enable_news else False

        st.markdown("---")
        st.header("Supply")
        enable_float = st.checkbox("Enable float (supply) filter", value=True)
        float_limit = st.number_input("Max float (shares)", min_value=1_000, max_value=500_000_000,
                                      value=int(DEFAULT_FLOAT_LIMIT), step=100_000)

        st.markdown("---")
        st.header("Other")
        top_n = st.slider("Top N results", 1, 50, 10)
        MAX_WORKERS = st.slider("Concurrency (max workers)", 1, 50, MAX_WORKERS)
        RATE_LIMIT_DELAY = st.number_input("Delay between symbols (s)", 0.0, 2.0, RATE_LIMIT_DELAY)

    # ---- Show true symbol count (list) ----
    st.write(f"Loaded **{len(symbol_list)}** symbols from `{symbol_source}`")

    # ---- Build & download pre-scan CSV (basic info) before scanning ----
    details_df = pd.DataFrame()
    if symbol_list:
        with st.spinner("Building symbols list (basic info) for download..."):
            details_df = build_basic_info_df(symbol_list)

        if details_df.empty:
            st.warning(
                "Symbol list loaded, but could not prefetch basic info (yfinance may be rate-limiting). "
                "You can still run the scanner."
            )

        csv_buffer = BytesIO()
        # Prefer the detailed CSV when available; otherwise fall back to just symbols.
        if not details_df.empty:
            details_df.to_csv(csv_buffer, index=False)
            st.download_button(
                label="📥 Download Symbols List (with basic info)",
                data=csv_buffer.getvalue(),
                file_name=f"{symbol_source}_symbols.csv",
                mime="text/csv",
            )
            st.caption(f"Prefetched basic info for {len(details_df)} symbols.")
            # Optional: quick preview
            st.dataframe(details_df.head(25), use_container_width=True)
        else:
            pd.DataFrame({"Symbol": symbol_list}).to_csv(csv_buffer, index=False)
            st.download_button(
                label="📥 Download Symbols (tickers only)",
                data=csv_buffer.getvalue(),
                file_name=f"{symbol_source}_symbols.csv",
                mime="text/csv",
            )

    # ---- Run scanner (always uses the pure list, not the details DF) ----
    run_scan = st.button("🚀 Run Scanner", disabled=(len(symbol_list) == 0))
    if run_scan:
        SKIPPED_SYMBOLS_LOG.clear()
        with st.spinner("Scanning symbols — this may take a while for large universes..."):
            results = scan_symbols(
                symbol_list,
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
                top_n,
            )

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

        if SKIPPED_SYMBOLS_LOG:
            st.markdown("---")
            st.subheader("⚠️ Skipped / Problematic Symbols")
            skipped_df = pd.DataFrame(SKIPPED_SYMBOLS_LOG, columns=["Symbol", "Reason"])
            st.dataframe(skipped_df, use_container_width=True)
            log_csv = skipped_df.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Download Skipped Log", log_csv, file_name="skipped_log.csv", mime="text/csv")

if __name__ == "__main__":
    app()
