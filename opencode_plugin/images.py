"""Bounded AstrBot attachments and inline ACP image blocks."""

import base64
import binascii
import hashlib
import io
import os
import stat
from contextlib import ExitStack
from pathlib import Path

from .errors import ImageLimitError, OpenCodeError

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_IMAGE_FRAME_BYTES = 4 * ((MAX_IMAGE_TOTAL_BYTES + 2) // 3) + 4096
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def read_local_image(root, ref):
    from urllib.parse import unquote, urlsplit

    try:
        parts = urlsplit(ref)
        if parts.scheme:
            if parts.scheme != "file" or parts.netloc or parts.query or parts.fragment:
                raise OpenCodeError("image_source")
            ref = unquote(parts.path)
        relative = Path(ref).relative_to(root)
        if not relative.parts or ".." in relative.parts:
            raise OpenCodeError("image_source")
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            parent = os.open(root, flags)
            stack.callback(os.close, parent)
            for part in relative.parts[:-1]:
                parent = os.open(part, flags, dir_fd=parent)
                stack.callback(os.close, parent)
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OpenCodeError("image_source")
                if info.st_size > MAX_IMAGE_BYTES:
                    raise ImageLimitError("file_bytes", MAX_IMAGE_BYTES, info.st_size)
                return file.read(MAX_IMAGE_BYTES + 1)
    except (ValueError, OSError):
        raise OpenCodeError("image_source") from None


def image_data(root, ref):
    if not isinstance(ref, str) or not ref:
        raise OpenCodeError("image_source")
    declared = None
    if ref.startswith("base64://"):
        encoded = ref[len("base64://") :]
    elif ref.startswith("data:"):
        header, _, encoded = ref.partition(",")
        declared, _, encoding = header[len("data:") :].partition(";")
        if encoding != "base64" or declared not in IMAGE_TYPES.values():
            raise OpenCodeError("image_invalid")
    else:
        return read_local_image(root, ref), declared
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        padding = 2 if encoded.endswith("==") else int(encoded.endswith("="))
        raise ImageLimitError("file_bytes", MAX_IMAGE_BYTES, len(encoded) * 3 // 4 - padding)
    try:
        return base64.b64decode(encoded, validate=True), declared
    except (binascii.Error, ValueError):
        raise OpenCodeError("image_invalid") from None


def validate_image(data, declared):
    from PIL import Image

    if not data:
        raise OpenCodeError("image_invalid")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageLimitError("file_bytes", MAX_IMAGE_BYTES, len(data))
    try:
        with Image.open(io.BytesIO(data), formats=list(IMAGE_TYPES)) as image:
            mime_type = IMAGE_TYPES[image.format]
            if declared and mime_type != declared:
                raise OpenCodeError("image_invalid")
            pixels = image.width * image.height
            if pixels > MAX_IMAGE_PIXELS:
                raise ImageLimitError("pixels", MAX_IMAGE_PIXELS, pixels)
            if min(image.size) < 8 or getattr(image, "is_animated", False):
                raise OpenCodeError("image_invalid")
            image.verify()
        with Image.open(io.BytesIO(data), formats=list(IMAGE_TYPES)) as image:
            image.load()
    except Image.DecompressionBombError:
        raise ImageLimitError("pixels", MAX_IMAGE_PIXELS) from None
    except (OSError, ValueError, SyntaxError, EOFError):
        raise OpenCodeError("image_invalid") from None
    return mime_type


class ImageInput:
    def __init__(self, root):
        self.root = Path(root)
        self.blocks = {}
        self.count = 0
        self.size = 0

    def read(self, ref):
        if self.count >= MAX_IMAGES:
            raise ImageLimitError("count", MAX_IMAGES, self.count + 1)
        data, declared = image_data(self.root, ref)
        mime_type = validate_image(data, declared)
        if self.size + len(data) > MAX_IMAGE_TOTAL_BYTES:
            raise ImageLimitError("total_bytes", MAX_IMAGE_TOTAL_BYTES, self.size + len(data))
        encoded = base64.b64encode(data)
        digest = hashlib.sha256(encoded).hexdigest()
        self.blocks[digest] = {
            "type": "image",
            "mimeType": mime_type,
            "data": encoded.decode("ascii"),
        }
        self.count += 1
        self.size += len(data)
        return digest


def historical_image_key(ref):
    """Compare portable history with a sent block without decoding or opening a file."""
    if not isinstance(ref, str):
        return "unavailable"
    if ref.startswith("base64://"):
        ref = ref[len("base64://") :]
    elif ref.startswith("data:"):
        header, _, encoded = ref.partition(",")
        if header.endswith(";base64"):
            ref = encoded
    else:
        return "reference:" + hashlib.sha256(ref.encode()).hexdigest()
    return hashlib.sha256(ref.encode()).hexdigest()


def prompt_images(messages, images):
    """Send only this turn's attachments, with explicit markers on replayed history."""
    transcript = []
    blocks = []
    for entry in messages:
        current = entry.get("_current", False)
        entry = {name: value for name, value in entry.items() if not name.startswith("_")}
        if attachments := entry.get("images"):
            if current:
                first = len(blocks) + 1
                for digest in attachments:
                    block = images.get(digest)
                    if block is None:
                        raise OpenCodeError("image_invalid")
                    blocks.append(block)
                entry["images"] = list(range(first, len(blocks) + 1))
            else:
                entry.pop("images")
                entry["historical_images_not_attached"] = len(attachments)
        transcript.append(entry)
    return transcript, blocks
