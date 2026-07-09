#!/usr/bin/env python3
"""
Read pricing.mdx and update the PRICING:START…PRICING:END block in
pricing-calculator.html in-place.

Run from the repo root:
    python3 packages/docs/scripts/update-pricing.py
"""
import os
import re
import sys
from datetime import date

MDX_PATH  = "packages/docs/docs/pricing.mdx"
HTML_PATH = "packages/docs/static/pricing-calculator.html"

SECTION_MAP = {
    "Storage":   "storage",
    "Query":     "query",
    "Indexing":  "indexing",
    "Bandwidth": "bandwidth",
}
KEY_ORDER = ["indexing", "query", "bandwidth", "storage"]


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
        return {"size": to_tb(val, unit) if is_data else val, "rate": rate}

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


def parse_fleet(mdx):
    m = re.search(r'^## Fleet\s*$(.*?)(?=^## |\Z)', mdx, re.MULTILINE | re.DOTALL)
    if not m:
        print("WARNING: '## Fleet' section not found", file=sys.stderr)
        return {"devicesIncluded": 5, "deviceRate": 45, "remoteIncludedPerDevice": 300, "remoteRate": 0.05}
    fleet = m.group(1)

    # Devices included (Pro/Enterprise column of the included-usage table)
    m2 = re.search(r'\|\s*\*{0,2}Devices\*{0,2}\s*\|\s*\d+\s*\|\s*(\d+)\s*\|', fleet)
    devices_included = int(m2.group(1)) if m2 else 5

    # Remote access included (Pro/Enterprise column)
    m2 = re.search(r'\|\s*\*{0,2}Remote access\*{0,2}\s*\|[^|]*\|\s*(\d+)\s*min', fleet)
    remote_included = int(m2.group(1)) if m2 else 300

    # Flat rates from prose
    m2 = re.search(r'\$([0-9.]+)/device/mo', fleet)
    device_rate = float(m2.group(1)) if m2 else 45

    m2 = re.search(r'\$([0-9.]+)/min', fleet)
    remote_rate = float(m2.group(1)) if m2 else 0.05

    return {
        "devicesIncluded":         devices_included,
        "deviceRate":              device_rate,
        "remoteIncludedPerDevice": remote_included,
        "remoteRate":              remote_rate,
    }


def format_js_block(tiers, fleet, last_updated):
    f = fleet
    lines = [
        f"  /* PRICING:START -- auto-updated by scripts/update-pricing.py -- lastUpdated: {last_updated} */",
        f"  const PRICING_LAST_UPDATED = '{last_updated}';",
        f"  const FLEET = {{",
        f"    devicesIncluded:         {f['devicesIncluded']},      // devices included per plan",
        f"    deviceRate:              {f['deviceRate']},     // $/device/mo for additional devices",
        f"    remoteIncludedPerDevice: {f['remoteIncludedPerDevice']},   // min/device/mo included on Pro/Enterprise",
        f"    remoteRate:              {f['remoteRate']},   // $/min for additional remote access",
        f"  }};",
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
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    mdx_path  = os.path.realpath(os.path.join(root, MDX_PATH))
    html_path = os.path.realpath(os.path.join(root, HTML_PATH))

    print(f"Reading {mdx_path}...", file=sys.stderr)
    mdx = open(mdx_path).read()

    tables       = parse_tables(mdx)
    tiers        = build_tiers(tables)
    fleet        = parse_fleet(mdx)
    last_updated = date.today().isoformat()
    new_block    = format_js_block(tiers, fleet, last_updated)

    html = open(html_path).read()
    html_new = re.sub(r'/\* PRICING:START.*?PRICING:END \*/', new_block, html, flags=re.DOTALL)

    if html_new == html:
        print("Pricing unchanged — no update needed.", file=sys.stderr)
        return

    open(html_path, 'w').write(html_new)
    print(f"Updated {html_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
