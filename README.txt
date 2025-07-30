# Market Demand & Supply Stock Scanner

This is a Streamlit web app that scans the market for stocks that meet specific demand and supply criteria.

## Features

- Filter by % price increase and relative volume
- Limit to a price range and float size
- Load symbols from NASDAQ, S&P500, or a custom CSV
- Scrape recent news headlines from Finviz
- Display top N results in a sortable table

## Requirements

- Python 3.8+
- See requirements.txt for dependencies

## To Run Locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Deployment

To deploy to Streamlit Cloud:
1. Push all files to a GitHub repo
2. Connect the repo to Streamlit Cloud
3. Set `streamlit_app.py` as the entry point
