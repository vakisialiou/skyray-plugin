"""Turns one object's GLTF_SEPARATE export into what a Send uploads: a glb
whose images name their textures as `skyray:<key>` URIs instead of carrying
them, the texture blobs themselves (keyed by content, so a texture used by
many objects travels once), and the list of keys the glb names.

The glb is gzip-compressed: meshopt-encoded geometry is built to shrink
further under a general-purpose compressor. Images are not: WebP/PNG already
are compressed.
"""

import gzip
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Optional

_SKYRAY_SCHEME = "skyray:"
_MESHOPT = "EXT_meshopt_compression"
_MIME_BY_EXT = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@dataclass
class Texture:
    key: str
    mime: str
    data: bytes
    width: int
    height: int


@dataclass
class Pack:
    """One object's export, ready to upload."""

    glb: bytes  # gzip-compressed
    textures: list = field(default_factory=list)  # [Texture]
    refs: list = field(default_factory=list)  # texture keys the glb names

    def size(self) -> int:
        return len(self.glb) + sum(len(texture.data) for texture in self.textures)


def content_key(prefix: str, data: bytes) -> str:
    """`<prefix>:<first 32 hex digits of SHA-256>`, as the browser names local blobs."""
    return f"{prefix}:{hashlib.sha256(data).hexdigest()[:32]}"


def edit_glb(glb: bytes, edit_json) -> bytes:
    """`edit_json(gltf)` on a GLB's parsed JSON chunk, True when it changed
    anything. Rewrites only that chunk (the BIN chunk is copied verbatim); a
    GLB with nothing to change comes back byte-identical."""
    magic, version, _length = struct.unpack_from("<4sII", glb, 0)
    json_length, json_type = struct.unpack_from("<I4s", glb, 12)
    if magic != b"glTF" or json_type != b"JSON":
        raise ValueError("not a GLB with a leading JSON chunk")
    gltf = json.loads(glb[20 : 20 + json_length])
    if not edit_json(gltf):
        return glb

    chunk = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    chunk += b" " * (-len(chunk) % 4)  # GLB chunks are 4-byte aligned, JSON padded with spaces
    body = struct.pack("<I4s", len(chunk), b"JSON") + chunk + glb[20 + json_length :]
    return struct.pack("<4sII", magic, version, 12 + len(body)) + body


def pack_separate(directory: str, gltf_name: str, edit_json=None) -> Pack:
    """Reads `directory/gltf_name` (+ its .bin and image files) and packs it.
    `edit_json(gltf)` may change the parsed JSON first (material extras)."""
    with open(os.path.join(directory, gltf_name), "rb") as handle:
        gltf = json.load(handle)
    if edit_json is not None:
        edit_json(gltf)

    textures: dict = {}
    for image in gltf.get("images", []):
        uri = image.get("uri")
        if not isinstance(uri, str) or uri.startswith(_SKYRAY_SCHEME):
            continue
        with open(os.path.join(directory, uri), "rb") as handle:
            data = handle.read()
        mime = image.get("mimeType") or _MIME_BY_EXT.get(os.path.splitext(uri)[1].lower(), "image/png")
        key = content_key("t", data)
        width, height = image_size(data)
        textures.setdefault(key, Texture(key, mime, data, width, height))
        image["uri"] = _SKYRAY_SCHEME + key
        image["mimeType"] = mime

    binary = _move_bin_buffer_first(gltf, directory)
    return Pack(
        glb=gzip.compress(_glb(gltf, binary), compresslevel=6),
        textures=list(textures.values()),
        refs=list(textures),
    )


def _move_bin_buffer_first(gltf: dict, directory: str) -> bytes:
    """A glb's binary chunk can only be buffer 0. With meshopt the exporter
    writes an empty fallback buffer first and the real data second, so the
    data buffer moves to index 0 and every reference follows."""
    buffers = gltf.get("buffers", [])
    with_uri = [index for index, buffer in enumerate(buffers) if "uri" in buffer]
    if len(with_uri) > 1:
        raise ValueError("glTF export has more than one data buffer")
    if not with_uri:
        return b""
    data_index = with_uri[0]
    with open(os.path.join(directory, buffers[data_index]["uri"]), "rb") as handle:
        binary = handle.read()
    del buffers[data_index]["uri"]
    if data_index != 0:
        order = [data_index] + [index for index in range(len(buffers)) if index != data_index]
        remap = {old: new for new, old in enumerate(order)}
        gltf["buffers"] = [buffers[old] for old in order]
        for view in gltf.get("bufferViews", []):
            view["buffer"] = remap[view["buffer"]]
            meshopt = view.get("extensions", {}).get(_MESHOPT)
            if meshopt is not None:
                meshopt["buffer"] = remap[meshopt["buffer"]]
    gltf["buffers"][0]["byteLength"] = len(binary)
    return binary


def _glb(gltf: dict, binary: bytes) -> bytes:
    chunk = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    chunk += b" " * (-len(chunk) % 4)
    body = struct.pack("<I4s", len(chunk), b"JSON") + chunk
    if binary:
        padded = binary + b"\0" * (-len(binary) % 4)
        body += struct.pack("<I4s", len(padded), b"BIN\0") + padded
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def image_size(data: bytes) -> tuple:
    """(width, height) from a PNG, WebP or JPEG header; (0, 0) when unknown."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    return (0, 0)


def _webp_size(data: bytes) -> tuple:
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return (width, height)
    if kind == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return (width & 0x3FFF, height & 0x3FFF)
    if kind == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
    return (0, 0)


def _jpeg_size(data: bytes) -> tuple:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            return (0, 0)
        marker = data[index + 1]
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height)
        index += 2 + length
    return (0, 0)


def gpu_bytes(pack: Optional[Pack]) -> int:
    """Rough GPU memory of the pack's textures: RGBA8 plus a third for mipmaps."""
    if pack is None:
        return 0
    return sum(texture.width * texture.height * 4 * 4 // 3 for texture in pack.textures)
