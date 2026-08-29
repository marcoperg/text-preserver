"""Render a static reader from a captured GRETIL package."""

from __future__ import annotations

import hashlib
from html import escape as escape_html
from pathlib import Path
from typing import Sequence
import xml.etree.ElementTree as ElementTree
import zipfile

from text_preserver.adapters import ReaderContext, ReaderReport

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
