#!/usr/bin/env python3
"""Train, evaluate and version the Random Forest discovery filter (RETIRED).

The filter is retired: discovery uses the Jev page gate
(``cyber_data_collector/filtering/page_gate.py``). This script is kept so the
evaluation that retired it can be reproduced.

Every model is written as a new immutable bundle (see
``cyber_data_collector/filtering/rf_bundle.py``) with its provenance, file
hashes, organisation-separated evaluation and acceptance verdict. Only an
accepted bundle drops records; any other runs report-only.

Labelled data: the 2025-09 hand labels (``machine_learning_filter/
event_training_data.csv``), ``evaluation/source_gate_labels.csv`` and
``evaluation/event_reality_labels.csv``; raw text comes from the database
(read-only).

Usage (from the project root):
    python scripts/train_rf_filter.py                      # train, evaluate, write a bundle
    python scripts/train_rf_filter.py --activate           # ... and make it the active bundle
    python scripts/train_rf_filter.py --adopt-legacy       # wrap the 2025-09 model as a bundle, evaluated
    python scripts/train_rf_filter.py --evaluate <bundle>  # re-evaluate a bundle (prints only)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.filtering import rf_bundle as rb  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402

LEGACY_ID = "rf-legacy-2025-09"
LEGACY_FILES = {"model": "random_forest_filter.pkl", "vectorizer": "text_vectorizer.pkl",
                "encoder": "source_type_encoder.pkl"}


def _connect(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True, timeout=30)


def _print(title: str, m: dict) -> None:
    def pct(v):
        return "-" if v is None else f"{v:.3f}"
    print(f"{title}: {m['records']} records ({m['real']} real, {m['not_incident']} not), "
          f"dropped {m['dropped']}, drop precision {pct(m['drop_precision'])}, "
          f"real loss {pct(m['real_loss'])}, junk caught {pct(m['junk_caught'])}")
    for origin, sub in (m.get("by_origin") or {}).items():
        _print(f"    {origin}", sub)


def train(args, frame) -> int:
    params = json.loads(Path(args.params).read_text()) if args.params else rb.DEFAULT_PARAMS
    digest = rb.data_hash(frame)
    bundle_id = args.bundle_id or f"rf-{datetime.now():%Y%m%d}-{digest[:8]}"
    print(f"Training on {len(frame)} labelled records in {frame['group'].nunique()} organisation groups")
    cv = rb.grouped_cv(frame, params, folds=args.folds)
    _print(f"Organisation-separated {cv['folds']}-fold evaluation", cv)
    is_accepted = rb.accepted(cv)
    objects = rb.fit(frame, params)
    manifest = {
        "kind": "trained", "params": params, "data_sha256": digest,
        "training_records": len(frame), "label_counts": frame["label"].value_counts().to_dict(),
        "sources": sorted(frame["origin"].unique()), "evaluation": {"grouped_cv": cv},
        "acceptance": rb.ACCEPTANCE, "accepted": is_accepted,
    }
    directory = rb.write_bundle(bundle_id, objects, manifest, frame.sample(min(25, len(frame)), random_state=0))
    frame[["raw_event_id", "label", "origin", "group"]].to_csv(directory / "training_records.csv", index=False)
    print(f"Bundle written: {directory} - {'ACCEPTED, will drop records' if is_accepted else 'NOT accepted, report-only'}")
    if args.activate:
        rb.ACTIVE_FILE.write_text(bundle_id + "\n", encoding="utf-8")
        print(f"Active bundle: {bundle_id}")
    return 0


def adopt_legacy(args, frame) -> int:
    """Wrap the 2025-09 model, unchanged, as a bundle - evaluated on records it never saw."""
    legacy = {key: rb.MODEL_DIR / name for key, name in LEGACY_FILES.items()}
    if not all(p.exists() for p in legacy.values()):
        print("Legacy model files not found; nothing to adopt")
        return 1
    import pickle
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        objects = tuple(pickle.loads(legacy[k].read_bytes()) for k in ("model", "vectorizer", "encoder"))
    trained = frame[frame["origin"] == "training_csv"]
    held_out = frame[(frame["origin"] != "training_csv") & ~frame["group"].isin(set(trained["group"]))]
    evaluation = rb.evaluate_objects(*objects, held_out.reset_index(drop=True))
    _print("Held-out (organisations absent from its training data)", evaluation)
    manifest = {
        "kind": "legacy", "params": {"tfidf": objects[1].get_params(), "forest": objects[0].get_params()},
        "provenance": ("2025-09 model trained by the deleted train_rf_filter.py on event_training_data.xlsx; "
                       "scikit-learn version at training unknown. Files copied unchanged."),
        "versions": {"scikit_learn": "unknown"},
        "evaluation": {"held_out": evaluation}, "acceptance": rb.ACCEPTANCE,
        "accepted": rb.accepted(evaluation),
    }
    directory = rb.write_bundle(LEGACY_ID, objects, manifest, held_out.head(25))
    print(f"Bundle written: {directory} - {'ACCEPTED' if manifest['accepted'] else 'NOT accepted, report-only'}")
    return 0


class TrainingMembershipUnknown(RuntimeError):
    """A bundle's training records cannot be identified, so no record can be shown to be held out."""


def held_out_records(bundle_id: str, manifest: dict, frame, bundles_dir: Path = None):
    """The labelled records a bundle never saw, by organisation.

    Training membership comes from the bundle's ``training_records.csv``; for
    the adopted 2025-09 bundle (``kind: legacy``) it is the 2025-09 training
    file's records (``origin == 'training_csv'``), as when it was adopted.
    Without either, membership is unknown and evaluation is refused: treating
    every record as held out would score the model on data it was trained on.
    """
    import pandas as pd

    records = (bundles_dir or rb.BUNDLES_DIR) / bundle_id / "training_records.csv"
    if records.exists():
        trained = pd.read_csv(records)
    elif manifest.get("kind") == "legacy":
        trained = frame[frame["origin"] == "training_csv"]
    else:
        raise TrainingMembershipUnknown(
            f"{records} is missing and bundle {bundle_id} is not the adopted legacy model: its training "
            "records are unknown, so a held-out evaluation cannot be made")
    held = frame[~frame["group"].isin(set(trained["group"])) & ~frame["raw_event_id"].isin(set(trained["raw_event_id"]))]
    return held.reset_index(drop=True)


def evaluate(args, frame) -> int:
    bundle = rb.load_bundle(args.evaluate)
    try:
        frame = held_out_records(args.evaluate, bundle.manifest, frame)
    except TrainingMembershipUnknown as exc:
        print(f"REFUSED: {exc}")
        return 1
    _print(f"{args.evaluate} on {len(frame)} records from organisations it never saw",
           rb.evaluate_objects(bundle.model, bundle.vectorizer, bundle.encoder, frame))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="database holding the labelled records' raw text (read-only)")
    parser.add_argument("--evaluation-dir", default="evaluation")
    parser.add_argument("--training-csv", default=str(rb.MODEL_DIR / "event_training_data.csv"))
    parser.add_argument("--params", default=None, help="JSON file of tfidf/forest parameters")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--bundle-id", default=None)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--adopt-legacy", action="store_true")
    parser.add_argument("--evaluate", default=None, metavar="BUNDLE_ID")
    args = parser.parse_args(argv)

    conn = _connect(resolve_db_path(args.db))
    try:
        frame = rb.load_labelled_records(conn, Path(args.evaluation_dir), Path(args.training_csv))
    finally:
        conn.close()
    if args.adopt_legacy:
        return adopt_legacy(args, frame)
    if args.evaluate:
        return evaluate(args, frame)
    return train(args, frame)


if __name__ == "__main__":
    raise SystemExit(main())
