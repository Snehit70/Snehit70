#!/usr/bin/env python3
"""Generate Snehit's profile telemetry SVGs from GitHub data.

Pulls repos (public + private, with archived/forks excluded), default-branch
commits, releases of public repos, and authored PRs via the GitHub GraphQL/REST API (through `gh`),
derives a battery of stats, and renders two SVG cards (activity, and shipping
& stack), each in a dark and a light variant, plus a JSON snapshot.
"""
import datetime as dt
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
USER = "Snehit70"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))  # IST for hour-of-day.
TODAY = dt.datetime.now(dt.timezone.utc).date()
SINCE = TODAY - dt.timedelta(days=364)  # 365 days, today included.

CATEGORY_KEYWORDS = {
    "AI / Voice": ("voice", "transcribe", "stt", "ai", "agent", "ralphy"),
    "Linux / CLI": ("hypr", "systemd", "tui", "cli", "tmux", "neovim", "dot", "lapstat"),
    "Web Apps": ("web", "app", "next", "vue", "react", "vercel", "aceon", "field", "fate", "saksham"),
    "Mobile": ("android", "kotlin", "pomo", "mobile"),
    "Infra": ("docker", "gateway", "proxy", "service", "monitor"),
}
IGNORED_LANGUAGES = {"Jupyter Notebook"}
# Repos whose bytes would misrepresent the language mix (coursestate is ~1.5 MB of generated HTML).
LANGUAGE_SKIP_REPOS = {"coursestate"}
VERBOSE = os.environ.get("VERBOSE") == "1"

# ---------------------------------------------------------------------------
# GitHub fetchers
# ---------------------------------------------------------------------------

def gh_json(args):
    proc = subprocess.run(["gh", "api", *args], cwd=ROOT, text=True, capture_output=True)
    if proc.returncode != 0:
        print("failed: gh api " + " ".join(args), file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)
    return json.loads(proc.stdout)


def gh_graphql(query, **fields):
    args = ["graphql", "-f", f"query={query}"]
    for key, value in fields.items():
        if value is None:
            continue
        args.extend(["-F", f"{key}={value}"])
    return gh_json(args)


def fetch_repos():
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false, orderBy: {field: PUSHED_AT, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            name
            description
            isPrivate
            isArchived
            pushedAt
            url
            primaryLanguage { name }
            defaultBranchRef { name }
            languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name color } }
            }
            releases(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
              pageInfo { hasNextPage endCursor }
              nodes { tagName publishedAt isDraft }
            }
          }
        }
      }
    }
    """
    repos = []
    cursor = None
    while True:
        data = gh_graphql(query, login=USER, cursor=cursor)
        conn = data["data"]["user"]["repositories"]
        for node in conn["nodes"]:
            if node["isArchived"] or not node["defaultBranchRef"]:
                continue
            repos.append({
                "name": node["name"],
                "description": node["description"] or "",
                "url": node["url"],
                "pushedAt": node["pushedAt"],
                "isPrivate": node["isPrivate"],
                "primaryLanguage": (node["primaryLanguage"] or {}).get("name"),
                "defaultBranch": node["defaultBranchRef"]["name"],
                "languages": node["languages"]["edges"],
                "releases": node["releases"]["nodes"] + more_releases(node["name"], node["releases"]),
            })
        if not conn["pageInfo"]["hasNextPage"]:
            return repos
        cursor = conn["pageInfo"]["endCursor"]


def more_releases(name, conn):
    """Pages past the first 100 releases until they fall out of the window."""
    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        releases(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes { tagName publishedAt isDraft }
        }
      }
    }
    """
    out = []
    page, nodes = conn.get("pageInfo") or {}, conn["nodes"]
    while page.get("hasNextPage") and nodes and (nodes[-1]["publishedAt"] or "9") >= SINCE.isoformat():
        conn = gh_graphql(query, owner=USER, name=name, cursor=page["endCursor"])["data"]["repository"]["releases"]
        page, nodes = conn["pageInfo"], conn["nodes"]
        out.extend(nodes)
    return out


def fetch_commits(repo):
    query = """
    query($owner: String!, $name: String!, $branch: String!, $since: GitTimestamp!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        ref(qualifiedName: $branch) {
          target {
            ... on Commit {
              history(first: 100, since: $since, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  committedDate
                  additions
                  deletions
                  author { user { login } }
                }
              }
            }
          }
        }
      }
    }
    """
    commits = []
    cursor = None
    while True:
        if VERBOSE:
            print(f"commits: {repo['name']}", file=sys.stderr)
        data = gh_graphql(
            query,
            owner=USER,
            name=repo["name"],
            branch=repo["defaultBranch"],
            since=SINCE.isoformat() + "T00:00:00Z",
            cursor=cursor,
        )
        ref = data["data"]["repository"]["ref"]
        if not ref:
            return commits
        history = ref["target"]["history"]
        for node in history["nodes"]:
            author = ((node.get("author") or {}).get("user") or {}).get("login")
            if author == USER:
                commits.append(node)
        if not history["pageInfo"]["hasNextPage"]:
            return commits
        cursor = history["pageInfo"]["endCursor"]


def fetch_prs():
    """Authored PRs in the window. Search serves at most 1,000 hits per query, so ask month by month."""
    items = {}
    lo = SINCE - dt.timedelta(days=1)  # PR timestamps are UTC; IST days start 5.5 h earlier.
    while lo <= TODAY:
        hi = min(lo + dt.timedelta(days=30), TODAY + dt.timedelta(days=1))
        query = f"author:{USER} type:pr created:{lo.isoformat()}..{hi.isoformat()}"
        page = 1
        while True:
            data = gh_json(["search/issues", "-X", "GET", "-f", f"q={query}", "-f", "per_page=100", "-f", f"page={page}"])
            for item in data.get("items", []):
                items[item["id"]] = item
            if page * 100 >= min(data.get("total_count", 0), 1000) or not data.get("items"):
                break
            page += 1
        lo = hi + dt.timedelta(days=1)
    return list(items.values())


def fetch_contributions():
    """GitHub's contribution calendar for the card window: commits, PRs, issues, reviews and private work."""
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
        }
      }
    }
    """
    window = {"from": f"{SINCE.isoformat()}T00:00:00Z",
              "to": f"{(TODAY + dt.timedelta(days=1)).isoformat()}T00:00:00Z"}
    cal = gh_graphql(query, login=USER, **window)["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    daily = {day["date"]: day["contributionCount"] for week in cal["weeks"] for day in week["contributionDays"]}
    return {"total": cal["totalContributions"], "daily": daily}


def repo_releases(name, nodes):
    releases = []
    for rel in nodes:
        if rel["isDraft"] or not rel["publishedAt"]:
            continue
        ts = dt.datetime.fromisoformat(rel["publishedAt"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        if ts.date() >= SINCE:
            releases.append({"repo": name, "tag": rel["tagName"], "date": ts.date().isoformat(), "at": ts.isoformat()})
    return releases


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def percentile(values, p):
    if not values:
        return 0
    vals = sorted(values)
    k = (len(vals) - 1) * (p / 100)
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return vals[int(k)]
    return vals[lower] * (upper - k) + vals[upper] * (k - lower)


def ist_date(stamp):
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(LOCAL_TZ).date()


def iso_week_start(day):
    return day - dt.timedelta(days=day.weekday())


def repo_category(repo):
    text = " ".join([repo["name"], repo.get("description") or "", repo.get("primaryLanguage") or ""]).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "Other"


def streaks(active_days):
    current = 0
    cursor = TODAY
    # Grace period: a streak stays alive until the end of the next day, so an
    # early-morning run that finds no commit *yet today* counts from yesterday
    # instead of resetting to 0.
    if cursor not in active_days:
        cursor -= dt.timedelta(days=1)
    while cursor in active_days:
        current += 1
        cursor -= dt.timedelta(days=1)
    longest = 0
    run = 0
    for offset in range((TODAY - SINCE).days + 1):
        day = SINCE + dt.timedelta(days=offset)
        if day in active_days:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return current, longest


# ---------------------------------------------------------------------------
# Collection + derivation
# ---------------------------------------------------------------------------

def collect():
    repos = fetch_repos()
    daily = Counter()
    repo_daily = defaultdict(Counter)
    hour_dist = Counter()
    punchcard = [[0] * 24 for _ in range(7)]  # Weekday x hour, IST, without the profile repo.
    additions = 0
    deletions = 0
    language_bytes = Counter()
    language_colors = {}
    category_commits = Counter()

    for repo in repos:
        for edge in repo["languages"]:
            lang = edge["node"]["name"]
            if lang not in IGNORED_LANGUAGES and repo["name"] not in LANGUAGE_SKIP_REPOS:
                language_bytes[lang] += edge["size"]
                language_colors[lang] = edge["node"].get("color")
        commits = fetch_commits(repo)
        category = repo_category(repo)
        for commit in commits:
            ts = dt.datetime.fromisoformat(commit["committedDate"].replace("Z", "+00:00"))
            local_ts = ts.astimezone(LOCAL_TZ)
            day = local_ts.date()
            daily[day] += 1
            hour_dist[local_ts.hour] += 1
            if repo["name"] not in SKIP:
                punchcard[local_ts.weekday()][local_ts.hour] += 1
            repo_daily[repo["name"]][day] += 1
            category_commits[category] += 1
            additions += commit.get("additions") or 0
            deletions += commit.get("deletions") or 0

    releases = []
    for repo in repos:
        # Popped so the snapshot's repo list carries no tags; private repos stay off the cards.
        nodes = repo.pop("releases")
        if not repo["isPrivate"]:
            releases.extend(repo_releases(repo["name"], nodes))
    releases.sort(key=lambda r: r["at"], reverse=True)

    prs = fetch_prs()
    contributions = fetch_contributions()
    pr_weekly = Counter()
    merged = 0
    prs = [pr for pr in prs if SINCE <= ist_date(pr["created_at"]) <= TODAY]
    for pr in prs:
        created = ist_date(pr["created_at"])
        pr_weekly[iso_week_start(created)] += 1
        if pr.get("pull_request", {}).get("merged_at"):
            merged += 1
    pr_active_weeks = sum(1 for v in pr_weekly.values() if v > 0)

    days = [SINCE + dt.timedelta(days=i) for i in range((TODAY - SINCE).days + 1)]
    daily_values = [daily[day] for day in days]
    active_days_set = {d for d, c in daily.items() if c > 0}
    active_values = [v for v in daily_values if v > 0]
    current_streak, longest_streak = streaks(active_days_set)

    # Weekday pulse: mean commits per occurrence of each weekday.
    weekday_total = [0] * 7
    weekday_count = [0] * 7
    weekday_active = [0] * 7
    for day in days:
        wd = day.weekday()
        weekday_count[wd] += 1
        weekday_total[wd] += daily[day]
        if daily[day] > 0:
            weekday_active[wd] += 1
    weekday_mean = [t / c if c else 0 for t, c in zip(weekday_total, weekday_count)]
    best_weekday = max(range(7), key=lambda i: weekday_mean[i])
    quiet_weekday = min(range(7), key=lambda i: weekday_mean[i])

    # Monthly commit totals over the trailing 12 calendar months.
    monthly = []
    cursor_month = dt.date(TODAY.year, TODAY.month, 1)
    for _ in range(12):
        start = cursor_month
        if start.month == 12:
            next_month = dt.date(start.year + 1, 1, 1)
        else:
            next_month = dt.date(start.year, start.month + 1, 1)
        total = sum(c for d, c in daily.items() if start <= d < next_month)
        monthly.append((start.isoformat(), total))
        if start.month == 1:
            cursor_month = dt.date(start.year - 1, 12, 1)
        else:
            cursor_month = dt.date(start.year, start.month - 1, 1)
    monthly.reverse()

    def repo_sum(name, days_back):
        start = TODAY - dt.timedelta(days=days_back - 1)
        return sum(c for d, c in repo_daily[name].items() if d >= start)

    top_week = [(n, c) for n, c in sorted(((n, repo_sum(n, 7)) for n in repo_daily), key=lambda x: x[1], reverse=True) if c > 0][:5]
    top_month = [(n, c) for n, c in sorted(((n, repo_sum(n, 30)) for n in repo_daily), key=lambda x: x[1], reverse=True) if c > 0][:5]
    top_year = sorted(((n, sum(repo_daily[n].values())) for n in repo_daily), key=lambda x: x[1], reverse=True)
    top_year = [(n, c) for n, c in top_year if c > 0]

    # Velocity trend: last 30d versus the prior 30d.
    last_30 = sum(c for d, c in daily.items() if d > TODAY - dt.timedelta(days=30))
    prev_30 = sum(c for d, c in daily.items() if TODAY - dt.timedelta(days=60) < d <= TODAY - dt.timedelta(days=30))
    velocity_trend = ((last_30 - prev_30) / prev_30 * 100) if prev_30 else 0.0
    momentum_7d = sum(c for d, c in daily.items() if d > TODAY - dt.timedelta(days=7))

    # Largest single day, burst days, longest quiet stretch.
    biggest_day = max(daily.items(), key=lambda x: x[1], default=(TODAY, 0))
    burst_threshold = percentile(active_values, 90) if active_values else 0
    burst_days = sum(1 for d, c in daily.items() if c >= max(1, burst_threshold))
    longest_gap = 0
    gap = 0
    for d in days:
        if daily[d] == 0:
            gap += 1
            longest_gap = max(longest_gap, gap)
        else:
            gap = 0
    # Mean gap between consecutive active days.
    sorted_active = sorted(active_days_set)
    if len(sorted_active) >= 2:
        gaps = [(sorted_active[i] - sorted_active[i - 1]).days for i in range(1, len(sorted_active))]
        mean_gap = statistics.mean(gaps)
    else:
        mean_gap = 0.0

    # Hour-of-day analysis (24 buckets, IST).
    hour_values = [hour_dist.get(h, 0) for h in range(24)]
    peak_hour = max(range(24), key=lambda h: hour_values[h]) if sum(hour_values) else 0
    # Group hours into night/morning/afternoon/evening buckets.
    hour_buckets = {
        "night (00–06)": sum(hour_values[0:6]),
        "morning (06–12)": sum(hour_values[6:12]),
        "afternoon (12–18)": sum(hour_values[12:18]),
        "evening (18–24)": sum(hour_values[18:24]),
    }

    # Shipping pulse: distinct repos touched.
    shipping_7d = sum(1 for n in repo_daily if repo_sum(n, 7) > 0)
    shipping_30d = sum(1 for n in repo_daily if repo_sum(n, 30) > 0)

    # Repo concentration & entropy across the 365d commit pool.
    year_totals = [c for _, c in top_year]
    year_sum = sum(year_totals) or 1
    focus_top3 = sum(year_totals[:3]) / year_sum * 100
    hhi = sum((c / year_sum) ** 2 for c in year_totals)
    if len(year_totals) > 1:
        entropy = -sum((c / year_sum) * math.log2(c / year_sum) for c in year_totals if c > 0)
        entropy_norm = entropy / math.log2(len(year_totals))
    else:
        entropy = 0.0
        entropy_norm = 0.0

    pr_week_values = list(pr_weekly.values()) or [0]
    # Weekly PR series over the same 12-month window as every other figure.
    pr_recent = []
    wk = iso_week_start(SINCE)
    while wk <= TODAY:
        pr_recent.append((wk.isoformat(), pr_weekly.get(wk, 0)))
        wk += dt.timedelta(weeks=1)

    momentum_score = min(100, round(
        len(active_values) / len(days) * 100 * 0.40
        + min(sum(daily_values) / 18, 35)
        + min(len(prs) / 4, 15)
        + min(max(velocity_trend, 0) / 4, 10)
    ))

    private_count = sum(1 for r in repos if r.get("isPrivate"))
    public_count = len(repos) - private_count

    # Code growth: additions vs deletions ratio (capped formatting elsewhere).
    code_total = additions + deletions
    growth_ratio = (additions / code_total * 100) if code_total else 0
    avg_commit_size = (code_total / sum(daily_values)) if sum(daily_values) else 0

    # PR momentum trend (last 4 weeks vs prior 4 weeks).
    this_4w = sum(c for d, c in daily.items() if d > TODAY - dt.timedelta(days=28))
    prev_4w = sum(c for d, c in daily.items() if TODAY - dt.timedelta(days=56) < d <= TODAY - dt.timedelta(days=28))
    pr_this_4w = sum(v for w, v in pr_weekly.items() if w > TODAY - dt.timedelta(days=28))
    pr_prev_4w = sum(v for w, v in pr_weekly.items() if TODAY - dt.timedelta(days=56) < w <= TODAY - dt.timedelta(days=28))
    pr_trend = ((pr_this_4w - pr_prev_4w) / pr_prev_4w * 100) if pr_prev_4w else 0

    stats = {
        "generated": TODAY.isoformat(),
        "windowDays": len(days),
        "repos": repos,
        "reposAnalyzed": len(repo_daily),
        "reposPublic": public_count,
        "reposPrivate": private_count,
        "biggestDay": {"date": biggest_day[0].isoformat(), "commits": biggest_day[1]},
        "burstDays": burst_days,
        "burstThreshold": burst_threshold,
        "longestGap": longest_gap,
        "meanGapDays": mean_gap,
        "hourDistribution": hour_values,
        "peakHour": peak_hour,
        "hourBuckets": hour_buckets,
        "additionsPct": growth_ratio,
        "avgCommitSize": avg_commit_size,
        "prActiveWeeks": pr_active_weeks,
        "prThis4w": pr_this_4w,
        "prPrev4w": pr_prev_4w,
        "prTrendPct": pr_trend,
        "daily": {d.isoformat(): daily[d] for d in days},
        "activeDaysPct": len(active_values) / len(days) * 100,
        "commitTotal": sum(daily_values),
        "commitMean": statistics.mean(daily_values),
        "commitMedian": statistics.median(daily_values),
        "commitP95": percentile(daily_values, 95),
        "commitActiveMean": statistics.mean(active_values) if active_values else 0,
        "commitMax": max(daily_values) if daily_values else 0,
        "currentStreak": current_streak,
        "longestStreak": longest_streak,
        "additions": additions,
        "deletions": deletions,
        "prs": len(prs),
        "mergedPrs": merged,
        "prMergeRatio": (merged / len(prs) * 100) if prs else 0,
        "prWeeklyMean": statistics.mean(pr_week_values),
        "prWeeklyMedian": statistics.median(pr_week_values),
        "prWeeklyP95": percentile(pr_week_values, 95),
        "prWeeklyMax": max(pr_week_values) if pr_week_values else 0,
        "prRecent": pr_recent,
        "topWeek": top_week,
        "topMonth": top_month,
        "topYear": top_year,
        "languages": language_bytes.most_common(),
        "languageColors": language_colors,
        "categories": category_commits.most_common(),
        "weekdayMean": weekday_mean,
        "weekdayActive": weekday_active,
        "bestWeekday": best_weekday,
        "quietWeekday": quiet_weekday,
        "monthly": monthly,
        "velocity30d": last_30,
        "velocityPrev30d": prev_30,
        "velocityTrendPct": velocity_trend,
        "momentum7d": momentum_7d,
        "momentumScore": momentum_score,
        "shipping7d": shipping_7d,
        "shipping30d": shipping_30d,
        "focusTop3Pct": focus_top3,
        "hhi": hhi,
        "entropy": entropy,
        "entropyNormalized": entropy_norm,
        "punchcard": punchcard,
        "releases": releases,
        "contributions": contributions,
    }
    # Per-repo daily counts feed the cards but stay out of the public snapshot.
    return stats, repo_daily


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------
# Two cards, activity and stack, each written in a dark and a light variant.
# The README picks the variant with <picture>. GitHub serves SVGs through
# <img>, so there are no web fonts or scripts; motion is plain CSS.

W = 880
PAD = 28
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

THEMES = {
    "dark": dict(
        bg="#0d1117", border="#30363d", text="#e6edf3", text2="#9198a1", muted="#7d8590",
        grid="#21262d", track="#161b22", bar="#3d444d", hi="#e6edf3",
        series=["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9", "#2aa7c9", "#a3b33a"], other="#4d545d", named="#8b949e", tick_ink=0,
    ),
    "light": dict(
        bg="#ffffff", border="#d1d9e0", text="#1f2328", text2="#59636e", muted="#6e7781",
        grid="#eff2f5", track="#f6f8fa", bar="#d1d9e0", hi="#1f2328",
        series=["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6", "#1690b3", "#7a8c12"], other="#b4bcc5", named="#8c959f", tick_ink=0.25,
    ),
}
# GitHub linguist colours; a fallback for when the API returns none.
LANG_COLORS = {
    "TypeScript": "#3178c6", "HTML": "#e34c26", "JavaScript": "#f1e05a", "Kotlin": "#a97bff",
    "Python": "#3572a5", "Dart": "#00b4ab", "Vue": "#41b883", "CSS": "#663399",
}
DISPLAY = {"fateWheel": "FateWheel", "systemdManager": "SystemdManager", "PLMPRO": "Mold", "fieldForce": "FieldForce",
           "agentObservatory": "Agent Observatory", "voice_web": "VoiceWeb", "ralphy-monitor": "Ralphy Monitor"}
# Story beats called out on the year chart: (repo, tag, label). Dropped once they leave the window.
MILESTONES = [("aceon", "v0.8.0", "Aceon 0.8"), ("pravah", "web-v1.0.0", "Pravah 1.0"),
              ("pomo", "v2.0.0", "Pomo 2.0"), ("vidyut", "v1.1.0", "Vidyut 1.1")]
SKIP = {USER, "dotfiles", "opencode_config"}  # The profile repo and machine config: setup churn, not projects.
# Private repos whose names may appear on the cards (and always get a series of their own);
# every other private repo is only ever counted.
NAMED_PRIVATE = {"hyprfolio", "blog"}
# Fixed colour slot per project, so a repo keeps its colour when the ranking shifts day to day.
# A featured repo not listed here takes the first free slot.
COLOR_SLOT = {"pravah": 0, "pomo": 1, "hyprvox": 2, "fateWheel": 3, "vidyut": 4, "aceon": 5, "hyprfolio": 6, "blog": 7}


def name(repo):
    return DISPLAY.get(repo, repo[:1].upper() + repo[1:])


def fmt(n):
    return f"{int(round(n)):,}"


def plural(n, word):
    return word if n == 1 else word + "s"


def t(x, y, s, size=12, fill=None, anchor="start", weight=400, font="sans", extra=""):
    fam = SANS if font == "sans" else MONO
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{fam}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}" {extra}>{escape(str(s))}</text>')


def text_w(s, size, font="sans"):
    """Rough advance width; good enough to space legend items, not to clip against."""
    if font == "mono":
        return len(s) * size * 0.6
    narrow, wide = set("il.,:;'|!ftjI1 ·"), set("mwMWOQGDHNU%")
    return sum(size * (0.3 if c in narrow else 0.78 if c in wide else 0.62 if c.isupper() else 0.55) for c in s)


def fit(s, size, max_w, font="sans"):
    """Trim with an ellipsis so a long name can't run into its neighbour."""
    if text_w(s, size, font) <= max_w:
        return s
    while s and text_w(s + "…", size, font) > max_w:
        s = s[:-1]
    return s.rstrip() + "…"


def pair_w(a, b, size=12):
    return text_w(a, size) + 5 + text_w(b, size, "mono")


def pair(x, y, a, b, th, size=12):
    """Primary text followed by a muted mono figure, flowing without width guesses."""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{SANS}" font-size="{size}" fill="{th["text"]}">{escape(a)}'
            f'<tspan dx="5" fill="{th["muted"]}" font-family="{MONO}" font-size="{size}">{escape(b)}</tspan></text>')


def bar(x, y, w, h, fill, r=2, extra=""):
    """Column with rounded top corners, flat on the baseline."""
    if h <= 0.4 or w <= 0:
        return ""
    r = min(r, w / 2, h)
    return (f'<path d="M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
            f'H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z" fill="{fill}" {extra}/>')


def hbar(x, y, w, frac, col, th, delay=0):
    return (f'<rect x="{x:.1f}" y="{y - 7}" width="{w:.1f}" height="6" rx="3" fill="{th["track"]}"/>'
            f'<rect x="{x:.1f}" y="{y - 7}" width="{max(6, frac * w):.1f}" height="6" rx="3" fill="{col}" {anim("grow", delay)}/>')


def mix(a, b, f):
    """Blend two hex colours; f=0 gives a, f=1 gives b."""
    ca, cb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(ca, cb))


def label(x, y, s, th):
    return t(x, y, s.upper(), 11, th["text2"], weight=600, font="mono", extra='letter-spacing="0.6"')


def note(x, y, s, th, anchor="end"):
    return t(x, y, s, 12, th["muted"], anchor=anchor, font="mono")


def rule(y, th):
    return f'<line x1="{PAD}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}" stroke="{th["grid"]}"/>'


MOTION = """<style>
@keyframes rise{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes pop{from{transform:scale(0);opacity:0}}
@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@keyframes show{from{opacity:0}}
.rise,.hang,.grow,.pop,.fade{transform-box:fill-box}
.rise{transform-origin:50% 100%;animation:rise .7s cubic-bezier(.2,.7,.2,1) both}
.hang{transform-origin:50% 0;animation:rise .6s cubic-bezier(.2,.7,.2,1) both}
.grow{transform-origin:0 50%;animation:grow .9s cubic-bezier(.2,.7,.2,1) both}
.pop{transform-origin:50% 50%;animation:pop .5s cubic-bezier(.3,1.5,.5,1) both}
.fade{animation:fade .6s ease-out both}
.show{animation:show .5s ease-out both}
@media (prefers-reduced-motion: reduce){.rise,.hang,.grow,.pop,.fade,.show{animation:none}}
</style>"""


def anim(cls, ms):
    return f'class="{cls}" style="animation-delay:{ms:.0f}ms"'


def card(h, th, title, sub, body, ttl, desc):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" role="img" aria-labelledby="t d">
<title id="t">{escape(ttl)}</title><desc id="d">{escape(desc)}</desc>
{MOTION}
<rect x="0.5" y="0.5" width="{W - 1}" height="{h - 1}" rx="8" fill="{th['bg']}" stroke="{th['border']}"/>
{t(PAD, 44, title, 18, th['text'], weight=600)}
{t(W - PAD, 43, sub, 11, th['muted'], anchor='end', font='mono')}
{rule(62, th)}
{body}
</svg>
"""


def nice_step(maxv, ticks=3):
    raw = max(maxv, 1) / ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def card_data(stats, repo_daily):
    """What both cards plot. Headline numbers come from GitHub's contribution calendar; per-project bars use commits."""
    private = {r["name"] for r in stats["repos"] if r["isPrivate"] and r["name"] not in NAMED_PRIVATE}
    per_repo = {n: Counter({d: c for d, c in counts.items() if SINCE <= d <= TODAY})
                for n, counts in repo_daily.items() if n not in SKIP}
    per_repo = {n: c for n, c in per_repo.items() if c}
    days = [SINCE + dt.timedelta(days=i) for i in range((TODAY - SINCE).days + 1)]
    contrib = Counter({dt.date.fromisoformat(d): c for d, c in stats["contributions"]["daily"].items()})

    year_totals = Counter({n: sum(c.values()) for n, c in per_repo.items()})
    featured = [n for n, _ in year_totals.most_common() if n not in private][:7]
    featured += sorted(n for n in NAMED_PRIVATE if n in per_repo and n not in featured)
    slot = {n: COLOR_SLOT[n] for n in featured if n in COLOR_SLOT}
    free = [i for i in range(8) if i not in slot.values()]
    for n in featured:
        if n not in slot:
            slot[n] = free.pop(0)

    week0 = iso_week_start(SINCE)
    weeks = [week0 + dt.timedelta(weeks=i) for i in range((TODAY - week0).days // 7 + 1)]
    weekly = [Counter() for _ in weeks]
    for n, counts in per_repo.items():
        key = n if n in featured else "other"
        for d, c in counts.items():
            weekly[(d - week0).days // 7][key] += c

    releases = [(r["repo"], r["tag"], dt.date.fromisoformat(r["date"])) for r in stats["releases"]
                if SINCE.isoformat() <= r["date"] <= TODAY.isoformat()]
    latest = {}
    for repo, tag, day in releases:  # Newest first, so the first hit per repo wins.
        latest.setdefault(repo, (tag, day))

    return SimpleNamespace(
        private=private, per_repo=per_repo, days=days,
        contrib_total=stats["contributions"]["total"],
        active_days=sum(1 for d in days if contrib[d]),
        vel=sum(contrib[TODAY - dt.timedelta(days=i)] for i in range(30)),
        vel_prev=sum(contrib[TODAY - dt.timedelta(days=i)] for i in range(30, 60)),
        year_totals=year_totals, featured=featured, slot=slot, week0=week0, weeks=weeks, weekly=weekly,
        punch=stats["punchcard"], releases=releases, latest=latest,
        release_counts=Counter(r for r, _, _ in releases),
        weekly_rel=[Counter(r for r, _, d in releases if (d - week0).days // 7 == i) for i in range(len(weeks))],
    )


def entity_color(v, repo, th):
    """Series colour for featured projects; a plain ink for other named ones; grey only for what's lumped."""
    if repo in v.featured:
        return th["series"][v.slot[repo]]
    return th["other"] if repo in v.private or repo == "other" else th["named"]


def top_projects(v, totals, k):
    """Top k public projects, plus what the rest adds up to, so rows + footnote equal the headline."""
    named = [(n, c) for n, c in totals.most_common() if c > 0 and n not in v.private][:k]
    rest = [(n, c) for n, c in totals.items() if c > 0 and n not in dict(named)]
    note = None
    if rest:
        more = sum(c for _, c in rest)
        priv = sum(c for n, c in rest if n in v.private)
        note = f"+ {fmt(more)} in {len(rest)} more {plural(len(rest), 'repo')}" + (f" ({fmt(priv)} private)" if priv else "")
    return named, note


def ranked_rows(v, rows, footnote, x, y0, width, th, label_w=110, step=24, delay=0):
    """Named projects as bars scaled to the top one; everything else is a footnote, not a bar,
    so one lumped bucket can't shrink every real project to a sliver."""
    out = []
    rmax = max((c for _, c in rows), default=0) or 1
    for i, (n, c) in enumerate(rows):
        y = y0 + i * step
        out.append(f'<g {anim("show", delay + i * 60)}>')
        out.append(t(x, y, fit(name(n), 12, label_w - 8), 12, th["text"]))
        out.append(t(x + width, y, fmt(c), 12, th["text2"], anchor="end", font="mono"))
        out.append(hbar(x + label_w, y, width - label_w - 46, c / rmax, entity_color(v, n, th), th, delay + i * 60))
        out.append("</g>")
    if footnote:
        y = y0 + len(rows) * step
        out.append(f'<g {anim("show", delay + len(rows) * 60)}>{t(x, y, fit(footnote, 12, width), 12, th["muted"])}</g>')
    return out


def render_activity(v, th):
    out = []
    trend = (v.vel - v.vel_prev) / v.vel_prev * 100 if v.vel_prev else 0
    apps = f"{len(v.release_counts)} {plural(len(v.release_counts), 'app')}"
    per_week = len(v.releases) * 7 / len(v.days)
    release_sub = f"~{per_week:.0f} a week · {apps}" if per_week >= 1.5 else f"across {apps}"
    # Outcome first, then habit and pace; raw commit volume last.
    stats = [
        ("Releases", fmt(len(v.releases)), "", release_sub),
        ("Active days", f"{v.active_days / len(v.days) * 100:.0f}", "%", f"{v.active_days} of {len(v.days)} days"),
        ("Last 30 days", fmt(v.vel), plural(v.vel, "contribution"), f"{'+' if trend >= 0 else '−'}{abs(trend):.0f}% on the 30 before"),
        ("Contributions", fmt(v.contrib_total), "", "commits, PRs, issues, reviews"),
    ]
    colw = (W - 2 * PAD) / len(stats)
    for i, (lab, val, unit, sub) in enumerate(stats):
        x = PAD + i * colw + (0 if i == 0 else 20)
        if i:
            out.append(f'<line x1="{PAD + i * colw:.1f}" y1="88" x2="{PAD + i * colw:.1f}" y2="150" stroke="{th["grid"]}"/>')
        out.append(f'<g {anim("fade", i * 70)}>')
        out.append(label(x, 96, lab, th))
        unit_svg = (f'<tspan font-size="14" font-weight="500" fill="{th["text2"]}" dx="{1 if unit == "%" else 3}">{unit}</tspan>'
                    if unit else "")
        out.append(f'<text x="{x:.1f}" y="130" font-family="{SANS}" font-size="30" font-weight="600" fill="{th["text"]}" '
                   f'style="font-variant-numeric: tabular-nums">{escape(val)}{unit_svg}</text>')
        out.append(t(x, 150, sub, 12, th["text2"]))
        out.append("</g>")

    # --- The year, week by week, with release milestones.
    top = 196
    out.append(rule(top - 24, th))
    out.append(label(PAD, top, "The year, week by week", th))
    out.append(note(W - PAD, top, "commits per week · releases per week below", th))
    ly = top + 26
    keys = v.featured + ["other"]
    items = []
    for i, k in enumerate(keys):
        col = entity_color(v, k, th) if k != "other" else th["other"]
        val = v.year_totals[k] if k != "other" else sum(v.year_totals.values()) - sum(v.year_totals[f] for f in v.featured)
        if val or k != "other":
            n_rest = sum(1 for n, c in v.year_totals.items() if c and n not in v.featured)
            rest_label = f"{n_rest} more {plural(n_rest, 'repo')}"  # Names the long tail, matching the list footnotes.
            items.append((col, name(k) if k != "other" else rest_label, fmt(val)))
    # Legend on a five-column grid; it doubles as the per-project totals for the year.
    lcols = 5
    lw = (W - 2 * PAD) / lcols
    for i, (col, a, b) in enumerate(items):
        lx, ly_ = PAD + (i % lcols) * lw, ly + (i // lcols) * 24
        out.append(f'<rect x="{lx:.1f}" y="{ly_ - 9}" width="10" height="10" rx="2" fill="{col}"/>')
        out.append(pair(lx + 15, ly_, fit(a, 12, lw - 30 - text_w(b, 12, "mono")), b, th))
    lrows = (len(items) + lcols - 1) // lcols

    cx0, cy0, ch = PAD + 34, top + 78 + (lrows - 1) * 24, 150
    cw = W - PAD - cx0
    peak = max((sum(wk.values()) for wk in v.weekly), default=0) or 1
    step = nice_step(peak, 3)
    ymax = max(peak * 1.04, math.ceil(peak / step) * step)
    for val in range(0, int(ymax) + 1, int(step)):
        y = cy0 + ch - val / ymax * ch
        out.append(f'<line x1="{cx0}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}" stroke="{th["grid"]}"/>')
        out.append(t(cx0 - 8, y + 4, fmt(val), 12, th["muted"], anchor="end", font="mono"))
    slot = cw / len(v.weeks)
    bw = slot - 3

    def day_x(day):
        return cx0 + ((day - v.week0).days / 7) * slot

    for i, wk in enumerate(v.weeks):
        x = cx0 + i * slot + 1.5
        y = cy0 + ch
        stack = [(entity_color(v, k, th), v.weekly[i][k]) for k in v.featured] + [(th["other"], v.weekly[i]["other"])]
        segs = [(c, n / ymax * ch) for c, n in stack if n > 0]
        partial = wk + dt.timedelta(days=6) > TODAY or wk < SINCE
        op = ' opacity="0.45"' if partial else ""
        out.append(f'<g {anim("rise", 200 + i * 8)}>')
        for n_seg, (col, h) in enumerate(segs):
            last = n_seg == len(segs) - 1
            out.append(bar(x, y - h, bw, h - (0 if last else 1.5), col, r=2 if last else 0, extra=op))
            y -= h
        out.append("</g>")

    # Milestones: dot + label above the plot, dotted guide down to the baseline. Each takes the first of
    # two rows where its label clears the others and no guide line crosses it (row-0 guides pass through
    # row 1); labels flip left of their dot near the right edge. A milestone with no clear row is left
    # out; its release tick still shows under the chart.
    hits = sorted((d, repo, text) for repo, tag, text in MILESTONES
                  for r, tg, d in v.releases if r == repo and tg == tag)
    spans, guides0 = ([], []), []

    def clear(row, x1, x2, mx):
        if any(x1 < b + 8 and a < x2 + 8 for a, b in spans[row]):
            return False
        if row == 0:
            return not any(a - 4 <= mx <= b + 4 for a, b in spans[1])
        return not any(x1 - 4 <= g <= x2 + 4 for g in guides0)

    shown = 0
    for hit, repo, text in hits:
        mx = day_x(hit) + slot / 2
        tw = text_w(text, 11, "mono")
        flip = mx + 8 + tw > W - PAD
        x1, x2 = (mx - 8 - tw, mx + 4) if flip else (mx - 4, mx + 8 + tw)
        row = next((k for k in (0, 1) if clear(k, x1, x2, mx)), None)
        if row is None:
            continue
        spans[row].append((x1, x2))
        if row == 0:
            guides0.append(mx)
        my = cy0 - 30 + row * 15
        j, shown = shown, shown + 1
        out.append(f'<g {anim("fade", 800 + j * 80)}>')
        out.append(f'<line x1="{mx:.1f}" y1="{my + 4:.1f}" x2="{mx:.1f}" y2="{cy0 + ch:.1f}" stroke="{th["text2"]}" '
                   f'stroke-width="1" stroke-dasharray="1 3"/>')
        out.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="3.5" fill="{entity_color(v, repo, th)}" stroke="{th["bg"]}" stroke-width="1.5"/>')
        out.append(t(mx - 8 if flip else mx + 8, my + 4, text, 11, th["text2"], anchor="end" if flip else "start", font="mono"))
        out.append("</g>")
    out.append(f'<line x1="{cx0}" y1="{cy0 + ch}" x2="{W - PAD}" y2="{cy0 + ch}" stroke="{th["border"]}"/>')

    # Releases per week, hanging below the baseline in the same columns, stacked by project.
    # Square-root height so a one-release week stays visible next to a 27-release one.
    ry, rh = cy0 + ch + 4, 18
    rmax = max((sum(c.values()) for c in v.weekly_rel), default=0) or 1
    for i, rel in enumerate(v.weekly_rel):
        n = sum(rel.values())
        if not n:
            continue
        x, total_h = cx0 + i * slot + 1.5, math.sqrt(n / rmax) * rh
        y = ry
        out.append(f'<g {anim("hang", 900 + i * 6)}><title>{v.weeks[i]:%b %-d}: {n} {plural(n, "release")}</title>')
        for repo, c in sorted(rel.items(), key=lambda kv: (kv[0] not in v.featured, -kv[1])):
            h = total_h * c / n
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(.8, h - .8):.1f}" '
                       f'fill="{mix(entity_color(v, repo, th), "#000000", th["tick_ink"])}"/>')
            y += h
        out.append("</g>")

    # The shipping run: the latest stretch of weeks with no two quiet weeks in a row.
    has = [bool(sum(c.values())) for c in v.weekly_rel]
    end = max((i for i, h in enumerate(has) if h), default=None)
    # Month labels; the first carries the year so the axis reads as a span, like the PR chart's.
    my_ = ry + rh + 14
    first_lab = f"{SINCE:%b '%y}"
    out.append(t(cx0, my_, first_lab, 12, th["muted"], font="mono"))
    first_end = cx0 + text_w(first_lab, 12, "mono") + 8
    for i, wk in enumerate(v.weeks):
        mx_ = cx0 + i * slot + 1.5
        if i and wk.day <= 7 and mx_ > first_end and mx_ + text_w("Nov", 12, "mono") <= W - PAD:
            out.append(t(mx_, my_, f"{wk:%b}", 12, th["muted"], font="mono"))
    yb = bottom = my_ + 10
    if end is not None:
        start, quiet = end, 0
        for i in range(end, -1, -1):
            quiet = 0 if has[i] else quiet + 1
            if quiet == 2:
                break
            if has[i]:
                start = i
        n_weeks = len(has) - start
        k = sum(has[start:])
        if n_weeks >= 8:
            bx1, bx2 = cx0 + start * slot + 1.5, W - PAD
            out.append(f'<g {anim("show", 1500)}>'
                       f'<path d="M{bx1:.1f},{yb - 4} V{yb} H{bx2:.1f} V{yb - 4}" fill="none" stroke="{th["text2"]}"/>'
                       f'<text x="{bx1:.1f}" y="{yb + 16}" font-family="{SANS}" font-size="12" fill="{th["text2"]}">'
                       f'Released in<tspan fill="{th["text"]}" font-weight="600"> {k} of {n_weeks} weeks</tspan>'
                       f' since {v.weeks[start]:%b %-d}</text></g>')
            bottom = yb + 16

    # --- Bottom row: when commits land + last 30 days.
    top2 = bottom + 50
    out.append(rule(top2 - 24, th))
    pw = 500
    out.append(label(PAD, top2, "Commit times · IST", th))
    # One hour-of-day profile. The weekday split is nearly flat for most people, so it is a sentence, not a chart.
    punch = v.punch
    total = sum(map(sum, punch)) or 1
    hours = [sum(punch[d][h] for d in range(7)) for h in range(24)]
    days_tot = [sum(row) for row in punch]
    weekend = (days_tot[5] + days_tot[6]) / total * 100
    names_ = "Mondays Tuesdays Wednesdays Thursdays Fridays Saturdays Sundays".split()
    even = min(days_tot) >= 0.6 * max(days_tot)
    week_note = (f"every day of the week · weekends {weekend:.0f}%" if even else
                 f"busiest on {names_[days_tot.index(max(days_tot))]} · weekends {weekend:.0f}%")
    out.append(note(PAD + pw, top2, week_note, th))
    ctop, ch = top2 + 26, 78
    base = ctop + ch
    out.append(f'<line x1="{PAD}" y1="{base + .5}" x2="{PAD + pw}" y2="{base + .5}" stroke="{th["grid"]}"/>')

    # Time of day, in four 6-hour bands; the busiest band is picked out.
    bands = [("Night", 0), ("Morning", 6), ("Afternoon", 12), ("Evening", 18)]
    share = [sum(hours[s:s + 6]) / total * 100 for _, s in bands]
    top_band = share.index(max(share))
    pitch = pw / 24
    hmax = max(hours) or 1
    accent = mix(th["bar"], th["hi"], 0.5)  # Picks out a group of bars without shouting over the year chart.
    for h, n in enumerate(hours):
        hh = n / hmax * ch
        col = accent if h // 6 == top_band else th["bar"]
        out.append(f'<g {anim("rise", 500 + h * 12)}>{bar(PAD + h * pitch + 2, base - hh, pitch - 4, hh, col)}'
                   f'<title>{h:02d}:00 · {fmt(n)} {plural(n, "commit")}</title></g>')
    for i, ((bname, s), pct) in enumerate(zip(bands, share)):
        bx = PAD + s * pitch
        if s:
            out.append(f'<line x1="{bx:.1f}" y1="{base + 4}" x2="{bx:.1f}" y2="{base + 9}" stroke="{th["muted"]}"/>')
        out.append(t(bx + 2, base + 22, f"{s:02d}", 12, th["muted"], font="mono"))
        strong = i == top_band
        out.append(f'<g {anim("show", 900 + i * 60)}>'
                   f'<text x="{bx + 3 * pitch:.1f}" y="{base + 44}" font-family="{SANS}" font-size="12" text-anchor="middle" '
                   f'fill="{th["text"] if strong else th["text2"]}" font-weight="{600 if strong else 400}">{bname}'
                   f'<tspan dx="5" font-family="{MONO}" font-size="12" fill="{th["text2"] if strong else th["muted"]}">'
                   f'{pct:.0f}%</tspan></text></g>')

    x0 = PAD + pw + 44
    out.append(label(x0, top2, "Busiest projects · 30 days", th))
    lo = TODAY - dt.timedelta(days=29)
    month = Counter({n: sum(c for d, c in counts.items() if d >= lo) for n, counts in v.per_repo.items()})
    rows, foot = top_projects(v, month, 5)
    # Rows spread from the label down to the heatmap's last row, so both columns end on one line.
    first_y, last_y = ctop + 6, base + 44
    step_ = min(30, (last_y - first_y) / max(1, len(rows) - (0 if foot else 1)))  # Capped: a quiet month stays top-aligned.
    out.extend(ranked_rows(v, rows, foot, x0, first_y, W - PAD - x0, th, label_w=104, step=step_, delay=500))

    h = int(base + 72)
    return card(h, th, "Build activity", f"{SINCE:%b %-d, %Y} – {TODAY:%b %-d, %Y}",
                "\n".join(out), "Atulya Rai — build activity",
                f"{fmt(v.contrib_total)} {plural(v.contrib_total, 'contribution')} and "
                f"{len(v.releases)} {plural(len(v.releases), 'release')} over the last year; "
                "weekly commits by project and releases per week, commits by hour of day, and the most active projects of the last 30 days.")


def render_stack(stats, v, th):
    out = []
    # --- Latest releases: newest release per project, newest first. Dated, so older ones read as older.
    top = 96
    out.append(label(PAD, top, "Latest releases", th))
    recent = sorted(v.latest.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    cw = (W - 2 * PAD) / max(1, len(recent))
    for i, (repo, (tag, day)) in enumerate(recent):
        x = PAD + i * cw + (0 if i == 0 else 18)
        out.append(f'<g {anim("fade", 100 + i * 70)}>')
        if i:
            out.append(f'<line x1="{PAD + i * cw:.1f}" y1="{top + 18}" x2="{PAD + i * cw:.1f}" y2="{top + 74}" stroke="{th["grid"]}"/>')
        out.append(f'<circle cx="{x + 4}" cy="{top + 28}" r="4" fill="{entity_color(v, repo, th)}"/>')
        colw = cw - (0 if i == 0 else 18) - 12
        out.append(t(x + 14, top + 32, fit(name(repo), 14, colw - 14), 14, th["text"], weight=600))
        # "mobile-v3.0.34" -> "v3.0.34" with the stream as a muted suffix; "web-v1.0.0-rc.1" keeps its suffix.
        m = re.match(r"^(.+?)-(v\d.*)$", tag)
        stream, ver = (m[1], m[2]) if m else ("", tag)
        ver = fit(ver, 12, colw - (text_w(stream, 11, "mono") + 6 if stream else 0), "mono")
        out.append(f'<text x="{x}" y="{top + 54}" font-family="{MONO}" font-size="12" fill="{th["text"]}">{escape(ver)}'
                   + (f'<tspan dx="6" fill="{th["muted"]}" font-size="11">{escape(stream)}</tspan>' if stream else "")
                   + "</text>")
        out.append(t(x, top + 72, f"{day:%b %-d} · {v.release_counts[repo]} {plural(v.release_counts[repo], 'release')}", 12, th["muted"], font="mono"))
        out.append("</g>")

    # --- Languages.
    top += 124
    out.append(rule(top - 24, th))
    langs = stats["languages"]
    colors = stats.get("languageColors", {})
    total = sum(b for _, b in langs) or 1
    shown = langs[:8]
    rest = total - sum(b for _, b in shown)
    parts = shown + ([("Other", rest)] if rest else [])

    def lang_col(n):
        # A language without a colour gets a lighter grey than the "Other" bucket, so the two never read as one.
        return colors.get(n) or LANG_COLORS.get(n) or th["text2"] if n != "Other" else th["other"]

    out.append(label(PAD, top, "Languages", th))
    out.append(note(W - PAD, top, f"{len(langs)} {plural(len(langs), 'language')} · all-time · by bytes", th))
    x, y, bw = PAD, top + 16, W - 2 * PAD
    out.append(f'<clipPath id="lc"><rect x="{PAD}" y="{y}" width="{bw}" height="10" rx="5"/></clipPath><g clip-path="url(#lc)"><g {anim("grow", 500)}>')
    for n, b in parts:
        sw = b / total * bw
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{max(0, sw - 2):.1f}" height="10" fill="{lang_col(n)}"/>')
        x += sw
    out.append("</g></g>")
    cols = 5
    lw = bw / cols
    out.append(f'<g {anim("show", 600)}>')
    for i, (n, b) in enumerate(parts):
        lx = PAD + (i % cols) * lw
        ly = y + 40 + (i // cols) * 22
        out.append(f'<circle cx="{lx + 5}" cy="{ly - 4}" r="5" fill="{lang_col(n)}"/>')
        out.append(pair(lx + 16, ly, fit(n, 12, lw - 70), f"{b / total * 100:.1f}%", th))
    out.append("</g>")

    # --- Pull requests. Per-project commit totals live in the activity card's legend, so not here.
    top2 = y + 100
    out.append(rule(top2 - 24, th))
    sw_ = 196  # Figures column on the right; fits "47 of 53 weeks with PRs".
    pw = W - 2 * PAD - sw_ - 40
    x0 = PAD
    out.append(label(x0, top2, "Pull requests · 12 months", th))
    out.append(note(x0 + pw, top2, "per week", th))
    pr = stats["prRecent"]
    pmax = max(n for _, n in pr) or 1
    ph = 96
    base = top2 + 22 + ph
    slot = pw / len(pr)
    gap_ = 1.5 if slot < 9 else 3
    for i, (wk, n) in enumerate(pr):
        out.append(f'<g {anim("rise", 400 + i * 8)}>')
        out.append(bar(x0 + i * slot + gap_ / 2, base - n / pmax * ph, slot - gap_, n / pmax * ph,
                       th["hi"] if n == pmax else th["bar"], r=1.5))
        out.append("</g>")
    pk = max(range(len(pr)), key=lambda i: pr[i][1])
    pk_txt = f"{pr[pk][1]} · week of {dt.date.fromisoformat(pr[pk][0]):%b %-d}"
    # Label beside the peak bar, flipped to its left when it would run off the panel.
    px_ = x0 + (pk + 1) * slot + 4
    anchor = "start"
    if px_ + text_w(pk_txt, 12, "mono") > x0 + pw:
        px_, anchor = x0 + pk * slot - 4, "end"
    out.append(f'<g {anim("show", 400 + pk * 8 + 600)}>{t(px_, base - ph + 9, pk_txt, 12, th["text2"], anchor=anchor, font="mono")}</g>')
    out.append(f'<line x1="{x0}" y1="{base}" x2="{x0 + pw}" y2="{base}" stroke="{th["border"]}"/>')
    out.append(note(x0, base + 18, f"{SINCE:%b '%y}", th, anchor="start"))
    out.append(note(x0 + pw, base + 18, "this week", th))

    # Figures stacked in a column, divided from the chart like the stat row's columns.
    fx = W - PAD - sw_
    out.append(f'<line x1="{fx - 20}" y1="{top2 + 22}" x2="{fx - 20}" y2="{base}" stroke="{th["grid"]}"/>')
    figs = [("opened", fmt(stats["prs"])), ("merged", f"{stats['prMergeRatio']:.0f}%"),
            ("weeks with PRs", f"{stats['prActiveWeeks']} of {len(pr)}")]
    fstep = (ph - 8) / (len(figs) - 1)
    for i, (lab, val) in enumerate(figs):
        fy = top2 + 40 + i * fstep
        out.append(f'<g {anim("fade", 1000 + i * 70)}><text x="{fx}" y="{fy:.1f}" font-family="{SANS}" font-size="22" '
                   f'font-weight="600" fill="{th["text"]}" style="font-variant-numeric: tabular-nums">{val}'
                   f'<tspan font-size="12" font-weight="400" fill="{th["text2"]}" dx="7">{lab}</tspan></text></g>')

    h = int(base + 46)
    return card(h, th, "Shipping & stack", "",
                "\n".join(out), "Shipping and stack",
                "Latest release per project, language mix by bytes, and weekly pull request volume.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    ASSETS.mkdir(exist_ok=True)
    stats, repo_daily = collect()
    (ASSETS / "profile-telemetry.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    view = card_data(stats, repo_daily)
    for mode, th in THEMES.items():
        for card_name, svg in (("activity", render_activity(view, th)), ("stack", render_stack(stats, view, th))):
            path = ASSETS / f"profile-{card_name}-{mode}.svg"
            path.write_text(svg, encoding="utf-8")
            print(f"Generated {path}")


if __name__ == "__main__":
    main()
