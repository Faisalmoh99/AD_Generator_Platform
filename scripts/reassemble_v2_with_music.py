"""Montage v2 + music at $0: re-assemble the approved v2 run with the Lyria track that montage v1
already generated (stored in the CAS). Same clips and peak windows, beat-cut to that track, same
end card. No generation call: run it with the wallet ceiling at the settled total so any paid
call would be refused.

    ADSMITH_LLM_PROVIDER=anthropic ADSMITH_MEDIA_PROVIDER=fal ADSMITH_FAL_BALANCE_USD=<settled> \\
    uv run python scripts/reassemble_v2_with_music.py
"""

from __future__ import annotations

import asyncio

from adsmith.agent import commands
from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings

V1 = "run_01M6MT0QP80T95Q796V25HE1VD"
V2 = "run_01M6MVHD6CE5V22VM38GXNRN09"


async def main() -> None:
    rt = AgentRuntime.from_settings(Settings())
    track = rt.storage.runs.load(V1).music
    assert track is not None, "montage v1 has no stored track"
    commands.reassemble(rt.storage, V2, music=track, music_from=f"montage v1 ({V1})")
    st = await Orchestrator(rt).run_until_blocked(V2)
    assert st.final is not None
    print(st.stage.value, rt.storage.artifacts.resolve(st.final))


if __name__ == "__main__":
    asyncio.run(main())
