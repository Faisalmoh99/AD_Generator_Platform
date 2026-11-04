"""Calibration set for the concept-distinctness threshold (ADR-0009).

Texts mimic the ``ConceptDraft.signature_text()`` of realistic LLM output for one brief:
"15-second ad for a luxury men's perfume targeting young men, cinematic". Arabic is stored in
logical order.
"""

from __future__ import annotations

EN_NIGHT_DRIVE = (
    "The Night Drive. A young man drives alone through the empty city at night, neon reflections "
    "sliding over his face, until he stops, sprays the perfume and steps into the party. Neon "
    "reflections on a black car window. Confidence and freedom. He drives through the city at "
    "night. Reflections on the glass. He sprays the perfume. He enters the party."
)
EN_ELEMENTAL = (
    "Elemental. An abstract sensory journey: black smoke, liquid gold and cracked desert stone "
    "swirl and form the bottle, translating the scent notes into texture. Liquid gold pouring in "
    "extreme slow motion. Sensory luxury. Smoke rises in darkness. Gold liquid pours and swirls. "
    "Desert stone cracks. The bottle forms from the elements."
)
EN_TAILOR = (
    "The Tailor. In a heritage tailoring atelier, an older master fits a young man for his first "
    "bespoke suit; the final touch is the perfume, passed on like a legacy. Hands adjusting a "
    "collar in warm window light. Belonging and legacy. Measuring tape and fabric. The master "
    "adjusts the jacket. He hands him the bottle. The young man walks out transformed."
)
# Distinct concepts that deliberately share vocabulary with NIGHT_DRIVE (young man, night,
# perfume, reflections): the hardest "should pass" cases.
EN_MIRROR = (
    "The Mirror. A young man prepares for the night in front of a mirror, buttoning a black shirt, "
    "adjusting his watch, and sprays the perfume; his reflection smiles first. Reflection that "
    "moves before he does. Confidence and self-image. Buttoning the shirt. Adjusting the watch. "
    "Spraying the perfume. The reflection smiles."
)
EN_ROOFTOP = (
    "Rooftop Majlis. A young man hosts his friends on a Riyadh rooftop at night, oud smoke "
    "drifting, city lights below; he passes the perfume around and the night begins. Oud smoke "
    "against city lights. Brotherhood and pride. Friends arrive on the rooftop at night. Oud smoke "
    "rises. He offers the perfume. They laugh as the city glows."
)
EN_DESERT_DAWN = (
    "Desert Dawn. At dawn a young man rides a horse across the dunes; sand and wind, then a "
    "close-up of the perfume bottle half-buried in golden sand. Golden sand pouring over the "
    "bottle. Freedom and heritage. Riding at dawn. Wind across dunes. Sand over the bottle. He "
    "looks to the horizon."
)
# Same idea as NIGHT_DRIVE: these "should fail".
EN_NIGHT_DRIVE_LIGHT_PARAPHRASE = (
    "Night Drive. A young man drives by himself through the empty city at night, with neon "
    "reflections sliding across his face, then he stops, sprays the perfume and walks into the "
    "party. Neon reflecting on a black car window. Freedom and confidence. Driving through the "
    "city at night. Reflections on glass. Spraying the perfume. Entering the party."
)
EN_NIGHT_DRIVE_SAME_WORLD = (
    "Neon City. A young man walks through the city at night under neon signs, the reflections of "
    "the party lights on wet streets, and sprays the perfume before entering a rooftop party. Neon "
    "reflections on wet asphalt. Confidence at night. Walking the city at night. Neon signs "
    "reflect. He sprays the perfume. Rooftop party entrance."
)
# Same idea, every content word swapped for a synonym: a KNOWN BLIND SPOT of a lexical metric.
EN_NIGHT_DRIVE_SYNONYM_REWRITE = (
    "Midnight Cruise. Alone behind the wheel, a guy cruises the deserted downtown streets after "
    "dark while city lights glide across his face; he parks, applies the fragrance, and joins the "
    "celebration. City lights reflected in the windshield. Independence and self-assurance. "
    "Cruising downtown after dark. Lights on the windshield. Applying the fragrance. Joining the "
    "celebration."
)

AR_NIGHT_DRIVE = (
    "رحلة الليل. شاب يقود سيارته وحده في المدينة الفارغة ليلاً، وانعكاسات النيون تنزلق على وجهه، "
    "ثم يتوقف ويرش العطر ويدخل الحفلة. انعكاسات النيون على نافذة السيارة السوداء. الثقة والحرية. "
    "يقود في المدينة ليلاً. انعكاسات على الزجاج. يرش العطر. يدخل الحفلة."
)
AR_ELEMENTAL = (
    "العناصر. رحلة حسية تجريدية: دخان أسود وذهب سائل وحجر صحراوي متشقق تتشكل منها الزجاجة وتترجم "
    "مكونات العطر إلى ملمس. ذهب سائل ينسكب بحركة بطيئة جداً. فخامة حسية. دخان يرتفع في الظلام. "
    "ذهب يتدفق. حجر الصحراء يتشقق. الزجاجة تتشكل."
)
AR_TAILOR = (
    "الخياط. في مشغل خياطة عريق، يقيس معلم كبير شاباً لأول بدلة مفصلة له، واللمسة الأخيرة هي العطر "
    "يُسلَّم كإرث. أيدٍ تعدل الياقة في ضوء نافذة دافئ. الانتماء والإرث. شريط قياس وقماش. المعلم يعدل "
    "الجاكيت. يسلمه الزجاجة. الشاب يخرج بثقة."
)
# Saudi-dialect paraphrase of AR_NIGHT_DRIVE (يسوق، بالليل، الحفل): should fail.
AR_NIGHT_DRIVE_DIALECT_PARAPHRASE = (
    "رحلة ليلية. شاب يسوق سيارته بمفرده في شوارع المدينة الخالية بالليل والأضواء تنعكس على وجهه، "
    "يوقف السيارة ويرش العطر ويدخل الحفل. انعكاس الأضواء على زجاج السيارة. الحرية والثقة. يسوق في "
    "المدينة بالليل. أضواء على الزجاج. يرش العطر. يدخل الحفل."
)

SHOULD_BE_DISTINCT: list[tuple[str, str, str]] = [
    ("night_drive vs elemental", EN_NIGHT_DRIVE, EN_ELEMENTAL),
    ("night_drive vs tailor", EN_NIGHT_DRIVE, EN_TAILOR),
    ("elemental vs tailor", EN_ELEMENTAL, EN_TAILOR),
    ("night_drive vs mirror (shared vocab)", EN_NIGHT_DRIVE, EN_MIRROR),
    ("night_drive vs rooftop (shared vocab)", EN_NIGHT_DRIVE, EN_ROOFTOP),
    ("night_drive vs desert_dawn", EN_NIGHT_DRIVE, EN_DESERT_DAWN),
    ("mirror vs rooftop", EN_MIRROR, EN_ROOFTOP),
    ("ar night_drive vs elemental", AR_NIGHT_DRIVE, AR_ELEMENTAL),
    ("ar night_drive vs tailor", AR_NIGHT_DRIVE, AR_TAILOR),
    ("ar elemental vs tailor", AR_ELEMENTAL, AR_TAILOR),
]
SHOULD_BE_DUPLICATES: list[tuple[str, str, str]] = [
    ("light paraphrase", EN_NIGHT_DRIVE, EN_NIGHT_DRIVE_LIGHT_PARAPHRASE),
    ("same idea, new details", EN_NIGHT_DRIVE, EN_NIGHT_DRIVE_SAME_WORLD),
    ("ar dialect paraphrase", AR_NIGHT_DRIVE, AR_NIGHT_DRIVE_DIALECT_PARAPHRASE),
]
