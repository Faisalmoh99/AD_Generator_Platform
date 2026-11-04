# Demo script (4:45)

Narration is in Saudi-friendly Arabic, with technical terms kept in English. Short sentences:
read them naturally, and pause on the bold beats.

## Before recording (2 minutes)

```bash
make setup
uv run adsmith serve --port 8765         # browser at http://127.0.0.1:8765, window ~1440 px
# second terminal, same font size, ready for segment 6:
make demo
```

- Open the README on GitHub in a second tab, scrolled to "The brain".
- Have `docs/media/majlis_ad_final.mp4` open in a player, paused on the first frame.
- In the UI: chaos OFF, "Agent picks the concept" ON, no run open (clean landing page).
- Mock media is fast but not instant (`ADSMITH_MOCK_LATENCY_S=1.2` makes the live board readable).

---

## 1 · Hook (0:00–0:30)

**On screen:** the majlis ad plays full screen (`docs/media/majlis_ad_final.mp4`, 17 s), ending on
the Arabic end card. Then cut to the UI landing page.

**Say:**
> هذا إعلان عطر من ١٧ ثانية.
> ما صوّرته، وما عدّلته بإيدي.
> كتبه وأخرجه **agent**: من الطلب، لثلاث أفكار، لـ storyboard، لكل مشهد، لين التجميع النهائي.
> بس الإعلان مو هو الشي المهم.
> المهم هو **العقل** اللي وراه: كيف يقرر، كيف يغلط، وكيف يصلّح غلطه.
> خلوني أوريكم.

---

## 2 · The brain (0:30–1:30)

**On screen:** README → "The brain". Scroll slowly: components diagram (2–3 s), run state machine,
then scene sub-state machine.

**Say:**
> أول قرار، وأهم قرار: **الـ LLM ما يقرر وش الخطوة الجاية أبدًا.**
> الـ stages والـ transitions جدول معرّف في الكود، و `next_stage` هي pure function.
> الـ LLM يفكر داخل كل node: يكتب الأفكار، يقيّمها، يكتب الـ prompts، ويراجع الجودة.
> بس ما يمسك الـ control flow.
>
> كل node ترجع **typed deltas**، ما تعدّل الـ state مباشرة.
> فيه writer واحد يطبّقها، ويحفظ **snapshot بعد كل خطوة** في SQLite.
> يعني أقدر أكمّل أي run بعد crash، أو أرجع لنسخة قديمة وأعمل منها fork.
>
> وكل مشهد له state machine خاصة فيه: pending، generating، QA، approved أو failed.
> ومشهد يفشل **ما يوقّف** المشاهد الثانية.
>
> ليش ما استخدمت LangGraph؟
> لأني أبغى الـ control flow يكون data أقدر أختبره، مو prompt أتمنى إنه يمشي صح.
> وكل output من الـ LLM يكون Pydantic object متحقق منه. إذا طلع غلط، يمر على **schema repair** ويرجع له الخطأ بالضبط.

---

## 3 · Live run with chaos ON (1:30–2:45)

**On screen (clicks):**
1. Click the first example chip (the Arabic request «اعطني إعلان ١٥ ثانية لعطر رجالي فاخر…»).
2. Toggle **Chaos mode** ON. Click **Generate ad**.
3. Concepts appear with scores and the judge's pick → hover over the weighted score.
4. Storyboard: scenes go pending → generating → QA → approved. Point at a **recovery chip**
   (policy rewrite / QA regeneration) on a card, then click **Recoveries** in the trace filter.
5. Point at the budget meter ("simulated: mock media priced at real Kling rates").
6. Final player: play the last 3 s → **Arabic end card**. Point at the placeholder note.

**Say:**
> هذا طلب بالسعودي. شغّلت **chaos mode**: يحقن أخطاء حقيقية، seeded عشان تتكرر.
> الـ agent كتب ثلاث أفكار، والحَكَم قيّمها على rubric موزون.
> **الكود** هو اللي يختار الأعلى، مو الـ LLM.
>
> شوفوا الـ storyboard. كل مشهد يمشي لحاله.
> هنا content policy رفض الـ prompt، والـ agent أعاد صياغته وحافظ على المنتج والفكرة.
> وهنا الـ QA critic رفض الـ clip، وانعاد بالـ fixes حقته.
> كل retry مصنّف ومحدود ومحسوب على الـ budget، ومكتوب في الـ trace.
>
> والـ budget هنا simulated، بس بأسعار Kling الحقيقية: كل call يحجز قبل ما يصير.
>
> وهذا الإعلان النهائي، مع end card عربي.
> اسم البراند **placeholder** من اختراع الـ agent، عمره ما يستخدم براند حقيقي.

---

## 4 · Targeted edits (2:45–3:30)

**On screen:**
1. Type in the edit bar: `خل المشهد الثاني أبطأ` → **Apply edit**.
2. The blast-radius banner appears: S2 "clip", the others "untouched · $0". Pause 2 s on it.
3. When it finishes, type `make scene 3 at night` → **Apply edit**. Banner: S3 prompt + S4
   "continues the same moment", S1 and S2 untouched.

**Say:**
> الحين أبغى أعدّل. أكتب بكلامي: «خل المشهد الثاني أبطأ».
> الكود يعرف إن «الثاني» يعني المشهد ٢، ويحسب الـ **blast radius**.
> التعديل حركة بس، فالـ keyframe يبقى. **clip واحد جديد، provider call واحد.**
> المشاهد الثانية: **صفر calls، صفر ريال.**
>
> تعديل ثاني: «make scene 3 at night».
> هذا يغيّر الإضاءة، فيرجع لمستوى الـ prompt.
> والـ agent انتبه إن المشهد ٤ كمّل نفس اللحظة، فخلاه ليل بعد.
> المشهد ١ و ٢ ما انلمسوا.

---

## 5 · Real runs (3:30–4:15)

**On screen:** click **Real runs**.
1. The curtain card: play **Before** (veil at 2 s), then **After**.
2. Scroll to the hero attempt card: pause on the close-up frame with the invented label text.
3. (Optional, 3 s) README → "What real runs taught me", items 2–3.

**Say:**
> كل اللي شفتوه شغّال بدون keys. بس أنا جرّبته على موديلات حقيقية: FLUX، وLTX، وKling.
>
> هنا الـ prompt قال «ظل ستارة يمر على الزجاجة»، والموديل حط **ستارة حقيقية** فوق المنتج.
> الـ critic رفضه، والـ loop صلّحه، وطلعت الزجاجة واضحة.
>
> والـ critic نفسه احتاج **ثلاث محاولات** عشان يتظبط:
> أول كان متساهل، بعدين صار متشدد ورفض عشرة من عشرة،
> والحل كان أقسم المشاكل لـ **hard وsoft**. صار يحكم صح في ١٢ من ١٤.
>
> وضفت **keyframe gate**: أراجع الصورة قبل ما أدفع على الفيديو. وفّر تقريبًا ٥ دولار ونص في أول run.
>
> وآخر تجربة: Kling كتب حروف من عنده على الزجاجة، والـ critic رفض النسختين.
> والسبب الحقيقي إن الـ gate عدّى غطا أسود على إنه «minor».
> كلّفني دولارين وربع، **والحين صار hard failure بالكود.**

---

## 6 · Crash/resume, limits, close (4:15–4:45)

**On screen:** the terminal with the end of `make demo`: the "Kill mid-generation, then resume"
table (clips after resume, "regenerated twice: none", "leaked reservations: $0.00"). Then README →
"Limitations and with more time".

**Say:**
> وهذا `make demo`: أقتل الـ process بـ SIGKILL وسط التوليد، وأسوي resume.
> ولا clip انعاد مرتين، ولا ريال ضاع في الحجوزات.
> وفي fal، الـ request id ينحفظ أول ما ينرسل الطلب، فالـ resume يكمّل نفس الطلب بدل ما يدفع مرة ثانية.
>
> الحدود بصراحة: دقة شكل المنتج في الصور هي أضعف نقطة، والـ critic مو deterministic.
> لو عندي وقت أكثر: **product reference image**، وinpainting بدل إعادة التوليد، وevals.
> شكرًا.

---

## Likely interview questions

**Why not LangGraph?**
I needed a small, fixed pipeline with heavy recovery logic, not a general graph runtime. A
declared transition table plus a pure `next_stage(state)` gives me control flow I can unit-test
exhaustively, typed state end to end, and snapshots and forks I own. LangGraph would add checkpointing
and graph abstractions I would have had to bend for per-scene isolation, budget reservations and
crash-safe provider jobs. (ADR-0001)

**Why does the LLM never pick the next stage?**
Control flow must be deterministic, testable and cheap to reason about. If a model chooses
transitions, one odd output can skip QA or loop and spend money. LLMs fill schema fields inside
nodes (concepts, scores, prompts, verdicts) and code decides: the concept winner (weighted rubric),
QA pass/fail (hard checks + threshold), what an edit invalidates (pure blast-radius function).

**How does the cache key work?**
`sha256` of the canonical JSON of {provider, model, phase, prompt, negative prompt, params, the
input keyframe's sha256}. Same request → cache hit at $0, logged as an event. Failures are never
cached, and an output the critic rejected is evicted, so a retry can't be served the bad image.
Best-of-N candidates carry a `candidate` index in params, so they are distinct paid jobs.

**How is a stale write prevented?**
Every write goes through one function: it opens a write transaction, loads the *latest* snapshot
inside it, applies the mutation and bumps `state_version` (optional `expected_version` → conflict
error). Scene workers never write directly; they return results, and the mutation checks the scene
version. If an edit bumped the scene while a clip was rendering, the mutation raises
`StaleWriteError` and the outdated clip is discarded, not applied.

**How do you stop it spending too much?**
Estimate → reserve → settle or release, in the run budget *and* a cross-run wallet ledger with a hard
ceiling (the prepaid fal balance). A call that would cross either limit is refused before it is
made, and that scene fails alone. Provider SDK retries are disabled, so every retry is counted.

**What happens on a crash mid-generation?**
Resume closes interrupted attempts and releases their reservations. For fal, the request id is
persisted right after submit, so resume polls the job that is already running and only resubmits if
the provider no longer knows it.

**What would you do with more time?**
A product reference image for keyframes and for the critic (layout and product shape are the
main weakness), inpainting near-miss keyframes instead of re-rolling, a second critic opinion near
the threshold, an escalation ladder (switch model, then simplify the spec), and the evals suite I
cut for time. See `docs/MORE_TIME.md`.
