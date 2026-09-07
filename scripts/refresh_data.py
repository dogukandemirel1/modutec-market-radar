#!/usr/bin/env python3
"""
Refreshes data.json for Modutec Market Radar from free RSS feeds.

Pulls headlines from a set of marine/offshore/energy/defence trade press
RSS feeds, sorts each item into one of the 5 existing dashboard categories
via keyword matching, and merges them into the existing data.json - it
never wipes a category and replaces it with just this run's fetch.

Merge behaviour, per category:
  - New items are combined with whatever is already on disk.
  - Duplicates are skipped by matching on source_url.
  - Items older than RETENTION_DAYS are dropped automatically, EXCEPT
    categories listed in EXPIRY_EXEMPT_CATEGORIES (day-rates: benchmark
    rate reports don't get published daily via RSS, so that category
    keeps its items until fresh day-rate items actually replace them).

Each item is also flagged "relevant": true/false based on whether its
title/summary mentions Modutec's core markets (UAE, KSA, Saudi Arabia,
Aberdeen, ADNOC, Aramco, Dubai, Abu Dhabi) - see MODUTEC_RELEVANCE_KEYWORDS.
Existing items from prior runs that predate this field are backfilled
the same way.

index.html requires no changes - the JSON structure is unchanged.
No API keys required - RSS only.
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = REPO_ROOT / "data.json"

RETENTION_DAYS = 60
EXPIRY_EXEMPT_CATEGORIES = {"day-rates"}
MIN_SCORE = 2  # a single weak (weight-1) keyword hit alone isn't enough to classify
SUMMARY_MAX_CHARS = 220
USER_AGENT = (
    "Mozilla/5.0 (compatible; ModutecMarketRadarBot/1.0; "
    "+https://github.com/dogukandemirel1/modutec-market-radar)"
)

# --- Feeds -----------------------------------------------------------------
# (source_name, feed_url) - all free/public RSS, no auth required.
FEEDS = [
    ("gCaptain", "https://gcaptain.com/feed/"),
    ("Splash247", "https://splash247.com/feed/"),
    ("Drilling Contractor", "https://drillingcontractor.org/feed"),
    ("Naval News", "https://www.navalnews.com/feed/"),
    ("Offshore Energy", "https://www.offshore-energy.biz/feed/"),
    ("Rigzone", "https://www.rigzone.com/news/rss/rigzone_latest.aspx"),
    ("Offshore Technology", "https://www.offshore-technology.com/feed/"),
    ("Naval Technology", "https://www.naval-technology.com/feed/"),
    ("Offshore Engineer", "https://www.oedigital.com/news/latest?format=feed"),
]

# --- Category keyword scoring -----------------------------------------------
# Each category maps to (keyword substring -> weight). Every matched
# feed item is scored against every category on lowercased title+summary
# text; it's assigned to the highest-scoring category (ties broken by the
# order of CATEGORY_ORDER). Items that score 0 everywhere are dropped.
CATEGORY_KEYWORDS: dict[str, dict[str, int]] = {
    "day-rates": {
        "day rate": 3, "day-rate": 3, "dayrate": 3, "day rates": 3,
        "daily rate": 3, "charter rate": 3, "leading-edge rate": 3,
        "utilisation": 2, "utilization": 2, "rig rate": 3,
        "jackup": 1, "drillship": 1, "semisubmersible": 1, "semisub": 1,
        "ahts": 1, "psv": 1, "osv": 1,
    },
    "tenders": {
        "epci": 3, "epc contract": 3, "fid": 3,
        "final investment decision": 3, "tender": 3,
        "letter of award": 3, "loa": 2, "charter contract": 2,
        "awarded": 2, "award": 2, "contract": 2, "wins contract": 2,
        "secures contract": 2, "signs contract": 2, "procurement": 1,
    },
    "regulatory": {
        "marpol": 3, "solas": 3, "sanction": 3, "sanctions": 3,
        "export control": 3, "itar": 3, "emissions trading": 3,
        "imo": 2, "compliance": 2, "regulation": 2, "regulator": 2,
        "class society": 2, "notation": 2, "ets": 2, "ballast water": 2,
        "dnv": 1, "lloyd's register": 1, "abs class": 1, "flag state": 1,
        "ban": 1,
    },
    "competitors": {
        "acquisition": 3, "acquires": 3, "acquired": 3, "merger": 3,
        "merges": 3, "joint venture": 2, "divest": 2, "divestment": 2,
        "stake sale": 2, "restructuring": 2, "partnership": 1,
        "expands": 1, "launches": 1, "new product": 1, "leadership": 1,
    },
    "regional": {
        "uae": 2, "abu dhabi": 2, "dubai": 2, "saudi": 2, "ksa": 2,
        "aramco": 2, "adnoc": 2, "malaysia": 2, "indonesia": 2,
        "singapore": 2, "vietnam": 2, "thailand": 2, "philippines": 2,
        "brunei": 2, "southeast asia": 2, "south east asia": 2,
        "gulf": 1, "middle east": 1,
    },
}

CATEGORY_ORDER = ["day-rates", "tenders", "regulatory", "competitors", "regional"]

REGION_KEYWORDS = {
    "UAE": ["uae", "abu dhabi", "dubai", "adnoc"],
    "KSA": ["saudi", "ksa", "aramco"],
    "SEA": [
        "malaysia", "indonesia", "singapore", "vietnam", "thailand",
        "philippines", "brunei", "southeast asia", "south east asia",
    ],
}

# Any item (in any category) whose title/summary mentions one of these is
# flagged "relevant": true, so the site can offer a "Modutec-relevant" filter.
MODUTEC_RELEVANCE_KEYWORDS = [
    "uae", "ksa", "saudi arabia", "aberdeen", "adnoc", "aramco",
    "dubai", "abu dhabi",
]


def clean_summary(raw_html: str) -> str:
    text = html.unescape(raw_html or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > SUMMARY_MAX_CHARS:
        cut = text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0]
        text = cut.rstrip(",.;:") + "…"
    return text


def entry_date(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None) or entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                continue
    return None


def classify(text: str) -> str | None:
    best_cat, best_score = None, 0
    for cat in CATEGORY_ORDER:
        score = sum(w for kw, w in CATEGORY_KEYWORDS[cat].items() if kw in text)
        if score > best_score:
            best_cat, best_score = cat, score
    return best_cat if best_score >= MIN_SCORE else None


def detect_region(text: str) -> str | None:
    for region, keywords in REGION_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return region
    return None


def is_modutec_relevant(text: str) -> bool:
    return any(kw in text for kw in MODUTEC_RELEVANCE_KEYWORDS)


def fetch_items() -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {cat: [] for cat in CATEGORY_ORDER}

    for source_name, url in FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
            parsed = feedparser.parse(raw)
        except Exception as exc:  # noqa: BLE001 - one bad feed shouldn't kill the run
            print(f"  [warn] could not fetch {source_name} ({url}): {exc}", file=sys.stderr)
            continue

        if parsed.bozo and not parsed.entries:
            print(f"  [warn] no entries parsed from {source_name} ({url})", file=sys.stderr)
            continue

        for entry in parsed.entries:
            title = html.unescape(getattr(entry, "title", "") or "").strip()
            link = getattr(entry, "link", "") or ""
            if not title or not link:
                continue

            date = entry_date(entry)
            if not date:
                continue

            raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            summary = clean_summary(raw_summary)

            haystack = f"{title} {summary}".lower()
            category = classify(haystack)
            if category is None:
                continue

            item = {
                "title": title,
                "summary": summary,
                "date": date,
                "source_name": source_name,
                "source_url": link,
                "relevant": is_modutec_relevant(haystack),
            }
            if category == "regional":
                region = detect_region(haystack)
                if region:
                    item["region"] = region

            buckets[category].append(item)

    return buckets


def backfill_relevant(item: dict) -> dict:
    if "relevant" not in item:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        item["relevant"] = is_modutec_relevant(text)
    return item


def merge_items(
    category: str, new_items: list[dict], existing_items: list[dict], cutoff_date: str
) -> list[dict]:
    """Merge new + existing items for one category: dedupe by source_url,
    drop anything older than cutoff_date (unless the category is expiry
    exempt), sort newest first. Never truncates to a fixed count - the
    only thing that removes an item going forward is age (or expiry
    exemption keeping it around indefinitely)."""
    existing_items = [backfill_relevant(dict(it)) for it in existing_items]

    seen_urls = set()
    merged = []
    for item in new_items + existing_items:
        url = item.get("source_url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(item)

    if category not in EXPIRY_EXEMPT_CATEGORIES:
        merged = [it for it in merged if it.get("date", "") >= cutoff_date]

    merged.sort(key=lambda i: i.get("date", ""), reverse=True)
    return merged


def main() -> None:
    if not DATA_PATH.exists():
        print(f"error: {DATA_PATH} not found", file=sys.stderr)
        sys.exit(1)

    existing = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    existing_sections = {s["id"]: s for s in existing.get("sections", [])}

    today = datetime.now(timezone.utc).date()
    cutoff_date = (today - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")

    print("Fetching feeds...")
    new_buckets = fetch_items()
    for cat in CATEGORY_ORDER:
        print(f"  {cat}: {len(new_buckets[cat])} new candidate item(s) matched")

    sections = []
    for cat in CATEGORY_ORDER:
        base = existing_sections.get(cat, {})
        existing_items = base.get("items", [])
        merged_items = merge_items(cat, new_buckets[cat], existing_items, cutoff_date)
        sections.append({
            "id": cat,
            "title": base.get("title", cat),
            "short_title": base.get("short_title", cat),
            "description": base.get("description", ""),
            "items": merged_items,
        })
        exempt_note = " (expiry exempt)" if cat in EXPIRY_EXEMPT_CATEGORIES else f" (>= {cutoff_date})"
        print(f"  {cat}: {len(merged_items)} item(s) after merge{exempt_note}")

    output = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "footer_note": existing.get(
            "footer_note",
            "Modutec Market Radar — compiled from public industry sources. "
            "Verify figures against primary sources before use in decision-making.",
        ),
        "sections": sections,
    }

    DATA_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote {DATA_PATH}")


if __name__ == "__main__":
    main()
