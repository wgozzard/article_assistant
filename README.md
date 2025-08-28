# article_assistant
Cleanly extract article title + body text from a URL (JSON out). Tiny requests + BeautifulSoup with smart selectors &amp; sane cleanup.

Article Scraper v1.0

A tiny, no-frills Python scraper that pulls clean article text from a single URL and returns a compact JSON payload:

- Inputs: any public article/blog URL
- Outputs: {"title", "text", "url", "word_count"} or an {"error"}
- Stack: requests + BeautifulSoup (no headless browser)
- Heuristics: tries common article selectors, strips scripts/styles, falls back to <p> tags, normalizes whitespace

Why? Sometimes you don’t want summaries or screenshots—you want the raw words, clean and ready for chunking, RAG, or quick analysis.
----------------------------------------------------------------------------------------------------------------------------------------

## Quick Start

> [!TIP]
> Use a virtualenv to keep things tidy:
> ```bash
> python -m venv .venv && source .venv/bin/activate
> pip install -r requirements.txt
> ```

### Minimal usage (Python)

```python
# quickstart.py
from extractor import extract_article_text

url = "https://example.com/some-article"  # ← replace me
result = extract_article_text(url)

if "error" in result:
    print("❌", result["error"])
else:
    print(f"📰 {result['title']}  •  {result['word_count']} words\n")
    preview = result["text"][:1000]
    print(preview + ("…" if len(result["text"]) > len(preview) else ""))



----------------------------------------------------------------------------------------------------------------------------------------

# What it does (today)?

- Sets a browser-like User-Agent
- Removes <script> / <style>
- Tries common content containers (article, .article-body, .content, etc.)
- Falls back to all <p> tags if needed
- Pulls h1 / <title> / .headline for a best-effort title
- Returns a clean JSON dict you can hand to your pipeline

# Roadmap (nice-to-haves)

- CLI (python extractor.py <url> --out article.json|md)
- Export to Markdown
- Rate limiting + retries
- Per-site selector overrides
- Optional Readability-style scoring

# Notes (legal/ethical)

Use on publicly available pages you have rights to access. Respect site robots/ToS and copyright. This repo is for educational/testing use only.
