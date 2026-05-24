#!/usr/bin/env python3
"""Generate Snehit's profile telemetry SVGs from GitHub data.

Pulls repos (public + private, with archived/forks excluded), default-branch
commits, and authored PRs via the GitHub GraphQL/REST API (through `gh`),
derives a battery of stats, and renders two static SVG dashboards plus a
JSON snapshot.
"""
import datetime as dt
import json
import math
import os
import statistics
import subprocess
import sys
import xml.sax.saxutils as xml
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
USER = "Snehit70"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))  # IST for hour-of-day.
TODAY = dt.datetime.now(dt.timezone.utc).date()
SINCE = TODAY - dt.timedelta(days=365)

CATEGORY_KEYWORDS = {
    "AI / Voice": ("voice", "transcribe", "stt", "ai", "agent", "ralphy"),
    "Linux / CLI": ("hypr", "systemd", "tui", "cli", "tmux", "neovim", "dot", "lapstat"),
    "Web Apps": ("web", "app", "next", "vue", "react", "vercel", "aceon", "field", "fate", "saksham"),
    "Mobile": ("android", "kotlin", "pomo", "mobile"),
    "Infra": ("docker", "gateway", "proxy", "service", "monitor"),
}
IGNORED_LANGUAGES = {"Jupyter Notebook"}
VERBOSE = os.environ.get("VERBOSE") == "1"

# --- Design tokens --------------------------------------------------------
BG = "#06101c"
BG2 = "#040912"
DOT = "#16263f"
HAIRLINE = "#1a2942"
HAIRLINE_BRIGHT = "#243652"
TEXT_HI = "#f4f7fb"
TEXT = "#c8d2e2"
TEXT_MUTED = "#7286a0"
TEXT_DIM = "#4a5b75"
# Editorial accent set: phosphor lime as primary, amber + cyan + magenta + violet as supports.
LIME = "#a3e635"
AMBER = "#fbbf24"
CYAN = "#67e8f9"
MAGENTA = "#f472b6"
VIOLET = "#c084fc"
RED = "#fb7185"
PALETTE = [LIME, CYAN, AMBER, VIOLET, MAGENTA, "#5eead4", "#fde68a", "#94a3b8"]

# GitHub-aligned language colors.
LANG_COLORS = {
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "Python": "#3572A5",
    "Vue": "#41b883",
    "CSS": "#a855f7",
    "HTML": "#e34c26",
    "Kotlin": "#A97BFF",
    "Go": "#00ADD8",
    "Shell": "#89e051",
    "Dockerfile": "#384d54",
    "Mako": "#7e858d",
    "Other": "#475569",
}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


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
            })
        if not conn["pageInfo"]["hasNextPage"]:
            return repos
        cursor = conn["pageInfo"]["endCursor"]


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
    query = f"author:{USER} type:pr created:>={SINCE.isoformat()}"
    items = []
    page = 1
    while True:
        data = gh_json(["search/issues", "-X", "GET", "-f", f"q={query}", "-f", "per_page=100", "-f", f"page={page}"])
        items.extend(data.get("items", []))
        if len(items) >= data.get("total_count", 0) or not data.get("items"):
            return items
        page += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def esc(value):
    return xml.escape(str(value), {'"': "&quot;"})


def pct(value, digits=0):
    return f"{value:.{digits}f}%"


def fmt_num(value):
    return f"{int(value):,}"


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
    additions = 0
    deletions = 0
    language_bytes = Counter()
    category_commits = Counter()

    for repo in repos:
        for edge in repo["languages"]:
            lang = edge["node"]["name"]
            if lang not in IGNORED_LANGUAGES:
                language_bytes[lang] += edge["size"]
        commits = fetch_commits(repo)
        category = repo_category(repo)
        for commit in commits:
            ts = dt.datetime.fromisoformat(commit["committedDate"].replace("Z", "+00:00"))
            local_ts = ts.astimezone(LOCAL_TZ)
            day = local_ts.date()
            daily[day] += 1
            hour_dist[local_ts.hour] += 1
            repo_daily[repo["name"]][day] += 1
            category_commits[category] += 1
            additions += commit.get("additions") or 0
            deletions += commit.get("deletions") or 0

    prs = fetch_prs()
    pr_weekly = Counter()
    merged = 0
    for pr in prs:
        created = dt.datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")).date()
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
    # Trailing 26-week PR series for the bar chart.
    week_cursor = iso_week_start(TODAY)
    pr_recent = []
    for i in range(26):
        wk = week_cursor - dt.timedelta(weeks=25 - i)
        pr_recent.append((wk.isoformat(), pr_weekly.get(wk, 0)))

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

    return {
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
    }


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def shell(W, H, defs_extra=""):
    """Outer container + subtle dotted background; no inner panels."""
    return f"""<rect width="{W}" height="{H}" rx="10" fill="url(#bgGrad)"/>
<rect width="{W}" height="{H}" rx="10" fill="url(#dots)"/>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="9.5" fill="none" stroke="{HAIRLINE_BRIGHT}"/>
{defs_extra}"""


def defs():
    return f"""<defs>
  <linearGradient id="bgGrad" x1="0" x2="1" y1="0" y2="1">
    <stop offset="0" stop-color="{BG}"/>
    <stop offset="1" stop-color="{BG2}"/>
  </linearGradient>
  <pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse">
    <circle cx="1" cy="1" r="0.9" fill="{DOT}" opacity="0.55"/>
  </pattern>
  <linearGradient id="limeFade" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0" stop-color="{LIME}" stop-opacity="0.55"/>
    <stop offset="1" stop-color="{LIME}" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="cyanFade" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0" stop-color="{CYAN}" stop-opacity="0.45"/>
    <stop offset="1" stop-color="{CYAN}" stop-opacity="0"/>
  </linearGradient>
</defs>"""


def section_label(x, y, text, tag=None, width=None):
    """Eyebrow heading + optional right-aligned tag, with a hairline."""
    out = [f'<text x="{x}" y="{y}" fill="{TEXT_HI}" font-size="11" font-weight="700" letter-spacing="2">{esc(text.upper())}</text>']
    if tag:
        out.append(f'<text x="{x + (width or 0)}" y="{y}" fill="{TEXT_DIM}" font-size="10.5" letter-spacing="1" text-anchor="end">{esc(tag.upper())}</text>')
    return "\n".join(out)


def hairline(x1, y, x2, color=HAIRLINE):
    return f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{color}"/>'


def number_block(x, y, label, value, sub=None, accent=TEXT_HI, label_color=TEXT_MUTED, big=34):
    parts = [f'<text x="{x}" y="{y}" fill="{label_color}" font-size="10" font-weight="700" letter-spacing="1.5">{esc(label.upper())}</text>']
    parts.append(f'<text x="{x}" y="{y + big - 4}" fill="{accent}" font-size="{big}" font-weight="800" letter-spacing="-1">{esc(value)}</text>')
    if sub:
        parts.append(f'<text x="{x}" y="{y + big + 14}" fill="{TEXT_DIM}" font-size="11">{esc(sub)}</text>')
    return "\n".join(parts)


# --- Squarified treemap ---------------------------------------------------

def squarify(items, x, y, w, h):
    """Squarified treemap.

    items: list of (label, value) sorted descending by value.
    Returns: list of (label, value, x, y, w, h) tuples (one per item).
    """
    sizes = [v for _, v in items]
    total = sum(sizes)
    if total == 0 or not sizes:
        return []
    scale = (w * h) / total
    scaled = [s * scale for s in sizes]
    results = [None] * len(scaled)
    pending = list(range(len(scaled)))  # original indices

    def worst(row_areas, side):
        s = sum(row_areas)
        return max(
            (side * side * max(row_areas)) / (s * s),
            (s * s) / (side * side * min(row_areas)),
        )

    def place(row_idx, x, y, w, h):
        # Lay row along the long edge; fill the short edge with the row.
        row_areas = [scaled[i] for i in row_idx]
        s = sum(row_areas)
        if w >= h:
            row_w = s / h
            cy = y
            for i, area in zip(row_idx, row_areas):
                rh = area / row_w if row_w else 0
                results[i] = (x, cy, row_w, rh)
                cy += rh
            return x + row_w, y, w - row_w, h
        else:
            row_h = s / w
            cx = x
            for i, area in zip(row_idx, row_areas):
                rw = area / row_h if row_h else 0
                results[i] = (cx, y, rw, row_h)
                cx += rw
            return x, y + row_h, w, h - row_h

    def go(remaining, row, x, y, w, h):
        if not remaining:
            if row:
                place(row, x, y, w, h)
            return
        side = min(w, h)
        if not row:
            go(remaining[1:], [remaining[0]], x, y, w, h)
            return
        cur_areas = [scaled[i] for i in row]
        new_areas = cur_areas + [scaled[remaining[0]]]
        if worst(new_areas, side) <= worst(cur_areas, side):
            go(remaining[1:], row + [remaining[0]], x, y, w, h)
        else:
            nx, ny, nw, nh = place(row, x, y, w, h)
            go(remaining, [], nx, ny, nw, nh)

    go(pending, [], x, y, w, h)
    out = []
    for i, (label, value) in enumerate(items):
        rx, ry, rw, rh = results[i] or (x, y, 0, 0)
        out.append((label, value, rx, ry, rw, rh))
    return out

# Telemetry SVG — flat editorial layout, no heatmap
# ---------------------------------------------------------------------------

def render_telemetry(stats):
    W, H = 1100, 730
    pad = 44

    daily_values = list(stats["daily"].values())
    monthly = stats["monthly"]
    hours = stats["hourDistribution"]

    # ------------ Hero band: streak + monthly area chart -----------------
    hero_y = 96
    streak_x = pad

    # Monthly trajectory area chart (right side of hero).
    chart_x = 470
    chart_y = hero_y + 18
    chart_w = W - chart_x - pad
    chart_h = 150
    max_m = max((v for _, v in monthly), default=1) or 1
    points = []
    for i, (_, v) in enumerate(monthly):
        px = chart_x + (i / max(1, len(monthly) - 1)) * chart_w
        py = chart_y + chart_h - (v / max_m) * chart_h
        points.append((px, py))
    poly = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    area = f"{chart_x},{chart_y + chart_h} {poly} {chart_x + chart_w},{chart_y + chart_h}"
    # Month axis ticks (every other month).
    axis = []
    for i, (mk, _) in enumerate(monthly):
        if i % 2 != 0:
            continue
        px = chart_x + (i / max(1, len(monthly) - 1)) * chart_w
        m = MONTH_NAMES[int(mk.split("-")[1]) - 1]
        axis.append(f'<text x="{px:.1f}" y="{chart_y + chart_h + 16}" fill="{TEXT_DIM}" font-size="10" text-anchor="middle">{m}</text>')
    # Annotate peak month and current month.
    peak_idx = max(range(len(monthly)), key=lambda i: monthly[i][1])
    cur_idx = len(monthly) - 1
    annots = []
    for idx, color, label in [(peak_idx, AMBER, f"peak · {monthly[peak_idx][1]}"), (cur_idx, LIME, f"now · {monthly[cur_idx][1]}")]:
        if monthly[idx][1] == 0:
            continue
        px, py = points[idx]
        annots.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{color}" stroke="{BG}" stroke-width="2"/>')
        text_anchor = "end" if idx == cur_idx else "start"
        text_dx = -6 if idx == cur_idx else 6
        annots.append(f'<text x="{px + text_dx:.1f}" y="{py - 8:.1f}" fill="{color}" font-size="10" font-weight="700" text-anchor="{text_anchor}">{label}</text>')

    # Y-axis baseline + max gridline.
    grid = [
        f'<line x1="{chart_x}" y1="{chart_y + chart_h}" x2="{chart_x + chart_w}" y2="{chart_y + chart_h}" stroke="{HAIRLINE}"/>',
        f'<line x1="{chart_x}" y1="{chart_y}" x2="{chart_x + chart_w}" y2="{chart_y}" stroke="{HAIRLINE}" stroke-dasharray="2 4"/>',
        f'<text x="{chart_x - 6}" y="{chart_y + 4}" fill="{TEXT_DIM}" font-size="9" text-anchor="end">{int(max_m)}</text>',
        f'<text x="{chart_x - 6}" y="{chart_y + chart_h + 3}" fill="{TEXT_DIM}" font-size="9" text-anchor="end">0</text>',
    ]

    velocity_trend = stats["velocityTrendPct"]
    trend_color = LIME if velocity_trend >= 0 else RED
    trend_sign = "+" if velocity_trend >= 0 else ""
    trend_arrow = "▲" if velocity_trend >= 0 else "▼"

    # ---------- Number strip --------------------------------------------
    strip_y = 320
    fields = [
        ("all commits", fmt_num(stats["commitTotal"]), f'across {stats["reposAnalyzed"]} repos', LIME),
        ("30d velocity", fmt_num(stats["velocity30d"]), f'{trend_arrow} {trend_sign}{velocity_trend:.0f}% vs prior 30', trend_color),
        ("active days", pct(stats["activeDaysPct"]), f'mean gap {stats["meanGapDays"]:.1f}d', CYAN),
        ("biggest day", fmt_num(stats["biggestDay"]["commits"]), f'on {stats["biggestDay"]["date"]}', AMBER),
        ("burst days", fmt_num(stats["burstDays"]), f'≥ {int(stats["burstThreshold"])} commits', VIOLET),
        ("pull requests", fmt_num(stats["prs"]), f'{stats["prMergeRatio"]:.0f}% merged · {stats["prActiveWeeks"]} active wks', MAGENTA),
    ]
    strip_parts = []
    col_w = (W - pad * 2) / len(fields)
    for i, (label, value, sub, color) in enumerate(fields):
        cx = pad + i * col_w
        strip_parts.append(number_block(cx, strip_y, label, value, sub, accent=color, big=24))
        if i > 0:
            strip_parts.append(f'<line x1="{cx - 10}" y1="{strip_y - 4}" x2="{cx - 10}" y2="{strip_y + 50}" stroke="{HAIRLINE}"/>')

    # ---------- Weekday pulse (left) + Hour-of-day (right) --------------
    sec_y = 432
    sec_label_y = sec_y
    # Weekday pulse
    wd_x = pad
    wd_w = 470
    wd_means = stats["weekdayMean"]
    wd_max = max(wd_means) or 1
    wd_bar_w = 42
    wd_bar_gap = 18
    wd_y0 = sec_y + 152
    wd_bar_h_max = 110
    wd_bars = []
    for i, m in enumerate(wd_means):
        bh = (m / wd_max) * wd_bar_h_max
        bx = wd_x + 8 + i * (wd_bar_w + wd_bar_gap)
        is_best = i == stats["bestWeekday"]
        is_quiet = i == stats["quietWeekday"]
        color = AMBER if is_best else (TEXT_DIM if is_quiet else LIME)
        opacity = 1.0 if is_best else (0.4 if is_quiet else 0.78)
        wd_bars.append(f'<rect x="{bx}" y="{wd_y0 - bh:.1f}" width="{wd_bar_w}" height="{bh:.1f}" rx="2" fill="{color}" opacity="{opacity}"/>')
        wd_bars.append(f'<text x="{bx + wd_bar_w / 2:.1f}" y="{wd_y0 + 16}" fill="{TEXT_MUTED}" font-size="10.5" text-anchor="middle">{WEEKDAY_NAMES[i]}</text>')
        wd_bars.append(f'<text x="{bx + wd_bar_w / 2:.1f}" y="{wd_y0 - bh - 6:.1f}" fill="{TEXT}" font-size="10" text-anchor="middle">{m:.1f}</text>')

    # Hour-of-day (24 hours, IST).
    hr_x = 580
    hr_w = W - hr_x - pad
    hr_max = max(hours) or 1
    hr_y0 = sec_y + 152
    hr_bar_h_max = 110
    hr_bar_w = (hr_w - 23 * 2) / 24
    hr_bars = []
    for h, v in enumerate(hours):
        bh = (v / hr_max) * hr_bar_h_max if v else 1.5
        bx = hr_x + h * (hr_bar_w + 2)
        is_peak = h == stats["peakHour"] and v > 0
        if 0 <= h < 6:
            color = VIOLET
        elif 6 <= h < 12:
            color = CYAN
        elif 12 <= h < 18:
            color = LIME
        else:
            color = AMBER
        if is_peak:
            color = TEXT_HI
        hr_bars.append(f'<rect x="{bx:.1f}" y="{hr_y0 - bh:.1f}" width="{hr_bar_w:.1f}" height="{bh:.1f}" rx="1" fill="{color}" opacity="{0.95 if is_peak else 0.7}"/>')
    # Hour axis ticks at 0, 6, 12, 18, 24
    for h in (0, 6, 12, 18, 23):
        bx = hr_x + h * (hr_bar_w + 2)
        label = f"{h:02d}" if h != 23 else "23"
        hr_bars.append(f'<text x="{bx:.1f}" y="{hr_y0 + 16}" fill="{TEXT_DIM}" font-size="10" text-anchor="middle">{label}</text>')
    hr_bars.append(f'<line x1="{hr_x}" y1="{hr_y0}" x2="{hr_x + hr_w}" y2="{hr_y0}" stroke="{HAIRLINE}"/>')

    # Section labels.
    sec_labels = [
        section_label(wd_x, sec_label_y, "Weekday pulse", tag=f'loudest {WEEKDAY_NAMES[stats["bestWeekday"]]} · quietest {WEEKDAY_NAMES[stats["quietWeekday"]]}', width=wd_w),
        section_label(hr_x, sec_label_y, "Hour of day (IST)", tag=f'peak {stats["peakHour"]:02d}:00 · {sum(hours)} commits', width=hr_w),
        hairline(wd_x, sec_label_y + 12, wd_x + wd_w),
        hairline(hr_x, sec_label_y + 12, hr_x + hr_w),
    ]

    # ---------- Top projects 7d / 30d (bottom band) ---------------------
    tp_y = 632
    tp_labels = [
        section_label(pad, tp_y, "Top projects · 7d", width=470),
        section_label(580, tp_y, "Top projects · 30d", width=476),
        hairline(pad, tp_y + 12, pad + 470),
        hairline(580, tp_y + 12, 580 + 476),
    ]
    def proj_inline(items, x, y, w, color):
        if not items:
            return f'<text x="{x}" y="{y + 22}" fill="{TEXT_DIM}" font-size="12">no commits</text>'
        max_v = max(v for _, v in items[:3])
        out = []
        for i, (name, value) in enumerate(items[:3]):
            ry = y + 24 + i * 18
            bw = max(2, (value / max_v) * (w - 230))
            out.append(f'<text x="{x}" y="{ry}" fill="{TEXT}" font-size="12" font-weight="600">{esc(name)}</text>')
            out.append(f'<rect x="{x + 150}" y="{ry - 8}" width="{w - 230}" height="6" rx="1" fill="{HAIRLINE}"/>')
            out.append(f'<rect x="{x + 150}" y="{ry - 8}" width="{bw:.1f}" height="6" rx="1" fill="{color}"/>')
            out.append(f'<text x="{x + w - 30}" y="{ry}" fill="{TEXT_MUTED}" font-size="11" text-anchor="end">{value}</text>')
        return "\n".join(out)

    return f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="ttl desc">
  <title id="ttl">Snehit build telemetry</title>
  <desc id="desc">Streak hero, twelve-month commit trajectory, weekday pulse, hour-of-day distribution, and top projects.</desc>
  <style>text {{ font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, monospace; }} .sans {{ font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; }}</style>
  {defs()}
  {shell(W, H)}

  <!-- Header -->
  <text x="{pad}" y="56" fill="{TEXT_HI}" font-size="26" font-weight="800" letter-spacing="-0.5" class="sans">build telemetry</text>
  <text x="{pad}" y="76" fill="{TEXT_MUTED}" font-size="11" letter-spacing="1">SNEHIT · TRAILING {stats["windowDays"]} DAYS · {stats["reposPublic"]} PUBLIC + {stats["reposPrivate"]} PRIVATE · {esc(stats["generated"]).upper()}</text>
  {hairline(pad, 86, W - pad, HAIRLINE_BRIGHT)}

  <!-- Hero: streak + trajectory -->
  <g>
    <text x="{streak_x}" y="{hero_y + 12}" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">CURRENT STREAK</text>
    <text x="{streak_x}" y="{hero_y + 105}" fill="{LIME}" font-size="118" font-weight="900" letter-spacing="-6" class="sans">{stats["currentStreak"]}<tspan font-size="40" fill="{TEXT_HI}" font-weight="800">d</tspan></text>
    <text x="{streak_x}" y="{hero_y + 138}" fill="{TEXT}" font-size="13">longest run {stats["longestStreak"]}d  ·  longest quiet stretch {stats["longestGap"]}d</text>
    <text x="{streak_x}" y="{hero_y + 168}" fill="{TEXT_DIM}" font-size="11">avg commit size {stats["avgCommitSize"]:.0f} lines · code growth {stats["additionsPct"]:.0f}% additions</text>
  </g>
  <g>
    <text x="{chart_x}" y="{hero_y + 12}" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">12-MONTH TRAJECTORY</text>
    {chr(10).join(grid)}
    <polygon points="{area}" fill="url(#limeFade)"/>
    <polyline points="{poly}" fill="none" stroke="{LIME}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
    {chr(10).join(annots)}
    {chr(10).join(axis)}
  </g>

  <!-- Hairline + Number strip -->
  {hairline(pad, 300, W - pad)}
  <text x="{pad}" y="312" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">VITALS</text>
  {chr(10).join(strip_parts)}

  <!-- Section row -->
  {hairline(pad, 412, W - pad)}
  {chr(10).join(sec_labels)}
  {chr(10).join(wd_bars)}
  {chr(10).join(hr_bars)}

  <!-- Bottom: Top projects -->
  {chr(10).join(tp_labels)}
  {proj_inline(stats["topWeek"], pad, tp_y, 470, LIME)}
  {proj_inline(stats["topMonth"], 580, tp_y, 476, CYAN)}
</svg>
"""


# ---------------------------------------------------------------------------
# Distribution SVG — treemap-led
# ---------------------------------------------------------------------------

def render_distribution(stats):
    W, H = 1100, 760
    pad = 44

    # --- Languages → treemap ---------------------------------------------
    langs = stats["languages"][:]
    total_lang = sum(v for _, v in langs) or 1
    top_langs = langs[:8]
    other = total_lang - sum(v for _, v in top_langs)
    if other > 0:
        top_langs.append(("Other", other))

    tm_x, tm_y = pad, 120
    tm_w, tm_h = W - pad * 2, 280
    rects = squarify(top_langs, tm_x, tm_y, tm_w, tm_h)

    tm_parts = []
    for label, value, rx, ry, rw, rh in rects:
        color = LANG_COLORS.get(label, PALETTE[0])
        pct_v = value / total_lang * 100
        # Outer cell (small inset gap for tile separation).
        gap = 2
        cell_x, cell_y, cell_w, cell_h = rx + gap, ry + gap, max(0, rw - gap * 2), max(0, rh - gap * 2)
        tm_parts.append(f'<rect x="{cell_x:.2f}" y="{cell_y:.2f}" width="{cell_w:.2f}" height="{cell_h:.2f}" rx="4" fill="{color}" opacity="0.92"><title>{esc(label)} · {pct_v:.1f}%</title></rect>')
        if cell_w > 60 and cell_h > 32:
            # Choose text color based on cell brightness (approx).
            text_color = "#0b1320" if color in ("#f1e05a", "#fbbf24", "#fde68a", "#a3e635", "#5eead4") else "#f8fafc"
            tm_parts.append(f'<text x="{cell_x + 10:.2f}" y="{cell_y + 22:.2f}" fill="{text_color}" font-size="14" font-weight="800" class="sans">{esc(label)}</text>')
            tm_parts.append(f'<text x="{cell_x + 10:.2f}" y="{cell_y + 38:.2f}" fill="{text_color}" font-size="11" opacity="0.85">{pct_v:.1f}%</text>')
        elif cell_w > 36 and cell_h > 18:
            text_color = "#0b1320" if color in ("#f1e05a", "#fbbf24", "#fde68a", "#a3e635", "#5eead4") else "#f8fafc"
            tm_parts.append(f'<text x="{cell_x + 6:.2f}" y="{cell_y + 14:.2f}" fill="{text_color}" font-size="10" font-weight="700">{esc(label)}</text>')

    # --- Categories: horizontal segmented bar ---------------------------
    cats = stats["categories"][:6]
    total_cat = sum(v for _, v in cats) or 1
    cat_y = 460
    seg_x = pad
    seg_w = W - pad * 2
    seg_h = 14
    cat_segs = []
    cx = seg_x
    for idx, (name, value) in enumerate(cats):
        sw = max(2, (value / total_cat) * seg_w)
        color = PALETTE[idx % len(PALETTE)]
        cat_segs.append(f'<rect x="{cx:.2f}" y="{cat_y + 26}" width="{sw - 2:.2f}" height="{seg_h}" rx="3" fill="{color}"><title>{esc(name)} · {value}</title></rect>')
        cx += sw
    # Category legend grid
    cat_legend = []
    for idx, (name, value) in enumerate(cats):
        col = idx % 3
        row = idx // 3
        lx = pad + col * 340
        ly = cat_y + 70 + row * 26
        color = PALETTE[idx % len(PALETTE)]
        share = value / total_cat * 100
        cat_legend.append(f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="{color}"/>')
        cat_legend.append(f'<text x="{lx + 18}" y="{ly}" fill="{TEXT}" font-size="12">{esc(name)}</text>')
        cat_legend.append(f'<text x="{lx + 320}" y="{ly}" fill="{TEXT_MUTED}" font-size="11" text-anchor="end">{value}  ·  {share:.0f}%</text>')

    # --- Top projects + PR rhythm side by side --------------------------
    bot_y = 580
    proj_w = 530
    pr_x = pad + proj_w + 26
    pr_w = W - pad - pr_x

    yearly = stats["topYear"][:8]
    max_year = max((v for _, v in yearly), default=1)
    proj_rows = []
    for idx, (name, value) in enumerate(yearly):
        ry = bot_y + 36 + idx * 18
        bw = (value / max_year) * (proj_w - 230)
        rank = f'{idx + 1:>2}'
        color = PALETTE[idx % len(PALETTE)]
        proj_rows.append(f'<text x="{pad}" y="{ry}" fill="{TEXT_DIM}" font-size="11" font-weight="700">{rank}</text>')
        proj_rows.append(f'<text x="{pad + 26}" y="{ry}" fill="{TEXT}" font-size="12">{esc(name)}</text>')
        proj_rows.append(f'<rect x="{pad + 170}" y="{ry - 8}" width="{proj_w - 230:.1f}" height="6" rx="1" fill="{HAIRLINE}"/>')
        proj_rows.append(f'<rect x="{pad + 170}" y="{ry - 8}" width="{bw:.1f}" height="6" rx="1" fill="{color}"/>')
        proj_rows.append(f'<text x="{pad + proj_w - 30}" y="{ry}" fill="{TEXT_MUTED}" font-size="11" text-anchor="end">{value}</text>')

    # PR rhythm: keep 26w bars + add more stats below.
    pr_recent = stats["prRecent"]
    pr_vals = [v for _, v in pr_recent]
    pr_max = max(pr_vals) or 1
    pr_chart_x = pr_x
    pr_chart_y = bot_y + 36
    pr_chart_w = pr_w
    pr_chart_h = 80
    pr_bar_w = (pr_chart_w - (len(pr_vals) - 1) * 3) / len(pr_vals)
    pr_bars = []
    p90 = percentile(pr_vals, 90)
    for i, v in enumerate(pr_vals):
        bh = (v / pr_max) * pr_chart_h if v else 0
        bx = pr_chart_x + i * (pr_bar_w + 3)
        by = pr_chart_y + pr_chart_h - bh
        if not v:
            color = "#1c2940"
            by = pr_chart_y + pr_chart_h - 2
            bh = 2
        elif v >= max(1, p90):
            color = MAGENTA
        else:
            color = VIOLET
        pr_bars.append(f'<rect x="{bx:.2f}" y="{by:.2f}" width="{pr_bar_w:.2f}" height="{bh:.2f}" rx="1" fill="{color}"/>')
    first_label = dt.date.fromisoformat(pr_recent[0][0]).strftime("%b %d")
    last_label = dt.date.fromisoformat(pr_recent[-1][0]).strftime("%b %d")

    pr_trend = stats["prTrendPct"]
    pr_trend_color = LIME if pr_trend >= 0 else RED
    pr_trend_arrow = "▲" if pr_trend >= 0 else "▼"
    pr_trend_sign = "+" if pr_trend >= 0 else ""

    pr_stats_y = pr_chart_y + pr_chart_h + 36
    pr_stat_blocks = [
        number_block(pr_x, pr_stats_y, "Merge ratio", f'{stats["prMergeRatio"]:.0f}%', f'{stats["mergedPrs"]}/{stats["prs"]} merged', accent=LIME, big=22),
        number_block(pr_x + 130, pr_stats_y, "Active weeks", str(stats["prActiveWeeks"]), f'of 53 ({stats["prActiveWeeks"]/53*100:.0f}%)', accent=CYAN, big=22),
        number_block(pr_x + 260, pr_stats_y, "p95 / week", f'{stats["prWeeklyP95"]:.0f}', f'max {stats["prWeeklyMax"]}/wk', accent=AMBER, big=22),
        number_block(pr_x + 390, pr_stats_y, "4w trend", f'{pr_trend_sign}{pr_trend:.0f}%', f'{pr_trend_arrow} {stats["prThis4w"]} vs prior {stats["prPrev4w"]}', accent=pr_trend_color, big=22),
    ]

    # --- Composition signals strip --------------------------------------
    sig_y = 740 - 26
    focus_label = "focused" if stats["focusTop3Pct"] >= 60 else ("balanced" if stats["focusTop3Pct"] >= 40 else "scattered")

    return f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="ttl desc">
  <title id="ttl">Snehit codebase composition</title>
  <desc id="desc">Language treemap, work category mix, top projects, PR rhythm with extended statistics.</desc>
  <style>text {{ font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, monospace; }} .sans {{ font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; }}</style>
  {defs()}
  {shell(W, H)}

  <!-- Header -->
  <text x="{pad}" y="56" fill="{TEXT_HI}" font-size="26" font-weight="800" letter-spacing="-0.5" class="sans">codebase composition</text>
  <text x="{pad}" y="76" fill="{TEXT_MUTED}" font-size="11" letter-spacing="1">SNEHIT · {stats["reposPublic"]} PUBLIC + {stats["reposPrivate"]} PRIVATE REPOS · {len(stats["languages"])} LANGUAGES · {esc(stats["generated"]).upper()}</text>
  {hairline(pad, 86, W - pad, HAIRLINE_BRIGHT)}

  <!-- Language treemap -->
  {section_label(pad, 110, "Language treemap · bytes", tag=f'jupyter excluded · top {len(top_langs)} of {len(stats["languages"])}', width=W - pad * 2)}
  {chr(10).join(tm_parts)}

  <!-- Categories -->
  {hairline(pad, 430, W - pad)}
  {section_label(pad, 444, "Work categories · commits", tag=f'focus top-3 {stats["focusTop3Pct"]:.0f}% · {focus_label} · entropy {stats["entropyNormalized"]:.2f}', width=W - pad * 2)}
  {chr(10).join(cat_segs)}
  {chr(10).join(cat_legend)}

  <!-- Bottom -->
  {hairline(pad, 570, W - pad)}
  {section_label(pad, 584, "Top projects · 365d", width=proj_w)}
  {section_label(pr_x, 584, "PR rhythm · 26w + signals", tag=f'mean {stats["prWeeklyMean"]:.1f}/wk', width=pr_w)}
  {hairline(pad, 590, pad + proj_w)}
  {hairline(pr_x, 590, pr_x + pr_w)}
  {chr(10).join(proj_rows)}
  <line x1="{pr_chart_x}" y1="{pr_chart_y + pr_chart_h}" x2="{pr_chart_x + pr_chart_w}" y2="{pr_chart_y + pr_chart_h}" stroke="{HAIRLINE}"/>
  {chr(10).join(pr_bars)}
  <text x="{pr_chart_x}" y="{pr_chart_y + pr_chart_h + 14}" fill="{TEXT_DIM}" font-size="10">{first_label}</text>
  <text x="{pr_chart_x + pr_chart_w}" y="{pr_chart_y + pr_chart_h + 14}" fill="{TEXT_DIM}" font-size="10" text-anchor="end">{last_label}</text>
  {chr(10).join(pr_stat_blocks)}
</svg>
"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    ASSETS.mkdir(exist_ok=True)
    stats = collect()
    (ASSETS / "profile-telemetry.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    (ASSETS / "profile-telemetry.svg").write_text(render_telemetry(stats), encoding="utf-8")
    (ASSETS / "profile-distribution.svg").write_text(render_distribution(stats), encoding="utf-8")
    print(f"Generated {ASSETS / 'profile-telemetry.svg'}")
    print(f"Generated {ASSETS / 'profile-distribution.svg'}")


if __name__ == "__main__":
    main()
