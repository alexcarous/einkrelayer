"""Shared static image source selection."""

import re

IMAGE_URL = re.compile(
    r"^(?:https?:)?/?/?[^\s,]+\.(?:jpe?g|png|gif|webp|avif|svg)(?:[?#]\S*)?$", re.I
)


def image_source(image):
    """Choose a real source from lazy images or picture/srcset alternatives."""
    choices = []
    nodes = [image]
    if image.parent and image.parent.name == "picture":
        nodes += image.parent.find_all("source")
    for node in nodes:
        if node.get("type") not in (None, "image/webp", "image/png", "image/jpeg", "image/avif"):
            continue
        srcset = node.get("data-srcset") or node.get("srcset", "")
        if srcset.lstrip().startswith("data:"):
            continue
        for candidate in srcset.split(","):
            fields = candidate.strip().split()
            if not fields:
                continue
            try:
                size = float(re.sub(r"[wx]$", "", fields[-1])) if len(fields) > 1 else 1
            except ValueError:
                continue
            if size > 0:
                choices.append((size, fields[0]))
    return (
        max(choices)[1]
        if choices
        else image.get("data-src")
        or image.get("data-original")
        or image.get("src")
        or lazy_source(image)
    )


def lazy_source(image):
    """An image URL kept in some other data attribute by a lazy-loading script."""
    for key, value in image.attrs.items():
        if key.startswith("data-") and isinstance(value, str) and IMAGE_URL.match(value.strip()):
            return value.strip()
    return None
