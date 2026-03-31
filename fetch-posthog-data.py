#!/usr/bin/env python3
"""
Fetch PostHog GitHub data and score engineers by impact.
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

import redis
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = "PostHog/posthog"
BASE_URL = "https://api.github.com"
OUTPUT_FILE = "data.json"
DAYS = 90

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise SystemExit("ERROR: REDIS_URL is not set. Add it to your .env file.")
_redis = redis.from_url(REDIS_URL, decode_responses=True)

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

PR_WEIGHTS = {
    "feat": 4.0,
    "fix": 3.5,
    "test": 2.0,
    "refactor": 1.5,
    "chore": 0.5,
    "docs": 0.5,
    "revert": 0.5,
    "perf": 3.0,
    "ci": 0.3,
    "build": 0.3,
    "style": 0.3,
}

DEFAULT_WEIGHT = 0.25

BOT_LOGINS = {"web-flow", "ghost", "posthog-bot", "posthog-ci-bot"}
BOT_PATTERNS = ("[bot]", "-bot", "dependabot", "renovate")

# Set to True via --no-cache flag in main()
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


def _cache_key(url: str, params=None) -> str:
    raw = url + json.dumps(params or {}, sort_keys=True)
    digest = hashlib.md5(raw.encode()).hexdigest()
    return f"posthog:{digest}"


def _parse_next_link(link_header: str):
    """Extract the 'next' URL from a GitHub Link header."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


def paginate(session: requests.Session, url: str, params=None,
             early_stop=None) -> list:
    """
    Fetch all pages from a GitHub endpoint with disk caching.
    early_stop: optional callable(page_items) -> bool; if True, stop pagination.
    """
    cache_key = _cache_key(url, params)

    if not NO_CACHE:
        cached = _redis.get(cache_key)
        if cached:
            print(f"  [redis cache] {url}")
            return json.loads(cached)

    print(f"  [fetch] {url}")
    results = []
    current_url = url
    current_params = params

    while current_url:
        resp = session.get(current_url, params=current_params, timeout=30)
        resp.raise_for_status()

        # Rate limit guard
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

        if early_stop and early_stop(page):
            print("  [early stop] reached time boundary")
            break

        current_url = _parse_next_link(resp.headers.get("Link", ""))
        current_params = None  # params only go on first request

    _redis.set(cache_key, json.dumps(results), ex=86400)  # 1-day TTL
    return results


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

def fetch_commits(session: requests.Session) -> list:
    print("\n--- Fetching commits ---")
    return paginate(session, f"{BASE_URL}/repos/{REPO}/commits", {
        "since": SINCE,
        "per_page": 100,
    })


def fetch_pulls(session: requests.Session) -> list:
    """Fetch closed PRs within the 90-day window."""
    print("\n--- Fetching pull requests ---")

    def should_stop(page):
        if not page:
            return True
        oldest = min(item["created_at"] for item in page)
        return oldest < SINCE

    raw = paginate(
        session,
        f"{BASE_URL}/repos/{REPO}/pulls",
        {"state": "closed", "per_page": 100, "sort": "updated", "direction": "desc"},
        early_stop=should_stop,
    )
    # Filter to only PRs created within window
    return [pr for pr in raw if pr.get("created_at", "") >= SINCE]


def fetch_pr_review_comments(session: requests.Session) -> list:
    print("\n--- Fetching PR review comments ---")
    return paginate(session, f"{BASE_URL}/repos/{REPO}/pulls/comments", {
        "since": SINCE,
        "per_page": 100,
    })


def fetch_issue_comments(session: requests.Session) -> list:
    print("\n--- Fetching issue comments ---")
    raw = paginate(session, f"{BASE_URL}/repos/{REPO}/issues/comments", {
        "since": SINCE,
        "per_page": 100,
    })
    # Only pure issue comments, not PR conversation comments
    return [c for c in raw if "/issues/" in c.get("html_url", "")]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def build_engineer_stats(commits: list, pulls: list,
                          pr_comments: list, issue_comments: list) -> dict:
    stats = {}

    def ensure(login: str, avatar: str = ""):
        if login not in stats:
            stats[login] = {
                "login": login,
                "avatar_url": avatar,
                "delivery_raw": 0.0,
                "commits_scored": 0,
                "prs_opened": 0,
                "prs_merged": 0,
                "review_comments": 0,
                "issue_comments": 0,
            }
        if avatar and not stats[login]["avatar_url"]:
            stats[login]["avatar_url"] = avatar

    # --- Pass 1: commits ---
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
        stats[login]["delivery_raw"] += weight
        stats[login]["commits_scored"] += 1

    # --- Pass 2: PRs ---
    for pr in pulls:
        user = pr.get("user")
        if not user:
            continue
        login = user.get("login", "")
        if not login or is_bot(login):
            continue
        avatar = user.get("avatar_url", "")
        ensure(login, avatar)

        stats[login]["prs_opened"] += 1

        if pr.get("merged_at") and pr["merged_at"] >= SINCE:
            stats[login]["prs_merged"] += 1
            title = pr.get("title", "")
            ptype = parse_type(title)
            weight = PR_WEIGHTS.get(ptype, DEFAULT_WEIGHT)
            stats[login]["delivery_raw"] += weight

    # --- Pass 3: PR review comments ---
    for comment in pr_comments:
        user = comment.get("user")
        if not user:
            continue
        login = user.get("login", "")
        if not login or is_bot(login):
            continue
        avatar = user.get("avatar_url", "")
        ensure(login, avatar)
        stats[login]["review_comments"] += 1

    # --- Pass 4: issue comments ---
    for comment in issue_comments:
        user = comment.get("user")
        if not user:
            continue
        login = user.get("login", "")
        if not login or is_bot(login):
            continue
        avatar = user.get("avatar_url", "")
        ensure(login, avatar)
        stats[login]["issue_comments"] += 1

    return stats


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _normalize(values: list) -> list:
    """Min-max normalize a list to 0-100. If all equal, return 50s."""
    mn, mx = min(values), max(values)
    if mx == mn:
        return [50.0] * len(values)
    return [(v - mn) / (mx - mn) * 100 for v in values]


def compute_scores(stats: dict) -> list:
    engineers = list(stats.values())

    # Compute merge rate; None = will receive neutral score
    for eng in engineers:
        if eng["prs_opened"] >= 3:
            eng["merge_rate_raw"] = eng["prs_merged"] / eng["prs_opened"]
        else:
            eng["merge_rate_raw"] = None

    # Filter out engineers with no activity
    engineers = [e for e in engineers
                 if e["delivery_raw"] > 0 or (e["review_comments"] + e["issue_comments"]) > 0]

    if not engineers:
        return []

    # Normalize delivery
    delivery_vals = [e["delivery_raw"] for e in engineers]
    delivery_norms = _normalize(delivery_vals)

    # Normalize merge rate among those with >= 3 PRs; others get 50 (neutral)
    has_rate = [e for e in engineers if e["merge_rate_raw"] is not None]
    if has_rate:
        rate_vals = [e["merge_rate_raw"] for e in has_rate]
        rate_norms = _normalize(rate_vals)
        rate_map = {e["login"]: n for e, n in zip(has_rate, rate_norms)}
    else:
        rate_map = {}

    # Normalize collaboration
    collab_vals = [e["review_comments"] + e["issue_comments"] for e in engineers]
    collab_norms = _normalize(collab_vals)

    scored = []
    for i, eng in enumerate(engineers):
        d_norm = delivery_norms[i]
        q_norm = rate_map.get(eng["login"], 50.0)
        c_norm = collab_norms[i]

        composite = d_norm * 0.40 + q_norm * 0.30 + c_norm * 0.30

        scored.append({
            "login": eng["login"],
            "avatar_url": eng["avatar_url"],
            "composite_score": round(composite, 1),
            "scores": {
                "delivery": {
                    "raw": round(eng["delivery_raw"], 2),
                    "normalized": round(d_norm, 1),
                    "weighted": round(d_norm * 0.40, 1),
                },
                "quality": {
                    "raw": round(eng["merge_rate_raw"], 3) if eng["merge_rate_raw"] is not None else None,
                    "normalized": round(q_norm, 1),
                    "weighted": round(q_norm * 0.30, 1),
                },
                "collaboration": {
                    "raw": eng["review_comments"] + eng["issue_comments"],
                    "normalized": round(c_norm, 1),
                    "weighted": round(c_norm * 0.30, 1),
                },
            },
            "details": {
                "commits_scored": eng["commits_scored"],
                "prs_opened": eng["prs_opened"],
                "prs_merged": eng["prs_merged"],
                "review_comments": eng["review_comments"],
                "issue_comments": eng["issue_comments"],
            },
        })

    scored.sort(key=lambda x: x["composite_score"], reverse=True)

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

    CACHE_DIR.mkdir(exist_ok=True)

    print(f"Window: last {DAYS} days (since {SINCE})")

    global NO_CACHE
    NO_CACHE = args.no_cache
    session = make_session()

    commits = fetch_commits(session)
    pulls = fetch_pulls(session)
    pr_comments = fetch_pr_review_comments(session)
    issue_comments = fetch_issue_comments(session)

    print(f"\nRaw counts — commits: {len(commits)}, PRs: {len(pulls)}, "
          f"PR comments: {len(pr_comments)}, issue comments: {len(issue_comments)}")

    stats = build_engineer_stats(commits, pulls, pr_comments, issue_comments)
    print(f"Unique engineers identified: {len(stats)}")

    # Sanity check: top 10 by raw delivery
    top_raw = sorted(stats.values(), key=lambda x: x["delivery_raw"], reverse=True)[:10]
    print("\nTop 10 by raw delivery score:")
    for eng in top_raw:
        print(f"  {eng['login']:<30} delivery={eng['delivery_raw']:.1f}  "
              f"prs={eng['prs_merged']}/{eng['prs_opened']}  "
              f"collab={eng['review_comments'] + eng['issue_comments']}")

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
        print(f"  #{eng['rank']} {eng['login']:<30} score={eng['composite_score']}")


if __name__ == "__main__":
    main()
