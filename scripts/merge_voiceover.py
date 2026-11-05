"""Merge your voice recordings with the silent demo segments into docs/demo/final.mp4.

    uv run python scripts/merge_voiceover.py            # voice from docs/demo/voice/NN_name.m4a

For each segment in docs/demo/segments/NN_name.mp4 the matching docs/demo/voice/NN_name.m4a
(or .wav / .mp3 / .aiff) is fitted to the segment, never sped up:
- shorter than the segment: padded with silence after it;
- longer by up to --trim-tolerance seconds (default 0.8): its tail is trimmed with a short fade
  (trailing breath and silence first: silence at the end is removed before measuring);
- longer than that: the segment's last frame is held until the voice ends (a warning is printed),
  so no word is ever cut.
Voices are loudness-normalised (EBU R128, -16 LUFS). Under the hook only, the montage v2 music
(docs/demo/assets/v2_music.m4a) plays as a very low bed (--bed-db, default -18 dB, about 25 dB
under the voice) and fades out at the end of the segment. A missing recording leaves its segment
silent (warned). Every part is encoded to the shared demo profile (H.264 High, 1920x1080,
SAR 1:1, 30 fps, AAC stereo 48 kHz) and joined with a re-encode (scripts/demo_media.py), so
docs/demo/final.mp4 plays everywhere (QuickTime included).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from demo_media import AUDIO_FILTER, ENCODE, VIDEO_FILTER, concat, profile

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs" / "demo"
# The Lyria track montage v2 is cut to (generated in the v1 run, 30 s): the same music the hook's
# ad plays, long enough to carry the whole hook segment.
MUSIC = ROOT / "docs" / "demo" / "assets" / "v2_music.m4a"
HOOK = "01_hook"
EXTS = (".m4a", ".wav", ".mp3", ".aiff", ".aac")


def run(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(out.stdout.strip())


def find_voice(voice_dir: Path, name: str) -> Path | None:
    return next(
        (voice_dir / f"{name}{e}" for e in EXTS if (voice_dir / f"{name}{e}").is_file()), None
    )


def prepare_voice(src: Path, out: Path) -> float:
    """Loudness-normalised mono 48 kHz WAV with the trailing silence removed; returns its length."""
    run("-i", str(src), "-af",
        "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse,"
        "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000", "-ac", "1", str(out))  # fmt: skip
    return duration(out)


def merge_segment(video: Path, voice: Path | None, out: Path, args: argparse.Namespace,
                  work: Path) -> None:  # fmt: skip
    seg_s = duration(video)
    hold = 0.0
    voice_wav: Path | None = None
    if voice is not None:
        voice_wav = work / f"{video.stem}.voice.wav"
        v_s = prepare_voice(voice, voice_wav)
        over = v_s - seg_s
        if over > args.trim_tolerance:
            hold = over + 0.3
            print(f"  ! {video.stem}: voice {v_s:.2f}s > segment {seg_s:.2f}s: holding the last "
                  f"frame {hold:.2f}s so no word is cut")  # fmt: skip
        elif over > 0:
            print(f"  {video.stem}: voice {v_s:.2f}s, tail trimmed by {over:.2f}s (fade)")
        else:
            print(f"  {video.stem}: voice {v_s:.2f}s, padded {-over:.2f}s of silence")
    else:
        print(f"  ! {video.stem}: no recording in {args.voice_dir}; the segment stays silent")
    total = seg_s + hold
    fade_at = max(0.0, total - 0.25)
    inputs = ["-i", str(video)]
    chains: list[str] = []
    mix: list[str] = []
    if voice_wav is not None:
        inputs += ["-i", str(voice_wav)]
        chains.append(
            f"[1:a]adelay={int(args.lead * 1000)}|{int(args.lead * 1000)},apad,"
            f"atrim=0:{total:.3f},afade=t=out:st={fade_at:.3f}:d=0.25,asetpts=N/SR/TB[vo]"
        )
        mix.append("[vo]")
    if video.stem == HOOK and MUSIC.is_file():
        idx = len(inputs) // 2
        inputs += ["-i", str(MUSIC)]
        gain = 10 ** (args.bed_db / 20)
        chains.append(
            f"[{idx}:a]aresample=48000,aformat=channel_layouts=mono,volume={gain:.4f},apad,"
            f"atrim=0:{total:.3f},afade=t=out:st={max(0.0, total - 2.0):.3f}:d=2,"
            "asetpts=N/SR/TB[bed]"
        )
        mix.append("[bed]")
    if not mix:
        chains.append(f"anullsrc=r=48000:cl=mono,atrim=0:{total:.3f}[silent]")
        mix.append("[silent]")
    audio = (
        f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=longest[a]"
        if len(mix) > 1
        else f"{mix[0]}anull[a]"
    )
    hold_f = f"tpad=stop_mode=clone:stop_duration={hold:.3f}," if hold else ""
    vf = f"[0:v]{hold_f}{VIDEO_FILTER}[v]"
    audio = audio.replace("[a]", f",{AUDIO_FILTER}[a]") if audio.endswith("[a]") else audio
    graph = ";".join([*chains, audio, vf])
    # the shared demo profile (demo_media.py): identical parameters in every part
    run(*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
        *ENCODE, str(out))  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", type=Path, default=DEMO / "segments")
    ap.add_argument("--voice-dir", type=Path, default=DEMO / "voice")
    ap.add_argument("--out", type=Path, default=DEMO / "final.mp4")
    ap.add_argument("--trim-tolerance", type=float, default=0.8)
    ap.add_argument("--bed-db", type=float, default=-18.0, help="hook music bed level")
    ap.add_argument("--lead", type=float, default=0.25, help="silence before the voice starts")
    args = ap.parse_args()
    videos = sorted(args.segments.glob("[0-9][0-9]_*.mp4"))
    if not videos:
        sys.exit(f"no segments in {args.segments}")
    work = args.out.parent / ".merge"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    parts = []
    for v in videos:
        part = work / v.name
        merge_segment(v, find_voice(args.voice_dir, v.stem), part, args, work)
        parts.append(part)
    concat(parts, args.out)  # one re-encode, never stream copy (QuickTime-safe)
    shutil.rmtree(work)
    print(f"{args.out} · {duration(args.out):.2f}s")
    print(profile(args.out))


if __name__ == "__main__":
    main()
