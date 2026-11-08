"""Convert Channel SDK resource descriptors into Agno media objects."""

from __future__ import annotations

from typing import Any

from agno.media import File, Image


async def resource_to_media(channel: Any, message_id: str, resource: Any) -> Image | File:
    resource_type = str(getattr(resource, "type", "file")).lower()
    if resource_type not in {"image", "file"}:
        raise ValueError(f"Unsupported Lark/Feishu resource type: {resource_type}")
    data = await channel.download_resource(
        getattr(resource, "file_key", None), resource_type=resource_type, message_id=message_id
    )
    if data is None:
        raise RuntimeError("Lark/Feishu resource download returned no data")
    name = getattr(resource, "file_name", None) or getattr(resource, "name", None) or "attachment"
    mime = getattr(resource, "mime_type", None)
    if resource_type == "image":
        fmt = name.rsplit(".", 1)[-1] if "." in name else None
        return Image(content=data, format=fmt, mime_type=mime)
    fmt = name.rsplit(".", 1)[-1] if "." in name else None
    return File(content=data, filename=name, format=fmt, mime_type=mime)
