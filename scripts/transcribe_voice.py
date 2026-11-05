"""Transcribe the demo voice notes LOCALLY with faster-whisper (no upload: the model runs on this
machine; only the model weights are downloaded once from Hugging Face).

    uv run --with faster-whisper python scripts/transcribe_voice.py \\
        --out /path/to/raw_transcripts   # one JSON per file: segments + word timestamps

Arabic (Saudi dialect) with English tech terms: an initial prompt biases the decoder toward the
terms used in the narration.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "docs" / "demo" / "voice" / "raw"
TERMS = (
    "state machine, LLM, Pydantic, Bible, keyframe, Keyframes, gate, blast radius, fal, Kling, "
    "snapshot, retry, fallback, Haiku, Sonnet, Brief, Concepts, Pick, Storyboard, Prompts, Clips, "
    "QA, Assembly, Chaos mode, swimlane, policy rewrite, make demo, LangGraph, agent, provider, "
    "Decisions, prompt, eval suite, test, Anthropic, Generate."
)
PROMPT = f"تسجيل عربي باللهجة السعودية فيه مصطلحات تقنية بالإنجليزي: {TERMS}"


def pcm16k(src: Path) -> np.ndarray:
    """Decode with the local ffmpeg to 16 kHz mono float32 (faster-whisper's own PyAV decoder
    breaks on some PyAV versions; this path also never leaves the machine)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.float32).copy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--only", nargs="*", default=[], help="time stamps to transcribe, e.g. 10.44.11"
    )
    ap.add_argument("--files", nargs="*", type=Path, default=[], help="explicit audio files")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=8)
    sources = args.files or sorted(RAW.glob("*.opus"))
    for src in sources:
        if not args.files and args.only and not any(o in src.name for o in args.only):
            continue
        t = time.monotonic()
        segments, info = model.transcribe(
            pcm16k(src), language="ar", word_timestamps=True, vad_filter=True,
            initial_prompt=PROMPT, beam_size=5, condition_on_previous_text=True,
        )  # fmt: skip
        segs = [
            {
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "text": s.text.strip(),
                "words": [
                    {"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word}
                    for w in (s.words or [])
                ],
            }
            for s in segments
        ]
        stamp = src.stem.split(" at ")[-1] if " at " in src.stem else src.stem
        data = {"file": src.name, "duration": round(info.duration, 2), "segments": segs}
        (args.out / f"{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
        print(f"{stamp}: {info.duration:.1f}s audio, {len(segs)} segments, "
              f"{time.monotonic() - t:.0f}s", flush=True)  # fmt: skip


if __name__ == "__main__":
    main()
