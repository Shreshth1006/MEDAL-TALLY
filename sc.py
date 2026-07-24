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

# maps NDTV's raw scraped name -> the exact name already stored in medal_tally
NAME_MAP = {
    "Brunei": "Brunei Darussalam",
    "Gambia": "The Gambia",
    "St Vincent and the Grenadines": "Saint Vincent and the Grenadines",
    # harmless safety nets in case NDTV shortens these too, later
    "Antigua & Barbuda": "Antigua and Barbuda",
    "St Helena": "Saint Helena",
    "Saint Kitts & Nevis": "Saint Kitts and Nevis",
    "Turks & Caicos": "Turks and Caicos Islands",
}

scrapfly = ScrapflyClient(key=os.environ["SCRAPFLY_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


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


def parse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("ul.OlyT_ul")
    if not container:
        raise RuntimeError("DRIFT: ul.OlyT_ul not found — NDTV markup changed again")

    rows = []
    for li in container.select("li.OlyT_li"):
        name_div = li.select_one(".OlyT_tab-nweb") or li.select_one(".OlyT_tab-nwap")
        country = name_div.get_text(strip=True) if name_div else None
        if not country:
            continue

        rt = li.select_one(".OlyT_tab-rt")
        if not rt:
            continue

        nums = [d.get_text(strip=True) for d in rt.find_all("div")
                if d.get_text(strip=True).lstrip("-").isdigit()]
        if len(nums) < 4:
            continue

        g, s, b, tot = (int(x) for x in nums[:4])
        country = NAME_MAP.get(country, country)   # <-- mapping applied here, before anything else touches this value
        rows.append({"country_name": country, "gold": g, "silver": s,
                     "bronze": b, "total": tot})
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
    dupes = {r["country_name"] for r in rows
             if [x["country_name"] for x in rows].count(r["country_name"]) > 1}
    if dupes:
        errs.append(f"duplicate countries: {sorted(dupes)}")
    return errs


def verify_names_match(rows: list[dict]):
    existing = supabase.table("medal_tally").select("country_name").execute()
    existing_names = {r["country_name"] for r in existing.data}
    scraped_names = {r["country_name"] for r in rows}

    unknown = scraped_names - existing_names
    if unknown:
        raise RuntimeError(
            f"DRIFT: scraped names not in medal_tally, would create duplicate rows: {sorted(unknown)}"
        )


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
    html = fetch_html()
    rows = parse(html)
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

    verify_names_match(rows)
    upsert(rows, stamp)