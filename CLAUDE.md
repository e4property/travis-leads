# Travis County (Austin) Motivated Seller Lead Scraper

## Project Overview
Scraper + dashboard for Travis County foreclosure/lien leads, modeled on
bexar-leads and nueces-leads but **fully independent** — separate repo,
separate GitHub Pages dashboard, separate GitHub Actions, zero shared code
or state with the other two counties.

## Data Sources (all confirmed live, Aug 2026)

### Primary: tccsearch.org — Travis County Clerk Recorder
**NOT the same platform as Bexar/Nueces.** This is Aumentum Recorder
(Harris Recording Solutions), a legacy ASP.NET WebForms app with
Infragistics UI controls — not the Tyler `tx.publicsearch.us` React SPA.
`travis.tx.publicsearch.us` exists but is essentially unpopulated
(confirmed: FC/RP departments return "No documents to search in
department" or "Error with search query") — do not build against it.

**Flow to reach the search form:**
1. GET `https://tccsearch.org/RealEstate/SearchEntry.aspx`
2. Redirects to a disclaimer page. Click the accept link
   (`a[id*="lnkAccept"]`) — plain `.click()` works, coordinate clicks
   were unreliable in testing. This sets a session cookie; the disclaimer
   won't reappear for the rest of the session.
3. Lands on `Search Real Estate Index: Selection Criteria` form.

**Document type checkboxes** — 130 total, each `input[type=checkbox]`
with `id="cphNoMargin_f_dclDocType_N"` and a short internal `value` code
(NOT the same as the visible label). Confirmed codes for motivated-seller
signals:

| Label | value code |
|---|---|
| NOTICE OF SUBSTITUTE TRUSTEE SALE (the foreclosure notice — primary lead type) | `FORECLOSURE` |
| APPT OF SUBSTITUTE TRUSTEE (pre-foreclosure signal) | `APT SUB TR` |
| LIS PENDENS | `LIS PEND` |
| ABSTRACT OF JUDGMENT | `AJ` |
| JUDGEMENT | `JDGMT` |
| STATE JUDGEMENT | `STATE JUDGEM` |
| FEDERAL TAX LIENS AND NOTICES | `FED TAX` |
| STATE TAX LIEN | `ST TAX LIEN` |
| MECHANICS LIEN | `ML` |
| HOSPITAL LIEN | `HOSP LIEN` |
| CHILD SUPPORT LIEN | `CHILD SL` |
| ASSESSMENT LIEN (HOA) | `ASSESS LIEN` |
| BROKERS LIEN | `BROKERS LIEN` |
| DEED OF TRUST | `DT` |
| HOME EQUITY LOAN | `HE LIEN` |

Full label→code map for anything else needed: re-run the JS snippet below
in a browser console on the search page —
```js
Array.from(document.querySelectorAll('input[type=checkbox]')).map(c => ({
  id: c.id, value: c.value,
  label: document.querySelector(`label[for="${c.id}"]`)?.textContent.trim()
}))
```

**Checking a box reliably:** `document.getElementById(id).click()` (real
`.click()`, not `.checked = true` — the latter doesn't fire the handlers
Aumentum needs and silently no-ops).

**Date range fields are unreliable to automate** — they're Infragistics
date-editor widgets with hidden `_clientState` sync fields
(`cphNoMargin_f_ddcDateFiledFrom_clientState` etc.), and typing into the
visible textbox via coordinate/ref click was flaky in testing (once landed
in the wrong field — combined Party Name textbox `cphNoMargin_f_txtParty`
— because refs go stale across page state changes). **Don't fight this.**
Instead: sort results by "Inst num, Descending" (or leave default) and
paginate from page 1, stopping as soon as a row's instrument number is
already in `known_docs` — same early-exit pattern already used in
bexar-leads/nueces-leads. No date field needed.

**Submitting:** `document.getElementById('cphNoMargin_SearchButtons1_btnSearch').click()`.

**Results page** — confirmed live structure (300 real
NOTICE OF SUBSTITUTE TRUSTEE SALE records returned, no date filter):
- One row per document: `#`, `Image` (View link, image not always available),
  `Instrument #` (e.g. `202641036`), `Book-Page`, `Date Filed`
  (`MM/DD/YYYY`), `Document Type`, `Name` (`[R] LASTNAME FIRST` — R =
  Respondent/grantor, i.e. the property owner being foreclosed on),
  a second bracketed line `[E] MM/DD/YYYY` **— this is the sale/auction
  date, already on the list page, no click-through required** (unlike
  Bexar where loan/sale details need a per-doc page visit), then
  `Legal Description` (subdivision/lot/block, sometimes includes a raw
  street address like `LOC 11314 GATLING GUN LN AUSTIN TX 78748`, same
  ambiguity Nueces already solved with `parse_legal_components()` — reuse
  that approach), and `Status` (`Temp` or `Perm` — docs move from the
  Temporary Index, 07/31/2026–08/04/2026 per the site banner, to
  Permanent after a few days).
- Pagination: **confirmed working.** It's a `<select id="cphNoMargin_cphNoMargin_OptionsBar1_ItemList">`
  with `onchange="itemChange(this)"`, options valued `"1"`, `"2"`, etc.
  (20 records/page). To go to page N: `sel.value = N;
  itemChange(sel);` then wait — tested live, "Showing Records 21 through
  40" appeared correctly after setting page 2.
- Auctions: first Tuesday of each month, 10:00am, west steps of the
  courthouse, 1000 Guadalupe St, Austin.

**Not yet verified — do before relying on in production:**
- Whether "View" image/detail links expose anything beyond the list
  (e.g. an actual loan/lien dollar amount) or require login/purchase
- Full grantee/trustee-company name shown on the `(+)` expand — some doc
  types (e.g. repeated "ZAVALA ANGELA") looked like a trustee/law-firm
  name rather than distinct owners; confirm before treating "Name" as
  the actual property owner across all doc types

### Tax foreclosure sales
Listings live at `https://travis.texas.realforeclose.com` (RealAuction.com
— same vendor many TX counties use). `tax-office.traviscountytx.gov/properties/foreclosed`
is procedural-only, no listing data. RealAuction blocked a plain HTTP
fetch (403) — needs a real browser session; unverified this session.

### Parcel / owner / valuation enrichment: TCAD via ArcGIS
**Correction (verified via `returnCountOnly=true`):** the research pass
that found `TCAD_Selected_Locations/FeatureServer/0` with owner/value
fields was misleading — that layer has **only 33 records total**, a tiny
sample dataset, not real county data. It happened to match on a spot
check ("TRAVISSO LTD") but returns 0 for real leads (`BRYANT`, `ZAVALA`
both tested, 0 results). **Do not use it.**

The real, comprehensive layer is:
`https://gis.traviscountytx.gov/server1/rest/services/Boundaries_and_Jurisdictions/TCAD_public/MapServer/0`
— confirmed **386,682 records** (the actual county parcel roll). Fields
confirmed via live sample query: `PROP_ID`, `geo_id`, `situs_num`,
`situs_street`, `situs_zip`, `situs_city`, `situs_address` (full string,
e.g. `"11502 TANGLEBRIAR TRL AUSTIN 78750"`), `sub_dec`, `legal_desc`,
`hyperlink` (→ `stage.travis.prodigycad.com/property-detail/{PROP_ID}`).
**No owner name, no valuation fields exist in this layer.**

Current scraper only uses this layer to confirm a parsed address and
get `PROP_ID` + the prodigycad detail link. **Owner mailing address and
market value are an open gap** — next step is verifying whether the
prodigycad `property-detail/{PROP_ID}` page (or `traviscad.org/propertysearch/`)
exposes them, the same way Bexar scrapes `bexar.trueautomation.com` for
deed history/ARV. Not yet attempted live.

Fallback / deed detail: `traviscad.org/propertysearch/` (ProdigyCAD
vendor, same family as Bexar's TrueAutomation). The ArcGIS `hyperlink`
field points to `stage.travis.prodigycad.com/property-detail/{PROP_ID}`
— confirm that "stage." host is actually production-stable (not a true
staging environment) before scraping it.

### Code enforcement: Austin Code Department (Socrata)
Confirmed working, unauthenticated: `https://data.austintexas.gov/resource/6wtj-zbtb.json`
("Austin Code Complaint Cases"). Fields: `case_id`, `priority` (1-5),
`status`, `address`/`house_number`/`street_name`/`city`/`zip_code`,
`opened_date`, `closed_date`, `case_type`, `description`, `inspector`,
`parcelid`, `location`, `repeatoffenderrelated`, `servicerequestnumber`.
Supports SoQL (`$where`, `$limit`, `$q`). Get a free Socrata App Token
before production use — anonymous calls are rate-limited.

## Dashboard — planned improvements over Bexar/Nueces (this repo is the
test bed; backport whatever works)
Based on a competitor survey (PropStream/REsimpli/BatchLeads/DealMachine/
iSpeedToLead), prioritized:
1. List-stacking: flag leads on 2+ distress sources as a first-class filter
2. Debt-stack breakdown as separate fields, not one `loan_amount`:
   mortgage balance est., tax delinquent $ + years owed, judgment $,
   HOA/mechanics/tax lien $ as discrete line items
3. Code violation severity + open/resolved history (Austin's `priority`
   and `repeatoffenderrelated` fields map directly to this)
4. Equity position ($ and %) front-and-center
5. ARV/comps panel (nearby recent sales, editable repair estimate)
6. Explainable motivation score (show which signals drove it)
7. Saved filter combos ("Smart Lists") auto-rerun as new data lands
8. Bulk actions on filtered selections
9. Deed/ownership timeline (last sale date/price, absentee flag) — cheap
   to add now, TCAD's `py_address`/`deed_date` already give this

## GitHub Secrets Required
None yet identified — tccsearch.org and the ArcGIS/Socrata endpoints are
all public/unauthenticated as tested.
