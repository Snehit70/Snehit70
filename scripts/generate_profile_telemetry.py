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
                "releases": node["releases"]["nodes"],
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
            if repo["name"] != USER:
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
        bg="#0d1117", border="#30363d", text="#e6edf3", text2="#9198a1", muted="#6e7681",
        grid="#21262d", track="#161b22", bar="#3d444d", hi="#e6edf3", live="#3fb950",
        series=["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"], other="#3d444d",
    ),
    "light": dict(
        bg="#ffffff", border="#d1d9e0", text="#1f2328", text2="#59636e", muted="#818b98",
        grid="#eff2f5", track="#f6f8fa", bar="#d1d9e0", hi="#1f2328", live="#1a7f37",
        series=["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"], other="#d1d9e0",
    ),
}
# GitHub linguist colours.
LANG_COLORS = {
    "TypeScript": "#3178c6", "HTML": "#e34c26", "JavaScript": "#f1e05a", "Kotlin": "#a97bff",
    "Python": "#3572a5", "Dart": "#00b4ab", "Vue": "#41b883", "CSS": "#663399",
}
DISPLAY = {"fateWheel": "FateWheel", "systemdManager": "SystemdManager", "PLMPRO": "Mold", "fieldForce": "FieldForce",
           "agentObservatory": "Agent Observatory", "voice_web": "VoiceWeb", "ralphy-monitor": "Ralphy Monitor"}
# Story beats called out on the year chart: (repo, tag, label). Dropped once they leave the window.
MILESTONES = [("aceon", "v0.8.0", "Aceon 0.8"), ("pravah", "web-v1.0.0", "Pravah 1.0"),
              ("pomo", "v2.0.0", "Pomo 2.0"), ("vidyut", "v1.1.0", "Vidyut 1.1")]
SKIP = {USER}  # The profile repo itself.


def name(repo):
    return DISPLAY.get(repo, repo[:1].upper() + repo[1:])


def fmt(n):
    return f"{int(round(n)):,}"


def t(x, y, s, size=12, fill=None, anchor="start", weight=400, font="sans", extra=""):
    fam = SANS if font == "sans" else MONO
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{fam}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}" {extra}>{escape(str(s))}</text>')


def pair(x, y, a, b, th, size=12):
    """Primary text followed by a muted mono figure, flowing without width guesses."""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{SANS}" font-size="{size}" fill="{th["text"]}">{escape(a)}'
            f'<tspan dx="5" fill="{th["muted"]}" font-family="{MONO}" font-size="{size - 1}">{escape(b)}</tspan></text>')


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
    return t(x, y, s, 11, th["muted"], anchor=anchor, font="mono")


def rule(y, th):
    return f'<line x1="{PAD}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}" stroke="{th["grid"]}"/>'


MOTION = """<style>
@keyframes rise{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes pop{from{transform:scale(0);opacity:0}}
@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@keyframes show{from{opacity:0}}
@keyframes pulse{from{transform:scale(1);opacity:.7}to{transform:scale(3.4);opacity:0}}
.rise,.grow,.pop,.fade,.pulse{transform-box:fill-box}
.rise{transform-origin:50% 100%;animation:rise .7s cubic-bezier(.2,.7,.2,1) both}
.grow{transform-origin:0 50%;animation:grow .9s cubic-bezier(.2,.7,.2,1) both}
.pop{transform-origin:50% 50%;animation:pop .5s cubic-bezier(.3,1.5,.5,1) both}
.fade{animation:fade .6s ease-out both}
.show{animation:show .5s ease-out both}
.pulse{transform-origin:50% 50%;animation:pulse 2.4s ease-out 2s infinite}
@media (prefers-reduced-motion: reduce){.rise,.grow,.pop,.fade,.show,.pulse{animation:none}}
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
    """What both cards plot. Commit figures all come from the per-repo daily counts so they agree."""
    private = {r["name"] for r in stats["repos"] if r["isPrivate"]}
    per_repo = {n: Counter({d: c for d, c in counts.items() if SINCE <= d <= TODAY})
                for n, counts in repo_daily.items() if n not in SKIP}
    per_repo = {n: c for n, c in per_repo.items() if c}
    days = [SINCE + dt.timedelta(days=i) for i in range((TODAY - SINCE).days + 1)]
    daily = Counter()
    for counts in per_repo.values():
        daily.update(counts)

    # Same grace as streaks(): a day with no commits yet doesn't end the streak.
    end = TODAY if daily[TODAY] else TODAY - dt.timedelta(days=1)
    streak = 0
    while daily[end - dt.timedelta(days=streak)]:
        streak += 1

    year_totals = Counter({n: sum(c.values()) for n, c in per_repo.items()})
    featured = [n for n, _ in year_totals.most_common() if n not in private][:6]

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
        private=private, per_repo=per_repo, days=days, daily=daily,
        commit_total=sum(daily.values()), repos_touched=len(per_repo),
        active_days=sum(1 for d in days if daily[d]),
        streak=streak, streak_start=end - dt.timedelta(days=streak - 1),
        vel=sum(daily[TODAY - dt.timedelta(days=i)] for i in range(30)),
        vel_prev=sum(daily[TODAY - dt.timedelta(days=i)] for i in range(30, 60)),
        year_totals=year_totals, featured=featured, week0=week0, weeks=weeks, weekly=weekly,
        punch=stats["punchcard"], releases=releases, latest=latest,
        release_counts=Counter(r for r, _, _ in releases),
    )


def entity_color(v, repo, th):
    if repo in v.private:
        return th["other"]
    return th["series"][v.featured.index(repo)] if repo in v.featured else th["text2"]


def recent_top(v, days_back, k=4):
    lo = TODAY - dt.timedelta(days=days_back - 1)
    tot = Counter({n: sum(c for d, c in counts.items() if d >= lo) for n, counts in v.per_repo.items()})
    rows, private_sum = [], 0
    for n, c in tot.most_common():
        if c == 0:
            continue
        if n in v.private:
            private_sum += c
        else:
            rows.append((n, c))
    rows = rows[:k]
    if private_sum:
        rows.append(("private repos", private_sum))
    return rows


def ranked_rows(v, rows, x, y0, width, th, label_w=110, step=24, delay=0):
    out = []
    rmax = max((c for _, c in rows), default=0) or 1
    for i, (n, c) in enumerate(rows):
        y = y0 + i * step
        priv = n == "private repos"
        out.append(t(x, y, n if priv else name(n), 12, th["muted"] if priv else th["text"]))
        out.append(t(x + width, y, fmt(c), 12, th["text2"], anchor="end", font="mono"))
        out.append(hbar(x + label_w, y, width - label_w - 46, c / rmax, th["other"] if priv else entity_color(v, n, th), th,
                        delay + i * 90))
    return out


def render_activity(v, th):
    out = []
    trend = (v.vel - v.vel_prev) / v.vel_prev * 100 if v.vel_prev else 0
    stats = [
        ("Streak", f"{v.streak}", "days", f"daily since {v.streak_start:%b %-d}"),
        ("Commits", fmt(v.commit_total), "", f"in {v.repos_touched} repos"),
        ("Releases", fmt(len(v.releases)), "", f"across {len(v.release_counts)} apps"),
        ("Active days", f"{v.active_days / len(v.days) * 100:.0f}", "%", f"{v.active_days} of {len(v.days)} days"),
        ("Last 30 days", fmt(v.vel), "commits", f"{'+' if trend >= 0 else '−'}{abs(trend):.0f}% on the 30 before"),
    ]
    colw = (W - 2 * PAD) / len(stats)
    for i, (lab, val, unit, sub) in enumerate(stats):
        x = PAD + i * colw + (0 if i == 0 else 20)
        if i:
            out.append(f'<line x1="{PAD + i * colw:.1f}" y1="88" x2="{PAD + i * colw:.1f}" y2="150" stroke="{th["grid"]}"/>')
        out.append(f'<g {anim("fade", i * 70)}>')
        out.append(label(x, 96, lab, th))
        unit_svg = f'<tspan font-size="14" font-weight="500" fill="{th["text2"]}" dx="3">{unit}</tspan>' if unit else ""
        out.append(f'<text x="{x:.1f}" y="130" font-family="{SANS}" font-size="30" font-weight="600" fill="{th["text"]}" '
                   f'style="font-variant-numeric: tabular-nums">{escape(val)}{unit_svg}</text>')
        if i == 0:
            out.append(f'<circle cx="{x + 4:.1f}" cy="146" r="3.5" fill="{th["live"]}"/>')
            out.append(t(x + 13, 150, sub, 12, th["text2"]))
        else:
            out.append(t(x, 150, sub, 12, th["text2"]))
        out.append("</g>")
        if i == 0:
            out.append(f'<circle cx="{x + 4:.1f}" cy="146" r="3.5" fill="{th["live"]}" class="pulse"/>')

    # --- The year, week by week, with release milestones.
    top = 196
    out.append(rule(top - 24, th))
    out.append(label(PAD, top, "The year, week by week", th))
    out.append(note(W - PAD, top, "commits per week · releases marked under the axis", th))
    ly = top + 26
    keys = v.featured + ["other"]
    lw = (W - 2 * PAD) / len(keys)
    for i, k in enumerate(keys):
        lx = PAD + i * lw
        col = th["series"][i] if k != "other" else th["other"]
        val = v.year_totals[k] if k != "other" else sum(v.year_totals.values()) - sum(v.year_totals[f] for f in v.featured)
        out.append(f'<rect x="{lx:.1f}" y="{ly - 9}" width="10" height="10" rx="2" fill="{col}"/>')
        out.append(pair(lx + 15, ly, name(k) if k != "other" else "Other", fmt(val), th))

    cx0, cy0, ch = PAD + 34, top + 78, 150
    cw = W - PAD - cx0
    peak = max((sum(wk.values()) for wk in v.weekly), default=0) or 1
    step = nice_step(peak, 3)
    ymax = max(peak * 1.04, math.ceil(peak / step) * step)
    for val in range(0, int(ymax) + 1, int(step)):
        y = cy0 + ch - val / ymax * ch
        out.append(f'<line x1="{cx0}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}" stroke="{th["grid"]}"/>')
        out.append(t(cx0 - 8, y + 4, fmt(val), 11, th["muted"], anchor="end", font="mono"))
    slot = cw / len(v.weeks)
    bw = slot - 3

    def day_x(day):
        return cx0 + ((day - v.week0).days / 7) * slot

    for i, wk in enumerate(v.weeks):
        x = cx0 + i * slot + 1.5
        y = cy0 + ch
        stack = [(th["series"][j], v.weekly[i][k]) for j, k in enumerate(v.featured)] + [(th["other"], v.weekly[i]["other"])]
        segs = [(c, n / ymax * ch) for c, n in stack if n > 0]
        partial = wk + dt.timedelta(days=6) > TODAY or wk < SINCE
        op = ' opacity="0.45"' if partial else ""
        out.append(f'<g {anim("rise", 250 + i * 16)}>')
        for n_seg, (col, h) in enumerate(segs):
            last = n_seg == len(segs) - 1
            out.append(bar(x, y - h, bw, h - (0 if last else 1.5), col, r=2 if last else 0, extra=op))
            y -= h
        out.append("</g>")

    # Milestones: dot + label above the plot, dotted guide down to the baseline. Two rows avoid overlaps.
    for j, (repo, tag, text) in enumerate(MILESTONES):
        hit = next((d for r, tg, d in v.releases if r == repo and tg == tag), None)
        if not hit:
            continue
        mx = day_x(hit) + slot / 2
        my = cy0 - 30 + (j % 2) * 15
        out.append(f'<g {anim("fade", 1300 + j * 150)}>')
        out.append(f'<line x1="{mx:.1f}" y1="{my + 4:.1f}" x2="{mx:.1f}" y2="{cy0 + ch:.1f}" stroke="{th["text2"]}" '
                   f'stroke-width="1" stroke-dasharray="1 3"/>')
        out.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="3.5" fill="{entity_color(v, repo, th)}" stroke="{th["bg"]}" stroke-width="1.5"/>')
        out.append(t(mx + 8, my + 4, text, 11, th["text2"], font="mono"))
        out.append("</g>")
    out.append(f'<line x1="{cx0}" y1="{cy0 + ch}" x2="{W - PAD}" y2="{cy0 + ch}" stroke="{th["border"]}"/>')

    # Release rug: one tick per project per release day, taller when several shipped that day.
    ry = cy0 + ch + 6
    per_day = Counter((r, d) for r, _, d in v.releases)
    for (repo, day), n in sorted(per_day.items(), key=lambda kv: kv[0][1]):
        hh = min(12, 6 + 2 * (n - 1))
        rx = day_x(day) + slot / 2
        out.append(f'<rect x="{rx - 0.75:.1f}" y="{ry + 12 - hh}" width="1.5" height="{hh}" '
                   f'fill="{entity_color(v, repo, th)}" {anim("rise", 450 + (rx - cx0) / cw * 850)}/>')
    for i, wk in enumerate(v.weeks):
        if i and wk.day <= 7:
            out.append(t(cx0 + i * slot + 1.5, ry + 30, f"{wk:%b}", 11, th["muted"], font="mono"))

    # --- Bottom row: punchcard + last 30 days.
    top2 = ry + 70
    out.append(rule(top2 - 24, th))
    pw = 500
    out.append(label(PAD, top2, "When I commit · IST", th))
    # Heatmap in ink tones, binned by quartile so a single bulk-commit hour can't flatten the rest.
    punch = v.punch
    nonzero = sorted(n for row in punch for n in row if n) or [0]
    cuts = [nonzero[int(len(nonzero) * q)] for q in (0.25, 0.5, 0.75)]
    ramp = [th["track"]] + [mix(th["track"], th["hi"], f) for f in (0.2, 0.4, 0.64, 0.9)]

    def level(n):
        return 0 if n == 0 else 1 + sum(n > c for c in cuts)

    # Legend sits on the label line, right-aligned to the panel.
    lx = PAD + pw - 30 - 5 * 12
    out.append(note(lx - 6, top2, "less", th))
    for i, col in enumerate(ramp):
        out.append(f'<rect x="{lx + i * 12}" y="{top2 - 9}" width="10" height="10" rx="2" fill="{col}"/>')
    out.append(note(PAD + pw, top2, "more", th))

    gx0 = PAD + 22
    pitch, sq = (pw - 22 - 72) / 24, 14
    hist_top, hist_h = top2 + 18, 34
    gy0 = hist_top + hist_h + 8
    # Hour profile above the grid: the daily rhythm in one glance.
    hours = [sum(punch[d][h] for d in range(7)) for h in range(24)]
    hmax = max(hours) or 1
    peak_h = hours.index(max(hours))
    for h, n in enumerate(hours):
        hh = n / hmax * hist_h
        out.append(f'<g {anim("rise", 900 + h * 30)}>'
                   f'{bar(gx0 + h * pitch, hist_top + hist_h - hh, sq, hh, th["hi"] if h == peak_h else th["bar"])}</g>')
    quiet = [h for h, n in enumerate(hours) if n < 0.1 * hmax]
    if quiet:
        qx = gx0 + (quiet[0] + quiet[-1] + 1) / 2 * pitch - (pitch - sq) / 2
        out.append(t(qx, hist_top + hist_h - 4, "quiet", 11, th["muted"], anchor="middle", font="mono"))
    out.append(t(gx0 + (peak_h + 1) * pitch + 2, hist_top + 9, f"peak {peak_h:02d}:00", 11, th["text2"], font="mono"))

    days_tot = [sum(row) for row in punch]
    dmax = max(days_tot) or 1
    mx0 = gx0 + 24 * pitch + 8
    for d in range(7):
        ry_ = gy0 + d * (sq + 3)
        out.append(t(gx0 - 10, ry_ + sq - 3, "MTWTFSS"[d], 11, th["muted"], anchor="end", font="mono"))
        for h in range(24):
            n = punch[d][h]
            out.append(f'<rect x="{gx0 + h * pitch:.1f}" y="{ry_}" width="{sq}" height="{sq}" rx="3" fill="{ramp[level(n)]}" '
                       f'{anim("show", 1000 + h * 35 + d * 15)}><title>{"MTWTFSS"[d]} {h:02d}:00 · {n} commits</title></rect>')
        # Weekday totals down the right edge.
        bw_ = days_tot[d] / dmax * 28
        out.append(f'<rect x="{mx0:.1f}" y="{ry_ + 4}" width="{bw_:.1f}" height="6" rx="3" '
                   f'fill="{th["hi"] if days_tot[d] == dmax else th["bar"]}" {anim("grow", 1100 + d * 60)}/>')
        out.append(t(PAD + pw, ry_ + sq - 3, fmt(days_tot[d]), 11, th["text2"], anchor="end", font="mono"))
    cell_h = sq + 3
    for h in (0, 6, 12, 18):
        out.append(t(gx0 + h * pitch, gy0 + 7 * cell_h + 12, f"{h:02d}", 11, th["muted"], font="mono"))

    x0 = PAD + pw + 44
    out.append(label(x0, top2, "Busiest projects · 30 days", th))
    out.extend(ranked_rows(v, recent_top(v, 30, 5), x0, top2 + 32, W - PAD - x0, th, label_w=104, step=26, delay=1000))

    h = int(gy0 + 7 * cell_h + 30)
    return card(h, th, "Atulya Rai", f"@{USER} · last 12 months · {SINCE:%b %-d, %Y} – {TODAY:%b %-d, %Y}",
                "\n".join(out), "Atulya Rai — build activity",
                f"{v.streak}-day streak, {v.commit_total} commits and {len(v.releases)} releases over the last year; "
                "weekly commits by project with release milestones, a weekday-by-hour commit punchcard, and the most active projects of the last 30 days.")


def render_stack(stats, v, th):
    out = []
    # --- Shipped lately: latest release per project, newest first.
    top = 96
    out.append(label(PAD, top, "Shipped lately", th))
    out.append(note(W - PAD, top, "newest release of each app", th))
    recent = sorted(v.latest.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    cw = (W - 2 * PAD) / max(1, len(recent))
    for i, (repo, (tag, day)) in enumerate(recent):
        x = PAD + i * cw + (0 if i == 0 else 18)
        out.append(f'<g {anim("fade", 100 + i * 90)}>')
        if i:
            out.append(f'<line x1="{PAD + i * cw:.1f}" y1="{top + 18}" x2="{PAD + i * cw:.1f}" y2="{top + 74}" stroke="{th["grid"]}"/>')
        out.append(f'<circle cx="{x + 4}" cy="{top + 28}" r="4" fill="{entity_color(v, repo, th)}"/>')
        out.append(t(x + 14, top + 32, name(repo), 14, th["text"], weight=600))
        out.append(t(x, top + 54, tag, 12, th["text"], font="mono"))
        out.append(t(x, top + 72, f"{day:%b %-d} · {v.release_counts[repo]} releases", 11, th["muted"], font="mono"))
        out.append("</g>")

    # --- Languages.
    top += 124
    out.append(rule(top - 24, th))
    langs = stats["languages"]
    total = sum(b for _, b in langs) or 1
    shown = [(n, b) for n, b in langs if n in LANG_COLORS][:8]
    parts = shown + [("Other", total - sum(b for _, b in shown))]
    out.append(label(PAD, top, "Languages", th))
    out.append(note(W - PAD, top, f"{len(langs)} languages · by bytes", th))
    x, y, bw = PAD, top + 16, W - 2 * PAD
    out.append(f'<clipPath id="lc"><rect x="{PAD}" y="{y}" width="{bw}" height="10" rx="5"/></clipPath><g clip-path="url(#lc)"><g {anim("grow", 500)}>')
    for n, b in parts:
        sw = b / total * bw
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{max(0, sw - 2):.1f}" height="10" fill="{LANG_COLORS.get(n, th["other"])}"/>')
        x += sw
    out.append("</g></g>")
    cols = 5
    lw = bw / cols
    for i, (n, b) in enumerate(parts):
        lx = PAD + (i % cols) * lw
        ly = y + 40 + (i // cols) * 22
        out.append(f'<circle cx="{lx + 5}" cy="{ly - 4}" r="5" fill="{LANG_COLORS.get(n, th["other"])}"/>')
        out.append(pair(lx + 16, ly, n, f"{b / total * 100:.1f}%", th))

    # --- Most commits + pull requests.
    top2 = y + 100
    out.append(rule(top2 - 24, th))
    pw = (W - 2 * PAD - 48) / 2
    out.append(label(PAD, top2, "Commits by project · 12 months", th))
    rows, priv = [], 0
    for n, c in v.year_totals.most_common():
        if n in v.private:
            priv += c
        elif len(rows) < 7:
            rows.append((n, c))
    if priv:
        rows.append(("private repos", priv))
    out.extend(ranked_rows(v, rows, PAD, top2 + 30, pw, th, delay=800))

    x0 = PAD + pw + 48
    out.append(label(x0, top2, "Pull requests · 12 months", th))
    out.append(note(W - PAD, top2, "weekly, last 26 weeks", th))
    pr = stats["prRecent"]
    pmax = max(n for _, n in pr) or 1
    ph = 96
    base = top2 + 22 + ph
    slot = pw / len(pr)
    for i, (wk, n) in enumerate(pr):
        out.append(f'<g {anim("rise", 800 + i * 30)}>')
        out.append(bar(x0 + i * slot + 1.5, base - n / pmax * ph, slot - 3, n / pmax * ph, th["hi"] if n == pmax else th["bar"]))
        out.append("</g>")
    pk = max(range(len(pr)), key=lambda i: pr[i][1])
    out.append(t(x0 + pk * slot + slot + 4, base - ph + 9, f"{pr[pk][1]} · week of {dt.date.fromisoformat(pr[pk][0]):%b %-d}", 11, th["text2"], font="mono"))
    out.append(f'<line x1="{x0}" y1="{base}" x2="{x0 + pw}" y2="{base}" stroke="{th["border"]}"/>')
    out.append(note(x0, base + 16, f"{dt.date.fromisoformat(pr[0][0]):%b %-d}", th, anchor="start"))
    out.append(note(x0 + pw, base + 16, "this week", th))
    sy = base + 46
    for i, (lab, val) in enumerate([("opened", fmt(stats["prs"])), ("merged", f"{stats['prMergeRatio']:.0f}%"),
                                     ("weeks with PRs", f"{stats['prActiveWeeks']}")]):
        sx = x0 + i * (pw / 3)
        out.append(f'<text x="{sx:.1f}" y="{sy}" font-family="{SANS}" font-size="18" font-weight="600" fill="{th["text"]}">{val}'
                   f'<tspan font-size="12" font-weight="400" fill="{th["text2"]}" dx="6">{lab}</tspan></text>')

    h = int(max(top2 + 30 + len(rows) * 24, sy) + 20)
    added, removed = stats["additions"], stats["deletions"]
    return card(h, th, "Shipping & stack",
                f"+{added / 1e6:.2f}M / −{removed / 1e3:.0f}K lines · {stats['reposPublic']} public, {stats['reposPrivate']} private repos",
                "\n".join(out), "Shipping and stack",
                "Latest release per project, language mix by bytes, most-committed projects over 12 months, and weekly pull request volume.")


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
