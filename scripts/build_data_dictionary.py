"""Build the data dictionary page (``dashboard/data_dictionary.html``).

The page documents every table and view of the database: first a picture of how
the tables relate, then one section per table with its purpose and, for each
column, the declared type, primary-key and not-null flags and a description.

Where everything comes from:

* **Schema** - read from the database itself (``sqlite_master``, ``PRAGMA
  table_info``, ``PRAGMA foreign_key_list``), so the page always matches the
  snapshot it is built from. The database is opened read-only.
* **Descriptions** - ``docs/data-dictionary.md`` (its column tables, its
  per-table paragraphs and its ``Table.column - text`` bullets); describe a new
  table or column there. ``docs/data_dictionary_descriptions.json`` holds the
  diagram's areas, the owners of shared key columns and the links SQLite does
  not declare, and may fill a gap the Markdown leaves. A column neither
  source describes is shown as "No description yet" and counted; the count is
  printed and is part of :class:`Dictionary`.
* **Links** - declared foreign keys, plus links inferred from column names: a
  column named after another table's key column (``raw_event_id``,
  ``entity_id``) or ending in it (``master_enriched_event_id``,
  ``customer_entity_id``). Inferred links are drawn dashed.

Usage:
    python scripts/build_data_dictionary.py [--db PATH] [--out dashboard/data_dictionary.html]
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MARKDOWN = ROOT / "docs" / "data-dictionary.md"
DEFAULT_SUPPLEMENT = ROOT / "docs" / "data_dictionary_descriptions.json"

OTHER_GROUP = "Other"
ARROW, DASHED_ARROW = "→", "⇢"
#: The detailed diagram: core tables at fixed grid positions (column, row); a column may be fractional.
CORE_LAYOUT: Dict[str, Tuple[float, int]] = {
    "RawEvents": (0, 0), "EnrichedEvents": (1, 0), "DeduplicatedEvents": (2, 0), "ASDRiskClassifications": (3, 0),
    "EventDeduplicationMap": (0, 1), "DeduplicatedEventSources": (3, 1),
    "EnrichedEventEntities": (0.5, 2), "EntitiesV2": (1.5, 2), "DeduplicatedEventEntities": (2.5, 2),
}


# --- schema ----------------------------------------------------------------------------------------

@dataclass
class Column:
    name: str
    type: str
    notnull: bool
    pk: int                     # position in the primary key, 0 when not part of it
    default: Optional[str]


@dataclass
class Link:
    table: str
    column: str
    target: str
    target_column: str
    declared: bool


@dataclass
class Table:
    name: str
    kind: str                   # "table" or "view"
    columns: List[Column]
    purpose: str = ""
    group: str = OTHER_GROUP

    @property
    def pk_columns(self) -> List[str]:
        return [c.name for c in sorted(self.columns, key=lambda c: c.pk) if c.pk]


@dataclass
class Descriptions:
    """Table purposes and column descriptions (Markdown text) from one source."""

    purposes: Dict[str, str] = field(default_factory=dict)
    columns: Dict[Tuple[str, str], str] = field(default_factory=dict)


@dataclass
class Supplement:
    descriptions: Descriptions
    groups: List[Tuple[str, str, List[str]]]          # (name, description, tables)
    key_owners: Dict[str, str]
    extra_links: List[Tuple[str, str, str]]           # (table, column, target table)
    not_links: Set[Tuple[str, str]]
    mirrors: Dict[str, Tuple[str, str]] = field(default_factory=dict)   # table -> (source table, note)


@dataclass
class Dictionary:
    tables: List[Table]
    links: List[Link]
    descriptions: Dict[Tuple[str, str], str]
    groups: List[Tuple[str, str]]                      # (name, description), in display order
    undescribed: List[Tuple[str, str]]
    no_purpose: List[str]

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables)


def read_schema(conn: sqlite3.Connection) -> Tuple[List[Table], List[Link]]:
    """Every user table and view, with its columns and its declared foreign keys."""
    tables, links = [], []
    rows = conn.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view') "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name COLLATE NOCASE").fetchall()
    for kind, name in rows:
        quoted = name.replace('"', '""')
        cols = [Column(r[1], r[2] or "", bool(r[3]), int(r[5]), r[4])
                for r in conn.execute(f'PRAGMA table_info("{quoted}")')]
        tables.append(Table(name, kind, cols))
        for fk in conn.execute(f'PRAGMA foreign_key_list("{quoted}")'):
            links.append(Link(name, fk[3], fk[2], fk[4] or "", True))
    names = {t.name for t in tables}
    for link in links:     # an FK naming no column refers to the target's primary key
        if not link.target_column and link.target in names:
            target = next(t for t in tables if t.name == link.target)
            link.target_column = ", ".join(target.pk_columns)
    return tables, links


def is_backup(name: str) -> bool:
    return "_backup" in name.lower()


def infer_links(tables: Sequence[Table], declared: Sequence[Link], key_owners: Dict[str, str],
                extra_links: Iterable[Tuple[str, str, str]] = (),
                not_links: Iterable[Tuple[str, str]] = ()) -> List[Link]:
    """Links SQLite does not declare, inferred from column names.

    A *key column* is the single-column primary key (ending ``_id``) of exactly one table that is
    not a backup, or a column ``key_owners`` assigns to a table. A column of another table named
    after a key column, or ending in ``_<key column>``, links to its owner. Declared links,
    ``not_links``, a table's own key, views and backup tables (``*_backup*``: copies kept from
    old schemas, which keep only their declared keys) are left out; ``extra_links`` are added.
    """
    by_name = {t.name: t for t in tables}
    owners: Dict[str, List[str]] = {}
    for t in tables:
        pk = t.pk_columns
        if t.kind == "table" and not is_backup(t.name) and len(pk) == 1 and pk[0].endswith("_id") and pk[0] != "id":
            owners.setdefault(pk[0], []).append(t.name)
    keys = {col: names[0] for col, names in owners.items() if len(names) == 1}
    keys.update({col: owner for col, owner in key_owners.items() if owner in by_name})
    declared_cols = {(l.table, l.column) for l in declared}
    skip = set(not_links)
    inferred: List[Link] = []
    for t in tables:
        if t.kind != "table" or is_backup(t.name):     # views and kept copies of old tables link nowhere
            continue
        for c in t.columns:
            if (t.name, c.name) in declared_cols or (t.name, c.name) in skip:
                continue
            target = None
            if c.name in keys:
                target = keys[c.name]
                key = c.name
            else:
                for key_col in sorted(keys, key=len, reverse=True):
                    if c.name.endswith("_" + key_col):
                        target, key = keys[key_col], key_col
                        break
            if not target or target == t.name and c.name == key:
                continue
            if target not in by_name or is_backup(target):
                continue
            inferred.append(Link(t.name, c.name, target, key, False))
    for table, column, target in extra_links:
        if table in by_name and target in by_name and (table, column) not in declared_cols \
                and not any(l.table == table and l.column == column for l in inferred):
            inferred.append(Link(table, column, target, ", ".join(by_name[target].pk_columns), False))
    return inferred


# --- descriptions ----------------------------------------------------------------------------------

_TICKED = re.compile(r"`([^`]+)`")
_BULLET = re.compile(r"^`(\w+)\.(\w+)`\s*(?:[-–—:]\s*)(.+)$", re.S)
_HEADING = re.compile(r"^(#{2,6})\s+(.*)$")


def _cells(line: str) -> List[str]:
    """The cells of a Markdown table row; ``\\|`` inside a cell is kept."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", body)]


def _names_in(text: str, known: Set[str]) -> List[str]:
    found = []
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        if word in known and word not in found:
            found.append(word)
    return found


def _column_names(cell: str) -> List[str]:
    if cell.startswith("**") and "`" not in cell:
        return []
    ticked = _TICKED.findall(cell)
    names = ticked or [p.strip(" *") for p in cell.split(",")]
    return [n for n in (x.strip() for x in names) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)]


def _first_sentences(text: str, limit: int = 420) -> str:
    """At most ``limit`` characters, cut at a sentence end where there is one."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".) "))
    return cut[:end + 1] if end > 80 else cut.rstrip() + "…"


def parse_markdown(text: str, known_tables: Iterable[str]) -> Descriptions:
    """Table purposes and column descriptions from the Markdown data dictionary.

    Read: column tables under a heading naming one table (``| Column | ... | Description |``),
    tables with a ``Table`` and a ``Column`` column, ``| Table | Description |`` tables, the first
    paragraph under a heading naming one table, paragraphs that open with a table's name, and
    bullets of the form ``Table.column - text``. A description already found is not replaced.
    """
    known = set(known_tables)
    out = Descriptions()
    heading_tables: List[str] = []
    subject: Optional[str] = None
    first_paragraph_taken = False
    lines = text.splitlines()
    i = 0

    def add_column(table: str, column: str, desc: str) -> None:
        if table in known and desc and (table, column) not in out.columns:
            out.columns[(table, column)] = desc.strip()

    def add_purpose(table: str, desc: str) -> None:
        if table in known and desc and table not in out.purposes:
            out.purposes[table] = _first_sentences(desc)

    while i < len(lines):
        line = lines[i]
        m = _HEADING.match(line)
        if m:
            heading_tables = _names_in(m.group(2), known)
            subject = heading_tables[0] if len(heading_tables) == 1 else None
            first_paragraph_taken = False
            i += 1
            continue
        if line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            if len(block) < 2:
                continue
            header = [h.lower().strip("* ") for h in _cells(block[0])]
            rows = [_cells(r) for r in block[2:]]
            desc_idx = header.index("description") if "description" in header else None
            if desc_idx is None:
                continue
            if "table" in header and "column" in header:
                t_idx, c_idx = header.index("table"), header.index("column")
                for r in rows:
                    if len(r) <= max(t_idx, c_idx, desc_idx):
                        continue
                    table = r[t_idx].strip("`* ")
                    for col in _column_names(r[c_idx]):
                        add_column(table, col, r[desc_idx])
            elif header and header[0] == "column" and subject:
                for r in rows:
                    if len(r) > desc_idx:
                        for col in _column_names(r[0]):
                            add_column(subject, col, r[desc_idx])
            elif header and header[0] == "table":
                for r in rows:
                    if len(r) > desc_idx:
                        add_purpose(r[0].strip("`* "), r[desc_idx])
            continue
        if not line.strip() or line.lstrip().startswith(("```", "**Indexes", "**Foreign", "**Constraints",
                                                         "**Triggers", "**Note", "---")):
            i += 1
            continue
        # a paragraph or a bullet: gather its continuation lines
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith(("|", "#", "* ", "- ")) \
                and not _HEADING.match(lines[i]):
            para.append(lines[i].strip())
            i += 1
        paragraph = " ".join(para)
        body = re.sub(r"^[*-]\s+", "", paragraph)
        bullet = _BULLET.match(body)
        if bullet:
            add_column(bullet.group(1), bullet.group(2), bullet.group(3))
            continue
        lead = re.match(r"^`?(\w+)`?[\s(]", body)
        if lead and lead.group(1) in known and lead.group(1) in heading_tables:
            subject = lead.group(1)
            add_purpose(subject, body)
            first_paragraph_taken = True       # the section introduces its tables one by one
            continue
        if heading_tables and not first_paragraph_taken and not paragraph.startswith(("* ", "- ")):
            for table in heading_tables:      # a heading naming several tables: the paragraph covers each
                add_purpose(table, body)
            first_paragraph_taken = True
    return out


def load_supplement(path: Optional[Path]) -> Supplement:
    """The committed descriptions, groups and link rules (``docs/data_dictionary_descriptions.json``)."""
    data = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else {}
    desc = Descriptions()
    for table, entry in (data.get("tables") or {}).items():
        if entry.get("purpose"):
            desc.purposes[table] = entry["purpose"]
        for col, text in (entry.get("columns") or {}).items():
            desc.columns[(table, col)] = text
    groups = [(g["name"], g.get("description", ""), list(g.get("tables", []))) for g in data.get("groups", [])]
    return Supplement(
        descriptions=desc,
        groups=groups,
        key_owners=dict(data.get("key_owners") or {}),
        extra_links=[tuple(x) for x in data.get("extra_links", [])],
        not_links={tuple(x.split(".", 1)) for x in data.get("not_links", [])},
        mirrors={t: (m["of"], m.get("note", "")) for t, m in (data.get("mirrors") or {}).items()},
    )


def collect(conn: sqlite3.Connection, markdown_path: Optional[Path] = DEFAULT_MARKDOWN,
            supplement_path: Optional[Path] = DEFAULT_SUPPLEMENT) -> Dictionary:
    """Schema, links, groups and descriptions, put together."""
    tables, declared = read_schema(conn)
    supplement = load_supplement(supplement_path)
    names = [t.name for t in tables]
    markdown = parse_markdown(Path(markdown_path).read_text(encoding="utf-8"), names) \
        if markdown_path and Path(markdown_path).is_file() else Descriptions()
    links = declared + infer_links(tables, declared, supplement.key_owners, supplement.extra_links,
                                   supplement.not_links)
    group_of = {}
    for name, _, members in supplement.groups:
        for member in members:
            group_of.setdefault(member, name)
    def own(table: str, column: str) -> Optional[str]:
        return markdown.columns.get((table, column)) or supplement.descriptions.columns.get((table, column))

    descriptions: Dict[Tuple[str, str], str] = {}
    undescribed, no_purpose = [], []
    for t in tables:
        t.group = group_of.get(t.name, OTHER_GROUP)
        t.purpose = supplement.descriptions.purposes.get(t.name) or markdown.purposes.get(t.name, "")
        if not t.purpose:
            no_purpose.append(t.name)
        for c in t.columns:
            text = own(t.name, c.name)
            if not text and t.name in supplement.mirrors:
                # a history or backup table: the column holds what the source table's column held
                source, note = supplement.mirrors[t.name]
                inherited = own(source, c.name)
                if inherited:
                    text = f"{note} `{source}.{c.name}`: {inherited}".strip()
            if text:
                descriptions[(t.name, c.name)] = text
            else:
                undescribed.append((t.name, c.name))
    groups = [(n, d) for n, d, _ in supplement.groups if any(t.group == n for t in tables)]
    if any(t.group == OTHER_GROUP for t in tables):
        groups.append((OTHER_GROUP, "Tables not yet assigned to an area."))
    return Dictionary(tables, links, descriptions, groups, undescribed, no_purpose)


# --- rendering helpers -----------------------------------------------------------------------------

def md_inline(text: str) -> str:
    """Escape ``text`` and render the inline Markdown the dictionary uses (code, bold, links as text)."""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", out)
    return out


def anchor(table: str) -> str:
    return "t-" + re.sub(r"[^A-Za-z0-9_-]", "-", table)


def _a(text: str) -> str:
    return html.escape(text, quote=True)


# --- diagrams --------------------------------------------------------------------------------------

BOX_W, LINE_H, HEAD_H, COL_STEP, GAP_Y, PAD = 260, 16, 26, 300, 70, 20


def _box_lines(table: Table, links: Sequence[Link], shown: Set[str]) -> List[Tuple[str, str]]:
    """(text, css class) lines for a core box: the key, then links to other core tables."""
    lines = [(f"PK {', '.join(table.pk_columns)}", "pk")] if table.pk_columns else []
    for link in links:
        if link.table == table.name and link.target in shown:
            arrow = ARROW if link.declared else DASHED_ARROW
            # a column named after the target's key needs no name: the arrow and the target say it
            text = f"{arrow} {link.target}" if link.column == link.target_column                 else f"{link.column} {arrow} {link.target}"
            lines.append((text, "fk" if link.declared else "inf"))
    return lines or [("(no keys)", "pk")]


def _anchors(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]
             ) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Endpoints of a link from box ``a`` to box ``b`` (x, y, w, h), on the sides facing each other."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if ay + ah <= by or by + bh <= ay:              # one above the other
        lo, hi = max(ax, bx) + 15, min(ax + aw, bx + bw) - 15
        if lo <= hi:
            xa = xb = (lo + hi) / 2
        else:
            xa = min(max(bx + bw / 2, ax + 15), ax + aw - 15)
            xb = min(max(ax + aw / 2, bx + 15), bx + bw - 15)
        if ay + ah <= by:
            return (xa, ay + ah), (xb, by)
        return (xa, ay), (xb, by + bh)
    ya = ay + ah / 2
    yb = by + bh / 2
    if ax + aw <= bx:
        return (ax + aw, ya), (bx, yb)
    return (ax, ya), (bx + bw, yb)


def core_svg(d: Dictionary) -> str:
    """The detailed diagram of the core pipeline tables, with their keys and links."""
    by_name = {t.name: t for t in d.tables}
    shown = {n for n in CORE_LAYOUT if n in by_name}
    if not shown:
        return ""
    contents = {n: _box_lines(by_name[n], d.links, shown) for n in shown}
    heights = {n: HEAD_H + 8 + LINE_H * len(contents[n]) for n in shown}
    rows = sorted({CORE_LAYOUT[n][1] for n in shown})
    row_y, y = {}, PAD
    for r in rows:
        row_y[r] = y
        y += max(heights[n] for n in shown if CORE_LAYOUT[n][1] == r) + GAP_Y
    boxes = {n: (PAD + CORE_LAYOUT[n][0] * COL_STEP, row_y[CORE_LAYOUT[n][1]], BOX_W, heights[n]) for n in shown}
    width = max(x + w for x, _, w, _ in boxes.values()) + PAD
    height = y - GAP_Y + PAD
    pairs: Dict[Tuple[str, str], List[Link]] = {}
    for link in d.links:
        if link.table in shown and link.target in shown and link.table != link.target:
            pairs.setdefault((link.table, link.target), []).append(link)
    parts = [f'<svg class="erd" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" role="img" '
             f'aria-label="Core pipeline tables and their links" xmlns="http://www.w3.org/2000/svg">',
             _ARROW_DEFS]
    for (src, dst), group in sorted(pairs.items()):
        (x1, y1), (x2, y2) = _anchors(boxes[src], boxes[dst])
        declared = any(l.declared for l in group)
        title = "; ".join(f"{l.table}.{l.column} → {l.target}.{l.target_column}"
                          f" ({'declared' if l.declared else 'inferred'})" for l in group)
        parts.append(f'<line class="edge {"decl" if declared else "infer"}" x1="{x1:.0f}" y1="{y1:.0f}" '
                     f'x2="{x2:.0f}" y2="{y2:.0f}" marker-end="url(#{"arr" if declared else "arri"})">'
                     f'<title>{_a(title)}</title></line>')
    for name in sorted(shown):
        x, y0, w, h = boxes[name]
        parts.append(f'<a href="#{anchor(name)}"><g class="box"><rect x="{x:.0f}" y="{y0:.0f}" width="{w}" '
                     f'height="{h}" rx="6"/><rect class="head" x="{x:.0f}" y="{y0:.0f}" width="{w}" '
                     f'height="{HEAD_H}" rx="6"/><text class="tname" x="{x + 10:.0f}" y="{y0 + 18:.0f}">'
                     f'{_a(name)}</text>')
        for k, (text, cls) in enumerate(contents[name]):
            parts.append(f'<text class="col {cls}" x="{x + 10:.0f}" y="{y0 + HEAD_H + 16 + k * LINE_H:.0f}">'
                         f'{_a(_fit(text, 40))}<title>{_a(text)}</title></text>')
        parts.append("</g></a>")
    parts.append("</svg>")
    return "".join(parts)


def _fit(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


_ARROW_DEFS = ('<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
               'orient="auto-start-reverse"><path class="arrowhead" d="M0,0 L10,5 L0,10 z"/></marker>'
               '<marker id="arri" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
               'orient="auto-start-reverse"><path class="arrowhead inf" d="M0,0 L10,5 L0,10 z"/></marker></defs>')

PANEL_W, PANEL_GAP, ITEM_H = 250, 60, 17


def _abbrev(name: str) -> str:
    """A short label for a core table in the overview chips: its capitals (``DeduplicatedEvents`` -> ``DE``)."""
    caps = "".join(ch for ch in name if ch.isupper() or ch.isdigit())
    return caps or name[:3]


def overview_svg(d: Dictionary, core_group: str) -> str:
    """Every area as a panel listing its tables; the core area in the middle, the others around it.

    Each table carries chips for the core tables it links to; a line between two areas is labelled
    with the number of links between their tables.
    """
    groups = [g for g, _ in d.groups]
    if not groups:
        return ""
    members = {g: sorted((t for t in d.tables if t.group == g), key=lambda t: t.name.lower()) for g in groups}
    group_of = {t.name: t.group for t in d.tables}
    core_tables = {t.name for t in members.get(core_group, [])}
    outer = [g for g in groups if g != core_group]
    heights = {g: 34 + ITEM_H * max(1, len(members[g])) + 8 for g in groups}
    # 3 x 3 grid with the core area in the middle; areas past eight go in rows underneath
    slots = [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (1, 2), (0, 2), (0, 1)]
    place: Dict[str, Tuple[int, int]] = {}
    if core_group in members:
        place[core_group] = (1, 1)
    extra_row = 3
    for k, g in enumerate(outer):
        if k < len(slots):
            place[g] = slots[k]
        else:
            place[g] = ((k - len(slots)) % 3, extra_row + (k - len(slots)) // 3)
    nrows = max(r for _, r in place.values()) + 1
    row_h = [max([heights[g] for g, (_, r) in place.items() if r == row] or [0]) for row in range(nrows)]
    row_y = [PAD + sum(row_h[:r]) + PANEL_GAP * r for r in range(nrows)]
    rect = {g: (PAD + c * (PANEL_W + PANEL_GAP), row_y[r] + (row_h[r] - heights[g]) / 2, PANEL_W, heights[g])
            for g, (c, r) in place.items()}
    width = PAD * 2 + 3 * PANEL_W + 2 * PANEL_GAP
    height = row_y[-1] + row_h[-1] + PAD
    between: Dict[Tuple[str, str], List[Link]] = {}
    for link in d.links:
        a, b = group_of.get(link.table), group_of.get(link.target)
        if a and b and a != b:
            between.setdefault(tuple(sorted((a, b))), []).append(link)
    parts = [f'<svg class="erd" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" role="img" '
             f'aria-label="Every table, grouped by area" xmlns="http://www.w3.org/2000/svg">']
    for (a, b), group in sorted(between.items()):
        if a not in rect or b not in rect:
            continue
        (x1, y1), (x2, y2) = _centre_line(rect[a], rect[b])
        declared = any(l.declared for l in group)
        title = "; ".join(sorted({f"{l.table}.{l.column} → {l.target}" for l in group}))
        parts.append(f'<g><line class="gedge {"decl" if declared else "infer"}" x1="{x1:.0f}" y1="{y1:.0f}" '
                     f'x2="{x2:.0f}" y2="{y2:.0f}"><title>{_a(title)}</title></line>'
                     f'<text class="gcount" x="{(x1 + x2) / 2:.0f}" y="{(y1 + y2) / 2 - 4:.0f}" '
                     f'text-anchor="middle">{len(group)}</text></g>')
    for g in groups:
        x, y, w, h = rect[g]
        cls = "panel core" if g == core_group else "panel"
        parts.append(f'<g class="{cls}"><rect x="{x:.0f}" y="{y:.0f}" width="{w}" height="{h:.0f}" rx="8"/>'
                     f'<a href="#g-{anchor(g)[2:]}"><text class="gname" x="{x + 12:.0f}" y="{y + 22:.0f}">'
                     f'{_a(g)} ({len(members[g])})</text></a>')
        for k, t in enumerate(members[g]):
            ty = y + 34 + 12 + k * ITEM_H
            parts.append(f'<a href="#{anchor(t.name)}"><text class="item{" view" if t.kind == "view" else ""}" '
                         f'x="{x + 12:.0f}" y="{ty:.0f}">{_a(_fit(t.name, 30))}<title>{_a(t.name)}</title></text></a>')
            targets = sorted({l.target for l in d.links if l.table == t.name and l.target in core_tables
                              and l.target != t.name})
            chips = " ".join(_abbrev(n) for n in targets)
            if chips and g != core_group:
                parts.append(f'<text class="chips" x="{x + w - 10:.0f}" y="{ty:.0f}" text-anchor="end">'
                             f'{_a(_fit(chips, 14))}<title>links to {_a(", ".join(targets))}</title></text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def _centre_line(a, b):
    """The segment joining two rectangles' centres, clipped to their borders."""
    ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
    bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2

    def clip(cx, cy, w, h, dx, dy):
        if dx == 0 and dy == 0:
            return cx, cy
        sx = (w / 2) / abs(dx) if dx else math.inf
        sy = (h / 2) / abs(dy) if dy else math.inf
        s = min(sx, sy)
        return cx + dx * s, cy + dy * s

    return clip(ax, ay, a[2], a[3], bx - ax, by - ay), clip(bx, by, b[2], b[3], ax - bx, ay - by)


# --- page ------------------------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data dictionary</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --ink: #16181d; --muted: #667;
           --line: #dfe3e8; --head: #e8edf4; --accent: #2b5fae; --infer: #a05a12; --core: #eef4fc;
           --warn: #8a5312; --warnbg: #fff3e0; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #15171b; --card: #1d2026; --ink: #e6e8ec; --muted: #9aa1ab; --line: #2e333b;
             --head: #263040; --accent: #8fb4ec; --infer: #f0b46a; --core: #1c2633;
             --warn: #f0c27a; --warnbg: #3a2c16; }} }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px 16px;
         background: var(--bg); color: var(--ink); }}
  main {{ max-width: 1150px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 28px 0 8px; }}
  h3 {{ font-size: 16px; margin: 0 0 4px; word-break: break-word; }}
  a {{ color: var(--accent); }}
  .sub, .m {{ color: var(--muted); font-size: 13px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 14px;
           padding: 12px 16px; }}
  .diagram {{ overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 8px;
              padding: 6px; margin-bottom: 10px; }}
  .diagram svg {{ display: block; max-width: 100%; height: auto; }}
  @media (max-width: 760px) {{ .diagram svg {{ max-width: none; }} }}
  .legend {{ font-size: 13px; color: var(--muted); margin: 4px 0 14px; }}
  .legend svg {{ vertical-align: middle; }}
  svg.erd text {{ font: 12px -apple-system, "Segoe UI", Roboto, sans-serif; fill: var(--ink); }}
  svg.erd .box rect {{ fill: var(--card); stroke: var(--line); stroke-width: 1.2; }}
  svg.erd .box rect.head {{ fill: var(--head); }}
  svg.erd .tname {{ font-weight: 700; font-size: 13px; }}
  svg.erd .col {{ font-size: 11.5px; fill: var(--muted); }}
  svg.erd .col.fk {{ fill: var(--accent); }}
  svg.erd .col.inf {{ fill: var(--infer); }}
  svg.erd .edge, svg.erd .gedge {{ stroke: var(--accent); stroke-width: 1.6; fill: none; }}
  svg.erd .infer {{ stroke: var(--infer); stroke-dasharray: 6 4; }}
  svg.erd .gedge {{ stroke-width: 1.3; opacity: .75; }}
  svg.erd .arrowhead {{ fill: var(--accent); }}
  svg.erd .arrowhead.inf {{ fill: var(--infer); }}
  svg.erd .panel rect {{ fill: var(--card); stroke: var(--line); stroke-width: 1.2; }}
  svg.erd .panel.core rect {{ fill: var(--core); stroke: var(--accent); }}
  svg.erd .gname {{ font-weight: 700; font-size: 13px; fill: var(--ink); }}
  svg.erd .gcount {{ font-size: 11px; fill: var(--muted); paint-order: stroke; stroke: var(--bg); stroke-width: 3; }}
  svg.erd .item {{ font-size: 12px; fill: var(--accent); }}
  svg.erd .item.view {{ font-style: italic; }}
  svg.erd .chips {{ font-size: 10.5px; fill: var(--muted); }}
  svg.erd a:hover text {{ text-decoration: underline; }}
  .toc {{ columns: 3 260px; column-gap: 24px; font-size: 14px; }}
  .toc div {{ break-inside: avoid; margin-bottom: 12px; }}
  .toc b {{ display: block; }}
  .toc a {{ display: block; text-decoration: none; word-break: break-word; }}
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
            color: var(--muted); border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; margin-left: 6px; }}
  .links {{ font-size: 13px; margin: 6px 0 8px; }}
  .links span.inf {{ color: var(--infer); }}
  .tw {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
  th, td {{ text-align: left; vertical-align: top; padding: 5px 8px; border-top: 1px solid var(--line); }}
  th {{ font-size: 12px; color: var(--muted); border-top: none; }}
  td.c {{ white-space: nowrap; }}
  td.c code {{ font-weight: 600; }}
  td.k {{ white-space: nowrap; font-size: 12px; color: var(--muted); }}
  td.d {{ min-width: 260px; }}
  .none {{ color: var(--warn); font-style: italic; }}
  code {{ font-size: 12.5px; word-break: break-word; }}
  @media (max-width: 760px) {{ td.d {{ min-width: 200px; }} td.c {{ white-space: normal; word-break: break-all; }} }}
</style>
</head>
<body>
<main>
<h1>Data dictionary</h1>
<div class="sub">{intro}</div>
<h2 id="relationships">How the tables relate</h2>
<div class="legend">{legend}</div>
<h3>Core pipeline tables</h3>
<div class="m">Each record moves from left to right: a discovered page (RawEvents), its enrichment (EnrichedEvents),
the incident it is part of (DeduplicatedEvents). Boxes list the primary key and the links to other core tables.</div>
<div class="diagram">{core}</div>
<h3>Every table, by area</h3>
<div class="m">Lines join areas whose tables link to each other, labelled with the number of links. The letters after
a table name are the core tables it links to ({chip_key}). Views are in italics. Click a name for its columns.</div>
<div class="diagram">{overview}</div>
<h2 id="contents">Contents</h2>
<div class="card toc">{toc}</div>
{sections}
</main>
</body>
</html>
"""


def _legend() -> str:
    solid = ('<svg width="44" height="10"><line x1="2" y1="5" x2="42" y2="5" stroke="currentColor" '
             'stroke-width="2" style="stroke: var(--accent)"/></svg>')
    dashed = ('<svg width="44" height="10"><line x1="2" y1="5" x2="42" y2="5" stroke-width="2" '
              'stroke-dasharray="6 4" style="stroke: var(--infer)"/></svg>')
    return (f"{solid} declared foreign key (→) &nbsp; {dashed} link inferred from the column name, not declared "
            "in SQLite (⇢)")


def _key_flags(c: Column) -> str:
    flags = []
    if c.pk:
        flags.append("PK" if c.pk == 1 else f"PK{c.pk}")
    if c.notnull:
        flags.append("NOT NULL")
    if c.default is not None:
        flags.append(f"default {c.default}")
    return "<br>".join(html.escape(f) for f in flags)


def _section(t: Table, d: Dictionary) -> str:
    out_links = [l for l in d.links if l.table == t.name]
    in_links = [l for l in d.links if l.target == t.name and l.table != t.name]
    link_html = []
    if out_links:
        items = [f'<span class="{"" if l.declared else "inf"}"><code>{_a(l.column)}</code> '
                 f'{ARROW if l.declared else DASHED_ARROW} <a href="#{anchor(l.target)}">{_a(l.target)}</a>'
                 f'{"" if l.declared else " (inferred)"}</span>' for l in out_links]
        link_html.append("Links to: " + "; ".join(items))
    if in_links:
        refs = sorted({(l.table, l.declared) for l in in_links})
        items = [f'<a href="#{anchor(name)}">{_a(name)}</a>{"" if declared else "*"}' for name, declared in refs]
        link_html.append("Referenced by: " + ", ".join(items)
                         + (" (* inferred)" if any(not dec for _, dec in refs) else ""))
    rows = []
    for c in t.columns:
        desc = d.descriptions.get((t.name, c.name))
        desc_html = md_inline(desc) if desc else '<span class="none">No description yet.</span>'
        rows.append(f'<tr><td class="c"><code>{_a(c.name)}</code></td><td class="k">{_a(c.type or "(none declared)")}'
                    f'</td><td class="k">{_key_flags(c)}</td><td class="d">{desc_html}</td></tr>')
    purpose = md_inline(t.purpose) if t.purpose else '<span class="none">No description yet.</span>'
    badge = f'<span class="badge">{t.kind}</span>'
    return (f'<section class="card" id="{anchor(t.name)}"><h3>{_a(t.name)}{badge}</h3>'
            f'<div class="m">{_a(t.group)} &middot; {len(t.columns)} column(s) &middot; '
            f'<a href="#contents">contents</a></div><p>{purpose}</p>'
            + "".join(f'<div class="links">{x}</div>' for x in link_html) +
            '<div class="tw"><table><thead><tr><th>Column</th><th>Type</th><th>Key / null</th>'
            f'<th>Description</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')


def build_html(d: Dictionary, generated: str, core_group: Optional[str] = None) -> str:
    """The whole page."""
    core_group = core_group or (d.groups[0][0] if d.groups else OTHER_GROUP)
    declared = sum(1 for l in d.links if l.declared)
    inferred = len(d.links) - declared
    views = sum(1 for t in d.tables if t.kind == "view")
    described = d.column_count - len(d.undescribed)
    intro = (f"{len(d.tables) - views} tables and {views} views, {d.column_count} columns ({described} described), "
             f"{declared} declared foreign keys and {inferred} links inferred from column names. Read from the "
             f"database schema; descriptions from docs/data-dictionary.md and docs/data_dictionary_descriptions.json. "
             f"Generated {html.escape(generated)}.")
    core_tables = sorted(t.name for t in d.tables if t.group == core_group)
    chip_key = ", ".join(f"{_abbrev(n)} = {n}" for n in core_tables) or "none"
    toc_parts, sections = [], []
    for g, gdesc in d.groups:
        members = sorted((t for t in d.tables if t.group == g), key=lambda t: t.name.lower())
        toc_parts.append(f'<div><b id="g-{anchor(g)[2:]}">{_a(g)}</b>'
                         + (f'<span class="m">{md_inline(gdesc)}</span>' if gdesc else "")
                         + "".join(f'<a href="#{anchor(t.name)}">{_a(t.name)}</a>' for t in members) + "</div>")
        sections.append(f'<h2>{_a(g)}</h2>' + "".join(_section(t, d) for t in members))
    return PAGE.format(intro=intro, legend=_legend(), core=core_svg(d), overview=overview_svg(d, core_group),
                       chip_key=_a(chip_key), toc="".join(toc_parts), sections="\n".join(sections))


def connect_read_only(db: Path) -> sqlite3.Connection:
    """Open ``db`` read-only: the dictionary never writes to the database it documents."""
    path = quote(Path(db).resolve().as_posix(), safe="/:")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="instance/cyber_events.db")
    ap.add_argument("--out", default="dashboard/data_dictionary.html")
    ap.add_argument("--markdown", default=str(DEFAULT_MARKDOWN))
    ap.add_argument("--descriptions", default=str(DEFAULT_SUPPLEMENT))
    ap.add_argument("--list-undescribed", action="store_true", help="print every column still without a description")
    args = ap.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    conn = connect_read_only(Path(args.db))
    try:
        d = collect(conn, Path(args.markdown), Path(args.descriptions))
    finally:
        conn.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(d, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")), encoding="utf-8")
    print(f"{len(d.tables)} tables/views, {d.column_count} columns, {len(d.links)} links -> {out}")
    print(f"{len(d.undescribed)} column(s) without a description; {len(d.no_purpose)} table(s) without a purpose")
    if args.list_undescribed:
        for table, column in d.undescribed:
            print(f"  {table}.{column}")
        for table in d.no_purpose:
            print(f"  {table} (purpose)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
