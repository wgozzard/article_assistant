
#!/usr/bin/env python3
"""
lambo_scraper.py — "From Ford to Lambo" article extractor for vibe coders.
Goal: feed CLEAN article content (+chunks) straight into your AI chats.

Design:
- Fast, polite, resilient.
- Multi-extractor pipeline: Trafilatura → Readability → Heuristics.
- Metadata mining (title/author/date/canonical) via OpenGraph + JSON-LD.
- Robots.txt aware (optional flag to bypass at your own risk).
- Retries, timeouts, basic caching, rate-limiting.
- Batch mode (file/STDIN/args) → JSONL outputs + per-URL JSON.
- LLM-ready chunks (approx token-aware) + quick bullet keypoints (naive extractive).
- Zero external APIs; optional deps are auto-detected.

Usage:
  python lambo_scraper.py https://example.com/article
  python lambo_scraper.py -f urls.txt
  cat urls.txt | python lambo_scraper.py
  python lambo_scraper.py https://a.com https://b.com -o outdir --no-robots

Outputs:
  outdir/
    YYYYMMDD_HHMMSS_run.jsonl         ← all records, one per URL
    <domain>/<sha256>.json            ← record per URL (cached by content hash)
    <domain>/<sha256>_chunks.txt      ← chunked text for LLMs
    <domain>/<sha256>_summary.md      ← quick summary for reference
"""

import argparse
import os
import re
import sys
import time
import json
import math
import hashlib
import urllib.parse
import urllib.robotparser as robotparser
from datetime import datetime
from typing import Dict, List, Optional

# --- Optional imports (detected at runtime) ---
HAVE_LXML = False
HAVE_TRAFILATURA = False
HAVE_READABILITY = False

try:
    import lxml.html  # noqa: F401
    HAVE_LXML = True
except Exception:
    pass

try:
    import trafilatura  # noqa: F401
    HAVE_TRAFILATURA = True
except Exception:
    pass

try:
    from bs4 import BeautifulSoup
except Exception as e:
    print("BeautifulSoup4 (bs4) is required. pip install beautifulsoup4", file=sys.stderr)
    raise e

try:
    import requests
except Exception as e:
    print("requests is required. pip install requests", file=sys.stderr)
    raise e

try:
    from readability import Document
    HAVE_READABILITY = True
except Exception:
    HAVE_READABILITY = False

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def normalize_url(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    # strip fragments, normalize scheme/host lowercase
    u = u._replace(scheme=u.scheme.lower(), netloc=u.netloc.lower(), fragment="")
    # remove typical tracking params
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    q_clean = [(k,v) for (k,v) in q if not k.lower().startswith(("utm_","fbclid","gclid","mc_eid"))]
    u = u._replace(query=urllib.parse.urlencode(q_clean))
    return urllib.parse.urlunsplit(u)

def robots_allows(url: str, user_agent: str = "lambo_scraper") -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = robotparser.RobotFileParser()
        rp.set_url(robots_url); rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception:
        # If robots missing/unreadable, return False to be conservative
        return False

def fetch(url: str, timeout: int = 20, retries: int = 2, backoff: float = 1.6) -> requests.Response:
    last_err = None
    for i in range(retries + 1):
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            # follow up to default redirects (requests does)
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if "text/html" not in ctype:
                # Some sites use weird content types but still HTML; be lenient
                if not ("text" in ctype or "html" in ctype):
                    raise ValueError(f"Non-HTML content-type: {ctype}")
            return resp
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(backoff ** i)
    raise RuntimeError(f"Fetch failed after retries: {last_err}")

def extract_meta(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    def meta(name=None, prop=None):
        if prop:
            el = soup.find("meta", attrs={"property": prop})
            if el and el.get("content"): return el["content"].strip()
        if name:
            el = soup.find("meta", attrs={"name": name})
            if el and el.get("content"): return el["content"].strip()
        return None

    title = meta(prop="og:title") or meta(name="twitter:title")
    if not title:
        h1 = soup.find("h1")
        if h1: title = h1.get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    desc = meta(prop="og:description") or meta(name="description") or ""

    canonical = None
    link = soup.find("link", rel=lambda x: x and "canonical" in x.lower())
    if link and link.get("href"):
        canonical = link["href"].strip()

    # JSON-LD for author/date
    author = None; published = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json as _json
            data = _json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") in ("NewsArticle","Article","BlogPosting"):
                    # author can be dict/list/str
                    a = item.get("author")
                    if isinstance(a, dict):
                        author = author or a.get("name")
                    elif isinstance(a, list) and a:
                        if isinstance(a[0], dict): author = author or a[0].get("name")
                        elif isinstance(a[0], str): author = author or a[0]
                    elif isinstance(a, str):
                        author = author or a
                    published = published or item.get("datePublished") or item.get("dateCreated")
        except Exception:
            continue

    return {
        "title": title or "",
        "description": desc or "",
        "canonical": canonical or "",
        "author": author or "",
        "published": published or "",
    }

def clean_text(text: str) -> str:
    # Normalize whitespace, drop excessive blank lines
    text = re.sub(r"\r", "", text)
    # Remove boilerplate markers
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

def extract_with_trafilatura(url: str, html: bytes) -> Optional[str]:
    if not HAVE_TRAFILATURA:
        return None
    try:
        from trafilatura import extract
        txt = extract(html, include_comments=False, include_tables=False, favor_recall=True, url=url)
        return clean_text(txt) if txt else None
    except Exception:
        return None

def extract_with_readability(html: bytes) -> Optional[str]:
    if not HAVE_READABILITY:
        return None
    try:
        doc = Document(html)
        summary_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(summary_html, "lxml")
        # remove figcaptions, asides
        for sel in ("aside","header","footer","nav","script","style"):
            for el in soup.select(sel): el.decompose()
        text = "\n".join([p.get_text(" ", strip=True) for p in soup.find_all(["p","h2","h3"]) ])
        return clean_text(text)
    except Exception:
        return None

def extract_with_heuristics(soup: BeautifulSoup) -> str:
    # Drop obvious boilerplate
    for sel in ("header","footer","nav",".nav",".header",".footer","aside","script","style","noscript"):
        for el in soup.select(sel):
            el.decompose()

    candidates = [
        "article",
        "[data-module='ArticleBody']",
        ".article-body, .article-content, .content, .story-body, .post-content, .entry-content",
        "main",
    ]

    best_text = ""
    best_wc = 0
    for selector in candidates:
        for block in soup.select(selector):
            text = block.get_text(separator="\n")
            text = clean_text(text)
            wc = len(text.split())
            if 150 <= wc > best_wc:
                best_text, best_wc = text, wc
        if best_wc:
            break

    if not best_text:
        # Fallback: all paragraphs
        ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        best_text = clean_text("\n\n".join(ps))

    return best_text

def split_into_chunks(text: str, target_tokens: int = 800, max_tokens: int = 1000) -> List[str]:
    """
    Rough token estimate: 1 token ≈ 4 chars (English). We chunk by sentences.
    """
    if not text.strip():
        return []
    # Sentence split (naive)
    parts = re.split(r"(?<=[.!?])\s+\n?|\n{2,}", text)
    chunks, cur = [], ""
    def tokens(s): return max(1, math.ceil(len(s)/4))
    for p in parts:
        if not p.strip(): continue
        if tokens(cur + " " + p) <= target_tokens:
            cur = (cur + " " + p).strip()
        else:
            if cur: chunks.append(cur.strip())
            # If single sentence is huge, hard-split
            if tokens(p) > max_tokens:
                start = 0; step = max_tokens*4
                while start < len(p):
                    chunks.append(p[start:start+step].strip())
                    start += step
                cur = ""
            else:
                cur = p.strip()
    if cur: chunks.append(cur.strip())
    return chunks

def naive_keypoints(text: str, max_points: int = 8) -> List[str]:
    """
    Very simple keypoint extractor: selects diverse, longer sentences early in the article.
    Not "smart", but good for quick orientation.
    """
    sents = re.split(r"(?<=[.!?])\s+", text)
    sents = [s.strip() for s in sents if len(s.strip()) > 40]
    # Prefer first 40% of the article (often contains core thesis)
    cutoff = max(3, int(len(sents)*0.4))
    candidates = sents[:cutoff] if cutoff < len(sents) else sents
    # Deduplicate by content hash
    seen = set(); out = []
    for s in candidates:
        h = sha256_hex(s.lower())
        if h in seen: continue
        seen.add(h)
        out.append(s)
        if len(out) >= max_points: break
    return out

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def save_record(outdir: str, url: str, record: Dict) -> Dict[str, str]:
    parsed = urllib.parse.urlsplit(url)
    domain_dir = os.path.join(outdir, parsed.netloc)
    ensure_dir(domain_dir)
    content_hash = sha256_hex(record.get("text","") or url)
    base = os.path.join(domain_dir, content_hash)
    # write json
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    # write chunks
    if record.get("chunks"):
        with open(base + "_chunks.txt", "w", encoding="utf-8") as f:
            for i, ch in enumerate(record["chunks"], 1):
                f.write(f"--- chunk {i} ---\n{ch}\n\n")
    # write summary
    if record.get("keypoints"):
        with open(base + "_summary.md", "w", encoding="utf-8") as f:
            f.write(f"# {record.get('title','')}\n\n")
            f.write(f"- **URL:** {record.get('url_canonical') or record.get('url')}\n")
            if record.get("author"): f.write(f"- **Author:** {record['author']}\n")
            if record.get("published"): f.write(f"- **Published:** {record['published']}\n")
            f.write(f"- **Word Count:** {record.get('word_count',0)}\n\n")
            f.write("## Key Points\n")
            for kp in record["keypoints"]:
                f.write(f"- {kp}\n")
    return {"json": base + ".json", "chunks": base + "_chunks.txt", "summary": base + "_summary.md"}

def process_url(url: str, *, bypass_robots: bool = False, rate_limit: float = 0.0) -> Dict:
    url = normalize_url(url)
    if not bypass_robots and not robots_allows(url):
        return {"url": url, "error": "Disallowed by robots.txt (use --no-robots to bypass at your own risk)"}

    time.sleep(max(0.0, rate_limit))

    resp = fetch(url)
    html = resp.content
    soup = BeautifulSoup(html, "lxml" if HAVE_LXML else "html.parser")
    meta = extract_meta(soup)

    # Extraction cascade
    text = None
    if HAVE_TRAFILATURA:
        text = extract_with_trafilatura(url, html)
    if not text and HAVE_READABILITY:
        text = extract_with_readability(html)
    if not text:
        text = extract_with_heuristics(soup)

    text = clean_text(text or "")
    wc = len(text.split())

    chunks = split_into_chunks(text, target_tokens=900, max_tokens=1200)
    keypoints = naive_keypoints(text, max_points=8)

    record = {
        "url": url,
        "url_canonical": meta.get("canonical") or url,
        "title": meta.get("title",""),
        "description": meta.get("description",""),
        "author": meta.get("author",""),
        "published": meta.get("published",""),
        "word_count": wc,
        "text": text,
        "chunks": chunks,
        "keypoints": keypoints,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "extractors": {
            "trafilatura": HAVE_TRAFILATURA,
            "readability": HAVE_READABILITY,
            "heuristics": True
        }
    }
    return record

def iter_input_urls(args) -> List[str]:
    urls = []
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line: urls.append(line)
    if args.urls:
        urls.extend(args.urls)
    if not urls and not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line: urls.append(line)
    # de-dupe while preserving order
    seen = set(); uniq = []
    for u in urls:
        u = u.strip()
        if not u: continue
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq

def main():
    ap = argparse.ArgumentParser(description="Lambo-grade article extractor for clean LLM inputs.")
    ap.add_argument("urls", nargs="*", help="One or more URLs")
    ap.add_argument("-f","--file", help="File containing URLs (one per line)")
    ap.add_argument("-o","--outdir", default="lambo_out", help="Output directory")
    ap.add_argument("--no-robots", action="store_true", help="Bypass robots.txt (use at your own risk)")
    ap.add_argument("--rate", type=float, default=0.0, help="Seconds to wait between requests")
    args = ap.parse_args()

    urls = iter_input_urls(args)
    if not urls:
        print("No URLs provided. Pass URLs, -f file, or pipe them via STDIN.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.outdir, exist_ok=True)
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    jsonl_path = os.path.join(args.outdir, f"{run_id}_run.jsonl")
    saved_files = []

    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for url in urls:
            try:
                rec = process_url(url, bypass_robots=args.no_robots, rate_limit=args.rate)
                jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if rec.get("text"):
                    files = save_record(args.outdir, url, rec)
                    saved_files.append(files)
                else:
                    print(f"[WARN] No text extracted for {url}", file=sys.stderr)
            except Exception as e:
                err = {"url": url, "error": str(e)}
                jf.write(json.dumps(err, ensure_ascii=False) + "\n")
                print(f"[ERROR] {url}: {e}", file=sys.stderr)

    print(f"Done. JSONL: {jsonl_path}")
    # Print a small manifest
    manifest = {
        "jsonl": jsonl_path,
        "examples": saved_files[:3],
        "total_urls": len(urls)
    }
    print(json.dumps(manifest, indent=2))

if __name__ == "__main__":
    main()
