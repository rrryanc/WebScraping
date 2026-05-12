#!/usr/bin/env python3
"""
Fetch pricing.mdx from foxglove/app and update the PRICING:START…PRICING:END
block in mockup.html in-place.

Requires FOXGLOVE_GITHUB_TOKEN env var with read access to foxglove/app.
"""
import json
import os
import re
import sys
from datetime import date
from urllib.request import urlopen, Request
from urllib.error import HTTPError

REPO     = "foxglove/app"
PATH     = "packages/docs/docs/pricing.mdx"
BRANCH   = "main"
HTML_OUT = os.path.join(os.path.dirname(__file__), "..", "mockup.html")

SECTION_MAP = {
    "Storage":   "storage",
    "Query":     "query",
    "Indexing":  "indexing",
    "Bandwidth": "bandwidth",
}
KEY_ORDER = ["indexing", "query", "bandwidth", "storage"]


def fetch_mdx(token):
    url = f"https://api.github.com/repos/{REPO}/contents/{PATH}?ref={BRANCH}"
    req = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(req) as resp:
        return resp.read().decode()


def parse_tables(mdx):
    """Return dict of section_name -> list of [range, rate] string pairs."""
    parts = re.split(r'^### (.+)$', mdx, flags=re.MULTILINE)
    result = {}
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1]
        rows = []
        for line in re.findall(r'^\|(.+)\|$', body, flags=re.MULTILINE):
            cells = [c.strip() for c in line.split('|')]
            if all(re.match(r'^-+$', c) or not c for c in cells):
                continue  # separator row
            rows.append(cells)
        if len(rows) >= 2:
            result[name] = rows[1:]  # skip header
    return result


def to_tb(val, unit):
    return val / 1000 if unit.lower() == 'gb' else val


def parse_tier(range_str, rate_str, section):
    """Return {"size": float|None, "rate": float}. size=None means Infinity."""
    rate_str = rate_str.strip()
    rate = 0.0 if rate_str.lower() == "included" else float(re.search(r'\$([0-9.]+)', rate_str).group(1))

    range_str = range_str.strip()
    is_data = section in ("Storage", "Indexing", "Bandwidth")

    # "First X unit"
    m = re.match(r'First ([0-9.]+)\s*(\w+)', range_str, re.IGNORECASE)
    if m:
        val, unit = float(m.group(1)), m.group(2)
        size = to_tb(val, unit) if is_data else val
        return {"size": size, "rate": rate}

    # "X+ unit"  →  Infinity
    if re.match(r'[0-9.]+\+', range_str):
        return {"size": None, "rate": rate}

    # "X unit - Y unit"  (both sides have explicit units)
    m = re.match(r'([0-9.]+)\s*(\w+)\s*[-–]\s*([0-9.]+)\s*(\w+)', range_str)
    if m:
        sv, su, ev, eu = float(m.group(1)), m.group(2), float(m.group(3)), m.group(4)
        if is_data:
            end_tb = ev * 1000 if eu.lower() == 'pb' else to_tb(ev, eu)
            size = end_tb - to_tb(sv, su)
        else:
            size = ev - sv
        return {"size": size, "rate": rate}

    # "X - Y unit"  (only the end has an explicit unit)
    m = re.match(r'([0-9.]+)\s*[-–]\s*([0-9.]+)\s*(\w+)', range_str)
    if m:
        sv, ev, eu = float(m.group(1)), float(m.group(2)), m.group(3)
        if is_data:
            size = (ev * 1000 if eu.lower() == 'pb' else to_tb(ev, eu)) - sv
        else:
            size = ev - sv
        return {"size": size, "rate": rate}

    raise ValueError(f"Unrecognised range format: {range_str!r}")


def build_tiers(tables):
    tiers = {}
    for section, key in SECTION_MAP.items():
        if section not in tables:
            print(f"WARNING: section '{section}' not found", file=sys.stderr)
            continue
        tiers[key] = [parse_tier(r[0], r[1], section) for r in tables[section] if len(r) >= 2]
    return tiers


def format_js_block(tiers, last_updated):
    lines = [
        f"  /* PRICING:START -- auto-updated by scripts/update-pricing.py -- lastUpdated: {last_updated} */",
        f"  const PRICING_LAST_UPDATED = '{last_updated}';",
        "  let TIERS = {",
    ]
    for key in KEY_ORDER:
        if key not in tiers:
            continue
        lines.append(f"    {key}: [")
        for t in tiers[key]:
            size = "Infinity" if t["size"] is None else t["size"]
            lines.append(f"      {{ size: {size}, rate: {t['rate']} }},")
        lines.append("    ],")
    lines.append("  };")
    lines.append("  /* PRICING:END */")
    return "\n".join(lines)


def main():
    token = os.environ.get("FOXGLOVE_GITHUB_TOKEN", "")
    if not token:
        sys.exit("FOXGLOVE_GITHUB_TOKEN is not set")

    print(f"Fetching {PATH} from {REPO}@{BRANCH}...", file=sys.stderr)
    try:
        mdx = fetch_mdx(token)
    except HTTPError as e:
        sys.exit(f"GitHub API error: {e.code} {e.reason}")

    tiers       = build_tiers(parse_tables(mdx))
    last_updated = date.today().isoformat()
    new_block   = format_js_block(tiers, last_updated)

    html_path = os.path.realpath(HTML_OUT)
    html = open(html_path).read()
    html_new = re.sub(r'/\* PRICING:START.*?PRICING:END \*/', new_block, html, flags=re.DOTALL)

    if html_new == html:
        print("Pricing unchanged — no update needed.", file=sys.stderr)
        return

    open(html_path, 'w').write(html_new)
    print(f"Updated {html_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
