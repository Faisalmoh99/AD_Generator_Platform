# دليل المقابلة: Adsmith من الداخل

> مكتوب لك لتشرح النظام لمحمد عمر وتدافع عن كل جزء فيه. كل ما هنا مأخوذ من الكود الفعلي في هذا
> المستودع (آخر تحديث: 2026-11-05، commit بعد `3923fd4`). المصطلحات التقنية بالإنجليزية عمداً.
> إذا قال لك شيئاً لست متأكداً منه، الجواب الصادق: "خلني أفتح الملف" ثم افتح الملف المذكور هنا.

---

## (أ) النظام في دقيقة، ثم في خمس دقائق

### في دقيقة (بدون مصطلحات)

تكتب طلباً عادياً، بالعربي أو الإنجليزي أو باللهجة السعودية، مثل: «سوّ لي إعلان 12 ثانية لعطر رجالي
فاخر للشباب، فخم وجريء». النظام يفهم الطلب ويقترح ثلاث أفكار مختلفة للإعلان ثم يختار أقواها. بعدها
يقسم الإعلان إلى لقطات، ويكتب لكل لقطة وصفاً دقيقاً، ويولّد صورة ثابتة لكل لقطة ويراجعها قبل أن
يدفع على الفيديو. ثم يحوّل الصور المقبولة إلى مقاطع فيديو ويراجعها بالذكاء الاصطناعي، ويركّبها مع
موسيقى وبطاقة نهاية بالعربي.

إذا فشلت لقطة يحاول إصلاحها بطريقة محددة لكل نوع من الأخطاء: يعيد المحاولة، أو يعيد كتابة الوصف،
أو يستبدل اللقطة بلقطة أسلم. ولا تُسقط لقطةٌ فاشلة باقي الإعلان. وتستطيع بعد ذلك أن تقول «خل المشهد
الثاني أبطأ»، فيعيد توليد ذلك المشهد وحده.

### في خمس دقائق (تقني)

**الفكرة المحورية:** "العقل" (control flow) **ليس** الـ LLM. هو state machine صريح مكتوب في الكود،
والـ LLM يفكّر **داخل** كل مرحلة ويملأ حقول schema فقط.

1. **Control flow محدد سلفاً:** جدول انتقالات `TRANSITIONS` في `src/adsmith/agent/transitions.py`
   يربط كل مرحلة بدالة guard نقية. `next_stage(state, completed)` لا تقرأ ملفات ولا تستدعي LLM، ومختبرة
   بالكامل في `tests/unit/test_transitions.py`.
2. **State مكتوب بالأنواع (typed):** كل الحالة `RunState` (`domain/run.py`) عبارة عن Pydantic v2
   models بـ `extra="forbid"` وحقول محدودة.
3. **Nodes لا تكتب الحالة:** كل node (`agent/nodes/*.py`) دالة `async (state, ctx) -> NodeResult` ترجع
   **deltas** مكتوبة الأنواع (`agent/deltas.py`). الـ orchestrator يطبّقها في commit ذرّي واحد مع
   الأحداث وتكلفة الـ LLM، ثم يحسب المرحلة التالية (`agent/orchestrator.py`، دالة `step`).
4. **كل استدعاء LLM يرجع object مُتحقَّق منه:** `StructuredCaller` (`agent/structured.py`) يرسل JSON
   Schema، ويتحقق بـ Pydantic، ثم يشغّل `convert` لقواعد الدومين. إذا فشل التحقق يضيف رسالة إصلاح فيها
   الأخطاء الدقيقة إلى نفس المحادثة ويعيد المحاولة (`llm_max_repairs=2`). النتيجة إما object صالح أو
   `AgentError` مصنّف، ولا crash أبداً.
5. **المشاهد مستقلة:** `SceneStageRunner` (`agent/scenes.py`) يشغّل المشاهد بالتوازي
   (`scene_concurrency`)، وكل مشهد يمر بدورة keyframe → gate → clip → QA. كل تغيير على المشهد commit
   مستقل يتحقق من `scene.version`، فلا تُطبَّق نتيجة قديمة على مشهد تغيّر.
6. **Keyframe-first:** صورة ثابتة أولاً، يراجعها vision critic (الـ "gate")، ولا يُدفع على الفيديو إلا
   لصورة مقبولة (ADR-0003). الصورة تكلف 0.025–0.04 دولار والمقطع حوالي 0.25 دولار.
7. **Recovery بتصنيف:** كل خطأ من 8 أنواع (`FailureKind` في `domain/enums.py`: transient، rate_limited،
   content_policy، quality_rejected، invalid_output، budget_exceeded، permanent، exhausted)، ولكل نوع
   استراتيجية محدودة العدد (`agent/recovery/`). الـ SDK retries مُعطّلة (`max_retries=0`)، وطبقتنا تملك
   كل retry.
8. **Storage:** SQLite بوضع WAL. الحالة snapshots مضغوطة ومرقّمة بإصدار، والأحداث append-only بـ
   `seq` بلا فجوات (يعتمد عليه الـ SSE)، والوسائط content-addressed (`data/cas/`). وفوق ذلك generation
   cache، و wallet ledger عابر للـ runs (`data/account.db`).
9. **المال:** ميزانية لكل run بنمط reserve → settle/release، وسقف للمحفظة كلها يرفض أي استدعاء مدفوع
   قبل إرساله.
10. **Edits:** تحليل بالـ LLM ← حل المراجع (أي مشهد يقصد المستخدم) بالكود ← فحص التعارضات ← blast
    radius محسوب بدالة نقية ← إعادة توليد المتأثر فقط (`agent/edit/`).
11. **Zero-key by default:** بدون مفاتيح يعمل كل شيء بـ `ScriptedLLMProvider` و mock media (ffmpeg +
    Pillow) لعرض demo كامل في دقيقة. مع المفاتيح: Anthropic للـ LLM، و fal.ai للصور والفيديو والموسيقى.

**ما أضفناه في PLAN_V2 (صيغة المونتاج):** مكتبة صيغ (`config/formats.toml`)، ومفردات لقطات مغلقة
(`config/shot_vocabulary.toml`)، وتوجيه هوية المنتج لكل لقطة (`none/direct/composite/crop`)، وقاموس
ملابس ثقافي (`config/culture.toml`)، و peak window، و beat cut على الموسيقى، و best-of-2 للقطات
المفتاحية، وموسيقى مولّدة (Lyria 2)، و fallback تلقائي للقطة الفاشلة.

---

## (ب) طلب واحد من البداية للنهاية

الطلب: «سوّ لي إعلان 12 ثانية لعطر رجالي فاخر للشباب، فخم وجريء».
الأمر: `uv run adsmith run "<الطلب>" --auto` (الملف `src/adsmith/cli/main.py`، الدالة `run`).

> القاعدة: **LLM** = نموذج يفكّر ويملأ schema. **Code** = منطق حتمي يقرر.

### 0. إنشاء الـ run
- `Orchestrator.create_run` (`agent/orchestrator.py`) ينشئ `RunState` بـ `RunConfig`، وهي نسخة مجمّدة
  من الإعدادات (`run_config_from_settings`): نموذج الفيديو، والميزانية، والعتبات. الهدف أن يعيد
  الـ resume نفس الإعدادات.
- **Code:** الميزانية `BudgetLedger`. مع fal يُضبط `media_limit_usd` من رصيد المحفظة المتبقي.
- الخرج: snapshot رقم 1 وحدث `run created`.

### 1. understand (LLM: Haiku 5.5، effort low)
- الملف `agent/nodes/understand.py`، والقالب `prompts/understand/v5.md`، والـ schema `BriefDraft`.
- **الدخل:** النص الخام.
- **LLM:** يملأ الحقول: product، tone، target_audience، duration_s=12، language=ar، dialect،
  requested_format، physical_product، و normalized_request_en.
- **Code:**
  - `domain/dialect.py` يصحح اللهجة إذا قال الـ LLM "msa/other" ووجد الكود علامات خليجية مثل «أبي».
  - `resolve_format` (`domain/formats.py`) **يقرر الصيغة بالكود**: منتج مادي ولا صيغة في الطلب يعني
    `montage`. في الـ run هذا مكتوب حرفياً: "physical product and no format in the request".
- **الخرج:** delta `SetBrief`. التكلفة الفعلية في v2: 0.0007 دولار في 3.7 ثانية.

### 2. ideate (LLM: Sonnet 5.5، effort high)
- `agent/nodes/ideate.py` و `prompts/ideate/v5.md`.
- **LLM:** يكتب 3 أفكار وقائمة بالكليشيهات المعروفة في الفئة (مثل دخان العود). الـ prompt يتضمن
  `format_block` (قواعد المونتاج) و `video_profile` (ما يجيده Kling وما يفشل فيه).
- **Code:** `domain/similarity.py` يحسب التشابه بين الأفكار بـ character trigrams مع تطبيع عربي (حذف
  التشكيل والتطويل وتوحيد الألف و"ال"). إذا تجاوز التشابه الحد صار ذلك validation error، فيعود إلى
  repair loop (ADR-0009).
- **الخرج:** `SetConcepts`. في v2: «ثلاث طبقات من العتمة»، «رشّة وحدة»، «قبل الباب».

### 3. select (LLM: Sonnet، effort medium) ← **الكود يختار**
- `agent/nodes/select.py` و `domain/concept.py`.
- **LLM:** يعطي كل فكرة درجات من 1 إلى 10 على 6 معايير، وثلاثة flags (كليشيه؟ تُصوَّر في لقطة
  واحدة؟ المنتج لا يظهر إلا في النهاية؟).
- **Code:**
  - `ConceptScore.weighted()` بأوزان `JUDGE_WEIGHTS`: brief_fit 0.20، originality 0.20، producibility
    0.20، insight 0.15، product_hero 0.15، cultural_fit 0.10.
  - خصم ثابت: كليشيه 1.5−، لقطة غير قابلة للتصوير 1.5−، منتج متأخر 1.0−.
  - `winner()` تختار الأعلى، وعند التعادل: brief_fit ثم originality ثم الرقم الأصغر.
- في v2: «رشّة وحدة» بـ 7.30 مقابل 4.85 و 4.90.
- **وضع interactive:** لا يختار. `_after_select` تتوقف عند `awaiting_concept_choice` والإنسان يختار.

### 4. storyboard (LLM: Sonnet، effort high)
- `agent/nodes/storyboard.py` و `prompts/storyboard/v7.md`.
- **LLM:** يكتب اللقطات، ولكل لقطة `vocab` من المفردات المغلقة و shot spec (shot_type، camera، lens،
  lighting، composition، subject_action…).
- **Code (validators تعيد الـ LLM إلى الإصلاح):**
  - `grammar_problems` (`domain/formats.py`):
    - عدد اللقطات من 8 إلى 12، ومدة كل لقطة 1.0–1.5 ث.
    - **3 لقطات أحداث فيزيائية على الأقل**، والأخيرة `hero_push_in`.
    - شخص واحد كحد أقصى، بتأطير detail أو silhouette، وفي آخر لقطتين.
    - حركات الكاميرا من قائمة مسموحة.
  - `flags_from_vocab`: علامتا `shows_product` و `shows_character` تأتيان من المفردات وليس من النص
    (ADR-0018).
  - `hidden_person_problems`: شخص مذكور في لقطة يفترض أنها بلا أشخاص يُعاد للإصلاح.
  - `shot_lint.py`: فعل واحد لكل لقطة، ولا أشياء تمر بين الكاميرا والمنتج.
  - `vocab.for_model(video_model)` يحذف المفردات التي يفشل فيها هذا النموذج (`avoid_on`)، مثل لقطات
    الضوء وحده على Kling.
- مجموع المدد يجب أن يساوي الطلب: `Storyboard.from_draft` يتحقق.
- **الخرج:** `SetStoryboard`. في v2: 8 لقطات، من spray_from_nozzle إلى hero_push_in.

### 5. continuity (LLM: Sonnet)
- `agent/nodes/continuity.py` و `domain/continuity.py`.
- **LLM:** يكتب `ContinuityBible`، "مصدر الحقيقة البصري": المنتج (الشكل، المواد، الألوان، و
  branding_text فارغ)، والإضاءة، و color grade، والشخصية إن وجدت.
- **Code:**
  - `CultureLexicon.wardrobe_problems` (`domain/culture.py`) يرفض "mandarin collar" و "button
    placket" و kurta و kandura للثوب السعودي، بـ regex مع تجاهل الجمل المنفية.
  - `enforce_scene_flags`: الأعلام تُرفع فقط ولا تُخفض أبداً، وكل تجاوز يُسجَّل (ADR-0010).

### 6. prompt (LLM: Sonnet)
- `agent/nodes/prompt.py` (`write_scene_prompt`) و `prompts/prompt_writer/v9.md`.
- **LLM:** لكل مشهد `keyframe_prompt` و `motion_prompt` و `negative_prompt`، بالإنجليزية دائماً
  (القاعدة 9).
- **Code:**
  - `negative_contradictions` يرفض negative prompt يناقض اللقطة.
  - المشهد الأول يُكتب وحده لكي يكتب prompt cache، والباقي بالتوازي ويقرؤونه.
- بعدها يُكتب Production Pack (`agent/export.py`): ملفا storyboard.json و storyboard.md.

### 7. keyframe (stage مشاهد: `SceneStageRunner`)
1. **Product reference** (`_product_reference`): packshot واحد بـ FLUX dev، يُراجَع بـ gate
   (`qa_product_ref`)، ثم **يُقفل** ويكون مرجع الهوية لكل اللقطات. في v2 قُبل من أول محاولة بدرجة
   0.82.
2. لكل مشهد يقرر **Code** هوية المنتج بالدالة `identity_for(settings, scene)` من المفردات:
   - `none`: نص إلى صورة (FLUX dev)، مثل قطرة على ورقة.
   - `direct`: FLUX Kontext من الـ packshot (المنتج كما هو في مشهد جديد).
   - `composite`: صورة عادية ثم Kontext multi يبدّل المنتج (يد تمسك القارورة).
   - `crop`: **قصّ بالكود** من الـ packshot (`media/crop.py`)، ثم Kontext يعيد الإضاءة فقط. الهوية
     صحيحة بالبناء.
3. `MediaGateway.prepare/run` (`agent/media.py`) بالترتيب: cache ← chaos ← provider ← CAS ← cache.
   الـ request يُحجز من الميزانية قبل الإرسال.
4. **Keyframe gate** (`_keyframe_gate`، LLM vision: Sonnet، القالب `qa_keyframe`) يعطي verdict.
   **Code** يقرر النجاح (`QAVerdict.fail_reasons`):
   - score < 0.7 فشل.
   - أي hard check خاطئ فشل: product_visible، continuity_ok، text_free، brand_safe، alcohol_free.
   - إذا كان التكوين هو المواصفة (`composition_hard`) فمشكلة تكوين كبيرة تُفشل.
5. الرفض يقود إلى `_revise` (LLM rewrite بإصلاحات الناقد) ثم صورة جديدة. وبعد `keyframe_max_tries` مرات
   رفض يأتي **fallback تلقائي** (`fallback_for` + `_fall_back`): يستبدل اللقطة بمفردة أسلم، ويقرر ذلك
   واحد تلو الآخر تحت قفل. في v2: انفجار الماء صار bottle_on_rock، وشريط الماء صار cap_lift.

### 8. generate (مقاطع)
- الموسيقى أولاً: `agent/music.py` (`ensure_music`) يولّد مقطع Lyria 2 بـ 0.10 دولار من الـ brief
  وشكل الصيغة، قبل أن تستهلك المقاطع الميزانية.
- `_key_shot_takes`: **Code** يقرر best-of-2 للقطات المفتاحية (البطل، الرذاذ، الماء) إذا غطّى المتبقي
  من الميزانية لقطتين بعد حساب باقي المشاهد. في v2: الرذاذ 0.78/0.82، والبطل 0.82/0.82.
- Kling v3 Standard يرندر 3 ث على الأقل (`render_window`)، بينما الإعلان يأخذ 1.5 ث.
- **Peak window** (`media/frames.py`: `motion_profile` + `peak_window`): الكود يختار الـ 1.5 ث الأعلى
  حركة بدل بداية المقطع.
- Fal queue (`providers/media/fal.py`): يحفظ `request_id` **قبل** الانتظار، فإذا مات البرنامج يكمل
  الـ resume نفس الطلب ولا يدفع مرتين.

### 9. qa (LLM vision critic: Sonnet، `qa_critic` v8)
- `LLMCritic.review` (`agent/qa.py`): يرسل الـ keyframe وإطارات من **نافذة العرض الفعلية**.
- **Code** يقرر النجاح، ويتحقق أن كل check فاشل مفسَّر بـ issue من نفس الفئة، وأن `frame_index` يشير
  إلى صورة أُرسلت فعلاً.
- الرفض يقود إلى إعادة توليد (`qa_max_regenerations`). والـ **Code** يقرر هل تُعاد الـ keyframe أيضاً،
  بحسب فئات المشاكل وأماكنها.
- `_after_qa`: إذا بقي مشهد pending أو stale يعود إلى generate، وإلا ينتقل إلى assemble.

### 10. assemble (ffmpeg، بلا LLM إلا لنص بطاقة النهاية إن لزم)
- `agent/nodes/assemble.py`:
  - `_beat_cut` يكتشف الإيقاع (`media/beats.py`: spectral flux + comb scoring)، ويحرك كل قطع إلى
    أقرب beat في حدود ±0.2 ث (`snap_cuts`).
  - `normalise_clip` يقص النافذة.
  - `endcard_copy` (LLM: Haiku) يقترح اسماً وشعاراً **وهميين** إذا لم يعطهما المستخدم، ويُعلَّمان
    PLACEHOLDER، مع denylist للعلامات التجارية الحقيقية.
  - `render_endcard` يشكّل العربي بـ arabic_reshaper و python-bidi.
  - concat ثم موسيقى ثم فحص المدة بـ ffprobe (تسامح ±0.15 ث).
- `_after_assemble`: `done` إذا قُبلت كل المشاهد، وإلا `done_partial`.

### 11. بعد التسليم: تعديل بالعربي
`adsmith edit <run> "خل المشهد الثاني أبطأ"` يمر بـ `EditService` (`agent/edit/service.py`):
1. `edit_parser` (LLM) يحوّل الجملة إلى `ScenePatchDraft`.
2. `refs.py` (Code) يحوّل «الثاني» إلى scene id.
3. فحص التعارضات.
4. `blast.py` (Code): تغيير حركة فقط يعني depth=`clip`، فتبقى الـ keyframe وتُعاد الحركة وحدها.
5. commit ذري واحد، وباقي المشاهد بـ 0 دولار.

---

## (ج) كل مجلد وملف مهم

| المسار | ببساطة | تقنياً | لماذا موجود |
|---|---|---|---|
| `CLAUDE.md` | دستور المشروع | 9 قواعد معمارية + معايير الكود + workflow (make check قبل كل commit) | يضمن أن أي شخص (أو Claude Code) يلتزم بنفس القواعد |
| `Makefile` | اختصارات | `setup`, `lint`, `typecheck`, `test`, `check`, `serve`, `demo` | `make check` هو بوابة الجودة الوحيدة |
| `pyproject.toml` | تعريف المشروع | Python 3.12، deps (pydantic, anthropic, httpx, fastapi, typer, rich, pillow, numpy…)، ruff + mypy strict + pytest | مصدر واحد للاعتمادات والأدوات |
| `uv.lock` | قفل النسخ | نسخ دقيقة لكل حزمة | بيئة قابلة للتكرار |
| `config/models.toml` | كتالوج النماذج والأسعار | profiles (fast/balanced/quality) لكل node، أسعار Anthropic، endpoints fal مع تعليقات التحقق | القاعدة 4: لا model IDs ولا أسعار خارج config |
| `config/formats.toml` | صيغ الإعلان | grammar لـ montage / single_take / narrative | الصيغة بيانات لا كود |
| `config/shot_vocabulary.toml` | قائمة اللقطات المسموحة | 16 مفردة: framing، identity_mode، key_shot، physical_event، fallback، avoid_on… | يقيّد الـ LLM بلقطات ينجح فيها نموذج الفيديو |
| `config/culture.toml` | قاموس الملابس | وصف الثوب والغترة السعوديين + regex lints | منع "kurta" بدل الثوب |
| `config/chaos.toml` | حقن أعطال | seed + احتمالات | demo حي للـ recovery قابل للتكرار |
| `config/real_runs.toml` | معرض التشغيلات الحقيقية | عناصر تعرضها الواجهة | تبويب Real runs |
| `prompts/<node>/vN.md` | كل الـ prompts | Jinja2 + frontmatter (name, version, output_schema)؛ SYSTEM بلا متغيرات | القاعدة 5: لا prompts داخل Python، وأي تغيير يرفع الإصدار، والـ SYSTEM الثابت يجعل prompt caching ممكناً |
| `src/adsmith/domain/` | "ما هو" النظام | Pydantic models: Brief، Concept، Storyboard، Scene (FSM للمشهد)، QAVerdict، Budget، Events، Formats، Vocabulary | الحالة typed ومتحقق منها؛ قواعد الدومين هنا لا في الـ nodes |
| `domain/run.py` | جذر الحالة | `RunState` + `RunConfig` + القواعد العابرة (storyboard ينتمي للفكرة المختارة…) | aggregate root يُحفظ كـ snapshot |
| `domain/scene.py` | حالة المشهد | status FSM (pending→generating→qa_review→approved/failed/stale)، attempts، versions | عزل المشاهد وأمان الإصدارات |
| `domain/budget.py` | الميزانية | reserve/settle/release، media cap، حدود المحاولات | المال لا يُصرف بدون حجز |
| `src/adsmith/agent/` | "كيف" يعمل | orchestrator، transitions، nodes، scenes، qa، media gateway، recovery، edit، music | العقل والتنفيذ |
| `agent/transitions.py` | خريطة المراحل | `TRANSITIONS` + `next_stage` نقية | القاعدة 1 |
| `agent/orchestrator.py` | المشغّل | `step`، `resume`، `run_until_blocked`، قفل لكل run | commit ذري لكل مرحلة |
| `agent/nodes/` | المراحل التي فيها LLM | understand, ideate, select, storyboard, continuity, prompt, rewrite, assemble | كل node يرجع deltas فقط |
| `agent/scenes.py` | منفّذ المشاهد | keyframe → gate → clip → QA، best-of-N، fallback، product reference | أكبر ملف (1400 سطر)، فيه منطق التوازي والعزل |
| `agent/structured.py` | استدعاء LLM آمن | schema → validate → convert → repair loop | القاعدة 7 |
| `agent/recovery/` | التعافي | `errors.py` (التصنيف)، `classifier.py` (أخطاء SDK إلى تصنيف)، `backoff.py` (full jitter) | القاعدة 6 |
| `agent/media.py` | بوابة الوسائط | cache → chaos → provider → CAS؛ توجيه الهوية | كل providers تتصرف بنفس الطريقة |
| `agent/edit/` | التعديلات | service، refs (عربي/إنجليزي)، blast (نقي) | تعديل بلا إعادة كل شيء |
| `agent/music.py` | الموسيقى | Lyria 2، cache، ميزانية audio بسقف 0.15 دولار | beat cut بلا تحميل ملفات |
| `agent/commands.py` | أوامر الإنسان | choose، approve، budget، edit_scene_spec، accept، reassemble، retry | نفس الأوامر للـ CLI والـ API |
| `src/adsmith/providers/` | الاتصال بالخارج | `llm/` (anthropic, openai, scripted)، `media/` (fal, mock, chaos)، `adapters/` | transport فقط؛ لا تحقق ولا retry |
| `providers/media/fal.py` | fal.ai | REST queue يدوياً (submit/poll/result) + wallet ledger | `fal-client` يعيد الـ submit داخلياً، وقد يدفع مرتين |
| `providers/llm/scripted.py` | LLM وهمي | fixtures JSON لكل قالب | اختبارات و demo بلا مفاتيح |
| `providers/adapters/base.py` | محوّلات prompt | ScenePrompt إلى payload كل نموذج (مدة، negative…) | باقي الكود لا يعرف الفروقات بين النماذج |
| `src/adsmith/media/` | معالجة وسائط حتمية | ffmpeg، إطارات + peak window، beats، crop، endcard | بلا نماذج، كلها قابلة للاختبار |
| `src/adsmith/storage/` | التخزين | `db.py` (SQLite WAL)، `repo.py` (snapshots + update ذري)، `events.py` (seq)، `artifacts.py` (CAS)، `cache.py`، `account.py` (المحفظة) | durability + resume + SSE |
| `src/adsmith/api/` | الواجهة | FastAPI + SSE + صفحة static (vanilla JS + SVG): رسم الـ state machine حياً (LLM مقابل code لكل مرحلة)، swimlane لكل مشهد بالحلقات (retry، rewrite، fallback)، خط حرق الميزانية، وسجل قرارات بلغة بسيطة | نفس الأوامر بواجهة ويب، وتُظهر "العقل" وهو يعمل |
| `src/adsmith/cli/` | سطر الأوامر | Typer + Rich؛ `demo.py` (عرض بلا مفاتيح، فيه SIGKILL + resume) | الواجهة الأساسية |
| `tests/` | الاختبارات | 46 ملفاً: `unit/` و `integration/`، helpers، fixtures | 584 ناجحة، و2 live متخطاة |
| `scripts/` | أدوات المطور | contact_sheet، decisions_report، run_report، fal_scene، qa_calibrate، gate_recalibrate، keyframe_ab، ui_screenshots… | تشغيل جزئي، معايرة بلا صرف، تقارير |
| `docs/adr/` | سجل القرارات | 21 ADR (Context/Decision/Alternatives/Consequences) | لماذا الأشياء كما هي |
| `docs/PLAN.md`، `PLAN_V2.md` | الخطة | المعمارية، ثم صيغة المونتاج | |
| `docs/PROGRESS.md` | سجل البناء | ما بُني، والقرارات، والنتائج، لكل مرحلة | |
| `docs/HANDOFF.md` | التسليم | الحالة الحالية، الدروس، المال | نقطة البداية لأي جلسة |
| `docs/MORE_TIME.md` | ما لم يُبنَ | 16 بنداً مرتبة | الصراحة بشأن الحدود |
| `docs/media/`، `docs/screenshots/` | مخرجات حقيقية | الفيديوهات المعتمدة، contact sheets، تقارير القرارات | دليل بصري |
| `data/` (gitignored) | بيانات التشغيل | `adsmith.db`، `account.db`، `cas/` (352MB)، `runs/` | لا تُرفع للـ git |
| `evals/` | فارغ | كان مخططاً (M12.5) وأُلغي | اعترف بهذا: الـ evaluation الحالية هي معايرة الناقد على مخرجات حقيقية مخزّنة (`scripts/qa_calibrate.py`، `gate_recalibrate.py`)، وليست eval suite |

---

## (د) كل اختيار تقني: ماذا اخترنا، والبدائل، ومتى يكون البديل أفضل

| القرار | اخترنا | البدائل | لماذا | متى البديل أفضل |
|---|---|---|---|---|
| Orchestration | **FSM مخصص** (جدول transitions، ADR-0001) | LangGraph، CrewAI، AutoGen | control flow بيانات قابلة للاختبار؛ لا يقرر الـ LLM المسار؛ commit ذري وresume من أي snapshot؛ لا dependency كبيرة | LangGraph عند الحاجة لـ graph ديناميكي يقرر فيه الـ LLM الفروع، أو checkpointers جاهزة وفريق يعرفها. CrewAI لمهام بحثية مفتوحة متعددة الأدوار |
| LLM SDK | **SDK مباشر** (anthropic، openai) | LangChain، LiteLLM | نحتاج raw output للـ repair، والتحكم في الـ retries (القاعدة 6)، و prompt caching breakpoints، و structured outputs. LangChain يخفي هذا | LiteLLM للتبديل السريع بين مزودين كثيرين؛ LangChain لـ RAG سريع جاهز |
| Database | **SQLite (WAL)** | Postgres، Redis | ملف واحد، zero-ops؛ `BEGIN IMMEDIATE` يعطي كاتباً واحداً، وهذا ما يجعل `seq` بلا فجوات؛ يكفي لعملية واحدة | Postgres لعدة workers وأجهزة، أو concurrent writers كثيرين، أو multi-tenant |
| Concurrency | **asyncio** + Semaphore | Celery، RQ، Temporal | التوليد I/O-bound؛ لا broker؛ crash-safety يأتي من snapshots + request_id محفوظ | Temporal/Celery عند التوزيع على أجهزة، أو jobs تستمر ساعات، أو آلاف المستخدمين |
| Media provider | **fal.ai** | Replicate، Runway، Higgsfield، APIs مباشرة | نماذج كثيرة (FLUX، Kontext، Kling، LTX، Lyria) خلف queue API واحدة؛ أسعار شفافة وpricing API | Higgsfield: Kling 3.0 Std بـ 0.063 دولار/ث، لكن بلا Kontext (MORE_TIME 16). Runway لجودة Gen-4 وأدوات المحرر. API مباشرة (Google Veo) للحجم الكبير |
| Video model | **Kling v3 Standard** (Pro سابقاً) | Veo 3.1، Seedance 2، LTX-2.3، Runway | يحافظ على المنتج من start frame؛ parallax حقيقي؛ 0.084 دولار/ث (الفعلي ~0.066) | LTX (0.06 دولار/ث، 35 ث): للتكرار السريع، لكنه يحوّل الظلال لستائر. Veo: صوت أصلي ونص أفضل لكنه أغلى. Seedance: حركة أقوى بسعر 0.30 دولار/ث |
| Image model | **FLUX.1 dev** للنص إلى صورة | FLUX.2، Nano Banana، Imagen، Seedream | رخيص (0.025 دولار/MP) وجيد للمنتج | FLUX.2 flex للالتزام بالتخطيط (مُعرّف في الكتالوج)؛ Nano Banana لتعديلات متعددة الصور |
| Product identity | **FLUX Kontext** (direct/multi) + **crop بالكود** | IP-Adapter، LoRA/DreamBooth | لا تدريب، صورة واحدة كمرجع؛ 0.04 دولار؛ الـ crop يضمن الهوية في الماكرو | LoRA عند منتج حقيقي بصور كثيرة وحملة طويلة (تدريب لمرة، ثم هوية ثابتة جداً). IP-Adapter عند استضافة النموذج ذاتياً |
| LLM per node | **Haiku** للفهم ونص البطاقة؛ **Sonnet** للأفكار والستوريبورد والنقد؛ **Opus** في profile "quality" | Opus في كل شيء | ADR-0007: Haiku low effort يأخذ 3.5 ث بنفس الجودة؛ ideate على medium رجع للكليشيهات، لذلك high؛ الناقد يحتاج vision جيدة بسعر معقول | Opus للـ storyboard/ideate عندما تكون الجودة الإبداعية أهم من التكلفة (profile `quality`) |
| UI | **FastAPI + vanilla JS + SVG** | Streamlit، React/Next | بلا build step؛ نفس الـ runtime؛ SSE أصلي في المتصفح | React لتطبيق منتج حقيقي فيه حالة معقدة وفريق frontend؛ Streamlit لنموذج داخلي سريع (لكنه سيئ مع streaming events) |
| Live updates | **SSE** | WebSockets، polling | اتجاه واحد (server→client)؛ `id: seq` + `Last-Event-ID` (أو `?after=`) + `seq` بلا فجوات = resume بلا فقد؛ يمر عبر proxies | WebSockets عند الحاجة لاتصال ثنائي الاتجاه منخفض الكمون (تعاون حي) |
| Package manager | **uv** | pip، poetry | أسرع بكثير؛ lockfile؛ يدير Python نفسه | poetry إذا كان الفريق عليه أصلاً |
| Validation | **Pydantic v2** + mypy strict | dataclasses، attrs | JSON Schema للـ LLM من نفس الموديل؛ تحقق runtime | — |
| Music | **Lyria 2** (fal، 0.10 دولار/30 ث) | Stable Audio 2.5 (0.20)، ElevenLabs (0.60/دقيقة)، MiniMax (يتطلب كلمات) | تحت سقف 0.15 دولار؛ instrumental بـ negative prompt | ElevenLabs عند الحاجة لطول دقيق بالـ ms |
| Beat detection | **numpy** (spectral flux + comb) | librosa، madmom | لا dependency ثقيلة؛ 20ms دقة على click track | librosa/madmom لموسيقى معقدة (تغير tempo) |

---

## (هـ) معالجة الأخطاء بأمثلة حقيقية

1. **الستارة الشفافة (curtain veil):**
   - **ما حدث:** "A curtain shadow drifts across the bottle" فسّرها LTX حرفياً، فظهرت ستارة شفافة
     فوق المنتج.
   - **كيف عولج:** الناقد رفض بدرجة 0.50، فأعاد `rewrite_prompt` كتابة الـ motion prompt من إصلاحات
     الناقد مع **إبقاء الـ keyframe**، لأن الكود قرر أن المشكلة في الحركة. المقطع الثاني نظيف
     (`docs/media/curtain_*`).
   - **القاعدة الناتجة:** النماذج تأخذ الأسماء حرفياً، فلا شيء يمر بين الكاميرا والمنتج
     (`shot_lint.py`).
2. **معايرة الناقد (3 محاولات):**
   - **ما حدث:** أولاً كان متساهلاً جداً (مقطع فيه مشكلتان كبيرتان نجح بـ 0.72)، ثم متشدداً جداً
     (10 من 10 keyframes رُفضت لتفاصيل الإخراج).
   - **الحل:** hard/soft split: الهوية والنص والأمان والكحول **تُفشل**؛ والإضاءة والتكوين تخفض
     الدرجة فقط، والكود يحدها عند "minor".
   - **النتيجة:** إعادة حكم 14 keyframe مخزنة أعطت 12/14 صحيحة. والقرار النهائي بالكود دائماً
     (`QAVerdict.passed`).
3. **Keyframe gate:**
   - **الملاحظة:** أغلب الرفض كان بسبب الصورة الثابتة نفسها، ولا يُكتشف إلا بعد دفع ثمن المقطع.
   - **النتيجة:** المراجعة قبل التحريك أوقفت 21 keyframe، أي حوالي 5.60 دولار من Kling في أول run
     كامل فقط.
4. **النص المخترع وغطاء القارورة (hero attempt، خسارة 2.24 دولار):**
   - **ما حدث:** الـ gate قبل غطاءً أسود على أنه "minor" بينما الوصف ذهبي، فحرّك best-of-2 القارورة
     الخطأ. وصارت "blank label plate" حروفاً مخترعة.
   - **الحل:** أي اختلاف في المنتج عند الـ gate صار hard بالكود، ووصف المنتج بلا أسطح ملصقات
     (`branding_text` فارغ). أُعيد الحكم على نفس الـ keyframe فانخفض من 0.78 إلى 0.45.
5. **الكُرتة بدل الثوب:**
   - **ما حدث:** الـ bible وصف "mandarin collar and a visible button placket"، وهذه kurta من جنوب
     آسيا.
   - **الحل:** `config/culture.toml` + `CultureLexicon.wardrobe_problems`، مع regex يتجاهل الجمل
     المنفية ("no visible buttons" تمر). يُطبَّق في continuity (repair) وفي prompt writer وفي
     keyframe gate.
6. **Fallbacks:**
   - **في v1:**
     - لقطتا ماء فشلتا مرتين، فاستُبدلتا تلقائياً، لكن الاثنتين اختارتا bottle_on_rock في نفس
       اللحظة (race).
     - ثلاث لقطات فشلت بلا بديل لأن كل السلسلة مستخدمة.
   - **الإصلاح:**
     - قفل `asyncio.Lock`.
     - الترتيب: بديل غير مستخدم من السلسلة، ثم لقطة منتج آمنة غير مستخدمة، ثم تكرار. لا ثقب أبداً.
     - تجاهل `avoid_on` والمفردات التي فشلت فيها لقطة أخرى.
   - **في v2:** حدث استبدالان صحيحان.
7. **Crash/resume:**
   - **ما يحصل عند الإرسال:** `FalQueue` يحفظ `RemoteRequest` (`request_id`، `status_url`) في الحالة
     **قبل** أن يبدأ الـ polling.
   - **ما يحصل عند `resume`:** يحرر الـ leases المعلقة، والمحاولة المستأنفة تبني نفس الـ request
     (نفس seed ونفس hash)، فتجد الطلب وتكمله بدل إرسال طلب جديد مدفوع (`tests/integration/test_fal_resume.py`).
   - **في الـ demo:** `make demo` يقتل عملية بـ SIGKILL أثناء التوليد ثم يكمل.
8. **الميزانية:**
   - **مستويان:**
     - `BudgetLedger` لكل run: reserve قبل الاستدعاء، settle بعده، release عند الفشل.
     - `SpendAccount` للمحفظة: `BEGIN IMMEDIATE`، فلا تتجاوز عمليتان السقف معاً.
   - **أمثلة حقيقية:**
     - في الـ majlis ad رفض الحارس محاولة ثالثة لمشهد الدخان.
     - في v2 رُفضت إعادة cap_lift لأن الميزانية نفدت، وانتهى الـ run بـ done_partial بدل الانهيار.
     - في v2 رُفضت الموسيقى لأنها كانت تُحجز آخراً. **الإصلاح:** الموسيقى تُولَّد أول مرحلة generate.
9. **Schema errors من الـ LLM:**
   - **ما حدث:** "Schema is too complex" (400) من Anthropic في STOP 1.
   - **الحل:** الحقول الجديدة صارت required literals، وفُحصت كل الـ schemas بـ Haiku.
   - **في الحالة العادية:** الـ repair loop يصلح مخرجات غير صالحة (في v2: إصلاحان، للـ storyboard
     والـ prompt writer).

---

## (و) نقاط الضعف بصراحة، وماذا أفعل بوقت أكثر

**نقاط الضعف:**
- **الناقد ليس ground truth.**
  - المعايرة 12/14 على عينة صغيرة.
  - في v2 قبل غطاءً كريستالياً شفافاً (0.78) بينما المرجع معدني.
  - ظل الغطاء يتغير بين gunmetal و champagne ولم يُكتشف.
  - التفاصيل الصغيرة للمواد نقطة عمياء.
- **لا eval suite.** `evals/` فارغ. لا يوجد مقياس آلي متكرر لجودة الإعلان. المعايرة يدوية على مخرجات
  مخزنة.
- **اللقطات الحدثية هشة.** شريط الماء والانفجار فشلا في كل run، ويعتمد النجاح على قلة من المفردات
  "المثبتة".
- **Single process.** SQLite + asyncio. لا توزيع ولا multi-tenant ولا auth.
- **Human-in-the-loop في الواجهة فقط عند بوابتين.** لا مراجعة بشرية للقطة وسط التوليد، إلا عبر
  `accept`.
- **التكلفة الفعلية تختلف عن الدفتر.** نسجّل بسعر الصفحة (0.084 دولار/ث) والفاتورة حوالي 0.066.
  الدفتر يبالغ عمداً، لكنه ليس مطابقاً.
- **OpenAI provider غير مجرّب live.**
- **نصوص بطاقة النهاية وهمية** عند غياب العلامة التجارية، ومعلّمة كـ placeholder.

**بوقت أكثر (من `docs/MORE_TIME.md`):**
1. **Eval suite حقيقية.** مجموعة ذهبية من keyframes ومقاطع موسومة يدوياً، دقة الناقد لكل فئة، و
   regression عند تغيير قالب.
2. **Cross-shot identity check.** مقارنة كل لقطة بالـ packshot بـ embedding أو crop-level vision، لصيد
   الغطاء الكريستالي.
3. **Escalation ladder.** تبسيط المواصفة قبل الـ fallback (MORE_TIME 15).
4. **Higgsfield كمزود ثانٍ.** بعد probe بحوالي 0.20 دولار أن Qwen Image 3 أو Grok Image 2 يحافظ على
   الهوية (MORE_TIME 16).
5. **Postgres + job queue** عند تعدد المستخدمين، و auth، وحصص لكل مستخدم.
6. **LoRA للمنتج** للعلامات الحقيقية ذات الحملات الطويلة.

---

## (ز) 44 سؤالاً متوقعاً مع إجابات نموذجية

### المعمارية والـ "agent"

**1. أليس هذا مجرد pipeline؟**
البنية الكبرى pipeline عن قصد، لأن إنتاج الإعلان له ترتيب طبيعي. لكنه ليس خطياً: فيه حلقات
(qa → generate)، وبوابات بشرية، وفروع للتعافي لكل نوع خطأ، و fallback يغيّر الـ storyboard نفسه، و
edits تحسب أثرها وتعيد جزءاً فقط. الفرق عن pipeline عادي أن كل قرار مسجّل ومبرر وقابل للاستئناف.

**2. لماذا لا تترك الـ agent يقرر الخطوة التالية؟**
لثلاثة أسباب:
- **المال:** خطوة خاطئة تعني فيديو مدفوعاً.
- **الاختبار:** جدول نقي أختبر كل فرع فيه في `test_transitions.py`.
- **الاستئناف:** `next_stage(state)` دالة من الحالة، فأي snapshot يكفي للاستئناف.

الـ LLM يقرر ما يجيده: الإبداع والتقييم والإصلاح. والكود يقرر ما يجب أن يكون حتمياً: المسار
والمال والنجاح والفشل.

**3. إذن كيف هو agent؟**
- يدرك: الـ vision critic يرى مخرجاته.
- يقرر داخل حدود: يختار المفردات ويكتب الـ prompts ويصلحها من النقد.
- يتصرف في العالم: يستدعي نماذج مدفوعة.
- يتعافى من فشله بنفسه: يعيد الكتابة، ويستبدل اللقطة، ويرفض قبل الدفع.

الاستقلالية موزعة: إبداعية للـ LLM، وتشغيلية للكود. هذا agent بـ "explicit brain"، وليس ReAct loop.

**4. ما الفرق بين ما يقرره الـ LLM وما يقرره الكود؟**
- **الـ LLM يملأ schemas:** brief، أفكار، درجات، لقطات، bible، prompts، نقد.
- **الكود يقرر:**
  - الصيغة (`resolve_format`).
  - الفائز (`weighted()`).
  - نجاح الـ QA (`fail_reasons`).
  - هوية المنتج لكل لقطة (`identity_for`).
  - هل تُعاد الـ keyframe.
  - الـ fallback.
  - best-of-2.
  - الـ blast radius.
  - الـ beat cut.

**5. لماذا لم تستخدم LangGraph؟**
- **التبرير:** ADR-0001. أردت control flow كبيانات مع commit ذري لكل خطوة، ومشاهد متوازية بإصدارات،
  و resume من snapshot. LangGraph يقدّم graph و checkpointer، لكن قواعدي (deltas مكتوبة، ونتائج قديمة
  تُرفض بالإصدار، وحجز المال) كنت سأكتبها فوقه على أي حال.
- **متى أختاره:** إذا احتجت فروعاً ديناميكية يقررها النموذج، أو فريقاً يعرفه.

**6. ماذا لو تعطل البرنامج في منتصف توليد فيديو مدفوع؟**
- الـ `request_id` محفوظ في الحالة قبل الـ polling.
- `adsmith resume` يحرر الـ leases المعلقة، والمحاولة تبني نفس الطلب (نفس hash)، فتجد الطلب عند fal
  وتكمل.
- إذا وجده fal مفقوداً أو فاشلاً يُعاد إرساله مرة واحدة.
- المخرجات المكتملة cache hits بـ 0 دولار.

**7. كيف تمنع تطبيق نتيجة قديمة على مشهد تغيّر؟**
كل commit للمشهد يمر بـ `_same_version`. إذا تغيّر `scene.version` (تعديل أو fallback) يُرفع
`StaleWriteError` وتُرمى النتيجة ولا تُطبَّق خطأً. والـ orchestrator يرمي نتيجة node كاملة إذا تحرك
الـ run أثناءها.

**8. كيف تضمن أن مشهداً فاشلاً لا يكسر الباقي؟**
كل مشهد task مستقلة بـ commits مستقلة. الفشل يحدّث ذلك المشهد فقط (`_fail_scene`)، و`assemble` يتخطاه
(`skip`) أو يضع slate، والنتيجة `done_partial`. هذا مختبر بـ fail plans.

### الجودة والناقد

**9. كيف تعرف أن الناقد على حق؟**
لا أفترض ذلك:
- **القرار بالكود:** الـ LLM يملأ الحقول والكود يحسب النجاح؛ كل check فاشل يجب أن يُفسَّر بـ issue
  من فئته، وكل `frame_index` يجب أن يشير لصورة أُرسلت.
- **المعايرة:** على مخرجات حقيقية مخزنة بلا صرف (`qa_calibrate.py`، `gate_recalibrate.py`)، والنتيجة
  12/14.
- **مراجعة بشرية:** أراجع كل contact sheet بعيني.
- **الحدود:** هو أخطأ فعلاً (الغطاء الكريستالي في v2)، والحل القادم eval suite و cross-shot identity
  check.

**10. لماذا vision LLM وليس مقاييس مثل CLIP score؟**
- **ما فشل فيه CLIP:** الأخطاء التي رأيناها دلالية: ستارة تغطي المنتج، نص مخترع، غطاء بلون خطأ، شكل
  يشبه زجاجة خمر. CLIP لا يكتشف معظمها ولا يعطي إصلاحاً قابلاً للتنفيذ.
- **ما يقدمه الناقد:** issue + fix، فتغذي إعادة الكتابة مباشرة.
- **أين يفيد CLIP:** كإشارة إضافية رخيصة للهوية، وهي من بنود "بوقت أكثر".

**11. ما الـ hard checks؟**
`product_visible`، `continuity_ok`، `text_free`، `brand_safe`، `alcohol_free` (`HARD_CHECKS` في
`domain/qa.py`). `alcohol_free` جاء من run حقيقي: قارورة دائرية بغطاء أسود طويل بدت كزجاجة ويسكي،
وهذا غير مقبول للسوق السعودي.

**12. لماذا threshold 0.7؟**
- **الرقم نفسه:** قيمة افتراضية معقولة في config (`ADSMITH_QA_THRESHOLD`)، وليس نتيجة معايرة دقيقة.
  لا تدّعِ أكثر من ذلك.
- **ما عايرناه فعلاً:** قاعدة النجاح (ADR-0016). مقطع حقيقي نجح بـ 0.72 وفيه مشكلتان كبيرتان، فصار
  النجاح = score ≥ threshold **و** كل hard checks صحيحة **و** لا blocker. والفئات الـ soft يحدها الكود
  عند "minor".
- **حدود الناقد:** غير حتمي؛ نفس المقطع أخذ 0.72 ثم 0.62 في ADR-0016. الحل المقترح للحالات القريبة
  من العتبة (±0.1): حكمان والقرار بالاتفاق.

**13. ماذا لو "هلوس" الـ LLM مشهداً غير ممكن؟**
عدة طبقات قبل أي صرف:
- **قبل الصورة:**
  - المفردات المغلقة: لا يختار إلا لقطات معروفة.
  - `avoid_on`: لا لقطات يفشل فيها النموذج.
  - `shot_lint`: فعل واحد، ولا أشياء بين الكاميرا والمنتج.
  - `video_profile` في الـ prompt: ما يجيده Kling وما يفشل فيه.
  - الحكم يخصم 1.5 لفكرة لا تُصوَّر في لقطة واحدة.
- **بعد الصورة:** إذا وصل مشهد مستحيل للصورة يرفضه الـ gate، ثم يأتي fallback إلى لقطة أسلم.

**14. ماذا لو اخترع الـ LLM علامة تجارية حقيقية؟**
`real_brand_in` (`domain/endcard.py`) denylist حتمية. إذا ظهرت علامة حقيقية في اسم أو شعار مخترع
يصير validation error ثم repair. والنص المخترع يُعلَّم PLACEHOLDER في الـ trace والـ Production Pack.

### التكلفة والأداء

**15. كم تكلفة الإعلان؟**
- **v2 (8 لقطات، 12.5 ث):**
  - fal في الدفتر 3.09 دولار: صور 0.57 ومقاطع 2.52.
  - الفعلي حوالي 2.55 دولار، لأن Kling يُفوتر حوالي 0.066 دولار/ث مقابل 0.084 في الصفحة.
  - Anthropic حوالي 0.77 دولار.
- **المجموع:** حوالي 3.3 إلى 3.9 دولار للإعلان.
- **الموسيقى:** 0.10 دولار.
- **الوقت:** حوالي 9 دقائق (526 ث).

**16. ما أكبر بند تكلفة؟ وكيف تخفضه؟**
- **البند الأكبر:** الفيديو، حوالي 75%. Kling يرندر 3 ث كحد أدنى ونحن نستخدم 1.5 ث.
- **ما يخفضه حالياً:**
  - الـ gate يمنع الدفع لصور سيئة.
  - الـ cache.
  - best-of-2 فقط عند توفر الميزانية.
- **خيارات مستقبلية:** LTX للمسودات، Higgsfield بسعر 0.063، و`qa_max_regenerations=0` لإعلان بلقطة
  واحدة لكل مشهد.

**17. كيف يتوسع لـ 1000 مستخدم؟**
- **ما يبقى كما هو:** الـ brain والـ domain والـ recovery والـ providers.
- **ما يتغير:**
  1. Postgres بدل SQLite. الـ repository pattern معزول، و`update(mutation)` يتحول إلى
     `SELECT … FOR UPDATE`.
  2. Job queue (Temporal أو Celery) بدل asyncio في عملية واحدة. كل `step` يصبح activity، وهو
     idempotent أصلاً.
  3. Object storage (S3) للـ CAS. التصميم content-addressed جاهز لذلك.
  4. Rate limits و concurrency لكل مزود. Higgsfield مثلاً يسمح بـ 2 إلى 40 job متزامنة حسب الرصيد.
  5. Auth، وحصص مالية لكل مستخدم (الـ wallet ledger موجود كنمط)، و multi-tenant.
- **عنق الزجاجة الحقيقي:** المال وحدود المزودين، لا الكود.

**18. لماذا asyncio وليس threads أو processes؟**
كل العمل انتظار شبكة. asyncio مع Semaphore يكفي ويحافظ على ترتيب الكتابة. والكتابة نفسها محمية بـ
RLock و `BEGIN IMMEDIATE`.

**19. كيف يعمل prompt caching عندك؟**
- القسم SYSTEM ثابت بلا متغيرات، ويُتحقق من ذلك عند التحميل في `agent/prompts.py`. لذلك الـ prefix
  متطابق بايت ببايت.
- قسم CONTEXT (الـ bible والـ storyboard) breakpoint ثانٍ.
- prompt writer يكتب المشهد الأول وحده (cache write) والباقي يقرأون.
- الأرقام مسجلة في `LLMCallRecord`، مثل "4092 cached".

### القرارات التقنية

**20. لماذا SQLite؟ أليس "لعبة"؟**
- لعملية واحدة هو قاعدة بيانات حقيقية، ACID مع WAL.
- `BEGIN IMMEDIATE` يعطيني كاتباً واحداً، وهذا بالضبط ما يجعل `seq` بلا فجوات وما يعتمد عليه SSE
  resume.
- Postgres يضيف تشغيلاً بلا فائدة الآن. وعند التوسع أنقله (انظر 17).

**21. لماذا SSE وليس WebSocket؟**
- البيانات في اتجاه واحد.
- كل حدث يُرسل بـ `id: <seq>`. `EventSource` يعيد الاتصال تلقائياً مع `Last-Event-ID`، والسيرفر
  يكمل من بعده (أو من `?after=<seq>`). ومع `seq` الخالي من الفجوات لا يُفقد ولا يتكرر حدث.
- يعمل عبر HTTP العادي والـ proxies.

**22. لماذا لا تستخدم `fal-client`؟**
- يعيد المحاولة داخلياً، حتى على الـ submit.
- إعادة submit قد تبدأ توليداً مدفوعاً ثانياً، وتكسر القاعدة 6.
- لذلك كتبت `FalQueue`: طلب HTTP واحد لكل خطوة، وكل خطأ مصنّف، و request_id يُحفظ قبل الانتظار
  (`providers/media/fal.py`).

**23. لماذا Kling وليس Veo؟**
- **قسنا Kling فعلياً:** يحافظ على المنتج من الإطار الأول، وفيه parallax حقيقي.
- **قسنا LTX:** أرخص وأسرع، لكنه حوّل ظلاً لستارة.
- **Veo 3.1 Fast:** في الكتالوج غير مفعّل ولم أقِسه، فلا أدّعي عنه شيئاً.
- **الوثيقة:** ملف القدرات (`profile` في models.toml) يميز observed عن expected.

**24. لماذا Kontext وليس LoRA؟**
- **Kontext:** إعلان واحد بمنتج مولَّد، لا صور حقيقية كثيرة للتدريب. صورة مرجع واحدة بـ 0.04 دولار،
  بلا تدريب.
- **الماكرو:** Kontext فشل في إعادة التأطير (direct أبقى إطار الـ packshot، و multi نسخ الـ packshot
  نفسه)، فصار القص بالكود + relight.
- **LoRA:** لعلامة حقيقية بحملة طويلة وصور منتج حقيقية.

**25. لماذا Haiku للفهم و Sonnet للأفكار؟**
ADR-0007، قياسات حقيقية:
- Haiku على low يفهم الطلب في 3.5 ث مقابل 5.7 ث على medium، بنفس جودة الـ brief.
- ideate على medium أو low رجع للكليشيهات، فصار high.
- profile "quality" يستخدم Opus للأفكار والـ storyboard والناقد.
- كل هذا في config، لا في الكود.

**26. لماذا vanilla JS؟**
- لا build step.
- يعمل من `uv run adsmith serve` مباشرة.
- SSE و SVG أصليان في المتصفح.
- الواجهة عرض للحالة، لا تطبيق معقد.

**27. لماذا Pydantic و mypy strict؟**
- **Pydantic:** كل مخرج LLM يمر بنفس الموديل الذي يولّد الـ JSON Schema، فالـ schema والتحقق لا
  يختلفان أبداً.
- **mypy strict:** يمسك أخطاء الأنواع قبل التشغيل المدفوع.

### التعافي والحالة

**28. ما الـ failure taxonomy ولماذا؟**
8 أنواع، ولكل نوع استراتيجية:
- transient و rate_limited: backoff بـ full jitter (4 محاولات، و5 بسقف 30 ث).
- content_policy: إعادة كتابة تحذف المحفز وتحافظ على النية (حتى 2).
- quality_rejected: إعادة توليد بإصلاحات الناقد.
- invalid_output: repair loop.
- budget_exceeded: إيقاف المشهد.
- permanent و exhausted: فشل المشهد وحده.

بدون تصنيف ستكون كل الأخطاء retry، وهذا يهدر المال.

**29. لماذا full jitter؟**
المشاهد المتوازية تفشل معاً عند انقطاع المزود. full jitter (`uniform(0, min(cap, base*2^n))`) يفرّق
محاولاتها فلا تضرب المزود دفعة واحدة. والـ sleep والـ rng injected، فالاختبارات فورية وحتمية.

**30. كيف يعمل الـ repair loop؟**
عند فشل التحقق تُضاف مخرجات النموذج **كما هي** ثم رسالة فيها الأخطاء الدقيقة **في نفس المحادثة**،
ويعيد. الحد `llm_max_repairs=2`. وإذا توقف بـ `max_tokens` يعيد مرة بضعف الـ tokens. كل محاولة
`LLMCallRecord` بتكلفتها.

**31. ما الـ blast radius؟**
دالة نقية (`agent/edit/blast.py`) تحسب أي المشاهد تصبح stale وبأي عمق:
- **clip:** تغيير حركة فقط (camera, speed, duration). الصورة تبقى.
- **prompt:** أي حقل لقطة آخر. prompt جديد وصورة ومقطع.
- **bible:** يُبطل فقط المشاهد التي تستخدم القسم المتغير. تغيير الملابس لا يمس لقطات المنتج.

**32. ماذا يحدث لو قال المستخدم «المشهد الثاني» وهو يقصد غيره؟**
`refs.py` جدول حتمي: ترتيبي عربي (مذكر ومؤنث، مع أو بدون "ال")، وأرقام هندية، وكلمات مثل
"الأخير". الـ LLM يقول **كيف** أشار المستخدم، والكود يحدد **أي** مشهد. إذا لم يُحسم يسأل ولا يخمّن.

### الصيغ والمونتاج

**33. ما مفردات اللقطات ولماذا مغلقة؟**
- **ما هي:** 16 لقطة في `config/shot_vocabulary.toml`. لكل واحدة الهوية والكاميرا والإضاءة والحركة و
  hint و fallback، وهل هي حدث فيزيائي أو لقطة مفتاحية.
- **لماذا مغلقة:** النماذج تنجح في الفيزياء (رذاذ، ماء، ضوء على معدن) وتفشل في التمثيل. القائمة
  المغلقة تحوّل دروس الـ runs إلى قواعد.

**34. كيف تختار الهوية لكل لقطة؟**
`identity_for` تقرأ `identity_mode` من المفردة:
- **none** للقطة بلا منتج.
- **direct:** Kontext بمقياس الـ packshot.
- **composite:** يد تمسك القارورة بحجمها الطبيعي.
- **crop:** قص الـ packshot بالكود ثم relight، للماكرو.

**35. ما الـ peak window؟**
- **المشكلة:** Kling يرندر 3 ث والإعلان يأخذ 1.5. الثانية الأولى غالباً بطيئة.
- **ما يفعله الكود:** `motion_profile` يحسب طاقة الحركة (numpy، 12 fps)، و`peak_window` يختار النافذة
  الأعلى حركة التي تحتوي الذروة.
- **التطبيق:** الناقد يراجع نفس النافذة، والتركيب يقصها.

**36. كيف يعمل الـ beat cut؟**
1. فك الموسيقى بـ ffmpeg.
2. spectral flux لاكتشاف الضربات.
3. اختيار الـ tempo بـ comb scoring: الضربات على الإيقاع ناقص بين الإيقاعات. هذا يحل خطأ الأوكتاف
   (60 بدل 120).
4. نقل كل قطع لأقرب beat في حدود ±0.2 ث، مع البطل على downbeat وبطاقة النهاية على beat.

في v2: 130 bpm، و7 من 7 قطعات انتقلت.

**37. ما الذي فعله الـ fallback في v2؟**
- لقطة انفجار الماء (bottle_into_water) رُفضت مرتين (0.50) لأن التكوين هو المواصفة. استُبدلت
  بـ bottle_on_rock فقُبلت بـ 0.82.
- شريط الماء رُفض مرتين (0.45)، فصار cap_lift. الصورة قُبلت، لكن المقطع فشل في الـ QA وإعادته رُفضت
  من الميزانية.
- كله مسجل كقرارات `vocab_fallback`.

### أسئلة صعبة

**38. ماذا فعلت أنت مقابل Claude Code؟**
بصراحة: Claude Code كتب أغلب الأسطر. دوري:
- **المعمارية والقواعد:** CLAUDE.md بقواعده التسع؛ اختيار FSM صريح؛ keyframe-first؛ hard/soft
  للناقد.
- **التخطيط والبوابات:** كل الخطط مع نقاط توقف (STOP 1 و STOP 2) أراجع عندها قبل أي صرف.
- **قرارات المال:** متى أشحن، وسقف كل run.
- **المراجعة البصرية:** راجعت كل مخرج بعيني، واكتشفت أخطاء لم يكتشفها الناقد: الكرتة، الغطاء
  الخطأ، شكل الويسكي.
- **القرارات الإبداعية:** مراجع المونتاج، صيغة الأحداث الفيزيائية، اعتماد v2.

أنا أعمل كمهندس مسؤول عن النظام، و Claude Code كمنفّذ سريع ضمن قواعد صارمة: `make check` قبل كل
commit، و ADR لكل قرار. وأستطيع شرح وتعديل كل جزء، وهذا الدليل دليلي على ذلك.

**39. كيف تتأكد أن الكود الذي كتبه AI صحيح؟**
- 584 اختباراً.
- mypy strict و ruff.
- كل مسار تعافٍ له اختبار.
- كل run حقيقي يُراجع بعين بشرية مع contact sheet وتقرير قرارات.
- الأخطاء التي ظهرت (race في الـ fallback، والموسيقى المحجوزة آخراً) ظهرت في runs حقيقية، وأُصلحت
  باختبار يعيد إنتاجها.

**40. ما أسوأ شيء في النظام الآن؟**
الناقد يفوّت تغيرات مواد صغيرة: الغطاء الكريستالي في v2 قُبل بـ 0.78. ولا يوجد eval suite تقيس ذلك
باستمرار. هذا أول ما أبنيه.

**41. هل النتيجة جيدة كإعلان؟**
v2 قريب من المراجع: أحداث فيزيائية، رذاذ حقيقي من الفوهة، بطل في النهاية، موسيقى بالإيقاع. لكنه ليس
بمستوى وكالة: هوية الغطاء تتذبذب، وبعض اللقطات ثابتة تقريباً. التكلفة حوالي 3 دولارات وحوالي 9
دقائق، بلا تدخل بشري.

**42. لماذا done_partial وليس إعادة المحاولة حتى ينجح كل شيء؟**
- المال محدود ومسقوف.
- إعلان من 7 لقطات جيدة أفضل من الانهيار أو صرف بلا حد.
- `adsmith retry <run> <scene>` يعيد مشهداً واحداً عند الرغبة، و`done_partial` يوضح ما فشل ولماذا.

**43. ما الذي ستفعله لو كان المنتج حقيقياً بصور حقيقية؟**
- تُستخدم صورة المنتج الحقيقية كـ packshot عبر `adopt_product_reference`، بدل التوليد.
- crop + Kontext يعملان كما هما.
- لحملة طويلة: LoRA مدرب على صور المنتج، و cross-shot identity check.

**44. كيف تعرف أن الأفكار الثلاث مختلفة فعلاً؟**
`domain/similarity.py` (ADR-0009): character trigrams بعد تطبيع عربي/إنجليزي، فوق حد معين يكون
validation error. في v1 كان أقصى تشابه 20%. والحكم يعاقب الكليشيهات المسماة في نفس الـ run.

---

## (ح) أرقام للحفظ

**الجودة والكود**
- **الاختبارات:** 584 ناجحة و2 متخطاة (live)، في 46 ملفاً. `make check` = ruff + ruff format + mypy
  --strict + pytest، حوالي 90 ث.
- **قرارات موثقة:** 21 ADR. وفي `prompts/` 19 قالباً.
- **الحدود الافتراضية:**

  | الحد | القيمة |
  |---|---|
  | `qa_threshold` | 0.7 |
  | `keyframe_max_tries` | 3 (2 في الـ runs الأخيرة) |
  | `qa_max_regenerations` | 2 (1 في الـ runs الأخيرة) |
  | `scene_concurrency` | 3 (4 في الـ runs الأخيرة) |
  | `llm_max_repairs` | 2 |
  | `max_attempts_per_scene` | 8 |
  | `max_total_attempts` | 80 |
  | transient retries | 4 |
  | rate-limit retries | 5، بسقف 30 ث |

**أوزان الحكم**

| المعيار | الوزن |
|---|---|
| brief_fit | 0.20 |
| originality | 0.20 |
| producibility | 0.20 |
| insight | 0.15 |
| product_hero | 0.15 |
| cultural_fit | 0.10 |

الخصومات: كليشيه 1.5−، لقطة غير قابلة للتصوير 1.5−، منتج متأخر 1.0−.

**الأسعار (الكتالوج)**

| النموذج | السعر |
|---|---|
| FLUX dev | 0.025 دولار/MP |
| Kontext pro / multi | 0.04 دولار |
| Kling v3 Standard | 0.084 دولار/ث في الدفتر (الفعلي ~0.066) |
| Kling v3 Pro | 0.14 دولار/ث في الدفتر (الصفحة 0.112) |
| LTX-2.3 Fast | 0.06 دولار/ث |
| Lyria 2 | 0.10 دولار/مقطع 30 ث |

**المونتاج v2 (الإعلان المعتمد)** `run_01M6MVHD6CE5V22VM38GXNRN09`
- **النتيجة:** 8 لقطات مخططة، 7 مقبولة، 12.54 ث، و130 bpm. الملف: `docs/media/montage_v2.mp4`.
- **الدرجات:**

  | العنصر | الدرجة |
  |---|---|
  | الفكرة المختارة «رشّة وحدة» | 7.30 مقابل 4.90 و 4.85 |
  | الـ packshot | 0.82 من أول محاولة |
  | الرذاذ (best-of-2) | 0.78 / 0.82 |
  | البطل (best-of-2) | 0.82 / 0.82 |
  | ماكرو الغطاء (crop) | 0.88 |
  | قطرة على ورقة | 0.72 |
  | قطرات على الزجاج | 0.80 |

- **المال:** fal في الدفتر 3.09 دولار (صور 0.57، مقاطع 2.52)، و LLM 0.77 دولار.
- **الوقت:** حوالي 526 ث. التخطيط حتى keyframe 132 ث، والـ keyframes 88 ث، والمقاطع + QA حوالي 300 ث.
- **الموسيقى:** أُضيفت بـ 0 دولار من مقطع v1 المخزّن.

**المونتاج v1** `run_01M6MT0QP80T95Q796V25HE1VD`
- 10 لقطات، 5 مقبولة، 8.4 ث.
- المال: fal 3.07 دولار، و LLM 0.89.
- الهوية مثالية والموسيقى موجودة، لكن بلا أحداث.

**دروس بالأرقام**
- الستارة: 0.50.
- معايرة الناقد: 12/14.
- الـ gate أوقف 21 keyframe، أي حوالي 5.60 دولار في أول run.
- خطأ الـ gate في الـ hero كلّف 2.24 دولار (0.78 ← 0.45 بعد الإصلاح).
- الـ single take: 1.43 دولار.
- الـ majlis ad: 2.93 دولار.

**الإجمالي**
- حوالي 18 دولاراً من fal على المشروع كله (الدفتر 18.158).
- الرصيد المتوقع حوالي 4.9 دولار.
