"""An NFO document that is edited in place.

Every writer (media probing, metadata plugins, future tools) loads the existing file,
changes only the elements it owns and saves. Elements it does not touch, comments (also
those before the root element) and Kodi-style trailing URL lines are preserved.
"""

from __future__ import annotations

import codecs
import io
import re
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

from ..filesystem import atomic
from .paths import ROOT_TAGS, NfoKind

XML_DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
# The end of the root element: its close tag, or the whole root when it is empty (``<movie />``).
_ROOT_CLOSE = re.compile(
    r"</\s*(movie|tvshow|season|episodedetails)\s*>|<(movie|tvshow|season|episodedetails)(\s[^<>]*)?/>",
    re.IGNORECASE,
)
_TAG_TO_KIND = {v: k for k, v in ROOT_TAGS.items()}
# Codecs that transform text (escape sequences) instead of only decoding it.
_ESCAPING_CODECS = frozenset({"unicode-escape", "raw-unicode-escape", "utf-7", "idna", "punycode"})
# Comments, processing instructions and a DOCTYPE between the declaration and the root element.
_PROLOG = re.compile(r"\A(?:\s*(?:<!--.*?-->|<\?.*?\?>|<!DOCTYPE[^\[>]*(?:\[.*?\])?\s*>))*", re.DOTALL | re.IGNORECASE)
_DECLARED_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z0-9._:-]+)["']""")


class NfoError(Exception):
    pass


class NfoParseError(NfoError):
    """The existing file is not valid NFO XML; it is left untouched."""


def _parser() -> ET.XMLParser:
    return ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))


# Everything outside the XML 1.0 ``Char`` production: most control characters, lone
# surrogates (e.g. from a JSON "\ud800" escape), U+FFFE and U+FFFF.
_NOT_XML_CHAR = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _clean_text(value: object) -> str:
    return _NOT_XML_CHAR.sub("", str(value))


def _clean_tree(root: ET.Element) -> None:
    """Drop characters XML cannot carry from every text, tail and attribute value.

    Writers may set ``.text`` directly; one such character would make the written NFO
    unreadable, and an unreadable NFO is never written again. A parsed tree has none.
    """
    for element in root.iter():
        if element.text and _NOT_XML_CHAR.search(element.text):
            element.text = _clean_text(element.text)
        if element.tail and _NOT_XML_CHAR.search(element.tail):
            element.tail = _clean_text(element.tail)
        for name, value in element.attrib.items():
            if _NOT_XML_CHAR.search(value):
                element.attrib[name] = _clean_text(value)


class NfoDocument:
    root: ET.Element
    trailing: str  # Kodi scraper URL lines after the root element
    existed: bool  # loaded from a file (False: created in memory)
    prolog: str
    _original: bytes  # the bytes as loaded or last saved; save() skips identical output

    def __init__(self, root: ET.Element, trailing: str = "", existed: bool = False, original: bytes = b"",
                 prolog: str = "") -> None:
        self.root = root
        self.trailing = trailing
        self.prolog = prolog  # kept verbatim: the XML parser drops what precedes the root element
        self.existed = existed
        self._original = original

    # -- construction -------------------------------------------------------------------------
    @classmethod
    def new(cls, kind: NfoKind) -> "NfoDocument":
        return cls(ET.Element(kind.root_tag))

    @staticmethod
    def _decode(data: bytes) -> str:
        """The text of ``data`` in its declared encoding (UTF-8 without a declaration or with a BOM).

        Bytes that do not decode cleanly raise :class:`NfoParseError`, so the file is never
        rewritten with replacement characters in place of the user's text.

        Contract:
            Raises:
                NfoParseError: The declared encoding is unknown, not ASCII-based (the
                    declaration was read as ASCII, so it cannot be true) or an escaping codec
                    (``unicode_escape``, ``utf-7`` ...); or the bytes are not valid in the
                    encoding used (UTF-8 when nothing is declared or a UTF-8 BOM is present).
        """
        encoding = "utf-8-sig"
        match = None if data.startswith(codecs.BOM_UTF8) else _DECLARED_ENCODING.match(data)
        if match is not None:
            encoding = match.group(1).decode("ascii")
            try:
                usable = (codecs.lookup(encoding).name not in _ESCAPING_CODECS
                          and "<?xml".encode(encoding) == b"<?xml")
            except (LookupError, UnicodeError):
                usable = False
            if not usable:
                # Unknown, not ASCII-based (the declaration itself was read as ASCII) or an
                # escaping codec that would rewrite the text rather than just decode it.
                raise NfoParseError(f"NFO declares an unsupported encoding: {encoding}")
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise NfoParseError(f"NFO is not valid {encoding} text: {exc}") from None

    @classmethod
    def parse(cls, data: bytes, expected: Optional[NfoKind] = None) -> "NfoDocument":
        """Parse NFO bytes into an editable document that remembers ``data`` as its original.

        Text after the last root close tag (Kodi scraper URL lines) becomes ``trailing``, and
        comments / processing instructions / a DOCTYPE before the root become ``prolog``.

        Contract:
            Preconditions:
                - The root close tag is found by a regex over the four known root tags; text
                  after a later ``</season>`` (e.g. inside a trailing comment) would be cut
                  at the wrong place and then fail to parse (NfoParseError, never a bad write).
            Raises:
                NfoParseError: The bytes do not decode (see :meth:`_decode`); the text holds no
                    ``<`` and ``expected`` is None; the XML is malformed; or the root tag is
                    not ``expected``'s. Text without any ``<`` and an ``expected`` kind is not
                    an error: it becomes an empty document of that kind with the text as
                    ``trailing`` (Kodi URL-only NFO).
        """
        text = cls._decode(data)
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
        prolog_match = _PROLOG.match(xml_text)  # always matches (possibly empty)
        prolog = prolog_match.group(0).strip() if prolog_match else ""
        try:
            root = ET.parse(io.StringIO(xml_text), parser=_parser()).getroot()
        except ET.ParseError as exc:
            raise NfoParseError(f"Invalid NFO XML: {exc}") from None
        if expected is not None and root.tag != expected.root_tag:
            raise NfoParseError(f"NFO root is <{root.tag}>, expected <{expected.root_tag}>")
        return cls(root, trailing=trailing, existed=True, original=data, prolog=prolog)

    @classmethod
    def load(cls, path: str, expected: Optional[NfoKind] = None) -> "NfoDocument":
        """Read and :meth:`parse` the file at ``path`` (read whole, no size limit).

        Contract:
            Raises:
                NfoError: The file cannot be read (any ``OSError``: missing, a folder,
                    permission denied, locked); the original error is only in the message.
                NfoParseError: As :meth:`parse`.
        """
        try:
            data = atomic.read_bytes(path)
        except OSError as exc:
            raise NfoError(f"Cannot read NFO: {exc}") from None
        return cls.parse(data, expected)

    @classmethod
    def load_or_create(cls, path: str, kind: NfoKind) -> "NfoDocument":
        """:meth:`load` the file when it exists, else a new in-memory document of ``kind``.

        Contract:
            Raises:
                NfoError / NfoParseError: As :meth:`load`, also when the file disappears
                    between the existence check and the read. A ``path`` that is a folder
                    counts as missing, so the later :meth:`save` fails with ``OSError``.
        """
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
        """Set ``<uniqueid type="...">`` for one ID type, keeping other ID types.

        A replaced ID that was the default one stays the default; with ``default=True`` this
        ID becomes the only default.

        Contract:
            Preconditions:
                - ``id_type`` is compared case-insensitively with existing types; the new
                  element is written with ``id_type`` as given, so the caller chooses the case.
            Silences:
                - ``None`` or a blank ``value`` leaves the document unchanged, without error.
        """
        if value is None or str(value).strip() == "":
            return
        existing = [e for e in self.root.findall("uniqueid") if (e.get("type") or "").lower() == id_type.lower()]
        element = ET.Element("uniqueid", {"type": id_type})
        if not default and any((e.get("default") or "").lower() == "true" for e in existing):
            element.set("default", "true")
        elif default:
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
        """The document as UTF-8 NFO bytes: declaration, prolog, indented root, trailing lines.

        Contract:
            Silences:
                - ``ET.indent`` re-indents whitespace-only text and tails of ``root`` in place,
                  so whitespace-only element content is not kept byte for byte.
                - Characters XML 1.0 cannot carry are dropped from texts and attribute values
                  of ``root`` in place (see :func:`_clean_tree`).
        """
        _clean_tree(self.root)
        tree = ET.ElementTree(self.root)
        ET.indent(tree, space="  ")
        buffer = io.BytesIO()
        tree.write(buffer, encoding="utf-8", xml_declaration=False, short_empty_elements=True)
        data = XML_DECLARATION
        if self.prolog:
            data += self.prolog.encode("utf-8") + b"\n"
        data += buffer.getvalue() + b"\n"
        if self.trailing:
            data += self.trailing.encode("utf-8") + b"\n"
        return data

    def save(self, path: str) -> bool:
        """Atomically write the document. Returns False (no write) when nothing changed.

        This bypasses NFO ownership: callers outside :func:`..service.update_nfo` leave the
        stored hash stale, which turns an application-owned NFO into a user file for good.

        Contract:
            Raises:
                NfoError: The document is new (not loaded) but a file now exists at ``path``;
                    it is never overwritten blindly.
                OSError: From :func:`atomic.atomic_write_bytes` (the old file is then
                    unchanged).
        """
        data = self.to_bytes()
        if self.existed and data == self._original:
            return False
        if not self.existed and atomic.is_file(path):
            # Someone created the file since we looked; never clobber it blindly.
            raise NfoError("NFO appeared while it was being created; retry")
        atomic.atomic_write_bytes(path, data)
        self.existed, self._original = True, data
        return True
