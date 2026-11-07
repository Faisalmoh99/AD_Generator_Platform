"""Deterministic user-facing messages in the request language (Arabic in → Arabic out)."""

from __future__ import annotations

from adsmith.domain.enums import FailureKind, Language, Stage

_STAGE: dict[Stage, tuple[str, str]] = {
    Stage.UNDERSTAND: ("understanding the request", "فهم الطلب"),
    Stage.INSIGHT: ("finding the insight", "استخراج البصيرة"),
    Stage.IDEATE: ("proposing concepts", "اقتراح الأفكار"),
    Stage.SELECT: ("selecting a concept", "اختيار الفكرة"),
    Stage.STORYBOARD: ("building the storyboard", "بناء القصة المصورة"),
    Stage.CONTINUITY: ("defining visual continuity", "تحديد الهوية البصرية"),
    Stage.PROMPT: ("writing generation prompts", "كتابة أوامر التوليد"),
    Stage.DIRECTOR: ("creative director review", "مراجعة المخرج الإبداعي"),
    Stage.PROPS: ("generating prop references", "توليد صور الأدوات المرجعية"),
    Stage.COPY: ("writing the brand and the tagline", "كتابة اسم العلامة والشعار"),
    Stage.ESTIMATE: ("estimating the generation cost", "تقدير تكلفة التوليد"),
    Stage.AWAITING_GENERATION_APPROVAL: (
        "waiting for spend approval",
        "انتظار الموافقة على التكلفة",
    ),
    Stage.KEYFRAME: ("generating keyframes", "توليد الإطارات الرئيسية"),
    Stage.GENERATE: ("generating clips", "توليد المقاطع"),
    Stage.QA: ("reviewing quality", "مراجعة الجودة"),
    Stage.ASSEMBLE: ("assembling the ad", "تجميع الإعلان"),
}

_FAILURE: dict[FailureKind, tuple[str, str]] = {
    FailureKind.TRANSIENT: (
        "The service was temporarily unavailable and kept failing after several retries.",
        "الخدمة لم تكن متاحة مؤقتاً واستمر الخطأ بعد عدة محاولات.",
    ),
    FailureKind.RATE_LIMITED: (
        "We hit the provider's rate limit repeatedly. Please try again shortly.",
        "تجاوزنا حد الطلبات لدى المزوّد عدة مرات. حاول مرة أخرى بعد قليل.",
    ),
    FailureKind.CONTENT_POLICY: (
        "The request was declined by the provider's content policy.",
        "رُفض الطلب بسبب سياسة المحتوى لدى المزوّد.",
    ),
    FailureKind.QUALITY_REJECTED: (
        "The result did not pass quality review after several attempts.",
        "لم تجتز النتيجة مراجعة الجودة بعد عدة محاولات.",
    ),
    FailureKind.INVALID_OUTPUT: (
        "The AI's answer could not be validated, even after asking it to correct itself.",
        "لم نتمكن من التحقق من إجابة الذكاء الاصطناعي حتى بعد طلب تصحيحها.",
    ),
    FailureKind.BUDGET_EXCEEDED: (
        "Stopped to stay within the run's budget.",
        "توقفنا للبقاء ضمن ميزانية التشغيل.",
    ),
    FailureKind.PERMANENT: (
        "A configuration or request error occurred that retrying cannot fix.",
        "حدث خطأ في الإعداد أو الطلب لا تحله إعادة المحاولة.",
    ),
    FailureKind.EXHAUSTED: (
        "All recovery attempts were used up.",
        "استُنفدت جميع محاولات الاستعادة.",
    ),
}


def _pick(pair: tuple[str, str], lang: Language | str) -> str:
    return pair[1] if str(lang) == Language.AR.value else pair[0]


def stage_label(stage: Stage, lang: Language | str) -> str:
    return _pick(_STAGE.get(stage, (stage.value, stage.value)), lang)


def failure_message(kind: FailureKind, stage: Stage, lang: Language | str) -> str:
    if str(lang) == Language.AR.value:
        return f"تعذّر {stage_label(stage, lang)}: {_pick(_FAILURE[kind], lang)}"
    return f"Failed while {stage_label(stage, lang)}: {_pick(_FAILURE[kind], lang)}"
