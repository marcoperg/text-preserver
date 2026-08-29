"""Shared inert HTML components for collection-specific readers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import escape
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit


READER_SHELL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReaderLink:
    """A local link rendered by the shared reader shell."""

    label: str
    href: str


@dataclass(frozen=True)
class ReaderFact:
    """One plain-text provenance or status fact."""

    label: str
    value: str


def reader_shell_identity() -> Mapping[str, object]:
    """Return the rendering-library identity used by deterministic build keys."""
    source = Path(__file__).read_bytes()
    return {
        "schema_version": READER_SHELL_SCHEMA_VERSION,
        "sha256": hashlib.sha256(source).hexdigest(),
    }


def render_document(
    title: str,
    body: str,
    *,
    asset_prefix: str = "",
    collection_stylesheet: str | None = None,
    language: str = "en",
) -> str:
    """Wrap recipe-rendered content in the common offline document envelope."""
    stylesheets = [f"{asset_prefix}assets/reader.css"]
    if collection_stylesheet is not None:
        stylesheets.append(f"{asset_prefix}assets/{collection_stylesheet}")
    links = "\n".join(
        f'<link rel="stylesheet" href="{_local_href(value)}">' for value in stylesheets
    )
    return f"""<!doctype html>
<html lang="{escape(language, quote=True)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'">
<title>{escape(title)}</title>
{links}
</head>
<body>{body}</body>
</html>
"""


def render_navigation(
    links: Sequence[ReaderLink],
    *,
    previous: ReaderLink | None = None,
    next_: ReaderLink | None = None,
) -> str:
    """Render common breadcrumbs and optional adjacent-item navigation."""
    breadcrumbs = "".join(_render_link(link) for link in links)
    adjacent = ""
    if previous is not None or next_ is not None:
        previous_html = _render_link(previous, prefix="Previous: ") if previous else "<span></span>"
        next_html = _render_link(next_, prefix="Next: ") if next_ else "<span></span>"
        adjacent = f'<span class="reader-adjacent">{previous_html}{next_html}</span>'
    return (
        '<nav class="reader-nav" aria-label="Reader navigation">'
        f'<span class="reader-breadcrumbs">{breadcrumbs}</span>{adjacent}</nav>'
    )


def render_notice(message: str) -> str:
    """Render a plain-text interpretive or rights notice."""
    return f'<aside class="reader-notice">{escape(message)}</aside>'


def render_status(status: str, warnings: Sequence[str]) -> str:
    """Render the access build status and its bounded recipe warnings."""
    labels = {
        "complete": "Access build complete",
        "complete_with_warnings": "Access build complete with warnings",
        "incomplete": "Access build incomplete",
    }
    try:
        label = labels[status]
    except KeyError as exc:
        raise ValueError(f"unsupported reader status: {status}") from exc
    warning_html = ""
    if warnings:
        items = "".join(f"<li>{escape(value)}</li>" for value in warnings)
        warning_html = f'<details><summary>Interpretive limits</summary><ul>{items}</ul></details>'
    return (
        f'<section class="reader-status reader-status-{escape(status, quote=True)}">'
        f'<p><strong>{label}</strong></p>{warning_html}</section>'
    )


def render_facts(facts: Sequence[ReaderFact]) -> str:
    """Render plain-text provenance facts with consistent semantics."""
    values = "".join(
        f"<dt>{escape(fact.label)}</dt><dd><code>{escape(fact.value)}</code></dd>"
        for fact in facts
    )
    return f'<dl class="reader-facts">{values}</dl>'


def render_artifact_reference(label: str, artifact_id: str, href: str) -> str:
    """Link visible provenance to its record in the local access graph."""
    return (
        f'<a class="reader-artifact" href="{_local_href(href)}" '
        f'data-access-id="{escape(artifact_id, quote=True)}">{escape(label)}</a>'
    )


def render_citation(citation: str, target_id: str) -> str:
    """Render a script-free citation and stable machine target."""
    return (
        '<aside class="reader-citation"><h2>Cite this item</h2>'
        f'<p><cite>{escape(citation)}</cite></p>'
        f'<p>Stable target <code>{escape(target_id)}</code></p></aside>'
    )


def reader_stylesheet() -> str:
    """Return the shared responsive, print-safe visual foundation."""
    return """:root{color-scheme:light;--reader-ink:#211d17;--reader-muted:#70685d;
--reader-paper:#eee9dc;--reader-sheet:#fffdf6;--reader-rule:#c8bea9;
--reader-accent:#8b321f;--reader-link:#243f58}*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;color:var(--reader-ink);background:var(--reader-paper);font:17px/1.65 Georgia,
"Times New Roman",serif}a{color:var(--reader-link);text-underline-offset:.16em}code,.reader-mono{
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}code{overflow-wrap:anywhere}h1,h2,h3,h4{
line-height:1.14;font-weight:600}.reader-header{padding:4rem max(5vw,1.2rem) 2.6rem;
border-bottom:1px solid var(--reader-rule);background:var(--reader-sheet)}.reader-header h1{
max-width:25ch;margin:.15rem 0;font-size:clamp(2.4rem,6vw,5.4rem)}.reader-eyebrow{margin:0;
color:var(--reader-accent);font:700 .74rem/1.2 ui-monospace,monospace;letter-spacing:.15em;
text-transform:uppercase}.reader-lede{max-width:62ch;color:var(--reader-muted);font-size:1.16rem}
.reader-nav{display:flex;justify-content:space-between;flex-wrap:wrap;gap:1rem 2rem;padding:1rem
max(5vw,1.2rem);border-bottom:1px solid var(--reader-rule);background:var(--reader-sheet)}
.reader-breadcrumbs,.reader-adjacent{display:flex;flex-wrap:wrap;gap:1rem 2rem}.reader-adjacent{
margin-left:auto}.reader-notice{padding:1rem 1.2rem;border-left:4px solid var(--reader-accent);
background:var(--reader-sheet)}.reader-status{margin:1.2rem 0;padding:.8rem 1.1rem;border:1px solid
var(--reader-rule);background:var(--reader-sheet)}.reader-status p{margin:.2rem 0}.reader-status-incomplete{
border-left:4px solid var(--reader-accent)}.reader-status details{margin:.65rem 0 0}.reader-status ul{
margin:.65rem 0}.reader-facts{display:grid;grid-template-columns:max-content minmax(0,1fr);
gap:.35rem .8rem}.reader-facts dt{font-weight:700}.reader-facts dd{margin:0;min-width:0}
.reader-artifact{font-size:.82rem}.reader-citation{margin:2rem 0;padding:1rem 1.2rem;
border-top:1px solid var(--reader-rule);border-bottom:1px solid var(--reader-rule)}
.reader-citation h2{font-size:1rem}.reader-citation p{margin:.4rem 0}
.reader-main,.reader-footer{width:min(1160px,92vw);margin:2rem auto}.reader-footer{padding:1rem 0 3rem;
border-top:1px solid var(--reader-rule);color:var(--reader-muted);font-size:.76rem}
@media(max-width:650px){.reader-nav{display:block}.reader-adjacent{justify-content:space-between;
margin-top:.8rem}.reader-facts{grid-template-columns:1fr}.reader-facts dd{margin:0 0 .6rem}}
@media print{body{background:#fff}.reader-nav{display:none}.reader-status details:not([open]){display:none}}
"""


def _render_link(link: ReaderLink, *, prefix: str = "") -> str:
    return (
        f'<a href="{_local_href(link.href)}">{escape(prefix)}{escape(link.label)}</a>'
    )


def _local_href(value: str) -> str:
    parsed = urlsplit(value)
    if (
        not value
        or "\\" in value
        or parsed.scheme
        or parsed.netloc
        or value.startswith("/")
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"reader link is not local: {value!r}")
    return escape(value, quote=True)
