"""Concept-distinctness metric: normalisation, calibration, and known limits (ADR-0009)."""

from __future__ import annotations

import pytest

from adsmith.domain.similarity import (
    DISTINCTNESS_THRESHOLD,
    normalize_text,
    shingles,
    similarity,
    tokens,
    too_similar_pairs,
)
from tests.fixtures.concept_texts import (
    EN_NIGHT_DRIVE,
    EN_NIGHT_DRIVE_SYNONYM_REWRITE,
    SHOULD_BE_DISTINCT,
    SHOULD_BE_DUPLICATES,
)


class TestNormalisation:
    def test_arabic_letter_variants_unify(self) -> None:
        assert normalize_text("أحمد") == normalize_text("احمد") == normalize_text("إحمد")
        assert normalize_text("ليلى") == normalize_text("ليلي")
        assert normalize_text("مدينة") == normalize_text("مدينه")

    def test_diacritics_and_tatweel_removed(self) -> None:
        assert normalize_text("يُسلَّم") == normalize_text("يسلم")
        assert normalize_text("جـــميل") == normalize_text("جميل")

    def test_arabic_indic_digits(self) -> None:
        assert normalize_text("١٥") == "15"

    def test_definite_article_stripped_but_short_words_kept(self) -> None:
        assert tokens("العطر") == ["عطر"]
        assert tokens("والعطر") == ["عطر"]
        assert tokens("الي") == []  # stop word, not an article-stripped token

    def test_stopwords_and_case(self) -> None:
        assert tokens("The Perfume and THE bottle") == ["perfume", "bottle"]

    def test_shingles_are_word_bounded(self) -> None:
        assert shingles("gold") == frozenset({"#go", "gol", "old", "ld#"})


class TestMetricProperties:
    def test_identity_and_symmetry(self) -> None:
        assert similarity(EN_NIGHT_DRIVE, EN_NIGHT_DRIVE) == 1.0
        a, b = SHOULD_BE_DISTINCT[0][1:]
        assert similarity(a, b) == similarity(b, a)

    def test_empty_texts_are_identical(self) -> None:
        assert similarity("", "the and of") == 1.0

    def test_deterministic(self) -> None:
        assert similarity(*SHOULD_BE_DISTINCT[3][1:]) == similarity(*SHOULD_BE_DISTINCT[3][1:])


class TestCalibration:
    """These pin the threshold. If a change to normalisation moves scores across it, this fails."""

    @pytest.mark.parametrize(("name", "a", "b"), SHOULD_BE_DISTINCT, ids=lambda x: str(x)[:40])
    def test_distinct_pairs_pass(self, name: str, a: str, b: str) -> None:
        assert similarity(a, b) < DISTINCTNESS_THRESHOLD, name

    @pytest.mark.parametrize(("name", "a", "b"), SHOULD_BE_DUPLICATES, ids=lambda x: str(x)[:40])
    def test_duplicate_pairs_rejected(self, name: str, a: str, b: str) -> None:
        assert similarity(a, b) >= DISTINCTNESS_THRESHOLD, name

    def test_margin_between_classes(self) -> None:
        worst_distinct = max(similarity(a, b) for _, a, b in SHOULD_BE_DISTINCT)
        best_dup = min(similarity(a, b) for _, a, b in SHOULD_BE_DUPLICATES)
        assert worst_distinct < DISTINCTNESS_THRESHOLD <= best_dup
        assert best_dup - worst_distinct >= 0.08  # the classes are separable, not borderline

    def test_known_blind_spot_full_synonym_rewrite(self) -> None:
        """Documented limitation: a lexical metric cannot see a same-idea rewrite that shares no
        words. Mitigated by the ideate prompt (distinct creative axes) and a future embedding
        check. If this ever starts failing, the metric improved: update ADR-0009."""
        assert similarity(EN_NIGHT_DRIVE, EN_NIGHT_DRIVE_SYNONYM_REWRITE) < DISTINCTNESS_THRESHOLD

    def test_too_similar_pairs_reports_indices(self) -> None:
        dup = SHOULD_BE_DUPLICATES[0][2]
        texts = [EN_NIGHT_DRIVE, SHOULD_BE_DISTINCT[0][2], dup]
        hits = too_similar_pairs(texts)
        assert [(i, j) for i, j, _ in hits] == [(0, 2)]
