# Voiceover guide

Record one file per segment, in Arabic, with the technical terms in English, and save it as
`docs/demo/voice/NN_name.m4a` (`.wav`, `.mp3`, `.aiff` also work). Then:

```bash
uv run python scripts/merge_voiceover.py      # → docs/demo/final.mp4
```

Each recording is fitted to its segment and never sped up. A shorter take is padded with silence
after it. A take up to 0.8 s too long has its tail faded out; trailing silence is removed before
measuring. A take longer than that holds the segment's last frame until you finish, so no word is
cut. Voices are loudness-normalised to −16 LUFS. Only the hook gets a very low bed of the
montage v2 music (`--bed-db`, default −18 dB).

**Pace:** about 2 Arabic words per second, with a breath between sentences. Each text below is
written to finish 3–5 s before its segment ends. Start ~0.3 s after you press record (the merge
adds a 0.25 s lead).

| # | File to record | Duration | Words |
|---|---|---|---|
| 1 | `voice/01_hook.m4a` | **30.0 s** | ~45 |
| 2 | `voice/02_brain.m4a` | **60.0 s** | ~100 |
| 3 | `voice/03_chaos_run.m4a` | **75.0 s** | ~110 |
| 4 | `voice/04_edit.m4a` | **45.0 s** | ~55 |
| 5 | `voice/05_real_runs.m4a` | **60.0 s** | ~100 |
| 6 | `voice/06_resume_limits.m4a` | **30.0 s** | ~50 |

---

## 01_hook · 30.0 s

On screen: montage v2 plays (0–12.5 s), then the studio landing page.

> هذا إعلان عطر، ١٢ ثانية ونص.
> ما أحد صوّره، وما أحد عدّله بإيده.
> طلب واحد بالعربي، والـ agent سوّى الباقي: الفكرة، اللقطات، الصور، الفيديو، الموسيقى، والتركيب.
> بس الإعلان مو أهم شي.
> الأهم هو العقل اللي وراه: كيف يقرر، كيف يغلط، وكيف يصلّح نفسه.

Tip: let the first ~2 s of the ad breathe before you start.

## 02_brain · 60.0 s

On screen: the real v2 run. The cursor walks the graph nodes (Brief, Pick, Keyframes, Clips,
QA, Assembly), the human gate, the qa → clips loop, then the Decisions panel.

> هذا الـ run الحقيقي للإعلان.
> فوق: الـ state machine. عشر مراحل، والانتقال بينها كود ثابت، مو قرار من الـ LLM.
> كل مرحلة مكتوب عليها مين يقرر.
> LLM يعني النموذج يفكّر ويملأ schema.
> LLM · CODE يعني النموذج يقيّم، بس الكود هو اللي يحسم.
> مثلاً في Pick: النموذج يعطي درجات على ست معايير، والكود يحسب الأوزان ويختار.
> وفي QA: الناقد يشوف الإطارات، بس النجاح والرسوب يقرره الكود.
> وهنا بوابات بشرية. في الوضع التفاعلي الـ run يوقف وينتظرك.
> وعلى اليمين: سجل القرارات، بكلام واضح. مين قرر، وليش.

Cue: "Pick" lands at ~0:08, "QA" at ~0:21, the human gate at ~0:30, Decisions from ~0:37.

## 03_chaos_run · 75.0 s

On screen: Chaos on → Generate; the graph lights stage by stage (~0:05–0:20), the swimlanes fill
(~0:20–0:53), hovers on retry, policy rewrite and a red QA (~0:54–1:08), then the real run's
"→ bottle_on_rock" fallback (last ~6 s).

> الحين أشغّل run جديد، وأفعّل Chaos mode.
> يعني أحقن أعطال متعمّدة: provider يطيح، رفض content policy، ولقطة جودتها سيئة.
> شوفوا الـ graph يمشي مرحلة مرحلة.
> وتحت: كل مشهد له swimlane خاص فيه. keyframe، gate، clip، QA.
> هذا retry: الـ provider رجّع 503، فانتظر backoff وعاد. محدود ومسجّل.
> وهذا policy rewrite: الـ prompt انرفض، فانكتب من جديد بنفس النية.
> وهذا الناقد رفض المقطع. السبب مكتوب، والـ loop رجع لمقطع جديد.
> ومشهد واحد يطيح ما يوقف الباقي.
> وفي الإعلان الحقيقي: لقطة الماء فشلت مرتين، فالـ storyboard بدّلها تلقائياً بلقطة أسلم.

Cue: say "هذا retry" when the cursor stops on the first amber "retry" tag (~0:55).

## 04_edit · 45.0 s

On screen: typing «خل المشهد الثاني أبطأ», Apply edit, the blast-radius banner (~0:06), S2's card
"new clip · keyframe kept", an untouched card, then S2's lane again.

> بعد ما خلص، أقدر أعدّل بكلام عادي: خل المشهد الثاني أبطأ.
> الـ LLM يفهم الطلب، بس الكود هو اللي يحدد أي مشهد، وكم عمق التغيير.
> هذا الـ blast radius.
> تغيير حركة بس، فالصورة تبقى، والمقطع وحده ينعاد.
> وباقي المشاهد ما تنلمس، وتكلفتها صفر.

## 05_real_runs · 60.0 s

On screen: the curtain before/after (0–15 s), then the lessons page: critic calibration
(~0:17–0:29), the kurta keyframe and its lint (~0:29–0:41), keyframe-gate savings (~0:41–0:52).

> هذي نتايج حقيقية على fal.
> هنا الـ video model فهم كلمة "ظل ستارة" حرفياً، وحط ستارة فوق المنتج.
> الناقد رفضها بـ 0.50، والـ prompt انكتب من جديد، والمقطع الثاني نظيف.
> الناقد نفسه احتاج معايرة: أول مرة كان متساهل، بعدين متشدد.
> الحل: فحوصات hard تُسقط اللقطة، وفحوصات soft تنقص الدرجة بس. صار يصيب ١٢ من ١٤.
> وهنا النموذج لبّس الرجل ياقة كُرتة بدل الثوب السعودي. الحين فيه lint ثقافي يرفضها قبل التوليد.
> والـ keyframe gate: نراجع الصورة قبل ما ندفع على الفيديو. وفّر ٢١ مقطع، تقريباً ٥ دولار ونص في أول run.

## 06_resume_limits · 30.0 s

On screen: `make demo` replays (0–20 s; the edit at ~0:12, the kill and resume at ~0:16–0:20), then the limits
card (~0:21–0:30).

> وهذا make demo: أقتل العملية بـ SIGKILL وسط التوليد، وأرجّع.
> يكمل من آخر snapshot، واللي خلص يرجع من الـ cache ببلاش.
> وبصراحة، فيه حدود.
> الناقد يفوّته تغيّر صغير في الخامة، مثل غطا كريستال.
> ما عندي eval suite للحين، والنظام process واحد.
> بس كل قرار مسجّل، وكل خطأ له طريق رجعة.
