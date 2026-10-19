"""
Asian Para Games 2026 medal tally scraper -> Supabase `para_medal_tally`.

Source: ESPN India medals-tally story (plain HTML <table class="inline-table">),
which sits behind an AWS WAF JavaScript challenge, so the page is loaded in
headless Chromium via Playwright (a plain requests/curl GET only gets the
challenge page). If SCRAPFLY_KEY is set and Playwright fails, it falls back
to Scrapfly (render_js + asp).

Writes: gold, silver, bronze, rank, updated_at (+ short_name, which is NOT NULL
on upsert and is read live from the table). `total` is a DB-generated column
and is never sent. `sorting` is never touched.

Usage:
  python para_scrape.py                 # scrape + upsert
  python para_scrape.py --dry-run       # scrape + print, no writes
  python para_scrape.py --html page.html --dry-run   # parse a saved page

Env (read from the environment, or from a .env file next to the script / in cwd):
     SUPABASE_URL
     SUPABASE_SERVICE_KEY  (service_role / secret key; the publishable key gets 42501 RLS)
     SCRAPFLY_API_KEY or SCRAPFLY_KEY  (optional fallback)
     FORCE=1  (optional, bypasses the medal-drop guard)
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win, so
    GitHub Actions secrets are never overridden by a stray .env file."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv(Path.cwd() / ".env")


def env(*names: str):
    """First non-empty value among several accepted variable names."""
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


URL = "https://www.espn.in/espn/story/_/id/50201306/asian-para-games-2026-medals-tally"
TABLE = "para_medal_tally"
SUPABASE_URL = (env("SUPABASE_URL") or "https://npwkyytdnylpyqosstnc.supabase.co").rstrip("/")
SUPABASE_KEY = env("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY")
SCRAPFLY_KEY = env("SCRAPFLY_KEY", "SCRAPFLY_API_KEY")
DEBUG_DIR = Path("debug")

# ESPN lists 40 of the 45 NPCs (Bhutan, Maldives, Palestine, Timor-Leste and Yemen
# were missing on 18 Oct). It may add rows later, so accept a range, not an exact count.
MIN_ROWS, MAX_ROWS = 35, 45

# ESPN's spelling -> candidate spellings in our table (first one that exists wins).
# Brunei is listed both ways so it works whether or not the rename to
# "Brunei Darussalam" was run on para_medal_tally.
NAME_MAP = {
    "Hong Kong, China": ["Hong Kong"],
    "Macao, China": ["Macau"],
    "Macau, China": ["Macau"],
    "Brunei Darussalam": ["Brunei Darussalam", "Brunei"],
    "Brunei": ["Brunei", "Brunei Darussalam"],
    "Korea": ["South Korea"],
    "Republic of Korea": ["South Korea"],
    "DPR Korea": ["North Korea"],
    "Democratic People's Republic of Korea": ["North Korea"],
    "IR Iran": ["Iran"],
    "Islamic Republic of Iran": ["Iran"],
    "People's Republic of China": ["China"],
    "Lao PDR": ["Laos"],
    "Lao People's Democratic Republic": ["Laos"],
    "Syrian Arab Republic": ["Syria"],
    "Viet Nam": ["Vietnam"],
    "UAE": ["United Arab Emirates"],
    "Timor Leste": ["Timor-Leste"],
    "Palestine, State of": ["Palestine"],
    "Taipei": ["Chinese Taipei"],
}


# country_name values seeded by create_para_medal_tally.sql (used only for dry runs without DB access)
SEED_NAMES = set("""Afghanistan|Bahrain|Bangladesh|Bhutan|Brunei|Cambodia|China|Hong Kong|India|Indonesia|Iran|Iraq|
Japan|Jordan|Kazakhstan|Kuwait|Kyrgyzstan|Laos|Lebanon|Macau|Malaysia|Maldives|Mongolia|Myanmar|Nepal|North Korea|
Oman|Pakistan|Palestine|Philippines|Qatar|Saudi Arabia|Singapore|South Korea|Sri Lanka|Syria|Chinese Taipei|
Tajikistan|Thailand|Timor-Leste|Turkmenistan|United Arab Emirates|Uzbekistan|Vietnam|Yemen""".replace("\n", "").split("|"))


# --------------------------------------------------------------------------- fetch
def fetch_playwright() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
            locale="en-IN",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        # The WAF challenge reloads the page once it has a token; wait for the real table.
        page.wait_for_selector("table.inline-table tr", timeout=60_000)
        html = page.content()
        browser.close()
        return html


def fetch_scrapfly() -> str:
    q = urllib.parse.urlencode({"key": SCRAPFLY_KEY, "url": URL, "render_js": "true",
                                "asp": "true", "country": "in", "wait_for_selector": "table.inline-table"})
    with urllib.request.urlopen(f"https://api.scrapfly.io/scrape?{q}", timeout=150) as r:
        return json.loads(r.read().decode())["result"]["content"]


def fetch_html() -> str:
    try:
        return fetch_playwright()
    except Exception as e:
        if not SCRAPFLY_KEY:
            raise
        print(f"Playwright failed ({e}); falling back to Scrapfly", file=sys.stderr)
        return fetch_scrapfly()


# --------------------------------------------------------------------------- parse
def find_tally_table(soup: BeautifulSoup):
    """The tally table is the one whose header is Country/Gold/Silver/Bronze/Total."""
    for t in soup.find_all("table"):
        first = t.find("tr")
        if not first:
            continue
        head = [c.get_text(" ", strip=True).lower() for c in first.find_all(["th", "td"])]
        if head[:5] == ["country", "gold", "silver", "bronze", "total"]:
            return t
    raise ValueError("medal tally table not found (header Country/Gold/Silver/Bronze/Total)")


def to_int(s: str) -> int:
    s = s.strip().replace(",", "")
    return int(s) if s not in ("", "-", "–", "—") else 0


def parse_tally(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_tally_table(soup)
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 5 or not cells[0]:
            continue
        name = " ".join(cells[0].split())
        g, s, b, t = (to_int(x) for x in cells[1:5])
        if g + s + b != t:
            raise ValueError(f"row total mismatch for {name}: {g}+{s}+{b} != {t}")
        rows.append({"espn_name": name, "gold": g, "silver": s, "bronze": b})
    return rows


def map_names(rows: list[dict], valid_names: set[str]) -> tuple[list[dict], list[str]]:
    """Map ESPN names onto the table's country_name, drop anything unknown, dedupe."""
    out, dropped, seen = [], [], set()
    for r in rows:
        candidates = [r["espn_name"]] + NAME_MAP.get(r["espn_name"], [])
        name = next((c for c in candidates if c in valid_names), None)
        if name is None:
            dropped.append(r["espn_name"])
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append({"country_name": name, "gold": r["gold"], "silver": r["silver"], "bronze": r["bronze"]})
    return out, dropped


def assign_ranks(rows: list[dict]) -> None:
    """Standard tally order: gold, then silver, then bronze; ties share a rank (1,2,2,4).
    Countries with no medals get rank = None (unranked) instead of all tying for 1st."""
    rows.sort(key=lambda r: (-r["gold"], -r["silver"], -r["bronze"], r["country_name"]))
    prev, rank = None, 0
    for i, r in enumerate(rows, 1):
        key = (r["gold"], r["silver"], r["bronze"])
        if key != prev:
            rank, prev = i, key
        r["rank"] = rank if sum(key) > 0 else None


# --------------------------------------------------------------------------- supabase
def sb_request(method: str, path: str, body=None, extra_headers=None):
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
               "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt else None


def fetch_db_rows() -> dict[str, dict]:
    rows = sb_request("GET", f"{TABLE}?select=country_name,short_name,gold,silver,bronze")
    return {r["country_name"]: r for r in rows}


def upsert(rows: list[dict]) -> None:
    sb_request("POST", f"{TABLE}?on_conflict=country_name", rows,
               {"Prefer": "resolution=merge-duplicates,return=minimal"})


# --------------------------------------------------------------------------- main
def save_debug(html: str, tag: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    p = DEBUG_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{tag}.html"
    p.write_text(html or "", encoding="utf-8")
    print(f"saved debug HTML -> {p}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--html", help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    if not args.dry_run:
        if not SUPABASE_KEY:
            hint = ""
            if os.environ.get("SUPABASE_KEY", "").startswith("sb_publishable"):
                hint = (" (SUPABASE_KEY in .env is the publishable key: it is read-only under RLS "
                        "and cannot write. Add SUPABASE_SERVICE_KEY=<service_role / secret key> "
                        "from Project Settings -> API Keys)")
            print("SUPABASE_SERVICE_KEY not set" + hint, file=sys.stderr)
            return 1
        if SUPABASE_KEY.startswith("sb_publishable"):
            print("SUPABASE_SERVICE_KEY is a publishable key; writes need the service_role / secret key",
                  file=sys.stderr)
            return 1

    html = Path(args.html).read_text(encoding="utf-8") if args.html else fetch_html()

    try:
        parsed = parse_tally(html)
        if not (MIN_ROWS <= len(parsed) <= MAX_ROWS):
            raise ValueError(f"parsed {len(parsed)} rows, expected {MIN_ROWS}-{MAX_ROWS}")

        db = fetch_db_rows() if SUPABASE_KEY else {}
        if db:
            valid = set(db)
        else:  # dry run without credentials: use the seeded list
            print("WARNING: no DB access, using the built-in 45-name seed list", file=sys.stderr)
            valid = SEED_NAMES

        rows, dropped = map_names(parsed, valid)
        if dropped:
            print(f"WARNING: dropped names not in {TABLE}: {dropped} -> add them to NAME_MAP", file=sys.stderr)
        if len(rows) < MIN_ROWS:
            raise ValueError(f"only {len(rows)} rows matched the table after mapping")

        # Guard: medal totals should never fall a lot between runs; a big drop means a bad page.
        if db and os.environ.get("FORCE") != "1":
            before = sum(r["gold"] + r["silver"] + r["bronze"] for r in db.values())
            after = sum(r["gold"] + r["silver"] + r["bronze"] for r in rows)
            if before > 0 and after < before * 0.9:
                raise ValueError(f"total medals dropped {before} -> {after}; refusing to write (FORCE=1 to override)")
    except Exception as e:
        save_debug(html, "parse_fail")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    assign_ranks(rows)
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r["short_name"] = db[r["country_name"]]["short_name"] if db else None
        r["updated_at"] = now

    top = ", ".join(f"{r['rank']}. {r['country_name']} {r['gold']}-{r['silver']}-{r['bronze']}" for r in rows[:5])
    print(f"{len(rows)} countries parsed | top: {top}")
    india = next((r for r in rows if r["country_name"] == "India"), None)
    if india:
        print(f"India: rank {india['rank']}, {india['gold']}G {india['silver']}S {india['bronze']}B")

    if args.dry_run:
        print(json.dumps(rows[:3], indent=2))
        return 0

    upsert(rows)
    print(f"upserted {len(rows)} rows into {TABLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
