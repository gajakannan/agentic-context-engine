"""XML skill rendering and skill retrieval helpers."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Iterable
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from ace.core.skillbook import Skill, Skillbook
    from ace.deduplication.detector import SimilarityDetector

logger = logging.getLogger(__name__)

RRF_K = 60


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _normalize_keywords(keywords: Iterable[str] | None) -> list[str]:
    if keywords is None:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        text = str(keyword).strip().lower().replace(" ", "_")
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _keyword_overlap(skill: "Skill", keywords: list[str]) -> int:
    if not keywords:
        return 0
    return sum(1 for keyword in keywords if keyword in skill.keywords)


def render_skills_xml(skills: list["Skill"]) -> str:
    """Render skills as XML ``<strategy>`` elements."""
    if not skills:
        return ""

    parts: list[str] = []
    for skill in skills:
        keyword_attr = ",".join(skill.keywords)
        body = [f"  <issue>{escape(skill.issue)}</issue>"]
        if skill.insight:
            body.append(f"  <insight>{escape(skill.insight)}</insight>")
        body.append(f"  <keywords>{escape(keyword_attr)}</keywords>")
        parts.append(
            f'<strategy id="{escape(skill.id)}" section="{escape(skill.section)}">\n'
            + "\n".join(body)
            + "\n</strategy>"
        )

    strategies_block = "\n".join(parts)
    return (
        f"{strategies_block}\n\n"
        "Adapt these strategies to your current situation; "
        "they are patterns, not rigid rules."
    )


def _lexical_ranking(
    skills: list["Skill"],
    query: str,
) -> list["Skill"]:
    if not skills:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return skills

    try:
        from rank_bm25 import BM25Okapi

        corpus = [_tokenize(skill.embedding_text()) for skill in skills]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(query_tokens)
        ranked_pairs = sorted(
            zip(scores, skills),
            key=lambda item: item[0],
            reverse=True,
        )
        return [skill for _, skill in ranked_pairs]
    except Exception as exc:
        logger.debug("BM25 unavailable, falling back to token overlap: %s", exc)
        query_token_set = set(query_tokens)
        ranked_pairs = []
        for skill in skills:
            doc_tokens = set(_tokenize(skill.embedding_text()))
            ranked_pairs.append((len(query_token_set & doc_tokens), skill))
        ranked_pairs.sort(key=lambda item: item[0], reverse=True)
        return [skill for _, skill in ranked_pairs]


def _dense_ranking(
    skills: list["Skill"],
    query: str,
    detector: "SimilarityDetector",
) -> list["Skill"]:
    if not skills:
        return []

    query_embedding = detector.compute_embedding(query)
    if query_embedding is None:
        raise RuntimeError(
            "Failed to embed retrieval query — "
            "check embedding provider credentials / network."
        )

    ranked_pairs: list[tuple[float, Skill]] = []
    for skill in skills:
        if skill.embedding is None:
            continue
        similarity = detector.cosine_similarity(query_embedding, skill.embedding)
        ranked_pairs.append((similarity, skill))

    ranked_pairs.sort(key=lambda item: item[0], reverse=True)
    return [skill for _, skill in ranked_pairs]


def retrieve_top_k(
    skillbook: "Skillbook",
    query: str,
    *,
    top_k: int = 5,
    detector: "SimilarityDetector | None" = None,
    section: str | None = None,
    keywords: list[str] | None = None,
) -> list["Skill"]:
    """Retrieve relevant skills using lexical + dense fusion."""
    if top_k <= 0:
        return []

    candidates = skillbook.skills()
    if section:
        normalized_section = str(section).strip().lower()
        candidates = [
            skill for skill in candidates if skill.section == normalized_section
        ]
    if not candidates:
        return []

    normalized_keywords = _normalize_keywords(keywords)

    if detector is None:
        from ace.deduplication.detector import SimilarityDetector as _Detector
        from ace.protocols.deduplication import DeduplicationConfig

        detector = _Detector(DeduplicationConfig())

    detector.ensure_embeddings(skillbook)
    lexical_ranked = _lexical_ranking(candidates, query)
    dense_ranked = _dense_ranking(candidates, query, detector)

    fused_scores: dict[str, float] = defaultdict(float)
    skills_by_id = {skill.id: skill for skill in candidates}

    for rank, skill in enumerate(lexical_ranked, start=1):
        fused_scores[skill.id] += 1.0 / (RRF_K + rank)
    for rank, skill in enumerate(dense_ranked, start=1):
        fused_scores[skill.id] += 1.0 / (RRF_K + rank)

    if normalized_keywords:
        for skill in candidates:
            overlap = _keyword_overlap(skill, normalized_keywords)
            if overlap:
                fused_scores[skill.id] += 0.25 * overlap

    ranked_ids = sorted(
        fused_scores,
        key=lambda skill_id: fused_scores[skill_id],
        reverse=True,
    )
    return [skills_by_id[skill_id] for skill_id in ranked_ids[:top_k]]
