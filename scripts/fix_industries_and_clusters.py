"""One-off: re-apply industry corrections to BOTH EnrichedEvents JSON AND
DeduplicatedEvents, plus force-merge residual same-incident clusters that
the post-force-dedup pass left as separate Active records.

Idempotent - safe to re-run.
"""
import json
import sqlite3

DB_PATH = "instance/cyber_events.db"

# substring matched against EITHER DeduplicatedEvents.victim_organization_name
# OR EnrichedEvents JSON.formal_entity_name (case-insensitive)
CORRECTIONS = [
    ("Frontier Software",                                  "Technology"),
    ("Datatime Services",                                  "Technology"),
    ("OracleCMS",                                          "Technology"),
    ("Dialog Pty Ltd",                                     "Technology"),
    ("IDMatch",                                            "Technology"),
    ("HWL Ebsworth",                                       "Legal Services"),
    ("State of Victoria - Department of Health",           "Healthcare"),
    ("National Disability Insurance Agency",               "Healthcare"),
    ("Insurance and Care NSW",                             "Finance"),
    ("Australian Labor Party",                             "Other"),
    ("Copyright Agency Limited",                           "Nonprofit"),
    ("PricewaterhouseCoopers",                             "Legal Services"),
    ("CPA Australia",                                      "Other"),
    ("Nexia Australia",                                    "Legal Services"),
    ("Gibbs Hurley",                                       "Legal Services"),
    ("Tabcorp Holdings",                                   "Entertainment"),
    ("Iress Limited",                                      "Technology"),
    ("Morningstar",                                        "Technology"),
    ("Gold Corporation",                                   "Manufacturing"),
    ("Australian Securities and Investments Commission",   "Government"),
    ("Beyond Bank Australia",                              "Finance"),
    ("Department of Primary Industries and Regional Development", "Government"),
    ("ProctorU",                                           "Technology"),
    ("Proctorio",                                          "Technology"),
    ("The Scout Association",                              "Nonprofit"),
    ("Australian Human Resources Institute",               "Nonprofit"),
    ("National Tertiary Education Union",                  "Nonprofit"),
    ("Thanks For The Help",                                "Technology"),
]


def update_enriched_json(conn):
    print("=== Step 1: EnrichedEvents JSON victim_industry ===")
    total = 0
    for name_substr, target in CORRECTIONS:
        rows = list(conn.execute(
            "SELECT enriched_event_id, perplexity_enrichment_data "
            "FROM EnrichedEvents WHERE perplexity_enrichment_data LIKE ?",
            (f"%{name_substr}%",),
        ))
        n = 0
        for eid, raw in rows:
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            fen = (d.get("formal_entity_name") or "")
            if name_substr.lower() in fen.lower() and d.get("victim_industry") != target:
                d["victim_industry"] = target
                conn.execute(
                    "UPDATE EnrichedEvents "
                    "SET perplexity_enrichment_data = ? "
                    "WHERE enriched_event_id = ?",
                    (json.dumps(d), eid),
                )
                n += 1
        if n:
            print(f"  {name_substr:55s}  -> {target:15s}  {n}")
            total += n
    print(f"Total JSON updated: {total}")
    return total


def update_dedup_events(conn):
    print("\n=== Step 2: DeduplicatedEvents.victim_organization_industry ===")
    total = 0
    for name_substr, target in CORRECTIONS:
        cur = conn.execute(
            "UPDATE DeduplicatedEvents "
            "SET victim_organization_industry = ? "
            "WHERE victim_organization_name LIKE ? "
            "AND (victim_organization_industry != ? OR victim_organization_industry IS NULL)",
            (target, f"%{name_substr}%", target),
        )
        if cur.rowcount:
            print(f"  {name_substr:55s}  -> {target:15s}  {cur.rowcount}")
            total += cur.rowcount
    print(f"Total DeduplicatedEvents updated: {total}")
    return total


def merge_cluster(conn, label, where_clause):
    rows = list(conn.execute(
        f"SELECT deduplicated_event_id, victim_organization_name, event_date, title, "
        f"LENGTH(COALESCE(title, '')) AS tl "
        f"FROM DeduplicatedEvents WHERE {where_clause} AND status = 'Active'"
    ))
    if len(rows) <= 1:
        return 0
    rows.sort(key=lambda r: (r[1] is None, r[2] or "9999", -r[4]))
    rest_ids = [r[0] for r in rows[1:]]
    placeholders = ",".join("?" * len(rest_ids))
    cur = conn.execute(
        f"UPDATE DeduplicatedEvents SET status='Merged' "
        f"WHERE deduplicated_event_id IN ({placeholders})",
        rest_ids,
    )
    canonical_name = rows[0][1] or "<None>"
    print(f"  {label}: kept '{canonical_name}' ({rows[0][2]}); merged {cur.rowcount}")
    return cur.rowcount


def force_merge_clusters(conn):
    print("\n=== Step 3: Force-merge residual clusters ===")
    apostrophe = chr(39)
    clusters = [
        ("HWL Ebsworth (Apr 2023 MOVEit breach)",
         "victim_organization_name LIKE '%HWL Ebsworth%' "
         "AND event_date BETWEEN '2023-03-01' AND '2023-06-30'"),
        ("ProctorU vendor breach (2020)",
         "(victim_organization_name LIKE 'ProctorU%' "
         "OR victim_organization_name LIKE 'Proctorio%') "
         "AND event_date BETWEEN '2020-05-01' AND '2020-08-31'"),
        ("Latitude Financial breach (2023)",
         "victim_organization_name LIKE '%Latitude Financial%' "
         "AND event_date BETWEEN '2023-01-01' AND '2023-06-30'"),
        ("Big Four banks credential leak (2021-01)",
         "(victim_organization_name LIKE 'Australia and New Zealand Banking%' "
         "OR victim_organization_name LIKE 'ANZ%' "
         "OR victim_organization_name LIKE 'Big Four%') "
         "AND event_date BETWEEN '2021-01-01' AND '2021-01-31'"),
        ("Mt Lilydale Mercy College (2023-01)",
         "victim_organization_name LIKE 'Mount Lilydale Mercy College%'"),
        ("NSW Department of Education (2021-07-07)",
         "(victim_organization_name='NSW Department of Education' "
         "OR victim_organization_name='New South Wales Department of Education')"),
        ("National Tertiary Education Union (2022-05)",
         "victim_organization_name LIKE 'National Tertiary Education Union%' "
         "AND event_date BETWEEN '2022-05-01' AND '2022-05-31'"),
        # Apostrophe escaped via concat to avoid SQL string headaches.
        ("Austin" + apostrophe + "s Financial Solutions (2024-12-19)",
         "(victim_organization_name LIKE 'Austin" + apostrophe + apostrophe + "s%' "
         "OR victim_organization_name LIKE 'Austin’s%') "
         "AND event_date='2024-12-19'"),
        ("Frontier Software (2021-11)",
         "victim_organization_name LIKE 'Frontier Software%' "
         "AND event_date BETWEEN '2021-11-01' AND '2021-11-30'"),
        ("Datatime Services",
         "victim_organization_name LIKE 'Datatime Services%'"),
    ]
    total = 0
    for label, where in clusters:
        total += merge_cluster(conn, label, where)
    print(f"Total residual merges: {total}")
    return total


def report(conn):
    print("\n=== FINAL: 2020-2025 sector distribution (Active) ===")
    for r in conn.execute(
        "SELECT victim_organization_industry, COUNT(*) c "
        "FROM DeduplicatedEvents "
        "WHERE event_date BETWEEN '2020-01-01' AND '2025-12-31' "
        "AND status='Active' "
        "AND victim_organization_industry IS NOT NULL "
        "GROUP BY victim_organization_industry "
        "ORDER BY c DESC LIMIT 12"
    ):
        print(f"  {r[0]:22s} {r[1]}")

    active = conn.execute(
        "SELECT COUNT(*) FROM DeduplicatedEvents WHERE status='Active'"
    ).fetchone()[0]
    merged = conn.execute(
        "SELECT COUNT(*) FROM DeduplicatedEvents WHERE status='Merged'"
    ).fetchone()[0]
    print(f"\nActive: {active}    Merged: {merged}    Total: {active + merged}")


def main():
    conn = sqlite3.connect(DB_PATH)
    update_enriched_json(conn)
    update_dedup_events(conn)
    force_merge_clusters(conn)
    conn.commit()
    report(conn)
    conn.close()


if __name__ == "__main__":
    main()
