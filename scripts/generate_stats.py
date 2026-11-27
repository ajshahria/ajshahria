"""Generate GitHub profile stat cards (SVG) for a README.

Outputs:
  assets/github-stats.svg         dashboard: contributions, streaks, commits by hour, languages
  assets/contribution-graph.svg   animated contribution grid (current streak glows purple)

Environment:
  GH_USER     GitHub login (default: ajshahria)
  GH_TOKEN    token for the GitHub API (GraphQL needs one)
  TZ_OFFSET   hours from UTC for "commits by hour" (default: 6 = Bangladesh)
  OUT_DIR     output folder (default: assets)

Run `python scripts/generate_stats.py --demo` to render with fake data (no token needed).
"""

import datetime as dt
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from xml.sax.saxutils import escape

USER = os.environ.get("GH_USER", "ajshahria")
TOKEN = os.environ.get("GH_TOKEN", "")
TZ_OFFSET = float(os.environ.get("TZ_OFFSET", "6"))
OUT_DIR = os.environ.get("OUT_DIR", "assets")
DISPLAY_NAME = os.environ.get("DISPLAY_NAME", "Abdul Julil Shahria")  # name shown on the card
# Other commit emails that are yours (lowercase, comma-separated), e.g. a personal address set in git config.
EXTRA_EMAILS = {e.strip().lower() for e in os.environ.get("EXTRA_EMAILS", "").split(",") if e.strip()}

LANG_COLORS = {
    "Python": "#3b82f6",
    "JavaScript": "#facc15",
    "TypeScript": "#2dd4bf",
    "HTML": "#ef4444",
    "CSS": "#a855f7",
    "Java": "#f59e0b",
    "C++": "#ec4899",
    "C": "#94a3b8",
    "Jupyter Notebook": "#f97316",
    "Shell": "#22c55e",
    "Other": "#475569",
}
FALLBACK_COLORS = ["#38bdf8", "#84cc16", "#e879f9", "#fb7185", "#fbbf24"]


# --------------------------------------------------------------------------- API
def api(url, payload=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-stats-generator",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.load(resp)


def graphql(query, **variables):
    data = api("https://api.github.com/graphql", {"query": query, "variables": variables})
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


CALENDAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}"""


def fetch_calendar(created_at):
    """Daily contribution counts from account creation until today."""
    days = {}
    now = dt.datetime.now(dt.timezone.utc)
    start = created_at
    while start < now:
        end = min(start + dt.timedelta(days=365), now)
        data = graphql(CALENDAR_QUERY, login=USER, **{"from": start.isoformat(), "to": end.isoformat()})
        weeks = data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        for week in weeks:
            for day in week["contributionDays"]:
                days[dt.date.fromisoformat(day["date"])] = day["contributionCount"]
        start = end
    return days


LEVELS = {}  # date -> GitHub's own color level (0-4) from the profile graph


def scrape_calendar(created_at):
    """No-token fallback: read the public contribution graph from github.com, one year at a time."""
    days = {}
    today = dt.date.today()
    for year in range(created_at.year, today.year + 1):
        url = f"https://github.com/users/{USER}/contributions?from={year}-01-01&to={year}-12-31"
        request = urllib.request.Request(url, headers={"User-Agent": "profile-stats-generator"})
        with urllib.request.urlopen(request, timeout=60) as resp:
            html = resp.read().decode("utf-8")
        cells = re.findall(r'data-date="(\d{4}-\d{2}-\d{2})" id="([^"]+)" data-level="(\d)"', html)
        id_to_date = {cell_id: date for date, cell_id, _ in cells}
        for date, _, lvl in cells:
            LEVELS[dt.date.fromisoformat(date)] = int(lvl)
        for cell_id, text in re.findall(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]*)</tool-tip>', html):
            if cell_id not in id_to_date:
                continue
            match = re.match(r"\s*([\d,]+) contribution", text)
            count = int(match.group(1).replace(",", "")) if match else 0
            day = dt.date.fromisoformat(id_to_date[cell_id])
            if day <= today:
                days[day] = count
    return days


def fetch_commits(repos, user_id):
    """Commit hours (local time) and commit counts per repo language.

    Reads each repo's full git history (all branches) instead of the commits API, because the API's
    author filter misses commits made under a previous username. Commits are matched by the
    numeric account id inside GitHub's noreply address ("<id>+<any username>@users.noreply.github.com"),
    which stays the same when the username changes.
    """
    hours = Counter()
    by_lang = Counter()
    marker = f"{user_id}+"
    with tempfile.TemporaryDirectory() as tmp:
        for repo in repos:
            dest = os.path.join(tmp, repo["name"] + ".git")
            clone = subprocess.run(
                ["git", "clone", "-q", "--bare", "--filter=blob:none", repo["clone_url"], dest],
                capture_output=True, text=True)
            if clone.returncode != 0:
                continue
            log = subprocess.run(["git", "-C", dest, "log", "--all", "--format=%ae|%at"],
                                 capture_output=True, text=True).stdout
            for line in log.splitlines():
                email, _, stamp = line.rpartition("|")
                if not (email.startswith(marker) or email.lower() in EXTRA_EMAILS):
                    continue
                local = dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)
                hours[local.hour] += 1
                if repo.get("language"):
                    by_lang[repo["language"]] += 1
    return [hours.get(h, 0) for h in range(24)], by_lang


def collect_live():
    profile = api(f"https://api.github.com/users/{USER}")
    created = dt.datetime.fromisoformat(profile["created_at"].replace("Z", "+00:00"))
    repos = api(f"https://api.github.com/users/{USER}/repos?type=owner&per_page=100")
    own = [r for r in repos if not r["fork"]]
    # The public profile graph is the source of truth: it matches the totals on github.com exactly.
    days = scrape_calendar(created)
    hours, commit_langs = fetch_commits(own, profile["id"])
    repo_langs = Counter(r["language"] for r in own if r["language"])
    return {
        "name": DISPLAY_NAME or profile.get("name") or USER,
        "bio": profile.get("bio") or "",
        "created": created.date(),
        "days": days,
        "hours": hours,
        "repo_langs": repo_langs,
        "commit_langs": commit_langs,
        "activity": fetch_activity(),
    }


ACTIVITY_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      commitContributionsByRepository(maxRepositories: 25) {
        repository { nameWithOwner } contributions { totalCount }
      }
      issueContributionsByRepository(maxRepositories: 25) {
        repository { nameWithOwner } contributions { totalCount }
      }
      pullRequestContributionsByRepository(maxRepositories: 25) {
        repository { nameWithOwner } contributions { totalCount }
      }
      pullRequestReviewContributionsByRepository(maxRepositories: 25) {
        repository { nameWithOwner } contributions { totalCount }
      }
    }
  }
}"""


def fetch_activity():
    """Last-12-months split between commits, issues, pull requests and reviews, like GitHub's
    "Activity overview", plus the repositories contributed to."""
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=365)
    if TOKEN:
        try:
            c = graphql(ACTIVITY_QUERY, login=USER, **{"from": since.isoformat(), "to": now.isoformat()})
            c = c["user"]["contributionsCollection"]
            repos = Counter()
            for key in ("commit", "issue", "pullRequest", "pullRequestReview"):
                for item in c[f"{key}ContributionsByRepository"]:
                    repos[item["repository"]["nameWithOwner"]] += item["contributions"]["totalCount"]
            return {
                "commits": c["totalCommitContributions"],
                "issues": c["totalIssueContributions"],
                "prs": c["totalPullRequestContributions"],
                "reviews": c["totalPullRequestReviewContributions"],
                "repos": repos,
            }
        except Exception as err:  # fall back to the search API below
            print(f"GraphQL activity query failed ({err}); using search API", file=sys.stderr)

    day = since.date().isoformat()
    search = "https://api.github.com/search/"
    repos = Counter()

    def total(kind, query):
        data = api(f"{search}{kind}?q={urllib.parse.quote(query)}&per_page=100")
        for item in data.get("items", []):
            name = (item.get("repository") or {}).get("full_name") or item.get("repository_url", "").split("/repos/")[-1]
            if name:
                repos[name] += 1
        return data.get("total_count", 0)

    return {
        "commits": total("commits", f"author:{USER} committer-date:>={day}"),
        "issues": total("issues", f"author:{USER} type:issue created:>={day}"),
        "prs": total("issues", f"author:{USER} type:pr created:>={day}"),
        "reviews": total("issues", f"reviewed-by:{USER} -author:{USER} type:pr updated:>={day}"),
        "repos": repos,
    }


def collect_demo():
    rng = random.Random(7)
    today = dt.date.today()
    created = today - dt.timedelta(days=680)
    days = {}
    for i in range(681):
        d = created + dt.timedelta(days=i)
        days[d] = rng.choice([0, 0, 0, 1, 2, 3, 5, 8]) if i < 640 else rng.randint(1, 9)
    return {
        "name": DISPLAY_NAME or USER,
        "bio": "Computer Science Student | Web Developer (MERN Stack) | AI Enthusiast",
        "created": created,
        "days": days,
        "hours": [rng.randint(0, 30) for _ in range(24)],
        "repo_langs": Counter({"JavaScript": 11, "HTML": 9, "Java": 3, "C++": 1, "TypeScript": 1, "Python": 1, "C": 1}),
        "commit_langs": Counter({"JavaScript": 420, "HTML": 160, "TypeScript": 90, "C++": 40, "Java": 35}),
        "activity": {"commits": 390, "issues": 6, "prs": 22, "reviews": 4,
                     "repos": Counter({f"{USER}/Quick-Dine-": 120, f"{USER}/SkillSwap": 80, "siam1251/siam1251.github.io": 12})},
    }


# --------------------------------------------------------------------- analysis
def streaks(days):
    """Return (current, cur_start, cur_end, longest, long_start, long_end)."""
    if not days:
        return 0, None, None, 0, None, None
    first, today = min(days), dt.date.today()
    longest = run = 0
    long_range = (None, None)
    run_start = None
    d = first
    while d <= today:
        if days.get(d, 0) > 0:
            run_start = run_start or d
            run += 1
            if run > longest:
                longest, long_range = run, (run_start, d)
        else:
            run, run_start = 0, None
        d += dt.timedelta(days=1)
    # Current streak: today may not have contributions yet, so allow starting from yesterday.
    end = today if days.get(today, 0) > 0 else today - dt.timedelta(days=1)
    current, d = 0, end
    while days.get(d, 0) > 0:
        current += 1
        d -= dt.timedelta(days=1)
    cur_start = d + dt.timedelta(days=1) if current else None
    return current, cur_start, end if current else None, longest, long_range[0], long_range[1]


def top_languages(counter, n=5):
    total = sum(counter.values()) or 1
    top = counter.most_common(n)
    rows = [(lang, count / total) for lang, count in top]
    rest = 1 - sum(p for _, p in rows)
    return rows, rest


def lang_color(lang, i):
    return LANG_COLORS.get(lang, FALLBACK_COLORS[i % len(FALLBACK_COLORS)])


def fmt_day(d):
    return f"{d:%b} {d.day}" if d else "-"


def tz_label():
    sign = "+" if TZ_OFFSET >= 0 else "-"
    hours = abs(TZ_OFFSET)
    whole, minutes = int(hours), int(round((hours - int(hours)) * 60))
    return f"UTC{sign}{whole}" + (f":{minutes:02d}" if minutes else "")


# ------------------------------------------------------------------ stats card
FONT = "'Segoe UI', Ubuntu, 'Helvetica Neue', Arial, sans-serif"


def donut_panel(x, y, w, h, title, counter):
    rows, rest = top_languages(counter)
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" class="tile"/>',
             f'<text x="{x + 24}" y="{y + 34}" class="h2">{escape(title)}</text>']
    if not rows:
        parts.append(f'<text x="{x + 24}" y="{y + 80}" class="muted">No data yet</text>')
        return "\n".join(parts)
    for i, (lang, pct) in enumerate(rows):
        ry = y + 70 + i * 26
        parts.append(f'<circle cx="{x + 30}" cy="{ry - 4}" r="5" fill="{lang_color(lang, i)}"/>'
                     f'<text x="{x + 44}" y="{ry}" class="label">{escape(lang)}</text>'
                     f'<text x="{x + 250}" y="{ry}" class="muted" text-anchor="end">{pct * 100:.0f}%</text>')
    cx, cy, r = x + w - 116, y + h / 2 + 12, 58
    circ = 2 * math.pi * r
    segs = rows + ([("Other", rest)] if rest > 0.005 else [])
    start = 0.0
    for i, (lang, pct) in enumerate(segs):
        length = max(pct * circ - 3, 0.5)
        color = LANG_COLORS["Other"] if lang == "Other" else lang_color(lang, i)
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="24" '
            f'stroke-dasharray="{length:.2f} {circ:.2f}" stroke-dashoffset="{-start * circ:.2f}" '
            f'transform="rotate(-90 {cx} {cy})" class="seg" style="animation-delay:{0.3 + i * 0.12:.2f}s"/>')
        start += pct
    lead, lead_pct = rows[0]
    parts.append(f'<text x="{cx}" y="{cy + 6}" class="donutnum" text-anchor="middle">{lead_pct * 100:.0f}%</text>'
                 f'<text x="{cx}" y="{cy + 24}" class="small" text-anchor="middle">{escape(lead)}</text>')
    return "\n".join(parts)


def hour_chart(x, y, w, h, hours):
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" class="tile"/>',
             f'<text x="{x + 24}" y="{y + 34}" class="h2">Commits by hour</text>',
             f'<text x="{x + w - 24}" y="{y + 34}" class="small" text-anchor="end">{tz_label()}</text>']
    left, right, top, bottom = x + 60, x + w - 24, y + 66, y + h - 36
    peak = max(hours) or 1
    step = max(1, math.ceil(peak / 3 / 5) * 5)
    ymax = step * math.ceil(peak / step)
    for t in range(0, ymax + 1, step):
        gy = bottom - (bottom - top) * t / ymax
        parts.append(f'<line x1="{left}" x2="{right}" y1="{gy:.1f}" y2="{gy:.1f}" class="grid"/>'
                     f'<text x="{left - 12}" y="{gy + 4:.1f}" class="axis" text-anchor="end">{t}</text>')
    slot = (right - left) / 24
    bw = slot * 0.62
    # busiest 3-hour window
    best = max(range(22), key=lambda i: sum(hours[i:i + 3]))
    if sum(hours) > 0:
        px = left + best * slot + 2
        parts.append(f'<rect x="{px:.1f}" y="{top - 20}" width="{slot * 3 - 4:.1f}" height="{bottom - top + 22}" rx="8" class="peak"/>'
                     f'<text x="{px + (slot * 3 - 4) / 2:.1f}" y="{top - 6}" class="peaklabel" text-anchor="middle">Peak hours</text>')
    for hr, count in enumerate(hours):
        bh = max((bottom - top) * count / ymax, 2 if count else 0)
        bx = left + hr * slot + (slot - bw) / 2
        fill = "url(#barPeak)" if best <= hr < best + 3 and sum(hours) else "url(#bar)"
        parts.append(f'<rect x="{bx:.1f}" y="{bottom - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="4" fill="{fill}" '
                     f'class="bar" style="animation-delay:{hr * 0.025:.3f}s"><title>{hr:02d}:00 - {count} commits</title></rect>')
        if hr % 3 == 0 or hr == 23:
            parts.append(f'<text x="{bx + bw / 2:.1f}" y="{bottom + 20}" class="axis" text-anchor="middle">{hr:02d}</text>')
    return "\n".join(parts)


def stats_svg(data):
    W, H = 1000, 700
    total = sum(data["days"].values())
    cur, cs, ce, best, ls, le = streaks(data["days"])
    ring_r = 52
    ring_c = 2 * math.pi * ring_r
    frac = cur / best if best else 0
    if cur and cur == best:
        note = "Matches the longest streak"
    elif cur:
        note = f"{best - cur} days to beat your record"
    else:
        note = "Commit today to start a streak"
    subtitle = data["name"] + (f" · {data['bio'].split('|')[0].strip()}" if data["bio"] else "")
    rx, ry = 306 + 96, 175

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="GitHub stats for {escape(USER)}">
<title>GitHub stats for {escape(data["name"])}</title>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#1e1b4b"/><stop offset="0.55" stop-color="#0b1023"/><stop offset="1" stop-color="#123047"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.15" cy="0" r="0.7">
    <stop offset="0" stop-color="#7c3aed" stop-opacity="0.35"/><stop offset="1" stop-color="#7c3aed" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#c084fc"/><stop offset="1" stop-color="#2dd4bf"/>
  </linearGradient>
  <linearGradient id="bar" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#818cf8"/><stop offset="1" stop-color="#4f46e5"/>
  </linearGradient>
  <linearGradient id="barPeak" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#5eead4"/><stop offset="1" stop-color="#6366f1"/>
  </linearGradient>
  <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">
    <circle cx="1" cy="1" r="1" fill="#ffffff" opacity="0.05"/>
  </pattern>
</defs>
<style>
  text {{ font-family: {FONT}; fill: #e2e8f0; }}
  .h1 {{ font-size: 26px; font-weight: 700; }}
  .h2 {{ font-size: 16px; font-weight: 700; }}
  .sub {{ font-size: 12px; fill: #94a3b8; }}
  .label {{ font-size: 13px; fill: #cbd5e1; }}
  .muted {{ font-size: 13px; fill: #94a3b8; }}
  .small {{ font-size: 11px; fill: #94a3b8; }}
  .axis {{ font-size: 10px; fill: #64748b; }}
  .accent {{ font-size: 12px; font-weight: 600; fill: #5eead4; }}
  .big {{ font-size: 64px; font-weight: 800; fill: #eef2ff; }}
  .unit {{ font-size: 20px; fill: #94a3b8; }}
  .ringnum {{ font-size: 34px; font-weight: 800; }}
  .donutnum {{ font-size: 20px; font-weight: 700; }}
  .peaklabel {{ font-size: 10px; font-weight: 700; fill: #5eead4; }}
  .tile {{ fill: #ffffff; fill-opacity: 0.04; stroke: #ffffff; stroke-opacity: 0.08; }}
  .tile-hl {{ fill: #ffffff; fill-opacity: 0.05; stroke: #818cf8; stroke-opacity: 0.6; }}
  .grid {{ stroke: #ffffff; stroke-opacity: 0.06; }}
  .peak {{ fill: #2dd4bf; fill-opacity: 0.07; stroke: #2dd4bf; stroke-opacity: 0.25; }}
  .bar {{ transform-box: fill-box; transform-origin: bottom; animation: grow .8s cubic-bezier(.2,.8,.2,1) both; }}
  .seg {{ animation: fade .7s ease-out both; }}
  .ringarc {{ animation: draw 1.4s cubic-bezier(.2,.8,.2,1) both; }}
  .fadein {{ animation: fade .8s ease-out both; }}
  @keyframes grow {{ from {{ transform: scaleY(0); }} to {{ transform: scaleY(1); }} }}
  @keyframes fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  @keyframes draw {{ from {{ stroke-dashoffset: {ring_c:.2f}; }} }}
</style>
<rect width="{W}" height="{H}" rx="24" fill="url(#bg)"/>
<rect width="{W}" height="{H}" rx="24" fill="url(#glow)"/>
<rect width="{W}" height="{H}" rx="24" fill="url(#dots)"/>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="24" fill="none" stroke="#ffffff" stroke-opacity="0.08"/>

<text x="40" y="56" class="h1">GitHub stats</text>
<text x="40" y="78" class="sub">{escape(subtitle)}</text>

<g class="fadein">
  <rect x="40" y="100" width="250" height="150" rx="16" class="tile"/>
  <text x="64" y="134" class="muted">Total contributions</text>
  <text x="64" y="206" class="big">{total:,}</text>
  <text x="64" y="232" class="accent">{fmt_day(data["created"])}, {data["created"].year} to present</text>

  <rect x="306" y="100" width="388" height="150" rx="16" class="tile-hl"/>
  <circle cx="{rx}" cy="{ry}" r="{ring_r}" fill="none" stroke="#ffffff" stroke-opacity="0.08" stroke-width="10"/>
  <circle cx="{rx}" cy="{ry}" r="{ring_r}" fill="none" stroke="url(#ring)" stroke-width="10" stroke-linecap="round"
          stroke-dasharray="{ring_c * frac:.2f} {ring_c:.2f}" transform="rotate(-90 {rx} {ry})" class="ringarc"/>
  <text x="{rx}" y="{ry + 10}" class="ringnum" text-anchor="middle">{cur}</text>
  <text x="{rx}" y="{ry + 28}" class="small" text-anchor="middle">days</text>
  <text x="{rx + 80}" y="{ry - 14}" class="h2">Current streak</text>
  <text x="{rx + 80}" y="{ry + 8}" class="accent">{fmt_day(cs)} to {fmt_day(ce)}</text>
  <text x="{rx + 80}" y="{ry + 32}" class="small">{escape(note)}</text>

  <rect x="710" y="100" width="250" height="150" rx="16" class="tile"/>
  <text x="734" y="134" class="muted">Longest streak</text>
  <text x="734" y="206" class="big">{best}<tspan class="unit" dx="10">days</tspan></text>
  <text x="734" y="232" class="accent">{fmt_day(ls)} to {fmt_day(le)}</text>
</g>

{hour_chart(40, 266, 920, 180, data["hours"])}

{donut_panel(40, 462, 452, 200, "Top languages by repository", data["repo_langs"])}
{donut_panel(508, 462, 452, 200, "Top languages by commit", data["commit_langs"])}
</svg>
'''


# ----------------------------------------------------------- contribution grid
def graph_svg(data):
    days = data["days"]
    cur, cs, ce, *_ = streaks(days)
    today = dt.date.today()
    # 53 weeks, columns start on Sunday like GitHub
    start = today - dt.timedelta(days=(today.weekday() + 1) % 7) - dt.timedelta(weeks=52)
    counts = [c for d, c in days.items() if d >= start and c > 0]
    counts.sort()

    def level(c):
        if c <= 0 or not counts:
            return 0
        q = [counts[int(len(counts) * f)] for f in (0.25, 0.5, 0.75)]
        return 1 + sum(c > t for t in q)

    greens = ["#ebedf0", "#f9a8d4", "#f472b6", "#db2777", "#9d174d"]  # pink scale
    cell, gap, ox, oy = 14, 4, 40, 40
    cols = 53
    W = ox * 2 + cols * (cell + gap) - gap
    H = oy + 7 * (cell + gap) - gap + 46
    rects, months = [], []
    last_month = None
    for col in range(cols):
        week_start = start + dt.timedelta(weeks=col)
        if week_start.month != last_month and col < cols - 2:
            months.append(f'<text x="{ox + col * (cell + gap)}" y="{oy - 12}" class="m">{week_start:%b}</text>')
            last_month = week_start.month
        for row in range(7):
            d = week_start + dt.timedelta(days=row)
            if d > today:
                continue
            c = days.get(d, 0)
            x, y = ox + col * (cell + gap), oy + row * (cell + gap)
            in_streak = cur and cs <= d <= ce
            cls = "c s" if in_streak else "c"
            fill = "#7c3aed" if in_streak else greens[LEVELS.get(d, level(c))]
            delay = col * 0.025 + row * 0.01
            rects.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3" fill="{fill}" class="{cls}" '
                         f'style="animation-delay:{delay:.2f}s"><title>{d:%b %d, %Y}: {c} contributions</title></rect>')
    legend_y = H - 22
    legend = [f'<text x="{W - ox - 5 * (cell + gap) - 44}" y="{legend_y + 11}" class="m" text-anchor="end">Less</text>']
    for i, g in enumerate(greens):
        lx = W - ox - (5 - i) * (cell + gap) - 36
        legend.append(f'<rect x="{lx}" y="{legend_y}" width="{cell}" height="{cell}" rx="3" fill="{g}"/>')
    legend.append(f'<text x="{W - ox - 30}" y="{legend_y + 11}" class="m">More</text>')
    total_year = sum(c for d, c in days.items() if d >= start)
    streak_note = f" · current streak {cur} days" if cur else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="Contribution graph">
<title>{total_year} contributions in the last year</title>
<style>
  .m {{ font-family: {FONT}; font-size: 11px; fill: #8b949e; }}
  .c {{ transform-box: fill-box; transform-origin: center; animation: pop .5s cubic-bezier(.2,.8,.2,1) both; }}
  .s {{ animation: pop .5s cubic-bezier(.2,.8,.2,1) both, pulse 2.4s ease-in-out 1.6s infinite; }}
  @keyframes pop {{ from {{ transform: scale(0); opacity: 0; }} to {{ transform: scale(1); opacity: 1; }} }}
  @keyframes pulse {{ 0%,100% {{ fill: #7c3aed; }} 50% {{ fill: #a78bfa; }} }}
</style>
<rect width="{W}" height="{H}" rx="14" fill="#0d1117"/>
{"".join(months)}
{"".join(rects)}
<text x="{ox}" y="{legend_y + 11}" class="m">{total_year:,} contributions in the last year{streak_note}</text>
{"".join(legend)}
</svg>
'''


# ------------------------------------------------------------ activity overview
def activity_svg(act):
    W, H = 1000, 340
    parts_total = act["commits"] + act["issues"] + act["prs"] + act["reviews"]

    def pct(n):
        return n / parts_total if parts_total else 0

    p = {k: pct(act[k]) for k in ("commits", "issues", "prs", "reviews")}

    # Left: repositories contributed to
    rows = []
    top = act["repos"].most_common(4)
    peak = top[0][1] if top else 1
    for i, (name, count) in enumerate(top):
        y = 140 + i * 40
        owner, _, repo = name.partition("/")
        room = 30 - len(owner)  # keep long names clear of the count on the right
        if len(repo) > room:
            repo = repo[:room - 1] + "…"
        bar = 190 * count / peak
        rows.append(
            f'<g class="fadein" style="animation-delay:{0.15 + i * 0.1:.2f}s">'
            f'<rect x="64" y="{y - 13}" width="12" height="15" rx="2" fill="none" stroke="#94a3b8" stroke-width="1.5"/>'
            f'<line x1="67" x2="73" y1="{y - 6}" y2="{y - 6}" stroke="#94a3b8" stroke-width="1.5"/>'
            f'<text x="88" y="{y}" class="repo"><tspan class="owner">{escape(owner)}/</tspan>{escape(repo)}</text>'
            f'<rect x="88" y="{y + 9}" width="{bar:.1f}" height="4" rx="2" fill="url(#repoBar)" class="grow"/>'
            f'<text x="472" y="{y}" class="small" text-anchor="end">{count} contributions</text></g>')
    others = len(act["repos"]) - len(top)
    if others > 0:
        rows.append(f'<text x="64" y="{140 + len(top) * 40}" class="small">and {others} other repositor{"y" if others == 1 else "ies"}</text>')
    if not top:
        rows.append('<text x="64" y="140" class="muted">No public contributions in the last year</text>')

    # Right: GitHub-style axis chart
    cx, cy, L = 760, 182, 110
    pts = {
        "commits": (cx - max(p["commits"], 0.04) * L, cy),
        "issues": (cx + max(p["issues"], 0.04) * L, cy),
        "reviews": (cx, cy - max(p["reviews"], 0.04) * L),
        "prs": (cx, cy + max(p["prs"], 0.04) * L),
    }
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pts["commits"], pts["reviews"], pts["issues"], pts["prs"]))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" class="dot"/>'
                   for k, (x, y) in pts.items() if p[k] > 0)

    def label(key, text, x, y, anchor):
        share = f'<tspan x="{x}" dy="0" class="pct">{p[key] * 100:.0f}%</tspan><tspan x="{x}" dy="16">{text}</tspan>' \
            if p[key] > 0 else f'<tspan x="{x}" dy="16">{text}</tspan>'
        return f'<text x="{x}" y="{y}" class="axislabel" text-anchor="{anchor}">{share}</text>'

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="Activity overview">
<title>Activity overview for {escape(USER)}</title>
<defs>
  <linearGradient id="bg2" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#1e1b4b"/><stop offset="0.55" stop-color="#0b1023"/><stop offset="1" stop-color="#123047"/>
  </linearGradient>
  <radialGradient id="glow2" cx="0.85" cy="0.1" r="0.6">
    <stop offset="0" stop-color="#7c3aed" stop-opacity="0.3"/><stop offset="1" stop-color="#7c3aed" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="repoBar" x1="0" x2="1">
    <stop offset="0" stop-color="#818cf8"/><stop offset="1" stop-color="#5eead4"/>
  </linearGradient>
</defs>
<style>
  text {{ font-family: {FONT}; fill: #e2e8f0; }}
  .h1 {{ font-size: 22px; font-weight: 700; }}
  .sub {{ font-size: 12px; fill: #94a3b8; }}
  .label {{ font-size: 12px; font-weight: 600; fill: #5eead4; letter-spacing: .04em; }}
  .repo {{ font-size: 15px; font-weight: 600; fill: #a5b4fc; }}
  .owner {{ fill: #94a3b8; font-weight: 400; }}
  .small {{ font-size: 12px; fill: #94a3b8; }}
  .muted {{ font-size: 13px; fill: #94a3b8; }}
  .axislabel {{ font-size: 13px; fill: #94a3b8; }}
  .pct {{ font-size: 15px; font-weight: 700; fill: #e2e8f0; }}
  .axis {{ stroke: #a78bfa; stroke-opacity: .55; stroke-width: 2; }}
  .shape {{ fill: #a78bfa; fill-opacity: .28; stroke: #c4b5fd; stroke-width: 3; stroke-linejoin: round;
           transform-origin: {cx}px {cy}px; animation: bloom 1s cubic-bezier(.2,.8,.2,1) .2s both; }}
  .dot {{ fill: #0b1023; stroke: #c4b5fd; stroke-width: 2.5; animation: fade .6s ease-out 1s both; }}
  .fadein {{ animation: fade .7s ease-out both; }}
  .grow {{ transform-box: fill-box; transform-origin: left; animation: growx .9s cubic-bezier(.2,.8,.2,1) .3s both; }}
  @keyframes fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  @keyframes bloom {{ from {{ transform: scale(0); opacity: 0; }} to {{ transform: scale(1); opacity: 1; }} }}
  @keyframes growx {{ from {{ transform: scaleX(0); }} to {{ transform: scaleX(1); }} }}
</style>
<rect width="{W}" height="{H}" rx="24" fill="url(#bg2)"/>
<rect width="{W}" height="{H}" rx="24" fill="url(#glow2)"/>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="24" fill="none" stroke="#ffffff" stroke-opacity="0.08"/>

<text x="40" y="56" class="h1">Activity overview</text>
<text x="40" y="78" class="sub">Last 12 months · {parts_total:,} contributions</text>
<text x="64" y="112" class="label">CONTRIBUTED TO</text>
{"".join(rows)}

<line x1="520" x2="520" y1="40" y2="{H - 40}" stroke="#ffffff" stroke-opacity="0.1"/>

<line x1="{cx - L}" x2="{cx + L}" y1="{cy}" y2="{cy}" class="axis"/>
<line x1="{cx}" x2="{cx}" y1="{cy - L}" y2="{cy + L}" class="axis"/>
<polygon points="{poly}" class="shape"/>
{dots}
{label("reviews", "Code review", cx, cy - L - 38, "middle")}
{label("prs", "Pull requests", cx, cy + L + 6, "middle")}
{label("commits", "Commits", cx - L - 14, cy - 12, "end")}
{label("issues", "Issues", cx + L + 14, cy - 12, "start")}
</svg>
'''


def main():
    data = collect_demo() if "--demo" in sys.argv else collect_live()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "activity-overview.svg"), "w", encoding="utf-8") as f:
        f.write(activity_svg(data["activity"]))
    with open(os.path.join(OUT_DIR, "github-stats.svg"), "w", encoding="utf-8") as f:
        f.write(stats_svg(data))
    with open(os.path.join(OUT_DIR, "contribution-graph.svg"), "w", encoding="utf-8") as f:
        f.write(graph_svg(data))
    print(f"Wrote SVGs to {OUT_DIR}/")


if __name__ == "__main__":
    main()
