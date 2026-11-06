"""Safety net: ONE Higgsfield Seedance generation from a prompt and the product photo.

Calls Higgsfield. Run it only when the user has approved it.

    upload the photo → print the exact request body and the account's estimate → ask y/N
    → submit ONCE (Idempotency-Key, wallet reservation of exactly the estimate)
    → poll → download → settle → print the ledger line

The submitted request id is written to ``<out>.json`` right after the submit, so if this process
dies the job is resumed by polling (``--resume <out>.json``), never paid twice. ``--dry-run``
stops after the estimate (upload + estimate are still calls to Higgsfield). There is no flag to
skip the y/N question.

    ADSMITH_AD_PROVIDER=higgsfield uv run python scripts/hf_generate_once.py \\
        --prompt-file ad_prompt.txt --photo assets/perfume.jpeg --duration 12 --resolution 720p
    ADSMITH_AD_PROVIDER=higgsfield uv run python scripts/hf_generate_once.py --run <run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from adsmith.agent.context import AgentRuntime
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.domain.generation import RemoteRequest
from adsmith.providers.media.base import MediaLabel, ReferenceVideoJob, RemoteControl


def load_prompt(args: argparse.Namespace, rt: AgentRuntime) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8").strip()
    state = rt.storage.runs.load(args.run)
    gen = getattr(state, "generation", None)
    if gen is None or gen.prompt is None:
        raise SystemExit(f"run {args.run} has no ad prompt yet (plan it with --until estimate)")
    text: str = gen.prompt.text
    return text


async def main() -> None:  # noqa: PLR0915
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--prompt-file")
    src.add_argument("--run", help="take the ad prompt from a planned run")
    src.add_argument("--resume", help="poll the job recorded in this .json (no new submit)")
    ap.add_argument("--photo", default="assets/perfume.jpeg")
    ap.add_argument("--model", default=None, help="catalog key, e.g. seedance_20_ref")
    ap.add_argument("--duration", type=int, default=12)
    ap.add_argument("--resolution", default="720p")
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--audio", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--dry-run", action="store_true", help="stop after the estimate")
    args = ap.parse_args()
    s = Settings()
    if s.ad_provider != "higgsfield":
        raise SystemExit("set ADSMITH_AD_PROVIDER=higgsfield to call Higgsfield")
    rt = AgentRuntime.from_settings(s)
    out_dir = s.data_dir / "hf_once"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        record = json.loads(await asyncio.to_thread(Path(args.resume).read_text))
        prompt, image_urls = record["prompt"], tuple(record["image_urls"])
        model, est = record["model_key"], float(record["est_usd"])
        duration, resolution = int(record["duration"]), record["resolution"]
        aspect, audio = record["aspect"], bool(record["audio"])
        resume = RemoteRequest.model_validate(record["remote"])
        state_path = Path(args.resume)
    else:
        if not (args.prompt_file or args.run):
            raise SystemExit("give --prompt-file, --run or --resume")
        prompt = load_prompt(args, rt)
        photo = Path(args.photo)
        if not await asyncio.to_thread(photo.is_file):
            raise SystemExit(f"no product photo at {photo}")
        image_urls = (await rt.hf_queue().upload(photo),)
        model = args.model or s.hf_video_model or "seedance_20_ref"
        duration, resolution, aspect, audio = (
            args.duration,
            args.resolution,
            args.aspect,
            args.audio,
        )
        resume, est = None, 0.0
        state_path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json"

    provider = rt.higgsfield_video(model)
    label = MediaLabel(scene_order=0, scene_version=1, phase=ScenePhase.CLIP, attempt_no=1)
    job = ReferenceVideoJob(
        prompt=prompt, image_urls=image_urls, duration_s=duration, resolution=resolution,
        aspect_ratio=aspect, generate_audio=audio, label=label,
    )  # fmt: skip
    if resume is None:
        body = provider.body(job)
        print("Request body:\n" + json.dumps(body, indent=2, ensure_ascii=False))
        est = await provider.estimate_usd(job)
        left = rt.hf_account().summary().remaining_usd
        sound = "on" if audio else "off"
        print(
            f"\nEstimate: ${est:.3f} for {duration} s {resolution} {aspect}, audio {sound}, "
            f"{provider.model}. Wallet headroom: ${left:.3f}."
        )
        if args.dry_run:
            print("Dry run: nothing submitted.")
            return
        question = f"Submit ONE generation for ${est:.3f}? [y/N] "
        answer = (await asyncio.to_thread(input, question)).strip().lower()
        if answer != "y":
            print("Not submitted.")
            return

    def on_submitted(req: RemoteRequest) -> None:
        record = {
            "prompt": prompt, "image_urls": list(image_urls), "model_key": model,
            "est_usd": est, "duration": duration, "resolution": resolution, "aspect": aspect,
            "audio": audio, "remote": req.model_dump(mode="json"),
        }  # fmt: skip
        state_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        print(f"Submitted: request {req.request_id} (resume with --resume {state_path})")

    job = ReferenceVideoJob(
        prompt=prompt, image_urls=image_urls, duration_s=duration, resolution=resolution,
        aspect_ratio=aspect, generate_audio=audio, label=label, est_usd=est,
        remote=RemoteControl(resume=resume, on_submitted=on_submitted),
    )  # fmt: skip
    t0 = time.monotonic()
    out = await provider.reference_to_video(job, out_dir)
    final = state_path.with_suffix(".mp4")
    out.path.replace(final)
    print(f"Done in {time.monotonic() - t0:.0f} s: {final} ({out.width}x{out.height}, "
          f"{out.duration_s:.2f} s), request {out.meta.get('request_id')}")  # fmt: skip
    summary = rt.hf_account().summary()
    print(f"Ledger: settled ${summary.settled_usd:.3f}, reserved ${summary.reserved_usd:.3f}, "
          f"headroom ${summary.remaining_usd:.3f} of ${summary.balance_usd:.2f}")  # fmt: skip


if __name__ == "__main__":
    asyncio.run(main())
