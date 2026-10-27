"""EPUB structural checks shared by building and SMTP delivery."""

import posixpath
import shutil
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

TOOLCHAINS = Path(".cache/toolchains")


def epubcheck():
    """The command that runs EPUBCheck, or None. An installed `epubcheck` comes first;
    otherwise a copy unpacked under .cache/toolchains, run by Java from there or the system."""
    installed = shutil.which("epubcheck")
    if installed:
        return [installed]
    jars = sorted(TOOLCHAINS.glob("epubcheck-*/epubcheck.jar"))
    local = TOOLCHAINS / "java/bin/java"
    java = str(local.resolve()) if local.is_file() else shutil.which("java")
    return [java, "-jar", str(jars[-1].resolve())] if jars and java else None


def validate(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 10_000 or sum(entry.file_size for entry in entries) > 256_000_000:
            raise ValueError("EPUB exceeds validation resource limits")
        if any(entry.flag_bits & 1 for entry in entries):
            raise ValueError("Encrypted EPUB resources are unsupported")
        if archive.testzip() is not None:
            raise ValueError("EPUB ZIP integrity check failed")
        names = set(archive.namelist())
        if len(names) != len(archive.namelist()):
            raise ValueError("EPUB contains duplicate ZIP entries")
        if (
            not archive.infolist()
            or archive.infolist()[0].filename != "mimetype"
            or archive.infolist()[0].compress_type != zipfile.ZIP_STORED
        ):
            raise ValueError("EPUB must start with an uncompressed mimetype entry")
        if archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("Invalid EPUB mimetype")
        if "META-INF/container.xml" not in names:
            raise ValueError("EPUB is missing its container")
        docs = {
            name: ET.fromstring(archive.read(name))
            for name in names
            if name.endswith((".xhtml", ".xml", ".opf", ".ncx"))
        }
        ns = {
            "c": "urn:oasis:names:tc:opendocument:xmlns:container",
            "o": "http://www.idpf.org/2007/opf",
        }
        rootfile = docs["META-INF/container.xml"].find(".//c:rootfile", ns)
        if rootfile is None or rootfile.get("full-path") not in docs:
            raise ValueError("EPUB package is missing")
        package_name = rootfile.get("full-path")
        package = docs[package_name]
        manifest = package.findall("o:manifest/o:item", ns)
        items = {item.get("id"): item for item in manifest}
        if None in items or "" in items or len(items) != len(manifest):
            raise ValueError("EPUB manifest has missing or duplicate IDs")
        references = package.findall("o:spine/o:itemref", ns)
        spine_ids = [ref.get("idref") for ref in references]
        if (
            not references
            or len(spine_ids) != len(set(spine_ids))
            or any(ref not in items for ref in spine_ids)
        ):
            raise ValueError("EPUB reading order is missing or invalid")
        for item in items.values():
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(package_name), unquote(item.get("href", "")))
            )
            if target not in names:
                raise ValueError("EPUB manifest references a missing resource")
            if item.get("media-type") == "application/xhtml+xml" and target not in docs:
                docs[target] = ET.fromstring(archive.read(target))
        ids = {
            name: {el.get("id") for el in doc.iter() if el.get("id")} for name, doc in docs.items()
        }
        for name, doc in docs.items():
            if doc.tag.rsplit("}", 1)[-1] not in {"html", "ncx"}:
                continue
            for element in doc.iter():
                for attribute in ("href", "src"):
                    value = element.get(attribute)
                    if not value:
                        continue
                    parsed = urlsplit(value)
                    if parsed.scheme or parsed.netloc:
                        if attribute == "src":
                            raise ValueError(f"Online image dependency in {name}")
                        continue
                    target = (
                        posixpath.normpath(
                            posixpath.join(posixpath.dirname(name), unquote(parsed.path))
                        )
                        if parsed.path
                        else name
                    )
                    if target not in names:
                        raise ValueError(f"Missing target: {name} -> {value}")
                    if (
                        parsed.fragment
                        and target in ids
                        and unquote(parsed.fragment) not in ids[target]
                    ):
                        raise ValueError(f"Missing anchor: {name} -> {value}")
