#!/usr/bin/env python3
"""Totals and attack-type shares of the older OAIC reports, read from the PDFs and promoted to the published dataset.

The HTML and PDF scraper took the first number a loose pattern matched. Owner review 2026-11-06 found the result:
2019 H2's total was 106 (November's count; the report says 537), 2021 H2's 436 (the previous period's; it says 464),
2022 H1's 250 and 2022 H2's 350 (the malicious-attack counts; they say 396 and 497). Its attack-type fields mixed
percentages (2021 H2 ransomware 23 is 23%: 40 of 173 notifications) with counts and misreadings (2022 H2 malware 109;
the report says 4%), and brute force and compromised credentials were never read. Its individuals_affected_average was
the first "an average of N" in the text, which is a human-error sub-category's (2021 H1's 523,998 is "unintended release
or publication"); these reports state no overall average. Its top_sectors held at most two sectors and it read no
distribution, though every report has both.

This module reads one report per period with a pattern written for that report's own wording; a pattern that does not
match is an error, never a blank. Shares are read from the report's "Cyber incident breakdown" chart, an image in
several reports, by the vision model once per page, and so is the "Number of individuals affected" chart, whose counts
must sum to the period's total, and the "Notifications received by month" chart, whose six months must sum to it too.
The sector table is read from the text. Each reading is committed in ``report_chart_readings.json``, keyed
on the PDF's hash and the prompt version, so a rerun asks nothing and anyone can compare it with the page.

    python scripts/oaic/oaic_report_figures.py --dry-run   # what would change
    python scripts/oaic/oaic_report_figures.py             # read and promote (needs the PDFs in oaic_pdfs/)

Records go through the same consistency checks as every other source (``oaic_dataset.promote_records``). Run from the
project root. Exit codes: 0 done; 1 a record was rejected or a figure could not be read.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

READINGS_PATH = Path(__file__).resolve().parent / "report_chart_readings.json"
PROMPT_VERSION = "cyber-breakdown-v1"
DISTRIBUTION_PROMPT_VERSION = "individuals-distribution-v1"
MONTHLY_PROMPT_VERSION = "monthly-notifications-v1"
#: The bands the published dataset uses (and the dashboard counts); a band a report does not show holds no breaches.
DISTRIBUTION_BANDS = ("1", "2-10", "11-100", "101-1,000", "1,001-5,000", "5,001-10,000", "10,001-25,000",
                      "25,001-50,000", "50,001-100,000", "100,001-250,000", "250,001-500,000", "500,001-1,000,000",
                      "1,000,001-10,000,000", "10,000,001+", "Unknown")
#: A distribution may miss a total by this share: some reports count a few notifications differently.
DISTRIBUTION_SUM_TOLERANCE = 0.02
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SHARE_FIELDS = ("phishing", "compromised_credentials", "ransomware", "hacking", "brute_force", "malware")
METHOD_FIELDS = SHARE_FIELDS
#: The shares of a report are rounded: they sum to about 100, not exactly.
SHARE_SUM_RANGE = (96, 104)

PROMPT = """This is one page of an Australian privacy regulator's data breach report. It has a chart or table headed
"Cyber incident breakdown" (or a table of the same categories with counts and percentages). Read the percentage shown
for each category and return JSON with exactly these keys: phishing, compromised_credentials, ransomware, hacking,
brute_force, malware, other. "Compromised or stolen credentials (method unknown)" is compromised_credentials;
"Phishing (compromised credentials)" is phishing; "Brute-force attack (compromised credentials)" is brute_force.
Values are the integers printed on the page; null for a category the page does not show. Do not calculate anything."""

DISTRIBUTION_PROMPT = """This is one page of an Australian privacy regulator's data breach report. It has a chart headed
"Number of individuals affected by breaches" (bars or a table: the number of breaches in each band of individuals
affected). Read the number of breaches printed for each band and return JSON whose keys are exactly these band labels:
""" + ", ".join(f'"{b}"' for b in DISTRIBUTION_BANDS) + """. "10,000,001 or more" is "10,000,001+". Values are the integers
printed on the page; null for a band the page does not show. Use the chart for the individuals affected worldwide, not
a table of Australians only. Do not calculate anything."""

MONTHLY_PROMPT = """This is one page of an Australian privacy regulator's data breach report. It has a chart headed
"Chart 1" of the data breach notifications received each month. Its last month is {last_month}, and each earlier point
is the month before. Read the number printed for each month shown and return JSON whose keys are the months in the form
YYYY-MM and whose values are the integers printed on the page. Do not calculate anything."""


@dataclass(frozen=True)
class Report:
    period: str
    pdf: str
    chart_page: int
    sector_page: int
    distribution_page: int
    monthly_page: int
    total: str
    malicious: str
    cyber: str


_MALICIOUS_ACCOUNTING = r"accounting\s+for\s+(\d{3})\s+(?:breaches|notifications)"
REPORTS = (
    Report("2019 H2", "notifiable-data-breaches-report-july-december-2019.pdf", 14, 6, 8, 5,
           r"(\d{3})\s+breaches\s+were\s+notified\s+under\s+the\s+scheme", _MALICIOUS_ACCOUNTING,
           r"received\s+(\d{3})\s+notifications\s+under\s+this\s+category"),
    Report("2020 H1", "notifiable-data-breaches-report-jan-jun-2020.pdf", 15, 6, 9, 5,
           r"The\s+(\d{3})\s+notifications\s+received\s+during\s+this\s+reporting", _MALICIOUS_ACCOUNTING,
           r"received\s+(\d{3})\s+notifications\s+under\s+this\s+category"),
    Report("2020 H2", "notifiable-data-breaches-report-july-dec-2020.pdf", 19, 7, 9, 5,
           r"received\s+(\d{3})\s+notifications\s+this\s+reporting\s+period", _MALICIOUS_ACCOUNTING,
           r"received\s+(\d{3})\s+notifications\s+of\s+cyber\s+incidents"),
    Report("2021 H1", "oaic-notifiable-data-breaches-report-jan-june-2021.pdf", 18, 8, 10, 7,
           r"(\d{3})\s+breaches\s+were\s+notified\s+under\s+the\s+scheme", _MALICIOUS_ACCOUNTING,
           r"of\s+all\s+data\s+breaches\s+\((\d{3})\s+notifications\)"),
    Report("2021 H2", "Final-Notifiable-Data-Breaches-Report-Jul-Dec-2021.pdf", 17, 20, 8, 7,
           r"(\d{3})\s+breaches\s+were\s+notified\s+under\s+the\s+scheme", _MALICIOUS_ACCOUNTING,
           r"of\s+all\s+breaches\s+\((\d{3})\s+notifications\)"),
    Report("2022 H1", "OAIC-Notifiable-Data-Breaches-Report-Jan-Jun-2022.pdf", 19, 23, 9, 7,
           r"received\s+(\d{3})\s+notifications\s+this\s+reporting\s+period", _MALICIOUS_ACCOUNTING,
           r"breaches\s+\((\d{3})\s+notifications\)\s+.\s+resulted\s+from\s+cyber"),
    Report("2022 H2", "OAIC-Notifiable-data-breaches-report-July-December-2022.pdf", 22, 28, 10, 7,
           r"received\s+(\d{3})\s+notifications\s+this\s+reporting\s+period",
           r"accounted\s+for\s+(\d{3})\s+notifications",
           r"were\s+(\d{3})\s+breaches\s+resulting\s+from\s+cyber"),
)


class FigureNotFound(RuntimeError):
    """A report does not read as this module expects: nothing is guessed."""


def first_number(pattern: str, text: str, what: str, period: str) -> int:
    found = re.search(pattern, text)
    if found is None:
        raise FigureNotFound(f"{period}: {what} not found by {pattern!r}")
    return int(found.group(1))


def report_figures(report: Report, text: str) -> Dict[str, int]:
    """The total, malicious-attack and cyber-incident counts stated in a report's text."""
    return {"total_notifications": first_number(report.total, text, "total", report.period),
            "malicious_attacks": first_number(report.malicious, text, "malicious attacks", report.period),
            "cyber_incidents_total": first_number(report.cyber, text, "cyber incidents", report.period)}


def check_shares(period: str, shares: Dict[str, Any]) -> Dict[str, int]:
    """A reading's shares, or an error when a category is missing or they do not sum to about 100."""
    missing = [f for f in SHARE_FIELDS if not isinstance(shares.get(f), int)]
    if missing:
        raise FigureNotFound(f"{period}: chart reading lacks {missing}: {shares}")
    total = sum(v for v in shares.values() if isinstance(v, int))
    if not SHARE_SUM_RANGE[0] <= total <= SHARE_SUM_RANGE[1]:
        raise FigureNotFound(f"{period}: chart reading sums to {total}: {shares}")
    return {k: v for k, v in shares.items() if isinstance(v, int)}


def read_sectors(period: str, page_text: str) -> List[Dict[str, Any]]:
    """The report's table of top sectors, from its first row (always health) to the last row of name and count."""
    rows: List[Dict[str, Any]] = []
    for line in page_text.split("\n"):
        if re.search(r"(?i)report\s+page", line):
            break
        found = re.match(r"^([A-Z][A-Za-z ,&()./-]*?)\d?\s+(\d{1,3})(?:\s+\d+%)?$", line.strip())
        if found is None:
            continue
        name, count = found.group(1).strip(), int(found.group(2))
        if not rows and not name.startswith("Health"):
            continue
        if name == "Total":
            break
        rows.append({"sector": name, "notifications": count})
    if not 5 <= len(rows) <= 7:
        raise FigureNotFound(f"{period}: sector table read as {rows}")
    return rows


def check_distribution(period: str, reading: Dict[str, Any], total: int) -> List[Dict[str, Any]]:
    """The bands a reading gives, in order, or an error when their counts do not sum to the period's total."""
    unknown = sorted(set(reading) - set(DISTRIBUTION_BANDS))
    if unknown:
        raise FigureNotFound(f"{period}: distribution reading has unknown bands {unknown}")
    rows = [{"range": band, "count": reading[band]} for band in DISTRIBUTION_BANDS
            if isinstance(reading.get(band), int)]
    found = sum(row["count"] for row in rows)
    if abs(found - total) > total * DISTRIBUTION_SUM_TOLERANCE:
        raise FigureNotFound(f"{period}: distribution sums to {found}, the period's total is {total}: {reading}")
    return rows


def period_months(period: str) -> List[str]:
    """The six YYYY-MM months of a period such as "2021 H2"."""
    year, half = period.split()
    first = 1 if half == "H1" else 7
    return [f"{year}-{month:02d}" for month in range(first, first + 6)]


def check_monthly(period: str, reading: Dict[str, Any], total: int) -> List[Dict[str, Any]]:
    """The period's six months from a chart reading, or an error when one is missing or they do not sum to the total."""
    months = period_months(period)
    missing = [m for m in months if not isinstance(reading.get(m), int)]
    if missing:
        raise FigureNotFound(f"{period}: monthly reading lacks {missing}: {reading}")
    found = sum(reading[m] for m in months)
    if found != total:
        raise FigureNotFound(f"{period}: months sum to {found}, the period's total is {total}: "
                             f"{[reading[m] for m in months]}")
    return [{"month": MONTH_NAMES[int(m[5:]) - 1], "count": reading[m]} for m in months]


def apply_figures(record: Dict[str, Any], figures: Dict[str, int], shares: Dict[str, int],
                  sectors: Optional[List[Dict[str, Any]]] = None,
                  distribution: Optional[List[Dict[str, Any]]] = None,
                  monthly: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The record with the report's figures: shares in percent, and the mixed method fields empty.

    The stored average is removed: it is a sub-category's, and these reports state no overall one.
    """
    out = dict(record)
    out.update(figures)
    if sectors is not None:
        out["top_sectors"] = sectors
    if distribution is not None:
        out["individuals_affected_distribution"] = distribution
        out["individuals_affected_average"] = None
    if monthly is not None:
        out["monthly_notifications"] = monthly
    out["cyber_incidents_percentage"] = round(100 * figures["cyber_incidents_total"] / figures["total_notifications"], 1)
    out["cyber_incident_breakdown_pct"] = shares
    for field in METHOD_FIELDS:
        out[field] = None
    return out


#: Averages stored for later periods that are not the period's overall average. The first two are the "rogue employee"
#: row of the report's malicious-attack table; the last two, with their medians, are its cyber-incident row.
MISATTRIBUTED_AVERAGES = ("2023 H1", "2023 H2", "2024 H1", "2024 H2")


def without_misattributed_average(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record with no average or median, so the dashboard estimates them from the distribution like every other."""
    return {**record, "individuals_affected_average": None, "individuals_affected_median": None}


def _read_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return " ".join((page.extract_text() or "") for page in pdf.pages[:32])


def _page_text(path: Path, page_number: int) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return pdf.pages[page_number - 1].extract_text() or ""


def _ask_vision(path: Path, page_number: int, prompt: str = PROMPT, resolution: int = 150,
                max_tokens: int = 500) -> Dict[str, Any]:
    import pdfplumber
    from openai import OpenAI

    from cyber_data_collector.utils.llm_models import chat_params, openai_model

    with pdfplumber.open(path) as pdf:
        image = pdf.pages[page_number - 1].to_image(resolution=resolution).original
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    response = OpenAI().chat.completions.create(
        **chat_params(openai_model(), max_tokens=max_tokens),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]}],
        response_format={"type": "json_object"})
    return json.loads(response.choices[0].message.content)


def chart_shares(report: Report, path: Path, readings: Dict[str, Any], dry_run: bool) -> Optional[Dict[str, int]]:
    """A report's shares: the committed reading of this PDF and prompt, else one vision call (stored)."""
    from cyber_data_collector.utils.llm_models import openai_model

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    stored = readings.get(report.period)
    if stored and stored.get("pdf_sha256") == digest and stored.get("prompt_version") == PROMPT_VERSION:
        return check_shares(report.period, stored["shares"])
    if dry_run:
        return None
    shares = _ask_vision(path, report.chart_page)
    checked = check_shares(report.period, shares)
    readings[report.period] = {"pdf": report.pdf, "pdf_sha256": digest, "page": report.chart_page,
                               "prompt_version": PROMPT_VERSION, "model": openai_model(), "shares": checked}
    READINGS_PATH.write_text(json.dumps(readings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return checked


def chart_distribution(report: Report, path: Path, readings: Dict[str, Any], total: int,
                       dry_run: bool) -> Optional[List[Dict[str, Any]]]:
    """A report's individuals-affected bands: the committed reading of this PDF and prompt, else one vision call."""
    from cyber_data_collector.utils.llm_models import openai_model

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    entry = readings.setdefault(report.period, {})
    stored = entry.get("distribution")
    if stored and stored.get("pdf_sha256") == digest and stored.get("prompt_version") == DISTRIBUTION_PROMPT_VERSION:
        return check_distribution(report.period, stored["bands"], total)
    if dry_run:
        return None
    reading = _ask_vision(path, report.distribution_page, DISTRIBUTION_PROMPT)
    rows = check_distribution(report.period, reading, total)
    entry["distribution"] = {"pdf_sha256": digest, "page": report.distribution_page,
                             "prompt_version": DISTRIBUTION_PROMPT_VERSION, "model": openai_model(), "bands": reading}
    READINGS_PATH.write_text(json.dumps(readings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rows


def chart_monthly(report: Report, path: Path, readings: Dict[str, Any], total: int,
                  dry_run: bool) -> Optional[List[Dict[str, Any]]]:
    """A report's six months: the committed reading of this PDF and prompt, else one vision call.

    The reading is stored before it is checked: a month that fails the check is looked at, not paid for twice.
    """
    from cyber_data_collector.utils.llm_models import openai_model

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    entry = readings.setdefault(report.period, {})
    stored = entry.get("monthly")
    if not (stored and stored.get("pdf_sha256") == digest and stored.get("prompt_version") == MONTHLY_PROMPT_VERSION):
        if dry_run:
            return None
        prompt = MONTHLY_PROMPT.format(last_month=period_months(report.period)[-1])
        stored = {"pdf_sha256": digest, "page": report.monthly_page, "prompt_version": MONTHLY_PROMPT_VERSION,
                  "model": openai_model(),
                  "months": _ask_vision(path, report.monthly_page, prompt, resolution=220, max_tokens=900)}
        entry["monthly"] = stored
        READINGS_PATH.write_text(json.dumps(readings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return check_monthly(report.period, stored["months"], total)


def main(argv: Optional[List[str]] = None) -> int:
    from scripts.oaic.oaic_consistency import period_label
    from scripts.oaic.oaic_dataset import load_dataset, promote_records

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf-dir", default="oaic_pdfs")
    parser.add_argument("--dry-run", action="store_true", help="report the changes; no vision call, nothing written")
    args = parser.parse_args(argv)

    dataset = load_dataset()
    if dataset is None:
        print("ERROR: no published OAIC dataset; run refresh_oaic_statistics.py first", file=sys.stderr)
        return 1
    current = {period_label(r): r for r in dataset["records"]}
    readings = json.loads(READINGS_PATH.read_text(encoding="utf-8")) if READINGS_PATH.exists() else {}
    new_records: List[Dict[str, Any]] = []
    for report in REPORTS:
        path = Path(args.pdf_dir) / report.pdf
        if not path.is_file():
            print(f"ERROR: {path} is missing", file=sys.stderr)
            return 1
        try:
            figures = report_figures(report, _read_text(path))
            shares = chart_shares(report, path, readings, args.dry_run)
            sectors = read_sectors(report.period, _page_text(path, report.sector_page))
            distribution = chart_distribution(report, path, readings, figures["total_notifications"], args.dry_run)
            monthly = chart_monthly(report, path, readings, figures["total_notifications"], args.dry_run)
        except FigureNotFound as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        record = current.get(report.period)
        if record is None:
            print(f"ERROR: the dataset has no record for {report.period}", file=sys.stderr)
            return 1
        print(report.period, {f: (record.get(f), v) for f, v in figures.items() if record.get(f) != v}, shares or "(not read yet)")
        print("   sectors", sectors)
        print("   bands", distribution or "(not read yet)", "| average was", record.get("individuals_affected_average"))
        print("   months", monthly or "(not read yet)")
        if shares is not None and distribution is not None and monthly is not None:
            new_records.append(apply_figures(record, figures, shares, sectors, distribution, monthly))
    new_records += [without_misattributed_average(current[p]) for p in MISATTRIBUTED_AVERAGES]
    if args.dry_run:
        return 0
    result = promote_records(new_records, "OAIC report PDF", legacy_dir=None)
    print(f"promoted {result.accepted}; rejected {result.rejected}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
