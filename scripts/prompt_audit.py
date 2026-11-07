"""Audit stored whole-ad prompts against the ADR-0028 and ADR-0029 rules (local, $0, no API call).

For each run: the words of the prompt that was stored (what Seedance would have received), the
writer's own words, and every word-budget and physics-library problem the new checks raise.

    uv run python scripts/prompt_audit.py                      # the five planning runs
    uv run python scripts/prompt_audit.py run_01M6SVSK8GD53SYGZ8SF7AA9TT
"""

from __future__ import annotations

import argparse

from adsmith.agent.context import AgentRuntime
from adsmith.config import Settings
from adsmith.domain.ad import word_count, word_problems, writer_words
from adsmith.domain.creative_rules import (
    key_beat_problems,
    physics_problems,
    unsupported_shots,
    word_budget_problems,
)
from adsmith.domain.culture_bank import object_problems, with_gulf_objects

RUNS = [
    "run_01M6TBP6H8VTXWGXFJR6XPVQ7M",  # coffee, the first video by the system alone
    "run_01M6SSVMPBEAY1WW1FWF4DGWRZ",  # women's perfume, first planning run
    "run_01M6SV0XZVPCFS6XA9HD7EE71M",  # women's perfume, re-run
    "run_01M6SVSK8GD53SYGZ8SF7AA9TT",  # coffee
    "run_01M6T075QBXT7VV9EW7K5V4MNC",  # dates, the grandmother
    "run_01M6T1CVKN10GHKGPZD3JFFQ23",  # dates, the host and the headlights
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs", nargs="*", default=RUNS)
    args = ap.parse_args()
    s = Settings()
    rules = s.creative_rules
    p = rules.params
    rt = AgentRuntime.from_settings(s)
    print(f"{'run':32} {'stored':>7} {'writer':>7}  problems (budget {p.max_words} writer words)")
    for run_id in args.runs:
        g = rt.storage.runs.load(run_id).generation
        if g is None or g.prompt.key_shot is None:
            print(f"{run_id:32} (no v2 whole-ad prompt)")
            continue
        d = g.prompt.to_draft()
        problems = word_budget_problems(d, p) + physics_problems(d, rules.physics)
        # ADR-0029: object fidelity (after code's own substitutions) and the key shot's beats
        problems += object_problems(with_gulf_objects(d, s.culture_bank), s.culture_bank)
        problems += key_beat_problems(d, p.key_max_beats)
        problems += [
            f"shot {n} shows the product with no hand and no surface (it floats)"
            for n in unsupported_shots(d, rules.support)
        ]
        problems += word_problems(
            d, duration_s=g.prompt.duration_s, max_words=p.max_words,
            key_within_words=p.key_within_words, identity=g.prompt.identity or None,
        )  # fmt: skip
        print(f"{run_id:32} {word_count(g.prompt.text):>7} {writer_words(d):>7}  {len(problems)}")
        for msg in problems:
            print(f"    - {msg[:150]}")


if __name__ == "__main__":
    main()
