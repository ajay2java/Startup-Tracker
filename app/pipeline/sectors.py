"""Sector classification via local sentence embeddings — no LLM, no keywords.

Per spec §4: embed a two-sentence description of each sector once, embed the
company's homepage text, and assign the highest cosine-similarity sector —
unless the top score is weak or the top two are too close, in which case
``Other`` is safer than a confidently wrong label. Runs on ``fastembed``
(ONNX Runtime) rather than ``sentence-transformers``/PyTorch for a much
smaller install; same ``all-MiniLM-L6-v2`` weights either way, so the
vectors and thresholds below behave identically to the spec's intent.

``Other`` is the fallback bucket, not a competing class — only the 11 real
sectors get embedded reference descriptions.
"""
from __future__ import annotations

import numpy as np
from fastembed import TextEmbedding

from ..config import DATA_DIR

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
# Persisted in the project's data dir rather than fastembed's OS-temp default
# — this is a one-time ~90MB download and should survive a temp-dir cleanup.
MODEL_CACHE_DIR = str(DATA_DIR / "fastembed_cache")

SECTOR_DESCRIPTIONS: dict[str, str] = {
    "BioTech": (
        "Biotechnology and life sciences, including drug discovery, "
        "diagnostics, genomics, and therapeutics. Companies here develop "
        "medicines, lab instruments, or biological research platforms."
    ),
    "Agtech": (
        "Agriculture technology, including farming automation, crop and "
        "livestock monitoring, precision agriculture, and food production "
        "at the farm level. Companies here serve farmers and agribusiness."
    ),
    "ClimateTech": (
        "Climate and clean energy technology, including renewable energy, "
        "carbon capture, grid storage, and decarbonization tools. Companies "
        "here reduce greenhouse gas emissions or adapt to climate change."
    ),
    "DeepTech": (
        "Deep technology built on hard scientific or engineering "
        "breakthroughs, including robotics, advanced materials, quantum "
        "computing, and novel hardware. Companies here are commercializing "
        "fundamental research rather than assembling existing components."
    ),
    "Cybersecurity": (
        "Cybersecurity and information security products, including threat "
        "detection, identity and access management, and data protection. "
        "Companies here help organizations defend against digital attacks."
    ),
    "Industrial Tech": (
        "Industrial and manufacturing technology, including factory "
        "automation, supply chain software, and industrial IoT. Companies "
        "here improve how physical goods are made, moved, or maintained."
    ),
    "DefenseTech": (
        "Defense and national security technology, including autonomous "
        "systems, military hardware, and government security contracting. "
        "Companies here primarily serve defense agencies or militaries."
    ),
    "EdTech": (
        "Education technology, including online learning platforms, "
        "classroom tools, and skills training software. Companies here "
        "serve students, teachers, or corporate learning and development."
    ),
    "Enterprise Software": (
        "Business software sold to other companies, including productivity "
        "tools, developer platforms, and workflow or data infrastructure. "
        "Companies here have other businesses as their primary customers."
    ),
    "Consumer Tech": (
        "Consumer-facing products and apps used by individuals in everyday "
        "life, including social, entertainment, shopping, and lifestyle "
        "apps. Companies here sell directly to individual consumers."
    ),
    "FinTech": (
        "Financial technology, including payments, banking, lending, "
        "investing, and insurance software. Companies here help people or "
        "businesses move, manage, or borrow money."
    ),
}

MIN_SCORE = 0.35
MIN_MARGIN = 0.03

_model: TextEmbedding | None = None
_sector_names: list[str] = []
_sector_vectors: np.ndarray | None = None


def _unit(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=-1, keepdims=True)
    return vecs / np.where(norms == 0, 1.0, norms)


def _ensure_loaded() -> None:
    global _model, _sector_names, _sector_vectors
    if _model is not None:
        return
    _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=MODEL_CACHE_DIR)
    _sector_names = list(SECTOR_DESCRIPTIONS.keys())
    vecs = np.array(list(_model.embed(list(SECTOR_DESCRIPTIONS.values()))))
    _sector_vectors = _unit(vecs)


def classify(homepage_text: str) -> str:
    text = (homepage_text or "").strip()
    if not text:
        return "Other"
    _ensure_loaded()
    assert _model is not None and _sector_vectors is not None

    vec = _unit(np.array(list(_model.embed([text]))[0]))
    scores = _sector_vectors @ vec
    order = np.argsort(-scores)
    top, second = order[0], order[1]

    if scores[top] < MIN_SCORE:
        return "Other"
    if (scores[top] - scores[second]) < MIN_MARGIN:
        return "Other"
    return _sector_names[top]
