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

from .validator import (
    BUILTIN_ENTITIES,
    ENTITY_REF_RE,
    InventoryError,
    KNOWN_UNTRANSLATED,
    XML_ENTRY_RE,
    _analyze_zip,
    _find_ota_package,
    _find_zip,
    _ota_support_files,
    _read_zip_text,
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


def build_reader(context: ReaderContext) -> ReaderReport:
    """Build the existing ETCSL reader through the recipe API 2 contract."""
    payload = render_static_reader(
        context.capture_directory,
        expected_work_count=context.expected_work_count,
    )
    return ReaderReport(
        payload["status"],
        payload["summary"],
        tuple(payload["warnings"]),
        payload["files"],
    )


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
    warnings.extend(str(value) for value in archive_report["warnings"])
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
                f"{_render_inline_content(child)}</p>"
            )
        elif tag == "div1":
            label = child.get("n") or child.get("type") or "Section"
            parts.append(
                f'<section class="segment"><h4>{escape_html(label)}</h4>'
                f"{_render_blocks(child)}</section>"
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
