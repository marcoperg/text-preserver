"""Extract and validate the ETCSL composition catalogue."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass
import hashlib
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
MAX_ARCHIVE_SIZE = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ENTRY_SIZE = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
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
