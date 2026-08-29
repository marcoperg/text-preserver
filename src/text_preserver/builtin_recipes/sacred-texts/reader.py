"""Render an honest preservation-status reader for Sacred Texts."""

from __future__ import annotations

from html import escape as escape_html
from pathlib import Path
from typing import Mapping, Sequence

from text_preserver.access.reader_model import (
    AccessArtifact,
    AccessCollection,
    access_id,
    access_json,
)
from text_preserver.access.reader_shell import (
    ReaderFact,
    reader_stylesheet,
    render_document,
    render_facts,
    render_notice,
    render_status,
)
from text_preserver.adapters import ReaderContext, ReaderReport

from . import validator


REQUIRED_SOURCE_IDS = ("internet-archive-2021", "wayback-download-recovery")
COLLECTION_RIGHTS = (
    "Rights vary by work, transcription, image, and site-authored context; public access "
    "does not imply a collection-wide redistribution licence."
)


def build_reader(context: ReaderContext) -> ReaderReport:
    """Build a small status view without extracting or replaying the captured WARC."""
    payload = render_static_reader(
        context.capture_directory,
        expected_work_count=context.expected_work_count,
    )
    return ReaderReport(
        str(payload["status"]),  # type: ignore[arg-type]
        payload["summary"],  # type: ignore[arg-type]
        tuple(payload["warnings"]),  # type: ignore[arg-type]
        payload["files"],  # type: ignore[arg-type]
    )


def render_static_reader(
    capture_directory: Path,
    *,
    expected_work_count: int,
) -> dict[str, object]:
    """Return inert status files derived from the bounded preservation validator."""
    report = validator.analyze_capture(
        capture_directory,
        expected_work_count=expected_work_count,
        required_representation_kinds=("warc-record",),
        required_source_ids=REQUIRED_SOURCE_IDS,
    )
    status = str(report["status"])
    errors = _strings(report.get("errors"))
    warnings = _strings(report.get("warnings"))
    artifacts = _access_artifacts(capture_directory, report)
    collection = AccessCollection(
        access_id("sacred-texts", "collection", ""),
        "Internet Sacred Text Archive",
        status,
        "index.html",
        (),
        artifacts,
        rights=(COLLECTION_RIGHTS,),
    )
    files = {
        "index.html": _render_index(report, capture_directory.name, errors, warnings),
        "access.json": access_json(collection),
        "assets/reader.css": reader_stylesheet(),
    }
    return {
        "status": status,
        "summary": {
            "item_count": 0,
            "artifact_count": len(artifacts),
            "cdx_record_count": report.get("cdx_record_count", 0),
            "remaining_known_gap_count": _mapping(report.get("download_page")).get(
                "remaining_known_gap_count", 0
            ),
            "errors": errors,
        },
        "warnings": warnings,
        "files": files,
    }


def _access_artifacts(
    capture_directory: Path,
    report: Mapping[str, object],
) -> tuple[AccessArtifact, ...]:
    values: list[AccessArtifact] = []
    mirror = capture_directory / "sources/internet-archive-2021/mirror"
    files, _errors = validator._find_capture_files(mirror)
    reported_names = {
        str(value["name"])
        for value in report.get("artifacts", [])
        if isinstance(value, dict) and isinstance(value.get("name"), str)
    }
    for name in sorted(reported_names):
        path = files.get(name)
        if path is None:
            continue
        values.append(
            AccessArtifact(
                access_id("sacred-texts", "artifact", f"internet-archive-2021/{name}"),
                name,
                "preservation_original",
                path.relative_to(capture_directory).as_posix(),
                _media_type(name),
            )
        )
    recovery_root = capture_directory / "sources/wayback-download-recovery/mirror"
    recovered = _mapping(report.get("download_page")).get("recovered_payloads", [])
    for value in recovered if isinstance(recovered, list) else []:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            continue
        name = value["name"]
        matches = sorted(
            path
            for path in recovery_root.rglob(name)
            if path.is_file() and not path.is_symlink()
        ) if recovery_root.is_dir() else []
        if len(matches) != 1:
            continue
        values.append(
            AccessArtifact(
                access_id("sacred-texts", "artifact", f"wayback-download-recovery/{name}"),
                name,
                "preservation_original",
                matches[0].relative_to(capture_directory).as_posix(),
                "application/gzip",
                str(value["sha256"]) if isinstance(value.get("sha256"), str) else None,
            )
        )
    return tuple(values)


def _render_index(
    report: Mapping[str, object],
    capture_id: str,
    errors: Sequence[str],
    warnings: Sequence[str],
) -> str:
    sources = _mapping(report.get("source_statuses"))
    download = _mapping(report.get("download_page"))
    media = _mapping(report.get("publisher_media"))
    facts = render_facts(
        (
            ReaderFact("Capture", capture_id),
            ReaderFact("Internet Archive item artifacts", str(report.get("artifact_count", 0))),
            ReaderFact("Indexed WARC records", str(report.get("cdx_record_count", 0))),
            ReaderFact(
                "Downloads-page payloads represented",
                f"{int(download.get('warc_successful_payload_count', 0)) + int(download.get('recovered_in_capture_count', 0))}/{download.get('published_payload_count', 0)}",
            ),
            ReaderFact("Remaining exact gzip gaps", str(download.get("remaining_known_gap_count", 0))),
            ReaderFact("Official media version", str(media.get("version", "unknown"))),
            ReaderFact("Official media preserved", "no" if not media.get("preserved") else "yes"),
        )
    )
    source_items = "".join(
        f"<li><code>{escape_html(str(source_id))}</code>: {escape_html(str(value))}</li>"
        for source_id, value in sorted(sources.items())
    )
    gap_items = "".join(f"<li>{escape_html(value)}</li>" for value in errors)
    gap_count = int(download.get("remaining_known_gap_count", 0))
    return render_document(
        "Internet Sacred Text Archive preservation status",
        f"""
<header class="reader-header">
  <p class="reader-eyebrow">Preservation status view</p>
  <h1>Internet Sacred Text Archive</h1>
  <p class="reader-lede">A bounded account of preserved evidence and known gaps, not the original site or a complete corpus reader.</p>
</header>
<main class="reader-main">
  {render_notice(f"This collection is incomplete. The captured 2021 WARC remains useful historical evidence, but official 9.0 media and {gap_count} exact downloads-page gzip payloads are not preserved in this capture.")}
  {render_notice(COLLECTION_RIGHTS)}
  {render_status(str(report.get("status", "incomplete")), (*errors, *warnings))}
  <h2>Verified facts</h2>{facts}
  <h2>Source acquisition states</h2><ul>{source_items}</ul>
  <h2>Known preservation gaps</h2><ul>{gap_items}</ul>
  <h2>Interpretive limits</h2>
  <p>The CDX count describes HTTP records, not works. Compressed WARC bytes are not directly comparable with the published logical size of the official media. No WARC content is extracted or replayed by this status view.</p>
</main>
<footer class="reader-footer" data-access-id="tp:sacred-texts/collection">Generated from validated capture metadata and bounded archive inspection.</footer>
""",
    )


def _strings(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _media_type(name: str) -> str:
    if name.endswith(".warc.gz"):
        return "application/warc+gzip"
    if name.endswith(".gz"):
        return "application/gzip"
    if name.endswith(".xml"):
        return "application/xml"
    if name.endswith(".sqlite"):
        return "application/vnd.sqlite3"
    if name.endswith(".torrent"):
        return "application/x-bittorrent"
    return "application/octet-stream"
