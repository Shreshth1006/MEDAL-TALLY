"""
ndtv_scrape.py — scrape NDTV's redesigned CWG medals-tally page, upsert into Supabase.
"""
import os, sys, json, pathlib, datetime as dt
from dotenv import load_dotenv
load_dotenv()

from bs4 import BeautifulSoup
from scrapfly import ScrapflyClient, ScrapeConfig
from supabase import create_client

NDTV_URL = "https://sports.ndtv.com/commonwealth-games-2026/medals-tally"
OUT = pathlib.Path("out"); OUT.mkdir(exist_ok=True)
EXPECTED_NATIONS = 74

NAME_MAP = {
    "Brunei": "Brunei Darussalam",
    "Gambia": "The Gambia",
    "St Vincent and the Grenadines": "Saint Vincent and the Grenadines",
    "Antigua & Barbuda": "Antigua and Barbuda",
    "St Helena": "Saint Helena",
    "Saint Kitts & Nevis": "Saint Kitts and Nevis",
    "Turks & Caicos": "Turks and Caicos Islands",
}

scrapfly = ScrapflyClient(key=os.environ["SCRAPFLY_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get_valid_names() -> set[str]:
    existing = supabase.table("medal_tally").select("country_name").execute()
    return {r["country_name"] for r in existing.data}


def fetch_html() -> str:
    config = ScrapeConfig(
        url=NDTV_URL,
        render_js=True,
        wait_for_selector="ul.OlyT_ul li.OlyT_li",
        rendering_wait=3000,
        asp=True,
        country="IN",
    )
    result = scrapfly.scrape(config)
    (OUT / "ndtv_debug.html").write_text(result.content)
    return result.content


def parse(html: str, valid_names: set[str]) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("ul.OlyT_ul")
    if not container:
        raise RuntimeError("DRIFT: ul.OlyT_ul not found — NDTV markup changed again")

    seen = set()
    dupes_dropped = 0
    unknown_dropped = []
    rows = []

    # direct children only — guards against nested li.OlyT_li elements
    # (e.g. per-country expandable detail) getting swept up by a recursive match
    for li in container.find_all("li", class_="OlyT_li", recursive=False):
        name_div = li.select_one(".OlyT_tab-nweb") or li.select_one(".OlyT_tab-nwap")
        country = name_div.get_text(strip=True) if name_div else None
        if not country:
            continue

        country = NAME_MAP.get(country, country)

        if country not in valid_names:
            unknown_dropped.append(country)
            continue
        if country in seen:
            dupes_dropped += 1
            continue
        seen.add(country)

        rt = li.select_one(".OlyT_tab-rt")
        if not rt:
            continue
        nums = [d.get_text(strip=True) for d in rt.find_all("div")
                if d.get_text(strip=True).lstrip("-").isdigit()]
        if len(nums) < 4:
            continue

        g, s, b, tot = (int(x) for x in nums[:4])
        rows.append({"country_name": country, "gold": g, "silver": s,
                     "bronze": b, "total": tot})

    if dupes_dropped:
        print(f"[warn] dropped {dupes_dropped} duplicate row(s)")
    if unknown_dropped:
        print(f"[warn] dropped {len(unknown_dropped)} unrecognized name(s): {sorted(set(unknown_dropped))}")

    return rows


def add_ranks(rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda r: (-r["gold"], -r["silver"], -r["bronze"]))
    rank, last_key = 0, None
    for row in ordered:
        key = (row["gold"], row["silver"], row["bronze"])
        if key != last_key:
            rank += 1
        row["rank"] = rank if row["total"] > 0 else None
        last_key = key
    return ordered


def validate(rows: list[dict]) -> list[str]:
    errs = []
    if not rows:
        errs.append("zero rows parsed")
    if abs(len(rows) - EXPECTED_NATIONS) > 6:
        errs.append(f"nation count {len(rows)} vs expected ~{EXPECTED_NATIONS}")
    for r in rows:
        if r["gold"] + r["silver"] + r["bronze"] != r["total"]:
            errs.append(f"total mismatch: {r['country_name']} {r}")
    return errs


def upsert(rows: list[dict], stamp: str):
    payload = []
    for r in rows:
        payload.append({
            "country_name": r["country_name"],
            "gold": r["gold"], "silver": r["silver"],
            "bronze": r["bronze"],
            "rank": r["rank"],
            "updated_at": stamp,
        })
    resp = supabase.table("medal_tally").upsert(payload, on_conflict="country_name").execute()
    print(f"upserted {len(resp.data)} rows to Supabase")


if __name__ == "__main__":
    valid_names = get_valid_names()
    html = fetch_html()
    rows = parse(html, valid_names)
    rows = add_ranks(rows)
    errs = validate(rows)

    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"{len(rows)} nations | medals awarded: {sum(r['total'] for r in rows)}")
    print(json.dumps(rows[:5], indent=2))

    if errs:
        print("\n!! VALIDATION FAILED — not pushing to Supabase", file=sys.stderr)
        for e in errs:
            print("  -", e, file=sys.stderr)
        sys.exit(1)

    upsert(rows, stamp)