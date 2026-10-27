"""Versioned, evaluated model bundles for the Random Forest discovery filter.

A bundle is an immutable directory under ``machine_learning_filter/bundles/``:

    model.pkl, vectorizer.pkl, encoder.pkl   the fitted objects
    manifest.json                            provenance, file hashes, metrics, acceptance
    reference.json                           inputs and predicted probabilities, to prove a
                                             reload predicts what was trained

``machine_learning_filter/ACTIVE_BUNDLE`` names the bundle discovery uses. A
bundle *enforces* (drops records) only if its manifest says it passed
acceptance; otherwise the filter runs report-only. Files are never rewritten:
the old loader re-pickled the model whenever scikit-learn's version changed,
which silenced the warning without showing the predictions were unchanged.

Evaluation is **organisation-separated**: out-of-fold predictions from
``GroupKFold`` over the victim organisation of each record (its event's victim;
the source domain where no event links it), so no organisation is scored by a
model that saw it in training.

Acceptance is about what the filter is for - dropping junk before enrichment
without losing incidents:

- precision of drops (dropped records that are truly not incidents) >= 0.90;
- loss of real incidents (real records dropped) <= 0.02.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import platform
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import numpy as np
import pandas as pd

FEATURE_VERSION = "rf-features-v1"   # bump when preprocess_* or feature assembly changes
MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "machine_learning_filter"
BUNDLES_DIR = MODEL_DIR / "bundles"
ACTIVE_FILE = MODEL_DIR / "ACTIVE_BUNDLE"
FILES = {"model": "model.pkl", "vectorizer": "vectorizer.pkl", "encoder": "encoder.pkl"}

ACCEPTANCE = {"min_drop_precision": 0.90, "max_real_loss": 0.02}

# The parameters of the 2025-09 model (recovered from its pickles), kept as the default.
DEFAULT_PARAMS: Dict[str, Any] = {
    "tfidf": {"max_features": 1000, "stop_words": "english", "ngram_range": [1, 2], "min_df": 2, "max_df": 0.95},
    "forest": {"n_estimators": 100, "max_depth": 5, "min_samples_split": 10, "min_samples_leaf": 5,
               "class_weight": "balanced", "random_state": 42, "n_jobs": 1},
}


# ----------------------------------------------------------------------
# Features (shared with RfEventFilter)
# ----------------------------------------------------------------------

def preprocess_url(url: Any) -> str:
    """Drop the protocol and common domain parts; separators become spaces."""
    if url is None or (isinstance(url, float) and np.isnan(url)) or not url:
        return ""
    url = re.sub(r'^https?://', '', str(url))
    url = re.sub(r'[-_]', ' ', url)
    url = re.sub(r'\.(com|org|net|edu|gov|au|uk|us)/?', ' ', url)
    url = re.sub(r'www\.', '', url)
    return re.sub(r'\s+', ' ', url).strip()


def preprocess_text(text: Any) -> str:
    if text is None or (isinstance(text, float) and np.isnan(text)) or not text:
        return ""
    return re.sub(r'\s+', ' ', str(text).lower()).strip()


def combined_text(title: Any, description: Any, content: Any, url: Any) -> str:
    return " ".join(preprocess_text(part) for part in (title, description, content, preprocess_url(url)))


def feature_matrix(vectorizer: Any, encoder: Any, frame: pd.DataFrame) -> np.ndarray:
    """[encoded source type] + TF-IDF of the combined text, as the 2025-09 model was trained."""
    known = set(encoder.classes_)
    codes = np.array([encoder.transform([s])[0] if s in known else 0 for s in frame["source_type"]])
    text = [combined_text(r.raw_title, r.raw_description, r.raw_content, r.source_url)
            for r in frame.itertuples()]
    return np.hstack([codes.reshape(-1, 1), vectorizer.transform(text).toarray()])


# ----------------------------------------------------------------------
# Labelled data
# ----------------------------------------------------------------------

RAW_COLUMNS = ["raw_event_id", "source_type", "raw_title", "raw_description", "raw_content", "source_url"]


def _group_for(conn: sqlite3.Connection, raw_event_id: str, url: Any) -> str:
    """The victim organisation of the record's event, else its source domain."""
    row = conn.execute("""
        SELECT d.victim_organization_name FROM EventDeduplicationMap m
        JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
        WHERE m.raw_event_id = ? AND d.victim_organization_name IS NOT NULL
        ORDER BY (d.status = 'Active') DESC LIMIT 1
    """, (raw_event_id,)).fetchone()
    if row and row[0] and str(row[0]).strip():
        return "org:" + re.sub(r"[^a-z0-9]+", " ", str(row[0]).lower()).strip()
    host = urlparse(str(url or "")).netloc.lower().removeprefix("www.")
    return "domain:" + (host or raw_event_id)


def _raw(conn: sqlite3.Connection, where: str, value: str) -> List[Tuple]:
    return conn.execute(f"SELECT {', '.join('r.' + c for c in RAW_COLUMNS)} FROM RawEvents r {where}",
                        (value,)).fetchall()


def load_labelled_records(conn: sqlite3.Connection, evaluation_dir: Path,
                          training_csv: Optional[Path] = None) -> pd.DataFrame:
    """Every labelled raw record, with ``label`` (1 = an incident, keep), ``origin`` and ``group``.

    - ``training_csv``: the 2025-09 hand labels (``filter_keep``);
    - ``source_gate_labels.csv``: Google pages, ``reports_incident`` = 1;
    - ``event_reality_labels.csv``: each labelled event's member records,
      ``real`` = 1. A real event's members are treated as incidents: they were
      merged into it as coverage of it.
    ``unsure`` labels are left out. A record labelled twice keeps its first label.
    """
    rows: List[Dict[str, Any]] = []
    if training_csv and training_csv.exists():
        df = pd.read_csv(training_csv)
        for r in df[df["filter_keep"].notna()].itertuples():
            rows.append({**{c: getattr(r, c) for c in RAW_COLUMNS}, "label": int(r.filter_keep),
                         "origin": "training_csv"})
    gate = evaluation_dir / "source_gate_labels.csv"
    if gate.exists():
        for r in pd.read_csv(gate).itertuples():
            if r.label in ("reports_incident", "not_incident"):
                for raw in _raw(conn, "JOIN EnrichedEvents e ON e.raw_event_id = r.raw_event_id "
                                      "WHERE e.enriched_event_id = ?", r.enriched_event_id):
                    rows.append({**dict(zip(RAW_COLUMNS, raw)), "label": int(r.label == "reports_incident"),
                                 "origin": "source_gate"})
    reality = evaluation_dir / "event_reality_labels.csv"
    if reality.exists():
        for r in pd.read_csv(reality).itertuples():
            if r.label in ("real", "not_incident"):
                for raw in _raw(conn, "JOIN EventDeduplicationMap m ON m.raw_event_id = r.raw_event_id "
                                      "WHERE m.deduplicated_event_id = ?", r.deduplicated_event_id):
                    rows.append({**dict(zip(RAW_COLUMNS, raw)), "label": int(r.label == "real"),
                                 "origin": "event_reality"})
    frame = pd.DataFrame(rows, columns=RAW_COLUMNS + ["label", "origin"])
    frame = frame.drop_duplicates("raw_event_id", keep="first").reset_index(drop=True)
    frame["group"] = [_group_for(conn, r.raw_event_id, r.source_url) for r in frame.itertuples()]
    for column in ("raw_title", "raw_description", "raw_content", "source_url", "source_type"):
        frame[column] = frame[column].fillna("")
    return frame


def data_hash(frame: pd.DataFrame) -> str:
    """Order-independent hash of the labelled data a bundle was trained on."""
    keyed = frame.sort_values("raw_event_id")[RAW_COLUMNS + ["label"]]
    return hashlib.sha256(keyed.to_csv(index=False).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Training and evaluation
# ----------------------------------------------------------------------

def fit(frame: pd.DataFrame, params: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import LabelEncoder

    tfidf = dict(params["tfidf"], ngram_range=tuple(params["tfidf"]["ngram_range"]))
    vectorizer = TfidfVectorizer(**tfidf)
    vectorizer.fit([combined_text(r.raw_title, r.raw_description, r.raw_content, r.source_url)
                    for r in frame.itertuples()])
    encoder = LabelEncoder().fit(sorted(set(frame["source_type"]) | {"unknown"}))
    model = RandomForestClassifier(**params["forest"])
    model.fit(feature_matrix(vectorizer, encoder, frame), frame["label"].to_numpy())
    return model, vectorizer, encoder


def predict_keep_probability(model: Any, vectorizer: Any, encoder: Any, frame: pd.DataFrame) -> np.ndarray:
    proba = model.predict_proba(feature_matrix(vectorizer, encoder, frame))
    classes = list(model.classes_)
    return proba[:, classes.index(1)] if 1 in classes else np.zeros(len(frame))


def metrics(labels: Iterable[int], keep: Iterable[bool]) -> Dict[str, Any]:
    labels = np.asarray(list(labels)).astype(int)
    keep = np.asarray(list(keep)).astype(bool)
    dropped = ~keep
    real = labels == 1
    drops = int(dropped.sum())
    return {
        "records": int(len(labels)), "real": int(real.sum()), "not_incident": int((~real).sum()),
        "dropped": drops,
        "drop_precision": (float((dropped & ~real).sum() / drops) if drops else None),
        "real_loss": float((dropped & real).sum() / real.sum()) if real.sum() else None,
        "junk_caught": float((dropped & ~real).sum() / (~real).sum()) if (~real).sum() else None,
    }


def accepted(m: Dict[str, Any]) -> bool:
    """Passes acceptance. A filter that drops nothing passes vacuously - and is pointless, so it must drop."""
    return (m["dropped"] > 0 and m["drop_precision"] is not None and m["real_loss"] is not None
            and m["drop_precision"] >= ACCEPTANCE["min_drop_precision"]
            and m["real_loss"] <= ACCEPTANCE["max_real_loss"])


def grouped_cv(frame: pd.DataFrame, params: Dict[str, Any], folds: int = 5) -> Dict[str, Any]:
    """Organisation-separated out-of-fold evaluation of training with ``params``."""
    from sklearn.model_selection import GroupKFold

    frame = frame.reset_index(drop=True)
    n_folds = min(folds, frame["group"].nunique())
    keep = np.ones(len(frame), dtype=bool)
    for train_idx, test_idx in GroupKFold(n_splits=n_folds).split(frame, groups=frame["group"]):
        model, vectorizer, encoder = fit(frame.iloc[train_idx], params)
        keep[test_idx] = model.predict(feature_matrix(vectorizer, encoder, frame.iloc[test_idx])) == 1
    result = metrics(frame["label"], keep)
    result["folds"] = int(n_folds)
    result["by_origin"] = {origin: metrics(frame.loc[idx, "label"], keep[idx])
                           for origin, idx in frame.groupby("origin").groups.items()}
    return result


def evaluate_objects(model: Any, vectorizer: Any, encoder: Any, frame: pd.DataFrame) -> Dict[str, Any]:
    """Evaluation of fitted objects on records they were not trained on."""
    keep = model.predict(feature_matrix(vectorizer, encoder, frame)) == 1
    result = metrics(frame["label"], keep)
    result["by_origin"] = {origin: metrics(frame.loc[idx, "label"], keep[frame.index.get_indexer(idx)])
                           for origin, idx in frame.groupby("origin").groups.items()}
    return result


# ----------------------------------------------------------------------
# Bundles
# ----------------------------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _versions() -> Dict[str, str]:
    import sklearn
    return {"python": platform.python_version(), "scikit_learn": sklearn.__version__,
            "numpy": np.__version__, "pandas": pd.__version__}


def write_bundle(bundle_id: str, objects: Tuple[Any, Any, Any], manifest: Dict[str, Any],
                 reference_frame: pd.DataFrame, root: Path = BUNDLES_DIR) -> Path:
    """Write a new bundle. Refuses to overwrite an existing one."""
    directory = root / bundle_id
    if directory.exists():
        raise FileExistsError(f"bundle {bundle_id} exists; bundles are immutable")
    directory.mkdir(parents=True)
    for (key, filename), obj in zip(FILES.items(), objects):
        with open(directory / filename, "wb") as handle:
            pickle.dump(obj, handle)
    model, vectorizer, encoder = objects
    reference = reference_frame[["source_type", "raw_title", "raw_description", "raw_content", "source_url"]]
    probabilities = predict_keep_probability(model, vectorizer, encoder, reference_frame)
    (directory / "reference.json").write_text(json.dumps({
        "inputs": reference.to_dict(orient="records"),
        "keep_probability": [float(p) for p in probabilities],
    }, indent=1, default=str), encoding="utf-8")
    manifest = dict(manifest, bundle_id=bundle_id, feature_version=FEATURE_VERSION,
                    created_at=manifest.get("created_at") or datetime.now().isoformat(timespec="seconds"),
                    versions=manifest.get("versions") or _versions(),
                    files={key: {"name": filename, "sha256": _sha256(directory / filename)}
                           for key, filename in FILES.items()},
                    reference_sha256=_sha256(directory / "reference.json"))
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return directory


@dataclass
class LoadedBundle:
    bundle_id: str
    model: Any
    vectorizer: Any
    encoder: Any
    manifest: Dict[str, Any]

    @property
    def enforcing(self) -> bool:
        return bool(self.manifest.get("accepted"))


class BundleError(RuntimeError):
    """A bundle that cannot be trusted: tampered, incomplete or not reproducing its predictions."""


def active_bundle_id(model_dir: Path = MODEL_DIR) -> Optional[str]:
    path = model_dir / "ACTIVE_BUNDLE"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def load_bundle(bundle_id: str, root: Path = BUNDLES_DIR) -> LoadedBundle:
    """Load a bundle, verifying file hashes and - on any library version change -
    that it still predicts its reference probabilities. Never rewrites anything."""
    import warnings

    directory = root / bundle_id
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise BundleError(f"bundle {bundle_id}: no manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key, entry in manifest["files"].items():
        if _sha256(directory / entry["name"]) != entry["sha256"]:
            raise BundleError(f"bundle {bundle_id}: {entry['name']} does not match its manifest hash")
    if _sha256(directory / "reference.json") != manifest.get("reference_sha256"):
        raise BundleError(f"bundle {bundle_id}: reference.json does not match its manifest hash")
    if manifest.get("feature_version") != FEATURE_VERSION:
        raise BundleError(f"bundle {bundle_id}: built for features {manifest.get('feature_version')}, "
                          f"code is {FEATURE_VERSION}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        objects = []
        for key in ("model", "vectorizer", "encoder"):
            with open(directory / manifest["files"][key]["name"], "rb") as handle:
                objects.append(pickle.load(handle))
    model, vectorizer, encoder = objects

    version_changed = (manifest.get("versions", {}).get("scikit_learn") != _versions()["scikit_learn"]
                       or any("InconsistentVersion" in type(w.message).__name__ for w in caught))
    reference = json.loads((directory / "reference.json").read_text(encoding="utf-8"))
    frame = pd.DataFrame(reference["inputs"])
    if len(frame):
        got = predict_keep_probability(model, vectorizer, encoder, frame)
        if not np.allclose(got, reference["keep_probability"], atol=1e-9):
            raise BundleError(
                f"bundle {bundle_id}: predictions differ from its reference "
                f"(trained with scikit-learn {manifest.get('versions', {}).get('scikit_learn')}, "
                f"running {_versions()['scikit_learn']}); retrain it with scripts/train_rf_filter.py")
    elif version_changed:
        raise BundleError(f"bundle {bundle_id}: library versions changed and it has no reference to verify")
    return LoadedBundle(bundle_id, model, vectorizer, encoder, manifest)
