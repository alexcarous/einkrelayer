"""Records shared by capture, composition, and delivery."""

from dataclasses import dataclass, field


@dataclass
class Document:
    source: str
    title: str
    html: str
    author: str = ""
    published: str = ""
    captured: str = ""
    assets: dict[str, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    language: str = "en"
    direction: str = ""
    input_sha256: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class BookJob:
    title: str
    documents: list[Document]
    mode: str = "book"
    edition_date: str = ""
    edition_number: int = 1
    identifier: str = ""
    answer_keys: bool = False
