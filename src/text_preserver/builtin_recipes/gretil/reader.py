"""Render a static reader from a captured GRETIL package."""

from __future__ import annotations

import hashlib
from html import escape as escape_html
from pathlib import Path
from typing import Mapping, Sequence
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
    access_segment_json,
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
    BULK_TEI_MAPPINGS,
    BULK_TEI_RE,
    BULK_TEI_ROOT_ID_EXCEPTIONS,
    EXPECTED_TEI_ID_SHA256,
    InventoryError,
    MAX_CATALOGUE_SIZE,
    TEI_ROOT,
    XML_ID,
    _analyze_zip,
    _find_unique,
    extract_inventory,
)


XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"
TEI = {"tei": TEI_NAMESPACE}
MAX_READER_TEI_SIZE = 64 * 1024 * 1024
MAX_READER_ACCESS_SEGMENTS = 500_000
MAX_SEGMENT_INDEX_SIZE = 128 * 1024 * 1024
SEGMENT_INDEX_ROUTE = "access-segments.jsonl"
KNOWN_BULK_ONLY_TEI = frozenset({"sa_jayAditya-and-vAmana-kAzikAvRtti"})
READER_PACKAGE = "1_sanskr.zip"
COLLECTION_RIGHTS = (
    "Rights vary by item and representation; preserve embedded notices and do not infer a collection-wide redistribution licence."
)
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


def build_reader(context: ReaderContext) -> ReaderReport:
    """Build the existing GRETIL reader through the recipe API 2 contract."""
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
    (assets_directory / "reader.css").write_text(reader_stylesheet(), encoding="utf-8")
    (assets_directory / "gretil.css").write_text(_gretil_stylesheet(), encoding="utf-8")

    records: list[dict[str, object]] = []
    no_body_ids: list[str] = []
    missing_availability_ids: list[str] = []
    missing_source_ids: list[str] = []
    package_sha256 = _sha256_file(package_path)
    segment_index_path = output_directory / SEGMENT_INDEX_ROUTE
    with (
        zipfile.ZipFile(package_path) as archive,
        segment_index_path.open("w", encoding="utf-8", newline="\n") as segment_index,
    ):
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
            if register_id not in expected_ids:
                continue
            if register_id in members:
                raise InventoryError(f"duplicate reader TEI record: {register_id}")
            members[register_id] = (info, filename_id, expected_root_id)

        rendered_order = tuple(identifier for identifier in expected_ids if identifier in members)
        routes = {
            identifier: f"works/{route_token(identifier)}.html"
            for identifier in rendered_order
        }
        legacy_routes = {
            identifier: f"works/{position:04d}.html"
            for position, identifier in enumerate(expected_ids, start=1)
        }
        collection_access_id = access_id("gretil", "collection", "")
        package_artifact_id = access_id("gretil", "artifact", READER_PACKAGE)
        package_capture_path = package_path.relative_to(capture_directory).as_posix()
        access_artifacts = [
            AccessArtifact(
                package_artifact_id,
                "GRETIL aggregate TEI package",
                "preservation_original",
                package_capture_path,
                "application/zip",
                package_sha256,
            )
        ]
        access_items: list[AccessItem] = []
        relations: list[AccessRelation] = []
        access_segment_count = 0
        segment_index_size = 0
        stable_routes = set(routes.values())
        for position, identifier in enumerate(rendered_order):
            member = members[identifier]
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
            text = root.find("./tei:text", TEI)
            body = root.find("./tei:text/tei:body", TEI)
            if body is None or not any((value or "").strip() for value in body.itertext()):
                no_body_ids.append(identifier)
            availability = root.find(
                "./tei:teiHeader/tei:fileDesc/tei:publicationStmt/tei:availability",
                TEI,
            )
            if availability is None:
                missing_availability_ids.append(identifier)
            item_rights = _access_rights(availability)
            source_description = root.find(
                "./tei:teiHeader/tei:fileDesc/tei:sourceDesc",
                TEI,
            )
            if source_description is None:
                missing_source_ids.append(identifier)
            previous_id = rendered_order[position - 1] if position else None
            next_id = (
                rendered_order[position + 1]
                if position + 1 < len(rendered_order)
                else None
            )
            item_id = access_id("gretil", "item", identifier)
            representation_id = access_id("gretil", "representation", f"{identifier}/tei")
            member_artifact_id = access_id("gretil", "artifact", info.filename)
            segments, segment_anchors = _access_segments(text, identifier, routes[identifier])
            access_segment_count += len(segments)
            if access_segment_count > MAX_READER_ACCESS_SEGMENTS:
                raise InventoryError(
                    f"reader access model exceeds {MAX_READER_ACCESS_SEGMENTS} segments"
                )
            for segment in segments:
                line = access_segment_json(representation_id, segment)
                segment_index_size += len(line.encode("utf-8"))
                if segment_index_size > MAX_SEGMENT_INDEX_SIZE:
                    raise InventoryError(
                        f"reader segment index exceeds {MAX_SEGMENT_INDEX_SIZE} bytes"
                    )
                segment_index.write(line)
            citation_text = (
                f'GRETIL {identifier}, {record["title"]}. '
                f"Derived from capture {capture_directory.name}."
            )
            access_artifacts.append(
                AccessArtifact(
                    member_artifact_id,
                    f"GRETIL TEI source for {identifier}",
                    "preservation_original",
                    package_capture_path,
                    "application/tei+xml",
                    container_id=package_artifact_id,
                    member_path=info.filename,
                )
            )
            access_items.append(
                AccessItem(
                    item_id,
                    str(record["title"]),
                    "text",
                    routes[identifier],
                    citation_text,
                    (
                        AccessRepresentation(
                            representation_id,
                            "TEI text",
                            "tei",
                            str(record["primary_language"]),
                            f'{routes[identifier]}#representation-tei',
                            (member_artifact_id,),
                            segment_index=SEGMENT_INDEX_ROUTE,
                        ),
                    ),
                    item_rights,
                    _record_access_facets(record, (member_artifact_id,)),
                )
            )
            relations.append(AccessRelation(item_id, "part_of", collection_access_id))
            page = _render_reader_work(
                root,
                record,
                capture_directory.name,
                package_sha256,
                routes.get(previous_id) if previous_id else None,
                routes.get(next_id) if next_id else None,
                previous_id,
                next_id,
                item_id,
                member_artifact_id,
                citation_text,
                segment_anchors,
            )
            (output_directory / routes[identifier]).write_text(page, encoding="utf-8")
            legacy_route = legacy_routes[identifier]
            if legacy_route != routes[identifier]:
                if legacy_route in stable_routes:
                    raise InventoryError(f"legacy reader route collides: {legacy_route}")
                (output_directory / legacy_route).write_text(
                    _render_legacy_route(identifier, routes[identifier]),
                    encoding="utf-8",
                )

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
    complete = (
        len(records) == expected_work_count
        and not missing_ids
        and not unexpected_extra_ids
        and not no_body_ids
        and not missing_availability_ids
        and not missing_source_ids
        and bool(package_report["crc_ok"])
    )
    status = "complete_with_warnings" if complete else "incomplete"

    (output_directory / "index.html").write_text(
        _render_reader_index(
            records,
            capture_directory.name,
            package_sha256,
            status,
            warnings,
            package_artifact_id,
        ),
        encoding="utf-8",
    )
    (output_directory / "about.html").write_text(
        _render_reader_about(
            capture_directory.name,
            package_sha256,
            package_report,
            extra_ids,
            status,
            warnings,
            package_artifact_id,
        ),
        encoding="utf-8",
    )
    (output_directory / "access.json").write_text(
        access_json(
            AccessCollection(
                collection_access_id,
                "GRETIL",
                status,
                "index.html",
                tuple(access_items),
                tuple(access_artifacts),
                tuple(relations),
                (COLLECTION_RIGHTS,),
            )
        ),
        encoding="utf-8",
    )
    return {
        "status": status,
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


def _record_access_facets(
    record: Mapping[str, object],
    artifact_ids: tuple[str, ...] = (),
) -> tuple[AccessFacet, ...]:
    facets: list[AccessFacet] = []
    authors = tuple(dict.fromkeys(_record_values(record, "authors")))
    terms = tuple(dict.fromkeys(_record_values(record, "terms")))
    if authors:
        facets.append(AccessFacet("author", "Author", authors, artifact_ids))
    if terms:
        facets.append(AccessFacet("tei_keyword", "TEI keyword", terms, artifact_ids))
    return tuple(facets)


def _record_values(record: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = record.get(key)
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if value)


def _access_segments(
    text: ElementTree.Element | None,
    identifier: str,
    route: str,
) -> tuple[tuple[AccessSegment, ...], dict[int, str]]:
    if text is None:
        return (), {}
    values: list[AccessSegment] = []
    anchors: dict[int, str] = {}
    occurrences: dict[str, int] = {}
    for element in text.iter():
        source_id = element.get(XML_ID)
        if not source_id:
            continue
        if len(values) >= MAX_READER_ACCESS_SEGMENTS:
            raise InventoryError(
                f"reader access model exceeds {MAX_READER_ACCESS_SEGMENTS} segments in {identifier}"
            )
        occurrence = occurrences.get(source_id, 0) + 1
        occurrences[source_id] = occurrence
        anchor = f"segment-{route_token(source_id)}--occurrence-{occurrence}"
        anchors[id(element)] = anchor
        values.append(
            AccessSegment(
                access_id(
                    "gretil",
                    "segment",
                    f"{identifier}/{source_id}/{occurrence}",
                ),
                source_id,
                f"{route}#{anchor}",
            )
        )
    return tuple(values), anchors


def _render_legacy_route(identifier: str, route: str) -> str:
    target = Path(route).name
    return render_document(
        f"GRETIL {identifier}",
        f"""
{render_navigation((ReaderLink("GRETIL catalogue", "../index.html"),))}
<main class="reader-main about">
  <h1>Reader route updated</h1>
  <p>This preserved positional route now has a stable identifier-based address.</p>
  <p><a href="{escape_html(target, quote=True)}">Open {escape_html(identifier)}</a></p>
</main>
""",
        asset_prefix="../",
        collection_stylesheet="gretil.css",
    )


def _render_reader_index(
    records: Sequence[dict[str, object]],
    capture_id: str,
    package_sha256: str,
    status: str,
    warnings: Sequence[str],
    package_artifact_id: str,
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
    return render_document(
        "GRETIL Reader",
        f"""
<header class="reader-header">
  <p class="reader-eyebrow">Verified local access copy</p>
  <h1>GRETIL</h1>
  <p class="reader-lede">{len(records)} electronic texts in Indian languages, rendered from the
  publisher's preserved TEI package without changing the archive master.</p>
</header>
{render_navigation((ReaderLink("About this derived reader", "about.html"),))}
<main class="reader-main">
  {render_notice(COLLECTION_RIGHTS)}
  {render_status(status, warnings)}
  <p class="catalogue-meta">Capture <code>{escape_html(capture_id)}</code></p>
  {''.join(sections)}
</main>
<footer class="reader-footer">Aggregate package SHA-256 <code>{escape_html(package_sha256)}</code>
{render_artifact_reference("Machine-readable source artifact", package_artifact_id, "access.json")}</footer>
""",
        collection_stylesheet="gretil.css",
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
    item_id: str,
    member_artifact_id: str,
    citation_text: str,
    segment_anchors: Mapping[int, str],
) -> str:
    navigation = render_navigation(
        (ReaderLink("GRETIL catalogue", "../index.html"),),
        previous=(
            ReaderLink(previous_id, f"../{previous_route}")
            if previous_route and previous_id
            else None
        ),
        next_=(
            ReaderLink(next_id, f"../{next_route}")
            if next_route and next_id
            else None
        ),
    )
    language_values = [
        f"{code} ({label})" if code and label and code != label else code or label
        for code, label in record["languages"]
    ]
    facets = render_facets(
        tuple(
            ReaderFacet(facet.label, facet.values)
            for facet in _record_access_facets(record)
        )
    )
    availability = root.find("./tei:teiHeader/tei:fileDesc/tei:publicationStmt/tei:availability", TEI)
    source_description = root.find("./tei:teiHeader/tei:fileDesc/tei:sourceDesc", TEI)
    notes = root.find("./tei:teiHeader/tei:fileDesc/tei:notesStmt", TEI)
    responsibilities = root.findall("./tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:respStmt", TEI)
    text = root.find("./tei:text", TEI)
    text_html = (
        _render_tei_element(text, segment_anchors=segment_anchors)
        if text is not None
        else '<p class="unavailable">No text element is present in this TEI record.</p>'
    )
    metadata_sections = [
        _reader_details("Availability and rights", availability, open_by_default=True),
        _reader_details("Source description", source_description),
        _reader_details("Editorial and conversion notes", notes),
    ]
    if responsibilities:
        responsibility_html = "".join(
            _render_tei_element(item, emit_segment_anchors=False)
            for item in responsibilities
        )
        metadata_sections.insert(
            1,
            f'<details><summary>Responsibilities</summary><div class="metadata-text">'
            f"{responsibility_html}</div></details>",
        )
    provenance = render_facts(
        (
            ReaderFact("Register ID", str(record["identifier"])),
            ReaderFact("Filename ID", str(record["filename_id"])),
            ReaderFact("TEI root ID", str(record["root_id"])),
            ReaderFact("ZIP member", str(record["member_path"])),
            ReaderFact("Capture", capture_id),
        )
    )
    citation = render_citation(citation_text, item_id)
    return render_document(
        f'{record["identifier"]} - {record["title"]}',
        f"""
{navigation}
<header class="reader-header">
  <p class="reader-eyebrow">{escape_html(str(record["identifier"]))}</p>
  <h1>{escape_html(str(record["title"]))}</h1>
  <p class="record-meta">{escape_html(' / '.join(language_values))}</p>
  {facets}
</header>
<main class="reader-main work-layout">
  <aside class="record-sidebar">
    {provenance}
    {render_artifact_reference("Source artifact", member_artifact_id, "../access.json")}
    {''.join(value for value in metadata_sections if value)}
  </aside>
  <article id="representation-tei" class="tei-text">{text_html}{citation}</article>
</main>
<footer class="reader-footer">Preserved aggregate package SHA-256 <code>{escape_html(package_sha256)}</code></footer>
""",
        asset_prefix="../",
        collection_stylesheet="gretil.css",
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
        f'<div class="metadata-text">{attributes}'
        f'{_render_tei_content(element, emit_segment_anchors=False)}</div></details>'
    )


def _access_rights(element: ElementTree.Element | None) -> tuple[str, ...]:
    if element is None:
        return ()
    values: list[str] = []
    text = _metadata_text(element)
    if text:
        values.append(text)
    for name in ("status", "type"):
        if value := (element.get(name) or "").strip():
            values.append(f"{name}: {value}")
    for licence in element.findall(".//tei:licence", TEI):
        if target := (licence.get("target") or "").strip():
            values.append(f"licence target: {target}")
    return tuple(dict.fromkeys(values))


def _render_reader_about(
    capture_id: str,
    package_sha256: str,
    package_report: dict[str, object],
    extra_ids: Sequence[str],
    status: str,
    warnings: Sequence[str],
    package_artifact_id: str,
) -> str:
    warning_items = "".join(f"<li>{escape_html(value)}</li>" for value in warnings)
    extra_items = "".join(f"<li><code>{escape_html(value)}</code></li>" for value in extra_ids)
    provenance = render_facts(
        (
            ReaderFact("Capture", capture_id),
            ReaderFact("Package SHA-256", package_sha256),
            ReaderFact("ZIP entries", str(package_report["entry_count"])),
            ReaderFact("Expanded bytes checked", str(package_report["uncompressed_size"])),
            ReaderFact("CRC check", "passed" if package_report["crc_ok"] else "failed"),
        )
    )
    return render_document(
        "About the GRETIL Reader",
        f"""
{render_navigation((ReaderLink("GRETIL catalogue", "index.html"),))}
<header class="reader-header"><p class="reader-eyebrow">Derived access copy</p>
<h1>About this reader</h1></header>
<main class="reader-main about">
  <p>This static reader was generated locally from <code>{READER_PACKAGE}</code> in capture
  <code>{escape_html(capture_id)}</code>. The preserved ZIP remains authoritative.</p>
  {provenance}
  {render_status(status, warnings)}
  <h2>Interpretive limits</h2><ul>{warning_items}</ul>
  {f'<h2>Bulk-only records not in the reviewed catalogue</h2><ul>{extra_items}</ul>' if extra_items else ''}
  <p>The reader escapes source text, loads no scripts or remote resources, does not resolve
  external TEI references, and does not alter or extract files into the preservation capture.</p>
</main>
<footer class="reader-footer">GRETIL local preservation reader
{render_artifact_reference("Machine-readable source artifact", package_artifact_id, "access.json")}</footer>
""",
        collection_stylesheet="gretil.css",
    )


def _metadata_text(element: ElementTree.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _render_tei_content(
    element: ElementTree.Element,
    *,
    inline_blocks: bool = False,
    segment_anchors: Mapping[int, str] | None = None,
    emit_segment_anchors: bool = True,
) -> str:
    parts = [_render_tei_text(element.text)]
    for child in element:
        parts.append(
            _render_tei_element(
                child,
                inline_blocks=inline_blocks,
                segment_anchors=segment_anchors,
                emit_segment_anchors=emit_segment_anchors,
            )
        )
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
    segment_anchors: Mapping[int, str] | None = None,
    emit_segment_anchors: bool = True,
) -> str:
    name = _local_name(element.tag)
    number = element.get("n") or ""
    number_html = escape_html(number)
    content = _render_tei_content(
        element,
        inline_blocks=inline_blocks,
        segment_anchors=segment_anchors,
        emit_segment_anchors=emit_segment_anchors,
    )
    source_id = element.get(XML_ID)
    source_anchor = ""
    if source_id and emit_segment_anchors:
        anchor = (
            segment_anchors.get(id(element))
            if segment_anchors is not None
            else f"segment-{route_token(source_id)}--occurrence-1"
        )
        if anchor is None:
            raise InventoryError(f"missing reader segment anchor: {source_id}")
        source_anchor = (
            f'<span id="{anchor}" class="segment-anchor"></span>'
        )
        content = f"{source_anchor}{content}"
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
        note_content = source_anchor + _render_tei_content(
            element,
            inline_blocks=True,
            segment_anchors=segment_anchors,
            emit_segment_anchors=emit_segment_anchors,
        )
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


def _gretil_stylesheet() -> str:
    return """:root{--reader-accent:#a9491c;--reader-link:#243f58;--note:#f2e2b9}
body{background:linear-gradient(90deg,#e3ddce 0,transparent 12%,transparent 88%,#e3ddce 100%),
var(--reader-paper)}.catalogue-meta{color:var(--reader-muted)}.language-group{margin:3rem 0}
.language-group>h2{padding-bottom:.5rem;border-bottom:3px double var(--reader-rule)}.language-group ol{
list-style:none;padding:0}.language-group li{display:grid;grid-template-columns:minmax(0,1fr) auto;
gap:1rem;padding:.8rem 0;border-bottom:1px solid var(--reader-rule)}.language-group a{display:grid;
grid-template-columns:minmax(11rem,18rem) minmax(0,1fr);gap:.75rem;text-decoration:none}
.record-id,.line-number,.line-ref{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.record-id{color:var(--reader-accent);font-size:.78rem;overflow-wrap:anywhere}.author,.terms{
display:block;color:var(--reader-muted);font-size:.84rem}.terms{align-self:center}.byline{font-size:1.2rem}
.record-meta{color:var(--reader-muted)}.work-layout{display:grid;grid-template-columns:
minmax(16rem,25rem) minmax(0,1fr);gap:2rem;align-items:start}.record-sidebar{position:sticky;
top:1rem;max-height:calc(100vh - 2rem);overflow:auto;padding:1.2rem;border:1px solid
var(--reader-rule);background:#f8f3e6;font-size:.85rem}.record-sidebar details{margin:1rem 0;
border-top:1px solid var(--reader-rule);padding-top:.65rem}summary{cursor:pointer;font-weight:700}
.metadata-text{margin-top:.7rem}.tei-text{min-width:0;padding:clamp(1rem,3vw,2.5rem);
border:1px solid var(--reader-rule);background:var(--reader-sheet);box-shadow:0 1rem 2.5rem #51462c18}
.division+.division{margin-top:2.5rem}.division-label,.line-group-label{color:var(--reader-accent);
font-size:.78rem;text-transform:uppercase;letter-spacing:.08em}.paragraph{margin:1rem 0}.line-group{
margin:1.4rem 0}.line{display:grid;grid-template-columns:4.5rem minmax(0,1fr);gap:.8rem;
margin:.22rem 0}.line-number{color:var(--reader-muted);font-size:.72rem;text-align:right}.line-ref{
color:var(--reader-muted);font-size:.65rem}.page-break{display:block;margin:1.4rem 0 .5rem;
color:var(--reader-muted);font-size:.72rem}.note{background:var(--note);font-size:.88em}.gap,
.milestone{color:var(--reader-accent)}.reference{border-bottom:1px dotted var(--reader-muted)}
.apparatus{border-bottom:1px dashed var(--reader-accent)}.lem:before{content:"lem. ";
color:var(--reader-muted);font-size:.72rem}.rdg:before{content:" rdg. ";color:var(--reader-muted);
font-size:.72rem}.rdg small,.lem small{margin-left:.3rem;color:var(--reader-muted)}.tei-foreign,
.tei-emph,.rend-it,.rend-italic{font-style:italic}.rend-bold{font-weight:700}.rend-underline{
text-decoration:underline}.rend-red{color:#8d241c}.rend-blue{color:#1c4d78}.rend-green{color:#35653a}
.rend-center{text-align:center}.rend-small{font-size:.85em}.rend-sup{vertical-align:super;font-size:.75em}
.rend-sub{vertical-align:sub;font-size:.75em}.tei-supplied:before{content:"["}.tei-supplied:after{
content:"]"}.tei-unclear{text-decoration:underline dotted}.tei-del,.tei-surplus{text-decoration:line-through}
.tei-orig,.tei-sic{color:#6d4226}.has-source-rendition:after{content:" [rend: " attr(data-tei-rend) "]";
color:var(--reader-muted);font:normal .68rem/1.3 ui-monospace,monospace}.tei-inline-block{
display:block;margin:.35rem 0}.tei-table{border-collapse:collapse;max-width:100%;overflow:auto}
.tei-table td{padding:.35rem;border:1px solid var(--reader-rule)}.tei-bibl,.tei-biblStruct,.tei-cit{
margin:.7rem 0;padding-left:1rem;border-left:2px solid var(--reader-rule)}.source-target{
display:inline-block;margin-left:.35rem;color:var(--reader-muted);font:normal .72rem/1.4
ui-monospace,monospace}.tei-unknown{outline:1px dotted transparent}.speaker{display:block;
margin-top:1rem}.about{max-width:780px}blockquote{margin:1rem 2rem;padding-left:1rem;
border-left:3px solid var(--reader-rule)}@media(max-width:850px){.work-layout{grid-template-columns:1fr}
.record-sidebar{position:static;max-height:none}.language-group li,.language-group a{
grid-template-columns:1fr}.line{grid-template-columns:3rem minmax(0,1fr)}}@media print{
.record-sidebar{display:none}.work-layout{display:block}.tei-text{border:0;box-shadow:none;padding:0}}
"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
