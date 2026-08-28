"""Extract and validate the ETCSL composition catalogue."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass
import hashlib
from html import escape as escape_html
from html.entities import html5 as HTML5_ENTITIES
from html.parser import HTMLParser
import json
from pathlib import Path
from pathlib import PurePosixPath
import posixpath
import re
import stat
from typing import Iterable, Sequence
from urllib.parse import parse_qs, urljoin, urlsplit
import xml.etree.ElementTree as ElementTree
import zipfile


CATALOGUE_URL = "https://etcsl.orinst.ox.ac.uk/cgi-bin/etcsl.cgi?text=all"
TEXT_ID_RE = re.compile(r"^(?P<kind>[ct])\.(?P<id>[0-9]+(?:\.(?:[0-9]+|[a-z])){2,4})$")
VISIBLE_ID_RE = re.compile(r"^(?P<id>[0-9]+(?:\.(?:[0-9]+|[a-z])){2,4})\s+")
XML_ENTRY_RE = re.compile(
    r"^etcsl/(?P<directory>transliterations|translations)/"
    r"(?P<kind>[ct])\.(?P<id>[0-9]+(?:\.(?:[0-9]+|[a-z])){2,4})\.xml$"
)
ENTITY_REF_RE = re.compile(r"&([A-Za-z][A-Za-z0-9._:-]*);")
ENTITY_DECL_RE = re.compile(r"<!ENTITY\s+(?!%\s)([A-Za-z][A-Za-z0-9._:-]*)\s")
SYSTEM_REF_RE = re.compile(r"\bSYSTEM\s+['\"]([^'\"]+)['\"]")
PUBLIC_REF_RE = re.compile(r"\bPUBLIC\s+['\"][^'\"]*['\"]\s+['\"]([^'\"]+)['\"]")
BUILTIN_ENTITIES = frozenset({"amp", "apos", "gt", "lt", "quot"})
# ETCSL entities must take precedence over same-named HTML entities such as mu.
ETCSL_CHARACTER_ENTITIES = {
    "aleph": "’",
    "C": "Š",
    "c": "š",
    "G": "Ĝ",
    "g": "ĝ",
    "H": "Ḫ",
    "h": "ḫ",
    "S": "Ṣ",
    "s": "ṣ",
    "T": "Ṭ",
    "t": "ṭ",
    "X": "…",
    "damb": "[damb]",
    "dame": "[dame]",
    "suppb": "[suppb]",
    "suppe": "[suppe]",
    "qryb": "[qryb]",
    "qrye": "[qre]",
    "subb": "[subb]",
    "sube": "[sube]",
    "s0": "₀",
    "s1": "₁",
    "s2": "₂",
    "s3": "₃",
    "s4": "₄",
    "s5": "₅",
    "s6": "₆",
    "s7": "₇",
    "s8": "₈",
    "s9": "₉",
    "times": "×",
    "plus": "+",
    "commat": "@",
    "sect": "§",
}
ETCSL_DETERMINATIVE_ENTITIES = {
    "ance": "anše",
    "cah2": "šah₂",
    "d": "d",
    "dug": "dug",
    "e2": "e₂",
    "f": "f",
    "gi": "gi",
    "gud": "gud",
    "id2": "id₂",
    "iku": "iku",
    "im": "im",
    "itid": "itid",
    "jic": "ĝiš",
    "kac": "kaš",
    "ki": "ki",
    "ku6": "ku₆",
    "kuc": "kuš",
    "kur": "kur",
    "lu2": "lu₂",
    "m": "m",
    "mu": "mu",
    "mucen": "mušen",
    "mul": "mul",
    "na4": "na₄",
    "ninda": "ninda",
    "sa": "sa",
    "sar": "sar",
    "tug2": "tug₂",
    "tum9": "tum₉",
    "u2": "u₂",
    "udu": "udu",
    "urud": "urud",
    "uzu": "uzu",
    "zabar": "zabar",
}
ETCSL_HORIZONTAL_RULING = "[[TP-ETCSL-HR]]"
ETCSL_DETERMINATIVE_TOKEN = "[[TP-ETCSL-DET:{name}]]"
ETCSL_READER_TOKEN_RE = re.compile(
    r"\[\[TP-ETCSL-(?:HR|DET:(?P<determinative>[a-z0-9]+))\]\]"
)
MAX_ARCHIVE_SIZE = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ENTRY_SIZE = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
TITLE_SUFFIXES = (
    " -- a composite transliteration",
    " -- an English prose translation",
)
KNOWN_UNTRANSLATED = frozenset(
    {
        "0.1.1",
        "0.1.2",
        "0.2.01",
        "0.2.02",
        "0.2.03",
        "0.2.04",
        "0.2.05",
        "0.2.06",
        "0.2.07",
        "0.2.08",
        "0.2.11",
        "0.2.12",
        "0.2.13",
    }
)


class InventoryError(ValueError):
    """Raised when the catalogue structure is ambiguous or inconsistent."""


@dataclass(frozen=True)
class Composition:
    id: str
    title: str
    transliteration_url: str
    translation_url: str | None


class _CatalogueParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.compositions: list[Composition] = []
        self._in_item = False
        self._title_parts: list[str] = []
        self._seen_inventory_link = False
        self._links: dict[str, tuple[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            if self._in_item:
                raise InventoryError("nested catalogue list items are not supported")
            self._in_item = True
            self._title_parts = []
            self._seen_inventory_link = False
            self._links = {}
            return
        if tag != "a" or not self._in_item:
            return
        href = dict(attrs).get("href")
        if href is None:
            return
        query = parse_qs(urlsplit(urljoin(self.base_url, href)).query)
        values = query.get("text", [])
        if len(values) != 1:
            return
        match = TEXT_ID_RE.fullmatch(values[0])
        if match is None:
            return
        kind = match.group("kind")
        if kind in self._links:
            raise InventoryError(f"duplicate {kind!r} link in one catalogue item")
        self._links[kind] = (match.group("id"), urljoin(self.base_url, href))
        self._seen_inventory_link = True

    def handle_data(self, data: str) -> None:
        if self._in_item and not self._seen_inventory_link:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "li" or not self._in_item:
            return
        self._in_item = False
        if "c" not in self._links and "t" not in self._links:
            return
        if "c" not in self._links:
            raise InventoryError("translation link has no transliteration in its catalogue item")
        composition_id, transliteration_url = self._links["c"]
        if "t" in self._links and self._links["t"][0] != composition_id:
            raise InventoryError(
                f"catalogue item links different composition IDs: {composition_id!r} and {self._links['t'][0]!r}"
            )
        visible = " ".join("".join(self._title_parts).split())
        match = VISIBLE_ID_RE.match(visible)
        if match is None or match.group("id") != composition_id:
            raise InventoryError(
                f"visible composition ID does not match transliteration {composition_id!r}"
            )
        title = visible[match.end() :].strip().removesuffix(":").strip()
        if not title:
            raise InventoryError(f"composition {composition_id!r} has no title")
        translation_url = self._links.get("t", ("", None))[1]
        self.compositions.append(
            Composition(composition_id, title, transliteration_url, translation_url)
        )


def extract_inventory(document: str, *, base_url: str = CATALOGUE_URL) -> tuple[Composition, ...]:
    """Extract unique composition records from an ETCSL catalogue document."""
    parser = _CatalogueParser(base_url)
    parser.feed(document)
    parser.close()
    ids = [composition.id for composition in parser.compositions]
    duplicates = sorted(identifier for identifier, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise InventoryError(f"duplicate composition ID: {duplicates[0]}")
    if not parser.compositions:
        raise InventoryError("no ETCSL composition records found")
    return tuple(parser.compositions)


def build_report(
    compositions: Sequence[Composition],
    *,
    expected_work_count: int = 394,
    known_untranslated: Iterable[str] = KNOWN_UNTRANSLATED,
    expected_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    """Build an explainable completeness report for extracted records."""
    known = frozenset(known_untranslated)
    actual_ids = {composition.id for composition in compositions}
    translated_ids = {
        composition.id for composition in compositions if composition.translation_url is not None
    }
    errors: list[str] = []
    if len(compositions) != expected_work_count:
        errors.append(
            f"expected {expected_work_count} compositions, found {len(compositions)}"
        )
    missing_translations = sorted(actual_ids - known - translated_ids)
    unexpected_translations = sorted(actual_ids & known & translated_ids)
    if missing_translations:
        errors.append(f"missing translation for {missing_translations[0]}")
    if unexpected_translations:
        errors.append(f"unexpected translation for {unexpected_translations[0]}")
    missing_ids: list[str] = []
    unexpected_ids: list[str] = []
    if expected_ids is not None:
        expected = set(expected_ids)
        missing_ids = sorted(expected - actual_ids)
        unexpected_ids = sorted(actual_ids - expected)
        if missing_ids:
            errors.append(f"missing composition {missing_ids[0]}")
        if unexpected_ids:
            errors.append(f"unexpected composition {unexpected_ids[0]}")
    return {
        "status": "complete" if not errors else "incomplete",
        "expected_work_count": expected_work_count,
        "work_count": len(compositions),
        "transliteration_count": len(compositions),
        "translation_count": len(translated_ids),
        "known_untranslated_count": len(actual_ids & known),
        "missing_translations": missing_translations,
        "unexpected_translations": unexpected_translations,
        "missing_ids": missing_ids,
        "unexpected_ids": unexpected_ids,
        "errors": errors,
        "compositions": [asdict(composition) for composition in compositions],
    }


def analyze_capture(
    capture_directory: Path,
    *,
    expected_work_count: int,
    required_representation_kinds: Sequence[str],
    required_source_ids: Sequence[str],
) -> dict[str, object]:
    """Analyze captured catalogue and deposit content without changing the archive."""
    capture = _read_json(capture_directory / "capture.json")
    source_results = {
        item.get("source_id"): item
        for item in capture.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("source_id"), str)
    }
    errors = [
        f"required source {source_id!r} is not complete"
        for source_id in required_source_ids
        if source_results.get(source_id, {}).get("status")
        not in {"complete", "complete_with_warnings"}
    ]
    warnings: list[str] = []

    catalogue_path = _find_catalogue(capture_directory / "sources/historical-web/mirror")
    catalogue_report: dict[str, object] | None = None
    catalogue_ids: set[str] = set()
    if catalogue_path is None:
        errors.append("captured ETCSL catalogue was not found")
    else:
        try:
            catalogue = extract_inventory(catalogue_path.read_text(encoding="utf-8"))
            catalogue_report = build_report(
                catalogue,
                expected_work_count=expected_work_count,
            )
            catalogue_ids = {item.id for item in catalogue}
            errors.extend(str(value) for value in catalogue_report["errors"])
        except (OSError, UnicodeError, InventoryError) as exc:
            errors.append(f"cannot parse captured ETCSL catalogue: {exc}")

    archive_path = _find_zip(capture_directory / "sources/ota-dataset/mirror")
    archive_report: dict[str, object] | None = None
    transliteration_ids: set[str] = set()
    translation_ids: set[str] = set()
    if archive_path is None:
        errors.append("captured ETCSL deposit ZIP was not found")
    else:
        try:
            archive_report = _analyze_zip(archive_path, expected_work_count)
            transliteration_ids = set(archive_report["transliteration_ids"])
            translation_ids = set(archive_report["translation_ids"])
            errors.extend(str(value) for value in archive_report["errors"])
            warnings.extend(str(value) for value in archive_report["warnings"])
        except (OSError, UnicodeError, zipfile.BadZipFile, InventoryError) as exc:
            errors.append(f"cannot analyze captured ETCSL deposit: {exc}")

    inventories_available = catalogue_report is not None and archive_report is not None
    missing_deposit_transliterations = (
        sorted(catalogue_ids - transliteration_ids) if inventories_available else []
    )
    unexpected_deposit_transliterations = (
        sorted(transliteration_ids - catalogue_ids) if inventories_available else []
    )
    expected_translation_ids = catalogue_ids - KNOWN_UNTRANSLATED
    missing_deposit_translations = (
        sorted(expected_translation_ids - translation_ids) if inventories_available else []
    )
    unexpected_deposit_translations = (
        sorted(translation_ids - expected_translation_ids) if inventories_available else []
    )
    for label, values in (
        ("deposit transliteration", missing_deposit_transliterations),
        ("catalogue transliteration", unexpected_deposit_transliterations),
        ("deposit translation", missing_deposit_translations),
        ("catalogue translation", unexpected_deposit_translations),
    ):
        if values:
            errors.append(f"missing {label} for {values[0]}")

    for kind in required_representation_kinds:
        if kind == "transliteration" and len(transliteration_ids) != expected_work_count:
            errors.append(
                f"required transliteration count is {len(transliteration_ids)}, expected {expected_work_count}"
            )

    status = "incomplete" if errors else ("complete_with_warnings" if warnings else "complete")
    return {
        "schema_version": 1,
        "capture_id": capture.get("capture_id"),
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "required_sources": list(required_source_ids),
        "source_statuses": {
            source_id: source_results.get(source_id, {}).get("status", "missing")
            for source_id in required_source_ids
        },
        "catalogue_path": (
            str(catalogue_path.relative_to(capture_directory)) if catalogue_path else None
        ),
        "catalogue": catalogue_report,
        "deposit": archive_report,
        "mapping": {
            "missing_deposit_transliterations": missing_deposit_transliterations,
            "unexpected_deposit_transliterations": unexpected_deposit_transliterations,
            "missing_deposit_translations": missing_deposit_translations,
            "unexpected_deposit_translations": unexpected_deposit_translations,
        },
    }


def render_static_reader(
    capture_directory: Path,
    *,
    expected_work_count: int,
) -> dict[str, object]:
    """Render an inert local catalogue and composition pages from the ETCSL deposit."""
    archive_path = _find_zip(capture_directory / "sources/ota-dataset/mirror")
    if archive_path is None:
        raise InventoryError("captured ETCSL deposit ZIP was not found")
    archive_report = _analyze_zip(archive_path, expected_work_count)
    records: dict[str, dict[str, object]] = {}
    unresolved_entities: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            match = XML_ENTRY_RE.fullmatch(info.filename)
            if match is None:
                continue
            kind = match.group("kind")
            expected_directory = "transliterations" if kind == "c" else "translations"
            if match.group("directory") != expected_directory:
                raise InventoryError(
                    f"deposit XML kind does not match directory: {info.filename}"
                )
            root, unresolved = _parse_reader_xml(_read_zip_text(archive, info), info.filename)
            unresolved_entities.update(unresolved)
            identifier = match.group("id")
            record = records.setdefault(identifier, {})
            key = "transliteration" if kind == "c" else "translation"
            if key in record:
                raise InventoryError(f"duplicate {key} for composition {identifier}")
            record[key] = root
            record[f"{key}_path"] = info.filename

    identifiers = sorted(records, key=_identifier_sort_key)
    if not identifiers:
        raise InventoryError("ETCSL deposit contains no text XML files")
    files: dict[str, str] = {}
    titles: dict[str, str] = {}
    for identifier in identifiers:
        record = records[identifier]
        preferred = record.get("translation")
        if not isinstance(preferred, ElementTree.Element):
            preferred = record.get("transliteration")
        if not isinstance(preferred, ElementTree.Element):
            raise InventoryError(f"composition {identifier} has no readable representation")
        titles[identifier] = _reader_title(preferred, identifier)
    for position, identifier in enumerate(identifiers):
        previous_id = identifiers[position - 1] if position else None
        next_id = identifiers[position + 1] if position + 1 < len(identifiers) else None
        files[f"works/{identifier}.html"] = _render_work_page(
            identifier,
            titles[identifier],
            records[identifier],
            capture_directory.name,
            archive_report["sha256"],
            previous_id,
            next_id,
        )
    files["index.html"] = _render_reader_index(
        identifiers,
        titles,
        records,
        capture_directory.name,
        archive_report["sha256"],
    )

    warnings = [str(value) for value in archive_report["errors"]]
    if unresolved_entities:
        warnings.append(
            f"{len(unresolved_entities)} named entities are displayed as source tokens"
        )
    warnings.append(
        "first reader version omits bibliography linking and advanced stand-off annotations"
    )
    reader_transliteration_ids = set(archive_report["transliteration_ids"])
    reader_translation_ids = set(archive_report["translation_ids"])
    inventory_complete = (
        len(reader_transliteration_ids) == expected_work_count
        and reader_translation_ids == reader_transliteration_ids - KNOWN_UNTRANSLATED
        and not archive_report["missing_transliteration_counterparts"]
        and archive_report["crc_ok"]
        and archive_report["entity_stubbed_xml_parse_count"]
        == archive_report["xml_file_count"]
        and archive_report["filename_id_match_count"] == archive_report["xml_file_count"]
    )
    return {
        "status": (
            "incomplete"
            if not inventory_complete
            else ("complete_with_warnings" if warnings else "complete")
        ),
        "files": files,
        "summary": {
            "work_count": len(records),
            "transliteration_count": sum(
                "transliteration" in record for record in records.values()
            ),
            "translation_count": sum("translation" in record for record in records.values()),
            "unresolved_entity_names": sorted(unresolved_entities),
            "archive_sha256": archive_report["sha256"],
        },
        "warnings": warnings,
    }


def _parse_reader_xml(
    document: str,
    source_name: str,
) -> tuple[ElementTree.Element, set[str]]:
    unresolved: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = ETCSL_CHARACTER_ENTITIES.get(name)
        if replacement is not None:
            return replacement
        if name in ETCSL_DETERMINATIVE_ENTITIES:
            return ETCSL_DETERMINATIVE_TOKEN.format(name=name)
        if name == "hr":
            return ETCSL_HORIZONTAL_RULING
        if name in BUILTIN_ENTITIES:
            return match.group(0)
        replacement = HTML5_ENTITIES.get(f"{name};")
        if replacement is None:
            unresolved.add(name)
            return f"&amp;{name};"
        return replacement.replace("&", "&amp;").replace("<", "&lt;")

    prepared = ENTITY_REF_RE.sub(replace, document)
    try:
        return ElementTree.fromstring(prepared), unresolved
    except ElementTree.ParseError as exc:
        raise InventoryError(f"cannot parse reader XML {source_name}: {exc}") from exc


def _reader_title(root: ElementTree.Element, identifier: str) -> str:
    title = root.find("./teiHeader/fileDesc/titleStmt/title")
    if title is None:
        return f"Composition {identifier}"
    value = " ".join(_plain_reader_text("".join(title.itertext())).split())
    for suffix in TITLE_SUFFIXES:
        if value.endswith(suffix):
            return value.removesuffix(suffix)
    return value or f"Composition {identifier}"


def _identifier_sort_key(identifier: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in identifier.split(".")
    )


def _render_reader_index(
    identifiers: Sequence[str],
    titles: dict[str, str],
    records: dict[str, dict[str, object]],
    capture_id: str,
    archive_sha256: object,
) -> str:
    entries: list[str] = []
    current_group: str | None = None
    for identifier in identifiers:
        group = identifier.split(".", 1)[0]
        if group != current_group:
            if current_group is not None:
                entries.append("</ol></section>")
            entries.append(
                f'<section class="catalogue-group"><h2>Group {escape_html(group)}</h2><ol>'
            )
            current_group = group
        record = records[identifier]
        representations: list[str] = []
        if "transliteration" in record:
            representations.append("transliteration")
        if "translation" in record:
            representations.append("translation")
        entries.append(
            '<li><a href="works/{identifier}.html"><span class="work-id">{identifier}</span> '
            '<span class="work-title">{title}</span></a>'
            ' <span class="representations">{representations}</span></li>'.format(
                identifier=escape_html(identifier, quote=True),
                title=escape_html(titles[identifier]),
            representations=escape_html(" / ".join(representations)),
            )
        )
    if current_group is not None:
        entries.append("</ol></section>")
    content = "".join(entries)
    return _html_document(
        "ETCSL Reader",
        f"""
<header class="site-header">
  <p class="eyebrow">Derived access copy</p>
  <h1>Electronic Text Corpus of Sumerian Literature</h1>
  <p class="lede">A local catalogue reconstructed from the verified canonical XML deposit.</p>
</header>
<main>
  <aside class="notice">This is not the original ETCSL website. ETCSL character,
  determinative, subscript, and editorial entities are rendered from the corpus support
  declarations; any unknown entity remains visible as its source token.</aside>
  <div class="catalogue-meta"><span>{len(identifiers)} compositions</span>
  <span>Capture <code>{escape_html(capture_id)}</code></span></div>
  {content}
</main>
<footer>Canonical archive SHA-256: <code>{escape_html(str(archive_sha256))}</code></footer>
""",
    )


def _render_work_page(
    identifier: str,
    title: str,
    record: dict[str, object],
    capture_id: str,
    archive_sha256: object,
    previous_id: str | None,
    next_id: str | None,
) -> str:
    navigation = ['<a href="../index.html">Catalogue</a>']
    if previous_id is not None:
        navigation.append(
            f'<a href="{escape_html(previous_id, quote=True)}.html">&larr; {escape_html(previous_id)}</a>'
        )
    if next_id is not None:
        navigation.append(
            f'<a href="{escape_html(next_id, quote=True)}.html">{escape_html(next_id)} &rarr;</a>'
        )
    columns: list[str] = []
    for key, heading in (
        ("transliteration", "Composite transliteration"),
        ("translation", "English prose translation"),
    ):
        root = record.get(key)
        if isinstance(root, ElementTree.Element):
            source_path = escape_html(str(record[f"{key}_path"]))
            language = "sux-Latn" if key == "transliteration" else "en"
            columns.append(
                f'<article class="representation" lang="{language}"><header><h2>{heading}</h2>'
                f'<p class="source-path">{source_path}</p></header>{_render_text_bodies(root)}</article>'
            )
        else:
            columns.append(
                f'<article class="representation unavailable"><h2>{heading}</h2>'
                f"<p>No {escape_html(key)} is present in the canonical deposit.</p></article>"
            )
    return _html_document(
        f"{identifier} {title}",
        f"""
<nav class="work-nav">{' '.join(navigation)}</nav>
<header class="work-header"><p class="eyebrow">ETCSL {escape_html(identifier)}</p>
<h1>{escape_html(title)}</h1></header>
<main class="parallel-text">{''.join(columns)}</main>
<footer><span>Capture <code>{escape_html(capture_id)}</code></span>
<span>Archive SHA-256 <code>{escape_html(str(archive_sha256))}</code></span></footer>
""",
    )


def _render_text_bodies(root: ElementTree.Element) -> str:
    sections: list[str] = []
    for text in root.iter("text"):
        for body in text.findall("./body"):
            label_parts: list[str] = []
            if text.get("n"):
                label_parts.append(str(text.get("n")))
            head = text.find("./head")
            if head is not None:
                head_text = " ".join(_plain_reader_text("".join(head.itertext())).split())
                if head_text:
                    label_parts.append(head_text)
            heading = " - ".join(label_parts)
            heading_html = f"<h3>{escape_html(heading or 'Text')}</h3>"
            sections.append(
                f'<section class="text-body">{heading_html}{_render_blocks(body)}</section>'
            )
    return "".join(sections) or '<p class="unavailable">No text body was found.</p>'


def _render_blocks(element: ElementTree.Element) -> str:
    parts: list[str] = []
    if element.text and element.text.strip():
        parts.append(f"<p>{_render_reader_text(element.text.strip())}</p>")
    for child in element:
        tag = child.tag
        if tag == "head":
            parts.append(f"<h4>{_render_inline_content(child)}</h4>")
        elif tag == "l":
            label = escape_html(child.get("n", ""))
            parts.append(
                f'<div class="line"><span class="line-number">{label}</span>'
                f'<span class="line-text">{_render_inline_content(child)}</span></div>'
            )
        elif tag == "p":
            label = escape_html(child.get("n", ""))
            parts.append(
                f'<p class="paragraph"><span class="line-number">{label}</span>'
                f'{_render_inline_content(child)}</p>'
            )
        elif tag == "div1":
            label = child.get("n") or child.get("type") or "Section"
            parts.append(
                f'<section class="segment"><h4>{escape_html(label)}</h4>'
                f'{_render_blocks(child)}</section>'
            )
        elif tag == "lg":
            label = child.get("type")
            heading = f'<h4>{escape_html(label)}</h4>' if label else ""
            parts.append(f'<section class="line-group">{heading}{_render_blocks(child)}</section>')
        elif tag == "trailer":
            parts.append(f'<aside class="trailer">{_render_blocks(child)}</aside>')
        else:
            content = _render_inline_element(child)
            if content.strip():
                parts.append(f'<div class="annotation-block">{content}</div>')
        if child.tail and child.tail.strip():
            parts.append(f"<p>{_render_reader_text(child.tail.strip())}</p>")
    return "".join(parts)


def _render_inline_content(element: ElementTree.Element) -> str:
    parts = [_render_reader_text(element.text or "")]
    for child in element:
        parts.append(_render_inline_element(child))
        parts.append(_render_reader_text(child.tail or ""))
    return "".join(parts)


def _plain_reader_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("determinative")
        return "―" if name is None else ETCSL_DETERMINATIVE_ENTITIES[name]

    return ETCSL_READER_TOKEN_RE.sub(replace, value)


def _render_reader_text(value: str) -> str:
    parts: list[str] = []
    position = 0
    for match in ETCSL_READER_TOKEN_RE.finditer(value):
        parts.append(escape_html(value[position : match.start()]))
        name = match.group("determinative")
        if name is None:
            parts.append(
                '<span class="ruling" role="separator" title="horizontal ruling">―</span>'
            )
        else:
            label = ETCSL_DETERMINATIVE_ENTITIES[name]
            parts.append(
                f'<sup class="determinative" title="ETCSL determinative {escape_html(name, quote=True)}">'
                f"{escape_html(label)}</sup>"
            )
        position = match.end()
    parts.append(escape_html(value[position:]))
    return "".join(parts)


def _render_inline_element(element: ElementTree.Element) -> str:
    content = _render_inline_content(element)
    if element.tag == "gap":
        extent = escape_html(element.get("extent", "unspecified"))
        return f'<span class="gap">[gap: {extent}]</span>'
    if element.tag == "note":
        return f'<span class="note">[note: {content}]</span>'
    if element.tag == "q":
        return f"<q>{content}</q>"
    if element.tag == "foreign":
        return f'<span class="foreign">{content}</span>'
    if element.tag == "unclear":
        return f'<span class="unclear">{content}</span>'
    if element.tag == "corr":
        return f'<span class="correction">{content}</span>'
    if element.tag == "supplied":
        return f'<span class="milestone" title="supplied text begins">&#x27e8;</span>{content}'
    if element.tag == "suppliedEnd":
        return '<span class="milestone" title="supplied text ends">&#x27e9;</span>'
    if element.tag == "damage":
        return f'<span class="milestone" title="damaged text begins">&#x2e22;</span>{content}'
    if element.tag == "damageEnd":
        return '<span class="milestone" title="damaged text ends">&#x2e23;</span>'
    if element.tag == "lb":
        return "<br>"
    if element.tag == "w":
        kind = escape_html(element.get("type", "word"), quote=True)
        return f'<span class="word" title="{kind}">{content}</span>'
    return content


def _html_document(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{escape_html(title)}</title>
<style>
:root {{ color-scheme: light; --ink:#211c17; --muted:#71675d; --paper:#f4efe3;
  --panel:#fffdf7; --rule:#c7bda9; --accent:#8b321f; --blue:#234f61; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:var(--paper); font:17px/1.62 Georgia,serif; }}
a {{ color:var(--blue); text-decoration-thickness:.08em; text-underline-offset:.16em; }}
code,.work-id,.line-number,.source-path {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
footer code {{ overflow-wrap:anywhere; }}
.site-header,.work-header {{ padding:4rem max(5vw,1.25rem) 2.5rem; border-bottom:1px solid var(--rule); }}
h1,h2,h3,h4 {{ line-height:1.15; font-weight:600; }} h1 {{ max-width:22ch; font-size:clamp(2.3rem,6vw,5rem); margin:.2rem 0; }}
.eyebrow {{ color:var(--accent); font:700 .76rem/1.2 ui-monospace,monospace; letter-spacing:.14em; text-transform:uppercase; }}
.lede {{ max-width:55ch; color:var(--muted); font-size:1.15rem; }}
main {{ width:min(1100px,92vw); margin:2rem auto 5rem; }}
.notice {{ border-left:4px solid var(--accent); padding:1rem 1.2rem; background:var(--panel); }}
.catalogue-meta,footer,.work-nav {{ display:flex; flex-wrap:wrap; gap:1rem 2rem; color:var(--muted); }}
.catalogue-group {{ margin-top:3rem; }} .catalogue-group ol {{ list-style:none; padding:0; }}
.catalogue-group li {{ display:grid; grid-template-columns:1fr auto; gap:1rem; border-top:1px solid var(--rule); padding:.8rem 0; }}
.catalogue-group a {{ text-decoration:none; }} .work-id {{ display:inline-block; width:7rem; color:var(--accent); }}
.representations {{ color:var(--muted); font-size:.82rem; }}
.work-nav {{ padding:1rem max(4vw,1rem); border-bottom:1px solid var(--rule); background:var(--panel); }}
.parallel-text {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:2rem; align-items:start; }}
.representation {{ min-width:0; padding:1.5rem; background:var(--panel); border:1px solid var(--rule); }}
.representation>header {{ border-bottom:1px solid var(--rule); margin-bottom:1.5rem; }}
.source-path {{ overflow-wrap:anywhere; color:var(--muted); font-size:.7rem; }}
.text-body+.text-body,.segment,.line-group,.trailer {{ margin-top:1.75rem; }}
.line,.paragraph {{ position:relative; padding-left:4.8rem; margin:.55rem 0; }}
.line-number {{ position:absolute; left:0; width:4rem; color:var(--muted); font-size:.72rem; text-align:right; }}
.note {{ color:var(--muted); font-size:.9em; }} .gap {{ color:var(--accent); }}
.foreign {{ font-style:italic; }} .unclear {{ text-decoration:underline dotted; }}
.correction {{ border-bottom:1px dashed var(--accent); }} .milestone {{ color:var(--accent); }}
.determinative {{ color:var(--accent); font-style:normal; }}
.ruling {{ display:inline-block; min-width:7rem; color:var(--muted); letter-spacing:.1em; }}
.trailer {{ border-top:1px solid var(--rule); padding-top:1rem; font-style:italic; }}
footer {{ width:min(1100px,92vw); margin:3rem auto; padding-top:1rem; border-top:1px solid var(--rule); font-size:.72rem; }}
@media (max-width:800px) {{ .parallel-text {{ grid-template-columns:1fr; }} .catalogue-group li {{ grid-template-columns:1fr; }} }}
@media print {{ body {{ background:white; }} .work-nav {{ display:none; }} .representation {{ border:0; padding:0; }} }}
</style>
</head>
<body>{content}</body>
</html>
"""


def _analyze_zip(path: Path, expected_work_count: int) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise InventoryError(
                f"deposit has {len(infos)} entries, above safety limit {MAX_ARCHIVE_ENTRIES}"
            )
        names = [info.filename for info in infos]
        duplicate_paths = sorted(name for name, count in Counter(names).items() if count > 1)
        unsafe_paths = sorted(
            info.filename for info in infos if not _safe_archive_path(info.filename)
        )
        symlinks = sorted(
            info.filename
            for info in infos
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)
        )
        encrypted = sorted(info.filename for info in infos if info.flag_bits & 0x1)
        oversized = sorted(
            info.filename for info in infos if info.file_size > MAX_ENTRY_SIZE
        )
        excessive_ratio = sorted(
            info.filename
            for info in infos
            if info.compress_size
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        )
        total_size = sum(info.file_size for info in infos)
        if total_size > MAX_ARCHIVE_SIZE:
            errors.append(f"deposit expands to {total_size} bytes, above safety limit")
        for label, values in (
            ("duplicate archive path", duplicate_paths),
            ("unsafe archive path", unsafe_paths),
            ("archive symlink", symlinks),
            ("encrypted archive entry", encrypted),
            ("oversized archive entry", oversized),
            ("excessive compression ratio", excessive_ratio),
        ):
            if values:
                errors.append(f"{label}: {values[0]}")
        if errors:
            raise InventoryError(errors[0])
        bad_crc = archive.testzip()
        if bad_crc is not None:
            errors.append(f"archive CRC failure: {bad_crc}")

        xml_entries: dict[str, tuple[str, str, zipfile.ZipInfo]] = {}
        transliteration_ids: set[str] = set()
        translation_ids: set[str] = set()
        for info in infos:
            match = XML_ENTRY_RE.fullmatch(info.filename)
            if match is None:
                continue
            identifier = match.group("id")
            kind = match.group("kind")
            expected_directory = "transliterations" if kind == "c" else "translations"
            if match.group("directory") != expected_directory:
                raise InventoryError(
                    f"deposit XML kind does not match directory: {info.filename}"
                )
            xml_entries[info.filename] = (kind, identifier, info)
            (transliteration_ids if kind == "c" else translation_ids).add(identifier)

        archive_names = set(names)
        support_files, declared_entities, external_references = _support_graph(
            archive,
            archive_names,
        )

        used_entities: set[str] = set()
        unbound_entities: set[str] = set()
        doctype_count = 0
        raw_parse_count = 0
        stubbed_parse_count = 0
        filename_id_match_count = 0
        xml_errors: list[str] = []
        for name, (_kind, identifier, info) in xml_entries.items():
            document = _read_zip_text(archive, info)
            entity_names = set(ENTITY_REF_RE.findall(document)) - BUILTIN_ENTITIES
            used_entities.update(entity_names)
            if "<!DOCTYPE" in document:
                doctype_count += 1
                unbound_entities.update(entity_names - declared_entities)
            else:
                unbound_entities.update(entity_names)
            try:
                ElementTree.fromstring(document)
            except ElementTree.ParseError:
                pass
            else:
                raw_parse_count += 1
            declarations = "".join(
                f'<!ENTITY {entity} "">' for entity in sorted(entity_names)
            )
            stubbed = f"<!DOCTYPE TEI.2 [{declarations}]>{document}"
            try:
                root = ElementTree.fromstring(stubbed)
            except ElementTree.ParseError as exc:
                xml_errors.append(f"{name}: {exc}")
                continue
            stubbed_parse_count += 1
            if root.tag == "TEI.2" and root.get("id") == f"{_kind}.{identifier}":
                filename_id_match_count += 1
            else:
                xml_errors.append(f"{name}: root name or ID does not match filename")

        missing_entity_names = sorted(unbound_entities)
        missing_external_resources = sorted(
            f"{source} -> {target}" for source, target in external_references
        )
        if xml_errors:
            errors.append(f"XML shell validation failed: {xml_errors[0]}")
        if missing_entity_names:
            errors.append(f"undeclared named XML entity: {missing_entity_names[0]}")
        if missing_external_resources:
            errors.append(f"missing external XML dependency: {missing_external_resources[0]}")
        missing_counterparts = sorted(translation_ids - transliteration_ids)
        if missing_counterparts:
            errors.append(f"translation has no transliteration: {missing_counterparts[0]}")
        expected_translation_count = expected_work_count - len(
            transliteration_ids & KNOWN_UNTRANSLATED
        )
        if len(transliteration_ids) != expected_work_count:
            errors.append(
                f"deposit has {len(transliteration_ids)} transliterations, expected {expected_work_count}"
            )
        if len(translation_ids) != expected_translation_count:
            errors.append(
                f"deposit has {len(translation_ids)} translations, expected {expected_translation_count}"
            )

    return {
        "archive_path": path.name,
        "sha256": _sha256(path),
        "compressed_size": path.stat().st_size,
        "entry_count": len(infos),
        "uncompressed_size": total_size,
        "crc_ok": bad_crc is None,
        "transliteration_count": len(transliteration_ids),
        "translation_count": len(translation_ids),
        "transliteration_ids": sorted(transliteration_ids),
        "translation_ids": sorted(translation_ids),
        "missing_transliteration_counterparts": missing_counterparts,
        "xml_file_count": len(xml_entries),
        "raw_xml_parse_count": raw_parse_count,
        "entity_stubbed_xml_parse_count": stubbed_parse_count,
        "filename_id_match_count": filename_id_match_count,
        "doctype_count": doctype_count,
        "used_entity_count": len(used_entities),
        "support_file_count": len(support_files),
        "support_declared_used_entity_count": len(used_entities & declared_entities),
        "missing_entity_names": missing_entity_names,
        "missing_external_resources": missing_external_resources,
        "duplicate_paths": duplicate_paths,
        "unsafe_paths": unsafe_paths,
        "symlinks": symlinks,
        "encrypted_entries": encrypted,
        "errors": errors,
        "warnings": warnings,
    }


def _support_graph(
    archive: zipfile.ZipFile,
    archive_names: set[str],
    root: str = "etcsl/tei/tei2.dtd",
) -> tuple[set[str], set[str], list[tuple[str, str]]]:
    visited: set[str] = set()
    declared_entities: set[str] = set()
    missing: list[tuple[str, str]] = []
    pending: deque[str] = deque([root])
    if root not in archive_names:
        return visited, declared_entities, [("deposit", root)]
    while pending:
        name = pending.popleft()
        if name in visited:
            continue
        visited.add(name)
        document = _read_zip_text(archive, archive.getinfo(name))
        declared_entities.update(ENTITY_DECL_RE.findall(document))
        targets = SYSTEM_REF_RE.findall(document) + PUBLIC_REF_RE.findall(document)
        for target in targets:
            if "://" in target:
                missing.append((name, target))
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if resolved not in archive_names:
                missing.append((name, resolved))
            elif resolved.endswith((".dtd", ".ent")):
                pending.append(resolved)
    return visited, declared_entities, missing


def _find_catalogue(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    matches: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ENTRY_SIZE:
            continue
        try:
            prefix = path.read_bytes()
        except OSError:
            continue
        if b"Catalogue of all available compositions" in prefix and b"text=c." in prefix:
            matches.append(path)
    if len(matches) > 1:
        raise InventoryError("multiple captured ETCSL catalogues found")
    return matches[0] if matches else None


def _find_zip(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    matches = [
        path
        for path in root.rglob("*")
        if not path.is_symlink() and path.is_file() and zipfile.is_zipfile(path)
    ]
    if len(matches) > 1:
        raise InventoryError("multiple captured ETCSL deposit ZIPs found")
    return matches[0] if matches else None


def _safe_archive_path(value: str) -> bool:
    if "\\" in value:
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def _read_zip_text(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if info.file_size > MAX_ENTRY_SIZE:
        raise InventoryError(f"archive entry exceeds safety limit: {info.filename}")
    return archive.read(info).decode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InventoryError(f"expected a JSON object: {path}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalogue", type=Path, help="captured ETCSL catalogue HTML")
    parser.add_argument("--expected-work-count", type=int, default=394)
    parser.add_argument("--known-untranslated", action="append", default=None)
    args = parser.parse_args(argv)
    try:
        document = args.catalogue.read_text(encoding="utf-8")
        compositions = extract_inventory(document)
        report = build_report(
            compositions,
            expected_work_count=args.expected_work_count,
            known_untranslated=(
                args.known_untranslated
                if args.known_untranslated is not None
                else KNOWN_UNTRANSLATED
            ),
        )
    except (OSError, UnicodeError, InventoryError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
