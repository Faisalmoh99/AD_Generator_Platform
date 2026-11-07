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
            "montage_v2",
        ]
        assert all(v["url"].startswith("/docs-media/") for g in gallery for v in g["videos"])
        first_clip = gallery[0]["videos"][0]["url"]
        assert c.get(first_clip).headers["content-type"] == "video/mp4"
        assert c.get("/docs-media/..%2F..%2FREADME.md").status_code == 404
        assert c.get("/docs-media/nope.mp4").status_code == 404


def test_pipeline_view_data_decisions_and_sse_resume(tmp_path: Path) -> None:
    """The live pipeline view: who decides in each stage, per-event data for the swimlanes, the
    plain-language decision log, and SSE resume after the last id the browser saw."""
    with client(tmp_path) as c:
        run_id = c.post("/api/runs", json={"request": AR, "auto": True, "chaos": True}).json()["id"]
        v = wait(c, run_id, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
        kinds = {p["stage"]: p["kind"] for p in v["pipeline"]}
        assert kinds["select"] == "mixed" and kinds["assemble"] == "code"
        assert kinds["ideate"] == "llm"
        assert all(p["who"] for p in v["pipeline"])

        def events(headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
            out = []
            with c.stream("GET", f"/api/runs/{run_id}/events", headers=headers or {}) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        out.append(json.loads(line[len("data: ") :]))
                    if out and out[-1]["type"] == "run_completed":
                        break
            return out

        evs = events()
        phases = {e["data"].get("phase") for e in evs if e["type"] == "attempt_started"}
        assert {"keyframe", "clip"} <= phases
        assert any(e["data"].get("gate") == "keyframe" for e in evs)
        picks = [e["plain"] for e in evs if e["actor"] == "code" and e["stage"] == "select"]
        assert picks and picks[0].startswith("Code picked concept")
        assert any(e["actor"] == "llm" for e in evs)
        resumed = events({"Last-Event-ID": str(evs[9]["seq"])})
        assert resumed[0]["seq"] == evs[10]["seq"]  # no event lost or repeated


def test_page_and_assets_change_together(tmp_path: Path) -> None:
    """A stale cached app.js next to a new index.html broke the dashboard after the redesign:
    asset URLs carry their content hash, and the page and assets must be revalidated."""
    import hashlib  # noqa: PLC0415
    import re  # noqa: PLC0415

    from adsmith.api.app import STATIC  # noqa: PLC0415

    with client(tmp_path) as c:
        page = c.get("/")
        assert page.headers["cache-control"] == "no-cache"
        for name in ("app.js", "app.css"):
            digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
            assert f"/static/{name}?v={digest}" in page.text
        src = re.search(r'src="(/static/app\.js\?v=\w+)"', page.text)
        assert src is not None
        asset = c.get(src.group(1))
        assert asset.status_code == 200 and asset.headers["cache-control"] == "no-cache"


# ------------------------------------------------- studio minimum slice (ADR-0029)
def _photo_b64(tmp_path: Path) -> str:
    import base64  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    path = tmp_path / "p.png"
    Image.new("RGB", (360, 640), (170, 100, 30)).save(path)
    return base64.b64encode(path.read_bytes()).decode()


def test_a_photo_run_reaches_the_spend_gate_and_the_copy_can_be_chosen(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        body = {"request": "Make an 8-second ad for my perfume.", "photo": _photo_b64(tmp_path)}
        run_id = c.post("/api/runs", json=body).json()["id"]
        wait(c, run_id, lambda v: v["stage"] == "awaiting_concept_choice" and not v["busy"])
        assert c.post(f"/api/runs/{run_id}/choose", json={"concept": "2"}).status_code == 200
        v = wait(
            c, run_id, lambda v: v["stage"] == "awaiting_generation_approval" and not v["busy"]
        )
        copy = v["generation"]["copy"]
        assert copy["choosable"] and len(copy["brands"]) == 3 and copy["taglines"][0]["picked"]
        assert c.post(f"/api/runs/{run_id}/copy", json={"tagline": 2}).status_code == 200
        v = c.get(f"/api/runs/{run_id}").json()
        tags = v["generation"]["copy"]["taglines"]
        assert tags[1]["picked"] and tags[0]["code_pick"]
        assert c.post(f"/api/runs/{run_id}/copy", json={"tagline": 9}).status_code == 409
        assert c.post(f"/api/runs/{run_id}/decline", json={}).status_code == 200
        assert c.get(f"/api/runs/{run_id}").json()["stage"] == "done_partial"
        assert c.get(f"/api/runs/{run_id}/download").status_code == 404


def test_an_auto_photo_run_can_be_downloaded(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        body = {"request": "Make an 8-second ad for my perfume.", "auto": True,
                "photo": _photo_b64(tmp_path)}  # fmt: skip
        run_id = c.post("/api/runs", json=body).json()["id"]
        wait(c, run_id, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
        r = c.get(f"/api/runs/{run_id}/download")
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        assert f"adsmith_{run_id}.mp4" in r.headers["content-disposition"]


def test_a_photo_that_is_not_an_image_is_refused(tmp_path: Path) -> None:
    import base64  # noqa: PLC0415

    with client(tmp_path) as c:
        bad = base64.b64encode(b"not an image").decode()
        r = c.post("/api/runs", json={"request": "an ad", "photo": bad})
        assert r.status_code == 422 and "PNG, JPEG or WebP" in r.json()["detail"]
