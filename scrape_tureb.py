#!/usr/bin/env python3
"""Scrape every guide from https://www.tureb.org.tr/RehberVeritabani.

The page is an ASP.NET application that exposes a search form. This script:
  1. Fetches the search page and captures any hidden form fields
     (__VIEWSTATE / __EVENTVALIDATION / RequestVerificationToken etc.) and
     cookies into a session.
  2. Submits an empty search so the server returns every guide.
  3. Walks the pagination, extracting one row per guide.
  4. Writes the result as JSON and CSV.

The DOM markup of the page is sniffed at runtime, so small changes in the
column order or field names will not break the scraper. If the site layout
changes substantially you can override the selectors at the top of the file.

Usage:
    pip install -r requirements.txt
    python scrape_tureb.py                 # writes guides.json and guides.csv
    python scrape_tureb.py --out data.json # custom output path
    python scrape_tureb.py --debug         # dump first page HTML to debug.html
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.tureb.org.tr"
SEARCH_URL = f"{BASE_URL}/RehberVeritabani"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

REQUEST_DELAY_SECONDS = 1.0
MAX_RETRIES = 4
RETRY_BACKOFF = 2.0


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def fetch(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    """GET/POST with exponential backoff for transient errors."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
            if resp.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"transient {resp.status_code}")
            resp.raise_for_status()
            return resp
        except (requests.RequestException, requests.HTTPError) as exc:
            last_exc = exc
            wait = RETRY_BACKOFF * (2 ** attempt)
            print(f"  ! {method} {url} failed ({exc}); retry in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{method} {url} failed after {MAX_RETRIES} retries") from last_exc


def collect_hidden_fields(form: Tag) -> dict[str, str]:
    """ASP.NET pages stash __VIEWSTATE etc. in hidden inputs."""
    data: dict[str, str] = {}
    for inp in form.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        if name:
            data[name] = inp.get("value", "")
    return data


def find_results_table(soup: BeautifulSoup) -> Tag | None:
    """Pick the table that most plausibly holds guide rows."""
    candidates = soup.find_all("table")
    best: Tag | None = None
    best_rows = 0
    for table in candidates:
        rows = table.find_all("tr")
        if len(rows) > best_rows:
            best = table
            best_rows = len(rows)
    return best


def parse_headers(table: Tag) -> list[str]:
    head_row = table.find("tr")
    if not head_row:
        return []
    headers: list[str] = []
    for cell in head_row.find_all(["th", "td"]):
        text = cell.get_text(" ", strip=True)
        headers.append(text or f"col_{len(headers)}")
    return headers


def parse_row(cells: list[Tag], headers: list[str]) -> dict[str, str]:
    record: dict[str, str] = {}
    for idx, cell in enumerate(cells):
        key = headers[idx] if idx < len(headers) else f"col_{idx}"
        record[key] = cell.get_text(" ", strip=True)
    return record


PAGE_NUM_RE = re.compile(r"page=(\d+)|sayfa=(\d+)|/(\d+)$", re.IGNORECASE)
DOPOSTBACK_RE = re.compile(r"__doPostBack\('([^']+)','([^']*)'\)")


def discover_pagination(soup: BeautifulSoup, current_url: str) -> list[tuple[str, dict[str, str] | None]]:
    """Return ordered list of (url, postdata) for the next pages.

    Supports three common patterns:
      - <a href="?page=2">2</a> style links
      - <a href="javascript:__doPostBack('ctl00$..$gv','Page$2')"> style
      - explicit `Sayfa` query strings
    """
    seen: set[str] = set()
    pages: list[tuple[str, dict[str, str] | None]] = []

    pager = soup.find(class_=re.compile(r"pag(in|er)", re.IGNORECASE)) or soup
    for a in pager.find_all("a"):
        label = a.get_text(strip=True)
        if not label or not label.isdigit():
            continue
        href = a.get("href", "")
        if href.startswith("javascript:") or "__doPostBack" in href:
            m = DOPOSTBACK_RE.search(href)
            if m:
                key = f"postback:{m.group(1)}|{m.group(2)}"
                if key in seen:
                    continue
                seen.add(key)
                pages.append((current_url, {"__EVENTTARGET": m.group(1), "__EVENTARGUMENT": m.group(2)}))
        elif href:
            full = urljoin(current_url, href)
            if full in seen:
                continue
            seen.add(full)
            pages.append((full, None))
    return pages


def extract_rows(table: Tag, headers: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        rows.append(parse_row(cells, headers))
    return rows


def scrape(debug: bool = False) -> list[dict[str, str]]:
    session = make_session()

    print(f"GET {SEARCH_URL}")
    resp = fetch(session, "GET", SEARCH_URL)
    soup = BeautifulSoup(resp.text, "html.parser")

    if debug:
        Path("debug.html").write_text(resp.text, encoding="utf-8")
        print("  wrote debug.html")

    form = soup.find("form")
    if form is None:
        raise RuntimeError("No <form> on the search page; site layout may have changed.")

    action = urljoin(SEARCH_URL, form.get("action") or SEARCH_URL)
    hidden = collect_hidden_fields(form)

    # Submit an empty search so the result set is unconstrained.
    submit_data = dict(hidden)
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name or name in submit_data:
            continue
        if inp.name == "input" and inp.get("type") in {"submit", "button", "image"}:
            # Trigger the search button if there is one named explicitly.
            value = inp.get("value", "")
            if "ara" in (value or "").lower() or "search" in (value or "").lower():
                submit_data[name] = value
            continue
        submit_data.setdefault(name, "")

    print(f"POST {action} (empty search)")
    time.sleep(REQUEST_DELAY_SECONDS)
    resp = fetch(session, "POST", action, data=submit_data)
    soup = BeautifulSoup(resp.text, "html.parser")

    table = find_results_table(soup)
    if table is None:
        raise RuntimeError("Could not locate the results table; rerun with --debug.")
    headers = parse_headers(table)
    print(f"  detected columns: {headers}")

    all_rows: list[dict[str, str]] = extract_rows(table, headers)
    print(f"  page 1: {len(all_rows)} rows")

    visited: set[str] = {SEARCH_URL}
    page_num = 1
    while True:
        next_pages = discover_pagination(soup, resp.url)
        # Filter out pages we have already walked.
        next_pages = [p for p in next_pages if p[0] not in visited or p[1] is not None]
        # Pick the page numerically just after the current one.
        candidate: tuple[str, dict[str, str] | None] | None = None
        for url, postdata in next_pages:
            if postdata is not None:
                arg = postdata.get("__EVENTARGUMENT", "")
                m = re.search(r"(\d+)", arg)
                if m and int(m.group(1)) == page_num + 1:
                    candidate = (url, postdata)
                    break
            else:
                m = re.search(r"(?:page|sayfa)=(\d+)", url, re.IGNORECASE)
                if m and int(m.group(1)) == page_num + 1:
                    candidate = (url, postdata)
                    break
        if candidate is None and next_pages:
            # Fall back to the first unseen link.
            candidate = next_pages[0]

        if candidate is None:
            break

        url, postdata = candidate
        page_num += 1
        time.sleep(REQUEST_DELAY_SECONDS)

        if postdata is not None:
            # Re-grab __VIEWSTATE etc. from the current page.
            current_form = soup.find("form")
            payload = collect_hidden_fields(current_form) if current_form else {}
            payload.update(postdata)
            print(f"POST {url} (page {page_num} via __doPostBack)")
            resp = fetch(session, "POST", url, data=payload)
        else:
            print(f"GET {url} (page {page_num})")
            resp = fetch(session, "GET", url)
            visited.add(url)

        soup = BeautifulSoup(resp.text, "html.parser")
        table = find_results_table(soup)
        if table is None:
            print(f"  ! no table on page {page_num}; stopping")
            break
        rows = extract_rows(table, headers)
        if not rows:
            print(f"  page {page_num}: empty; stopping")
            break
        print(f"  page {page_num}: {len(rows)} rows")
        all_rows.extend(rows)

    return all_rows


def write_outputs(rows: list[dict[str, str]], out_json: Path, out_csv: Path) -> None:
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        fieldnames: list[str] = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with out_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    print(f"wrote {len(rows)} guides → {out_json} & {out_csv}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="guides.json", help="JSON output path (CSV uses the same stem)")
    parser.add_argument("--debug", action="store_true", help="dump first response to debug.html")
    args = parser.parse_args(argv)

    rows = scrape(debug=args.debug)
    out_json = Path(args.out)
    out_csv = out_json.with_suffix(".csv")
    write_outputs(rows, out_json, out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
