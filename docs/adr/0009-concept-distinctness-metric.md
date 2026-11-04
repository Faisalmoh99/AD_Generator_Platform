# ADR-0009: Concept distinctness metric

- Status: Accepted (2026-11-04)

## Context
The agent must propose **3 distinct** ad concepts. LLMs often return three variations of one idea,
especially at lower effort. "Distinct" must therefore be *enforced* by code (so a violation feeds
the schema-repair loop), and the check must be deterministic, dependency-light and work for Arabic
as well as English. Embeddings are explicitly out of scope at this stage.

## Decision
`domain/similarity.py`, pure standard library:

1. **Text per concept** = title + logline + visual hook + emotional driver + narrative beats
   (`ConceptDraft.signature_text()`). The `differentiator` and `risk_notes` are excluded: a
   differentiator talks *about* the other concepts and would inflate overlap.
2. **Normalise:** NFKC + casefold. Arabic: strip diacritics and tatweel; unify أ/إ/آ/ٱ→ا, ى→ي,
   ة→ه, ؤ→و, ئ→ي; map Arabic-Indic digits; strip the definite-article prefix (ال، وال، بال،
   كال، فال، لل) when ≥3 letters remain.
3. **Tokenise**, then drop EN/AR stop words (including common Saudi fillers such as زي، مره، بس، اللي)
   and tokens shorter than 2 characters.
4. **Word-bounded character trigrams** per token (`#gold#` → `#go gol old ld#`). They absorb
   inflection and clitics without a stemmer.
5. **Jaccard index** of the two trigram sets. A pair is "not distinct" if it is ≥ **0.35**. The
   validator names the offending pair and percentage ("concepts 1 and 3 are 47% similar") so the
   repair prompt is actionable.

### Threshold justification (measured, pinned by tests)
On a calibration set of realistic concept texts for the perfume brief (`tests/fixtures/concept_texts.py`):

| Class | Pairs | Jaccard |
|---|---|---|
| Distinct concepts (EN) | night drive / elemental / tailor | 0.08 – 0.13 |
| Distinct concepts (AR) | same three in Arabic | 0.05 – 0.10 |
| Distinct but **shared vocabulary** (young man + night + perfume + reflection) | night drive vs mirror / rooftop | 0.18 – **0.29** |
| Same idea, new surface details | night drive vs "neon city walk" | **0.39** |
| Saudi-dialect paraphrase | AR night drive vs يسوق/بالليل rewrite | 0.47 |
| Light paraphrase | EN | 0.75 |

0.35 sits between the hardest legitimately-distinct pair (0.29) and the weakest duplicate the metric
can see (0.39). The tests assert every distinct pair is below the threshold, every duplicate is
at or above it, and the gap between the classes is at least 0.08. A future change to normalisation
that blurs the classes fails CI.

Error asymmetry: a false positive (rejecting a valid set) costs one cheap repair call; a false
negative shows the user two near-identical options. Both are tolerable, so the midpoint is right.

## Known limitation
A full **synonym rewrite** of the same idea ("drives alone at night… sprays the perfume" →
"cruises after dark… applies the fragrance") scores 0.20 and passes. A lexical metric cannot see
meaning. A test documents this. Mitigations: the `ideate` prompt asks for concepts on deliberately
different creative axes (narrative / sensory-abstract / aspirational), and each concept must state a
`differentiator`. "More time": add an embedding-similarity check behind the same interface.

## Alternatives considered
- **Embeddings (cosine):** semantic, but needs a model download or API call, is not bit-for-bit
  reproducible across versions, and is overkill for this guard.
- **Word-level Jaccard / TF-IDF:** brittle to inflection and Arabic clitics; TF-IDF needs a corpus.
- **LLM-as-judge for distinctness:** nondeterministic and costs another call. It can't be a
  validator.
- **`difflib.SequenceMatcher`:** order-sensitive, and slow on longer texts.

## Consequences
- + Deterministic, ~70 lines, no dependencies, bilingual, explainable error messages.
- − Lexical only (see limitation). The threshold is tied to the signature fields; changing them
  means re-running calibration.
