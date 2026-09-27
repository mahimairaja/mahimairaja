"""Rebuild the live parts of this profile README.

Runs in GitHub Actions every six hours (see .github/workflows/build.yml) and on demand.
Each section of README.MD sits between a pair of markers,

    <!-- writing starts -->
    ...
    <!-- writing ends -->

and only the text between them is rewritten. It also draws the two SVGs under assets/
(the brand banner, the wordmark and the contribution waveform) and writes contributions.md.

Every source is fetched independently. If one fails (a feed is down, an API rate limit),
that section keeps its previous content and the rest still update, so a bad hour never
blanks the profile.

Standard library only, so the workflow needs no install step.

To test without the network, point README_FIXTURES at a directory of saved responses
(feed.xml, search_1.json, repo__<owner>__<name>.json, pypi__<name>.json, contributions.json):

    README_FIXTURES=/path/to/fixtures python build_readme.py
"""

from __future__ import annotations

import base64
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

# The claw M, from the Mahimai wordmark: three shapes in a 306 x 258 box.
CLAW_M = ('<path d="M 0 1 L 153 113 L 306 0 L 306 33 L 174 133 L 227 106 L 306 53 L 306 78 L 254 117 L 250 106 '
          'L 153 180 L 55 104 L 54 215 L 0 257 Z"/><path d="M 252 174 L 306 141 L 306 227 L 253 258 Z"/>'
          '<path d="M 208 170 L 306 97 L 306 121 Z"/>')
# "ahimai" in Space Grotesk Bold outlines, as (x offset, path) pairs; the claw M leads the word.
WORDMARK_GLYPHS = [
    (813.448, 'M224 -14Q171 -14 129.0 4.5Q87 23 62.5 58.5Q38 94 38 145Q38 196 62.5 230.5Q87 265 130.5 282.5Q174 300 230 300H366V328Q366 363 344.0 385.5Q322 408 274 408Q227 408 204.0 386.5Q181 365 174 331L58 370Q70 408 96.5 439.5Q123 471 167.5 490.5Q212 510 276 510Q374 510 431.0 461.0Q488 412 488 319V134Q488 104 516 104H556V0H472Q435 0 411.0 18.0Q387 36 387 66V67H368Q364 55 350.0 35.5Q336 16 306.0 1.0Q276 -14 224 -14ZM246 88Q299 88 332.5 117.5Q366 147 366 196V206H239Q204 206 184.0 191.0Q164 176 164 149Q164 122 185.0 105.0Q206 88 246 88Z'),
    (1369.448, 'M70 0V700H196V435H214Q222 451 239.0 467.0Q256 483 284.5 493.5Q313 504 357 504Q415 504 458.5 477.5Q502 451 526.0 404.5Q550 358 550 296V0H424V286Q424 342 396.5 370.0Q369 398 318 398Q260 398 228.0 359.5Q196 321 196 252V0Z'),
    (1963.448, 'M70 0V496H196V0ZM133 554Q99 554 75.5 576.0Q52 598 52 634Q52 670 75.5 692.0Q99 714 133 714Q168 714 191.0 692.0Q214 670 214 634Q214 598 191.0 576.0Q168 554 133 554Z'),
    (2207.448, 'M70 0V496H194V442H212Q225 467 255.0 485.5Q285 504 334 504Q387 504 419.0 483.5Q451 463 468 430H486Q503 462 534.0 483.0Q565 504 622 504Q668 504 705.5 484.5Q743 465 765.5 425.5Q788 386 788 326V0H662V317Q662 358 641.0 378.5Q620 399 582 399Q539 399 515.5 371.5Q492 344 492 293V0H366V317Q366 358 345.0 378.5Q324 399 286 399Q243 399 219.5 371.5Q196 344 196 293V0Z'),
    (3039.448, 'M224 -14Q171 -14 129.0 4.5Q87 23 62.5 58.5Q38 94 38 145Q38 196 62.5 230.5Q87 265 130.5 282.5Q174 300 230 300H366V328Q366 363 344.0 385.5Q322 408 274 408Q227 408 204.0 386.5Q181 365 174 331L58 370Q70 408 96.5 439.5Q123 471 167.5 490.5Q212 510 276 510Q374 510 431.0 461.0Q488 412 488 319V134Q488 104 516 104H556V0H472Q435 0 411.0 18.0Q387 36 387 66V67H368Q364 55 350.0 35.5Q336 16 306.0 1.0Q276 -14 224 -14ZM246 88Q299 88 332.5 117.5Q366 147 366 196V206H239Q204 206 184.0 191.0Q164 176 164 149Q164 122 185.0 105.0Q206 88 246 88Z'),
    (3595.448, 'M70 0V496H196V0ZM133 554Q99 554 75.5 576.0Q52 598 52 634Q52 670 75.5 692.0Q99 714 133 714Q168 714 191.0 692.0Q214 670 214 634Q214 598 191.0 576.0Q168 554 133 554Z'),
]
BRAND = ASSETS / 'brand'


def wordmark_svg(fill: str) -> str:
    glyphs = ''.join(f'<path transform="translate({x} 700) scale(1 -1)" d="{d}"/>' for x, d in WORDMARK_GLYPHS)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -12 3847.448 730" role="img" aria-label="Mahimai">'
            f'<g fill="{fill}"><g transform="translate(55 92) scale(2)">{CLAW_M}</g>{glyphs}</g></svg>\n')


def banner_svg() -> str:
    """The brand header, after the Mahimai banner: the claw M, the line, and a signal that runs
    CAPTURE, UNDERSTAND, RESPOND, DELIVER into the panther. One dark card in both themes."""
    t = THEMES['dark']
    panther = base64.b64encode((BRAND / 'panther.jpg').read_bytes()).decode()
    y, x0, x1, cycle = 300, 40, 822, 6.0
    stages = [('CAPTURE', 196, 'circle'), ('UNDERSTAND', 384, 'square'), ('RESPOND', 572, 'triangle'), ('DELIVER', 744, 'dot')]

    def shape(kind: str, x: int) -> str:
        if kind == 'circle':
            return f'<circle cx="{x}" cy="{y}" r="8"/>'
        if kind == 'square':
            return f'<rect x="{x - 8}" y="{y - 8}" width="16" height="16"/>'
        if kind == 'triangle':
            return f'<path d="M {x} {y - 9} L {x + 9} {y + 7} L {x - 9} {y + 7} Z"/>'
        return f'<circle class="solid" cx="{x}" cy="{y}" r="5"/>'

    # Each stage flashes as the pulse reaches it.
    nodes = ''.join(
        f'<g class="node" style="animation-delay:{(x - x0) / (x1 - x0) * cycle:.2f}s">{shape(kind, x)}</g>'
        f'<text class="stage" x="{x}" y="{y - 30}" text-anchor="middle">{label}</text>'
        for label, x, kind in stages)
    # A short burst of speech before CAPTURE: a sine under a bell-shaped envelope.
    pts = ' '.join(f'{70 + i * 1.8:.1f},{y - 24 * math.sin(i * 0.9) * math.exp(-((i - 30) / 16) ** 2):.1f}'
                   for i in range(66))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="1200" height="400" viewBox="0 0 1200 400" role="img" aria-label="Mahimai: build voice products that keep working. From prototype to production.">
<defs>
  <clipPath id="card"><rect width="1200" height="400" rx="20"/></clipPath>
  <linearGradient id="fade" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="0.28" stop-color="#fff"/></linearGradient>
  <mask id="soft"><rect x="780" y="0" width="420" height="400" fill="url(#fade)"/></mask>
  <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="{t['accent']}" stop-opacity="0.9"/><stop offset="1" stop-color="{t['accent']}" stop-opacity="0"/></radialGradient>
</defs>
<style>
  .eyebrow {{ font: 500 15px {MONO}; fill: {t['muted']}; letter-spacing: 1.5px; }}
  .h1 {{ font: 700 50px {SANS}; fill: {t['ink']}; letter-spacing: -1px; }}
  .sub {{ font: 400 24px {SANS}; fill: #8a8a8a; }}
  .stage {{ font: 500 13px {MONO}; fill: {t['muted']}; letter-spacing: 1px; }}
  .grid {{ stroke: #1c1c1c; fill: none; }}
  .wire {{ stroke: {t['accent']}; stroke-width: 1.6; fill: none; opacity: 0.8; }}
  .speech {{ stroke: {t['accent']}; stroke-width: 1.8; fill: none; }}
  .node {{ fill: {t['bg']}; stroke: #d4d4d4; stroke-width: 1.6; animation: hit {cycle}s linear infinite; }}
  .node .solid {{ fill: {t['accent']}; stroke: none; }}
  .pulse {{ animation: run {cycle}s linear infinite; }}
  @keyframes run {{ from {{ transform: translateX(0); }} to {{ transform: translateX({x1 - x0}px); }} }}
  @keyframes hit {{ 0% {{ stroke: {t['accent']}; }} 10% {{ stroke: #d4d4d4; }} 100% {{ stroke: #d4d4d4; }} }}
  @media (prefers-reduced-motion: reduce) {{ .pulse {{ display: none; }} .node {{ animation: none; }} }}
</style>
<g clip-path="url(#card)">
  <rect width="1200" height="400" fill="{t['bg']}"/>
  <circle class="grid" cx="20" cy="330" r="120"/><circle class="grid" cx="1080" cy="150" r="110"/>
  <line class="grid" x1="0" x2="1200" y1="{y + 70}" y2="{y + 70}"/><line class="grid" x1="960" x2="960" y1="0" y2="400"/>
  <image x="784" y="68" width="416" height="400" href="data:image/jpeg;base64,{panther}" xlink:href="data:image/jpeg;base64,{panther}" mask="url(#soft)"/>
  <g transform="translate(64 55) scale(0.078)" fill="{t['ink']}">{CLAW_M}</g>
  <text class="eyebrow" x="100" y="71">MAHIMAI · VOICE AI PRODUCT ENGINEERING</text>
  <text class="h1" x="64" y="140">Build voice products</text>
  <text class="h1" x="64" y="196">that keep working.</text>
  <text class="sub" x="64" y="236">From prototype to production.</text>
  <path class="wire" d="M {x0} {y} H {x1} C {x1 + 10} {y} {x1 + 14} {y - 4} {x1 + 22} {y - 6}"/>
  <polyline class="speech" points="{pts}"/>
  {nodes}
  <g class="pulse"><circle cx="{x0}" cy="{y}" r="16" fill="url(#glow)" opacity="0.6"/><circle cx="{x0}" cy="{y}" r="3.5" fill="#fafafa"/></g>
</g>
<rect x="0.5" y="0.5" width="1199" height="399" rx="20" fill="none" stroke="{t['line']}"/>
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
    (ASSETS / 'banner.svg').write_text(banner_svg())
    for theme, t in THEMES.items():
        (ASSETS / f'wordmark-{theme}.svg').write_text(wordmark_svg(t['ink']))

    posts = section('writing', blog_posts)
    prs = section('upstream', merged_upstream_prs)
    pkgs = section('packages', pypi_releases)
    refs = section('references', references)
    days = section('waveform', contribution_days)

    # None means the source failed and the section keeps what it had; an empty list is a real
    # answer (no posts, no qualifying PRs) and replaces stale content.
    if posts is not None:
        readme = replace_chunk(readme, 'writing', writing_md(posts))
    if prs is not None:
        readme = replace_chunk(readme, 'upstream', upstream_md(prs))
        readme = replace_chunk(readme, 'upstream_count', f'{len(prs)}', inline=True)
        readme = replace_chunk(readme, 'upstream_projects', f'{len({p["repo"] for p in prs})}', inline=True)
        CONTRIBUTIONS.write_text(contributions_page(prs) + '\n')
    if pkgs is not None:
        readme = replace_chunk(readme, 'releases', releases_md(pkgs))
        readme = replace_chunk(readme, 'packages', packages_md(pkgs))
    if refs is not None:
        readme = replace_chunk(readme, 'references', references_md(refs))
    if days is not None:
        since = date.today() - timedelta(days=365)
        total = sum(c for d, c in days if d > since)
        summary = f'{total:,} contributions in the last 12 months'
        for theme, t in THEMES.items():
            (ASSETS / f'waveform-{theme}.svg').write_text(waveform_svg(t, days, summary))

    README.write_text(readme)
    print('Updated:', ', '.join(n for n, v in [('writing', posts), ('upstream', prs), ('packages', pkgs),
                                              ('references', refs), ('waveform', days)] if v is not None) or 'nothing')


if __name__ == '__main__':
    main()
