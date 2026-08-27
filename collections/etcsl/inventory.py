"""Extract and validate the ETCSL composition catalogue."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Iterable, Sequence
from urllib.parse import parse_qs, urljoin, urlsplit


CATALOGUE_URL = "https://etcsl.orinst.ox.ac.uk/cgi-bin/etcsl.cgi?text=all"
TEXT_ID_RE = re.compile(r"^(?P<kind>[ct])\.(?P<id>[0-9]+(?:\.(?:[0-9]+|[a-z])){2,4})$")
VISIBLE_ID_RE = re.compile(r"^(?P<id>[0-9]+(?:\.(?:[0-9]+|[a-z])){2,4})\s+")
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
    duplicates = sorted({identifier for identifier in ids if ids.count(identifier) > 1})
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
