"""Refresh profile charts; optionally include private language data using authenticated gh."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "stats"
COLORS = {
    "TypeScript": "#3178c6", "JavaScript": "#d4b830", "Python": "#3572a5",
    "Java": "#b07219", "Go": "#00add8", "HTML": "#e34c26", "CSS": "#663399",
    "Ruby": "#701516", "Rust": "#dea584", "Lua": "#5260a9", "Luau": "#00a2ff",
    "Shell": "#89a057", "C#": "#178600", "PLpgSQL": "#336791", "Other": "#8b949e",
}
THEMES = {
    "light": {"bg": "#ffffff", "border": "#d1d9e0", "text": "#24292f",
              "muted": "#59636e", "levels": ["#eff2f6", "#c6dcff", "#8cb8ff", "#468ce4", "#1f6feb"]},
    "dark": {"bg": "#0d1117", "border": "#3d444d", "text": "#f0f6fc",
             "muted": "#9198a1", "levels": ["#161b22", "#193a66", "#245b9c", "#3382cf", "#65b1ff"]},
}


def fetch(url, api=False):
    headers = {"User-Agent": "profile-readme-charts", "Accept-Language": "en-US"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        content = response.read().decode("utf-8")
    return json.loads(content) if api else content


class Calendar(HTMLParser):
    def __init__(self):
        super().__init__()
        self.days = {}
        self.tooltips = {}
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-date" in attrs:
            day = date.fromisoformat(attrs["data-date"]).isoformat()
            self.days[day] = (attrs["id"], int(attrs["data-level"]))
        if tag == "tool-tip":
            self.active = attrs.get("for")
            self.tooltips[self.active] = ""

    def handle_data(self, data):
        if self.active:
            self.tooltips[self.active] += data

    def handle_endtag(self, tag):
        if tag == "tool-tip":
            self.active = None

    def counts(self, start, end):
        output = []
        for offset in range((end - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            if day not in self.days:
                raise ValueError("Contribution calendar missing " + day)
            identifier, level = self.days[day]
            tooltip = self.tooltips.get(identifier, "").strip()
            if tooltip.startswith("No contributions on "):
                count = 0
            else:
                match = re.match(r"^([\d,]+) contributions? on ", tooltip)
                if not match:
                    raise ValueError("Unrecognized contribution count for " + day)
                count = int(match[1].replace(",", ""))
            if level not in range(5) or (count == 0) != (level == 0):
                raise ValueError("Inconsistent contribution intensity for " + day)
            output.append({"date": day, "count": count, "level": level})
        return output


def gh_json(endpoint, paginate=False):
    command = ["gh", "api", "--hostname", "github.com", endpoint]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Unable to run GitHub CLI; check installation and authentication.") from None
    if result.returncode:
        # gh errors can contain private repository names. Keep them out of logs.
        raise RuntimeError("GitHub CLI request failed; check authentication and repository permissions.")
    return json.loads(result.stdout)


def language_data(login, include_private):
    if include_private:
        if gh_json("user")["login"].casefold() != login.casefold():
            raise ValueError("GitHub CLI must be signed in as the profile owner.")
        pages = gh_json("user/repos?affiliation=owner&per_page=100", paginate=True)
        candidates = [repo for page in pages for repo in page]
    else:
        candidates = []
        page = 1
        while True:
            batch = fetch(f"https://api.github.com/users/{quote(login)}/repos?per_page=100&page={page}&type=owner", api=True)
            candidates.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    repositories = [repo["full_name"] for repo in candidates
                    if repo["owner"]["login"].casefold() == login.casefold()
                    and not repo["fork"] and (include_private or not repo["private"])]

    def languages(name):
        owner, name = name.split("/", 1)
        endpoint = f"repos/{quote(owner)}/{quote(name)}/languages"
        return gh_json(endpoint) if include_private else fetch("https://api.github.com/" + endpoint, api=True)

    totals = Counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(languages, repositories):
            for language, size in result.items():
                if type(size) is not int or size < 0:
                    raise ValueError("Invalid language byte count")
                totals[language] += size
    if not sum(totals.values()):
        raise ValueError("No language data returned")
    # Publish only aggregates; repository identifiers remain in memory.
    return {"repository_count": len(repositories),
            "language_scope": "public_and_private" if include_private else "public",
            "languages_updated": datetime.now(timezone.utc).date().isoformat(),
            "languages": dict(totals.most_common())}


def collect(login, include_private=False, preserve_languages=False):
    today = datetime.now(timezone.utc).date()
    # Twelve calendar-week columns, with the current week shown through today.
    start = today - timedelta(days=(today.weekday() + 1) % 7 + 77)
    calendar = Calendar()
    calendar.feed(fetch("https://github.com/users/" + quote(login) + "/contributions"))
    contributions = calendar.counts(start, today)
    snapshot = OUTPUT / "profile-data.json"
    if preserve_languages:
        previous = json.loads(snapshot.read_text(encoding="utf-8"))
        if previous["login"].casefold() != login.casefold():
            raise ValueError("Cached language data belongs to another profile.")
        languages = {key: previous[key] for key in
                     ("repository_count", "language_scope", "languages_updated", "languages")}
    else:
        languages = language_data(login, include_private)
    return {"login": login, "updated": today.isoformat(), **languages, "contributions": contributions}


def label(x, y, text, color, size=12, weight="400", anchor="start"):
    return (f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{escape(str(text))}</text>')


def frame(title, subtitle, body, theme, updated):
    p = THEMES[theme]
    footer = "Updated " + date.fromisoformat(updated).strftime("%d %b %Y")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="350" height="238" viewBox="0 0 350 238" role="img">'
            f'<title>{escape(title)}. {escape(subtitle)}. {footer}.</title>'
            f'<rect x=".5" y=".5" width="349" height="237" rx="6" fill="{p["bg"]}" stroke="{p["border"]}"/>'
            '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif">'
            + label(17, 28, title, p["text"], 15, "600")
            + label(17, 49, subtitle, p["muted"], 11)
            + body + label(17, 222, footer, p["muted"], 10) + "</g></svg>\n")


def language_chart(data, theme):
    p = THEMES[theme]
    totals = sorted(data["languages"].items(), key=lambda item: (-item[1], item[0]))
    total = sum(value for _, value in totals)
    items = totals[:5]
    if len(totals) > 5:
        items.append(("Other", sum(value for _, value in totals[5:])))
    body = '<defs><clipPath id="bar"><rect x="17" y="64" width="316" height="12" rx="5"/></clipPath></defs>'
    body += '<g clip-path="url(#bar)">'
    x = 17
    for name, size in items:
        width = size / total * 316
        color = COLORS.get(name, "#8b949e")
        body += f'<rect x="{x:.3f}" y="64" width="{width:.3f}" height="12" fill="{color}"/>'
        x += width
    body += "</g>"
    for index, (name, size) in enumerate(items):
        y = 99 + index * 20
        body += f'<circle cx="22" cy="{y - 4}" r="4" fill="{COLORS.get(name, "#8b949e")}"/>'
        body += label(34, y, name, p["text"])
        body += label(332, y, f"{size / total:.1%}", p["muted"], anchor="end")
    count = data["repository_count"]
    scope = "public + private" if data["language_scope"] == "public_and_private" else "public"
    return frame("Code by language", f"{count} owned non-fork repos · {scope}",
                 body, theme, data["languages_updated"])


def activity_chart(data, theme):
    p = THEMES[theme]
    days = data["contributions"]
    start = date.fromisoformat(days[0]["date"])
    end = date.fromisoformat(days[-1]["date"])
    total = sum(day["count"] for day in days)
    body = ""
    for row, text in [(1, "Mon"), (3, "Wed"), (5, "Fri")]:
        body += label(17, 86 + row * 16, text, p["muted"], 9)
    previous_month = None
    for day in days:
        current = date.fromisoformat(day["date"])
        column, row = divmod((current - start).days, 7)
        x, y = 53 + column * 23, 75 + row * 16
        if row == 0 and current.month != previous_month:
            body += label(x, 68, current.strftime("%b"), p["muted"], 9)
            previous_month = current.month
        title = f'{day["date"]}: {day["count"]} contributions'
        body += (f'<rect x="{x}" y="{y}" width="13" height="13" rx="2" '
                 f'fill="{p["levels"][day["level"]]}"><title>{title}</title></rect>')
    body += label(167, 201, "Less", p["muted"], 9)
    for index, color in enumerate(p["levels"]):
        body += f'<rect x="{194 + index * 16}" y="191" width="12" height="12" rx="2" fill="{color}"/>'
    body += label(277, 201, "More", p["muted"], 9)
    subtitle = f'{total:,} contributions · {start:%d %b}–{end:%d %b}'
    return frame("Recent contributions", subtitle, body, theme, data["updated"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", default="PatrykBr")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--from-cache", action="store_true", help="Render the saved snapshot without fetching")
    mode.add_argument("--include-private", action="store_true", help="Use authenticated gh to aggregate public and private repositories")
    mode.add_argument("--preserve-languages", action="store_true", help="Refresh activity while retaining the language snapshot and its date")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+", args.login):
        parser.error("Invalid GitHub login")
    snapshot = OUTPUT / "profile-data.json"
    if args.from_cache:
        data = json.loads(snapshot.read_text(encoding="utf-8"))
    else:
        if snapshot.exists() and not (args.include_private or args.preserve_languages):
            previous = json.loads(snapshot.read_text(encoding="utf-8"))
            if previous.get("language_scope") == "public_and_private":
                parser.error("Keep private-inclusive data: use --include-private or --preserve-languages.")
        data = collect(args.login, args.include_private, args.preserve_languages)
    # Build all assets before touching the previous successful set.
    assets = {}
    for theme in THEMES:
        assets[f"languages-{theme}.svg"] = language_chart(data, theme)
        assets[f"activity-{theme}.svg"] = activity_chart(data, theme)
    assets["profile-data.json"] = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        (OUTPUT / name).write_text(content, encoding="utf-8")
    print(f'Updated charts from {data["repository_count"]} owned non-fork repositories '
          f'({data["language_scope"]}); '
          f'{sum(day["count"] for day in data["contributions"]):,} profile contributions in the displayed period.')


if __name__ == "__main__":
    main()
