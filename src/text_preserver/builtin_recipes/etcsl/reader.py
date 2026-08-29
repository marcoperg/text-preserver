"""Render a static reader from a captured ETCSL deposit."""

from __future__ import annotations

from html import escape as escape_html
from html.entities import html5 as HTML5_ENTITIES
from pathlib import Path
import re
from typing import Sequence
import xml.etree.ElementTree as ElementTree
import zipfile

from text_preserver.adapters import ReaderContext, ReaderReport
from text_preserver.access.reader_model import (
    AccessArtifact,
    AccessCollection,
    AccessFacet,
    AccessItem,
    AccessRelation,
    AccessRepresentation,
    AccessSegment,
    access_id,
    access_json,
    route_token,
)
from text_preserver.access.reader_shell import (
    ReaderFact,
    ReaderFacet,
    ReaderLink,
    reader_stylesheet,
    render_artifact_reference,
    render_citation,
    render_document,
    render_facts,
    render_facets,
    render_navigation,
    render_notice,
    render_status,
)

from .validator import (
    BUILTIN_ENTITIES,
    Composition,
    ENTITY_REF_RE,
    InventoryError,
    KNOWN_UNTRANSLATED,
    XML_ENTRY_RE,
    _analyze_zip,
    _find_ota_package,
    _find_zip,
    _ota_support_files,
    _read_zip_text,
    _sha256,
    extract_inventory,
)


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
TITLE_SUFFIXES = (
    " -- a composite transliteration",
    " -- an English prose translation",
)
COLLECTION_RIGHTS = (
    "Public access does not imply permission to redistribute captures; preserve the site's notices and repository metadata."
)
CATALOGUE_CAVEAT = (
    "ETCSL states that this thematic arrangement reflects modern perceptions and may "
    "suggest misleading relationships between compositions or genres."
)


def build_reader(context: ReaderContext) -> ReaderReport:
    """Build the existing ETCSL reader through the recipe API 2 contract."""
    payload = write_static_reader(
        context.capture_directory,
        output_directory=context.output_directory,
        expected_work_count=context.expected_work_count,
    )
    return ReaderReport(
        payload["status"],
        payload["summary"],
        tuple(payload["warnings"]),
    )


def write_static_reader(
    capture_directory: Path,
    *,
    output_directory: Path,
    expected_work_count: int,
) -> dict[str, object]:
    """Write the reader within the adapter output sandbox and return a bounded report."""
    payload = render_static_reader(
        capture_directory,
        expected_work_count=expected_work_count,
    )
    files = payload["files"]
    if not isinstance(files, dict):
        raise InventoryError("ETCSL reader produced invalid files")
    output_directory.mkdir(parents=True, exist_ok=True)
    for relative, source in files.items():
        if not isinstance(relative, str) or not isinstance(source, str):
            raise InventoryError("ETCSL reader produced invalid output files")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise InventoryError("ETCSL reader produced an unsafe output path")
        destination = output_directory / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
    return {
        "status": payload["status"],
        "summary": payload["summary"],
        "warnings": payload["warnings"],
    }


def render_static_reader(
    capture_directory: Path,
    *,
    expected_work_count: int,
) -> dict[str, object]:
    """Render an inert local catalogue and composition pages from the ETCSL deposit."""
    dataset_root = capture_directory / "sources/ota-dataset/mirror"
    package_files, _package_errors = _find_ota_package(dataset_root)
    archive_path = package_files.get("etcsl.zip") or _find_zip(dataset_root)
    if archive_path is None:
        raise InventoryError("captured ETCSL deposit ZIP was not found")
    archive_report = _analyze_zip(
        archive_path,
        expected_work_count,
        support_files=_ota_support_files(package_files),
    )
    catalogue_path = package_files.get("etcslfullcat.html")
    catalogue: tuple[Composition, ...] = ()
    catalogue_sha256: str | None = None
    classification_warnings: list[str] = []
    if catalogue_path is None:
        classification_warnings.append(
            "captured ETCSL catalogue is unavailable; human-readable classifications are omitted"
        )
    else:
        try:
            catalogue = extract_inventory(catalogue_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise InventoryError(f"cannot read captured ETCSL catalogue: {exc}") from exc
        catalogue_sha256 = _sha256(catalogue_path)
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

    catalogue_ids = {composition.id for composition in catalogue}
    for composition in catalogue:
        record = records.get(composition.id)
        if record is not None:
            record["catalogue_path"] = composition.catalogue_path
    identifiers = [composition.id for composition in catalogue if composition.id in records]
    identifiers.extend(
        sorted(set(records) - set(identifiers), key=_identifier_sort_key)
    )
    if not catalogue:
        identifiers = sorted(records, key=_identifier_sort_key)
    if not identifiers:
        raise InventoryError("ETCSL deposit contains no text XML files")
    files: dict[str, str] = {
        "assets/reader.css": reader_stylesheet(),
        "assets/etcsl.css": _etcsl_stylesheet(),
    }
    collection_access_id = access_id("etcsl", "collection", "")
    package_artifact_id = access_id("etcsl", "artifact", "ota-package")
    archive_capture_path = archive_path.relative_to(capture_directory).as_posix()
    artifacts = [
        AccessArtifact(
            package_artifact_id,
            "Canonical ETCSL deposit ZIP",
            "preservation_original",
            archive_capture_path,
            "application/zip",
            str(archive_report["sha256"]),
        )
    ]
    catalogue_artifact_id: str | None = None
    if catalogue_path is not None and catalogue_sha256 is not None:
        catalogue_artifact_id = access_id("etcsl", "artifact", "ota-catalogue")
        artifacts.append(
            AccessArtifact(
                catalogue_artifact_id,
                "Deposited ETCSL catalogue",
                "preservation_original",
                catalogue_path.relative_to(capture_directory).as_posix(),
                "text/html",
                catalogue_sha256,
            )
        )
    access_items: list[AccessItem] = []
    relations: list[AccessRelation] = []
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
        page, item, item_artifacts, item_relations = _render_work_page(
            identifier,
            titles[identifier],
            records[identifier],
            capture_directory.name,
            archive_report["sha256"],
            previous_id,
            next_id,
            archive_capture_path,
            package_artifact_id,
            catalogue_path=_catalogue_path(records[identifier]),
            catalogue_artifact_id=catalogue_artifact_id,
        )
        files[f"works/{identifier}.html"] = page
        access_items.append(item)
        artifacts.extend(item_artifacts)
        relations.append(AccessRelation(item.id, "part_of", collection_access_id))
        relations.extend(item_relations)
    warnings = [str(value) for value in archive_report["errors"]]
    warnings.extend(str(value) for value in archive_report["warnings"])
    warnings.extend(classification_warnings)
    reader_transliteration_ids = set(archive_report["transliteration_ids"])
    missing_catalogue_ids = sorted(reader_transliteration_ids - catalogue_ids)
    unavailable_catalogue_ids = sorted(catalogue_ids - reader_transliteration_ids)
    if missing_catalogue_ids:
        warnings.append(
            f"{len(missing_catalogue_ids)} reader compositions have no deposited catalogue classification"
        )
    if unavailable_catalogue_ids:
        warnings.append(
            f"{len(unavailable_catalogue_ids)} deposited catalogue compositions have no reader transliteration"
        )
    classified_ids = {
        composition.id for composition in catalogue if composition.catalogue_path
    }
    if catalogue and classified_ids != catalogue_ids:
        warnings.append(
            f"{len(catalogue_ids - classified_ids)} catalogue compositions have no source classification path"
        )
    if unresolved_entities:
        warnings.append(
            f"{len(unresolved_entities)} named entities are displayed as source tokens"
        )
    warnings.append(
        "first reader version omits bibliography linking and advanced stand-off annotations"
    )
    reader_translation_ids = set(archive_report["translation_ids"])
    catalogue_inventory_complete = (
        catalogue_path is not None and catalogue_ids == reader_transliteration_ids
    )
    classification_paths_complete = (
        not classified_ids or classified_ids == catalogue_ids
    )
    inventory_complete = (
        len(reader_transliteration_ids) == expected_work_count
        and reader_translation_ids == reader_transliteration_ids - KNOWN_UNTRANSLATED
        and not archive_report["missing_transliteration_counterparts"]
        and archive_report["crc_ok"]
        and archive_report["entity_stubbed_xml_parse_count"]
        == archive_report["xml_file_count"]
        and archive_report["filename_id_match_count"] == archive_report["xml_file_count"]
        and catalogue_inventory_complete
        and classification_paths_complete
    )
    status = (
        "incomplete"
        if not inventory_complete
        else ("complete_with_warnings" if warnings else "complete")
    )
    files["index.html"] = _render_reader_index(
        identifiers,
        titles,
        records,
        capture_directory.name,
        archive_report["sha256"],
        status,
        warnings,
        package_artifact_id,
        catalogue_artifact_id,
    )
    files["access.json"] = access_json(
        AccessCollection(
            collection_access_id,
            "Electronic Text Corpus of Sumerian Literature",
            status,
            "index.html",
            tuple(access_items),
            tuple(artifacts),
            tuple(relations),
            (COLLECTION_RIGHTS,),
        )
    )
    return {
        "status": status,
        "files": files,
        "summary": {
            "work_count": len(records),
            "transliteration_count": sum(
                "transliteration" in record for record in records.values()
            ),
            "translation_count": sum("translation" in record for record in records.values()),
            "unresolved_entity_names": sorted(unresolved_entities),
            "archive_sha256": archive_report["sha256"],
            "catalogue_sha256": catalogue_sha256,
            "classified_work_count": len(classified_ids & set(records)),
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


def _catalogue_path(record: dict[str, object]) -> tuple[str, ...]:
    value = record.get("catalogue_path")
    if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
        return value
    return ()


def _render_reader_index(
    identifiers: Sequence[str],
    titles: dict[str, str],
    records: dict[str, dict[str, object]],
    capture_id: str,
    archive_sha256: object,
    status: str,
    warnings: Sequence[str],
    package_artifact_id: str,
    catalogue_artifact_id: str | None,
) -> str:
    entries: list[str] = []
    current_group: str | None = None
    for identifier in identifiers:
        path = _catalogue_path(records[identifier])
        group = path[0] if path else f"Catalogue group {identifier.split('.', 1)[0]}"
        if group != current_group:
            if current_group is not None:
                entries.append("</ol></section>")
            entries.append(
                f'<section class="catalogue-group"><h2>{escape_html(group)}</h2><ol>'
            )
            current_group = group
        record = records[identifier]
        representations: list[str] = []
        if "transliteration" in record:
            representations.append("transliteration")
        if "translation" in record:
            representations.append("translation")
        path_html = (
            '<span class="catalogue-path">{}</span>'.format(
                escape_html(" › ".join(path[1:]))
            )
            if len(path) > 1
            else ""
        )
        entries.append(
            '<li><a href="works/{identifier}.html"><span class="work-id">{identifier}</span> '
            '<span class="work-title">{title}</span>{path}</a>'
            ' <span class="representations">{representations}</span></li>'.format(
                identifier=escape_html(identifier, quote=True),
                title=escape_html(titles[identifier]),
                path=path_html,
                representations=escape_html(" / ".join(representations)),
            )
        )
    if current_group is not None:
        entries.append("</ol></section>")
    content = "".join(entries)
    return render_document(
        "ETCSL Reader",
        f"""
<header class="reader-header">
  <p class="reader-eyebrow">Derived access copy</p>
  <h1>Electronic Text Corpus of Sumerian Literature</h1>
  <p class="reader-lede">A local catalogue reconstructed from the verified canonical XML deposit.</p>
</header>
<main class="reader-main">
  {render_notice("This is not the original ETCSL website. ETCSL character, determinative, subscript, and editorial entities are rendered from the corpus support declarations; any unknown entity remains visible as its source token.")}
  {render_notice(CATALOGUE_CAVEAT) if any(_catalogue_path(records[identifier]) for identifier in identifiers) else ""}
  {render_notice(COLLECTION_RIGHTS)}
  {render_status(status, warnings)}
  <div class="catalogue-meta"><span>{len(identifiers)} compositions</span>
  <span>Capture <code>{escape_html(capture_id)}</code></span></div>
  {content}
</main>
<footer class="reader-footer">Canonical archive SHA-256: <code>{escape_html(str(archive_sha256))}</code>
 {render_artifact_reference("Machine-readable source artifact", package_artifact_id, "access.json")}
 {render_artifact_reference("Catalogue classification source", catalogue_artifact_id, "access.json") if catalogue_artifact_id else ""}</footer>
""",
        collection_stylesheet="etcsl.css",
    )


def _render_work_page(
    identifier: str,
    title: str,
    record: dict[str, object],
    capture_id: str,
    archive_sha256: object,
    previous_id: str | None,
    next_id: str | None,
    archive_capture_path: str,
    package_artifact_id: str,
    *,
    catalogue_path: tuple[str, ...] = (),
    catalogue_artifact_id: str | None = None,
) -> tuple[str, AccessItem, tuple[AccessArtifact, ...], tuple[AccessRelation, ...]]:
    navigation = render_navigation(
        (ReaderLink("ETCSL catalogue", "../index.html"),),
        previous=(
            ReaderLink(previous_id, f"{previous_id}.html")
            if previous_id is not None
            else None
        ),
        next_=(ReaderLink(next_id, f"{next_id}.html") if next_id is not None else None),
    )
    columns: list[str] = []
    representations: list[AccessRepresentation] = []
    artifacts: list[AccessArtifact] = []
    route = f"works/{identifier}.html"
    item_id = access_id("etcsl", "item", identifier)
    for key, heading in (
        ("transliteration", "Composite transliteration"),
        ("translation", "English prose translation"),
    ):
        root = record.get(key)
        if isinstance(root, ElementTree.Element):
            member_path = str(record[f"{key}_path"])
            source_path = escape_html(member_path)
            language = "sux-Latn" if key == "transliteration" else "en"
            artifact_id = access_id("etcsl", "artifact", member_path)
            representation_id = access_id("etcsl", "representation", f"{identifier}/{key}")
            body, segments = _render_text_bodies(
                root,
                identifier=identifier,
                representation=key,
                route=route,
            )
            artifacts.append(
                AccessArtifact(
                    artifact_id,
                    f"ETCSL {heading} XML",
                    "preservation_original",
                    archive_capture_path,
                    "application/xml",
                    container_id=package_artifact_id,
                    member_path=member_path,
                )
            )
            representations.append(
                AccessRepresentation(
                    representation_id,
                    heading,
                    key,
                    language,
                    f"{route}#representation-{key}",
                    (artifact_id,),
                    segments,
                )
            )
            columns.append(
                f'<article id="representation-{key}" class="representation" lang="{language}"><header><h2>{heading}</h2>'
                f'<p class="source-path">{source_path}</p>'
                f'{render_artifact_reference("Source artifact", artifact_id, "../access.json")}'
                f"</header>{body}</article>"
            )
        else:
            columns.append(
                f'<article id="representation-{key}" class="representation unavailable"><h2>{heading}</h2>'
                f"<p>No {escape_html(key)} is present in the canonical deposit.</p></article>"
            )
    provenance = render_facts(
        (
            ReaderFact("Capture", capture_id),
            ReaderFact("Archive SHA-256", str(archive_sha256)),
        )
    )
    relations: list[AccessRelation] = []
    by_kind = {value.kind: value for value in representations}
    if "translation" in by_kind and "transliteration" in by_kind:
        relations.append(
            AccessRelation(
                by_kind["translation"].id,
                "translation_of",
                by_kind["transliteration"].id,
            )
        )
    citation_text = f"ETCSL {identifier}, {title}. Derived from capture {capture_id}."
    access_facets: list[AccessFacet] = []
    if catalogue_path:
        access_facets.append(
            AccessFacet(
                "catalogue_path",
                "ETCSL catalogue path",
                (" › ".join(catalogue_path),),
                ((catalogue_artifact_id,) if catalogue_artifact_id else ()),
                CATALOGUE_CAVEAT,
            )
        )
    item = AccessItem(
        item_id,
        title,
        "composition",
        route,
        citation_text,
        tuple(representations),
        facets=tuple(access_facets),
    )
    citation = render_citation(citation_text, item_id)
    classification = render_facets(
        (
            ReaderFacet(
                "ETCSL catalogue path",
                (" › ".join(catalogue_path),),
                CATALOGUE_CAVEAT,
            ),
        )
        if catalogue_path
        else ()
    )
    classification_source = (
        render_artifact_reference(
            "Catalogue classification source", catalogue_artifact_id, "../access.json"
        )
        if catalogue_path and catalogue_artifact_id is not None
        else ""
    )
    page = render_document(
        f"{identifier} {title}",
        f"""
{navigation}
<header class="reader-header"><p class="reader-eyebrow">ETCSL {escape_html(identifier)}</p>
<h1>{escape_html(title)}</h1>{classification}{classification_source}</header>
<main class="reader-main"><div class="parallel-text">{''.join(columns)}</div>{citation}</main>
<footer class="reader-footer">{provenance}</footer>
""",
        asset_prefix="../",
        collection_stylesheet="etcsl.css",
    )
    return page, item, tuple(artifacts), tuple(relations)


def _render_text_bodies(
    root: ElementTree.Element,
    *,
    identifier: str,
    representation: str,
    route: str,
) -> tuple[str, tuple[AccessSegment, ...]]:
    sections: list[str] = []
    segments: list[AccessSegment] = []
    used_segments: dict[str, int] = {}
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
                f'<section class="text-body">{heading_html}'
                f'{_render_blocks(body, identifier, representation, route, used_segments, segments)}'
                "</section>"
            )
    content = "".join(sections) or '<p class="unavailable">No text body was found.</p>'
    return content, tuple(segments)


def _render_blocks(
    element: ElementTree.Element,
    identifier: str,
    representation: str,
    route: str,
    used_segments: dict[str, int],
    segments: list[AccessSegment],
) -> str:
    parts: list[str] = []
    if element.text and element.text.strip():
        parts.append(f"<p>{_render_reader_text(element.text.strip())}</p>")
    for child in element:
        tag = child.tag
        if tag == "head":
            parts.append(f"<h4>{_render_inline_content(child)}</h4>")
        elif tag == "l":
            label = escape_html(child.get("n", ""))
            anchor = _segment_anchor(
                identifier,
                representation,
                "line",
                child.get("n", ""),
                route,
                used_segments,
                segments,
            )
            parts.append(
                f'<div{anchor} class="line"><span class="line-number">{label}</span>'
                f'<span class="line-text">{_render_inline_content(child)}</span></div>'
            )
        elif tag == "p":
            label = escape_html(child.get("n", ""))
            anchor = _segment_anchor(
                identifier,
                representation,
                "paragraph",
                child.get("n", ""),
                route,
                used_segments,
                segments,
            )
            parts.append(
                f'<p{anchor} class="paragraph"><span class="line-number">{label}</span>'
                f"{_render_inline_content(child)}</p>"
            )
        elif tag == "div1":
            label = child.get("n") or child.get("type") or "Section"
            parts.append(
                f'<section class="segment"><h4>{escape_html(label)}</h4>'
                f"{_render_blocks(child, identifier, representation, route, used_segments, segments)}</section>"
            )
        elif tag == "lg":
            label = child.get("type")
            heading = f'<h4>{escape_html(label)}</h4>' if label else ""
            parts.append(
                f'<section class="line-group">{heading}'
                f"{_render_blocks(child, identifier, representation, route, used_segments, segments)}"
                "</section>"
            )
        elif tag == "trailer":
            parts.append(
                f'<aside class="trailer">'
                f"{_render_blocks(child, identifier, representation, route, used_segments, segments)}"
                "</aside>"
            )
        else:
            content = _render_inline_element(child)
            if content.strip():
                parts.append(f'<div class="annotation-block">{content}</div>')
        if child.tail and child.tail.strip():
            parts.append(f"<p>{_render_reader_text(child.tail.strip())}</p>")
    return "".join(parts)


def _segment_anchor(
    identifier: str,
    representation: str,
    kind: str,
    label: str,
    route: str,
    used: dict[str, int],
    segments: list[AccessSegment],
) -> str:
    if not label:
        return ""
    key = f"{kind}:{label}"
    occurrence = used.get(key, 0) + 1
    used[key] = occurrence
    anchor = (
        f"segment-{route_token(representation)}-{route_token(kind)}-"
        f"{route_token(label)}--occurrence-{occurrence}"
    )
    segment_id = access_id(
        "etcsl",
        "segment",
        f"{identifier}/{representation}/{kind}/{label}/{occurrence}",
    )
    segments.append(AccessSegment(segment_id, f"{kind.title()} {label}", f"{route}#{anchor}"))
    return f' id="{anchor}"'


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


def _etcsl_stylesheet() -> str:
    return """:root{--reader-paper:#f4efe3;--reader-sheet:#fffdf7;--reader-rule:#c7bda9;
--reader-accent:#8b321f;--reader-link:#234f61}.catalogue-meta{display:flex;flex-wrap:wrap;
gap:1rem 2rem;color:var(--reader-muted)}.catalogue-group{margin-top:3rem}.catalogue-group ol{
list-style:none;padding:0}.catalogue-group li{display:grid;grid-template-columns:1fr auto;gap:1rem;
border-top:1px solid var(--reader-rule);padding:.8rem 0}.catalogue-group a{text-decoration:none}
.work-id,.line-number,.source-path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.work-id{display:inline-block;width:7rem;color:var(--reader-accent)}.representations{
color:var(--reader-muted);font-size:.82rem}.catalogue-path{display:block;margin:.2rem 0 0 7rem;
color:var(--reader-muted);font-size:.84rem}.parallel-text{display:grid;grid-template-columns:
minmax(0,1fr) minmax(0,1fr);gap:2rem;align-items:start}.representation{min-width:0;
padding:1.5rem;background:var(--reader-sheet);border:1px solid var(--reader-rule)}
.representation>header{border-bottom:1px solid var(--reader-rule);margin-bottom:1.5rem}
.source-path{overflow-wrap:anywhere;color:var(--reader-muted);font-size:.7rem}.text-body+.text-body,
.segment,.line-group,.trailer{margin-top:1.75rem}.line,.paragraph{position:relative;
padding-left:4.8rem;margin:.55rem 0}.line-number{position:absolute;left:0;width:4rem;
color:var(--reader-muted);font-size:.72rem;text-align:right}.note{color:var(--reader-muted);
font-size:.9em}.gap,.correction,.milestone,.determinative{color:var(--reader-accent)}.foreign{
font-style:italic}.unclear{text-decoration:underline dotted}.correction{border-bottom:1px dashed
var(--reader-accent)}.determinative{font-style:normal}.ruling{display:inline-block;min-width:7rem;
color:var(--reader-muted);letter-spacing:.1em}.trailer{border-top:1px solid var(--reader-rule);
padding-top:1rem;font-style:italic}@media(max-width:800px){.parallel-text,.catalogue-group li{
grid-template-columns:1fr}.catalogue-path{margin-left:0}}@media print{.representation{border:0;padding:0}}
"""
