"""The whole-ad generation unit (multishot format, ADR-0023).

The storyboard's scenes are the numbered shots of ONE prompt; one reference-to-video generation
renders them all, with native sound, from the real product photo. ``AdPrompt`` is that prompt
(written by the ``ad_prompt_writer`` LLM, rendered to text by code). ``AdGeneration`` is the
run-level generation with its own small state machine:

    pending ──estimate──▶ estimated ──approve (human / mock auto)──▶ estimated + approved
       │                     │                                         │ start
       └──fail──▶ failed ◀───┘                                         ▼
                                    generating ──complete──▶ qa_review ──review──▶ approved
                                        │ release (crash)                     └──▶ flagged
                                        └──▶ estimated (approved kept)

``flagged`` = the critic's hard gate failed. Nothing regenerates automatically: a new generation
costs a full ad, so it goes back through the estimate gate by a human command (warn only).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel, utcnow
from adsmith.domain.generation import Artifact, GenerationAttempt
from adsmith.domain.qa import AdVerdict, QAVerdict
from adsmith.domain.scene import SceneFailure

ShotText = Annotated[str, Field(min_length=1, max_length=400)]
Line = Annotated[str, Field(min_length=1, max_length=200)]
TIMECODE_TOLERANCE_S = 0.25
ProductSize = Literal["absent", "small", "half", "fills"]
SIZE_TEXT: dict[str, str] = {
    "small": "Product small in frame",
    "half": "Product at half the frame",
    "fills": "Product fills the frame",
}


PropSize = Literal["small", "half", "fills"]


class ReferenceDraft(DomainModel):
    """A non-product reference the prompt relies on (ADR-0026), as the writer and the director
    write it (every field required): a prop the video model must not confuse with the product,
    or the location. Code renders the References block from these and the images that exist."""

    name: Annotated[str, Field(min_length=1, max_length=40)] = Field(
        description="Short English name used in the shots, e.g. 'incense burner'."
    )
    description: Line = Field(
        description="One physical line: shape, size, material, colour. Never the product's."
    )
    role: Literal["prop", "location"]
    shots: list[Annotated[int, Field(ge=1, le=12)]] = Field(min_length=1, max_length=12)
    transforms_into_product: bool = Field(
        description="True only for a prop that visibly transforms into the product on screen."
    )
    handled: bool = Field(description="A person's hand holds or uses this prop on screen.")
    max_size: PropSize = Field(description="The largest it appears in frame across its shots.")
    lookalike: bool = Field(
        description="It shares the product's material or shape family (glass flask, gold cap): "
        "a video model could confuse the two."
    )


class ReferenceSpec(ReferenceDraft):
    """A stored reference. Defaults keep references written before these fields loadable."""

    handled: bool = False
    max_size: PropSize = "small"
    lookalike: bool = False


def earns_image(r: ReferenceDraft) -> bool:
    """A prop gets a generated reference image only when the video model can get it wrong in a
    way a viewer sees (user rule, 2026-11-07): it is handled, in 2+ shots, at half the frame or
    more, a look-alike of the product, or it transforms into the product. Otherwise it stays in
    the shot text only (the dallah of run_01M6SSVMPBEAY1WW1FWF4DGWRZ: one wide shot, small)."""
    return r.role == "prop" and (
        r.handled
        or len(set(r.shots)) >= 2
        or r.max_size in ("half", "fills")
        or r.lookalike
        or r.transforms_into_product
    )


class AdShotDraft(DomainModel):
    """One numbered shot as the writer and the director write it (v2, every field required)."""

    number: int = Field(ge=1, le=12)
    start_s: float = Field(ge=0, le=60)
    end_s: float = Field(gt=0, le=60)
    action: ShotText = Field(description="What the viewer sees happen in this shot, one beat.")
    event: Annotated[str, Field(min_length=1, max_length=160)] = Field(
        description="The one physical motion of the shot (a hand lifts, smoke spirals, a spray)."
    )
    camera: ShotText = Field(description="Shot size, lens feel and the camera move.")
    sound: ShotText = Field(description="What is heard in this shot (sound design, music cue).")
    shows_product: bool = Field(description="The product itself (Image 1) is visible.")
    product_in_frame: ProductSize = Field(description="How big the product is; absent if unseen.")
    handled: bool = Field(description="A person's hand holds or uses the product in this shot.")


class AdPromptDraft(DomainModel):
    """``ad_prompt_writer`` v2 / ``director`` output. English only (generation-facing)."""

    location: Line = Field(description="The ONE location of the whole film.")
    look: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="One look for the whole ad: grade, contrast, texture."
    )
    lighting: Line = Field(description="One lighting logic for the whole film.")
    references: list[ReferenceDraft] = Field(max_length=3)
    shots: list[AdShotDraft] = Field(min_length=2, max_length=8)
    key_shot: int = Field(ge=1, le=12, description="The shot where the turn happens.")
    audio: Annotated[str, Field(min_length=1, max_length=400)] = Field(
        description="The soundtrack arc across the ad (music feel, key sound effects, no voice)."
    )


class AdShot(DomainModel):
    """A stored shot. Defaults keep prompts written before v2 loadable (they had per-shot
    lighting and none of the v2 fields)."""

    number: int = Field(ge=1, le=12)
    start_s: float = Field(ge=0, le=60)
    end_s: float = Field(gt=0, le=60)
    action: ShotText
    camera: ShotText
    sound: ShotText
    lighting: str = Field(default="", max_length=400)
    event: str = Field(default="", max_length=160)
    shows_product: bool = True
    product_in_frame: ProductSize = "half"
    handled: bool = False


class AdPrompt(DomainModel):
    """The stored whole-ad prompt: the structured shots and the text the model receives."""

    look: Annotated[str, Field(min_length=1, max_length=500)]
    shots: list[AdShot] = Field(min_length=2, max_length=8)
    audio: Annotated[str, Field(min_length=1, max_length=400)]
    location: str = Field(default="", max_length=200)
    lighting: str = Field(default="", max_length=200)
    references: list[ReferenceSpec] = Field(default_factory=list, max_length=3)
    key_shot: int | None = Field(default=None, ge=1, le=12)
    identity: str = Field(default="", max_length=120)  # the identity clause by product type
    duration_s: int = Field(ge=1, le=60)
    aspect_ratio: str
    text: str = Field(min_length=1)  # rendered by code: what the model receives
    template_version: str
    bible_version: int = Field(ge=1)

    @classmethod
    def from_draft(
        cls,
        d: AdPromptDraft,
        *,
        duration_s: int,
        aspect_ratio: str,
        text: str,
        template_version: str,
        bible_version: int,
        identity: str = "",
    ) -> AdPrompt:
        return cls(
            **d.model_dump(),
            identity=identity,
            duration_s=duration_s,
            aspect_ratio=aspect_ratio,
            text=text,
            template_version=template_version,
            bible_version=bible_version,
        )

    def to_draft(self) -> AdPromptDraft:
        """The v2 draft of a stored v2 prompt (the director rewrites it, props re-render it)."""
        keep = set(AdPromptDraft.model_fields)
        data = {k: v for k, v in self.model_dump().items() if k in keep}
        shot_keys = set(AdShotDraft.model_fields)
        data["shots"] = [{k: v for k, v in s.items() if k in shot_keys} for s in data["shots"]]
        return AdPromptDraft.model_validate(data)


def ref_key(name: str) -> str:
    """Comparable name of a reference: 'Incense_Burner.png' and 'the incense burner' match."""
    stem = re.sub(r"\.(png|jpe?g|webp)$", "", name.strip().lower())
    stem = re.sub(r"[_\-]+", " ", stem)
    return re.sub(r"^(the|a|an) ", "", " ".join(stem.split()))


def shot_list(numbers: Sequence[int]) -> str:
    """'1-3', '2, 4', '1-2, 4' (sorted, ranges collapsed)."""
    nums = sorted(set(numbers))
    parts: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(f"{nums[i]}-{nums[j]}" if j > i else str(nums[i]))
        i = j + 1
    return ", ".join(parts)


def shots_phrase(numbers: Sequence[int]) -> str:
    """'shot 3' or 'shots 1-3'."""
    return f"shot{'s' if len(set(numbers)) > 1 else ''} {shot_list(numbers)}"


def _clause(text: str) -> str:
    """A description that follows a colon: lower-case first letter unless it is an acronym."""
    t = _sentence(text)
    return t[0].lower() + t[1:] if len(t) > 1 and not t[1].isupper() else t


def _sentence(text: str) -> str:
    t = text.strip()
    return t if t.endswith((".", "!", "?")) else f"{t}."


def _capital(text: str) -> str:
    t = _sentence(text)
    return t[0].upper() + t[1:] if t else t


BOTTLE_IDENTITY = "keep its shape, colours, cap and materials identical"
_IDENTITY_BY_KIND: tuple[tuple[str, str], ...] = (
    (r"\b(bottle|flacon|flask|vial|atomi[sz]er|perfume|spray)\b", BOTTLE_IDENTITY),
    (
        r"\b(pouch|bag|sachet|packet|pack)\b",
        "keep its shape, colour, material, seal and emblem identical",
    ),
    (r"\b(box|case|chest|crate|casket)\b", "keep its shape, wood, lining and emblem identical"),
)
GENERIC_IDENTITY = "keep its shape, colours, materials and emblem identical"


def identity_line(product_text: str) -> str:
    """The product-identity clause by product type, from the bible's product spec (name and
    form): a coffee pouch has no cap, a date box has wood and lining (user, 2026-11-07)."""
    for pattern, line in _IDENTITY_BY_KIND:
        if re.search(pattern, product_text, re.IGNORECASE):
            return line
    return GENERIC_IDENTITY


NO_TEXT = "No text, logos, subtitles or watermark."


def identity_sentence(p: AdPromptDraft | AdPrompt, identity: str | None = None) -> str:
    """The first line of every prompt (ADR-0028): what Image 1 is and what must not change. The
    model weighs the first words most (community guides, 2026), so identity comes first."""
    ident = identity or (p.identity if isinstance(p, AdPrompt) and p.identity else BOTTLE_IDENTITY)
    return f"Image 1 is the product: {ident}."


def references_block(
    p: AdPromptDraft | AdPrompt,
    image_names: Sequence[str] = (),
    identity: str | None = None,
    glosses: Mapping[str, str] | None = None,
    *,
    with_identity: bool = True,
) -> str:
    """The References line, built by code. Image 1 is always the product photo; image k+2 is
    ``image_names[k]`` (the run's reference images, in the order they are sent). A reference
    with no image gets no line: it stays in the shot text only (user rule, 2026-11-07), except a
    prop that transforms into the product, whose line carries the turn. A reference image no spec
    names is still declared a prop, so numbering and roles never drift. ``with_identity=False``
    leaves out the identity sentence (``render_ad_prompt`` writes it as the first line).

    With ``glosses`` (compact mode, used when the full text is over the model's limit) a prop
    with an image gets its name and a 2-4 word gloss instead of its description: the image
    carries the look ("Image 2 is the dallah (Saudi coffee pot). It appears in shots 1-4 and is
    never the product.")."""
    keys = [ref_key(n) for n in image_names]
    seen = [s.number for s in p.shots if s.shows_product]
    parts = [identity_sentence(p, identity)] if with_identity else []
    if seen:
        parts.append(f"{'It' if with_identity else 'The product'} appears in {shots_phrase(seen)}.")
    used: set[int] = set()
    for r in p.references:
        k = ref_key(r.name)
        idx = keys.index(k) if k in keys else None
        head = f"Image {idx + 2} is the {r.name}" if idx is not None else f"The {r.name}"
        if idx is None and not (r.role == "prop" and r.transforms_into_product):
            continue  # text only: the shots name it
        if idx is not None:
            used.add(idx)
        if r.role == "location":
            parts.append(f"{head}: {_clause(r.description)} Every shot takes place there.")
            continue
        if glosses is not None and idx is not None:
            gloss = glosses.get(ref_key(r.name))
            name = f"the {r.name} ({gloss})" if gloss else f"the {r.name}"
            if r.transforms_into_product:
                turn = p.key_shot if p.key_shot in r.shots else max(r.shots)
                what = f"transforms into the product in shot {turn}"
            else:
                what = "never the product"
            parts.append(f"Image {idx + 2}: {name}, {shots_phrase(r.shots)}, {what}.")
            continue
        tail = f" It appears in {shots_phrase(r.shots)}"
        if r.transforms_into_product:
            turn = p.key_shot if p.key_shot in r.shots else max(r.shots)
            tail += f" and transforms into the product in shot {turn}; before that it is never "
            tail += "the product."
        else:
            tail += " and is never the product."
        parts.append(f"{head}: {_clause(r.description)}{tail}")
    for idx, name in enumerate(image_names):
        if idx not in used:
            parts.append(
                f"Image {idx + 2} is the {ref_key(name)}, a prop; it is never the product."
            )
    parts.append(NO_TEXT)
    return "References: " + " ".join(parts)


_NO_VOICE = re.compile(
    r"\s*\b(no voice(?:[- ]?over)?s?|no voiceover)\b[,;]?\s*(?:and\s+)?(?:no\s+dialogu?e)?\.?",
    re.IGNORECASE,
)


def strip_no_voice(audio: str) -> str:
    """Code adds "No voice, no dialogue." itself; a writer-typed copy is removed so it is never
    said twice (the dates run ended its audio line with it twice)."""
    out = _NO_VOICE.sub(" ", audio)
    out = re.sub(r"\s*\bno dialogu?e\b\.?", " ", out, flags=re.IGNORECASE)
    return " ".join(out.split()).rstrip(" ,;") or audio


def fit_references(
    p: AdPrompt,
    refs: Sequence[tuple[str, str]],
    *,
    duration_s: int,
    aspect_ratio: str,
    max_chars: int,
    glosses: Mapping[str, str],
) -> tuple[str, list[int], bool]:
    """Fit the prompt within the model's limit (user rule, 2026-11-07). ``refs`` are the run's
    reference images as (name, source) in the order sent. First the full References lines; if
    over the limit, every prop line shortened to its name and a gloss (the image carries the
    look); only if still over, generated props are dropped last-first, then library props as a
    last resort; user images are never dropped. Returns (text, indices of refs dropped,
    compact)."""
    kept = list(range(len(refs)))

    def render(compact: bool) -> str:
        return render_ad_prompt(
            p, duration_s=duration_s, aspect_ratio=aspect_ratio,
            image_names=[refs[i][0] for i in kept], glosses=glosses if compact else None,
        )  # fmt: skip

    text = render(False)
    if len(text) <= max_chars:
        return text, [], False
    dropped: list[int] = []
    for source in ("generated", "library"):
        while len(text := render(True)) > max_chars:
            candidates = [i for i in kept if refs[i][1] == source]
            if not candidates:
                break
            kept.remove(candidates[-1])
            dropped.append(candidates[-1])
    return render(True), dropped, True


def _article(duration_s: int) -> str:
    return "An" if str(duration_s).startswith("8") or duration_s in (11, 18) else "A"


def _key_shot(p: AdPromptDraft | AdPrompt) -> AdShotDraft | AdShot | None:
    return next((s for s in p.shots if s.number == p.key_shot), None)


def key_moment_line(p: AdPromptDraft | AdPrompt) -> str | None:
    """The second line (ADR-0028): the turn, named before anything else, slow by rule. Seedance
    compresses events it is not told to hold (try 2: a 2.5 s morph lasted 0.5 s)."""
    k = _key_shot(p)
    if k is None:
        return None
    what = (k.event or k.action).strip().rstrip(".")
    secs = k.end_s - k.start_s
    return (
        f"Key moment, shot {k.number}, slowly over {secs:g} s: "
        f"{what[0].lower() + what[1:] if len(what) > 1 and not what[1].isupper() else what}."
    )


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def render_ad_prompt(
    p: AdPromptDraft | AdPrompt,
    *,
    duration_s: int,
    aspect_ratio: str,
    image_names: Sequence[str] = (),
    identity: str | None = None,
    glosses: Mapping[str, str] | None = None,
) -> str:
    """The text the video model receives (ADR-0028): the product's identity first, then the key
    moment (both inside the first ~30 words, where the model listens most), the header, the
    References line (props, no-text rule), Look, the numbered shots with "Cut to:" between them
    (models honour a shot list "far more reliably" with explicit cut cues), and Audio. The
    identity, key-moment, no-text and no-voice lines are written by code, so a prompt can never
    drop them."""
    orient = "vertical" if aspect_ratio in {"9:16", "3:4"} else "horizontal"
    look = " ".join(_capital(x) for x in (p.location, p.look, p.lighting) if x and x.strip())
    lines = [identity_sentence(p, identity)]
    if key := key_moment_line(p):
        lines.append(key)
    lines += [
        f"{_article(duration_s)} {duration_s}-second {orient} {aspect_ratio} film, "
        f"{len(p.shots)} shots, hard cuts.",
        references_block(p, image_names, identity, glosses, with_identity=False),
        f"Look: {look}",
    ]
    shots: Sequence[AdShotDraft | AdShot] = p.shots
    for i, s in enumerate(shots):
        size = SIZE_TEXT.get(s.product_in_frame) if s.shows_product else None
        legacy = s.lighting if isinstance(s, AdShot) else ""  # prompts stored before v2
        light = f" Lighting: {_sentence(legacy)}" if legacy else ""
        lines.append(
            ("Cut to: " if i else "")
            + f"Shot {s.number} ({s.start_s:g}-{s.end_s:g} s): {_capital(s.action)}"
            + (f" {size}." if size else "")
            + f" Camera: {_sentence(s.camera)}{light} Sound: {_sentence(s.sound)}"
        )
    lines.append(f"Audio: {_capital(strip_no_voice(p.audio))} No voice, no dialogue.")
    return "\n".join(lines)


def timecode_problems(
    draft: AdPromptDraft, durations: Sequence[float] | None, *, duration_s: int
) -> list[str]:
    """Numbered 1..n, contiguous from 0, ending at the ad's length; with ``durations`` (the
    storyboard's), one shot per scene and each shot as long as its scene."""
    problems: list[str] = []
    if durations is not None and len(draft.shots) != len(durations):
        problems.append(
            f"write exactly {len(durations)} shots (one per storyboard scene), "
            f"not {len(draft.shots)}"
        )
    numbers = [s.number for s in draft.shots]
    if numbers != list(range(1, len(draft.shots) + 1)):
        problems.append(f"number the shots 1..{len(draft.shots)} in order, got {numbers}")
    t = 0.0
    for i, s in enumerate(draft.shots):
        if abs(s.start_s - t) > TIMECODE_TOLERANCE_S or s.end_s <= s.start_s:
            problems.append(f"shot {s.number} must start at {t:g} s (the previous shot's end)")
        if durations is not None and i < len(durations):
            expected_end = t + durations[i]
            if abs(s.end_s - expected_end) > TIMECODE_TOLERANCE_S:
                problems.append(
                    f"shot {s.number} must end at {expected_end:g} s (its storyboard scene lasts "
                    f"{durations[i]:g} s)"
                )
        t = s.end_s
    if draft.shots and abs(draft.shots[-1].end_s - duration_s) > TIMECODE_TOLERANCE_S:
        problems.append(f"the last shot must end at {duration_s} s, the ad's length")
    return problems


MAX_PROP_IMAGES = 3


def planned_images(d: AdPromptDraft | AdPrompt, existing: Sequence[str] = ()) -> list[str]:
    """The reference images the prompt will be sent with: the run's images (``existing``, in
    order) then the props that earn an image (library or generated), at most three."""
    have = {ref_key(n) for n in existing}
    extra = [r.name for r in d.references if earns_image(r) and ref_key(r.name) not in have]
    return [*existing, *extra[:MAX_PROP_IMAGES]]


def budgeted_length(
    d: AdPromptDraft | AdPrompt,
    *,
    duration_s: int,
    existing: Sequence[str] = (),
    glosses: Mapping[str, str] | None = None,
    identity: str | None = None,
) -> tuple[int, int, list[str]]:
    """(length of the whole prompt with the compact References lines of every image it will be
    sent with, length of that References line, the image names). The writer and the director
    are checked against this, so the props stage never has to drop a prop to fit (user,
    2026-11-07: the dates prompt came out at 2,893 characters once its two library props were
    added)."""
    names = planned_images(d, existing)
    text = render_ad_prompt(
        d, duration_s=duration_s, aspect_ratio="9:16", image_names=names,
        identity=identity, glosses=glosses or {},
    )  # fmt: skip
    refs_line = next(x for x in text.splitlines() if x.startswith("References:"))
    return len(text), len(refs_line), names


def length_problem(
    d: AdPromptDraft | AdPrompt,
    *,
    duration_s: int,
    max_chars: int,
    existing: Sequence[str] = (),
    glosses: Mapping[str, str] | None = None,
    identity: str | None = None,
) -> str | None:
    total, refs, names = budgeted_length(
        d, duration_s=duration_s, existing=existing, glosses=glosses, identity=identity
    )
    if total <= max_chars:
        return None
    props = ", ".join(names) or "none"
    return (
        f"the prompt, with the References lines of the images it will be sent with ({props}), "
        f"is {total} characters; the limit is {max_chars}. The References line takes {refs} "
        f"of them, so the rest must fit in {max_chars - refs}: shorten the look, the lighting "
        "and the shots' action, camera and sound (and never list props in the look that the "
        "shots already name)"
    )


def writer_words(d: AdPromptDraft | AdPrompt) -> int:
    """The words the writer controls: location, look, lighting, every shot's action, camera and
    sound, and the audio line. Code-written lines (identity, key moment, header, References,
    labels, timecodes) are not counted: with four shots and two props they alone take ~100 words
    (measured on the multishot fixture, ADR-0028)."""
    shots: Sequence[AdShotDraft | AdShot] = d.shots
    parts = [d.location, d.look, d.lighting, d.audio]
    parts += [f"{s.action} {s.camera} {s.sound}" for s in shots]
    return sum(word_count(x) for x in parts)


def word_problems(
    d: AdPromptDraft | AdPrompt,
    *,
    duration_s: int,
    max_words: int,
    key_within_words: int,
    existing: Sequence[str] = (),
    glosses: Mapping[str, str] | None = None,
    identity: str | None = None,
) -> list[str]:
    """The word budget (ADR-0028, the user's rule 2026-11-07): the writer's own words (look,
    shots, audio) are at most ``max_words``, and the product's identity and the key moment both
    end within the first ``key_within_words`` words of the rendered prompt. Community guides
    converge on 60-100 words with the non-negotiables first ("8 requirements written, 4-5
    honored"); our writers produced 350-400."""
    names = planned_images(d, existing)
    text = render_ad_prompt(
        d, duration_s=duration_s, aspect_ratio="9:16", image_names=names,
        identity=identity, glosses=glosses or {},
    )  # fmt: skip
    problems: list[str] = []
    own = writer_words(d)
    if own > max_words:
        problems.append(
            f"the look, the shots and the audio take {own} words; the limit is {max_words}. Cut "
            "adjectives and repeated nouns: each shot is one beat (who, the one motion, the "
            "contact), one camera move, one sound; the look is one short sentence"
        )
    lines = text.splitlines()
    head = word_count(lines[0]) + (word_count(lines[1]) if lines[1].startswith("Key moment") else 0)
    if head > key_within_words:
        k = _key_shot(d)
        ev = word_count(k.event) if k is not None else 0
        problems.append(
            f"the identity line and the key-moment line take {head} words; both must end within "
            f"the first {key_within_words}: shorten the key shot's event ({ev} words now) to the "
            "one visible motion, e.g. 'his wrist stops mid-shake'"
        )
    return problems


def ad_prompt_problems(
    draft: AdPromptDraft,
    durations: Sequence[float] | None,
    *,
    duration_s: int,
    max_chars: int,
    image_names: Sequence[str] = (),
    identity: str | None = None,
    glosses: Mapping[str, str] | None = None,
    max_words: int | None = None,
    key_within_words: int | None = None,
) -> list[str]:
    """Deterministic checks (→ schema repair): the timecodes (against the storyboard's scene
    durations when given), the key shot exists, and the prompt fits the model's limit WITH the
    compact References lines of every image it will be sent with (``budgeted_length``)."""
    problems = timecode_problems(draft, durations, duration_s=duration_s)
    if not 1 <= draft.key_shot <= len(draft.shots):
        problems.append(f"key_shot {draft.key_shot} is not one of the shots 1..{len(draft.shots)}")
    too_long = length_problem(
        draft, duration_s=duration_s, max_chars=max_chars, existing=image_names,
        glosses=glosses, identity=identity,
    )  # fmt: skip
    if too_long:
        problems.append(too_long)
    if max_words is not None and key_within_words is not None:
        problems += word_problems(
            draft, duration_s=duration_s, max_words=max_words, key_within_words=key_within_words,
            existing=image_names, glosses=glosses, identity=identity,
        )  # fmt: skip
    return problems


def references_budget_note(
    *, existing: Sequence[str], glosses: Mapping[str, str], max_chars: int, identity: str | None
) -> str:
    """Before writing: what the References line already takes, and what each prop that will get
    an image adds (an estimate from the compact line; the code check counts the real ones)."""
    probe = AdPromptDraft.model_validate(
        {
            "location": "x",
            "look": "x",
            "lighting": "x",
            "audio": "x",
            "key_shot": 1,
            "references": [
                {
                    "name": "dallah",
                    "description": "x",
                    "role": "prop",
                    "shots": [1, 2, 3, 4],
                    "transforms_into_product": False,
                    "handled": True,
                    "max_size": "small",
                    "lookalike": False,
                }
            ],
            "shots": [
                {
                    "number": n,
                    "start_s": 2.0 * (n - 1),
                    "end_s": 2.0 * n,
                    "action": "x",
                    "event": "x",
                    "camera": "x",
                    "sound": "x",
                    "shows_product": True,
                    "product_in_frame": "half",
                    "handled": False,
                }
                for n in (1, 2, 3, 4)
            ],
        }
    )
    base = references_block(probe.model_copy(update={"references": []}), existing, identity)
    with_one = references_block(
        probe, [*existing, "dallah"], identity, {**glosses, "dallah": "Saudi coffee pot"}
    )
    per_prop = len(with_one) - len(base)
    return (
        f"The References line, written by code, takes about {len(base)} characters now and "
        f"about {per_prop} more for each prop that will get a reference image (one that is "
        f"handled, in 2+ shots, at half the frame or more, a look-alike, or that transforms "
        f"into the product). Everything you write must fit in {max_chars} characters minus "
        "that line; the code check counts the real lines."
    )


# ----------------------------------------------------------------------------- human prompt
_SHOT_LINE = re.compile(
    r"^\s*(?:Cut to:\s*)?Shot\s+(\d+)\s*\(\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*s\s*\)"
    r"\s*:\s*(.+)$",
    re.IGNORECASE,
)
_SECTION = re.compile(r"^\s*(References|Look|Audio|Key moment)\b", re.IGNORECASE)


class HumanShot(DomainModel):
    number: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    action: str = Field(min_length=1, max_length=2000)


class HumanPrompt(DomainModel):
    """A prompt written by a human creative director that replaces the model-written one at the
    spend gate. Sent to the video model verbatim; its numbered shots (parsed by code) drive the
    critic's frame windows."""

    text: str = Field(min_length=1)
    shots: list[HumanShot] = Field(min_length=1)
    replaced_at: datetime = Field(default_factory=utcnow)
    source: str = Field(default="", max_length=500)  # e.g. the file it came from


def parse_prompt_shots(text: str) -> list[HumanShot]:
    out = []
    for line in text.splitlines():
        m = _SHOT_LINE.match(line)
        if m:
            out.append(
                HumanShot(
                    number=int(m[1]), start_s=float(m[2]), end_s=float(m[3]), action=m[4].strip()
                )
            )
    return out


def human_prompt_problems(text: str, *, duration_s: int, max_chars: int) -> list[str]:
    """Deterministic checks before a human prompt is accepted: within the model's prompt limit,
    numbered shots 1..n with contiguous timecodes from 0 that end at the ad's length."""
    problems: list[str] = []
    if len(text) > max_chars:
        problems.append(f"the prompt is {len(text)} characters; the limit is {max_chars}")
    shots = parse_prompt_shots(text)
    if not shots:
        problems.append('no numbered shots found (lines like "Shot 1 (0-2 s): ...")')
        return problems
    numbers = [s.number for s in shots]
    if numbers != list(range(1, len(shots) + 1)):
        problems.append(f"shots must be numbered 1..{len(shots)} in order, got {numbers}")
    t = 0.0
    for sh in shots:
        if abs(sh.start_s - t) > TIMECODE_TOLERANCE_S or sh.end_s <= sh.start_s:
            problems.append(f"shot {sh.number} must start at {t:g} s (the previous shot's end)")
        t = sh.end_s
    if abs(shots[-1].end_s - duration_s) > TIMECODE_TOLERANCE_S:
        problems.append(f"the last shot ends at {shots[-1].end_s:g} s, not at {duration_s} s")
    return problems


def _sections(text: str) -> dict[str, str]:
    """Split a prompt into comparable parts: header, Look, Shot N, Audio."""
    parts: dict[str, list[str]] = {}
    key = "header"
    for line in text.splitlines():
        if not line.strip():
            continue
        if m := _SHOT_LINE.match(line):
            key = f"Shot {int(m[1])}"
        elif m2 := _SECTION.match(line):
            key = m2[1].capitalize()
        parts.setdefault(key, []).append(" ".join(line.split()))
    return {k: " ".join(v) for k, v in parts.items()}


def prompt_diff_summary(old: str, new: str) -> str:
    """One line for the trace: which parts changed, were added or removed, and the size."""
    a, b = _sections(old), _sections(new)
    changed = [k for k in b if k in a and a[k] != b[k]]
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    same = [k for k in b if k in a and a[k] == b[k]]
    bits = []
    if changed:
        bits.append("changed " + ", ".join(changed))
    if added:
        bits.append("added " + ", ".join(added))
    if removed:
        bits.append("removed " + ", ".join(removed))
    if same:
        bits.append("unchanged " + ", ".join(same))
    bits.append(f"{len(old)} → {len(new)} chars")
    return "; ".join(bits)


# ----------------------------------------------------------------------------- retime edits
RetimeMethod = Literal["blend", "mci"]


class RetimeEdit(DomainModel):
    """A human edit applied at assembly: slow ``window`` by ``factor`` and remove ``trim`` (a
    later, static stretch of the same shot) so the film keeps its length and every timestamp
    after the trim stays where it was. The generation's audio is never touched."""

    window_start_s: float = Field(ge=0)
    window_end_s: float = Field(gt=0)
    factor: float = Field(gt=1, le=4)
    trim_start_s: float = Field(ge=0)
    trim_end_s: float = Field(gt=0)
    method: RetimeMethod = "blend"
    # Dissolve across the trim's join over this many frames (even; 0 = hard join).
    crossfade_frames: int = Field(default=0, ge=0, le=12, multiple_of=2)


def retime_frames(e: RetimeEdit, fps: float) -> tuple[int, int, int, int, int]:
    """(window start, window end, trim start, trim end, frames the slowed window lasts), as frame
    indices on the source (ends exclusive)."""
    ws, we = round(e.window_start_s * fps), round(e.window_end_s * fps)
    ts, te = round(e.trim_start_s * fps), round(e.trim_end_s * fps)
    return ws, we, ts, te, round((we - ws) * e.factor)


def retime_problems(edits: list[RetimeEdit], *, fps: float, frames: int) -> list[str]:
    """Deterministic checks: inside the clip, the trim after its window, no overlaps, and the
    frames added by slowing equal the frames trimmed (the length is unchanged)."""
    problems: list[str] = []
    spans: list[tuple[int, int]] = []
    for i, e in enumerate(edits, start=1):
        ws, we, ts, te, slowed = retime_frames(e, fps)
        if not 0 <= ws < we <= frames or not 0 <= ts < te <= frames:
            problems.append(f"edit {i}: windows must lie inside the clip (0-{frames / fps:.3f} s)")
            continue
        if ts < we:
            problems.append(f"edit {i}: the trim must start at or after the slowed window's end")
        added, removed = slowed - (we - ws), te - ts
        if added != removed:
            problems.append(
                f"edit {i}: slowing adds {added} frames ({added / fps:.3f} s) but the trim removes "
                f"{removed} ({removed / fps:.3f} s): they must match to keep the length"
            )
        half = e.crossfade_frames // 2
        if half and (slowed + (ts - we) < half or frames - te < half):
            problems.append(f"edit {i}: not enough frames on each side for the crossfade")
        spans.append((ws, te))
    spans.sort()
    if any(a[1] > b[0] for a, b in itertools.pairwise(spans)):
        problems.append("edits overlap")
    return problems


AdStatus = Literal[
    "pending", "estimated", "generating", "qa_review", "approved", "flagged", "failed"
]


class IllegalAdTransitionError(ValueError):
    pass


class AdGenerationVersion(DomainModel):
    """A past generation, archived when a human asks for a new one."""

    version: int = Field(ge=1)
    status: AdStatus
    est_usd: float | None = None
    artifact: Artifact | None = None
    qa: AdVerdict | QAVerdict | None = None
    reason: str = Field(max_length=500)
    created_at: datetime = Field(default_factory=utcnow)


class AdGeneration(DomainModel):
    status: AdStatus = "pending"
    prompt: AdPrompt
    model: str = Field(description="Catalog key of the reference-to-video endpoint.")
    provider: str
    duration_s: int = Field(ge=1, le=60)
    resolution: str
    aspect_ratio: str
    generate_audio: bool = True
    # est_usd is the amount the human approves and the wallet reserves (computed + margin);
    # computed_usd is the price itself, price_basis how it was obtained.
    est_usd: float | None = Field(default=None, ge=0)
    computed_usd: float | None = Field(default=None, ge=0)
    price_basis: str | None = Field(default=None, max_length=500)
    estimated_at: datetime | None = None
    approved_usd: float | None = Field(default=None, ge=0)
    approved_by: Literal["user", "auto_mock", "cache"] | None = None
    attempts: list[GenerationAttempt] = Field(default_factory=list)
    artifact: Artifact | None = None
    # AdVerdict from the vision critic; a plain QAVerdict from the mock critic (no rubric: mock
    # media cannot be judged creatively). AdVerdict first: it is the stricter shape.
    qa: AdVerdict | QAVerdict | None = None
    accepted_by_human: str | None = Field(default=None, max_length=500)
    # Set when a human replaced the model-written prompt at the gate: sent verbatim instead.
    human_prompt: HumanPrompt | None = None
    # Human retime edits applied at assembly, and the file name the assembly writes.
    retimes: list[RetimeEdit] = Field(default_factory=list, max_length=8)
    final_name: str = Field(default="final.mp4", pattern=r"^[A-Za-z0-9_.-]+\.mp4$")
    failure: SceneFailure | None = None
    version: int = Field(default=1, ge=1)
    history: list[AdGenerationVersion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        self.check_invariants()
        return self

    def check_invariants(self) -> None:
        s = self.status
        if (s == "failed") != (self.failure is not None):
            raise ValueError("status 'failed' and a failure record must go together")
        if s in {"qa_review", "approved", "flagged"} and self.artifact is None:
            raise ValueError(f"status {s!r} requires the generated video")
        if s in {"approved", "flagged"} and self.qa is None:
            raise ValueError(f"status {s!r} requires the critic's verdict")
        if s == "generating" and self.approved_usd is None:
            raise ValueError("a generation starts only after its estimate was approved")
        if self.approved_usd is not None and self.est_usd is None:
            raise ValueError("an approval needs an estimate")

    @property
    def prompt_text(self) -> str:
        """What the video model receives: the human's prompt if one replaced the model's."""
        return self.human_prompt.text if self.human_prompt is not None else self.prompt.text

    def replace_prompt(self, human: HumanPrompt) -> None:
        """Human creative direction at the spend gate: the new prompt must be priced again, so
        the estimate and any approval are cleared (back to pending)."""
        self._need("pending", "estimated")
        self.human_prompt = human
        self.status = "pending"
        self.est_usd = self.computed_usd = self.price_basis = None
        self.estimated_at = self.approved_usd = self.approved_by = None
        self.check_invariants()

    @property
    def approved(self) -> bool:
        return self.approved_usd is not None and self.est_usd is not None

    # ----------------------------------------------------------------------- transitions
    def _need(self, *allowed: AdStatus) -> None:
        if self.status not in allowed:
            raise IllegalAdTransitionError(f"ad generation is {self.status}, needs {allowed}")

    def set_estimate(
        self, usd: float, *, computed_usd: float | None = None, basis: str | None = None
    ) -> None:
        """``usd`` is the amount to approve and reserve; ``computed_usd`` the price before the
        safety margin (defaults to ``usd``: no margin)."""
        self._need("pending", "estimated")
        self.est_usd, self.estimated_at, self.status = usd, utcnow(), "estimated"
        self.computed_usd = usd if computed_usd is None else computed_usd
        self.price_basis = basis[:500] if basis else None
        self.approved_usd = self.approved_by = None
        self.check_invariants()

    def approve(self, by: Literal["user", "auto_mock", "cache"]) -> None:
        self._need("estimated")
        self.approved_usd, self.approved_by = self.est_usd, by
        self.check_invariants()

    def start(self, attempt: GenerationAttempt) -> None:
        self._need("estimated")
        if not self.approved:
            raise IllegalAdTransitionError("the estimate has not been approved")
        self.status = "generating"
        self.attempts.append(attempt)
        self.check_invariants()

    def release(self) -> None:
        """generating → estimated (crash recovery: the approval is kept, the job is polled)."""
        self._need("generating")
        self.status = "estimated"
        self.check_invariants()

    def complete(self, artifact: Artifact) -> None:
        self._need("generating", "estimated")
        self.artifact, self.status = artifact, "qa_review"
        self.check_invariants()

    def review(self, verdict: QAVerdict) -> None:
        """The critic's verdict: approved when the hard gate passes, flagged otherwise. Neither
        triggers another generation (warn only)."""
        self._need("qa_review")
        self.qa, self.status = verdict, "approved" if verdict.passed else "flagged"
        self.check_invariants()

    def accept(self, reason: str) -> None:
        """A human accepts a flagged generation (logged; the failing verdict stays)."""
        self._need("flagged")
        self.status, self.accepted_by_human = "approved", reason
        self.check_invariants()

    def fail(self, failure: SceneFailure) -> None:
        self._need("pending", "estimated", "generating", "qa_review")
        self.status, self.failure = "failed", failure
        self.check_invariants()

    def renew(self, reason: str) -> None:
        """Archive this generation and start a new one with the same prompt (a human asked for
        it): new version, so a new idempotency key and a new estimate at the gate."""
        self.history.append(
            AdGenerationVersion(
                version=self.version, status=self.status, est_usd=self.est_usd,
                artifact=self.artifact, qa=self.qa, reason=reason,
            )
        )  # fmt: skip
        self.version += 1
        self.status, self.failure, self.artifact, self.qa = "pending", None, None, None
        self.est_usd = self.estimated_at = self.approved_usd = self.approved_by = None
        self.computed_usd = self.price_basis = None
        self.accepted_by_human = None
        self.retimes, self.final_name = [], "final.mp4"  # edits belong to the old video
        self.check_invariants()
