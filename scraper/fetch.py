"""
Travis County (Austin, TX) Motivated Seller Lead Scraper v1.0

Primary source: tccsearch.org — Travis County Clerk Recorder (Aumentum
Recorder / Harris Recording Solutions, legacy ASP.NET WebForms — NOT the
Tyler PublicSearch SPA that Bexar/Nueces use; travis.tx.publicsearch.us
is essentially unpopulated for this county, confirmed dead end).

Confirmed live (see CLAUDE.md for full reconnaissance notes):
  - Disclaimer accept: a[id*="lnkAccept"].click()
  - Doc type checkboxes: input#cphNoMargin_f_dclDocType_N, .click()
    (NOT .checked = true — that silently no-ops on this site)
  - Submit: #cphNoMargin_SearchButtons1_btnSearch.click()
  - Pagination: select#...OptionsBar1_ItemList, set .value then
    itemChange(select) — NOT a normal <select> onchange
  - Results are parsed from rendered body text (regex), not DOM
    selectors — the grid's cell class names look session/build-generated
    (e.g. "igede12b8e") and aren't safe to depend on. Sale date is
    already on the results list (no per-doc click-through needed, unlike
    Bexar).

Enrichment: TCAD parcel/owner/valuation via ArcGIS
  (services1.arcgis.com/.../TCAD_Selected_Locations/FeatureServer/0 —
  the *_public/MapServer/0 layer has no owner/value fields, don't use it)
Code enforcement: Austin Code Dept complaint cases (Socrata API,
  data.austintexas.gov/resource/6wtj-zbtb.json)

Debt-stack schema: unlike Bexar/Nueces's single loan_amount field, this
scraper tracks tax/judgment/HOA/mechanics liens as discrete fields —
this repo is the test bed for backporting that to the other counties.
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

COUNTY = "travis"
TCC_BASE = "https://tccsearch.org"
# The real, full county parcel layer (386,682 records, confirmed live) —
# has PROP_ID/situs address/legal_desc but NO owner name or valuation.
# services1.arcgis.com/.../TCAD_Selected_Locations has owner+value fields
# but is a 33-record sample dataset, not usable for real lookups — do not
# use it despite it "matching" on a spot check.
TCAD_ARCGIS = "https://gis.traviscountytx.gov/server1/rest/services/Boundaries_and_Jurisdictions/TCAD_public/MapServer/0"
AUSTIN_CE_API = "https://data.austintexas.gov/resource/6wtj-zbtb.json"

RECORDS_PATH = Path("dashboard/records.json")
DATA_PATH    = Path("data/records.json")

TODAY        = datetime.now(timezone.utc)
# 2026-09-01: TODAY_NAIVE used to be datetime.now() -- the runner's local
# clock rather than Central time, where Travis County auctions actually
# happen. This is the identical bug pattern confirmed live in bexar-leads
# 2026-09-01: it purged 418 real leads the instant UTC crossed midnight
# into a sale date, hours before that date started in Central time (and
# combining a full datetime against a bare sale_date compounds it further
# -- a lead is wrongly "passed" any time after midnight on its own sale
# date, not just after the actual auction hour). Fixed the same way here
# before this scraper's first real run, proactively -- not yet exposed to
# it since this scraper's been paused, but the same auction_passed() /
# days_until_sale() functions have the identical structure.
TODAY_NAIVE  = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
RUN_TIMESTAMP = TODAY.strftime("%Y-%m-%dT%H:%M:%SZ")

SCRAPE_DAYS  = 90   # rolling retention window for leads with no sale_date
MAX_PAGES    = 15   # 20 records/page; early-exits on known_docs anyway

# ── ARV / on-market (HomeHarvest, free, no API key) ─────────────────────────
# Same method as bexar-leads/guadalupe-leads: homeharvest scrapes Realtor.com's
# public page data for an estimated_value + on-market status. Ported here
# 2026-09-04 -- see fetch_arv_homeharvest()/refresh_on_market_status() below
# for the full rationale (copied near-verbatim from bexar-leads/scraper/fetch.py).
ARV_FETCH_LIMIT        = 30   # max leads to look up via HomeHarvest/Realtor.com per run
ON_MARKET_STATUSES     = {"FOR_SALE", "PENDING", "FOR_RENT"}
ON_MARKET_REFRESH_DAYS = 7    # re-check a lead's market status at most this often
ON_MARKET_REFRESH_LIMIT = 15  # max already-checked leads to re-check per run

# Doc types to pull from tccsearch.org, mapped to our internal lead "type".
# checkbox `value` codes confirmed live against the site — see CLAUDE.md.
# Foreclosure-only for now per user request (2026-08-04) — other types
# (pre-fore/lis pendens/liens/judgments) and Code Enforcement disabled
# until foreclosure scraping itself is confirmed working end-to-end.
DOC_TYPES = {
    "FORECLOSURE": "NOF",     # Notice of Substitute Trustee Sale
}
_DISABLED_DOC_TYPES = {
    "APT SUB TR":  "APPT",    # Appointment of Substitute Trustee (pre-fore signal)
    "LIS PEND":    "LP",      # Lis Pendens
    "AJ":          "JUD",     # Abstract of Judgment
    "FED TAX":     "LNFED",   # Federal tax lien
    "ST TAX LIEN": "LNSTATE", # State tax lien
    "ML":          "LNMECH",  # Mechanics lien
    "ASSESS LIEN": "LNHOA",   # HOA/assessment lien
}


# ── HELPERS ───────────────────────────────────────────────────────────────────
def get_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    svc = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=svc, options=opts)


def fetch_json(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "TravisLeadsBot/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.debug(f"fetch_json error [{url}]: {e}")
        return None


def new_record(doc_number, lead_type, source="tccsearch", run_ts=None):
    return {
        "doc_number":          doc_number,
        "county":              COUNTY,
        "type":                lead_type,
        "source":              source,
        "owner":               "",
        "address":             "",
        "city":                "Austin",
        "zip":                 "",
        "date_filed":          "",
        "sale_date":           "",
        "days_until_sale":     None,
        "legal_desc":          "",
        "status_index":        "",   # Temp | Perm (tccsearch temp/permanent index)
        "global_id":           "",   # tccsearch detail-page id
        "trustee":             "",   # substitute trustee conducting the sale (NOT the owner)
        "lender":              "",   # servicer/lender named on the notice
        "absentee":            False,
        "is_entity":           False,
        "duplicate":           False,
        "is_new":              True,
        "score":               0,
        "flags":               [],
        "run_ts":              run_ts or RUN_TIMESTAMP,
        # ── Debt-stack (discrete fields, not one loan_amount) ──────────────
        "mortgage_balance_est": "",
        "tax_delinquent_amt":   "",
        "tax_delinquent_years": "",
        "judgment_amt":         "",
        "hoa_lien_amt":         "",
        "mechanics_lien_amt":   "",
        "other_lien_amt":       "",
        "total_liens_est":      "",
        "equity_est":           "",
        "equity_pct":           "",
        "ltv_est":              "",
        "stacked_liens":        [],   # doc types seen for this owner/address across runs
        # ── TCAD enrichment ─────────────────────────────────────────────────
        "market_value":        "",
        "appraised_val":       "",
        "assessed_val":        "",
        "value_history":       [],   # [{"year":"2026","appraised":426209}, ...] from TCAD
        "deed_date":           "",
        "prop_id":             "",
        "owner_mail_addr":     "",
        # ── ARV / on-market (HomeHarvest/Realtor.com) ───────────────────────
        "arv_estimate":        "",
        "arv_status":          "",
        "arv_sqft":            "",
        "arv_fetched_at":      "",
        "on_market":           False,
        "on_market_status":    "",
        "on_market_checked_at": "",
        # ── Code enforcement ────────────────────────────────────────────────
        "ce_case_id":          "",
        "ce_status":           "",
        "ce_priority":         "",
        "ce_case_type":        "",
        "ce_repeat_offender":  False,
        "ce_opened_date":      "",
        # ── Dashboard/CRM fields ────────────────────────────────────────────
        "dash_phone":          "",
        "dash_dispo":          "new",
        "dash_notes":          "",
        "ghl_pushed":          False,
        "ghl_id":              "",
    }


# ── TCCSEARCH.ORG SCRAPER ───────────────────────────────────────────────────
RESULT_RE = re.compile(
    r"(\d{9})\s+(\d{2}/\d{2}/\d{4})\s+[A-Z][A-Z &/'.\-]+?\s+"
    r"\[R\]\s*([^\n\[]+?)\s*\(\+\)\s*"
    r"(?:\[E\]\s*(\d{2}/\d{2}/\d{4})\s*\(\+\)\s*)?"
    r"(.+?)\s+(Temp|Perm)\b",
    re.DOTALL,
)


def _page_diag(driver):
    try:
        title = driver.title
        url = driver.current_url
        snippet = driver.execute_script("return document.body ? document.body.innerText.slice(0,400) : '(no body)';")
        return f"url={url} | title={title!r} | body_snippet={snippet!r}"
    except Exception as e:
        return f"(diag failed: {e})"


def accept_disclaimer(driver):
    from selenium.webdriver.support.ui import WebDriverWait

    driver.set_page_load_timeout(45)
    try:
        driver.get(f"{TCC_BASE}/RealEstate/SearchEntry.aspx")
    except Exception as e:
        log.warning(f"  accept_disclaimer: driver.get() error: {e}")

    def on_search_form(d):
        return (
            d.execute_script(
                "return document.querySelectorAll('input[id*=\"dclDocType\"]').length;"
            ) > 0
        )

    try:
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script(
                "return !!document.querySelector('a[id*=\"lnkAccept\"]') || "
                "document.querySelectorAll('input[id*=\"dclDocType\"]').length > 0;"
            )
        )
    except Exception:
        log.warning(f"  accept_disclaimer: 30s wait for disclaimer link/search form failed | {_page_diag(driver)}")

    clicked = driver.execute_script(
        "var a = document.querySelector('a[id*=\"lnkAccept\"]'); "
        "if (a) { a.click(); return true; } return false;"
    )
    if clicked:
        try:
            WebDriverWait(driver, 30).until(on_search_form)
            log.info("  accept_disclaimer: clicked accept, search form loaded")
        except Exception:
            log.warning(f"  accept_disclaimer: clicked accept but search form never appeared | {_page_diag(driver)}")
    elif on_search_form(driver):
        log.info("  accept_disclaimer: already on search form (no disclaimer this time)")
    else:
        log.warning(f"  accept_disclaimer: no disclaimer link found and not on search form either | {_page_diag(driver)}")


def select_doc_type(driver, code):
    """Check exactly one doc-type checkbox by its internal value code."""
    js = """
        var target = arguments[0];
        var boxes = document.querySelectorAll('input[type=checkbox][id*="dclDocType"]');
        for (var i = 0; i < boxes.length; i++) {
            if (boxes[i].checked) boxes[i].click();  // clear any stale state
        }
        for (var i = 0; i < boxes.length; i++) {
            if (boxes[i].value === target) { boxes[i].click(); return true; }
        }
        return false;
    """
    return driver.execute_script(js, code)


def submit_search(driver):
    from selenium.webdriver.support.ui import WebDriverWait

    driver.execute_script("""
        var btn = document.getElementById('cphNoMargin_SearchButtons1_btnSearch');
        if (btn) btn.click();
    """)
    try:
        WebDriverWait(driver, 20).until(
            lambda d: any(s in d.find_element("tag name", "body").text
                          for s in ("records found", "No Results Found", "Error While Running Search"))
        )
    except Exception:
        log.warning("  submit_search: timed out waiting for results/no-results/error text")
    time.sleep(1)


def goto_results_page(driver, page_num):
    js = """
        var n = arguments[0];
        var sel = document.getElementById('cphNoMargin_cphNoMargin_OptionsBar1_ItemList');
        if (!sel) return false;
        sel.value = String(n);
        if (typeof itemChange === 'function') { itemChange(sel); }
        else { sel.dispatchEvent(new Event('change', {bubbles:true})); }
        return true;
    """
    ok = driver.execute_script(js, page_num)
    time.sleep(2)
    return ok


# ── DOCUMENT DETAIL (real owner) ────────────────────────────────────────────
# 2026-09-01: the search-RESULTS index's "[R]" name -- what this scraper
# used to treat as "owner" -- is actually the substitute TRUSTEE, not the
# homeowner. Confirmed live: searching Party Name = "ZAVALA ANGELA" (the
# most common "[R]" name in a live pull) returned the SAME 300-record
# total as the unfiltered doc-type search -- she's the trustee handling
# nearly the entire foreclosure docket for multiple lenders, not one
# homeowner in 300 places. Opening an actual document detail page shows
# the real structure: a "Lender/Trustee" section (trustee name, lender
# company) and a completely separate "Sale Date/Owner" section (sale
# date, then the real owner's name) -- confirmed against doc 202641153:
# [R] index said "ZAVALA ANGELA"; the detail page's Sale Date/Owner said
# "KIMBROUGH DEWAYNE". Real text, not an image -- no OCR needed, unlike
# Bexar's equivalent per-document enrichment.
DETAIL_INSTNUM_RE = re.compile(r"Instrument #:[ \t]+(\d+)")
DETAIL_OWNER_RE    = re.compile(r"Sale Date/Owner\s*\n+1[ \t]+[^\n]*\n2[ \t]+([^\n\t]+)")
DETAIL_TRUSTEE_RE  = re.compile(r"Lender/Trustee\s*\n+1[ \t]+([^\n\t]+?)[ \t]+[^\n]*\n2[ \t]+([^\n\t]+)")


def _clean_name(s):
    return " ".join((s or "").split()).title()


def click_doc_detail(driver, doc_number, timeout=15):
    """From a results LIST page, click the row link for doc_number and
    land on its detail page. Returns True/False."""
    from selenium.webdriver.support.ui import WebDriverWait
    clicked = driver.execute_script("""
        var docNum = arguments[0];
        var links = document.querySelectorAll('a');
        for (var i = 0; i < links.length; i++) {
            if (links[i].textContent.trim() === docNum) { links[i].click(); return true; }
        }
        return false;
    """, doc_number)
    if not clicked:
        log.warning(f"  click_doc_detail: no row link found for {doc_number}")
        return False
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: "Sale Date/Owner" in d.find_element("tag name", "body").text
        )
        return True
    except Exception:
        log.warning(f"  click_doc_detail: detail page never showed Sale Date/Owner for {doc_number} | {_page_diag(driver)}")
        return False


def jump_detail_index(driver, idx, timeout=15):
    """From a document detail page, jump to a different document at
    position idx (0-based) within the SAME results page via the
    within-page item dropdown -- avoids a full back-to-list round trip
    per document. Returns True/False."""
    from selenium.webdriver.support.ui import WebDriverWait
    try:
        before = driver.execute_script("return document.body.innerText;")
    except Exception:
        before = ""
    ok = driver.execute_script("""
        var n = arguments[0];
        var sel = document.getElementById('cphNoMargin_OptionsBar1_ItemList');
        if (!sel || n >= sel.options.length) return false;
        sel.selectedIndex = n;
        if (typeof itemChange === 'function') { itemChange(sel); }
        sel.dispatchEvent(new Event('change', {bubbles:true}));
        return true;
    """, idx)
    if not ok:
        return False
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.body.innerText;") != before
        )
        return True
    except Exception:
        log.warning(f"  jump_detail_index: page content didn't change for index {idx}")
        return False


def extract_detail_fields(driver):
    # Selenium's WebElement.text normalizes/collapses internal whitespace
    # (including the tabs the detail regexes above depend on) -- confirmed
    # live this broke every single match on the first attempt (instrument
    # number came back empty too, not just owner). document.body.innerText
    # via a raw JS call preserves the real tab structure -- this is what
    # was actually tested against when the regexes above were written.
    body_text = driver.execute_script("return document.body.innerText;") or ""
    result = {"instrument": "", "owner": "", "trustee": "", "lender": ""}
    m = DETAIL_INSTNUM_RE.search(body_text)
    if m:
        result["instrument"] = m.group(1).strip()
    m = DETAIL_OWNER_RE.search(body_text)
    if m:
        result["owner"] = _clean_name(m.group(1))
    m = DETAIL_TRUSTEE_RE.search(body_text)
    if m:
        result["trustee"] = _clean_name(m.group(1))
        result["lender"]  = _clean_name(m.group(2))
    if not result["owner"]:
        log.warning(f"  extract_detail_fields: no owner matched | body snippet: {body_text[:300]!r}")
    return result


def fetch_real_owners(driver, rows):
    """Given the rows parsed off a single results-list page (in the same
    top-to-bottom order the page displayed them), visit each one's detail
    page and replace the index's trustee-as-owner with the real owner.
    Mutates `rows` in place. Matches results back to rows by instrument
    number (not by position) so this is correct even if the within-page
    dropdown's ordering doesn't exactly mirror the list's ordering.
    """
    by_doc = {r["doc_number"]: r for r in rows}
    if not rows:
        return

    if not click_doc_detail(driver, rows[0]["doc_number"]):
        log.warning("  fetch_real_owners: could not open first detail page for this page — "
                    "leaving trustee names as owner for this page's rows")
        return

    for i in range(len(rows)):
        if i > 0:
            if not jump_detail_index(driver, i):
                continue
        details = extract_detail_fields(driver)
        rec = by_doc.get(details["instrument"])
        if not rec:
            log.warning(f"  fetch_real_owners: detail instrument {details['instrument']!r} "
                        f"didn't match any row on this page")
            continue
        if details["owner"]:
            rec["owner"] = details["owner"]
        rec["trustee"] = details["trustee"]
        rec["lender"]  = details["lender"]


def parse_results_page(driver, debug_label=""):
    from selenium.webdriver.common.by import By
    body_text = driver.find_element(By.TAG_NAME, "body").text
    records = []
    for m in RESULT_RE.finditer(body_text):
        doc_num, filed, name, sale_date, legal, status = m.groups()
        records.append({
            "doc_number":   doc_num.strip(),
            "date_filed":   filed.strip(),
            "owner":        name.strip().title(),
            "sale_date":    sale_date.strip() if sale_date else "",
            "legal_desc":   legal.strip(),
            "status_index": status.strip(),
        })

    if not records:
        # Self-diagnosing: log enough of the raw page so a 0-match run is
        # debuggable from the Action log instead of a silent empty result.
        has_9digit = bool(re.search(r"\b\d{9}\b", body_text))
        log.warning(
            f"  [{debug_label}] parse_results_page: 0 regex matches | "
            f"body_text_len={len(body_text)} | contains_9digit_number={has_9digit} | "
            f"url={driver.current_url}"
        )
        log.warning(f"  [{debug_label}] body text snippet: {body_text[:600]!r}")

    return records


ADDR_IN_LEGAL_RE = re.compile(
    r"LOC\s+(\d{1,6}[A-Z0-9 .\-/]{3,40}?)\s+"
    r"(AUSTIN|DEL VALLE|PFLUGERVILLE|MANOR|LAGO VISTA|LAKEWAY|BEE CAVE|"
    r"CEDAR PARK|LEANDER|ROLLINGWOOD|WEST LAKE HILLS|BRIARCLIFF)\s+TX\s+(\d{5})",
    re.IGNORECASE,
)


def parse_address_from_legal(legal_desc):
    m = ADDR_IN_LEGAL_RE.search(legal_desc or "")
    if not m:
        return "", "", ""
    return m.group(1).strip().upper(), m.group(2).strip().title(), m.group(3).strip()


def scrape_tccsearch(known_docs, driver, doc_code, lead_type):
    log.info(f"Scraping tccsearch.org: {doc_code} -> type={lead_type}")
    if not select_doc_type(driver, doc_code):
        log.warning(f"  Could not find checkbox for doc code {doc_code} — skipping")
        return []
    submit_search(driver)

    from selenium.webdriver.common.by import By
    banner = driver.find_element(By.TAG_NAME, "body").text
    m = re.search(r"Criteria:.{0,120}", banner)
    log.info(f"  Search criteria banner: {m.group(0) if m else '(not found — see warning below if 0 rows)'}")

    new_leads = []
    zero_new_streak = 0

    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            if not goto_results_page(driver, page):
                log.info(f"  Page {page}: pagination control not found — stopping")
                break

        rows = parse_results_page(driver, debug_label=f"{doc_code} p{page}")
        if not rows:
            log.info(f"  Page {page}: 0 rows — stopping")
            break

        new_rows = [r for r in rows if r["doc_number"] not in known_docs]
        if new_rows:
            # row["owner"] up to this point is the search-index's [R] name,
            # which is the substitute TRUSTEE, not the homeowner (see the
            # v2.0 note above fetch_real_owners). Overwrite it with the
            # real owner from each document's own detail page before
            # building records. Leaves the working tree on a results LIST
            # page again (any page — goto_results_page() below sets the
            # target page by value, not relatively) once done.
            fetch_real_owners(driver, new_rows)
            driver.execute_script("""
                var a = Array.from(document.querySelectorAll('a'))
                    .find(a => a.textContent.trim() === 'Back to Results');
                if (a) a.click();
            """)
            time.sleep(1.5)

        page_new = 0
        for row in rows:
            doc_num = row["doc_number"]
            if doc_num in known_docs:
                continue
            page_new += 1
            known_docs.add(doc_num)

            rec = new_record(doc_num, lead_type)
            rec["owner"]        = row["owner"]
            rec["trustee"]      = row.get("trustee", "")
            rec["lender"]       = row.get("lender", "")
            rec["date_filed"]   = row["date_filed"]
            rec["sale_date"]    = row["sale_date"]
            rec["legal_desc"]   = row["legal_desc"]
            rec["status_index"] = row["status_index"]

            addr, city, zipc = parse_address_from_legal(row["legal_desc"])
            if addr:
                rec["address"] = addr
                rec["city"]    = city
                rec["zip"]     = zipc

            d = days_until_sale(rec["sale_date"])
            rec["days_until_sale"] = d

            new_leads.append(rec)

        log.info(f"  Page {page}: {len(rows)} rows, {page_new} new")
        if page_new == 0:
            zero_new_streak += 1
        else:
            zero_new_streak = 0
        if zero_new_streak >= 2:
            log.info("  2 consecutive pages with 0 new — stopping early")
            break

    log.info(f"  {doc_code}: {len(new_leads)} new leads")
    return new_leads


def days_until_sale(sale_date_str):
    if not sale_date_str:
        return None
    try:
        dt = datetime.strptime(sale_date_str.strip(), "%m/%d/%Y")
        return max((dt.date() - TODAY_NAIVE.date()).days, 0)
    except Exception:
        return None


def auction_passed(sale_date_str):
    if not sale_date_str:
        return False
    try:
        dt = datetime.strptime(sale_date_str.strip(), "%m/%d/%Y")
        # Travis auctions run first-Tuesday-of-the-month, 10am, and this
        # scraper has no reliable way to know whether TODAY's single
        # monthly auction has already happened at whatever time it
        # actually runs -- treat sale_date == today as already passed
        # too (<=, not <), rather than risk showing a dead lead for the
        # rest of the day. Confirmed relevant on this scraper's first
        # real run, 2026-09-01, which happens to BE the first Tuesday.
        return dt.date() <= TODAY_NAIVE.date()
    except Exception:
        return False


def filed_within_window(date_str, days=SCRAPE_DAYS):
    if not date_str:
        return True
    try:
        dt = datetime.strptime(date_str.strip(), "%m/%d/%Y")
        return (TODAY_NAIVE - dt).days <= days
    except Exception:
        return True


# ── TCAD ENRICHMENT (ArcGIS) ────────────────────────────────────────────────
def tcad_query(where, out_fields="*", limit=5):
    params = urllib.parse.urlencode({
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "false",
        "resultRecordCount": limit,
        "f": "json",
    })
    data = fetch_json(f"{TCAD_ARCGIS}/query?{params}")
    if not data:
        return []
    return [f.get("attributes", {}) for f in data.get("features", [])]


def enrich_from_tcad(rec):
    """
    Matches by situs address (parsed from the legal description) against
    the full county parcel layer. This confirms the address and gets
    PROP_ID + a prodigycad.com detail-page link, but NOT owner name or
    market value — TCAD's public ArcGIS layer doesn't expose those (see
    CLAUDE.md). Owner comes from the notice's own detail page (see
    fetch_real_owners); market value comes from fetch_tcad_property_value
    below, using this record's prop_id.
    """
    addr = (rec.get("address") or "").strip()
    if not addr:
        return
    num = addr.split()[0] if addr.split() else ""
    if not num.isdigit():
        return
    esc = addr.replace("'", "''")
    feats = tcad_query(f"situs_address LIKE UPPER('%{esc}%')", limit=3)
    if not feats:
        return
    a = feats[0]
    rec["prop_id"]    = a.get("PROP_ID") or ""
    rec["legal_desc"] = rec.get("legal_desc") or a.get("legal_desc") or ""
    rec["_tcad_hyperlink"] = a.get("hyperlink") or ""


# 2026-09-02: confirmed live -- travis.prodigycad.com/property-detail/{PROP_ID}
# (the real production host; the "stage." version from the earlier notes just
# redirects here) exposes full appraised-value history with no login. It's a
# React SPA -- the underlying API (prod-container.trueprodigyapi.com) needs a
# per-tenant client config this scraper couldn't cleanly replicate (hit a
# backend DB-host-resolution error trying), so this drives a real browser and
# reads the rendered text instead, the same approach already used elsewhere
# in this codebase. One real gotcha: the Values section is scroll-triggered
# lazy content, not present in the DOM on page load -- confirmed live, "Values"
# heading renders immediately but "Appraised" only appears after scrolling
# roughly halfway down the page. Values are real text, not an image -- no OCR
# needed, unlike Bexar's equivalent deed/ARV lookup.
TCAD_DETAIL_BASE = "https://travis.prodigycad.com/property-detail/"
CURRENT_APPRAISED_RE = re.compile(r"CURRENT VALUES.*?Appraised\n\n([\d,]+)\n\nValue Limitation", re.DOTALL)
VALUE_HISTORY_ROW_RE = re.compile(r"^(\d{4})\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)$", re.MULTILINE)


def fetch_tcad_property_value(driver, prop_id, timeout=15, _retried=False):
    from selenium.webdriver.support.ui import WebDriverWait

    result = {"market_value": "", "appraised_val": "", "value_history": []}
    try:
        driver.set_page_load_timeout(timeout)
        driver.get(f"{TCAD_DETAIL_BASE}{prop_id}")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.4);")
        WebDriverWait(driver, timeout).until(
            lambda d: "Appraised" in (d.execute_script("return document.body.innerText;") or "")
        )
        text = driver.execute_script("return document.body.innerText;") or ""

        m = CURRENT_APPRAISED_RE.search(text)
        if m:
            val = m.group(1).replace(",", "")
            result["appraised_val"] = val
            result["market_value"] = val

        history = []
        for row in VALUE_HISTORY_ROW_RE.finditer(text):
            year, land, impr, excl, appraised, adj, net = row.groups()
            history.append({"year": year, "appraised": int(appraised.replace(",", ""))})
        if history:
            result["value_history"] = history
    except Exception as e:
        log.debug(f"  fetch_tcad_property_value: prop_id={prop_id} failed: {e}")

    # Confirmed live: a freshly-created driver's very FIRST page load to this
    # site can be slow enough to miss the wait window (Google Maps API,
    # several JS chunks, cold DNS) even though the site itself is fine --
    # every subsequent call on the same driver succeeded in under 2s. One
    # retry covers this without masking a genuinely-down site (retry only
    # fires once).
    if not result["appraised_val"] and not _retried:
        return fetch_tcad_property_value(driver, prop_id, timeout=timeout, _retried=True)
    return result


def fetch_arv_homeharvest(records):
    """
    Free ARV estimate via homeharvest (pip, MIT license) scraping Realtor.com's
    public page data -- no API key, no cost. Ported near-verbatim from
    bexar-leads/scraper/fetch.py 2026-09-04 (same method already live on
    Bexar and Guadalupe). Realtor.com blends CoreLogic, Collateral
    Analytics, and Quantarium AVMs into an estimated_value field that's
    present even for off-market/distressed properties.

    Kept as a soft dependency: any failure (network, no match, library
    error) just leaves arv_estimate blank rather than breaking the run --
    the lead still has TCAD's appraised_val as a fallback value signal.

    No Selenium driver needed -- homeharvest does its own HTTP requests.
    """
    import pandas as pd
    from homeharvest import scrape_property

    def clean(val):
        if val is None or pd.isna(val):
            return None
        s = str(val).strip()
        return None if s in ("", "nan", "<NA>", "None") else val

    candidates = [
        r for r in records
        if r.get("address") and not r.get("arv_estimate")
    ]
    candidates = candidates[:ARV_FETCH_LIMIT]

    if not candidates:
        log.info("ARV (HomeHarvest): no eligible leads -- skipping")
        return records

    log.info(f"ARV (HomeHarvest): {len(candidates)} leads to look up (cap={ARV_FETCH_LIMIT})")
    fetched = 0
    errors  = 0

    for rec in candidates:
        full_addr = f"{rec['address']}, {rec.get('city', '')}, TX {rec.get('zip', '')}".strip(", ")
        try:
            df = scrape_property(location=full_addr)
            now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

            if df is None or len(df) == 0:
                log.info(f"  ARV [{rec.get('doc_number')}] {full_addr}: no match on Realtor.com")
                rec["on_market_checked_at"] = now_iso
                continue

            row = df.iloc[0]
            status = clean(row.get("status")) or ""
            rec["on_market"]            = status in ON_MARKET_STATUSES
            rec["on_market_status"]     = status
            rec["on_market_checked_at"] = now_iso

            est = clean(row.get("estimated_value"))
            if est is not None:
                rec["arv_estimate"]   = int(est)
                rec["arv_status"]     = status
                sqft_val = clean(row.get("sqft"))
                rec["arv_sqft"]       = int(sqft_val) if sqft_val is not None else None
                rec["arv_fetched_at"] = now_iso
                fetched += 1
                log.info(f"  ARV [{rec.get('doc_number')}] {full_addr}: ${int(est):,} (status={status})")
            else:
                log.info(f"  ARV [{rec.get('doc_number')}] {full_addr}: matched but no estimated_value (status={status})")
        except Exception as e:
            log.warning(f"  ARV [{rec.get('doc_number')}] {full_addr}: error: {e}")
            errors += 1
            # Fall back to TCAD's own appraised_val rather than leaving
            # arv_estimate permanently blank when Realtor.com blocks the
            # request -- lower-confidence than a true Realtor.com estimate,
            # marked APPRAISED_FALLBACK so dashboard code can tell the two
            # apart (same convention as bexar-leads).
            tcad_value = rec.get("appraised_val")
            if tcad_value and not rec.get("arv_estimate"):
                try:
                    rec["arv_estimate"]   = int(float(tcad_value))
                    rec["arv_status"]     = "APPRAISED_FALLBACK"
                    rec["arv_fetched_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                except (TypeError, ValueError):
                    pass
        finally:
            time.sleep(1)

    log.info(f"ARV (HomeHarvest): {fetched} enriched, {errors} errors out of {len(candidates)} candidates")
    return records


def refresh_on_market_status(records):
    """
    Re-checks on-market status for leads that already went through the ARV
    pass above (excluded from those candidates) but haven't had a fresh
    on-market check in ON_MARKET_REFRESH_DAYS -- a lead can get listed by
    someone else weeks after we first looked it up. Ported from
    bexar-leads/scraper/fetch.py 2026-09-04, extra_property_data=False
    (matches the AuthenticationError workaround confirmed live there
    2026-08-26 -- this function only reads `status`, which survives
    without the deeper per-property call that estimated_value needs).
    """
    import pandas as pd
    from homeharvest import scrape_property

    def clean(val):
        if val is None or pd.isna(val):
            return None
        s = str(val).strip()
        return None if s in ("", "nan", "<NA>", "None") else val

    cutoff = datetime.utcnow() - timedelta(days=ON_MARKET_REFRESH_DAYS)

    def needs_refresh(r):
        if not r.get("address") or not r.get("arv_estimate"):
            return False  # never-checked leads are handled by the ARV pass
        checked_at = r.get("on_market_checked_at")
        if not checked_at:
            return True
        try:
            return datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ") < cutoff
        except Exception:
            return True

    candidates = [r for r in records if needs_refresh(r)]
    candidates = candidates[:ON_MARKET_REFRESH_LIMIT]

    if not candidates:
        log.info("On-market refresh: no eligible leads -- skipping")
        return records

    log.info(f"On-market refresh: {len(candidates)} leads to re-check (cap={ON_MARKET_REFRESH_LIMIT})")
    changed = 0
    errors  = 0

    for rec in candidates:
        full_addr = f"{rec['address']}, {rec.get('city', '')}, TX {rec.get('zip', '')}".strip(", ")
        try:
            df = scrape_property(location=full_addr, extra_property_data=False)
            now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            was_on_market = bool(rec.get("on_market"))

            if df is None or len(df) == 0:
                rec["on_market_checked_at"] = now_iso
                continue

            status = clean(df.iloc[0].get("status")) or ""
            rec["on_market"]            = status in ON_MARKET_STATUSES
            rec["on_market_status"]     = status
            rec["on_market_checked_at"] = now_iso

            if rec["on_market"] != was_on_market:
                changed += 1
                log.info(f"  On-market [{rec.get('doc_number')}] {full_addr}: "
                         f"{was_on_market} -> {rec['on_market']} (status={status})")
        except Exception as e:
            log.warning(f"  On-market [{rec.get('doc_number')}] {full_addr}: error: {e}")
            errors += 1
        finally:
            time.sleep(1)

    log.info(f"On-market refresh: {changed} status changes, {errors} errors out of {len(candidates)} candidates")
    return records


# ── CODE ENFORCEMENT (Austin Socrata) ───────────────────────────────────────
def fetch_code_enforcement(known_docs, limit=200):
    app_token = os.environ.get("SOCRATA_APP_TOKEN", "")
    params = {
        "$limit": limit,
        "$order": "opened_date DESC",
        "$where": "status = 'Active'",
    }
    url = f"{AUSTIN_CE_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "TravisLeadsBot/1.0",
        "Accept": "application/json",
        **({"X-App-Token": app_token} if app_token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            cases = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning(f"Austin CE fetch error: {e}")
        return []

    new_leads = []
    for c in cases:
        case_id = c.get("case_id") or c.get("servicerequestnumber")
        if not case_id:
            continue
        doc_key = f"CE-{case_id}"
        if doc_key in known_docs:
            continue
        known_docs.add(doc_key)

        rec = new_record(doc_key, "CE", source="code_enforcement")
        rec["owner"]              = ""
        street = c.get("street_name", "")
        house  = c.get("house_number", "")
        rec["address"]            = f"{house} {street}".strip()
        rec["city"]               = c.get("city", "Austin") or "Austin"
        rec["zip"]                = c.get("zip_code", "")
        rec["date_filed"]         = c.get("opened_date", "")[:10]
        rec["ce_case_id"]         = case_id
        rec["ce_status"]          = c.get("status", "")
        rec["ce_priority"]        = c.get("priority", "")
        rec["ce_case_type"]       = c.get("case_type", "")
        rec["ce_repeat_offender"] = str(c.get("repeatoffenderrelated", "")).lower() in ("true", "yes", "1")
        rec["ce_opened_date"]     = c.get("opened_date", "")[:10]
        rec["flags"].append("CODE ENFORCE")
        if rec["ce_repeat_offender"]:
            rec["flags"].append("REPEAT OFFENDER")

        new_leads.append(rec)

    log.info(f"Austin Code Enforcement: {len(new_leads)} new leads")
    return new_leads


# ── SCORING ───────────────────────────────────────────────────────────────────
def score_record(rec):
    s = 0
    if rec.get("address"):  s += 2
    if rec.get("owner"):    s += 2
    if rec.get("type") in ("NOF", "APPT"): s += 3
    if rec.get("sale_date"): s = min(s + 2, 10)
    if rec.get("prop_id"): s = min(s + 1, 10)
    stacked = len(rec.get("stacked_liens") or [])
    if stacked >= 2:
        s = min(s + 2, 10)
    if rec.get("source") == "code_enforcement":
        s += 1
        if str(rec.get("ce_status", "")).lower() == "active": s += 1
        if rec.get("ce_repeat_offender"): s += 1
    return min(s, 10)


# ── MERGE / DEDUP / PURGE ───────────────────────────────────────────────────
def load_existing():
    if RECORDS_PATH.exists():
        try:
            return json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Could not load existing records.json: {e}")
    return []


def purge_past_auctions(records):
    keep = []
    purged = 0
    for r in records:
        if r.get("type") in ("NOF",) and r.get("sale_date"):
            if auction_passed(r["sale_date"]) and not (r.get("dash_phone") or r.get("ghl_pushed")):
                purged += 1
                continue
        keep.append(r)
    if purged:
        log.info(f"Purged {purged} past-auction leads")
    return keep


def stack_liens(records):
    """
    Flag leads sharing the same owner+address across multiple doc types as
    'stacked' — the strongest single motivation signal per competitor
    research (see CLAUDE.md). Groups by (owner, address); anything with
    2+ distinct doc types gets stacked_liens populated on every member.
    """
    groups = {}
    for r in records:
        key = (r.get("owner", "").upper().strip(), r.get("address", "").upper().strip())
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, set()).add(r.get("type", ""))

    for r in records:
        key = (r.get("owner", "").upper().strip(), r.get("address", "").upper().strip())
        types = groups.get(key, set())
        if len(types) >= 2:
            r["stacked_liens"] = sorted(types)
            if "STACKED" not in r["flags"]:
                r["flags"].append("STACKED")


# ── DASHBOARD ─────────────────────────────────────────────────────────────────
def build_dashboard(records):
    os.makedirs("dashboard", exist_ok=True)
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
    json_str = json.dumps(clean, separators=(",", ":"), ensure_ascii=True)
    with open("dashboard/records.json", "w", encoding="utf-8") as f:
        f.write(json_str)
    log.info(f"Dashboard: {len(clean)} records, {os.path.getsize('dashboard/records.json'):,} bytes")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Travis County Lead Scraper v1.0")
    log.info(f"Run: {RUN_TIMESTAMP}")
    log.info("=" * 60)

    existing = load_existing()
    dropped_ce = sum(1 for r in existing if r.get("type") == "CE")
    existing = [r for r in existing if r.get("type") != "CE"]
    if dropped_ce:
        log.info(f"Dropped {dropped_ce} existing CE records (foreclosure-only mode)")
    for r in existing:
        r["is_new"] = False
    known_docs = {r["doc_number"] for r in existing if r.get("doc_number")}
    log.info(f"Loaded {len(existing)} existing records | {len(known_docs)} known doc numbers")

    driver = get_driver()
    all_new = []
    try:
        accept_disclaimer(driver)
        for code, lead_type in DOC_TYPES.items():
            try:
                leads = scrape_tccsearch(known_docs, driver, code, lead_type)
                all_new.extend(leads)
            except Exception as e:
                log.error(f"scrape_tccsearch error for {code}: {e}", exc_info=True)
            # fresh search entry for the next doc type
            accept_disclaimer(driver)

        log.info(f"tccsearch.org total new: {len(all_new)}")

        # TCAD ArcGIS enrichment — cap per run to avoid hammering the endpoint.
        # Plain HTTP, doesn't need the browser, but has to run before the
        # prodigycad value lookup below since it's what fills prop_id.
        enrich_targets = [r for r in all_new if r["type"] in ("NOF", "APPT") and r.get("owner")][:60]
        log.info(f"TCAD ArcGIS enrichment: {len(enrich_targets)} candidates")
        for r in enrich_targets:
            try:
                enrich_from_tcad(r)
            except Exception as e:
                log.debug(f"TCAD ArcGIS enrich error [{r['doc_number']}]: {e}")
            time.sleep(0.3)

        # TCAD property-value enrichment (travis.prodigycad.com) — needs the
        # browser, so has to happen before driver.quit() below. Covers both
        # this run's new leads AND any existing lead still missing a value
        # (catch-up for leads that fell outside a previous run's budget),
        # capped so a normal run stays a reasonable length.
        value_targets = [
            r for r in (existing + all_new)
            if r.get("prop_id") and not r.get("appraised_val")
        ][:60]
        log.info(f"TCAD property-value enrichment: {len(value_targets)} candidates")
        value_filled = 0
        for r in value_targets:
            try:
                vals = fetch_tcad_property_value(driver, r["prop_id"])
                if vals.get("appraised_val"):
                    r["appraised_val"]  = vals["appraised_val"]
                    r["market_value"]   = vals["market_value"]
                    r["value_history"]  = vals["value_history"]
                    value_filled += 1
            except Exception as e:
                log.debug(f"TCAD value enrich error [{r.get('doc_number')}]: {e}")
            time.sleep(0.5)
        log.info(f"TCAD property-value enrichment: {value_filled}/{len(value_targets)} filled")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Code Enforcement disabled for now — foreclosure-only per user request,
    # and CE data has no owner name anyway (see fetch_code_enforcement docstring).

    all_records = existing + all_new

    # ARV + on-market via HomeHarvest (free, Realtor.com, no Selenium needed) —
    # same method as bexar-leads/guadalupe-leads, ported 2026-09-04.
    try:
        all_records = fetch_arv_homeharvest(all_records)
    except Exception as e:
        log.warning(f"ARV (HomeHarvest) error: {e}")

    try:
        all_records = refresh_on_market_status(all_records)
    except Exception as e:
        log.warning(f"On-market refresh error: {e}")
    all_records = purge_past_auctions(all_records)
    stack_liens(all_records)

    before = len(all_records)
    all_records = [
        r for r in all_records
        if r.get("type") in ("CE", "APPT")
        or r.get("ghl_pushed") or r.get("dash_phone")
        or filed_within_window(r.get("date_filed", ""), SCRAPE_DAYS)
    ]
    log.info(f"{SCRAPE_DAYS}d filter: {before} -> {len(all_records)}")

    for r in all_records:
        r["score"] = score_record(r)
        r["days_until_sale"] = days_until_sale(r.get("sale_date", ""))

    def sort_key(r):
        d = r.get("days_until_sale")
        urgency = 0 if (d is not None and d <= 14) else (1 if (d is not None and d <= 30) else 2)
        return (urgency, -r["score"], d if d is not None else 9999)

    all_records.sort(key=sort_key)

    total    = len(all_records)
    new_ct   = sum(1 for r in all_records if r.get("is_new"))
    nof_ct   = sum(1 for r in all_records if r.get("type") == "NOF")
    stacked  = sum(1 for r in all_records if r.get("stacked_liens"))
    ce_ct    = sum(1 for r in all_records if r.get("type") == "CE")
    enriched = sum(1 for r in all_records if r.get("prop_id"))

    log.info(f"Final: {total} total | {new_ct} new | NOF={nof_ct} | CE={ce_ct}")
    log.info(f"       Stacked (2+ lien types): {stacked} | TCAD parcel-matched: {enriched}")

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(all_records, indent=2), encoding="utf-8")
    build_dashboard(all_records)
    log.info("Done.")


if __name__ == "__main__":
    main()
