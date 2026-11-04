"""``adsmith demo`` / ``make demo``: the zero-key showcase, about one minute on mock media.

1. The Arabic perfume request, ``--auto --chaos`` (seed from config/chaos.toml): live trace of
   planning, keyframe gate, generation with injected faults and their recoveries (transient retry,
   policy rewrite, QA regeneration), assembly with the Arabic end card.
2. One natural-language edit ("خل المشهد الثاني أبطأ") with the blast-radius table: only scene 2
   regenerates (clip only), the others cost $0.
3. Crash and resume: a second run in a child process is killed with SIGKILL mid-generation;
   ``resume`` releases the interrupted lease and reservation and finishes the run. Clips that were
   done before the crash are not generated again.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from adsmith.cli.live import LiveTrace
from adsmith.config import PROJECT_ROOT, Settings
from adsmith.domain.enums import ScenePhase, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState

REQUEST = "اعطني إعلان ١٥ ثانية لعطر رجالي فاخر يستهدف الشباب بأسلوب سينمائي"
EDIT = "خل المشهد الثاني أبطأ"


def _settings(data_dir: Path, latency: float) -> dict[str, str]:
    return {
        "ADSMITH_DATA_DIR": str(data_dir),
        "ADSMITH_LLM_PROVIDER": "scripted",
        "ADSMITH_MEDIA_PROVIDER": "mock",
        "ADSMITH_QA_MODE": "mock",
        "ADSMITH_MOCK_LATENCY_S": str(latency),
        "ADSMITH_BUDGET_USD": "15",  # simulated money: mock media is priced at Kling rates
        "ADSMITH_MOCK_FAIL_PLAN": "",
    }


def run_demo(console: Console, *, crash: bool, latency: float) -> None:
    from adsmith.cli.main import _orchestrator, print_state, run_edit  # noqa: PLC0415

    data_dir = PROJECT_ROOT / "data" / "demo"
    shutil.rmtree(data_dir, ignore_errors=True)
    env = _settings(data_dir, latency)
    os.environ.update(env)
    orch = _orchestrator(Settings())
    started = time.monotonic()
    console.print(
        Panel.fit(
            "[bold]Adsmith demo[/]: zero keys · scripted LLM · mock media · Chaos Mode on\n"
            f"[dim]Request:[/] {REQUEST}",
            border_style="cyan",
        )
    )

    # 1. chaos run -------------------------------------------------------------------------
    console.rule("[bold cyan]1 · Full run with injected faults[/]")
    state = orch.create_run(REQUEST, interactive=False, chaos=True)
    trace = LiveTrace(console, orch.rt.storage)
    state = asyncio.run(trace.follow(state.id, orch.run_until_blocked(state.id)))
    print_state(orch, state)
    events = orch.rt.storage.events.tail(state.id, limit=100_000)
    recov = [e for e in events if e.type in {EventType.RETRY_SCHEDULED, EventType.RECOVERY}]
    console.print(
        f"[bold]Recoveries:[/] {sum(e.type is EventType.RETRY_SCHEDULED for e in recov)} retries, "
        f"{_revisions(recov, 'policy_rewrite')} policy rewrites, "
        f"{_revisions(recov, 'qa_regenerate')} QA regenerations "
        f"(all chaos-injected and labelled in the trace)"
    )

    # 2. edit ----------------------------------------------------------------------------
    console.rule("[bold magenta]2 · Natural-language edit[/]")
    _chaos_off(orch, state.id)
    run_edit(orch, state.id, EDIT, None)

    # 3. crash / resume --------------------------------------------------------------------
    if crash:
        console.rule("[bold red]3 · Kill mid-generation, then resume[/]")
        _crash_resume(console, orch, env)
    console.print(
        f"\n[dim]Demo finished in {time.monotonic() - started:.0f}s · data: {data_dir}[/]"
    )


def _revisions(events: list[AgentEvent], strategy: str) -> int:
    """Count applied revisions only (each also has a preceding 'rejected' recovery event)."""
    return sum(
        1 for e in events if e.payload.get("strategy") == strategy and "keyframe_kept" in e.payload
    )


def _chaos_off(orch: object, run_id: str) -> None:
    """The edit demo shows the blast radius, so no injected faults muddy it."""
    from adsmith.agent.orchestrator import Orchestrator  # noqa: PLC0415

    assert isinstance(orch, Orchestrator)

    def mutate(s: RunState) -> list[AgentEvent]:
        s.config = s.config.model_copy(update={"chaos": False})
        return [
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                type=EventType.DECISION,
                summary="Chaos Mode off for the edit demo (clean blast radius).",
            )
        ]

    orch.rt.storage.runs.update(run_id, mutate, reason="chaos off")
    orch.rt._gateways.pop(run_id, None)


def _crash_resume(console: Console, orch: object, env: dict[str, str]) -> None:
    from adsmith.agent.orchestrator import Orchestrator  # noqa: PLC0415
    from adsmith.cli.main import _orchestrator, print_state  # noqa: PLC0415

    assert isinstance(orch, Orchestrator)
    # Own data dir: a shared cache would make every clip of this second run a hit (nothing to kill).
    crash_dir = Path(env["ADSMITH_DATA_DIR"]) / "crash"
    child_env = {
        **os.environ,
        **env,
        "ADSMITH_DATA_DIR": str(crash_dir),
        "ADSMITH_MOCK_LATENCY_S": "1.2",
    }
    os.environ.update({"ADSMITH_DATA_DIR": str(crash_dir)})
    orch = _orchestrator(Settings())
    store = orch.rt.storage
    known = {r.id for r in store.runs.list_runs()}
    child = subprocess.Popen(
        [sys.executable, "-m", "adsmith.cli.main", "run", REQUEST, "--auto", "--no-chaos"],
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    run_id, deadline = None, time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(0.1)
        new = [r for r in store.runs.list_runs() if r.id not in known]
        if not new:
            continue
        run_id = new[0].id
        st = store.runs.load(run_id)
        done = [
            a for s in (st.storyboard.scenes if st.storyboard else []) for a in s.attempts
            if a.phase is ScenePhase.CLIP and a.outcome == "success"
        ]  # fmt: skip
        busy = [s for s in (st.storyboard.scenes if st.storyboard else [])
                if s.status.value == "generating"]  # fmt: skip
        if st.stage is Stage.GENERATE and done and busy:
            break
        if st.stage in {Stage.DONE, Stage.DONE_PARTIAL}:
            break
    child.send_signal(signal.SIGKILL)
    child.wait()
    if run_id is None:
        console.print("[red]the child run never started; skipping[/]")
        return
    st = store.runs.load(run_id)
    assert st.storyboard is not None
    clips_before = {
        s.order for s in st.storyboard.scenes
        for a in s.attempts if a.phase is ScenePhase.CLIP and a.outcome == "success"
    }  # fmt: skip
    in_flight = [s.order for s in st.storyboard.scenes if s.status.value == "generating"]
    console.print(
        f"[bold red]💥 killed[/] run {run_id} at stage {st.stage.value}: clips done for scenes "
        f"{sorted(clips_before)}, in flight {in_flight}, reserved ${st.budget.reserved_usd:.2f}"
    )
    trace = LiveTrace(console, store)
    seq = trace.last_seq(run_id)
    final = asyncio.run(trace.follow(run_id, orch.resume(run_id)))
    print_state(orch, final)
    after = store.events.tail(run_id, after_seq=seq, limit=100_000)
    regen = sorted(
        {
            final.storyboard.by_id(e.scene_id).order  # type: ignore[union-attr]
            for e in after
            if e.type is EventType.ATTEMPT_STARTED
            and e.scene_id
            and e.payload.get("est_usd")
            and "clip" in e.summary
        }
    )
    table = Table("Check", "Result", title="Crash / resume")
    table.add_row("clips finished before the kill", str(sorted(clips_before)))
    table.add_row("clips generated after resume", str(regen))
    table.add_row(
        "regenerated twice", "[green]none[/]" if not set(regen) & clips_before else "[red]yes[/]"
    )
    table.add_row("leaked reservations", f"${final.budget.reserved_usd:.2f}")
    console.print(table)
