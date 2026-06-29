# PostHog Engineer Impact Dashboard

**Live:** [Dashboard](https://jaylocks7.github.io/posthog-pet-proj/)

A static dashboard that ranks the top 5 most impactful engineers at PostHog based on commit activity over the last 90 days.

---

## How Impact Is Measured

The only publicly available signals from a GitHub repo are commits, pull requests, and issues. Of these, commits carry the most structured, per-engineer signal — so that's the focus here.

Raw commit count is a bad metric. You can inflate it with trivial `docs()` or `chore()` commits all day. What matters is the *type* of work being done.

PostHog follows [Conventional Commits](https://www.conventionalcommits.org/), which means every commit message is prefixed with a type:

```
feat: sandbox dev environment v2
fix: disable egress proxy in LLM gateway client
test(experiments): add feature flag for DW A/A test
refactor: use outputs for hog transformations
docs(internal): document parallel query execution pattern
chore(code): add worktree config for posthog code
```

`feat`, `fix`, `test`, and `perf` are what keep the product shipping and stable — they're the primary drivers. `refactor`, `chore`, `docs`, `revert` are supporting work. The scoring weights reflect that.

### Impact Score Formula

```
raw   = Σ weight(commit_type)
score = (raw − min) / (max − min) × 100
```

| Type       | Weight |
|------------|--------|
| `feat`     | 3.0    |
| `fix`      | 2.5    |
| `perf`     | 2.0    |
| `test`     | 1.5    |
| `refactor` | 1.0    |
| `chore`    | 0.5    |
| `docs`     | 0.5    |
| `revert`   | 0.5    |
| `ci`       | 0.3    |
| `build`    | 0.3    |
| `style`    | 0.3    |

Scores are normalized 0–100 relative to the field, so the top engineer always sits at 100 and everyone else is ranked proportionally.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub Actions                           │
│              (cron: daily at midnight PT)                       │
└────────────────────────────┬────────────────────────────────────┘
                             │ triggers
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   fetch-posthog-data.py                         │
│                                                                 │
│  1. Calls GitHub API → GET /repos/PostHog/posthog/commits       │
│     - last 90 days, paginated, 100/page                        │
│     - filters out bots                                          │
│     - disk-cached in cache/ to avoid redundant API calls        │
│                                                                 │
│  2. Parses conventional commit prefixes per author              │
│                                                                 │
│  3. Scores each engineer by weighted commit type sum            │
│                                                                 │
│  4. Normalizes scores (min-max → 0–100), takes top 5           │
│                                                                 │
│  5. Writes → data.json                                          │
└────────────────────────────┬────────────────────────────────────┘
                             │ commits data.json
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    GitHub Repository                            │
│              jaylocks7/posthog-pet-proj                         │
└────────────────────────────┬────────────────────────────────────┘
                             │ served via
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      GitHub Pages                               │
│                       index.html                                │
│                                                                 │
│  - fetches data.json at page load                               │
│  - renders ranked engineer cards with avatars                   │
│  - bar chart of commit type breakdown per engineer              │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow Summary

| Flow | Trigger | Output |
|------|---------|--------|
| **Nightly refresh** | GitHub Actions cron (midnight PT) | Updated `data.json` committed to repo |
| **Manual refresh** | `python fetch-posthog-data.py` locally | `data.json` written to disk |
| **Cache bypass** | `python fetch-posthog-data.py --no-cache` | Fresh API fetch, overwrites cache |
| **Dashboard view** | User opens GitHub Pages URL | Reads `data.json`, renders charts |

---

## Running Locally

```bash
GITHUB_TOKEN=ghp_xxx python fetch-posthog-data.py

# force fresh data (ignore cache)
GITHUB_TOKEN=ghp_xxx python fetch-posthog-data.py --no-cache
```

---

## Known Issues

n/a

## Future Iterations

A section in the dashboard that shows close-to-real-time-updates for the
repo's commit history within the last 90-days