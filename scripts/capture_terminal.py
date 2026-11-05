"""Record a command's terminal output with timestamps through a pseudo-terminal (colours and
Rich live output intact), for replay in a browser terminal (``docs/demo/terminal.html``).

    uv run python scripts/capture_terminal.py --out docs/demo/make_demo.json -- make demo
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import pty
import select
import struct
import termios
import time
from fcntl import ioctl
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cols", type=int, default=110)
    ap.add_argument("--rows", type=int, default=34)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = [c for c in args.cmd if c != "--"]
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(args.cols),
           "LINES": str(args.rows), "FORCE_COLOR": "1"}  # fmt: skip
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(cmd[0], cmd, env)
    ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", args.rows, args.cols, 0, 0))
    t0, frames = time.monotonic(), []
    # incremental: a multi-byte character split across two reads must not become U+FFFD
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            continue
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        text = decoder.decode(data)
        if text:
            frames.append([round(time.monotonic() - t0, 3), text])
    args.out.write_text(json.dumps({"cols": args.cols, "rows": args.rows, "frames": frames}))
    print(f"{len(frames)} chunks, {frames[-1][0] if frames else 0:.1f} s → {args.out}")


if __name__ == "__main__":
    main()
