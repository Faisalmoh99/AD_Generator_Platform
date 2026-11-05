"""Splice the demo narration from the raw voice notes and the pickups (local, numpy + ffmpeg).

Cut points come from word timestamps and are refined to the quietest 10 ms frame within
±150 ms (verified cut points are given exactly), joined with 20 ms equal-power crossfades. Each
pickup is gain-matched to the recording it goes into (EBU R128 integrated loudness); each file
is then set to −16 LUFS. A gentle peak limiter (−1 dBFS ceiling, 2 ms attack, 80 ms release)
only acts where peaks exceed the ceiling. Stumbles and self-corrections are kept.

    uv run python scripts/splice_voice.py --plan docs/demo/voice/splice_plan.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import numpy as np

SR = 48_000
RAW = Path(__file__).resolve().parents[1] / "docs" / "demo" / "voice" / "raw"
XF = 0.020  # crossfade seconds


def load(stamp: str) -> np.ndarray:
    src = next(RAW.glob(f"*{stamp}.opus"))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def lufs(x: np.ndarray) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-f", "f64le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-af", "ebur128", "-f", "null", "-"],
        input=x.astype(np.float64).tobytes(), capture_output=True, check=True,
    )  # fmt: skip
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", out.stderr.decode())
    return float(m[-1])


def quietest(x: np.ndarray, t: float, win: float = 0.15) -> float:
    """The quietest 10 ms frame centre within ±win of t (seconds)."""
    hop = int(0.010 * SR)
    a, b = max(0, int((t - win) * SR)), min(len(x) - hop, int((t + win) * SR))
    best, best_t = np.inf, t
    for i in range(a, b, hop // 2):
        e = float(np.sqrt(np.mean(x[i : i + hop] ** 2)))
        if e < best:
            best, best_t = e, (i + hop / 2) / SR
    return best_t


def level_db(x: np.ndarray, t: float) -> float:
    i = int(t * SR)
    w = x[max(0, i - 240) : i + 240]
    return 20 * np.log10(np.sqrt(np.mean(w**2)) + 1e-9)


def join(parts: list[np.ndarray]) -> np.ndarray:
    n = int(XF * SR)
    out = parts[0]
    ramp = np.linspace(0, np.pi / 2, n)
    for p in parts[1:]:
        if len(out) < n or len(p) < n:
            out = np.concatenate([out, p])
            continue
        mixed = out[-n:] * np.cos(ramp) + p[:n] * np.sin(ramp)
        out = np.concatenate([out[:-n], mixed, p[n:]])
    return out


def limit(x: np.ndarray, ceiling_db: float = -1.0) -> tuple[np.ndarray, int]:
    """Peak limiter that only engages where peaks exceed the ceiling: 2 ms look-ahead, 2 ms
    attack, 80 ms release, processed only in windows around the hot samples. Returns the
    signal and the number of samples whose gain it reduced."""
    thr = 10 ** (ceiling_db / 20)
    hot = np.flatnonzero(np.abs(x) > thr)
    if hot.size == 0:
        return x, 0
    y = x.copy()
    touched = 0
    a, r = np.exp(-1 / (0.002 * SR)), np.exp(-1 / (0.080 * SR))
    look = int(0.002 * SR)
    pad = int(0.25 * SR)
    clusters: list[list[int]] = []
    for i in hot:
        if clusters and i - clusters[-1][1] < pad:
            clusters[-1][1] = int(i)
        else:
            clusters.append([int(i), int(i)])
    for c0, c1 in clusters:
        s0, s1 = max(0, c0 - pad), min(len(x), c1 + pad)
        seg = x[s0:s1]
        need = np.minimum(1.0, thr / np.maximum(np.abs(seg), 1e-12))
        need = np.lib.stride_tricks.sliding_window_view(
            np.concatenate([need, np.ones(look)]), look + 1).min(axis=1)  # fmt: skip
        g = np.empty_like(need)
        cur = 1.0
        for k, v in enumerate(need):
            cur = a * cur + (1 - a) * v if v < cur else r * cur + (1 - r) * v
            g[k] = min(cur, v)
        y[s0:s1] = seg * g
        touched += int(np.count_nonzero(g < 0.999))
    return y, touched


def flat_tops(x: np.ndarray, run: int = 5) -> list[float]:
    """Times where the recording itself was clipped: ≥ ``run`` consecutive samples near the
    file's peak that barely change (a flat top). Opus overshoot above 0 dBFS is not clipping."""
    a = np.abs(x)
    m = float(a.max())
    hot = a > 0.95 * m
    flat = np.abs(np.diff(x)) < 0.002 * m
    times: list[float] = []
    k = 0
    for i in range(1, len(x)):
        if hot[i] and hot[i - 1] and flat[i - 1]:
            k += 1
        else:
            if k >= run and (not times or i / SR - times[-1] > 0.5):
                times.append(round(i / SR, 2))
            k = 0
    return times


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--previews", type=Path, required=True)
    args = ap.parse_args()
    plan = json.loads(args.plan.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    args.previews.mkdir(parents=True, exist_ok=True)
    cache: dict[str, np.ndarray] = {}
    report: dict[str, object] = {}
    for seg in plan["segments"]:
        name = seg["name"]
        base_stamp = seg.get("base", seg["pieces"][0]["src"])
        base = cache.setdefault(base_stamp, load(base_stamp))
        target = lufs(base)
        parts: list[np.ndarray] = []
        joins: list[float] = []
        notes: list[str] = []
        for pc in seg["pieces"]:
            x = cache.setdefault(pc["src"], load(pc["src"]))
            a = (
                quietest(x, pc["start"])
                if pc.get("refine_start", True) and pc["start"] > 0
                else pc["start"]
            )
            b = (
                quietest(x, pc["end"])
                if pc.get("refine_end", True) and pc["end"] < len(x) / SR
                else min(pc["end"], len(x) / SR)
            )
            piece = x[int(a * SR) : int(b * SR)]
            if pc["src"] != base_stamp:
                piece = piece * 10 ** ((target - lufs(x)) / 20)  # match the recording it joins
            for cut in pc.get("remove", []):
                ca, cb = quietest(x, cut[0]) - a, quietest(x, cut[1]) - a
                lv = f"{level_db(x, cut[0]):.0f} / {level_db(x, cut[1]):.0f} dBFS"
                notes.append(f"{pc['src']} cut {cut[0]:.2f}-{cut[1]:.2f}s: levels at the cut {lv}")
                piece = join([piece[: int(ca * SR)], piece[int(cb * SR) :]])
            if parts:
                joins.append(sum(len(p) for p in parts) / SR - XF * (len(parts) - 1))
            if pc.get("pause_before"):
                parts.append(np.zeros(int(pc["pause_before"] * SR)))
            parts.append(piece)
            notes.append(f"{pc['src']} {a:.2f}-{b:.2f}s (levels at the cuts "
                         f"{level_db(x, a):.0f} / {level_db(x, b):.0f} dBFS)")  # fmt: skip
        y = join(parts)
        clipped = flat_tops(y)
        y = y * 10 ** ((-16.0 - lufs(y)) / 20)  # the delivery level, then peaks over -1 dBFS
        before = y.copy()
        y, _ = limit(y)
        gr = 20 * np.log10(np.maximum(np.abs(y), 1e-9) / np.maximum(np.abs(before), 1e-9))
        loud = np.abs(before) > 0.05
        wav = args.out / f"{name}.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "f64le", "-ar", str(SR), "-ac", "1", "-i", "-",
             "-c:a", "pcm_s24le", str(wav)], input=y.tobytes(), check=True,
        )  # fmt: skip
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "192k",
             str(args.out / f"{name}.m4a")], check=True,
        )  # fmt: skip
        for k, t in enumerate(joins):
            s0 = max(0.0, t - 5)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{s0:.2f}",
                 "-t", f"{min(10.0, t + 5 - s0):.2f}", "-i", str(wav),
                 "-c:a", "aac", "-b:a", "192k",
                 str(args.previews / f"{name}_splice{k + 1}_at_"
                     f"{int(t // 60):02d}m{t % 60:04.1f}s.m4a")],
                check=True,
            )  # fmt: skip
        report[name] = {"seconds": round(len(y) / SR, 2), "joins_s": [round(j, 2) for j in joins],
                        "limiter_max_gr_db": round(float(gr.min()), 1),
                        "limiter_gr_over_3db_pct": round(float(np.mean(gr[loud] < -3) * 100), 2),
                        "source_clipping_at_s": clipped,
                        "peak_dbfs_before_limiter": round(
                            float(20 * np.log10(np.max(np.abs(before)) + 1e-9)), 2),
                        "notes": notes}  # fmt: skip
        print(name, report[name])
    (args.out / "splice_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
