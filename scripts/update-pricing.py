#!/usr/bin/env python3
"""
Fetch pricing.mdx from foxglove/app and regenerate pricing-data.json.
Requires FOXGLOVE_GITHUB_TOKEN env var with read access to foxglove/app.
"""
import json
import os
import re
import sys
from datetime import date, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError

REPO   = "foxglove/app"
PATH   = "packages/docs/docs/pricing.mdx"
BRANCH = "main"
OUT    = os.path.join(os.path.dirname(__file__), "..", "pricing-data.json")

SECTION_MAP = {
    "Storage":   "storage",
    "Query":     "query",
    "Indexing":  "indexing",
    "Bandwidth": "bandwidth",
}


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
    return val / 1000 if unit.lower() in ('gb',) else val


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


def build(tables):
    tiers = {}
    for section, key in SECTION_MAP.items():
        if section not in tables:
            print(f"WARNING: section '{section}' not found in MDX", file=sys.stderr)
            continue
        tiers[key] = [parse_tier(row[0], row[1], section) for row in tables[section] if len(row) >= 2]
    return tiers


def main():
    token = os.environ.get("FOXGLOVE_GITHUB_TOKEN", "")
    if not token:
        sys.exit("FOXGLOVE_GITHUB_TOKEN is not set")

    print(f"Fetching {PATH} from {REPO}@{BRANCH}...", file=sys.stderr)
    try:
        mdx = fetch_mdx(token)
    except HTTPError as e:
        sys.exit(f"GitHub API error: {e.code} {e.reason}")

    tables = parse_tables(mdx)
    tiers  = build(tables)

    data = {
        "lastUpdated": date.today().isoformat(),
        "source": f"https://github.com/{REPO}/blob/{BRANCH}/{PATH}",
        "tiers": tiers,
    }

    out_path = os.path.realpath(OUT)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"Written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
