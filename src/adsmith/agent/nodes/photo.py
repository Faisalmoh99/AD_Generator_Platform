"""The real product photo as an image part for LLM calls that must see the product (continuity,
the ad prompt writer, the creative director)."""

from __future__ import annotations

import asyncio
import base64

from adsmith.agent.context import NodeContext
from adsmith.domain.run import RunState
from adsmith.media.frames import image_to_jpeg
from adsmith.providers.llm.base import ImagePart, TextPart


async def product_photo_parts(
    state: RunState, ctx: NodeContext, label: str = "Image 0: the real product photo"
) -> list[ImagePart | TextPart]:
    if state.product_photo is None:
        return []
    jpeg = await asyncio.to_thread(
        image_to_jpeg, ctx.artifacts.resolve(state.product_photo), long_side=1024
    )
    return [TextPart(label), ImagePart("image/jpeg", base64.b64encode(jpeg).decode("ascii"))]
