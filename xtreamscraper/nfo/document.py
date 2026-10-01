"""An NFO document that is edited in place.

Every writer (media probing, metadata plugins, future tools) loads the existing file,
changes only the elements it owns and saves. Elements it does not touch, comments and
Kodi-style trailing URL lines are preserved.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

from ..filesystem import atomic
from .paths import ROOT_TAGS, NfoKind

XML_DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_ROOT_CLOSE = re.compile(r"</\s*(movie|tvshow|season|episodedetails)\s*>", re.IGNORECASE)
_TAG_TO_KIND = {v: k for k, v in ROOT_TAGS.items()}


class NfoError(Exception):
    pass


class NfoParseError(NfoError):
    """The existing file is not valid NFO XML; it is left untouched."""


def _parser() -> ET.XMLParser:
    return ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))


def _clean_text(value: object) -> str:
    # XML 1.0 cannot carry most control characters.
    return "".join(ch for ch in str(value) if ch in "\t\n\r" or ord(ch) >= 0x20)


class NfoDocument:
    def __init__(self, root: ET.Element, trailing: str = "", existed: bool = False, original: bytes = b"") -> None:
        self.root = root
        self.trailing = trailing
        self.existed = existed
        self._original = original

    # -- construction -------------------------------------------------------------------------
    @classmethod
    def new(cls, kind: NfoKind) -> "NfoDocument":
        return cls(ET.Element(kind.root_tag))

    @classmethod
    def parse(cls, data: bytes, expected: Optional[NfoKind] = None) -> "NfoDocument":
        text = data.decode("utf-8-sig", errors="replace")
        if "<" not in text:
            # URL-only NFO (Kodi scraper hint): keep the text, build XML around it.
            if expected is None:
                raise NfoParseError("NFO contains no XML")
            doc = cls.new(expected)
            doc.trailing = text.strip()
            doc.existed, doc._original = True, data
            return doc
        trailing = ""
        closes = list(_ROOT_CLOSE.finditer(text))
        xml_text = text
        if closes:
            end = closes[-1].end()
            xml_text, trailing = text[:end], text[end:].strip()
        # Parsing a str (not bytes) needs the encoding declaration removed.
        xml_text = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text, count=1)
        try:
            root = ET.parse(io.StringIO(xml_text), parser=_parser()).getroot()
        except ET.ParseError as exc:
            raise NfoParseError(f"Invalid NFO XML: {exc}") from None
        if expected is not None and root.tag != expected.root_tag:
            raise NfoParseError(f"NFO root is <{root.tag}>, expected <{expected.root_tag}>")
        return cls(root, trailing=trailing, existed=True, original=data)

    @classmethod
    def load(cls, path: str, expected: Optional[NfoKind] = None) -> "NfoDocument":
        try:
            data = atomic.read_bytes(path)
        except OSError as exc:
            raise NfoError(f"Cannot read NFO: {exc}") from None
        return cls.parse(data, expected)

    @classmethod
    def load_or_create(cls, path: str, kind: NfoKind) -> "NfoDocument":
        if atomic.is_file(path):
            return cls.load(path, kind)
        return cls.new(kind)

    @property
    def kind(self) -> Optional[NfoKind]:
        return _TAG_TO_KIND.get(self.root.tag)

    # -- reading ------------------------------------------------------------------------------
    def find(self, path: str) -> Optional[ET.Element]:
        return self.root.find(path)

    def get_text(self, tag: str) -> Optional[str]:
        element = self.root.find(tag)
        return element.text if element is not None else None

    def get_list(self, tag: str) -> list[str]:
        return [e.text or "" for e in self.root.findall(tag)]

    # -- editing (callers only touch the tags they own) -----------------------------------------
    def _index_of_first(self, parent: ET.Element, tag: str) -> Optional[int]:
        for index, child in enumerate(list(parent)):
            if child.tag == tag:
                return index
        return None

    def replace_elements(self, tag: str, new_elements: Iterable[ET.Element], parent: Optional[ET.Element] = None) -> None:
        """Replace every direct ``tag`` child with ``new_elements`` at the first one's position."""
        parent = self.root if parent is None else parent
        position = self._index_of_first(parent, tag)
        for child in [c for c in parent if c.tag == tag]:
            parent.remove(child)
        elements = list(new_elements)
        if position is None:
            parent.extend(elements)
        else:
            for offset, element in enumerate(elements):
                parent.insert(position + offset, element)

    def set_text(self, tag: str, value: object, parent: Optional[ET.Element] = None) -> None:
        """Set a single-valued field. ``None`` leaves the field untouched."""
        if value is None:
            return
        element = ET.Element(tag)
        element.text = _clean_text(value)
        self.replace_elements(tag, [element], parent)

    def set_list(self, tag: str, values: Optional[Iterable[object]], parent: Optional[ET.Element] = None) -> None:
        """Replace a multi-valued field (genre, studio...). ``None`` leaves it untouched."""
        if values is None:
            return
        elements = []
        for value in values:
            if value is None or str(value).strip() == "":
                continue
            element = ET.Element(tag)
            element.text = _clean_text(value)
            elements.append(element)
        self.replace_elements(tag, elements, parent)

    def remove(self, tag: str, parent: Optional[ET.Element] = None) -> None:
        self.replace_elements(tag, [], parent)

    def set_uniqueid(self, id_type: str, value: object, default: bool = False) -> None:
        """Set ``<uniqueid type="...">`` for one ID type, keeping other ID types."""
        if value is None or str(value).strip() == "":
            return
        existing = [e for e in self.root.findall("uniqueid") if (e.get("type") or "").lower() == id_type.lower()]
        element = ET.Element("uniqueid", {"type": id_type})
        if default:
            for other in self.root.findall("uniqueid"):
                other.attrib.pop("default", None)
            element.set("default", "true")
        element.text = _clean_text(value)
        if existing:
            index = list(self.root).index(existing[0])
            for old in existing:
                self.root.remove(old)
            self.root.insert(index, element)
        else:
            position = self._index_of_first(self.root, "uniqueid")
            if position is None:
                self.root.append(element)
            else:
                self.root.insert(position, element)

    def find_or_create(self, path: str) -> ET.Element:
        node = self.root
        for part in path.split("/"):
            child = node.find(part)
            if child is None:
                child = ET.SubElement(node, part)
            node = child
        return node

    # -- output -------------------------------------------------------------------------------
    def to_bytes(self) -> bytes:
        tree = ET.ElementTree(self.root)
        ET.indent(tree, space="  ")
        buffer = io.BytesIO()
        tree.write(buffer, encoding="utf-8", xml_declaration=False, short_empty_elements=True)
        data = XML_DECLARATION + buffer.getvalue() + b"\n"
        if self.trailing:
            data += self.trailing.encode("utf-8") + b"\n"
        return data

    def save(self, path: str) -> bool:
        """Atomically write the document. Returns False (no write) when nothing changed."""
        data = self.to_bytes()
        if self.existed and data == self._original:
            return False
        if not self.existed and atomic.is_file(path):
            # Someone created the file since we looked; never clobber it blindly.
            raise NfoError("NFO appeared while it was being created; retry")
        atomic.atomic_write_bytes(path, data)
        self.existed, self._original = True, data
        return True
