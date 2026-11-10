import argparse
import json
import os
import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from html import escape
from html.parser import HTMLParser
from itertools import count
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "stats"
ACTIVITY_WEEKS = 12
COLORS = {
    "TypeScript": "#3178c6",
    "JavaScript": "#d4b830",
    "Python": "#3572a5",
    "Java": "#b07219",
    "Go": "#00add8",
    "HTML": "#e34c26",
    "CSS": "#663399",
    "Ruby": "#701516",
    "Rust": "#dea584",
    "Lua": "#5260a9",
    "Luau": "#00a2ff",
    "Shell": "#89a057",
    "C#": "#178600",
    "PLpgSQL": "#336791",
    "Other": "#8b949e",
}
THEMES = {
    "light": {
        "bg": "#ffffff",
        "border": "#d1d9e0",
        "text": "#24292f",
        "muted": "#59636e",
        "levels": ["#eff2f6", "#c6dcff", "#8cb8ff", "#468ce4", "#1f6feb"],
    },
    "dark": {
        "bg": "#0d1117",
        "border": "#3d444d",
        "text": "#f0f6fc",
        "muted": "#9198a1",
        "levels": ["#161b22", "#193a66", "#245b9c", "#3382cf", "#65b1ff"],
    },
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


def gh_json(endpoint, paginate=False, query=None):
    command = ["gh", "api", "--hostname", "github.com", endpoint]
    if paginate:
        command += ["--paginate", "--slurp"]
    if query is not None:
        command += ["-f", "query=" + query]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(
            "Unable to run GitHub CLI; check installation and authentication."
        ) from None
    if result.returncode:
        raise RuntimeError(
            "GitHub CLI request failed; check authentication and repository permissions."
        )
    data = json.loads(result.stdout)
    pages = data if isinstance(data, list) else [data]
    if any(isinstance(page, dict) and page.get("errors") for page in pages):
        raise RuntimeError("GitHub returned incomplete contribution data.")
    return data


def repository_endpoint(name):
    return "repos/" + "/".join(quote(part, safe="") for part in name.split("/"))


def owned_non_fork(repo, login):
    return repo["owner"]["login"].casefold() == login.casefold() and not repo["fork"]


def public_repositories(login):
    repositories = []
    for page in count(1):
        batch = fetch(
            f"https://api.github.com/users/{quote(login)}/repos?per_page=100&page={page}&type=owner",
            api=True,
        )
        repositories.extend(
            repo["full_name"]
            for repo in batch
            if owned_non_fork(repo, login) and not repo["private"]
        )
        if len(batch) < 100:
            return repositories


def contributed_repositories(login):
    if gh_json("user")["login"].casefold() != login.casefold():
        raise ValueError("GitHub CLI must be signed in as the profile owner.")
    pages = gh_json(
        "user/repos?affiliation=owner,collaborator,organization_member&per_page=100",
        paginate=True,
    )
    accessible = {repo["full_name"].casefold(): repo for page in pages for repo in page}
    selected = {
        key: repo["full_name"]
        for key, repo in accessible.items()
        if owned_non_fork(repo, login)
    }
    query = """query($endCursor:String) {
      viewer { repositoriesContributedTo(first:100, after:$endCursor,
        includeUserRepositories:false, contributionTypes:[COMMIT,PULL_REQUEST]) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      } }
    }"""
    for page in gh_json("graphql", paginate=True, query=query):
        for repo in page["data"]["viewer"]["repositoriesContributedTo"]["nodes"]:
            name = repo["nameWithOwner"]
            selected[name.casefold()] = name

    def authored(repo):
        name = repo["full_name"]
        endpoint = repository_endpoint(name)
        if gh_json(endpoint + f"/commits?author={quote(login)}&per_page=1"):
            return name
        return None

    remaining = [
        repo
        for key, repo in accessible.items()
        if key not in selected and repo.get("size", 1) > 0
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name in pool.map(authored, remaining):
            if name:
                selected[name.casefold()] = name
    return sorted(selected.values(), key=str.casefold)


def language_data(login, include_private):
    if include_private:
        repositories = contributed_repositories(login)
    else:
        repositories = public_repositories(login)

    def languages(name):
        endpoint = repository_endpoint(name) + "/languages"
        return (
            gh_json(endpoint)
            if include_private
            else fetch("https://api.github.com/" + endpoint, api=True)
        )

    totals = Counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(languages, repositories):
            if any(type(size) is not int or size < 0 for size in result.values()):
                raise ValueError("Invalid language byte count")
            totals.update(result)
    if not sum(totals.values()):
        raise ValueError("No language data returned")
    return {
        "repository_count": len(repositories),
        "repository_scope": "owned_and_contributed" if include_private else "owned",
        "language_scope": "public_and_private" if include_private else "public",
        "languages_updated": datetime.now(UTC).date().isoformat(),
        "languages": dict(totals.most_common()),
    }


def cached_language_data(login):
    previous = read_snapshot()
    if previous["login"].casefold() != login.casefold():
        raise ValueError("Cached language data belongs to another profile.")
    return {
        **{
            key: previous[key]
            for key in (
                "repository_count",
                "language_scope",
                "languages_updated",
                "languages",
            )
        },
        "repository_scope": previous.get("repository_scope", "owned"),
    }


def collect(login, include_private=False, preserve_languages=False):
    today = datetime.now(UTC).date()
    days_since_sunday = (today.weekday() + 1) % 7
    start = today - timedelta(days=days_since_sunday, weeks=ACTIVITY_WEEKS - 1)
    calendar = Calendar()
    calendar.feed(fetch("https://github.com/users/" + quote(login) + "/contributions"))
    contributions = calendar.counts(start, today)
    if preserve_languages:
        languages = cached_language_data(login)
    else:
        languages = language_data(login, include_private)
    return {
        "login": login,
        "updated": today.isoformat(),
        **languages,
        "contributions": contributions,
    }


def label(x, y, text, color, size=12, weight="400", anchor="start"):
    return (
        f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{escape(str(text))}</text>'
    )


def frame(title, subtitle, body, theme, updated):
    palette = THEMES[theme]
    footer = "Updated " + date.fromisoformat(updated).strftime("%d %b %Y")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="350" height="238" viewBox="0 0 350 238" role="img">'
        f"<title>{escape(title)}. {escape(subtitle)}. {footer}.</title>"
        f'<rect x=".5" y=".5" width="349" height="237" rx="6" fill="{palette["bg"]}" stroke="{palette["border"]}"/>'
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif">'
        + label(17, 28, title, palette["text"], 15, "600")
        + label(17, 49, subtitle, palette["muted"], 11)
        + body
        + label(17, 222, footer, palette["muted"], 10)
        + "</g></svg>\n"
    )


def language_chart(data, theme):
    palette = THEMES[theme]
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
        body += (
            f'<rect x="{x:.3f}" y="64" width="{width:.3f}" height="12" fill="{color}"/>'
        )
        x += width
    body += "</g>"
    for index, (name, size) in enumerate(items):
        y = 99 + index * 20
        body += (
            f'<circle cx="22" cy="{y - 4}" r="4" fill="{COLORS.get(name, "#8b949e")}"/>'
        )
        body += label(34, y, name, palette["text"])
        body += label(332, y, f"{size / total:.1%}", palette["muted"], anchor="end")
    count = data["repository_count"]
    scope = (
        "public + private"
        if data["language_scope"] == "public_and_private"
        else "public"
    )
    coverage = (
        "owned + contributed"
        if data.get("repository_scope") == "owned_and_contributed"
        else "owned non-fork repos"
    )
    return frame(
        "Code by language",
        f"{count} {coverage} · {scope}",
        body,
        theme,
        data["languages_updated"],
    )


def activity_chart(data, theme):
    palette = THEMES[theme]
    days = data["contributions"]
    start = date.fromisoformat(days[0]["date"])
    end = date.fromisoformat(days[-1]["date"])
    total = sum(day["count"] for day in days)
    body = ""
    for row, text in [(1, "Mon"), (3, "Wed"), (5, "Fri")]:
        body += label(17, 86 + row * 16, text, palette["muted"], 9)
    previous_month = None
    for day in days:
        current = date.fromisoformat(day["date"])
        column, row = divmod((current - start).days, 7)
        x, y = 53 + column * 23, 75 + row * 16
        if row == 0 and current.month != previous_month:
            body += label(x, 68, current.strftime("%b"), palette["muted"], 9)
            previous_month = current.month
        title = f"{day['date']}: {day['count']} contributions"
        body += (
            f'<rect x="{x}" y="{y}" width="13" height="13" rx="2" '
            f'fill="{palette["levels"][day["level"]]}"><title>{title}</title></rect>'
        )
    body += label(167, 201, "Less", palette["muted"], 9)
    for index, color in enumerate(palette["levels"]):
        body += f'<rect x="{194 + index * 16}" y="191" width="12" height="12" rx="2" fill="{color}"/>'
    body += label(277, 201, "More", palette["muted"], 9)
    subtitle = f"{total:,} contributions · {start:%d %b}–{end:%d %b}"
    return frame("Recent contributions", subtitle, body, theme, data["updated"])


def read_snapshot():
    return json.loads((OUTPUT / "profile-data.json").read_text(encoding="utf-8"))


def write_assets(data):
    assets = {}
    for theme in THEMES:
        assets[f"languages-{theme}.svg"] = language_chart(data, theme)
        assets[f"activity-{theme}.svg"] = activity_chart(data, theme)
    assets["profile-data.json"] = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        (OUTPUT / name).write_text(content, encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Refresh profile charts; optionally include private language data using authenticated gh."
    )
    parser.add_argument("--login", default="PatrykBr")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--from-cache",
        action="store_true",
        help="Render the saved snapshot without fetching",
    )
    mode.add_argument(
        "--include-private",
        action="store_true",
        help="Use authenticated gh to aggregate public and private repositories",
    )
    mode.add_argument(
        "--preserve-languages",
        action="store_true",
        help="Refresh activity while retaining the language snapshot and its date",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+", args.login):
        parser.error("Invalid GitHub login")
    public_refresh = not (
        args.from_cache or args.include_private or args.preserve_languages
    )
    if (
        public_refresh
        and (OUTPUT / "profile-data.json").exists()
        and read_snapshot().get("language_scope") == "public_and_private"
    ):
        parser.error(
            "Keep private-inclusive data: use --include-private or --preserve-languages."
        )
    return args


def main():
    args = parse_args()
    data = (
        read_snapshot()
        if args.from_cache
        else collect(
            args.login,
            include_private=args.include_private,
            preserve_languages=args.preserve_languages,
        )
    )
    write_assets(data)
    print(
        f"Updated charts from {data['repository_count']} repositories "
        f"({data['language_scope']}); "
        f"{sum(day['count'] for day in data['contributions']):,} profile contributions in the displayed period."
    )


if __name__ == "__main__":
    main()
