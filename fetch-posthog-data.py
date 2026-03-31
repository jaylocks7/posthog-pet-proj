#!/usr/bin/env python3
"""
Fetch PostHog GitHub commit data and score engineers by impact.
Outputs data.json for the static dashboard.

Usage:
    GITHUB_TOKEN=ghp_xxx python fetch-posthog-data.py
    GITHUB_TOKEN=ghp_xxx python fetch-posthog-data.py --no-cache
"""

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = "PostHog/posthog"
BASE_URL = "https://api.github.com"
CACHE_DIR = Path("cache")
OUTPUT_FILE = Path("data.json")
DAYS = 90

SINCE = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

COMMIT_WEIGHTS = {
    "feat": 3.0,
    "fix": 2.5,
    "test": 1.5,
    "refactor": 1.0,
    "chore": 0.5,
    "docs": 0.5,
    "revert": 0.5,
    "perf": 2.0,
    "ci": 0.3,
    "build": 0.3,
    "style": 0.3,
}

DEFAULT_WEIGHT = 0.25

BOT_LOGINS = {"web-flow", "ghost", "posthog-bot", "posthog-ci-bot"}
BOT_PATTERNS = ("[bot]", "-bot", "dependabot", "renovate")

NO_CACHE = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("ERROR: GITHUB_TOKEN environment variable is not set.")
    session = requests.Session()
    session.headers.update({
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return session


def parse_type(text: str) -> str:
    """Extract conventional commit prefix from message/title."""
    match = re.match(r"^([a-z]+)[\(:]", text.strip().lower())
    return match.group(1) if match else "other"


def is_bot(login: str) -> bool:
    if not login:
        return True
    lower = login.lower()
    if lower in BOT_LOGINS:
        return True
    return any(p in lower for p in BOT_PATTERNS)


def _cache_key(url: str, params=None) -> Path:
    raw = url + json.dumps(params or {}, sort_keys=True)
    digest = hashlib.md5(raw.encode()).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _parse_next_link(link_header: str):
    """Extract the 'next' URL from a GitHub Link header."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


def paginate(session: requests.Session, url: str, params=None) -> list:
    """Fetch all pages from a GitHub endpoint with disk caching."""
    cache_file = _cache_key(url, params)

    if not NO_CACHE and cache_file.exists():
        print(f"  [cache] {url}")
        return json.loads(cache_file.read_text())

    print(f"  [fetch] {url}")
    results = []
    current_url = url
    current_params = params

    while current_url:
        resp = session.get(current_url, params=current_params, timeout=30)
        resp.raise_for_status()

        remaining = int(resp.headers.get("X-RateLimit-Remaining", 999))
        if remaining < 5:
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(0, reset_ts - int(time.time())) + 2
            print(f"  [rate limit] sleeping {wait}s")
            time.sleep(wait)

        page = resp.json()
        if not isinstance(page, list):
            results.append(page)
            break

        results.extend(page)
        print(f"    fetched {len(page)} items (total: {len(results)})")

        current_url = _parse_next_link(resp.headers.get("Link", ""))
        current_params = None

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(results))
    return results


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_commits(session: requests.Session) -> list:
    print("\n--- Fetching commits ---")
    return paginate(session, f"{BASE_URL}/repos/{REPO}/commits", {
        "since": SINCE,
        "per_page": 100,
    })


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def build_engineer_stats(commits: list) -> dict:
    stats = {}

    def ensure(login: str, avatar: str = ""):
        if login not in stats:
            stats[login] = {
                "login": login,
                "avatar_url": avatar,
                "score": 0.0,
                "commits_by_type": {},
                "total_commits": 0,
            }
        if avatar and not stats[login]["avatar_url"]:
            stats[login]["avatar_url"] = avatar

    for commit in commits:
        author = commit.get("author")
        if not author:
            continue
        login = author.get("login", "")
        if not login or is_bot(login):
            continue
        avatar = author.get("avatar_url", "")
        ensure(login, avatar)

        msg = commit.get("commit", {}).get("message", "").split("\n")[0]
        ctype = parse_type(msg)
        weight = COMMIT_WEIGHTS.get(ctype, DEFAULT_WEIGHT)
        stats[login]["score"] += weight
        stats[login]["total_commits"] += 1
        stats[login]["commits_by_type"][ctype] = stats[login]["commits_by_type"].get(ctype, 0) + 1

    return stats


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_scores(stats: dict) -> list:
    engineers = [e for e in stats.values() if e["score"] > 0]

    if not engineers:
        return []

    max_score = max(e["score"] for e in engineers)
    min_score = min(e["score"] for e in engineers)

    scored = []
    for eng in engineers:
        if max_score == min_score:
            normalized = 100.0
        else:
            normalized = (eng["score"] - min_score) / (max_score - min_score) * 100

        scored.append({
            "login": eng["login"],
            "avatar_url": eng["avatar_url"],
            "impact_score": round(normalized, 1),
            "raw_score": round(eng["score"], 2),
            "total_commits": eng["total_commits"],
            "commits_by_type": eng["commits_by_type"],
        })

    scored.sort(key=lambda x: x["impact_score"], reverse=True)

    for i, eng in enumerate(scored[:5], start=1):
        eng["rank"] = i

    return scored[:5]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch PostHog GitHub data and score engineers.")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached data and re-fetch")
    args = parser.parse_args()

    global NO_CACHE
    NO_CACHE = args.no_cache

    print(f"Window: last {DAYS} days (since {SINCE})")

    session = make_session()
    commits = fetch_commits(session)
    print(f"\nTotal commits fetched: {len(commits)}")

    stats = build_engineer_stats(commits)
    print(f"Unique engineers identified: {len(stats)}")

    top_raw = sorted(stats.values(), key=lambda x: x["score"], reverse=True)[:10]
    print("\nTop 10 by raw score:")
    for eng in top_raw:
        print(f"  {eng['login']:<30} score={eng['score']:.1f}  commits={eng['total_commits']}")

    top5 = compute_scores(stats)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": DAYS,
        "engineers": top5,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {OUTPUT_FILE} with {len(top5)} engineers.")
    print("\nTop 5 Most Impactful Engineers:")
    for eng in top5:
        print(f"  #{eng['rank']} {eng['login']:<30} score={eng['impact_score']}")


if __name__ == "__main__":
    main()
