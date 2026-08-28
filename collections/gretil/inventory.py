"""Extract and validate the publisher-manifested GRETIL corpus."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from html import escape as escape_html
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable, Sequence
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ElementTree
import zipfile


REGISTER_URL = "https://gretil.sub.uni-goettingen.de/gretil.html"
PRIMARY_HOST = "gretil.sub.uni-goettingen.de"
FIRST_PARTY_HOSTS = frozenset({PRIMARY_HOST, "gretil.uni-goettingen.de"})
EXPECTED_BULK_PACKAGES = (
    "1_sanskr.zip",
    "2_pali.zip",
    "2_prakrt.zip",
    "3_nia.zip",
    "4_drav.zip",
    "5_oldjav.zip",
    "5_tib.zip",
    "6_sres.zip",
)
EXPECTED_DICTIONARIES = (
    "aptese.dict.xdxf",
    "aptees.dict.xdxf",
    "aufrcc.dict.xdxf",
    "benfse.dict.xdxf",
    "boppsl.dict.xdxf",
    "bores.dict.xdxf",
    "bursf.dict.xdxf",
    "cappse.dict.xdxf",
    "cappsg.dict.xdxf",
    "grasg_a.dict.xdxf",
    "macdse.dict.xdxf",
    "mwes.dict.xdxf",
    "mwse.dict.xdxf",
    "mwse72.dict.xdxf",
    "pese.dict.xdxf",
    "pwg.dict.xdxf",
    "pwk.dict.xdxf",
    "schnzsw.dict.xdxf",
    "skdss.dict.xdxf",
    "stcsf.dict.xdxf",
    "vcpss.dict.xdxf",
)
EXPECTED_CURRENT_FILES = (
    "GAU-SUB_einzeilig_weiss.png",
    "feed.xml",
    "gr_elib.htm",
    "gr_elib2.gif",
    "gretil.htm",
    "gretil.html",
    "hist.html",
    "i_download.gif",
    "index.html",
    "script.js",
    "style-1.css",
    "style.css?rnd=132",
)
EXPECTED_FROZEN_FILES = (
    "SUB-Header_logo_1200.png",
    "b_jaune.gif",
    "gret_csxbk.htm",
    "gret_reebk.htm",
    "gret_utfbk.htm",
    "gretinfobk.htm",
    "gretdiac.pdf",
    "gretdias.pdf",
    "gretilbk.htm",
    "style-1.css",
    "unten4.gif",
)
EXPECTED_TEI_ID_SHA256 = (
    "e51d93dc2547b26456bb466ceca65bd392a97ced8af2945d6ebfa67af28bebe4"
)
TEI_RE = re.compile(r"^/gretil/corpustei/(?P<id>[^/]+)\.xml$")
HTML_RE = re.compile(
    r"^/gretil/corpustei/transformations/html/(?P<id>[^/]+)\.htm$"
)
TEXT_RE = re.compile(
    r"^/gretil/corpustei/transformations/plaintext/(?P<id>[^/]+)\.txt$"
)
BULK_TEI_RE = re.compile(r"(?:^|/)tei/(?P<id>[^/]+)\.xml$")
TEI_ROOT = "{http://www.tei-c.org/ns/1.0}TEI"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"
TEI = {"tei": TEI_NAMESPACE}
BULK_TEI_MAPPINGS = {
    "sa_bANa-kAdambarI-1,84-122": (
        "sa_bANa-kAdambarI-1",
        "sa_bANa-kAdambarI-1.84-122",
    ),
    "sa_bAdarAyaNa-brahmasUtra1,1-1,3-comm-subcomm": (
        "sa_bAdarAyaNa-brahmasUtra1",
        "sa_bAdarAyaNa-brahmasUtra1.1-1.3-comm-subcomm",
    ),
    "sa_bhAgavatapurANa-10,29-33": (
        "sa_bhAgavatapurANa-10",
        "sa_bhAgavatapurANa-10.29-33",
    ),
    "sa_bhartRhari-vAkyapadIya1-3,7": (
        "sa_bhartRhari-vAkyapadIya1-3",
        "sa_bhartRhari-vAkyapadIya1-3.7",
    ),
    "sa_gautama-nyAyasUtra5,1-comm": (
        "sa_gautama-nyAyasUtra5",
        "sa_gautama-nyAyasUtra5.1-comm",
    ),
    "sa_gopathabrAhmaNa-1,1,1-1,3,6": (
        "sa_gopathabrAhmaNa-1",
        "sa_gopathabrAhmaNa-1.1.1-1.3.6",
    ),
    "sa_khaNDadeva-bhATTadIpikA1-3,3": (
        "sa_khaNDadeva-bhATTadIpikA1-3",
        "sa_khaNDadeva-bhATTadIpikA1-3.3",
    ),
    "sa_kumArila-tantravArttika-2,1,1-4": (
        "sa_kumArila-tantravArttika-2",
        "sa_kumArila-tantravArttika-2.1.1-4",
    ),
    "sa_liGgapurANa2,1-55": (
        "sa_liGgapurANa2",
        "sa_liGgapurANa2.1-55",
    ),
    "sa_mRgendrAgama1,1-7,23": (
        "sa_mRgendrAgama1",
        "sa_mRgendrAgama1.1-7.23",
    ),
    "sa_nAgArjuna-ratnAvali1,2,4": (
        "sa_nAgArjuna-ratnAvali1",
        "sa_nAgArjuna-ratnAvali1.2.4",
    ),
    "sa_rAmAnuja-zrIbhASya-1,1,3": (
        "sa_rAmAnuja-zrIbhASya-1",
        "sa_rAmAnuja-zrIbhASya-1.1.3",
    ),
    "sa_skandapurANa1-31,14": (
        "sa_skandapurANa1-31",
        "sa_skandapurANa1-31,14",
    ),
    "sa_vAgbhaTa-rasaratnasamuccaya-1-18,29": (
        "sa_vAgbhaTa-rasaratnasamuccaya-1-18",
        "sa_vAgbhaTa-rasaratnasamuccaya-1-18.29",
    ),
    "sa_viSNudharmottarapurANa-2,127": (
        "sa_viSNudharmottarapurANa-2",
        "sa_viSNudharmottarapurANa-2.127",
    ),
    "sa_viSNudharmottarapurANa3,343-353": (
        "sa_viSNudharmottarapurANa3",
        "sa_viSNudharmottarapurANa3-343-353",
    ),
    "sa_zabara-mImAMsAsUtrabhASya-1,1,1-5": (
        "sa_zabara-mImAMsAsUtrabhASya-1",
        "sa_zabara-mImAMsAsUtrabhASya-1.1.1-5",
    ),
}
BULK_TEI_ROOT_ID_EXCEPTIONS = {
    "sa_kAtyAyanasmRti": "sa_kAtyAyanasmRti.xml",
    "sa_veGkaTanAtha-nyAyaparizuddhibhUmikA-13-5": (
        "sa_veGkaTanAtha-nyAyaparizuddhibhUmik-A13-5"
    ),
    "ta-sa_periyavaccanpillai-perumaltiromolivyAkhyAnam": (
        "ta-sa_periyavaccanpillai-perumaltiromolivyAkhyAnam.xml"
    ),
    "xct_tshangs-dbyangs-rgya-mtsho'i-mgul-glu": (
        "xct_tshangs-dbyangs-rgya-mtsho-i-mgul-glu"
    ),
}
MAX_CATALOGUE_SIZE = 4 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_COMPRESSED_ARCHIVE_SIZE = 400 * 1024 * 1024
MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
MAX_ENTRY_SIZE = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500
MAX_READER_TEI_SIZE = 64 * 1024 * 1024
KNOWN_BULK_ONLY_TEI = frozenset({"sa_jayAditya-and-vAmana-kAzikAvRtti"})
READER_PACKAGE = "1_sanskr.zip"
READER_RENDITIONS = frozenset({
    "bold",
    "blue",
    "center",
    "green",
    "it",
    "italic",
    "red",
    "small",
    "sub",
    "sup",
    "underline",
})


class InventoryError(ValueError):
    """Raised when GRETIL inventory evidence is unsafe or ambiguous."""


@dataclass(frozen=True)
class RegisterInventory:
    first_party_urls: tuple[str, ...]
    tei_ids: tuple[str, ...]
    analytic_html_ids: tuple[str, ...]
    plaintext_ids: tuple[str, ...]
    bulk_packages: tuple[str, ...]
    dictionaries: tuple[str, ...]


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        key = "href" if tag in {"a", "link"} else "src" if tag in {"img", "script"} else None
        if key is None:
            return
        value = dict(attrs).get(key)
        if value:
            self.links.append(urljoin(self.base_url, value))


def extract_inventory(
    document: str,
    *,
    base_url: str = REGISTER_URL,
) -> RegisterInventory:
    """Extract normalized first-party representation links from the register."""
    parser = _LinkParser(base_url)
    parser.feed(document)
    parser.close()
    urls = sorted({
        normalized
        for value in parser.links
        if (normalized := _normalize_first_party_url(value)) is not None
    })
    if not urls:
        raise InventoryError("GRETIL register contains no first-party links")

    tei_ids: set[str] = set()
    html_ids: set[str] = set()
    text_ids: set[str] = set()
    bulk_packages: set[str] = set()
    dictionaries: set[str] = set()
    for url in urls:
        path = unquote(urlsplit(url).path)
        if match := TEI_RE.fullmatch(path):
            tei_ids.add(match.group("id"))
        elif match := HTML_RE.fullmatch(path):
            html_ids.add(match.group("id"))
        elif match := TEXT_RE.fullmatch(path):
            text_ids.add(match.group("id"))
        elif path.startswith("/gretil/") and path.endswith(".dict.xdxf"):
            dictionaries.add(PurePosixPath(path).name)
        elif path.startswith("/gretil/") and path.count("/") == 2 and path.endswith(".zip"):
            bulk_packages.add(PurePosixPath(path).name)
    if not tei_ids:
        raise InventoryError("GRETIL register contains no TEI corpus links")
    return RegisterInventory(
        tuple(urls),
        tuple(sorted(tei_ids)),
        tuple(sorted(html_ids)),
        tuple(sorted(text_ids)),
        tuple(sorted(bulk_packages)),
        tuple(sorted(dictionaries)),
    )


def build_report(
    inventory: RegisterInventory,
    *,
    expected_work_count: int = 801,
    expected_bulk_packages: Iterable[str] = EXPECTED_BULK_PACKAGES,
    expected_dictionary_count: int = 21,
    required_representation_kinds: Sequence[str] = ("tei",),
    expected_tei_id_sha256: str | None = None,
) -> dict[str, object]:
    """Build an explainable completeness report from register links."""
    tei = set(inventory.tei_ids)
    html = set(inventory.analytic_html_ids)
    text = set(inventory.plaintext_ids)
    errors: list[str] = []
    warnings: list[str] = []

    if len(tei) != expected_work_count:
        errors.append(f"expected {expected_work_count} TEI records, found {len(tei)}")
    tei_id_sha256 = hashlib.sha256(("\n".join(sorted(tei)) + "\n").encode()).hexdigest()
    if expected_tei_id_sha256 is not None and tei_id_sha256 != expected_tei_id_sha256:
        errors.append("TEI identifier set does not match the reviewed register baseline")
    expected_bulk = set(expected_bulk_packages)
    missing_bulk = sorted(expected_bulk - set(inventory.bulk_packages))
    unexpected_bulk = sorted(set(inventory.bulk_packages) - expected_bulk)
    if missing_bulk:
        errors.append(f"register does not link bulk package {missing_bulk[0]}")
    if unexpected_bulk:
        errors.append(f"register links unexpected bulk package {unexpected_bulk[0]}")
    actual_dictionaries = set(inventory.dictionaries)
    expected_dictionaries = set(EXPECTED_DICTIONARIES)
    missing_dictionaries = sorted(expected_dictionaries - actual_dictionaries)
    unexpected_dictionaries = sorted(actual_dictionaries - expected_dictionaries)
    if len(inventory.dictionaries) != expected_dictionary_count:
        errors.append(
            f"expected {expected_dictionary_count} dictionaries, found {len(inventory.dictionaries)}"
        )
    if expected_dictionary_count == len(EXPECTED_DICTIONARIES):
        if missing_dictionaries:
            errors.append(f"register does not link dictionary {missing_dictionaries[0]}")
        if unexpected_dictionaries:
            errors.append(f"register links unexpected dictionary {unexpected_dictionaries[0]}")

    missing_html = sorted(tei - html)
    missing_text = sorted(tei - text)
    orphan_html = sorted(html - tei)
    orphan_text = sorted(text - tei)
    required = set(required_representation_kinds)
    if "analytic-html" in required and missing_html:
        errors.append(f"TEI record has no analytic HTML: {missing_html[0]}")
    elif missing_html:
        warnings.append(f"{len(missing_html)} TEI records have no analytic HTML link")
    if "plaintext" in required and missing_text:
        errors.append(f"TEI record has no plain-text transformation: {missing_text[0]}")
    elif missing_text:
        warnings.append(f"{len(missing_text)} TEI records have no plain-text link")
    if orphan_html:
        warnings.append(f"{len(orphan_html)} analytic HTML links have no TEI counterpart")
    if orphan_text:
        warnings.append(f"{len(orphan_text)} plain-text links have no TEI counterpart")
    unknown_required = sorted(required - {"tei", "analytic-html", "plaintext"})
    if unknown_required:
        errors.append(f"unknown required representation kind: {unknown_required[0]}")
    return {
        "status": (
            "incomplete"
            if errors
            else ("complete_with_warnings" if warnings else "complete")
        ),
        "expected_work_count": expected_work_count,
        "work_count": len(tei),
        "tei_count": len(tei),
        "analytic_html_count": len(html),
        "plaintext_count": len(text),
        "dictionary_count": len(inventory.dictionaries),
        "bulk_package_count": len(inventory.bulk_packages),
        "first_party_url_count": len(inventory.first_party_urls),
        "tei_id_sha256": tei_id_sha256,
        "missing_analytic_html": missing_html,
        "missing_plaintext": missing_text,
        "orphan_analytic_html": orphan_html,
        "orphan_plaintext": orphan_text,
        "missing_bulk_packages": missing_bulk,
        "unexpected_bulk_packages": unexpected_bulk,
        "missing_dictionaries": missing_dictionaries,
        "unexpected_dictionaries": unexpected_dictionaries,
        "errors": errors,
        "warnings": warnings,
    }


def analyze_capture(
    capture_directory: Path,
    *,
    expected_work_count: int,
    required_representation_kinds: Sequence[str],
    required_source_ids: Sequence[str],
) -> dict[str, object]:
    """Analyze captured GRETIL manifests and packages without extracting them."""
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

    try:
        current_files = _named_files(
            capture_directory / "sources/current-register/mirror",
            None,
        )
    except InventoryError as exc:
        current_files = {}
        errors.append(f"cannot inventory current-register files: {exc}")
    missing_current = sorted(set(EXPECTED_CURRENT_FILES) - set(current_files))
    if missing_current:
        errors.append(f"captured current-register file was not found: {missing_current[0]}")

    register_report: dict[str, object] | None = None
    register_tei_ids: set[str] = set()
    try:
        register_path = _find_unique(
            capture_directory / "sources/current-register/mirror",
            "gretil.html",
        )
    except InventoryError as exc:
        register_path = None
        errors.append(f"cannot locate captured GRETIL register: {exc}")
    if register_path is None:
        errors.append("captured GRETIL register was not found")
    else:
        try:
            if register_path.stat().st_size > MAX_CATALOGUE_SIZE:
                raise InventoryError("GRETIL register exceeds catalogue safety limit")
            inventory = extract_inventory(register_path.read_text(encoding="utf-8"))
            register_tei_ids = set(inventory.tei_ids)
            register_report = build_report(
                inventory,
                expected_work_count=expected_work_count,
                required_representation_kinds=required_representation_kinds,
                expected_tei_id_sha256=(
                    EXPECTED_TEI_ID_SHA256 if expected_work_count == 801 else None
                ),
            )
            errors.extend(str(value) for value in register_report["errors"])
            warnings.extend(str(value) for value in register_report["warnings"])
        except (OSError, UnicodeError, InventoryError) as exc:
            errors.append(f"cannot analyze captured GRETIL register: {exc}")

    bulk_reports: list[dict[str, object]] = []
    bulk_tei_sources: dict[str, list[str]] = {}
    bulk_root = capture_directory / "sources/bulk-packages/mirror"
    for name in EXPECTED_BULK_PACKAGES:
        try:
            path = _find_unique(bulk_root, name)
        except InventoryError as exc:
            errors.append(f"cannot locate bulk package {name}: {exc}")
            continue
        if path is None:
            errors.append(f"captured bulk package was not found: {name}")
            continue
        try:
            package_report = _analyze_zip(path)
            bulk_reports.append(package_report)
            for identifier in set(package_report["tei_ids"]):
                bulk_tei_sources.setdefault(str(identifier), []).append(name)
        except (OSError, zipfile.BadZipFile, InventoryError) as exc:
            errors.append(f"cannot analyze bulk package {name}: {exc}")

    bulk_tei_ids = set(bulk_tei_sources)
    missing_bulk_tei = sorted(register_tei_ids - bulk_tei_ids)
    unexpected_bulk_tei = sorted(bulk_tei_ids - register_tei_ids)
    duplicate_bulk_tei = {
        identifier: packages
        for identifier, packages in sorted(bulk_tei_sources.items())
        if len(packages) > 1
    }
    if register_tei_ids and missing_bulk_tei:
        errors.append(f"registered TEI record is absent from bulk packages: {missing_bulk_tei[0]}")
    if unexpected_bulk_tei:
        warnings.append(
            f"{len(unexpected_bulk_tei)} bulk TEI identifiers are absent from the register"
        )
    if duplicate_bulk_tei:
        warnings.append(
            f"{len(duplicate_bulk_tei)} TEI identifiers occur in multiple bulk packages"
        )

    try:
        dictionary_files = _named_files(
            capture_directory / "sources/dictionaries/mirror",
            ".xdxf",
        )
    except InventoryError as exc:
        dictionary_files = {}
        errors.append(f"cannot inventory captured dictionaries: {exc}")
    missing_dictionaries = sorted(set(EXPECTED_DICTIONARIES) - set(dictionary_files))
    unexpected_dictionaries = sorted(set(dictionary_files) - set(EXPECTED_DICTIONARIES))
    if missing_dictionaries:
        errors.append(f"captured dictionary was not found: {missing_dictionaries[0]}")
    if unexpected_dictionaries:
        errors.append(f"unexpected captured dictionary: {unexpected_dictionaries[0]}")

    try:
        frozen_files = _named_files(
            capture_directory / "sources/frozen-register/mirror",
            None,
        )
    except InventoryError as exc:
        frozen_files = {}
        errors.append(f"cannot inventory frozen-register files: {exc}")
    missing_frozen = sorted(set(EXPECTED_FROZEN_FILES) - set(frozen_files))
    if missing_frozen:
        errors.append(f"captured frozen-register file was not found: {missing_frozen[0]}")

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
        "register_path": (
            str(register_path.relative_to(capture_directory)) if register_path else None
        ),
        "register": register_report,
        "current_register": {
            "count": len(current_files),
            "missing": missing_current,
        },
        "bulk_packages": bulk_reports,
        "bulk_tei": {
            "count": len(bulk_tei_ids),
            "missing_from_packages": missing_bulk_tei,
            "absent_from_register": unexpected_bulk_tei,
            "cross_package_duplicates": duplicate_bulk_tei,
        },
        "dictionaries": {
            "count": len(dictionary_files),
            "missing": missing_dictionaries,
            "unexpected": unexpected_dictionaries,
        },
        "frozen_register": {
            "count": len(frozen_files),
            "missing": missing_frozen,
        },
    }


def write_static_reader(
    capture_directory: Path,
    *,
    output_directory: Path,
    expected_work_count: int,
) -> dict[str, object]:
    """Stream a complete inert HTML reader from the aggregate publisher TEI package."""
    expected_ids = _reader_expected_ids(capture_directory, expected_work_count)
    package_path = _find_unique(
        capture_directory / "sources/bulk-packages/mirror",
        READER_PACKAGE,
    )
    if package_path is None:
        raise InventoryError(f"captured reader package was not found: {READER_PACKAGE}")
    package_report = _analyze_zip(package_path)
    package_ids = set(package_report["tei_ids"])
    missing_ids = sorted(set(expected_ids) - package_ids)
    extra_ids = sorted(package_ids - set(expected_ids))
    unexpected_extra_ids = sorted(set(extra_ids) - KNOWN_BULK_ONLY_TEI)

    output_directory.mkdir(parents=True, exist_ok=True)
    works_directory = output_directory / "works"
    assets_directory = output_directory / "assets"
    works_directory.mkdir()
    assets_directory.mkdir()
    (assets_directory / "reader.css").write_text(_reader_stylesheet(), encoding="utf-8")

    routes = {
        identifier: f"works/{position:04d}.html"
        for position, identifier in enumerate(expected_ids, start=1)
    }
    records: list[dict[str, object]] = []
    no_body_ids: list[str] = []
    missing_availability_ids: list[str] = []
    missing_source_ids: list[str] = []
    package_sha256 = _sha256_file(package_path)
    with zipfile.ZipFile(package_path) as archive:
        members: dict[str, tuple[zipfile.ZipInfo, str, str]] = {}
        for info in archive.infolist():
            match = BULK_TEI_RE.search(info.filename)
            if match is None:
                continue
            filename_id = match.group("id")
            register_id, expected_root_id = BULK_TEI_MAPPINGS.get(
                filename_id,
                (
                    filename_id,
                    BULK_TEI_ROOT_ID_EXCEPTIONS.get(filename_id, filename_id),
                ),
            )
            if register_id not in routes:
                continue
            if register_id in members:
                raise InventoryError(f"duplicate reader TEI record: {register_id}")
            members[register_id] = (info, filename_id, expected_root_id)

        for position, identifier in enumerate(expected_ids):
            member = members.get(identifier)
            if member is None:
                continue
            info, filename_id, expected_root_id = member
            if info.file_size > MAX_READER_TEI_SIZE:
                raise InventoryError(
                    f"reader TEI member exceeds {MAX_READER_TEI_SIZE} bytes: {info.filename}"
                )
            try:
                with archive.open(info) as stream:
                    root = ElementTree.parse(stream).getroot()
            except ElementTree.ParseError as exc:
                raise InventoryError(f"invalid reader TEI member {info.filename}: {exc}") from exc
            if root.tag != TEI_ROOT or root.get(XML_ID) != expected_root_id:
                raise InventoryError(f"reader TEI identity does not match: {info.filename}")
            record = _reader_record(root, identifier, filename_id, info.filename, routes[identifier])
            records.append(record)
            body = root.find("./tei:text/tei:body", TEI)
            if body is None or not any((value or "").strip() for value in body.itertext()):
                no_body_ids.append(identifier)
            availability = root.find(
                "./tei:teiHeader/tei:fileDesc/tei:publicationStmt/tei:availability",
                TEI,
            )
            if availability is None:
                missing_availability_ids.append(identifier)
            source_description = root.find(
                "./tei:teiHeader/tei:fileDesc/tei:sourceDesc",
                TEI,
            )
            if source_description is None:
                missing_source_ids.append(identifier)
            previous_id = expected_ids[position - 1] if position else None
            next_id = expected_ids[position + 1] if position + 1 < len(expected_ids) else None
            page = _render_reader_work(
                root,
                record,
                capture_directory.name,
                package_sha256,
                routes.get(previous_id) if previous_id else None,
                routes.get(next_id) if next_id else None,
                previous_id,
                next_id,
            )
            (output_directory / routes[identifier]).write_text(page, encoding="utf-8")

    rendered_ids = {str(record["identifier"]) for record in records}
    missing_rendered = sorted(set(expected_ids) - rendered_ids)
    if missing_rendered != missing_ids:
        raise InventoryError("reader package inventory changed during rendering")
    warnings = [
        "TEI stand-off references and source rendition tokens are displayed without external resolution",
    ]
    if extra_ids:
        warnings.append(
            f"{len(extra_ids)} bulk-only TEI record is excluded from the reviewed register reader"
        )
    if no_body_ids:
        warnings.append(f"{len(no_body_ids)} TEI records contain no direct text body")
    if missing_availability_ids:
        warnings.append(
            f"{len(missing_availability_ids)} TEI records have no availability statement"
        )
    if missing_source_ids:
        warnings.append(f"{len(missing_source_ids)} TEI records have no source description")
    if missing_ids:
        warnings.append(f"{len(missing_ids)} reviewed TEI records are absent from the reader package")
    if unexpected_extra_ids:
        warnings.append(
            f"{len(unexpected_extra_ids)} unexpected TEI records occur in the reader package"
        )

    (output_directory / "index.html").write_text(
        _render_reader_index(records, capture_directory.name, package_sha256),
        encoding="utf-8",
    )
    (output_directory / "about.html").write_text(
        _render_reader_about(
            capture_directory.name,
            package_sha256,
            package_report,
            extra_ids,
            warnings,
        ),
        encoding="utf-8",
    )
    complete = (
        len(records) == expected_work_count
        and not missing_ids
        and not unexpected_extra_ids
        and not no_body_ids
        and not missing_availability_ids
        and not missing_source_ids
        and bool(package_report["crc_ok"])
    )
    return {
        "status": "complete_with_warnings" if complete else "incomplete",
        "summary": {
            "work_count": len(records),
            "full_text_work_count": len(records) - len(no_body_ids),
            "expected_work_count": expected_work_count,
            "package": READER_PACKAGE,
            "package_sha256": package_sha256,
            "package_entry_count": package_report["entry_count"],
            "package_uncompressed_size": package_report["uncompressed_size"],
            "missing_ids": missing_ids,
            "bulk_only_ids": extra_ids,
            "unexpected_extra_ids": unexpected_extra_ids,
            "no_body_ids": no_body_ids,
            "missing_availability_ids": missing_availability_ids,
            "missing_source_ids": missing_source_ids,
        },
        "warnings": warnings,
    }


def _reader_expected_ids(capture_directory: Path, expected_work_count: int) -> tuple[str, ...]:
    if expected_work_count <= 0:
        raise InventoryError("reader requires a positive expected work count")
    register_path = _find_unique(
        capture_directory / "sources/current-register/mirror",
        "gretil.html",
    )
    if register_path is not None:
        if register_path.stat().st_size > MAX_CATALOGUE_SIZE:
            raise InventoryError("GRETIL register exceeds catalogue safety limit")
        identifiers = extract_inventory(
            register_path.read_text(encoding="utf-8")
        ).tei_ids
    elif expected_work_count == 801:
        fixture = Path(__file__).parent / "fixtures/reviewed-tei-ids.txt"
        try:
            identifiers = tuple(fixture.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError) as exc:
            raise InventoryError(f"cannot read reviewed GRETIL identifiers: {exc}") from exc
    else:
        raise InventoryError("reader capture has no register for its expected inventory")
    if len(identifiers) != expected_work_count or len(set(identifiers)) != len(identifiers):
        raise InventoryError(
            f"reader expected {expected_work_count} unique identifiers, found {len(identifiers)}"
        )
    digest = hashlib.sha256(("\n".join(sorted(identifiers)) + "\n").encode()).hexdigest()
    if expected_work_count == 801 and digest != EXPECTED_TEI_ID_SHA256:
        raise InventoryError("reader identifiers do not match the reviewed register baseline")
    return tuple(sorted(identifiers))


def _reader_record(
    root: ElementTree.Element,
    identifier: str,
    filename_id: str,
    member_path: str,
    route: str,
) -> dict[str, object]:
    title_values = [
        _metadata_text(value)
        for value in root.findall("./tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:title", TEI)
    ]
    author_values = [
        _metadata_text(value)
        for value in root.findall("./tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:author", TEI)
    ]
    languages: list[tuple[str, str]] = []
    for language in root.findall(".//tei:profileDesc/tei:langUsage/tei:language", TEI):
        code = (language.get("ident") or "").strip()
        label = _metadata_text(language)
        value = (code, label)
        if value not in languages and (code or label):
            languages.append(value)
    text = root.find("./tei:text", TEI)
    primary_language = (text.get(XML_LANG) or "").strip() if text is not None else ""
    if not primary_language and languages:
        primary_language = languages[0][0] or languages[0][1]
    terms = [
        value
        for term in root.findall(".//tei:textClass/tei:keywords/tei:term", TEI)
        if (value := _metadata_text(term))
    ]
    return {
        "identifier": identifier,
        "filename_id": filename_id,
        "root_id": root.get(XML_ID, ""),
        "member_path": member_path,
        "route": route,
        "title": next((value for value in title_values if value), identifier),
        "authors": [value for value in author_values if value],
        "languages": languages,
        "primary_language": primary_language or "und",
        "terms": terms,
    }


def _render_reader_index(
    records: Sequence[dict[str, object]],
    capture_id: str,
    package_sha256: str,
) -> str:
    groups: dict[str, list[dict[str, object]]] = {}
    for record in records:
        groups.setdefault(str(record["primary_language"]), []).append(record)
    sections: list[str] = []
    for language, values in sorted(groups.items()):
        entries: list[str] = []
        for record in values:
            authors = "; ".join(str(value) for value in record["authors"])
            author_html = f'<span class="author">{escape_html(authors)}</span>' if authors else ""
            terms = " / ".join(str(value) for value in record["terms"][:3])
            terms_html = f'<span class="terms">{escape_html(terms)}</span>' if terms else ""
            entries.append(
                '<li><a href="{route}"><span class="record-id">{identifier}</span>'
                '<strong>{title}</strong>{author}</a>{terms}</li>'.format(
                    route=escape_html(str(record["route"]), quote=True),
                    identifier=escape_html(str(record["identifier"])),
                    title=escape_html(str(record["title"])),
                    author=author_html,
                    terms=terms_html,
                )
            )
        sections.append(
            f'<section class="language-group"><h2>{escape_html(language)}</h2>'
            f'<ol>{"".join(entries)}</ol></section>'
        )
    return _reader_document(
        "GRETIL Reader",
        f"""
<header class="masthead">
  <p class="eyebrow">Verified local access copy</p>
  <h1>GRETIL</h1>
  <p class="lede">{len(records)} electronic texts in Indian languages, rendered from the
  publisher's preserved TEI package without changing the archive master.</p>
</header>
<main>
  <nav class="utility"><a href="about.html">About this derived reader</a>
  <span>Capture <code>{escape_html(capture_id)}</code></span></nav>
  <aside class="notice">Rights and attribution vary by text. Open a record to review its
  preserved availability statement and source description.</aside>
  {''.join(sections)}
</main>
<footer>Aggregate package SHA-256 <code>{escape_html(package_sha256)}</code></footer>
""",
        "",
    )


def _render_reader_work(
    root: ElementTree.Element,
    record: dict[str, object],
    capture_id: str,
    package_sha256: str,
    previous_route: str | None,
    next_route: str | None,
    previous_id: str | None,
    next_id: str | None,
) -> str:
    navigation = ['<a href="../index.html">Catalogue</a>']
    if previous_route and previous_id:
        navigation.append(
            f'<a href="../{escape_html(previous_route, quote=True)}">&larr; '
            f'{escape_html(previous_id)}</a>'
        )
    if next_route and next_id:
        navigation.append(
            f'<a href="../{escape_html(next_route, quote=True)}">'
            f'{escape_html(next_id)} &rarr;</a>'
        )
    authors = "; ".join(str(value) for value in record["authors"])
    language_values = [
        f"{code} ({label})" if code and label and code != label else code or label
        for code, label in record["languages"]
    ]
    terms = " / ".join(str(value) for value in record["terms"])
    availability = root.find("./tei:teiHeader/tei:fileDesc/tei:publicationStmt/tei:availability", TEI)
    source_description = root.find("./tei:teiHeader/tei:fileDesc/tei:sourceDesc", TEI)
    notes = root.find("./tei:teiHeader/tei:fileDesc/tei:notesStmt", TEI)
    responsibilities = root.findall("./tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:respStmt", TEI)
    text = root.find("./tei:text", TEI)
    text_html = (
        _render_tei_element(text)
        if text is not None
        else '<p class="unavailable">No text element is present in this TEI record.</p>'
    )
    metadata_sections = [
        _reader_details("Availability and rights", availability, open_by_default=True),
        _reader_details("Source description", source_description),
        _reader_details("Editorial and conversion notes", notes),
    ]
    if responsibilities:
        responsibility_html = "".join(_render_tei_element(item) for item in responsibilities)
        metadata_sections.insert(
            1,
            f'<details><summary>Responsibilities</summary><div class="metadata-text">'
            f"{responsibility_html}</div></details>",
        )
    provenance = (
        f'<dl class="provenance"><dt>Register ID</dt><dd><code>{escape_html(str(record["identifier"]))}</code></dd>'
        f'<dt>Filename ID</dt><dd><code>{escape_html(str(record["filename_id"]))}</code></dd>'
        f'<dt>TEI root ID</dt><dd><code>{escape_html(str(record["root_id"]))}</code></dd>'
        f'<dt>ZIP member</dt><dd><code>{escape_html(str(record["member_path"]))}</code></dd>'
        f'<dt>Capture</dt><dd><code>{escape_html(capture_id)}</code></dd></dl>'
    )
    return _reader_document(
        f'{record["identifier"]} - {record["title"]}',
        f"""
<nav class="work-nav">{' '.join(navigation)}</nav>
<header class="record-header">
  <p class="eyebrow">{escape_html(str(record["identifier"]))}</p>
  <h1>{escape_html(str(record["title"]))}</h1>
  {f'<p class="byline">{escape_html(authors)}</p>' if authors else ''}
  <p class="record-meta">{escape_html(' / '.join(language_values))}</p>
  {f'<p class="terms">{escape_html(terms)}</p>' if terms else ''}
</header>
<main class="work-layout">
  <aside class="record-sidebar">
    {provenance}
    {''.join(value for value in metadata_sections if value)}
  </aside>
  <article class="tei-text">{text_html}</article>
</main>
<footer>Preserved aggregate package SHA-256 <code>{escape_html(package_sha256)}</code></footer>
""",
        "../",
    )


def _reader_details(
    label: str,
    element: ElementTree.Element | None,
    *,
    open_by_default: bool = False,
) -> str:
    if element is None:
        return ""
    opened = " open" if open_by_default else ""
    attributes = _tei_attribute_annotations(element, ("status", "type"))
    return (
        f"<details{opened}><summary>{escape_html(label)}</summary>"
        f'<div class="metadata-text">{attributes}{_render_tei_content(element)}</div></details>'
    )


def _render_reader_about(
    capture_id: str,
    package_sha256: str,
    package_report: dict[str, object],
    extra_ids: Sequence[str],
    warnings: Sequence[str],
) -> str:
    warning_items = "".join(f"<li>{escape_html(value)}</li>" for value in warnings)
    extra_items = "".join(f"<li><code>{escape_html(value)}</code></li>" for value in extra_ids)
    return _reader_document(
        "About the GRETIL Reader",
        f"""
<nav class="work-nav"><a href="index.html">Catalogue</a></nav>
<header class="record-header"><p class="eyebrow">Derived access copy</p>
<h1>About this reader</h1></header>
<main class="about">
  <p>This static reader was generated locally from <code>{READER_PACKAGE}</code> in capture
  <code>{escape_html(capture_id)}</code>. The preserved ZIP remains authoritative.</p>
  <dl class="provenance"><dt>Package SHA-256</dt><dd><code>{escape_html(package_sha256)}</code></dd>
  <dt>ZIP entries</dt><dd>{package_report["entry_count"]}</dd>
  <dt>Expanded bytes checked</dt><dd>{package_report["uncompressed_size"]}</dd>
  <dt>CRC check</dt><dd>{'passed' if package_report["crc_ok"] else 'failed'}</dd></dl>
  <h2>Interpretive limits</h2><ul>{warning_items}</ul>
  {f'<h2>Bulk-only records not in the reviewed catalogue</h2><ul>{extra_items}</ul>' if extra_items else ''}
  <p>The reader escapes source text, loads no scripts or remote resources, does not resolve
  external TEI references, and does not alter or extract files into the preservation capture.</p>
</main>
<footer>GRETIL local preservation reader</footer>
""",
        "",
    )


def _metadata_text(element: ElementTree.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _render_tei_content(
    element: ElementTree.Element,
    *,
    inline_blocks: bool = False,
) -> str:
    parts = [_render_tei_text(element.text)]
    for child in element:
        parts.append(_render_tei_element(child, inline_blocks=inline_blocks))
        parts.append(_render_tei_text(child.tail))
    return "".join(parts)


def _render_tei_text(value: str | None) -> str:
    if not value:
        return ""
    if not value.strip():
        return " "
    return escape_html(value)


def _render_tei_element(
    element: ElementTree.Element,
    *,
    inline_blocks: bool = False,
) -> str:
    name = _local_name(element.tag)
    number = element.get("n") or ""
    number_html = escape_html(number)
    content = _render_tei_content(element, inline_blocks=inline_blocks)
    provenance_names: tuple[str, ...] = (XML_ID, "resp", "corresp", "source", "edRef")
    if name not in {"graphic", "licence", "ptr", "ref"}:
        provenance_names += ("target",)
    if name not in {"lem", "rdg"}:
        provenance_names += ("wit",)
    content += _tei_attribute_annotations(element, provenance_names)
    early_rendition = element.get("rend") or ""
    early_annotation = _source_values("rend", [early_rendition] if early_rendition else [])
    if name == "lb":
        marker = f'<span class="line-ref">{number_html}</span>' if number else ""
        return f"<br>{marker}{content}{early_annotation}"
    if name == "pb":
        label = f"page {number}" if number else "page break"
        return (
            f'<span class="page-break">[{escape_html(label)}]'
            f"{content}{early_annotation}</span>"
        )
    if name == "gap":
        reason = element.get("reason") or element.get("extent") or "unspecified"
        return (
            f'<span class="gap">[gap: {escape_html(reason)}]'
            f"{content}{early_annotation}</span>"
        )
    if name in {"milestone", "anchor"}:
        label = number or element.get("unit") or name
        return (
            f'<span class="milestone">[{escape_html(label)}]'
            f"{content}{early_annotation}</span>"
        )
    if name in {"graphic", "ptr"}:
        label = element.get("target") or element.get("url") or name
        return (
            f'<span class="reference">[{escape_html(label)}]'
            f"{content}{early_annotation}</span>"
        )

    language = (element.get(XML_LANG) or "").strip()
    language_attr = f' lang="{escape_html(language, quote=True)}"' if language else ""
    rendition_tokens = [
        token.lower().lstrip("#")
        for token in (element.get("rend") or "").split()
    ]
    rendition_classes = " ".join(
        f"rend-{token}" for token in rendition_tokens if token in READER_RENDITIONS
    )
    rendition_attr = f" {rendition_classes}" if rendition_classes else ""
    unknown_renditions = [
        token
        for token in rendition_tokens
        if token and token not in READER_RENDITIONS
    ]
    unknown_rendition_attr = (
        ' data-tei-rend="{}"'.format(escape_html(" ".join(unknown_renditions), quote=True))
        if unknown_renditions
        else ""
    )
    unknown_rendition_class = " has-source-rendition" if unknown_renditions else ""
    source_classes = f"{rendition_attr}{unknown_rendition_class}"
    source_attributes = f"{language_attr}{unknown_rendition_attr}"

    block_names = {
        "back", "bibl", "biblStruct", "body", "cell", "cit", "closer", "div",
        "front", "head", "item", "l", "lg", "list", "listBibl", "opener", "p",
        "quote", "row", "sp", "speaker", "table", "text", "trailer",
    }
    if inline_blocks and name in block_names:
        return (
            f'<span class="tei-inline-block tei-{escape_html(name, quote=True)}{source_classes}"'
            f"{source_attributes}>"
            f"{content}</span>"
        )

    if name == "head":
        return f'<h3 class="tei-head{source_classes}"{source_attributes}>{content}</h3>'
    if name in {"text", "front", "body", "back"}:
        return (
            f'<section class="tei-{name}{source_classes}"{source_attributes}>'
            f"{content}</section>"
        )
    if name == "div":
        label = element.get("type") or number
        label_html = f'<p class="division-label">{escape_html(label)}</p>' if label else ""
        return (
            f'<section class="division{source_classes}"{source_attributes}>'
            f"{label_html}{content}</section>"
        )
    if name == "p":
        return (
            f'<p class="paragraph{source_classes}"{source_attributes}>{content}</p>'
        )
    if name == "lg":
        label = element.get("type") or number
        label_html = f'<p class="line-group-label">{escape_html(label)}</p>' if label else ""
        return (
            f'<section class="line-group{source_classes}"{source_attributes}>'
            f"{label_html}{content}</section>"
        )
    if name == "l":
        label = f'<span class="line-number">{number_html}</span>' if number else ""
        return (
            f'<div class="line{source_classes}"{source_attributes}>'
            f"{label}<span>{content}</span></div>"
        )
    if name in {"list", "listBibl"}:
        return f'<ul class="tei-list{source_classes}"{source_attributes}>{content}</ul>'
    if name == "item":
        return f'<li class="tei-item{source_classes}"{source_attributes}>{content}</li>'
    if name == "table":
        return (
            f'<table class="tei-table{source_classes}"{source_attributes}>'
            f"<tbody>{content}</tbody></table>"
        )
    if name == "row":
        return f'<tr class="tei-row{source_classes}"{source_attributes}>{content}</tr>'
    if name == "cell":
        return f'<td class="tei-cell{source_classes}"{source_attributes}>{content}</td>'
    if name in {"bibl", "biblStruct", "cit", "sp", "opener", "closer", "trailer"}:
        return f'<div class="tei-{name}{source_classes}"{source_attributes}>{content}</div>'
    if name == "speaker":
        return f'<strong class="speaker{source_classes}"{source_attributes}>{content}</strong>'
    if name == "quote":
        return f'<blockquote class="tei-quote{source_classes}"{source_attributes}>{content}</blockquote>'
    if name == "note":
        note_content = _render_tei_content(element, inline_blocks=True)
        note_content += _tei_attribute_annotations(element, provenance_names)
        return f'<span class="note{source_classes}"{source_attributes}>[note: {note_content}]</span>'
    if name == "app":
        return f'<span class="apparatus{source_classes}"{source_attributes}>{content}</span>'
    if name in {"lem", "rdg"}:
        witness = element.get("wit") or ""
        witness_html = f'<small>{escape_html(witness)}</small>' if witness else ""
        return (
            f'<span class="{name}{source_classes}"{source_attributes}>'
            f"{content}{witness_html}</span>"
        )
    if name == "ref":
        target = element.get("target") or ""
        reference_type = element.get("type") or ""
        reference_label = f"{reference_type} target" if reference_type else "target"
        target_html = (
            f'<small class="source-target">[{escape_html(reference_label)}: '
            f'{escape_html(target)}]</small>'
            if target
            else ""
        )
        return (
            f'<span class="reference{source_classes}"{source_attributes}>'
            f"{content}{target_html}</span>"
        )
    if name == "licence":
        target = element.get("target") or ""
        target_html = (
            f'<small class="source-target">[licence target: {escape_html(target)}]</small>'
            if target
            else ""
        )
        return (
            f'<span class="tei-licence{source_classes}"{source_attributes}>'
            f"{content}{target_html}</span>"
        )
    if name == "date":
        values = [
            value
            for key in ("when-iso", "when", "from-iso", "to-iso")
            if (value := element.get(key))
        ]
        visible_text = "".join(element.itertext())
        missing_values = [value for value in values if value not in visible_text]
        value_html = _source_values("date", missing_values)
        return (
            f'<span class="tei-date{source_classes}"{source_attributes}>'
            f"{content}{value_html}</span>"
        )
    if name == "biblScope":
        return (
            f'<span class="tei-biblScope{source_classes}"{source_attributes}>{content}'
            f'{_tei_attribute_annotations(element, ("unit", "from", "to"))}</span>'
        )
    if name in {"idno", "witness"}:
        return (
            f'<span class="tei-{name}{source_classes}"{source_attributes}>{content}'
            f'{_tei_attribute_annotations(element, ("type", XML_ID))}</span>'
        )
    if name == "title":
        return (
            f'<span class="tei-title{source_classes}"{source_attributes}>{content}'
            f'{_tei_attribute_annotations(element, ("level", "type"))}</span>'
        )
    if name in {
        "abbr", "add", "author", "choice", "corr", "del", "emph",
        "foreign", "hi", "mentioned", "name", "num", "orig", "q", "reg",
        "rs", "seg", "sic", "soCalled", "supplied", "surplus", "term",
        "unclear", "w",
    }:
        return (
            f'<span class="tei-{name}{source_classes}"{source_attributes}>{content}</span>'
        )
    if name in {"formula", "code"}:
        return f'<code class="tei-{name}{source_classes}"{source_attributes}>{content}</code>'
    if not content:
        return ""
    return (
        f'<span class="tei-unknown{source_classes}" title="TEI {escape_html(name, quote=True)}"'
        f"{source_attributes}>{content}</span>"
    )


def _tei_attribute_annotations(
    element: ElementTree.Element,
    names: Sequence[str],
) -> str:
    values: list[str] = []
    for name in names:
        value = element.get(name)
        if value:
            label = "xml:id" if name == XML_ID else name
            values.append(f"{label}: {value}")
    return _source_values("source", values)


def _source_values(label: str, values: Sequence[str]) -> str:
    if not values:
        return ""
    return (
        f'<small class="source-target">[{escape_html(label)}: '
        f'{escape_html("; ".join(values))}]</small>'
    )


def _reader_document(title: str, content: str, asset_prefix: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'">
<title>{escape_html(title)}</title>
<link rel="stylesheet" href="{asset_prefix}assets/reader.css">
</head>
<body>{content}</body>
</html>
"""


def _reader_stylesheet() -> str:
    return """:root{color-scheme:light;--ink:#211d17;--muted:#70685d;--paper:#eee9dc;
--sheet:#fffdf6;--rule:#c8bea9;--saffron:#a9491c;--indigo:#243f58;--note:#f2e2b9}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);
background:linear-gradient(90deg,#e3ddce 0,transparent 12%,transparent 88%,#e3ddce 100%),var(--paper);
font:17px/1.65 Georgia,"Times New Roman",serif}a{color:var(--indigo);text-underline-offset:.16em}
code,.record-id,.line-number,.line-ref{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
code{overflow-wrap:anywhere}.masthead,.record-header{padding:4rem max(5vw,1.2rem) 2.6rem;
border-bottom:1px solid var(--rule);background:var(--sheet)}h1,h2,h3{line-height:1.12;font-weight:600}
h1{max-width:25ch;margin:.15rem 0;font-size:clamp(2.4rem,6vw,5.4rem)}.eyebrow{margin:0;
color:var(--saffron);font:700 .74rem/1.2 ui-monospace,monospace;letter-spacing:.15em;text-transform:uppercase}
.lede{max-width:62ch;color:var(--muted);font-size:1.16rem}.utility,.work-nav{display:flex;flex-wrap:wrap;
gap:1rem 2rem;padding:1rem max(5vw,1.2rem);border-bottom:1px solid var(--rule);background:#faf6ea}
main,footer{width:min(1160px,92vw);margin:2rem auto}.notice{padding:1rem 1.2rem;border-left:4px solid var(--saffron);
background:var(--sheet)}.language-group{margin:3rem 0}.language-group>h2{padding-bottom:.5rem;border-bottom:3px double var(--rule)}
.language-group ol{list-style:none;padding:0}.language-group li{display:grid;grid-template-columns:minmax(0,1fr) auto;
gap:1rem;padding:.8rem 0;border-bottom:1px solid var(--rule)}.language-group a{display:grid;
grid-template-columns:minmax(11rem,18rem) minmax(0,1fr);gap:.75rem;text-decoration:none}.record-id{color:var(--saffron);
font-size:.78rem;overflow-wrap:anywhere}.author,.terms{display:block;color:var(--muted);font-size:.84rem}.terms{align-self:center}
.byline{font-size:1.2rem}.record-meta{color:var(--muted)}.work-layout{display:grid;grid-template-columns:minmax(16rem,25rem) minmax(0,1fr);
gap:2rem;align-items:start}.record-sidebar{position:sticky;top:1rem;max-height:calc(100vh - 2rem);overflow:auto;
padding:1.2rem;border:1px solid var(--rule);background:#f8f3e6;font-size:.85rem}.provenance{display:grid;
grid-template-columns:max-content minmax(0,1fr);gap:.35rem .8rem}.provenance dt{font-weight:700}.provenance dd{margin:0}
details{margin:1rem 0;border-top:1px solid var(--rule);padding-top:.65rem}summary{cursor:pointer;font-weight:700}
.metadata-text{margin-top:.7rem}.tei-text{min-width:0;padding:clamp(1rem,3vw,2.5rem);border:1px solid var(--rule);
background:var(--sheet);box-shadow:0 1rem 2.5rem #51462c18}.division+.division{margin-top:2.5rem}.division-label,.line-group-label{
color:var(--saffron);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em}.paragraph{margin:1rem 0}
.line-group{margin:1.4rem 0}.line{display:grid;grid-template-columns:4.5rem minmax(0,1fr);gap:.8rem;margin:.22rem 0}
.line-number{color:var(--muted);font-size:.72rem;text-align:right}.line-ref{color:var(--muted);font-size:.65rem}
.page-break{display:block;margin:1.4rem 0 .5rem;color:var(--muted);font-size:.72rem}.note{background:var(--note);font-size:.88em}
.gap,.milestone{color:var(--saffron)}.reference{border-bottom:1px dotted var(--muted)}.apparatus{border-bottom:1px dashed var(--saffron)}
.lem:before{content:"lem. ";color:var(--muted);font-size:.72rem}.rdg:before{content:" rdg. ";color:var(--muted);font-size:.72rem}
.rdg small,.lem small{margin-left:.3rem;color:var(--muted)}.tei-foreign,.tei-emph,.rend-it,.rend-italic{font-style:italic}
.rend-bold{font-weight:700}.rend-underline{text-decoration:underline}.rend-red{color:#8d241c}.rend-blue{color:#1c4d78}
.rend-green{color:#35653a}.rend-center{text-align:center}.rend-small{font-size:.85em}.rend-sup{vertical-align:super;font-size:.75em}
.rend-sub{vertical-align:sub;font-size:.75em}.tei-supplied:before{content:"["}.tei-supplied:after{content:"]"}
.tei-unclear{text-decoration:underline dotted}.tei-del,.tei-surplus{text-decoration:line-through}.tei-orig,.tei-sic{color:#6d4226}
.has-source-rendition:after{content:" [rend: " attr(data-tei-rend) "]";color:var(--muted);font:normal .68rem/1.3 ui-monospace,monospace}
.tei-inline-block{display:block;margin:.35rem 0}
.tei-table{border-collapse:collapse;max-width:100%;overflow:auto}.tei-table td{padding:.35rem;border:1px solid var(--rule)}
.tei-bibl,.tei-biblStruct,.tei-cit{margin:.7rem 0;padding-left:1rem;border-left:2px solid var(--rule)}
.source-target{display:inline-block;margin-left:.35rem;color:var(--muted);font:normal .72rem/1.4 ui-monospace,monospace}
.tei-unknown{outline:1px dotted transparent}.speaker{display:block;margin-top:1rem}.about{max-width:780px}
blockquote{margin:1rem 2rem;padding-left:1rem;border-left:3px solid var(--rule)}footer{padding:1rem 0 3rem;border-top:1px solid var(--rule);
color:var(--muted);font-size:.76rem}@media(max-width:850px){.work-layout{grid-template-columns:1fr}.record-sidebar{position:static;max-height:none}
.language-group li,.language-group a{grid-template-columns:1fr}.line{grid-template-columns:3rem minmax(0,1fr)}}
@media print{body{background:#fff}.work-nav,.record-sidebar{display:none}.work-layout{display:block}.tei-text{border:0;box-shadow:none;padding:0}}
"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_first_party_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return None
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host not in FIRST_PARTY_HOSTS or parsed.scheme not in {"http", "https"}:
        return None
    return urlunsplit(("https", PRIMARY_HOST, parsed.path or "/", parsed.query, ""))


def _find_unique(root: Path, name: str) -> Path | None:
    if not root.is_dir() or root.is_symlink():
        return None
    matches = [
        path
        for path in root.rglob(name)
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) > 1:
        raise InventoryError(f"multiple captured files named {name!r}")
    return matches[0] if matches else None


def _named_files(root: Path, suffix: str | None) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    result: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if suffix is not None and not path.name.endswith(suffix):
            continue
        if path.name in result:
            raise InventoryError(f"duplicate captured filename: {path.name}")
        result[path.name] = path
    return result


def _analyze_zip(path: Path) -> dict[str, object]:
    if path.stat().st_size > MAX_COMPRESSED_ARCHIVE_SIZE:
        raise InventoryError(
            f"archive is {path.stat().st_size} bytes, above compressed-size safety limit"
        )
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise InventoryError(
                f"archive has {len(infos)} entries, above safety limit {MAX_ARCHIVE_ENTRIES}"
            )
        names = [info.filename for info in infos]
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        unsafe = sorted(
            info.filename
            for info in infos
            if PurePosixPath(info.filename).is_absolute()
            or ".." in PurePosixPath(info.filename).parts
            or "\\" in info.filename
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
        failures = (
            ("duplicate archive path", duplicates),
            ("unsafe archive path", unsafe),
            ("archive symlink", symlinks),
            ("encrypted archive entry", encrypted),
            ("oversized archive entry", oversized),
            ("excessive compression ratio", excessive_ratio),
        )
        for label, values in failures:
            if values:
                raise InventoryError(f"{label}: {values[0]}")
        if total_size > MAX_ARCHIVE_SIZE:
            raise InventoryError(f"archive expands to {total_size} bytes, above safety limit")
        bad_crc = archive.testzip()
        if bad_crc is not None:
            raise InventoryError(f"archive CRC failure: {bad_crc}")
        tei_ids: set[str] = set()
        tei_root_ids: dict[str, str] = {}
        for name, info in zip(names, infos, strict=True):
            match = BULK_TEI_RE.search(name)
            if match is None:
                continue
            filename_id = match.group("id")
            register_id, expected_root_id = BULK_TEI_MAPPINGS.get(
                filename_id,
                (
                    filename_id,
                    BULK_TEI_ROOT_ID_EXCEPTIONS.get(filename_id, filename_id),
                ),
            )
            root_id = _validate_tei_member(archive, info)
            if root_id != expected_root_id:
                raise InventoryError(
                    f"TEI root ID does not match reviewed mapping: {name}"
                )
            tei_ids.add(register_id)
            tei_root_ids[filename_id] = root_id
    return {
        "name": path.name,
        "compressed_size": path.stat().st_size,
        "entry_count": len(infos),
        "uncompressed_size": total_size,
        "crc_ok": True,
        "tei_ids": sorted(tei_ids),
        "tei_root_ids": tei_root_ids,
    }


def _validate_tei_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    root_tag: str | None = None
    root_id: str | None = None
    try:
        with archive.open(info) as stream:
            for event, element in ElementTree.iterparse(stream, events=("start", "end")):
                if root_tag is None and event == "start":
                    root_tag = element.tag
                    root_id = element.get(XML_ID)
                if event == "end":
                    element.clear()
    except ElementTree.ParseError as exc:
        raise InventoryError(f"invalid TEI XML member {info.filename}: {exc}") from exc
    if root_tag != TEI_ROOT:
        raise InventoryError(f"archive member is not a TEI document: {info.filename}")
    if not root_id:
        raise InventoryError(f"TEI root has no xml:id: {info.filename}")
    return root_id


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InventoryError(f"JSON document is not an object: {path}")
    return value
