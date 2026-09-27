"""Rebuild the live parts of this profile README.

Runs in GitHub Actions every six hours (see .github/workflows/build.yml) and on demand.
Each section of README.MD sits between a pair of markers,

    <!-- writing starts -->
    ...
    <!-- writing ends -->

and only the text between them is rewritten. It also draws the two SVGs under assets/
(the header banner and the contribution waveform) and writes contributions.md.

Every source is fetched independently. If one fails (a feed is down, an API rate limit),
that section keeps its previous content and the rest still update, so a bad hour never
blanks the profile.

Standard library only, so the workflow needs no install step.

To test without the network, point README_FIXTURES at a directory of saved responses
(feed.xml, search_1.json, repo__<owner>__<name>.json, pypi__<name>.json, contributions.json):

    README_FIXTURES=/path/to/fixtures python build_readme.py
"""

from __future__ import annotations

import html
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).parent.resolve()
README = ROOT / 'README.MD'
CONTRIBUTIONS = ROOT / 'contributions.md'
ASSETS = ROOT / 'assets'

USER = 'mahimairaja'
# Pull requests to repositories under these owners are my own work, not upstream contributions.
OWN_OWNERS = {'mahimairaja', 'mahimailabs'}
# An upstream contribution counts when the project has at least this many stars. It keeps
# hackathon and team repositories out of the list without maintaining a list by hand.
UPSTREAM_MIN_STARS = 100
FEED_URL = 'https://mahimai.ca/feed.xml'

# The packages table. The descriptions are mine; the version and date come from PyPI.
PACKAGES = [
    ('voicegateway', 'Observability and inference routing for voice AI'),
    ('openrtc', 'Shared memory layer for multi-agent LiveKit workers'),
    ('envoic', 'Horizontal voice AI infrastructure toolkit'),
    ('fastrtc-whisper-cpp', 'whisper.cpp STT backend for FastRTC'),
    ('fastrtc-canary', 'NVIDIA Canary STT backend for FastRTC'),
    ('vapiserve', 'Custom tool server for Vapi'),
    ('locallens', 'Local inference tooling'),
]

# The reference lists I maintain, each with its own site.
REFERENCES = [
    ('mahimairaja/voiceai', 'Voice AI', 'https://voiceai.mahimai.ca'),
    ('mahimairaja/realtime', 'Realtime Voice', 'https://realtime.mahimai.ca'),
    ('mahimairaja/tts', 'Awesome TTS', 'https://tts.mahimai.ca'),
    ('mahimailabs/voice-prices', 'Voice Prices', 'https://prices.mahimai.ca'),
    ('mahimailabs/voice-ai-skills', 'Voice AI Skills', 'https://skills.mahimai.ca'),
]

# The palette of mahimai.ca and the reference sites: lavender on near-black, deepened for light.
THEMES = {
    'dark': {'bg': '#0a0a0a', 'surface': '#141414', 'line': '#262626', 'ink': '#fafafa', 'muted': '#a3a3a3',
             'accent': '#cba6f7', 'accent2': '#86efac', 'blue': '#89b4fa'},
    'light': {'bg': '#fcfcfc', 'surface': '#f3f3f4', 'line': '#e3e3e6', 'ink': '#0a0a0a', 'muted': '#55555c',
              'accent': '#6f3fb0', 'accent2': '#15803d', 'blue': '#1e66f5'},
}
SANS = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"

FIXTURES = os.environ.get('README_FIXTURES')
TOKEN = os.environ.get('GITHUB_TOKEN', '')


# --- Fetching -------------------------------------------------------------------------------

def fetch(url: str, *, data: bytes | None = None, accept: str = 'application/json') -> bytes:
    """GET (or POST) with retries on the transient errors GitHub and PyPI return under load."""
    headers = {'User-Agent': f'{USER}-profile-readme', 'Accept': accept}
    if TOKEN and url.startswith('https://api.github.com/'):
        headers['Authorization'] = f'Bearer {TOKEN}'
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
        except urllib.error.URLError:
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError('unreachable')


def fixture(name: str) -> bytes | None:
    if not FIXTURES:
        return None
    return (pathlib.Path(FIXTURES) / name).read_bytes()


def get_json(url: str, fixture_name: str):
    raw = fixture(fixture_name)
    return json.loads(raw if raw is not None else fetch(url))


# --- Sources --------------------------------------------------------------------------------

def blog_posts(limit: int = 5) -> list[dict]:
    raw = fixture('feed.xml')
    root = ET.fromstring(raw if raw is not None else fetch(FEED_URL, accept='application/rss+xml'))
    posts = []
    for item in root.iter('item'):
        published = datetime.strptime(item.findtext('pubDate', '').strip(), '%a, %d %b %Y %H:%M:%S %Z')
        posts.append({'title': item.findtext('title', '').strip(), 'url': item.findtext('link', '').strip(),
                      'date': published.date().isoformat()})
    posts.sort(key=lambda p: p['date'], reverse=True)
    return posts[:limit]


def repo_stars(full_name: str, cache: dict[str, int]) -> int:
    if full_name not in cache:
        slug = full_name.replace('/', '__')
        cache[full_name] = get_json(f'https://api.github.com/repos/{full_name}', f'repo__{slug}.json')['stargazers_count']
    return cache[full_name]


def merged_upstream_prs() -> list[dict]:
    """Every merged PR to someone else's repository, newest merge first, with the repo's stars."""
    query = f'is:pr is:merged author:{USER} ' + ' '.join(f'-user:{o}' for o in sorted(OWN_OWNERS))
    items = []
    for page in range(1, 11):  # search stops at 1,000 results
        url = 'https://api.github.com/search/issues?' + urllib.parse.urlencode(
            {'q': query, 'per_page': 100, 'page': page})
        batch = get_json(url, f'search_{page}.json') if not FIXTURES else (
            json.loads(fixture('search_1.json')) if page == 1 else {'items': []})
        items.extend(batch['items'])
        if len(batch['items']) < 100:
            break
    stars: dict[str, int] = {}
    prs = []
    for it in items:
        repo = it['repository_url'].removeprefix('https://api.github.com/repos/')
        if repo.split('/')[0] in OWN_OWNERS:
            continue
        merged = (it.get('pull_request') or {}).get('merged_at') or it['closed_at']
        prs.append({'repo': repo, 'number': it['number'], 'title': it['title'].strip(), 'url': it['html_url'],
                    'date': merged[:10], 'stars': repo_stars(repo, stars)})
    prs.sort(key=lambda p: p['date'], reverse=True)
    return [p for p in prs if p['stars'] >= UPSTREAM_MIN_STARS]


def pypi_releases() -> list[dict]:
    out = []
    for name, description in PACKAGES:
        d = get_json(f'https://pypi.org/pypi/{name}/json', f'pypi__{name}.json')
        version = d['info']['version']
        uploads = [f['upload_time_iso_8601'] for f in d['releases'].get(version, [])]
        out.append({'name': name, 'description': description, 'version': version,
                    'date': min(uploads)[:10] if uploads else '', 'releases': len(d['releases'])})
    return out


def references() -> list[dict]:
    out = []
    for full_name, title, site in REFERENCES:
        d = get_json(f'https://api.github.com/repos/{full_name}', f'repo__{full_name.replace("/", "__")}.json')
        out.append({'repo': full_name, 'title': title, 'site': site, 'stars': d['stargazers_count'],
                    'updated': d['pushed_at'][:10], 'url': d['html_url']})
    return out


def contribution_days() -> list[tuple[date, int]]:
    """The last year of the contribution calendar, one (day, count) per day, oldest first."""
    raw = fixture('contributions.json')
    if raw is None and TOKEN:
        try:
            q = ('query { user(login: "%s") { contributionsCollection { contributionCalendar '
                 '{ weeks { contributionDays { date contributionCount } } } } } }' % USER)
            body = json.loads(fetch('https://api.github.com/graphql', data=json.dumps({'query': q}).encode()))
            weeks = body['data']['user']['contributionsCollection']['contributionCalendar']['weeks']
            return [(date.fromisoformat(d['date']), d['contributionCount']) for w in weeks for d in w['contributionDays']]
        except Exception as e:  # fall back to the public calendar page
            print(f'  GraphQL calendar unavailable ({e}); reading the public calendar page')
    if raw is not None:
        return [(date.fromisoformat(k), v) for k, v in json.loads(raw)]
    page = fetch(f'https://github.com/users/{USER}/contributions', accept='text/html').decode()
    counts = {}
    for tip_for, text in re.findall(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]*)</tool-tip>', page):
        m = re.match(r'\s*(\d+|No) contribution', text)
        counts[tip_for] = 0 if not m or m.group(1) == 'No' else int(m.group(1))
    days = []
    for cell in re.findall(r'<td\b[^>]*\bdata-date="[^"]+"[^>]*>', page):
        attr = dict(re.findall(r'([\w-]+)="([^"]*)"', cell))
        # The tooltip carries the exact count; the cell's shade level (0 to 4) is the fallback.
        count = counts.get(attr.get('id', ''), int(attr.get('data-level', 0) or 0))
        days.append((date.fromisoformat(attr['data-date']), count))
    if not days:
        raise RuntimeError('contribution calendar markup not recognised')
    return sorted(days)


# --- Rendering: markdown --------------------------------------------------------------------

def replace_chunk(content: str, marker: str, chunk: str, inline: bool = False) -> str:
    pattern = re.compile(rf'<!-- {marker} starts -->.*?<!-- {marker} ends -->', re.DOTALL)
    if not pattern.search(content):
        raise KeyError(f'marker "{marker}" not found in README.MD')
    body = chunk if inline else f'\n{chunk}\n'
    return pattern.sub(lambda _: f'<!-- {marker} starts -->{body}<!-- {marker} ends -->', content)


def md_escape(text: str) -> str:
    # Only what would break a link or read as HTML; backticks stay, so code in a PR title renders.
    return re.sub(r'([\[\]<>*])', r'\\\1', text)


def writing_md(posts):
    return '\n\n'.join(f'[{md_escape(p["title"])}]({p["url"]})<br><sub>{p["date"]}</sub>' for p in posts)


def upstream_md(prs, limit=5):
    return '\n\n'.join(
        f'[{md_escape(p["title"])}]({p["url"]})<br><sub>{p["repo"]} #{p["number"]} · {p["date"]}</sub>'
        for p in prs[:limit])


def releases_md(pkgs, limit=5):
    latest = sorted((p for p in pkgs if p['date']), key=lambda p: p['date'], reverse=True)[:limit]
    return '\n\n'.join(
        f'[{p["name"]} {p["version"]}](https://pypi.org/project/{p["name"]}/{p["version"]}/)<br><sub>{p["date"]}</sub>'
        for p in latest)


def references_md(refs):
    return '\n\n'.join(
        f'[{r["title"]}]({r["site"]}) · [repo]({r["url"]})<br><sub>★ {r["stars"]:,} · updated {r["updated"]}</sub>'
        for r in refs)


def packages_md(pkgs):
    rows = ['| Package | What it does | Latest | Downloads |', '|---|---|---|---|']
    for p in pkgs:
        badge = (f'[![PyPI Downloads](https://static.pepy.tech/personalized-badge/{p["name"]}?period=total'
                 f'&units=INTERNATIONAL_SYSTEM&left_color=ORANGE&right_color=BLUE&left_text=downloads)]'
                 f'(https://pepy.tech/projects/{p["name"]})')
        latest = f'[{p["version"]}](https://pypi.org/project/{p["name"]}/) · {p["date"]}' if p['date'] else p['version']
        rows.append(f'| **{p["name"]}** | {p["description"]} | {latest} | {badge} |')
    return '\n'.join(rows)


def contributions_page(prs):
    by_repo: dict[str, list[dict]] = {}
    for p in prs:
        by_repo.setdefault(p['repo'], []).append(p)
    order = sorted(by_repo, key=lambda r: (-by_repo[r][0]['stars'], r.lower()))
    lines = [
        '# Open-source contributions',
        '',
        f'Pull requests I wrote that were merged into other people\'s projects with at least '
        f'{UPSTREAM_MIN_STARS} stars: **{len(prs)}** across **{len(order)}** projects, newest first within each. '
        'Rebuilt by [build_readme.py](build_readme.py).',
        '',
    ]
    for repo in order:
        rows = by_repo[repo]
        lines.append(f'### [{repo}](https://github.com/{repo}) · ★ {rows[0]["stars"]:,}')
        lines.append('')
        lines.extend(f'- [#{p["number"]} {md_escape(p["title"])}]({p["url"]}) · {p["date"]}' for p in rows)
        lines.append('')
    return '\n'.join(lines)


# --- Rendering: SVG --------------------------------------------------------------------------

def banner_svg(t: dict) -> str:
    """Header card: name, role, a cycling line of what I build with, and a breathing waveform."""
    stack = ['LiveKit', 'Pipecat', 'TTS and STT models', 'WebRTC and SIP']
    cycle = 12
    words = ''.join(
        f'<text class="word" style="animation-delay:{i * cycle / len(stack):.2f}s" x="120" y="182">{w}</text>'
        for i, w in enumerate(stack))
    bars, n = [], 44
    for i in range(n):
        h = 18 + 70 * abs(math.sin(i * 0.55) * math.cos(i * 0.21))
        x = 780 + i * 8.6
        bars.append(f'<rect class="bar" style="animation-delay:{-i * 0.09:.2f}s" x="{x:.1f}" y="{120 - h / 2:.1f}" '
                    f'width="4" height="{h:.1f}" rx="2"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="240" viewBox="0 0 1200 240" role="img" aria-label="Mahimai Raja, Voice AI Engineer">
<defs>
  <linearGradient id="wave" gradientUnits="userSpaceOnUse" x1="780" x2="1160" y1="0" y2="0"><stop offset="0" stop-color="{t['accent']}"/><stop offset="1" stop-color="{t['blue']}"/></linearGradient>
  <radialGradient id="glow" cx="0.78" cy="0.5" r="0.45"><stop offset="0" stop-color="{t['accent']}" stop-opacity="0.18"/><stop offset="1" stop-color="{t['accent']}" stop-opacity="0"/></radialGradient>
</defs>
<style>
  .name {{ font: 700 56px {SANS}; fill: {t['ink']}; letter-spacing: -1px; }}
  .role {{ font: 500 23px {SANS}; fill: {t['muted']}; }}
  .lead {{ font: 600 20px {MONO}; fill: {t['muted']}; }}
  .word {{ font: 600 20px {MONO}; fill: {t['accent']}; opacity: 0; animation: word {cycle}s infinite; }}
  .mark {{ fill: {t['accent']}; }}
  .bar {{ fill: url(#wave); transform-box: fill-box; transform-origin: center; animation: breathe 1.8s ease-in-out infinite; }}
  @keyframes word {{ 0% {{ opacity: 0; }} 3% {{ opacity: 1; }} 22% {{ opacity: 1; }} 25% {{ opacity: 0; }} 100% {{ opacity: 0; }} }}
  @keyframes breathe {{ 0%, 100% {{ transform: scaleY(0.35); opacity: 0.6; }} 50% {{ transform: scaleY(1); opacity: 1; }} }}
  @media (prefers-reduced-motion: reduce) {{ .bar {{ animation: none; }} .word {{ animation: none; }} .word:first-of-type {{ opacity: 1; }} }}
</style>
<rect width="1200" height="240" rx="20" fill="{t['surface']}" stroke="{t['line']}"/>
<rect width="1200" height="240" rx="20" fill="url(#glow)"/>
<g class="mark"><rect x="48" y="66" width="6" height="18" rx="3"/><rect x="58" y="56" width="6" height="38" rx="3"/><rect x="68" y="63" width="6" height="24" rx="3"/><rect x="78" y="70" width="6" height="10" rx="3"/></g>
<text class="name" x="96" y="95">Mahimai Raja</text>
<text class="role" x="96" y="136">Voice AI Engineer · Founder, Mahimai AI</text>
<text class="lead" x="96" y="182">&gt;</text>
{words}
{''.join(bars)}
</svg>
'''


def waveform_svg(t: dict, days: list[tuple[date, int]], summary: str) -> str:
    """A year of contributions drawn as audio: one mirrored bar per week, with a playhead."""
    weeks: list[tuple[date, int]] = []
    for d, c in days:
        if not weeks or (d - weeks[-1][0]).days >= 7:
            weeks.append((d, c))
        else:
            weeks[-1] = (weeks[-1][0], weeks[-1][1] + c)
    weeks = weeks[-53:]
    peak = max((c for _, c in weeks), default=0) or 1
    width, height, left, right, mid, half = 1200, 260, 48, 1152, 134, 72
    step = (right - left) / max(len(weeks), 1)
    sweep = 9.0
    bars, labels, last_month, last_label = [], [], None, -9
    for i, (start, count) in enumerate(weeks):
        h = 2 if count == 0 else max(4, math.sqrt(count / peak) * half)
        x = left + i * step + step * 0.18
        delay = i / max(len(weeks), 1) * sweep
        bars.append(f'<rect class="bar{" zero" if count == 0 else ""}" style="animation-delay:{delay:.2f}s" '
                    f'x="{x:.1f}" y="{mid - h:.1f}" width="{step * 0.64:.1f}" height="{2 * h:.1f}" rx="{min(3, step * 0.3):.1f}">'
                    f'<title>Week of {start.isoformat()}: {count} contributions</title></rect>')
        # A month label where the month turns, if the last one is at least three weeks back.
        if start.month != last_month and i - last_label >= 3 and i < len(weeks) - 2:
            labels.append(f'<text class="axis" x="{x:.1f}" y="{height - 16}">{start.strftime("%b")}</text>')
            last_label = i
        last_month = start.month
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(summary)}">
<style>
  .bar {{ fill: url(#wave); opacity: 0.6; animation: lit {sweep}s linear infinite; }}
  .zero {{ fill: {t['line']}; opacity: 1; animation: none; }}
  .axis {{ font: 500 15px {MONO}; fill: {t['muted']}; }}
  .title {{ font: 600 21px {SANS}; fill: {t['ink']}; }}
  .sub {{ font: 500 17px {MONO}; fill: {t['muted']}; }}
  .head {{ stroke: {t['accent2']}; stroke-width: 2; animation: sweep {sweep}s linear infinite; }}
  @keyframes lit {{ 0% {{ opacity: 1; }} 12% {{ opacity: 0.6; }} 100% {{ opacity: 0.6; }} }}
  @keyframes sweep {{ from {{ transform: translateX(0); }} to {{ transform: translateX({right - left}px); }} }}
  @media (prefers-reduced-motion: reduce) {{ .bar, .head {{ animation: none; }} .bar {{ opacity: 0.85; }} .head {{ display: none; }} }}
</style>
<defs><linearGradient id="wave" gradientUnits="userSpaceOnUse" x1="{left}" x2="{right}" y1="0" y2="0"><stop offset="0" stop-color="{t['accent']}"/><stop offset="1" stop-color="{t['blue']}"/></linearGradient></defs>
<rect width="{width}" height="{height}" rx="20" fill="{t['surface']}" stroke="{t['line']}"/>
<text class="title" x="{left}" y="40">The last year, drawn as a waveform</text>
<text class="sub" x="{right}" y="40" text-anchor="end">{html.escape(summary)}</text>
<line x1="{left}" x2="{right}" y1="{mid}" y2="{mid}" stroke="{t['line']}"/>
{''.join(bars)}
<line class="head" x1="{left}" x2="{left}" y1="{mid - half - 8}" y2="{mid + half + 8}"/>
{''.join(labels)}
</svg>
'''


# --- Main -----------------------------------------------------------------------------------

def section(name: str, build):
    """Run one section; on failure, report it and keep what the README already has."""
    try:
        return build()
    except Exception as e:
        print(f'! {name}: {type(e).__name__}: {e} (keeping the previous content)', file=sys.stderr)
        return None


def main() -> None:
    readme = README.read_text()
    ASSETS.mkdir(exist_ok=True)
    for theme, t in THEMES.items():
        (ASSETS / f'banner-{theme}.svg').write_text(banner_svg(t))

    posts = section('writing', blog_posts)
    prs = section('upstream', merged_upstream_prs)
    pkgs = section('packages', pypi_releases)
    refs = section('references', references)
    days = section('waveform', contribution_days)

    if posts:
        readme = replace_chunk(readme, 'writing', writing_md(posts))
    if prs:
        readme = replace_chunk(readme, 'upstream', upstream_md(prs))
        readme = replace_chunk(readme, 'upstream_count', f'{len(prs)}', inline=True)
        readme = replace_chunk(readme, 'upstream_projects', f'{len({p["repo"] for p in prs})}', inline=True)
        CONTRIBUTIONS.write_text(contributions_page(prs) + '\n')
    if pkgs:
        readme = replace_chunk(readme, 'releases', releases_md(pkgs))
        readme = replace_chunk(readme, 'packages', packages_md(pkgs))
    if refs:
        readme = replace_chunk(readme, 'references', references_md(refs))
    if days:
        since = date.today() - timedelta(days=365)
        total = sum(c for d, c in days if d > since)
        summary = f'{total:,} contributions in the last 12 months'
        for theme, t in THEMES.items():
            (ASSETS / f'waveform-{theme}.svg').write_text(waveform_svg(t, days, summary))

    README.write_text(readme)
    print('Updated:', ', '.join(n for n, v in [('writing', posts), ('upstream', prs), ('packages', pkgs),
                                              ('references', refs), ('waveform', days)] if v) or 'nothing')


if __name__ == '__main__':
    main()
