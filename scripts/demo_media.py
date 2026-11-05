"""Shared video normalisation for the demo scripts (record_demo.py, merge_voiceover.py).

Why: concatenating separately encoded MP4s with stream copy keeps only the FIRST file's codec
parameters (SPS/PPS in the avcC box). Frames from a file encoded with other x264 settings or a
different sample aspect ratio are then decoded with the wrong parameters: ffmpeg tolerates it,
QuickTime stalls (the draft froze at 12.5 s, where the hook's montage clip met the recorded page).

So every piece is normalised to one profile, and every join is a re-encode:
H.264 High, yuv420p, 1920x1080, SAR 1:1, 30 fps CFR, timescale 15360, AAC stereo 48 kHz (silence
where a piece has no sound), +faststart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

W, H, FPS, TIMESCALE = 1920, 1080, 30, 15360
VIDEO_FILTER = f"scale={W}:{H}:flags=lanczos,setsar=1,fps={FPS},format=yuv420p"
AUDIO_FILTER = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
ENCODE = [
    "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "18",
    "-pix_fmt", "yuv420p", "-r", str(FPS), "-video_track_timescale", str(TIMESCALE),
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    "-movflags", "+faststart",
]  # fmt: skip


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(out.stdout.strip())


def has_audio(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return bool(out.stdout.strip())


def normalise(
    src: Path, out: Path, *, seconds: float | None = None, keep_audio: bool = True
) -> None:
    """``src`` re-encoded to the demo profile, ``seconds`` long (default: its own length). Its
    audio is kept (resampled to stereo 48 kHz) or replaced by silence."""
    secs = seconds if seconds is not None else duration(src)
    audio_in = keep_audio and has_audio(src)
    a = (
        f"[0:a]{AUDIO_FILTER},apad[a]"
        if audio_in
        else f"anullsrc=r=48000:cl=stereo,{AUDIO_FILTER}[a]"
    )
    ffmpeg("-i", str(src), "-filter_complex", f"[0:v]{VIDEO_FILTER}[v];{a}",
           "-map", "[v]", "-map", "[a]", "-t", f"{secs:.3f}", *ENCODE, str(out))  # fmt: skip


def concat(parts: list[Path], out: Path) -> None:
    """Join normalised pieces with ONE re-encode (concat filter), never stream copy."""
    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    chains = "".join(
        f"[{i}:v]{VIDEO_FILTER}[v{i}];[{i}:a]{AUDIO_FILTER}[a{i}];" for i in range(len(parts))
    )
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    graph = f"{chains}{joined}concat=n={len(parts)}:v=1:a=1[v][a]"
    ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *ENCODE, str(out))


def profile(path: Path) -> str:
    """One line per stream: what the normalisation guarantees (for checks and reports)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,profile,pix_fmt,width,height,sample_aspect_ratio,"
         "r_frame_rate,time_base,sample_rate,channels", "-of", "compact=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout.strip()
