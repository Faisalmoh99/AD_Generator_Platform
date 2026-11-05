"""M10: the web API end to end (zero-key: scripted LLM + mock media)."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adsmith.api.app import create_app
from tests.media_helpers import fast_settings

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
AR = "سوّ لي إعلان 15 ثانية لعطر رجالي فاخر يستهدف الشباب، بأسلوب سينمائي"


def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(fast_settings(tmp_path, budget_usd=15.0)))


def wait(c: TestClient, run_id: str, pred: Any, timeout: float = 60) -> dict[str, Any]:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        v: dict[str, Any] = c.get(f"/api/runs/{run_id}").json()
        if pred(v):
            return v
        time.sleep(0.1)
    raise TimeoutError(v["stage"])


def test_auto_run_produces_a_final_ad_and_a_pack(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        assert c.get("/").status_code == 200 and "Adsmith" in c.get("/").text
        assert c.get("/api/config").json()["zero_key"] is True
        run_id = c.post("/api/runs", json={"request": AR, "auto": True, "chaos": True}).json()["id"]
        v = wait(c, run_id, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
        assert v["final"].startswith("/files/") and c.get(v["final"]).status_code == 200
        assert len(v["concepts"]) == 3 and any(x["selected"] for x in v["concepts"])
        assert all(x["score"] is not None for x in v["concepts"])
        assert v["endcard"]["brand_placeholder"] is True
        assert any(s["recoveries"] for s in v["scenes"])  # chaos recoveries visible per scene
        assert [p["state"] for p in v["pipeline"]].count("done") == len(v["pipeline"])
        pack = c.get(f"/api/runs/{run_id}/pack.zip")
        assert pack.status_code == 200 and pack.content[:2] == b"PK"


def test_interactive_gates_recommendation_and_edit_blast_radius(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        run_id = c.post("/api/runs", json={"request": AR, "auto": False}).json()["id"]
        v = wait(c, run_id, lambda v: v["stage"] == "awaiting_concept_choice")
        assert sum(x["recommended"] for x in v["concepts"]) == 1  # the judge recommends
        assert not any(x["selected"] for x in v["concepts"])  # but the human picks
        assert c.post(f"/api/runs/{run_id}/choose", json={"concept": "3"}).status_code == 200
        wait(c, run_id, lambda v: v["stage"] == "awaiting_storyboard_approval" and not v["busy"])
        assert c.post(f"/api/runs/{run_id}/approve").status_code == 200
        wait(c, run_id, lambda v: v["stage"] == "done" and not v["busy"])
        out = c.post(f"/api/runs/{run_id}/edit", json={"text": "خل المشهد الثاني أبطأ"}).json()
        assert out["status"] == "applied"
        v = wait(c, run_id, lambda v: v["stage"] == "done" and not v["busy"])
        assert v["blast"]["affected"] == {"2": "clip"} and v["blast"]["untouched"] == [1, 3, 4]
        bad = c.post(f"/api/runs/{run_id}/edit", json={"text": "غيّر شي"}).json()
        assert bad["status"] == "clarify"


def test_events_stream_and_files_are_path_checked(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        run_id = c.post("/api/runs", json={"request": AR, "auto": True}).json()["id"]
        wait(c, run_id, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
        with c.stream("GET", f"/api/runs/{run_id}/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            first = next(line for line in resp.iter_lines() if line.startswith("data: "))
        event = json.loads(first[len("data: ") :])
        assert event["seq"] == 1 and "tags" in event
        assert c.get("/files/../../etc/passwd").status_code == 404
        assert c.get("/files/adsmith.db").status_code == 404  # never the database
        assert c.get("/api/runs/run_nope").status_code == 404
        # Fresh data dir: the gallery falls back to the clips committed in docs/media.
        gallery = c.get("/api/gallery").json()
        assert [g["key"] for g in gallery] == [
            "majlis",
            "single_take",
            "curtain",
            "hero_spray",
            "montage_v1",
        ]
        assert all(v["url"].startswith("/docs-media/") for g in gallery for v in g["videos"])
        first_clip = gallery[0]["videos"][0]["url"]
        assert c.get(first_clip).headers["content-type"] == "video/mp4"
        assert c.get("/docs-media/..%2F..%2FREADME.md").status_code == 404
        assert c.get("/docs-media/nope.mp4").status_code == 404
