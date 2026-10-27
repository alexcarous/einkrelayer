"""Safe XHTML serialization for book chapters."""

import re
from xml.etree import ElementTree as ET

from bs4 import Comment, NavigableString

XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
ET.register_namespace("", XHTML)
ET.register_namespace("epub", EPUB)


def xml(element):
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def document(title, content, body_class=None, *, language="en", direction=""):
    attrs = {"lang": language}
    if direction:
        attrs["dir"] = direction
    root = ET.Element(f"{{{XHTML}}}html", attrs)
    head = ET.SubElement(root, f"{{{XHTML}}}head")
    ET.SubElement(head, f"{{{XHTML}}}title").text = title
    ET.SubElement(
        head, f"{{{XHTML}}}link", {"rel": "stylesheet", "href": "reader.css", "type": "text/css"}
    )
    body = ET.SubElement(root, f"{{{XHTML}}}body")
    body.text = content.text
    if body_class:
        body.set("class", body_class)
    for child in list(content):
        body.append(child)
    return xml(root)


def to_xml(node):
    """Serialize sanitized HTML as well-formed XHTML, preserving text and tails."""
    name = {
        "main": "section",
        "details": "div",
        "summary": "div",
        "picture": "div",
        "form": "div",
        "fieldset": "div",
        "legend": "p",
        "label": "span",
    }.get(node.name, node.name)
    allowed = {
        "id",
        "title",
        "lang",
        "dir",
        "href",
        "src",
        "alt",
        "width",
        "height",
        "colspan",
        "rowspan",
        "scope",
        "start",
        "type",
        "reversed",
        "class",
        "hidden",
        "datetime",
        "headers",
        "value",
    }
    attrs = {
        k: " ".join(v) if isinstance(v, list) else str(v)
        for k, v in node.attrs.items()
        if k in allowed
    }
    if name != "ol":
        attrs.pop("type", None)
    element = ET.Element(f"{{{XHTML}}}{name}", attrs)
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(child))
            if len(element):
                element[-1].tail = (element[-1].tail or "") + text
            else:
                element.text = (element.text or "") + text
        else:
            element.append(to_xml(child))
    return element
