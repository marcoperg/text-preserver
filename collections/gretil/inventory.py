"""Extract and validate the publisher-manifested GRETIL corpus."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
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
