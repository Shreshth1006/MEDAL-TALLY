"""
cwg_tally.py — scrape the Flourish medal table embedded in BBC's CWG article,
via Scrapfly (JS rendering + anti-bot), upsert into Supabase medal_tally.
"""
import os, sys, json, pathlib, datetime as dt
from dotenv import load_dotenv
load_dotenv()

from bs4 import BeautifulSoup
from scrapfly import ScrapflyClient, ScrapeConfig
from supabase import create_client

FLOURISH_URL = "https://flo.uri.sh/visualisation/29748648/embed?auto=1"
EXPECTED_NATIONS = 74
OUT = pathlib.Path("out"); OUT.mkdir(exist_ok=True)

NAME_MAP = {
    "Antigua and Barbuda": "Antigua & Barbuda",
    "Saint Helena": "St Helena",
    "Saint Kitts and Nevis": "Saint Kitts & Nevis",
    "Saint Vincent and the Grenadines": "St Vincent & The Grenadines",
    "Turks and Caicos Islands": "Turks & Caicos",
}

scrapfly = ScrapflyClient(key=os.environ["SCRAPFLY_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def fetch_html() -> str:
    config = ScrapeConfig(
        url=FLOURISH_URL,
        render_js=True,
        wait_for_selector="table#table-inner tbody tr",
        rendering_wait=3000,
        asp=True,
        country="GB",
    )
    result = scrapfly.scrape(config)
    (OUT / "flourish_debug.html").write_text(result.content)
    return result.content


def cell_value(td) -> str:
    body = td.select_one("div.cell-body")
    return (body.get_text(strip=True) if body else td.get_text(strip=True))


def parse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#table-inner")
    if not table:
        raise RuntimeError("DRIFT: table#table-inner not found — Flourish markup changed")

    rows = []
    for tr in table.select("tbody tr"):
        tds = tr.select("td")
        if len(tds) < 5:
            continue
        cells = [cell_value(td) for td in tds[:5]]
        country, g, s, b, tot = cells
        try:
            g, s, b, tot = int(g), int(s), int(b), int(tot)
        except ValueError:
            continue
        country = NAME_MAP.get(country, country)
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


def upsert(rows: list[dict], stamp: str):
    payload = []
    for r in rows:
        payload.append({
            "country_name": r["country_name"],
            "gold": r["gold"], "silver": r["silver"],
            "bronze": r["bronze"],
            # total intentionally omitted — it's a generated column in Postgres
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

    upsert(rows, stamp)