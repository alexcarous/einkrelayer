"""Optional answer keys: move each article's answers into a linked chapter after it."""

import re
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from einkrelayer.books.xhtml import XHTML, document, xml

HEADING = re.compile(r"(?:answer key|answers|solutions)(?: and explanations)?", re.I)
LEVELS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def mark_answer_key(soup, body):
    """Mark the answer section with class "answer-key"; return whether one was found."""
    marked = body.find(class_="answer-key")
    if marked is not None:
        marked["class"] = "answer-key"
        return True
    for heading in body.find_all(["h2", "h3"]):
        if not HEADING.fullmatch(heading.get_text(" ", strip=True)):
            continue
        level = LEVELS.index(heading.name)
        nodes, node = [heading], heading.next_sibling
        while node is not None and not (
            getattr(node, "name", None) in LEVELS and LEVELS.index(node.name) <= level
        ):
            nodes.append(node)
            node = node.next_sibling
        section = soup.new_tag("div", attrs={"class": "answer-key"})
        heading.insert_before(section)
        for item in nodes:
            section.append(item.extract())
        return True
    return False


def separate_answer_keys(chapters):
    """Split marked answer sections into "-answers" chapters and keep links working."""
    roots = {name: ET.fromstring(data) for name, data in chapters.items()}
    moved, answer_files, sources, result = {}, {}, {}, {}
    for filename, root in roots.items():
        parents = {child: parent for parent in root.iter() for child in parent}
        key = next((node for node in root.iter() if node.get("class") == "answer-key"), None)
        result[filename] = root
        if key is None:
            continue
        target = filename.replace(".xhtml", "-answers.xhtml")
        for node in key.iter():
            if node.get("id"):
                moved[(filename, node.get("id"))] = target
        parents[key].remove(key)
        tags = {f"{{{XHTML}}}{level}" for level in LEVELS}
        heading = next((node for node in key.iter() if node.tag in tags), None)
        if heading is not None:
            heading.tag = f"{{{XHTML}}}h1"
        title = root.findtext(f"{{{XHTML}}}head/{{{XHTML}}}title")
        back = ET.SubElement(key, f"{{{XHTML}}}p", {"class": "chapter-links"})
        ET.SubElement(back, f"{{{XHTML}}}a", {"href": filename}).text = "Back to " + title
        wrapper = ET.Element(f"{{{XHTML}}}div")
        wrapper.append(key)
        page = document(
            "Answers — " + title,
            wrapper,
            language=root.get("lang", "en"),
            direction=root.get("dir", ""),
        )
        result[target] = ET.fromstring(page)
        answer_files[filename] = target
        sources[target] = filename
    for filename, root in result.items():
        original = sources.get(filename, filename)
        for node in root.iter(f"{{{XHTML}}}a"):
            parsed = urlsplit(node.get("href", ""))
            if parsed.scheme or parsed.netloc or not parsed.fragment:
                continue
            destination = moved.get((parsed.path or original, unquote(parsed.fragment)))
            if destination:
                prefix = destination if destination != filename else ""
                node.set("href", prefix + "#" + parsed.fragment)
            elif not parsed.path and original != filename:
                node.set("href", original + "#" + parsed.fragment)
    return {name: xml(root) for name, root in result.items()}, answer_files
