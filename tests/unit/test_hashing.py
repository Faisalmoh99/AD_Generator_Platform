"""Scene.content_hash / keyframe_spec_hash: the foundation of caching and blast radius."""

from __future__ import annotations

import unicodedata
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from adsmith.domain.base import stable_hash
from adsmith.domain.enums import CameraMove, ScenePurpose, Speed, TimeOfDay
from adsmith.domain.generation import GenerationRequest
from adsmith.domain.scene import Scene
from tests.factories import make_image, make_prompt, make_scene, make_shot, sha


def test_hash_is_stable_across_serialisation() -> None:
    s = make_scene()
    assert Scene.model_validate_json(s.model_dump_json()).content_hash() == s.content_hash()


def test_hash_ignores_identity_status_and_derived_data() -> None:
    a = make_scene(order=1)
    b = make_scene(order=3, id="scn_other")  # identity and position are not content
    b.set_prompt(make_prompt())
    b.start()
    b.complete_keyframe(make_image())
    b.bump_version("whatever")
    assert a.content_hash() == b.content_hash()


@pytest.mark.parametrize(
    "change",
    [
        {"description": "something else"},
        {"purpose": ScenePurpose.HOOK},
        {"duration_s": 4.0},
        {"shows_product": True},
        {"shows_character": False},
        {"shot": make_shot(time_of_day=TimeOfDay.DAWN)},
        {"shot": make_shot(lens="35mm")},
        {"shot": make_shot(camera_movement=CameraMove.ORBIT)},
    ],
)
def test_every_spec_field_changes_content_hash(change: dict[str, Any]) -> None:
    assert make_scene(**change).content_hash() != make_scene().content_hash()


@pytest.mark.parametrize(
    "change",
    [
        {"duration_s": 5.0},
        {"shot": make_shot(camera_movement=CameraMove.ORBIT)},
        {"shot": make_shot(speed=Speed.SLOW_MOTION)},
    ],
)
def test_motion_only_changes_keep_keyframe_hash(change: dict[str, Any]) -> None:
    base, changed = make_scene(), make_scene(**change)
    assert changed.content_hash() != base.content_hash()
    assert changed.keyframe_spec_hash() == base.keyframe_spec_hash()


@pytest.mark.parametrize(
    "change",
    [
        {"shot": make_shot(lighting="soft daylight")},
        {"shot": make_shot(time_of_day=TimeOfDay.DAY)},
        {"shot": make_shot(subject_action="he turns away")},
        {"description": "new description"},
    ],
)
def test_look_changes_change_keyframe_hash(change: dict[str, Any]) -> None:
    assert make_scene(**change).keyframe_spec_hash() != make_scene().keyframe_spec_hash()


def test_unicode_normalisation_in_hash() -> None:
    composed = unicodedata.normalize("NFC", "café")
    decomposed = unicodedata.normalize("NFD", "café")
    assert composed != decomposed
    assert stable_hash("t", composed) == stable_hash("t", decomposed)


def test_namespace_versions_hash() -> None:
    assert stable_hash("scene.content.v1", {"a": 1}) != stable_hash("scene.content.v2", {"a": 1})


@settings(max_examples=60, deadline=None)
@given(
    order=st.integers(min_value=1, max_value=6),
    version_bumps=st.integers(min_value=0, max_value=3),
    with_prompt=st.booleans(),
)
def test_property_non_spec_mutations_never_change_hash(
    order: int, version_bumps: int, with_prompt: bool
) -> None:
    base = make_scene().content_hash()
    s = make_scene(order=order)
    if with_prompt:
        s.set_prompt(make_prompt())
    for i in range(version_bumps):
        s.bump_version(f"r{i}")
    assert s.content_hash() == base


class TestGenerationRequestCacheKey:
    def _req(self, **over: Any) -> GenerationRequest:
        base: dict[str, Any] = {
            "provider": "mock",
            "model": "mock-video",
            "phase": "clip",
            "prompt": "slow dolly in",
            "params": {"duration": 4, "aspect_ratio": "9:16"},
            "input_image_sha256": sha("kf"),
        }
        return GenerationRequest.model_validate(base | over)

    def test_stable_and_param_order_independent(self) -> None:
        a = self._req(params={"duration": 4, "aspect_ratio": "9:16"})
        b = self._req(params={"aspect_ratio": "9:16", "duration": 4})
        assert a.cache_key() == b.cache_key()

    @pytest.mark.parametrize(
        "change",
        [
            {"provider": "fal"},
            {"model": "other"},
            {"prompt": "fast dolly in"},
            {"params": {"duration": 5, "aspect_ratio": "9:16"}},
            {"input_image_sha256": sha("kf2")},
        ],
    )
    def test_any_input_change_misses(self, change: dict[str, Any]) -> None:
        assert self._req(**change).cache_key() != self._req().cache_key()

    def test_clip_requires_input_image(self) -> None:
        with pytest.raises(ValueError, match="input_image_sha256"):
            self._req(input_image_sha256=None)
