# News Headline Scraper

Scrapes headlines from **5 different RSS feeds** (BBC, Reuters, NPR, CNBC, AP) and saves them to a CSV file.

## Quick Start

```bash
# 1. Create a virtual environment
python -m venv venv

# 2. Activate it
#    Windows:
venv\Scripts\activate
#    macOS / Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the scraper
python news_scraper.py
```

## Output

| File | Description |
|---|---|
| `headlines.csv` | All scraped headlines (source, title, link, published, summary) |
| `news_scraper.log` | Timestamped log with per-feed fetch/parse results |

## RSS Feeds Scraped

| # | Source |
|---|---|
| 1 | BBC Top Stories |
| 2 | Reuters Top News |
| 3 | NPR News |
| 4 | CNBC Top News |
| 5 | Associated Press (AP) Top News |

## Features

- ✅ 5 diverse RSS feeds
- ✅ Robust error handling — each feed is fetched & parsed independently
- ✅ Request timeout & user-agent header
- ✅ Logging to both console and file
- ✅ Deduplication of identical headlines
- ✅ Summary breakdown by source