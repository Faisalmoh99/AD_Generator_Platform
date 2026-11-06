"""Higgsfield price table from the account's own /estimate endpoint. NO generation is submitted.

Calls Higgsfield (upload + estimate): run it only when the user has approved it. The docs do not
say estimates are free, so the default is ONE estimate; check the console balance, then run the
full table with --matrix.

    ADSMITH_AD_PROVIDER=higgsfield uv run python scripts/hf_estimate.py            # one estimate
    ADSMITH_AD_PROVIDER=higgsfield uv run python scripts/hf_estimate.py --matrix   # the table
    ADSMITH_AD_PROVIDER=higgsfield uv run python scripts/hf_estimate.py --prompt-limit

Never prints the key. The reference image is a generated 64x64 PNG (the estimate does not
depend on the image content), never the product photo.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import tempfile
from pathlib import Path

from PIL import Image

from adsmith.agent.context import AgentRuntime
from adsmith.agent.recovery.errors import AgentError, PermanentProviderError
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.providers.media.base import MediaLabel, ReferenceVideoJob

PROMPT = (
    "Shot 1 (0-3 s): extreme macro of a gold lattice perfume cap, slow push-in, one hard rim "
    "light. Shot 2 (3-7 s): a fine mist leaves the nozzle and catches the backlight. Shot 3 "
    "(7-12 s): the amber bottle on black, slow push-in, the lower fifth empty. Sound: a low "
    "string swell and a soft hiss of mist."
)


def job(url: str, *, duration: int, resolution: str, aspect: str, audio: bool, images: int,
        prompt: str = PROMPT) -> ReferenceVideoJob:  # fmt: skip
    return ReferenceVideoJob(
        prompt=prompt,
        image_urls=tuple([url] * images),
        duration_s=duration,
        resolution=resolution,
        aspect_ratio=aspect,
        generate_audio=audio,
        label=MediaLabel(scene_order=0, scene_version=1, phase=ScenePhase.CLIP, attempt_no=1),
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", action="store_true", help="the full price table")
    ap.add_argument("--prompt-limit", action="store_true", help="probe the prompt length limit")
    args = ap.parse_args()
    s = Settings()
    if s.ad_provider != "higgsfield":
        raise SystemExit("set ADSMITH_AD_PROVIDER=higgsfield to call Higgsfield")
    rt = AgentRuntime.from_settings(s)
    queue = rt.hf_queue()
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "probe.png"
        Image.new("RGB", (64, 64), (180, 110, 40)).save(png)
        url = await queue.upload(png)
    print("upload OK (presigned PUT accepted, public url received)")

    v20, v25 = rt.higgsfield_video("seedance_20_ref"), rt.higgsfield_video("seedance_25_ref")
    if not args.matrix and not args.prompt_limit:
        usd = await v20.estimate_usd(job(url, duration=12, resolution="720p", aspect="9:16",
                                          audio=True, images=1))  # fmt: skip
        print(f"seedance 2.0, 12 s, 720p, 9:16, audio on, 1 image: ${usd:.3f} (${usd / 12:.4f}/s)")
        print("Now check the Higgsfield console balance: did this estimate cost anything?")
        return

    if args.matrix:
        print(f"{'model':<6} {'dur':>4} {'res':>6} {'aspect':>6} {'audio':>5} {'imgs':>4} "
              f"{'usd':>8} {'usd/s':>8}")  # fmt: skip
        rows = [
            (name, p, d, r, "9:16", a, 1)
            for (name, p), d, r, a in itertools.product(
                [("2.0", v20), ("2.5", v25)], [8, 12, 15], ["720p", "1080p"], [True, False]
            )
        ]
        rows += [
            ("2.0", v20, 12, "720p", "9:16", True, 3),
            ("2.5", v25, 12, "720p", "9:16", True, 3),
            ("2.0", v20, 12, "720p", "16:9", True, 1),
            ("2.0", v20, 12, "4k", "9:16", True, 1),
        ]
        for name, p, d, r, aspect, a, n in rows:
            try:
                usd = await p.estimate_usd(job(url, duration=d, resolution=r, aspect=aspect,
                                               audio=a, images=n))  # fmt: skip
                print(f"{name:<6} {d:>4} {r:>6} {aspect:>6} {'on' if a else 'off':>5} {n:>4} "
                      f"{usd:>8.3f} {usd / d:>8.4f}")  # fmt: skip
            except AgentError as exc:
                print(f"{name:<6} {d:>4} {r:>6} {aspect:>6} {'on' if a else 'off':>5} {n:>4} "
                      f"  error: {exc.kind.value}: {exc.message[:120]}")  # fmt: skip
        print(f"estimate calls this run: {queue.estimates}")

    if args.prompt_limit:
        last_ok = 0
        for size in (500, 1000, 2000, 3000, 4000, 6000, 8000):
            text = (PROMPT + " ") * (size // len(PROMPT) + 1)
            try:
                await v20.estimate_usd(job(url, duration=12, resolution="720p", aspect="9:16",
                                           audio=True, images=1, prompt=text[:size]))  # fmt: skip
                last_ok = size
                print(f"prompt {size:>5} chars: accepted")
            except PermanentProviderError as exc:
                print(f"prompt {size:>5} chars: refused ({exc.message[:160]})")
                break
        print(f"longest accepted prompt: {last_ok} chars")


if __name__ == "__main__":
    asyncio.run(main())
