#!/usr/bin/env python3
"""
Export deduplicated cyber events to Excel with LLM-summarized descriptions.

This script:
1. Reads the active deduplicated events (the shared reporting population:
   status = 'Active', dated in the dashboard reporting period unless --all-dates)
2. Collects all associated text from EnrichedEvents and RawEvents
3. Uses OpenAI GPT to summarize each event
4. Creates anonymized versions with entity names replaced by generic terms
5. Outputs to Excel with columns:
   - Event Date
   - Event Title
   - Event Description (LLM-summarized)
   - Anonymised Description (entity names removed, no dates/years, no title)
   - Records Affected
   - Entity Type (industry category)
   - Attack Type
   - Export Notes (missing source links, summary / anonymisation failures)

Failures are never hidden. A failed anonymisation writes "[anonymisation
failed] (reason)" - never the original text - and is counted; --no-llm marks
the anonymised column "[not anonymised]". Source text comes only from exact
relationships (EventDeduplicationMap, DeduplicatedEventSources), never title
matching. The script exits 1 if any summary or anonymisation failed.

Usage:
    python export_events_excel.py
    python export_events_excel.py --output events_export.xlsx
    python export_events_excel.py --limit 100  # Export only first 100 events
    python export_events_excel.py --no-llm     # Skip LLM summarization (use raw text)
    python export_events_excel.py --exclude-unknown-records  # Exclude events with unknown records affected
    python export_events_excel.py --all-dates  # Include pre-2020 and undated incidents
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from openai import OpenAI
import pandas as pd
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cyber_data_collector.utils.llm_models import (  # noqa: E402
    add_model_arguments, apply_model_arguments, chat_params, openai_fast_model,
)
from cyber_data_collector.reporting.population import (  # noqa: E402
    ReconciliationCheck,
    count_incidents,
    dashboard_period,
    incident_filter,
    reconcile,
)
from scripts.export.anonymisation import (  # noqa: E402
    NOT_ANONYMISED,
    AnonymisationError,
    failure_placeholder,
    identifying_leak,
)

# Load environment variables
load_dotenv()


class ExportConfigurationError(RuntimeError):
    """The export cannot run as asked (e.g. LLM requested but no API key)."""


def get_openai_client() -> Optional[OpenAI]:
    """OpenAI client, or None when OPENAI_API_KEY is not set."""
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


NO_SOURCE_NOTE = ("No linked source records (EventDeduplicationMap / DeduplicatedEventSources); "
                  "description built from the event row only")


def get_event_source_text(cursor: sqlite3.Cursor, deduplicated_event_id: str) -> Tuple[str, Optional[str]]:
    """Collect the source text of one deduplicated event.

    Only exact relationships are used: the event row itself, its member records
    through ``EventDeduplicationMap`` (enriched text and the raw article), and
    ``DeduplicatedEventSources`` snippets. There is deliberately no title
    matching - a 30-character title prefix used to attach unrelated articles
    ("Cyber attack on ..." matches hundreds) when the map was missing.

    Returns:
        ``(text, note)``; ``note`` explains a missing link and is None otherwise.
    """
    texts: List[str] = []

    cursor.execute("""
        SELECT title, description, summary
        FROM DeduplicatedEvents
        WHERE deduplicated_event_id = ?
    """, (deduplicated_event_id,))
    row = cursor.fetchone()
    if row:
        if row[1]:
            texts.append(f"Description: {row[1]}")
        if row[2]:
            texts.append(f"Summary: {row[2]}")

    cursor.execute("""
        SELECT DISTINCT ee.title, ee.description, ee.summary
        FROM EventDeduplicationMap edm
        JOIN EnrichedEvents ee ON edm.enriched_event_id = ee.enriched_event_id
        WHERE edm.deduplicated_event_id = ?
    """, (deduplicated_event_id,))
    enriched_rows = cursor.fetchall()
    for erow in enriched_rows:
        if erow[1] and erow[1] not in str(texts):
            texts.append(f"Event Description: {erow[1]}")
        if erow[2] and erow[2] not in str(texts):
            texts.append(f"Event Summary: {erow[2]}")

    cursor.execute("""
        SELECT DISTINCT re.raw_title, re.raw_description,
               SUBSTR(re.raw_content, 1, 10000) as content_preview
        FROM EventDeduplicationMap edm
        JOIN EnrichedEvents ee ON edm.enriched_event_id = ee.enriched_event_id
        JOIN RawEvents re ON ee.raw_event_id = re.raw_event_id
        WHERE edm.deduplicated_event_id = ?
        LIMIT 3
    """, (deduplicated_event_id,))
    raw_rows = cursor.fetchall()
    for rrow in raw_rows:
        if rrow[2] and len(rrow[2]) > 100:
            texts.append(f"Article Content:\n{rrow[2]}")

    snippets: List[str] = []
    try:
        cursor.execute("""
            SELECT content_snippet FROM DeduplicatedEventSources
            WHERE deduplicated_event_id = ? AND content_snippet IS NOT NULL
              AND TRIM(content_snippet) != ''
            LIMIT 5
        """, (deduplicated_event_id,))
        snippets = [r[0] for r in cursor.fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
    for snippet in snippets:
        if snippet not in str(texts):
            texts.append(f"Source Snippet: {snippet}")

    note = None if (enriched_rows or raw_rows or snippets) else NO_SOURCE_NOTE
    return "\n\n".join(texts), note


class SummaryError(RuntimeError):
    """The LLM summary could not be produced."""


def summarize_with_llm(client: OpenAI, text: str, max_words: int = 500) -> str:
    """Summarise ``text`` to at most ``max_words`` with the fast OpenAI model.

    Raises:
        SummaryError: the call failed or returned nothing. The caller decides
            what to show; this function never passes raw text off as a summary.
    """
    if not text or len(text.strip()) < 50:
        return text or ""

    # Truncate input if too long (to stay within token limits)
    if len(text) > 30000:
        text = text[:30000] + "\n\n[Text truncated...]"

    try:
        response = client.chat.completions.create(
            **chat_params(openai_fast_model(), max_tokens=2000),
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a cybersecurity analyst summarizing data breach events.
Create a comprehensive summary of the cyber security incident described below.
The summary should be factual, professional, and include:
- What happened (type of attack/breach)
- Who was affected (organization and individuals)
- What data was compromised (be specific about data types)
- When it occurred (if known)
- Scale of impact (number of records/individuals affected)
- Any response or remediation mentioned

Write up to {max_words} words. Be thorough and include all relevant details."""
                },
                {
                    "role": "user",
                    "content": f"Summarize this cyber security event:\n\n{text}"
                }
            ]
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as e:
        raise SummaryError(f"LLM summarization failed: {e}") from e
    if not content:
        raise SummaryError("LLM summarization returned no text")
    return content


def anonymize_with_llm(client: OpenAI, text: str, industry: str = None, title: str = None,
                       all_entity_names: List[str] = None,
                       identifying_names: Optional[List[Optional[str]]] = None) -> str:
    """Anonymise ``text`` with the fast OpenAI model.

    Returns the anonymised text ('' for empty input). Raises
    :class:`AnonymisationError` when there is no client, the call fails or
    returns nothing, or the answer still names the victim / attacker or repeats
    the title - the original text is never returned in its place. Short text is
    anonymised like any other: a short description can still name the victim.
    """
    if not text or not text.strip():
        return ""
    if client is None:
        raise AnonymisationError("no LLM client available")

    industry_hint = f"The victim organization operates in the {industry} sector." if industry and industry != "Unknown" else ""

    # Build list of known entities to help the LLM
    entity_hint = ""
    if all_entity_names and len(all_entity_names) > 0:
        # Include up to 50 entity names as examples
        sample_entities = all_entity_names[:50]
        entity_hint = f"\n\nKnown entity names that MUST be anonymized if present: {', '.join(sample_entities)}"

    try:
        response = client.chat.completions.create(
            **chat_params(openai_fast_model(), max_tokens=2000),
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a cybersecurity analyst creating a fully anonymized incident report.
Your task is to remove ALL identifying information while preserving the incident details.

CRITICAL RULES - You MUST follow ALL of these:

1. REMOVE ALL ENTITY NAMES:
   - Replace ALL company/organization names with generic descriptions based on their industry
     (e.g., "Medibank" → "a major health insurer", "Optus" → "a telecommunications provider",
      "Commonwealth Bank" → "a major bank", "Woolworths" → "a major retailer")
   - Replace ALL person names with generic roles (e.g., "John Smith, CEO" → "the CEO")
   - Replace ALL threat actor/hacker group names with "threat actors" or "attackers"
   - Be thorough - catch ALL organization names including subsidiaries, partners, vendors

2. REMOVE ALL DATES AND TIME REFERENCES:
   - Remove ALL specific dates (e.g., "January 15, 2024", "15/01/2024", "2024-01-15")
   - Remove ALL year references (e.g., "in 2024", "during 2023", "since 2022")
   - Remove ALL month references with years (e.g., "March 2024", "Q1 2024")
   - Replace with generic terms if needed for context (e.g., "recently", "the incident")

3. REMOVE ANY TITLE AT THE START:
   - If the text begins with a title or headline, remove it completely
   - Start directly with the incident description

4. PRESERVE:
   - Technical attack details and methods
   - Types of data compromised
   - Number of records/individuals affected (keep the numbers)
   - Impact and consequences
   - Response actions taken (anonymized)

{industry_hint}{entity_hint}

Return ONLY the fully anonymized text with no dates, no entity names, and no title. Do not include any explanations."""
                },
                {
                    "role": "user",
                    "content": f"Fully anonymize this incident description (remove all entity names, dates, years, and any title):\n\n{text}"
                }
            ]
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as e:
        # The exception text is not echoed: an API error can quote its input.
        raise AnonymisationError(f"LLM call failed ({type(e).__name__})") from e
    if not content:
        raise AnonymisationError("LLM returned no text")
    leak = identifying_leak(content, identifying_names or [], title)
    if leak:
        raise AnonymisationError(leak)
    return content


def truncate_text(text: str, max_words: int = 500) -> str:
    """Simple truncation without LLM."""
    if not text:
        return ""
    words = text.split()
    if len(words) > max_words:
        return " ".join(words[:max_words]) + "..."
    return text


SUMMARY_FAILED_PREFIX = "[LLM summary failed - first words of the source text follow] "
NOT_ANONYMISED_NO_LLM = f"{NOT_ANONYMISED} (--no-llm: no anonymiser was run)"


def _row(event_data: Dict, description: str, anonymised: str, notes: List[str]) -> Dict:
    return {
        'Event Date': event_data['event_date'],
        'Event Title': event_data['title'] or 'Untitled Event',
        'Event Description': description,
        'Anonymised Description': anonymised,
        'Records Affected': event_data['records_affected'],
        'Entity Type': event_data['industry'],
        'Attack Type': event_data['event_type'],
        'Export Notes': "; ".join(n for n in notes if n),
    }


def process_single_event(
    client: OpenAI,
    event_data: Dict,
    source_text: str,
    max_words: int,
    use_llm: bool,
    all_entity_names: List[str] = None,
    source_note: Optional[str] = None,
) -> Dict:
    """Summarise and anonymise one event.

    The returned row carries ``_summary_failed`` / ``_anonymisation_failed``
    flags (stripped before writing) so the export can count failures. The
    anonymised column is never the original text: without an LLM it says it was
    not anonymised, and on any failure it says anonymisation failed.
    """
    notes: List[str] = [source_note] if source_note else []
    summary_failed = False
    anonymisation_failed = False

    if not use_llm:
        description = truncate_text(source_text, max_words)
        anonymised = NOT_ANONYMISED_NO_LLM if description else ""
        row = _row(event_data, description, anonymised, notes)
        row.update(_summary_failed=False, _anonymisation_failed=False)
        return row

    try:
        description = summarize_with_llm(client, source_text, max_words)
    except SummaryError as exc:
        summary_failed = True
        anonymisation_failed = True
        notes.append(str(exc))
        description = SUMMARY_FAILED_PREFIX + truncate_text(source_text, max_words)
        anonymised = failure_placeholder("(no summary to anonymise)")
    else:
        try:
            anonymised = anonymize_with_llm(
                client, description, event_data['industry'], event_data['title'], all_entity_names,
                identifying_names=[event_data.get('victim_organization_name'),
                                   event_data.get('attacking_entity_name')])
        except AnonymisationError as exc:
            anonymisation_failed = True
            notes.append(f"anonymisation failed: {exc}")
            anonymised = failure_placeholder(f"({exc})")

    row = _row(event_data, description, anonymised, notes)
    row.update(_summary_failed=summary_failed, _anonymisation_failed=anonymisation_failed)
    return row


def failed_worker_row(event_data: Dict, error: Exception) -> Dict:
    """Row for an event whose worker raised: placeholders only, never source text."""
    row = _row(event_data, f"[export failed: {error}]",
               failure_placeholder(f"(export worker error: {error})"),
               [f"export worker error: {error}"])
    row.update(_summary_failed=True, _anonymisation_failed=True)
    return row


@dataclass
class ExcelExportResult:
    """What an Excel export wrote and what went wrong while writing it."""
    output_path: str
    rows: int = 0
    summary_failures: int = 0
    anonymisation_failures: int = 0
    events_without_sources: int = 0
    used_llm: bool = True

    @property
    def ok(self) -> bool:
        """True only when every requested summary and anonymisation succeeded."""
        return self.summary_failures == 0 and self.anonymisation_failures == 0


def get_all_entity_names(cursor: sqlite3.Cursor) -> List[str]:
    """Every known entity name, longest first, for the anonymisation prompt.

    A query error propagates: an anonymiser run without its name list would
    quietly let names through.
    """
    entity_names = set()
    for query in (
        "SELECT DISTINCT entity_name FROM EntitiesV2 WHERE entity_name IS NOT NULL",
        "SELECT DISTINCT victim_organization_name FROM DeduplicatedEvents "
        "WHERE victim_organization_name IS NOT NULL",
        "SELECT DISTINCT attacking_entity_name FROM DeduplicatedEvents "
        "WHERE attacking_entity_name IS NOT NULL",
    ):
        cursor.execute(query)
        for row in cursor.fetchall():
            if row[0]:
                entity_names.add(row[0].strip())

    # Sort by length descending to replace longer names first
    return sorted(list(entity_names), key=len, reverse=True)


def export_events_to_excel(
    db_path: str = 'instance/cyber_events.db',
    output_path: str = None,
    limit: int = None,
    use_llm: bool = True,
    max_words: int = 500,
    max_workers: int = 10,
    exclude_unknown_records: bool = False,
    all_dates: bool = False,
    client: Optional[OpenAI] = None,
) -> ExcelExportResult:
    """Export deduplicated events to Excel file with parallel LLM processing.

    The rows are the reporting population: active incidents dated in the dashboard
    reporting period (``all_dates`` lifts the date bound), so this workbook and the
    dashboards count the same incidents.

    Failures are counted, never hidden: a failed summary is labelled as such, a
    failed anonymisation leaves a placeholder (never the original text), and the
    result reports both counts. ``ExcelExportResult.ok`` is False if any failed.

    Raises:
        FileNotFoundError: the database does not exist.
        ExportConfigurationError: ``use_llm`` without an OpenAI key (the export
            used to fall back to raw text, which also filled the anonymised
            column with identifying text).
    """

    if output_path is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = f'cyber_events_export_{timestamp}.xlsx'

    if not Path(db_path).exists():
        raise FileNotFoundError(f"Database not found at {db_path}")

    if use_llm and client is None:
        client = get_openai_client()
        if client is None:
            raise ExportConfigurationError(
                "OPENAI_API_KEY is not set: cannot summarise or anonymise. Set the key, "
                "or pass --no-llm (descriptions are then truncated source text and the "
                "anonymised column is marked as not anonymised).")

    result = ExcelExportResult(output_path=output_path, used_llm=use_llm)

    # Connect to database using context manager to ensure the connection is always closed
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get all entity names for thorough anonymization
        all_entity_names = get_all_entity_names(cursor) if use_llm else []
        if use_llm:
            print(f"Loaded {len(all_entity_names)} entity names for anonymization")

        # Build query with optional filter for unknown records
        period = None if all_dates else dashboard_period()
        where, where_params = incident_filter(period)
        query = f"""
            SELECT
                deduplicated_event_id,
                title,
                event_date,
                event_type,
                victim_organization_name,
                victim_organization_industry,
                attacking_entity_name,
                records_affected,
                description,
                summary
            FROM DeduplicatedEvents
            WHERE {where}
        """

        if exclude_unknown_records:
            query += " AND records_affected IS NOT NULL AND records_affected != '' AND CAST(records_affected AS TEXT) != 'Unknown'"

        query += " ORDER BY event_date DESC"

        params: tuple
        if limit:
            query += " LIMIT ?"
            params = (*where_params, limit)
        else:
            params = tuple(where_params)

        cursor.execute(query, params)
        events = cursor.fetchall()

        # An unfiltered export must contain exactly the population.
        if not limit and not exclude_unknown_records:
            reconcile([ReconciliationCheck(
                "Excel export rows", count_incidents(conn, period), len(events),
                f"period: {period}" if period else "all dates")],
                context="export_events_excel")

        print(f"Processing {len(events)} events...")

        # Phase 1: Collect all source texts (sequential - uses DB)
        print("Phase 1: Gathering source texts from database...")
        event_data_list = []
        for event in tqdm(events, desc="Reading events", smoothing=0):
            event_id = event['deduplicated_event_id']

            # Clean up event type - use "Unknown" for unknown/null values
            event_type = event['event_type']
            if not event_type or event_type.lower() in ('unknown', 'none', '', 'null'):
                event_type = 'Unknown'
            elif '.' in event_type:
                event_type = event_type.split('.')[-1].replace('_', ' ').title()

            # Clean up industry - use "Unknown" for unknown/null values
            industry = event['victim_organization_industry']
            if not industry or industry.lower() in ('unknown', 'none', '', 'null'):
                industry = 'Unknown'

            source_text, source_note = get_event_source_text(cursor, event_id)
            if source_note:
                result.events_without_sources += 1

            event_data_list.append({
                'event_data': {
                    'event_date': event['event_date'],
                    'title': event['title'],
                    'records_affected': event['records_affected'],
                    'industry': industry,
                    'event_type': event_type,
                    'victim_organization_name': event['victim_organization_name'],
                    'attacking_entity_name': event['attacking_entity_name'],
                },
                'source_text': source_text,
                'source_note': source_note,
            })

    # Phase 2: Process with LLM in parallel
    print(f"Phase 2: Processing ({max_workers} parallel workers)...")
    rows: List[Optional[Dict]] = [None] * len(event_data_list)  # Pre-allocate to maintain order

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(
                process_single_event,
                client,
                item['event_data'],
                item['source_text'],
                max_words,
                use_llm,
                all_entity_names,
                item['source_note'],
            ): idx
            for idx, item in enumerate(event_data_list)
        }

        for future in tqdm(as_completed(future_to_idx), total=len(future_to_idx), desc="Processing", smoothing=0):
            idx = future_to_idx[future]
            try:
                rows[idx] = future.result()
            except Exception as e:
                print(f"Error processing event {idx}: {e}")
                rows[idx] = failed_worker_row(event_data_list[idx]['event_data'], e)

    for row in rows:
        result.summary_failures += int(row.pop('_summary_failed'))
        result.anonymisation_failures += int(row.pop('_anonymisation_failed'))
    result.rows = len(rows)

    # Create DataFrame and export to Excel
    df = pd.DataFrame(rows, columns=list(_row(
        {'event_date': None, 'title': None, 'records_affected': None, 'industry': None,
         'event_type': None}, '', '', []).keys()))

    # Write to Excel with formatting
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Cyber Events')

        # Get worksheet and apply formatting
        worksheet = writer.sheets['Cyber Events']
        from openpyxl.styles import Alignment

        # Define column widths - wider for description columns
        column_widths = {
            'A': 12,   # Event Date
            'B': 40,   # Event Title
            'C': 80,   # Event Description
            'D': 80,   # Anonymised Description
            'E': 15,   # Records Affected
            'F': 25,   # Entity Type
            'G': 20,   # Attack Type
            'H': 50,   # Export Notes
        }

        # Apply column widths
        for col_letter, width in column_widths.items():
            worksheet.column_dimensions[col_letter].width = width

        # Enable text wrapping for description columns (C and D)
        # and set appropriate row heights
        for row_num in range(2, len(df) + 2):  # Start from row 2 (after header)
            # Event Description (column C)
            cell_c = worksheet.cell(row=row_num, column=3)
            cell_c.alignment = Alignment(wrap_text=True, vertical='top')

            # Anonymised Description (column D)
            cell_d = worksheet.cell(row=row_num, column=4)
            cell_d.alignment = Alignment(wrap_text=True, vertical='top')

            # Calculate row height based on content length
            desc_len = len(str(cell_c.value or ''))
            # Approximate: 80 chars per line at width 80, ~15 points per line
            estimated_lines = max(desc_len // 80, 1)
            row_height = min(estimated_lines * 15, 400)  # Cap at 400 points
            worksheet.row_dimensions[row_num].height = max(row_height, 30)

        # Format header row
        for col_num in range(1, len(df.columns) + 1):
            cell = worksheet.cell(row=1, column=col_num)
            cell.alignment = Alignment(horizontal='center', vertical='center')

        summary = pd.DataFrame([
            ('Rows', result.rows),
            ('LLM summaries and anonymisation', 'yes' if use_llm else 'no (--no-llm)'),
            ('Summary failures', result.summary_failures),
            ('Anonymisation failures (placeholder written)', result.anonymisation_failures),
            ('Events with no linked source records', result.events_without_sources),
        ], columns=['Item', 'Value'])
        summary.to_excel(writer, index=False, sheet_name='Export Summary')

    print(f"\nExported {len(rows)} events to: {output_path}")
    print(f"  Summary failures: {result.summary_failures}")
    print(f"  Anonymisation failures (placeholder written): {result.anonymisation_failures}")
    print(f"  Events with no linked source records: {result.events_without_sources}")
    return result


def main():
    parser = argparse.ArgumentParser(
        description='Export deduplicated cyber events to Excel with LLM-summarized descriptions.'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        default=None,
        help='Output Excel file path (default: cyber_events_export_<timestamp>.xlsx)'
    )
    parser.add_argument(
        '--db-path',
        type=str,
        default='instance/cyber_events.db',
        help='Path to the SQLite database (default: instance/cyber_events.db)'
    )
    parser.add_argument(
        '--limit', '-l',
        type=int,
        default=None,
        help='Limit number of events to export (default: all)'
    )
    parser.add_argument(
        '--no-llm',
        action='store_true',
        help='Skip LLM summarization, use simple text truncation instead'
    )
    parser.add_argument(
        '--max-words', '-w',
        type=int,
        default=500,
        help='Maximum words for event description (default: 500)'
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=10,
        help='Number of parallel workers for LLM processing (default: 10)'
    )
    parser.add_argument(
        '--exclude-unknown-records',
        action='store_true',
        help='Exclude events where the number of customer records affected is unknown'
    )
    add_model_arguments(parser)
    parser.add_argument(
        '--all-dates',
        action='store_true',
        help='Do not limit to the dashboard reporting period (adds pre-2020 and undated incidents)'
    )

    args = parser.parse_args()
    apply_model_arguments(args)

    # Check if openpyxl is available
    try:
        import openpyxl
    except ImportError:
        print("Error: openpyxl is required for Excel export.")
        print("Install with: pip install openpyxl")
        sys.exit(1)

    try:
        result = export_events_to_excel(
            db_path=args.db_path,
            output_path=args.output,
            limit=args.limit,
            use_llm=not args.no_llm,
            max_words=args.max_words,
            max_workers=args.workers,
            exclude_unknown_records=args.exclude_unknown_records,
            all_dates=args.all_dates,
        )
    except (FileNotFoundError, ExportConfigurationError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    print(f"\nOutput file: {Path(result.output_path).resolve()}")
    if not result.ok:
        # The workbook is written (with placeholders, never the original text in
        # the anonymised column) but the export did not succeed.
        print(f"[FAILED] {result.summary_failures} summary failure(s) and "
              f"{result.anonymisation_failures} anonymisation failure(s); see the "
              "'Export Notes' column and the 'Export Summary' sheet.")
        sys.exit(1)


if __name__ == '__main__':
    main()
