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
import sys
import urllib.error
import urllib.request
from collections import Counter
from xml.sax.saxutils import escape

USER = os.environ.get("GH_USER", "ajshahria")
TOKEN = os.environ.get("GH_TOKEN", "")
TZ_OFFSET = float(os.environ.get("TZ_OFFSET", "6"))
OUT_DIR = os.environ.get("OUT_DIR", "assets")
DISPLAY_NAME = os.environ.get("DISPLAY_NAME", "Abdul Julil Shahria")  # name shown on the card
MAX_COMMIT_PAGES = 30  # per repo, 100 commits per page

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


def fetch_commits(repos):
    """Commit hours (local time) and commit counts per repo language."""
    hours = Counter()
    by_lang = Counter()
    for repo in repos:
        for page in range(1, MAX_COMMIT_PAGES + 1):
            url = (f"https://api.github.com/repos/{repo['full_name']}/commits"
                   f"?author={USER}&per_page=100&page={page}")
            try:
                commits = api(url)
            except urllib.error.HTTPError as err:
                if err.code in (409, 404):  # empty or unavailable repo
                    break
                raise
            if not commits:
                break
            for c in commits:
                stamp = c["commit"]["author"]["date"].replace("Z", "+00:00")
                local = dt.datetime.fromisoformat(stamp) + dt.timedelta(hours=TZ_OFFSET)
                hours[local.hour] += 1
                if repo.get("language"):
                    by_lang[repo["language"]] += 1
            if len(commits) < 100:
                break
    return [hours.get(h, 0) for h in range(24)], by_lang


def collect_live():
    profile = api(f"https://api.github.com/users/{USER}")
    created = dt.datetime.fromisoformat(profile["created_at"].replace("Z", "+00:00"))
    repos = api(f"https://api.github.com/users/{USER}/repos?type=owner&per_page=100")
    own = [r for r in repos if not r["fork"]]
    # The public profile graph is the source of truth: it matches the totals on github.com exactly.
    days = scrape_calendar(created)
    hours, commit_langs = fetch_commits(own)
    repo_langs = Counter(r["language"] for r in own if r["language"])
    return {
        "name": DISPLAY_NAME or profile.get("name") or USER,
        "bio": profile.get("bio") or "",
        "created": created.date(),
        "days": days,
        "hours": hours,
        "repo_langs": repo_langs,
        "commit_langs": commit_langs,
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

    greens = ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]
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
            fill = "#a21caf" if in_streak else greens[LEVELS.get(d, level(c))]
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
  @keyframes pulse {{ 0%,100% {{ fill: #a21caf; }} 50% {{ fill: #e879f9; }} }}
</style>
<rect width="{W}" height="{H}" rx="14" fill="#0d1117"/>
{"".join(months)}
{"".join(rects)}
<text x="{ox}" y="{legend_y + 11}" class="m">{total_year:,} contributions in the last year{streak_note}</text>
{"".join(legend)}
</svg>
'''


def main():
    data = collect_demo() if "--demo" in sys.argv else collect_live()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "github-stats.svg"), "w", encoding="utf-8") as f:
        f.write(stats_svg(data))
    with open(os.path.join(OUT_DIR, "contribution-graph.svg"), "w", encoding="utf-8") as f:
        f.write(graph_svg(data))
    print(f"Wrote SVGs to {OUT_DIR}/")


if __name__ == "__main__":
    main()
