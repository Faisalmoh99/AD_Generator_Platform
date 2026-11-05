# Demo script (5:00)

Six segments for the new studio UI and the approved montage v2. Narration is Arabic with the
technical terms in English: short sentences, read naturally. Each segment has its exact on-screen
actions (what `scripts/record_demo.py` does) and a target duration. The silent footage is in
`docs/demo/segments/`, the voice guide in `docs/demo/VOICEOVER.md`.

| # | File | Time | Duration |
|---|---|---|---|
| 1 | `01_hook.mp4` | 0:00–0:30 | 30 s |
| 2 | `02_brain.mp4` | 0:30–1:30 | 60 s |
| 3 | `03_chaos_run.mp4` | 1:30–2:45 | 75 s |
| 4 | `04_edit.mp4` | 2:45–3:30 | 45 s |
| 5 | `05_real_runs.mp4` | 3:30–4:30 | 60 s |
| 6 | `06_resume_limits.mp4` | 4:30–5:00 | 30 s |

## Setup (what the recorder uses)

```bash
# A: the real runs (montage v2, gallery): the default data dir, read only
uv run adsmith serve --port 8793
# B: live runs on mock media, fresh data dir (no cache hits), montage scenario
ADSMITH_DATA_DIR=/tmp/demo/data ADSMITH_ACCOUNT_DB=/tmp/demo/data/account.db \
ADSMITH_SCRIPTED_SCENARIO=montage_en ADSMITH_MOCK_LATENCY_S=2.2 ADSMITH_SCRIPTED_LATENCY_S=2.5 \
ADSMITH_MEDIA_LONG_SIDE=480 uv run adsmith serve --port 8792
```

Browser: 1920×1080 video, page at ~115 % (viewport 1670×940), a visible cursor with a click
ripple. Mock latencies above make the chaos run take ~48 s and the edit ~14 s.

---

## 1 · Hook (0:00–0:30, 30 s)

**On screen**
1. 0:00–0:12.5: `docs/media/montage_v2.mp4` full height, centred on black, with its music.
2. 0:12.5–0:30: the studio landing page. The cursor rests on the request box, then moves over
   the three "Made with real models" cards (v2, v1, the curtain before/after).

**Say**
> هذا إعلان عطر، ١٢ ثانية ونص.
> ما أحد صوّره، وما أحد عدّله بإيده.
> طلب واحد بالعربي، والـ agent سوّى الباقي: الفكرة، اللقطات، الصور، الفيديو، الموسيقى، والتركيب.
> بس الإعلان مو أهم شي.
> الأهم هو العقل اللي وراه: كيف يقرر، كيف يغلط، وكيف يصلّح نفسه.

---

## 2 · The brain (0:30–1:30, 60 s)

**On screen** (the real montage v2 run, `run_01M6MVHD6CE5V22VM38GXNRN09`)
1. The run opens: pipeline graph on top, swimlanes below, budget and Decisions on the right.
2. The cursor visits the graph nodes left to right, pausing ~1.5 s on each tooltip:
   **Brief** (LLM), **Pick** (LLM · CODE), **Keyframes** (LLM · CODE), **Clips** (CODE),
   **QA** (LLM · CODE), **Assembly** (CODE).
3. It points at the first human gate diamond ("auto"), then the dashed qa → clips loop.
4. It moves to the **Decisions** panel, scrolls it to the top and rests on: "Code chose the
   montage format…", "Code picked concept 2: highest weighted score 7.30…", a fallback line,
   "Beat cut: 130 bpm…".

**Say**
> هذا الـ run الحقيقي للإعلان.
> فوق: الـ state machine. عشر مراحل، والانتقال بينها كود ثابت، مو قرار من الـ LLM.
> كل مرحلة مكتوب عليها مين يقرر.
> LLM يعني النموذج يفكّر ويملأ schema.
> LLM · CODE يعني النموذج يقيّم، بس الكود هو اللي يحسم.
> مثلاً في Pick: النموذج يعطي درجات على ست معايير، والكود يحسب الأوزان ويختار.
> وفي QA: الناقد يشوف الإطارات، بس النجاح والرسوب يقرره الكود.
> وهنا بوابات بشرية. في الوضع التفاعلي الـ run يوقف وينتظرك.
> وعلى اليمين: سجل القرارات، بكلام واضح. مين قرر، وليش.

---

## 3 · A live run with Chaos ON (1:30–2:45, 75 s)

**On screen** (server B, mock media)
1. Landing page. The cursor ticks **Chaos mode**, then clicks **Generate ad**.
2. The graph lights up stage by stage (Brief → Concepts → Pick → …); the cursor follows the lit
   node.
3. Keyframes: nine swimlanes fill in. The cursor rests on a **retry** tag (hover: the 503 and
   its backoff), then a **policy rewrite** loop (hover: the content-policy rejection and the
   rewrite), then a red **Q** diamond (hover: "rejected by QA … motion: …") and its loop back to
   a new clip.
4. The run reaches Assembly → done. The budget burn line and the Decisions panel have filled.
5. Last 10 s: the real v2 run's lanes again, hovering **→ bottle_on_rock** (the automatic
   vocabulary fallback after the splash shot failed the gate twice).

**Say**
> الحين أشغّل run جديد، وأفعّل Chaos mode.
> يعني أحقن أعطال متعمّدة: provider يطيح، رفض content policy، ولقطة جودتها سيئة.
> شوفوا الـ graph يمشي مرحلة مرحلة.
> وتحت: كل مشهد له swimlane خاص فيه. keyframe، gate، clip، QA.
> هذا retry: الـ provider رجّع 503، فانتظر backoff وعاد. محدود ومسجّل.
> وهذا policy rewrite: الـ prompt انرفض، فانكتب من جديد بنفس النية.
> وهذا الناقد رفض المقطع. السبب مكتوب، والـ loop رجع لمقطع جديد.
> ومشهد واحد يطيح ما يوقف الباقي.
> وفي الإعلان الحقيقي: لقطة الماء فشلت مرتين، فالـ storyboard بدّلها تلقائياً بلقطة أسلم.

---

## 4 · The edit (2:45–3:30, 45 s)

**On screen** (the finished chaos run)
1. Scroll to the Storyboard. The cursor clicks the edit box and types «خل المشهد الثاني أبطأ»,
   then clicks **Apply edit**.
2. The amber **blast radius** banner appears: "S2 clip · untouched S1, S3 … S9 $0". The cursor
   rests on it.
3. It hovers S2's card ("new clip · keyframe kept"), then an untouched card ("untouched · $0").
4. S2 regenerates and returns to approved; scroll up to S2's swimlane, now v2.

**Say**
> بعد ما خلص، أقدر أعدّل بكلام عادي: خل المشهد الثاني أبطأ.
> الـ LLM يفهم الطلب، بس الكود هو اللي يحدد أي مشهد، وكم عمق التغيير.
> هذا الـ blast radius.
> تغيير حركة بس، فالصورة تبقى، والمقطع وحده ينعاد.
> وباقي المشاهد ما تنلمس، وتكلفتها صفر.

---

## 5 · Real runs (3:30–4:30, 60 s)

**On screen**
1. **Real runs** tab (server A). Scroll to "Before → after: the curtain veil"; both clips play;
   the cursor rests on "Before: rejected 0.50", then "After: rewritten prompt".
2. The lessons page (`docs/demo/lessons.html`, same styling), three cards in turn:
   - **Critic calibration:** too lenient (2 major issues passed at 0.72) → too strict (10 of 10
     rejected) → hard/soft split → 12 of 14 verdicts right.
   - **The kurta lesson:** the real hero v2 keyframe (mandarin collar, scarf at the neck) and the
     wardrobe lint that now rejects it: "mandarin collar · button placket → white Saudi thobe,
     concealed placket".
   - **Keyframe gate savings:** 21 keyframes stopped before animation, ≈ $5.60 of Kling in the
     first full run; the v2 contact sheet with its rejected stills.

**Say**
> هذي نتايج حقيقية على fal.
> هنا الـ video model فهم كلمة "ظل ستارة" حرفياً، وحط ستارة فوق المنتج.
> الناقد رفضها بـ 0.50، والـ prompt انكتب من جديد، والمقطع الثاني نظيف.
> الناقد نفسه احتاج معايرة: أول مرة كان متساهل، بعدين متشدد.
> الحل: فحوصات hard تُسقط اللقطة، وفحوصات soft تنقص الدرجة بس. صار يصيب ١٢ من ١٤.
> وهنا النموذج لبّس الرجل ياقة كُرتة بدل الثوب السعودي. الحين فيه lint ثقافي يرفضها قبل التوليد.
> والـ keyframe gate: نراجع الصورة قبل ما ندفع على الفيديو. وفّر ٢١ مقطع، تقريباً ٥ دولار ونص في أول run.

---

## 6 · Kill + resume, then honest limits (4:30–5:00, 30 s)

**On screen**
1. A terminal plays `make demo` (recorded through a pty and replayed with xterm.js): the chaos run,
   the edit with its blast radius, then a run killed with SIGKILL mid-generation and resumed,
   finished generations coming back as cache hits at $0.
2. Last ~8 s: a limits card: the critic misses small material changes (a crystal cap passed at
   0.78), no eval suite yet, single process (SQLite + asyncio), event shots are fragile.

**Say**
> وهذا make demo: أقتل العملية بـ SIGKILL وسط التوليد، وأرجّع.
> يكمل من آخر snapshot، واللي خلص يرجع من الـ cache ببلاش.
> وبصراحة، فيه حدود.
> الناقد يفوّته تغيّر صغير في الخامة، مثل غطا كريستال.
> ما عندي eval suite للحين، والنظام process واحد.
> بس كل قرار مسجّل، وكل خطأ له طريق رجعة.
