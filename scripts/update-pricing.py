#!/usr/bin/env python3
"""
Scrape www.foxglove.dev/pricing with a headless browser and update the
PRICING:START…PRICING:END block in mockup.html in-place.

Run from repo root:
    python3 scripts/update-pricing.py
"""
import os
import re
import sys
from datetime import date

URL      = "https://www.foxglove.dev/pricing"
ROOT     = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
HTML_OUT = os.path.join(ROOT, "mockup.html")

KEY_ORDER   = ["indexing", "query", "bandwidth", "storage"]
SECTION_MAP = {
    "storage":   "storage",
    "query":     "query",
    "indexing":  "indexing",
    "bandwidth": "bandwidth",
}

# ── Playwright fetch ──────────────────────────────────────────────────────────

def fetch_page_text():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        print(f"Navigating to {URL}...", file=sys.stderr)
        page.goto(URL, wait_until="networkidle", timeout=30000)

        # Extract each table with its nearest preceding heading.
        sections = page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('table').forEach(table => {
                // Find nearest preceding heading
                let heading = '';
                let el = table;
                outer: while (el) {
                    let sib = el.previousElementSibling;
                    while (sib) {
                        if (/^H[1-5]$/.test(sib.tagName)) { heading = sib.innerText.trim(); break outer; }
                        const h = sib.querySelector('h1,h2,h3,h4,h5');
                        if (h) { heading = h.innerText.trim(); break outer; }
                        sib = sib.previousElementSibling;
                    }
                    el = el.parentElement;
                }
                // Collect rows: [cell0, cell1, ...]
                const rows = [];
                table.querySelectorAll('tr').forEach(tr => {
                    const cells = [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim());
                    if (cells.length >= 2) rows.push(cells);
                });
                if (rows.length > 1) results.push({ heading, rows });
            });
            // Also grab the full body text for prose rate parsing (fleet etc.)
            return { tables: results, bodyText: document.body.innerText };
        }""")

        browser.close()
        return sections


# ── Table → tier parsing ──────────────────────────────────────────────────────

def to_tb(val, unit):
    return val / 1000 if unit.lower() == 'gb' else val


def parse_tier(range_str, rate_str, section):
    """Return {"size": float|None, "rate": float}. size=None means Infinity."""
    rate_str = rate_str.strip()
    rate = 0.0 if re.search(r'included|free', rate_str, re.IGNORECASE) \
        else float(re.search(r'\$([0-9.]+)', rate_str).group(1))

    range_str = range_str.strip()
    is_data = section in ("storage", "indexing", "bandwidth")

    # "First X unit"
    m = re.match(r'First ([0-9.]+)\s*(\w+)', range_str, re.IGNORECASE)
    if m:
        val, unit = float(m.group(1)), m.group(2)
        return {"size": to_tb(val, unit) if is_data else val, "rate": rate}

    # "X+ unit" or "X,XXX+ unit"  →  Infinity
    if re.match(r'[\d,]+\+', range_str):
        return {"size": None, "rate": rate}

    # "X unit – Y unit" or "X unit - Y unit" (both sides have explicit units)
    m = re.match(r'([\d,.]+)\s*(\w+)\s*[-–—]\s*([\d,.]+)\s*(\w+)', range_str)
    if m:
        sv = float(m.group(1).replace(',', '')); su = m.group(2)
        ev = float(m.group(3).replace(',', '')); eu = m.group(4)
        if is_data:
            end_tb = ev * 1000 if eu.lower() == 'pb' else to_tb(ev, eu)
            size = end_tb - to_tb(sv, su)
        else:
            size = ev - sv
        return {"size": size, "rate": rate}

    # "X – Y unit" (only the end has an explicit unit)
    m = re.match(r'([\d,.]+)\s*[-–—]\s*([\d,.]+)\s*(\w+)', range_str)
    if m:
        sv = float(m.group(1).replace(',', ''))
        ev = float(m.group(2).replace(',', '')); eu = m.group(3)
        if is_data:
            size = (ev * 1000 if eu.lower() == 'pb' else to_tb(ev, eu)) - sv
        else:
            size = ev - sv
        return {"size": size, "rate": rate}

    raise ValueError(f"Unrecognised range format: {range_str!r}")


def match_section(heading):
    """Return a SECTION_MAP key if the heading matches a known dimension."""
    h = heading.lower()
    for keyword, key in SECTION_MAP.items():
        if keyword in h:
            return key
    return None


def build_tiers(page_data):
    tiers = {}
    for table_info in page_data["tables"]:
        key = match_section(table_info["heading"])
        if not key:
            continue
        rows = table_info["rows"]
        # Skip header row(s) — any row where the second cell has no $ and no "included"
        data_rows = [
            r for r in rows
            if re.search(r'\$|included|free', r[1], re.IGNORECASE)
        ]
        parsed = []
        for row in data_rows:
            try:
                parsed.append(parse_tier(row[0], row[1], key))
            except Exception as e:
                print(f"  WARNING: skipping row {row!r}: {e}", file=sys.stderr)
        if parsed:
            tiers[key] = parsed
            print(f"  {key}: {len(parsed)} tiers", file=sys.stderr)
    return tiers


# ── Fleet parsing (prose text) ────────────────────────────────────────────────

def parse_fleet(page_data):
    text = page_data["bodyText"]

    # First try to find a "Fleet" / "Devices" table
    devices_included   = 5
    device_rate        = None
    remote_included    = 300
    remote_rate        = None

    for table_info in page_data["tables"]:
        h = table_info["heading"].lower()
        if "device" not in h and "fleet" not in h and "remote" not in h:
            continue
        for row in table_info["rows"]:
            row_text = " ".join(row).lower()
            # Included devices
            m = re.search(r'(\d+)\s*device', row_text)
            if m and "included" in row_text:
                devices_included = int(m.group(1))
            # Included remote minutes
            m = re.search(r'(\d+)\s*min', row_text)
            if m and "included" in row_text:
                remote_included = int(m.group(1))
            # Device rate
            m = re.search(r'\$([0-9.]+)\s*/\s*device', row_text)
            if m:
                device_rate = float(m.group(1))
            # Remote rate
            m = re.search(r'\$([0-9.]+)\s*/\s*min', row_text)
            if m:
                remote_rate = float(m.group(1))

    # Fall back to body text regex
    if device_rate is None:
        m = re.search(r'\$([0-9.]+)\s*/?\s*device\s*/?\s*mo', text, re.IGNORECASE)
        if m:
            device_rate = float(m.group(1))
    if remote_rate is None:
        m = re.search(r'\$([0-9.]+)\s*/?\s*min', text, re.IGNORECASE)
        if m:
            remote_rate = float(m.group(1))
    m = re.search(r'(\d+)\s*min(?:utes?)?\s*/?\s*device', text, re.IGNORECASE)
    if m:
        remote_included = int(m.group(1))
    m = re.search(r'(\d+)\s*devices?\s*included', text, re.IGNORECASE)
    if m:
        devices_included = int(m.group(1))

    fleet = {
        "devicesIncluded":         devices_included,
        "deviceRate":              device_rate or 20,
        "remoteIncludedPerDevice": remote_included,
        "remoteRate":              remote_rate or 0.05,
    }
    print(f"  fleet: {fleet}", file=sys.stderr)
    return fleet


# ── JS block generation ───────────────────────────────────────────────────────

def format_js_block(tiers, fleet, last_updated):
    f = fleet
    lines = [
        f"  /* PRICING:START -- auto-updated by scripts/update-pricing.py -- lastUpdated: {last_updated} */",
        f"  const PRICING_LAST_UPDATED = '{last_updated}';",
        f"  const FLEET = {{",
        f"    devicesIncluded:         {f['devicesIncluded']},",
        f"    deviceRate:              {f['deviceRate']},",
        f"    remoteIncludedPerDevice: {f['remoteIncludedPerDevice']},",
        f"    remoteRate:              {f['remoteRate']},",
        f"  }};",
        "  let TIERS = {",
    ]
    for key in KEY_ORDER:
        if key not in tiers:
            print(f"WARNING: no tiers parsed for '{key}' — keeping existing value", file=sys.stderr)
            continue
        lines.append(f"    {key}: [")
        for t in tiers[key]:
            size = "Infinity" if t["size"] is None else t["size"]
            lines.append(f"      {{ size: {size}, rate: {t['rate']} }},")
        lines.append("    ],")
    lines.append("  };")
    lines.append("  /* PRICING:END */")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    page_data = fetch_page_text()

    print("Parsing tiers...", file=sys.stderr)
    tiers = build_tiers(page_data)
    if not tiers:
        print("ERROR: No pricing tiers found. Page structure may have changed.", file=sys.stderr)
        print("Tables found:", file=sys.stderr)
        for t in page_data["tables"]:
            print(f"  heading={t['heading']!r}  rows={len(t['rows'])}", file=sys.stderr)
        sys.exit(1)

    print("Parsing fleet...", file=sys.stderr)
    fleet = parse_fleet(page_data)

    missing = [k for k in KEY_ORDER if k not in tiers]
    if missing:
        print(f"WARNING: missing dimensions: {missing}", file=sys.stderr)

    last_updated = date.today().isoformat()
    new_block    = format_js_block(tiers, fleet, last_updated)

    html = open(HTML_OUT).read()
    html_new = re.sub(r'/\* PRICING:START.*?PRICING:END \*/', new_block, html, flags=re.DOTALL)

    if html_new == html:
        print("Pricing unchanged — no update needed.", file=sys.stderr)
        return

    open(HTML_OUT, 'w').write(html_new)
    print(f"Updated {HTML_OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
