#!/usr/bin/env python3
"""Extract terminal slot availability and upload CSVs + screenshots to Google Drive.

Supported terminals:
  - TDF type   : Terminal de France (Le Havre) — JS-rendered, regex-parsed text
  - GCT type   : GCT Deltaport / Vanterm (Vancouver) — server-rendered HTML table
  - TruckGate  : Hamburg TruckGate — React SPA, one day at a time

Each terminal is processed independently. A failure in one does not abort others.
The run exits 1 only if at least one terminal had a genuine TECHNICAL failure
(page didn't load, expected DOM structure missing entirely, upload error, etc).

IMPORTANT — zero slots is a result, not a failure:
The whole point of this scrape is to catch the moments a terminal has NO open
appointment slots, so BuyCo can crossbill the terminal for missing appointments.
A terminal that is fully booked will legitimately show 0 (or very few) rows.
That is exactly the data we're here to capture, so it must always be written
to Drive (CSV + screenshot) and reported in the summary — never treated as a
scraper failure. Only raise/fail when the page itself is broken (wrong
structure, missing elements we rely on to parse at all), not when it's empty.

When a GCT terminal DOES hit a genuine technical failure (no table found /
malformed table), the screenshot and a page HTML + HTTP-status snapshot are
still uploaded to Drive, named with a "_FAILED_" marker, so the failure can
be diagnosed by looking in Drive rather than needing to re-run the job.
"""
import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Terminal configuration — add new terminals here only
# ---------------------------------------------------------------------------
TERMINALS = [
    {
        "slug": "tdf",
        "label": "Terminal de France (Le Havre)",
        "type": "tdf",
        "file_prefix": "GMP",
        "urls": [
            "https://www.rdvgmp.fr/static/calendar_tdf.html",
            "https://www.rdvgmp.fr/static/calendar_tdf_next_week.html",
        ],
        # Informational only — see "low_row_watermark" note below. A fully
        # booked week can legitimately show far fewer than this.
        "low_row_watermark": 10,
    },
    {
        "slug": "gct_deltaport",
        "label": "GCT Deltaport (Vancouver)",
        "type": "gct",
        "file_prefix": "GCT_Deltaport",
        "url": "https://webservices.globalterminals.com/tsiWebServiceClient/ReservationAvailabilityStatus.jsp?terminal=DELTAPORT",
        "low_row_watermark": 5,
    },
    {
        "slug": "gct_vanterm",
        "label": "GCT Vanterm (Vancouver)",
        "type": "gct",
        "file_prefix": "GCT_Vanterm",
        "url": "https://webservices.globalterminals.com/tsiWebServiceClient/ReservationAvailabilityStatus.jsp?terminal=VANTERM",
        "low_row_watermark": 5,
    },
    {
        "slug": "truckgate_hamburg",
        "label": "Hamburg TruckGate",
        "type": "truckgate",
        "file_prefix": "Hamburg_TruckGate",
        "url": "https://slot.truckgate.de/slots/",
        # Only extract these sub-terminals; empty list = all
        "terminals_filter": [
            "Eurogate CTH", "Eurogate EKOM", "EUROGATE CTB", "EUROGATE CTW",
            "HHLA CTA", "HHLA CTB", "HHLA CTT",
        ],
        # TruckGate legitimately has days with very few (or zero) open
        # slots, so no watermark is set for it.
        "low_row_watermark": 0,
    },
]

# "low_row_watermark" is NOT a pass/fail gate. It never aborts the run —
# it only controls whether a terminal gets flagged with a ⚠ in the log/summary
# so a human can glance at "did this terminal look unusually empty today"
# without it ever blocking the CSV/screenshot from being uploaded. Zero rows
# is the single most important result this scraper can produce (it's the
# crossbilling signal), so it must never be suppressed or turned into a
# failure.

# Known GCT column order (server-rendered JSP, stable format)
GCT_HEADERS = [
    "Date", "Period",
    "Empty In", "Empty Out", "Full In", "Full Out", "Reefer In",
    "AE", "AW", "BE", "BW", "CE", "CW", "DE", "DW",
    "EW", "FW", "IT", "IW", "IZ", "JT", "JZ",
    "KT", "KZ", "LE", "LZ", "MW", "NW",
]

# TruckGate background-color → human-readable status
TRUCKGATE_STATUS = {
    "lightgrey":         "Closed",
    "lightgreen":        "Available",
    "gold":              "Near Full",
    "rgb(255, 110, 84)": "Full",
    "lightblue":         "Other System",
}

# TDF regexes
DAY_HEADER = re.compile(r"Détails des disponibilités\s*du\s*(\d{2}/\d{2}/\d{4})")
HOUR_LABEL = re.compile(r"\b(\d{1,2}):00\s*-\s*(\d{1,2}):00\b")
CAPACITY   = re.compile(
    r"Capacité\s*/\s*Restants\s*/\s*Attente\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*%"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def open_page(url: str, browser, extra_wait_ms: int = 0):
    """Navigate to a URL; return (page, png_bytes, response). Caller must
    close page. `response` is the main-frame navigation Response (has
    .status / .status_text) or None if Playwright didn't get one."""
    page = browser.new_page()
    response = page.goto(url, wait_until="networkidle", timeout=60_000)
    if extra_wait_ms:
        page.wait_for_timeout(extra_wait_ms)
    png_bytes = page.screenshot(full_page=True)
    return page, png_bytes, response


def upload_file(filename: str, data: bytes, mimetype: str, svc, folder_id: str,
                verify: bool) -> str:
    """Upload or update a file in Drive; optionally verify byte-identical round-trip."""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
    existing = (
        svc.files()
        .list(
            q=f"name = '{filename}' and '{folder_id}' in parents and trashed = false",
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    if existing:
        file_id = existing[0]["id"]
        svc.files().update(
            fileId=file_id, media_body=media, supportsAllDrives=True
        ).execute()
        action = "updated"
    else:
        meta = {"name": filename, "parents": [folder_id]}
        file_id = (
            svc.files()
            .create(body=meta, media_body=media, fields="id", supportsAllDrives=True)
            .execute()["id"]
        )
        action = "created"
    if verify:
        stored = svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        if stored != data:
            raise RuntimeError(f"Verification failed for {filename} — bytes differ in Drive.")
    print(f"    {action}: {filename} ({len(data):,} bytes)")
    return file_id


# ---------------------------------------------------------------------------
# TDF parser (Le Havre)
# ---------------------------------------------------------------------------

def _parse_tdf_section(section: str):
    """Yield (start_hour, capacity, remaining, waitlist_pct) for a TDF day section."""
    events = []
    for m in HOUR_LABEL.finditer(section):
        events.append((m.start(), "hour", (int(m.group(1)), int(m.group(2)))))
    for m in CAPACITY.finditer(section):
        events.append(
            (m.start(), "cap", (int(m.group(1)), int(m.group(2)), int(m.group(3))))
        )
    events.sort(key=lambda e: e[0])
    current_hour = None
    for _, kind, val in events:
        if kind == "hour":
            current_hour = val
        else:
            if current_hour is not None:
                start, end = current_hour
                if end == start + 1 and 0 <= start <= 23:
                    yield (start, *val)
            current_hour = None


def process_tdf(terminal: dict, browser, today_str: str):
    """Returns (headers, rows, screenshots_list)."""
    headers = ["Date", "Day", "Time Slot", "Capacity", "Remaining", "Fill %", "Waitlist %"]
    rows = []
    screenshots = []

    for url in terminal["urls"]:
        print(f"    Fetching {url}")
        page, png_bytes, _resp = open_page(url, browser)
        try:
            html = page.content()
        finally:
            page.close()

        week = "nextweek" if "next_week" in url else "currentweek"
        prefix = terminal["file_prefix"]
        screenshots.append((
            f"{prefix}_terminal_screenshot_{today_str}_{week}.png",
            png_bytes,
        ))

        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)

        parts = DAY_HEADER.split(text)
        for i in range(1, len(parts) - 1, 2):
            date_str = parts[i]
            body = parts[i + 1]
            day_name = datetime.strptime(date_str, "%d/%m/%Y").strftime("%A")
            for start, cap, rem, wait in _parse_tdf_section(body):
                fill = round(100 * (cap - rem) / cap) if cap else 0
                rows.append([date_str, day_name, f"{start}:00 - {start + 1}:00",
                              cap, rem, f"{fill}%", f"{wait}%"])
            slot_count = sum(1 for r in rows if r[0] == date_str)
            print(f"    {date_str} ({day_name}): {slot_count} slots")

    return headers, rows, screenshots


# ---------------------------------------------------------------------------
# GCT parser (Vancouver — server-rendered HTML table)
# ---------------------------------------------------------------------------

def _upload_gct_failure_diagnostics(
    svc, folder_id: str, prefix: str, today_str: str, url: str,
    status, status_text, title: str, html: str, png_bytes: bytes,
):
    """Best-effort: when GCT parsing fails, upload the screenshot plus an
    HTTP-status/title/HTML snapshot to Drive so the failure can be
    diagnosed without re-running the job or SSHing into CI. Uses a
    "_FAILED_" filename so it never collides with (or gets mistaken for) a
    real successful-day screenshot. Never lets a problem uploading
    diagnostics mask the real parsing failure the caller is about to raise.
    """
    try:
        upload_file(
            f"{prefix}_terminal_screenshot_FAILED_{today_str}.png",
            png_bytes, "image/png", svc, folder_id, verify=False,
        )

        html_cap = 200_000
        html_out = html if len(html) <= html_cap else (
            html[:html_cap] + f"\n\n... [truncated, {len(html):,} chars total]"
        )
        debug_text = (
            f"URL: {url}\n"
            f"Timestamp (Europe/Paris): "
            f"{datetime.now(ZoneInfo('Europe/Paris')).isoformat()}\n"
            f"HTTP status: {status} {status_text or ''}\n"
            f"Page title: {title!r}\n"
            f"HTML length: {len(html):,} chars\n"
            f"{'-' * 70}\n"
            f"{html_out}\n"
        )
        upload_file(
            f"{prefix}_terminal_debug_FAILED_{today_str}.html",
            debug_text.encode("utf-8"), "text/html", svc, folder_id, verify=False,
        )
        print("    (uploaded failure screenshot + HTML/status snapshot for diagnosis)")
    except Exception as diag_exc:
        print(f"    (could not upload failure diagnostics: {diag_exc})")


# When GCT has nothing to reserve, it doesn't render the table at all — it
# renders a plain status banner: "result: reservationInfoXmlList: Empty
# <code>[: <detail>]". Two very different codes have been observed under
# this banner:
#   - "Empty 0"           -> genuinely no reservation data for this window
#                            (terminal closed / fully booked). A real
#                            zero-slots result — confirmed against
#                            https://globalterminals.com/terminal-operations/gate-schedule/
#   - "Empty 6: Previous request from the user <x> is still active. Please
#     wait and repeat request in a few seconds." -> GCT's own backend is
#     telling us to retry; NOT a zero-availability result and NOT a broken
#     page. Any other non-zero code is treated the same way: transient, so
#     we retry rather than either failing immediately or misreporting it
#     as "no slots".
GCT_STATUS_RE = re.compile(
    r"reservationInfoXmlList\s*:\s*Empty\s*(\d+)\s*:?\s*([^\r\n]*)", re.I
)
GCT_MAX_ATTEMPTS = 3
GCT_RETRY_WAIT_S = 8  # GCT's own busy message asks us to wait "a few seconds"


def process_gct(terminal: dict, browser, today_str: str, svc, folder_id: str):
    """Returns (headers, rows, screenshots_list)."""
    url = terminal["url"]
    prefix = terminal["file_prefix"]
    screenshots = None
    diag = None       # (status, status_text, title, html, png_bytes) of the
                       # most recent attempt, kept in case we ultimately fail
    code = detail = None

    for attempt in range(1, GCT_MAX_ATTEMPTS + 1):
        suffix = f" (attempt {attempt}/{GCT_MAX_ATTEMPTS})" if attempt > 1 else ""
        print(f"    Fetching {url}{suffix}")
        page, png_bytes, resp = open_page(url, browser)
        try:
            page_data = page.evaluate("""
            () => {
                const table = document.querySelector('table');
                const rows = table
                    ? Array.from(table.querySelectorAll('tr')).map(tr =>
                        Array.from(tr.querySelectorAll('th, td')).map(cell => {
                            const img = cell.querySelector('img');
                            return img
                                ? (img.getAttribute('title') || img.getAttribute('alt') || '')
                                : cell.innerText.trim();
                        })
                      )
                    : [];
                return {
                    hasTable: !!table,
                    rows: rows,
                    bodyText: (document.body ? document.body.innerText : ''),
                };
            }
            """)
            has_table = page_data["hasTable"]
            table_data = page_data["rows"]
            body_text = page_data["bodyText"]
            # Gathered unconditionally (cheap) so they're ready to attach if
            # we ultimately give up and need to report a real failure.
            status = resp.status if resp else None
            status_text = resp.status_text if resp else None
            title = page.title()
            html = page.content()
        finally:
            page.close()

        screenshots = [(
            f"{prefix}_terminal_screenshot_{today_str}.png",
            png_bytes,
        )]
        diag = (status, status_text, title, html, png_bytes)

        if has_table:
            if len(table_data) >= 2:
                # Rows 0–1 are the double-labeled headers; data starts at
                # row 2. Nothing after the headers is also a legitimate
                # zero-slots result (fully booked) — flows through as 0
                # rows rather than raising.
                data_rows = [
                    row for row in table_data[2:] if any(cell.strip() for cell in row)
                ]
                n = len(GCT_HEADERS)
                rows = [row[:n] + [""] * max(0, n - len(row)) for row in data_rows]
                print(f"    {len(rows)} time slots parsed")
                return GCT_HEADERS, rows, screenshots

            # A table is there, but even the two header rows are missing —
            # this isn't the reservation table we know how to read, and
            # we've no evidence this is transient, so fail immediately
            # rather than burn retry budget on it.
            _upload_gct_failure_diagnostics(svc, folder_id, prefix, today_str, url, *diag)
            raise RuntimeError(
                f"GCT table found but malformed ({len(table_data)} row(s), "
                "expected at least the 2 header rows) — page failed to "
                "load or its structure has changed (this is a parsing "
                "problem, not a zero-availability result)"
            )

        m = GCT_STATUS_RE.search(body_text)
        if not m:
            # No table AND no recognized status banner either — the page
            # rendered something we don't recognize at all (down, blocked,
            # redesigned). Real technical failure.
            _upload_gct_failure_diagnostics(svc, folder_id, prefix, today_str, url, *diag)
            raise RuntimeError(
                "GCT table not found in page, and no recognized "
                "'reservationInfoXmlList' status message either — page "
                "failed to load or its structure has changed (this is a "
                "parsing problem, not a zero-availability result)"
            )

        code, detail = m.group(1), m.group(2).strip()
        if code == "0":
            print("    0 slots — GCT reports no reservation data available "
                  "('reservationInfoXmlList: Empty 0' — terminal closed or "
                  "fully booked for this window)")
            return GCT_HEADERS, [], screenshots

        # Any other code is GCT telling us something transient happened on
        # its side (observed: code 6, "Previous request from the user ...
        # is still active. Please wait and repeat request in a few
        # seconds."). Retry rather than treating it as broken or empty.
        print(f"    GCT busy (code {code}): {detail or '(no detail)'}")
        if attempt < GCT_MAX_ATTEMPTS:
            print(f"    Waiting {GCT_RETRY_WAIT_S}s before retrying "
                  "(GCT asked us to)...")
            time.sleep(GCT_RETRY_WAIT_S)

    # GCT kept reporting itself busy on every attempt.
    _upload_gct_failure_diagnostics(svc, folder_id, prefix, today_str, url, *diag)
    raise RuntimeError(
        f"GCT backend reported itself busy on all {GCT_MAX_ATTEMPTS} "
        f"attempts (reservationInfoXmlList: Empty {code}: {detail}) — this "
        "is GCT's own 'still processing, please retry' response, not a "
        "zero-availability result"
    )


# ---------------------------------------------------------------------------
# TruckGate parser (Hamburg — React SPA, single day only)
# ---------------------------------------------------------------------------

def process_truckgate(terminal: dict, browser, today_str: str):
    """Returns (headers, rows, screenshots_list).

    Note: TruckGate only exposes the current day — no multi-day navigation.
    """
    url = terminal["url"]
    tf  = terminal.get("terminals_filter", [])
    print(f"    Fetching {url}")

    page, png_bytes, _resp = open_page(url, browser, extra_wait_ms=4000)
    try:
        date_text  = page.inner_text("span.titlebar span")
        date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_text)
        page_date  = date_match.group(1) if date_match else today_str

        slot_data = page.evaluate(
            """
            (terminalsFilter) => {
                const blocks = Array.from(
                    document.querySelectorAll('div.SlotGrid-TermBlock')
                );
                const rows = [];
                for (const block of blocks) {
                    const nameEl = block.querySelector('div.SlotGrid-TermHeader1 a');
                    if (!nameEl) continue;
                    const termName = nameEl.innerText.trim();
                    if (terminalsFilter.length > 0 &&
                        !terminalsFilter.includes(termName)) continue;

                    const cells = Array.from(
                        block.querySelectorAll('div.SlotGrid-Cell')
                    );
                    for (const cell of cells) {
                        const style   = cell.getAttribute('style') || '';
                        const bgMatch = style.match(/background-color:\\s*([^;]+)/);
                        const bgColor = bgMatch ? bgMatch[1].trim() : '';

                        const hourEl = cell.querySelector(
                            'div[style*="font-size: 75%"]'
                        );
                        const hour = hourEl ? hourEl.innerText.trim() : '';

                        const pb     = cell.querySelector('[role="progressbar"]');
                        const fillPct = pb ? pb.getAttribute('aria-valuenow') : '';

                        rows.push([termName, hour, bgColor, fillPct]);
                    }
                }
                return rows;
            }
            """,
            tf,
        )
    finally:
        page.close()

    prefix = terminal["file_prefix"]
    screenshots = [(
        f"{prefix}_terminal_screenshot_{today_str}.png",
        png_bytes,
    )]

    headers = ["Date", "Terminal", "Hour", "Status", "Fill %"]
    rows = []
    for term_name, hour, bg_color, fill_pct in slot_data:
        status = TRUCKGATE_STATUS.get(bg_color, "Unknown")
        if status == "Closed":
            continue  # skip outside-hours slots
        fill = f"{fill_pct}%" if fill_pct else ""
        rows.append([page_date, term_name, f"{hour}:00", status, fill])

    print(f"    Date: {page_date} — {len(rows)} open slots across "
          f"{len(set(r[1] for r in rows))} terminals")
    return headers, rows, screenshots


# ---------------------------------------------------------------------------
# Terminal dispatcher
# ---------------------------------------------------------------------------

def process_terminal(terminal: dict, browser, svc, folder_id: str, today_str: str) -> dict:
    """Process one terminal end-to-end. Raises RuntimeError on a genuine
    technical failure only. Returns {"rows": int, "zero_availability": bool}
    on success — a 0-row result is a success, not an error."""
    slug  = terminal["slug"]
    label = terminal["label"]
    ttype = terminal.get("type", "tdf")
    print(f"\n── {label} ({slug}) ──")

    if ttype == "tdf":
        headers, rows, screenshots = process_tdf(terminal, browser, today_str)
    elif ttype == "gct":
        headers, rows, screenshots = process_gct(
            terminal, browser, today_str, svc, folder_id
        )
    elif ttype == "truckgate":
        headers, rows, screenshots = process_truckgate(terminal, browser, today_str)
    else:
        raise RuntimeError(f"Unknown terminal type: {ttype!r}")

    # Zero (or unusually low) rows is NEVER a failure here — it's the
    # crossbilling signal this whole scrape exists to catch. We still flag
    # it in the logs/summary so it's easy to spot, but the CSV and
    # screenshot are always uploaded regardless of the row count.
    watermark = terminal.get("low_row_watermark", 0)
    zero_availability = len(rows) == 0
    if zero_availability:
        print(f"    ⚠ 0 slots parsed — {label} appears fully booked "
              "(no open appointments). Recording as zero-availability, "
              "not a failure.")
    elif len(rows) < watermark:
        print(f"    ⚠ Only {len(rows)} rows parsed (below the usual "
              f"~{watermark}) — low availability today; uploading as-is.")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    csv_data = buf.getvalue().encode("utf-8")

    prefix = terminal["file_prefix"]
    upload_file(
        f"{prefix}_terminal_slots_{today_str}.csv",
        csv_data, "text/csv", svc, folder_id, verify=True,
    )
    for name, png_bytes in screenshots:
        upload_file(name, png_bytes, "image/png", svc, folder_id, verify=False)

    print(f"  ✓ {slug}: {len(rows)} rows + {len(screenshots)} screenshot(s) uploaded.")
    return {"rows": len(rows), "zero_availability": zero_availability}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    today_str = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()

    folder_id = os.environ["GDRIVE_FOLDER_ID"]
    info = json.loads(os.environ["GDRIVE_CREDENTIALS_JSON"])
    if isinstance(info, str):
        info = json.loads(info)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    svc = build("drive", "v3", credentials=creds)

    failures = []
    zero_availability = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for terminal in TERMINALS:
                try:
                    result = process_terminal(terminal, browser, svc, folder_id, today_str)
                    if result["zero_availability"]:
                        zero_availability.append(terminal["slug"])
                except Exception as exc:
                    msg = f"{terminal['slug']}: {exc}"
                    print(f"\n  ✗ FAILED — {msg}")
                    failures.append(msg)
        finally:
            browser.close()

    print(f"\n── Summary ──")
    print(f"  Terminals : {len(TERMINALS)}")
    print(f"  Succeeded : {len(TERMINALS) - len(failures)}")
    print(f"  Failed    : {len(failures)}")

    if zero_availability:
        print(f"\nZero availability today (possible crossbill candidates):")
        for slug in zero_availability:
            print(f"  • {slug}")

    if failures:
        print("\nFailures (technical — page/parsing broke, not a slots result):")
        for f in failures:
            print(f"  • {f}")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
