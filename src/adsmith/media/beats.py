"""Beats from user-supplied music and beat-snapped cuts (PLAN_V2 item 6). No model involved.

1. ``decode_pcm``: ffmpeg → mono float PCM.
2. ``onset_envelope``: half-wave rectified spectral flux of the log magnitude spectrum
   (rectangular 512-sample frames, 128-sample hop: ≈ 6 ms resolution at 22.05 kHz).
3. ``beat_grid``: tempo and phase by comb scoring (on-beat minus off-beat onset strength on a
   candidate grid, 60-180 bpm, a mild prior toward 120 bpm), then each grid beat is snapped to the
   strongest onset within ±40 ms. Downbeats: the one of four phases with the most accent.
4. ``snap_cuts``: every shot boundary moves to the nearest beat within ±slack (the hero shot's
   start prefers a downbeat, the end card starts on a beat), never outside a shot's min/max.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from adsmith.agent.recovery.errors import PermanentProviderError

SR = 22_050
FRAME = 512
HOP = 128
_SNAP_S = 0.04


@dataclass(frozen=True, slots=True)
class BeatGrid:
    bpm: float
    beats: tuple[float, ...]
    downbeats: tuple[float, ...]


async def decode_pcm(path: Path, *, sr: int = SR) -> NDArray[np.float32]:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-f", "s16le", "-ac", "1", "-ar", str(sr), "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await proc.communicate()
    if proc.returncode != 0 or not out:
        raise PermanentProviderError(f"decode {path.name}: {err.decode(errors='replace')[-300:]}")
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0


def onset_envelope(
    samples: NDArray[np.float32], *, sr: int = SR
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """(times_s, strength) of the spectral flux, normalised to max 1."""
    n = 1 + (len(samples) - FRAME) // HOP
    if n < 3:
        return np.zeros(0), np.zeros(0)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    mag = np.log1p(np.abs(np.fft.rfft(samples[idx], axis=1)) * 10.0)
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    # frame i+1 is the first to contain the new sound: time = its end minus half a hop
    times = (HOP * np.arange(1, n) + FRAME - HOP / 2) / sr
    peak = float(flux.max())
    return times.astype(np.float64), (flux / peak if peak > 0 else flux).astype(np.float64)


def beat_grid(
    times: NDArray[np.float64], env: NDArray[np.float64], *, lo: float = 60, hi: float = 180
) -> BeatGrid:
    if env.size == 0:
        return BeatGrid(0.0, (), ())
    dt = float(times[1] - times[0]) if times.size > 1 else HOP / SR
    t0, end = float(times[0]), float(times[-1])

    def strength(at: NDArray[np.float64]) -> NDArray[np.float64]:
        i = np.clip(np.round((at - t0) / dt).astype(int), 0, env.size - 1)
        picked: NDArray[np.float64] = env[i]
        return picked

    best: tuple[float, float, float] = (-1.0, 0.0, 0.0)  # score, bpm, phase
    for bpm in np.arange(lo, hi + 0.25, 0.25):
        period = 60.0 / float(bpm)
        for phase in np.arange(0.0, period, dt):
            grid = np.arange(t0 + phase, end, period)
            if grid.size < 2:
                continue
            # On-beat strength minus off-beat strength: a grid at half the tempo has real beats on
            # its off-beats, a grid at double the tempo has silence on half its beats.
            off = strength(grid + period / 2)
            score = float(strength(grid).mean() - off.mean())
            score -= 0.02 * abs(math.log2(float(bpm) / 120.0))
            if score > best[0] + 1e-9:
                best = (score, float(bpm), float(phase))
    _, bpm, phase = best
    period = 60.0 / bpm
    beats: list[float] = []
    for b in np.arange(t0 + phase, end + 1e-9, period):
        near = (times >= b - _SNAP_S) & (times <= b + _SNAP_S)
        beats.append(float(times[near][np.argmax(env[near])]) if near.any() else float(b))
    arr = np.array(beats)
    accents = [float(strength(arr[k::4]).sum()) for k in range(min(4, arr.size))]
    first = int(np.argmax(accents)) if accents else 0
    return BeatGrid(
        bpm=round(bpm, 2),
        beats=tuple(round(b, 4) for b in beats),
        downbeats=tuple(round(b, 4) for b in beats[first::4]),
    )


async def detect_beats(path: Path) -> BeatGrid:
    times, env = await asyncio.to_thread(onset_envelope, await decode_pcm(path))
    return await asyncio.to_thread(beat_grid, times, env)


def snap_cuts(
    durations: Sequence[float],
    grid: BeatGrid,
    *,
    slack: float = 0.2,
    hero_index: int | None = None,
    min_s: Sequence[float] | None = None,
    max_s: Sequence[float] | None = None,
) -> list[float]:
    """New shot durations whose boundaries (and the end card's start) sit on beats where a beat
    lies within ±``slack`` and the shot stays within its bounds; otherwise unchanged."""
    n = len(durations)
    lo = list(min_s) if min_s is not None else [0.5] * n
    hi = list(max_s) if max_s is not None else [math.inf] * n
    planned = np.cumsum(durations).tolist()
    beats = np.array(grid.beats)
    downs = set(grid.downbeats)
    out: list[float] = []
    prev = 0.0
    for k in range(n):  # boundary k+1 = end of shot k (= start of shot k+1, or of the end card)
        target = planned[k]
        options = beats[np.abs(beats - target) <= slack + 1e-9] if beats.size else beats
        options = np.array(
            [c for c in options if lo[k] - 1e-9 <= c - prev <= hi[k] + 1e-9], dtype=float
        )
        chosen = target
        if options.size:
            starts_hero = hero_index is not None and k + 1 == hero_index
            pool = np.array([c for c in options if c in downs]) if starts_hero else options
            pool = pool if pool.size else options
            chosen = float(pool[np.argmin(np.abs(pool - target))])
        chosen = min(max(chosen, prev + lo[k]), prev + hi[k])
        out.append(round(chosen - prev, 4))
        prev = chosen
    return out
