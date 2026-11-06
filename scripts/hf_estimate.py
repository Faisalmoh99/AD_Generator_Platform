"""Higgsfield prices. NO generation is ever submitted.

    uv run python scripts/hf_estimate.py --matrix          # LOCAL table: no HTTP, no key, free
    ADSMITH_AD_PROVIDER=higgsfield uv run python scripts/hf_estimate.py            # one /estimate
    ADSMITH_AD_PROVIDER=higgsfield uv run python scripts/hf_estimate.py --prompt-limit

The default and --prompt-limit call Higgsfield (upload + /estimate): run them only when the user
has approved it. Observed 2026-11-06: /estimate was free (balance unchanged) and, for Seedance
2.0, answers with a token-pricing description instead of a number; the provider then prices from
[pricing] in config/models.toml (+ the margin). --matrix computes the same formula locally.

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
from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.providers.media.base import MediaLabel, ReferenceVideoJob, token_quote

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


def matrix(s: Settings, balance: float | None) -> None:
    """The price table from the configured token formula. Pure arithmetic: no HTTP."""
    hf = s.catalog.higgsfield
    assert hf is not None
    fits = f"tries in ${balance or 0:g}"
    print(f"{'model':<6} {'dur':>4} {'res':>6} {'aspect':>6} {'size (assumed)':>15} "
          f"{'price':>8} {'reserve':>8} {'$/s':>7} {fits:>12}")  # fmt: skip
    for key, ep in hf.video.endpoints.items():
        name = key.removeprefix("seedance_").removesuffix("_ref").replace("2", "2.", 1)
        if ep.pricing is None:
            print(f"{name:<6} no token pricing configured (/estimate not observed): unknown")
            continue
        rows = itertools.product([8, 10, 12, 15], ["480p", "720p", "1080p"], ["9:16"])
        extra = [(8, "720p", "16:9"), (8, "4k", "9:16")]
        for d, r, aspect in [*rows, *extra]:
            if r not in ep.resolutions or not ep.min_duration_s <= d <= ep.max_duration_s:
                continue
            q = token_quote(ep.pricing, resolution=r, aspect_ratio=aspect, duration_s=d)
            capped = q.reserved_usd > ep.max_usd_per_second * d
            tries = "" if balance is None else str(int(balance // q.reserved_usd))
            print(f"{name:<6} {d:>4} {r:>6} {aspect:>6} {f'{q.width}x{q.height}':>15} "
                  f"{q.computed_usd:>8.3f} {q.reserved_usd:>8.3f} {q.computed_usd / d:>7.4f} "
                  f"{tries:>12}" + ("  (above the sanity cap)" if capped else ""))  # fmt: skip
    print(
        "Audio on/off and 1-9 reference images do not change the price (the formula counts "
        "only video seconds x frame size).\nReserve = price + the margin; tries = how many "
        "reservations fit in the balance, one after another, if each settles at the reserve."
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", action="store_true", help="the price table, computed locally")
    ap.add_argument("--prompt-limit", action="store_true", help="probe the prompt length limit")
    args = ap.parse_args()
    s = Settings()
    if args.matrix:
        matrix(s, s.hf_balance_usd)
        return
    if s.ad_provider != "higgsfield":
        raise SystemExit("set ADSMITH_AD_PROVIDER=higgsfield to call Higgsfield")
    rt = AgentRuntime.from_settings(s)
    queue = rt.hf_queue()
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "probe.png"
        Image.new("RGB", (64, 64), (180, 110, 40)).save(png)
        url = await queue.upload(png)
    print("upload OK (presigned PUT accepted, public url received)")

    v20 = rt.higgsfield_video("seedance_20_ref")
    if not args.prompt_limit:
        q = await v20.quote(job(url, duration=8, resolution="720p", aspect="9:16",
                                audio=True, images=1))  # fmt: skip
        print(
            f"seedance 2.0, 8 s, 720p, 9:16, audio on, 1 image: price ${q.computed_usd:.3f}, "
            f"reserve ${q.reserved_usd:.3f} ({q.source}: {q.basis})"
        )
        print("Now check the Higgsfield console balance: did this estimate cost anything?")
        return

    if args.prompt_limit:
        last_ok = 0
        for size in (500, 1000, 2000, 3000, 4000, 6000, 8000):
            text = (PROMPT + " ") * (size // len(PROMPT) + 1)
            try:
                await v20.quote(job(url, duration=8, resolution="720p", aspect="9:16",
                                           audio=True, images=1, prompt=text[:size]))  # fmt: skip
                last_ok = size
                print(f"prompt {size:>5} chars: accepted")
            except PermanentProviderError as exc:
                print(f"prompt {size:>5} chars: refused ({exc.message[:160]})")
                break
        print(f"longest accepted prompt: {last_ok} chars")


if __name__ == "__main__":
    asyncio.run(main())
