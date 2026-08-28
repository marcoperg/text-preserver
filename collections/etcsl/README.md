# ETCSL collection recipe

This recipe preserves the Electronic Text Corpus of Sumerian Literature as three independently auditable sources:

- `historical-web`: the project site, complete CGI catalogue, manual, translations, and transliterations;
- `ota-record`: the Oxford Text Archive repository metadata;
- `ota-dataset`: the canonical 4.9 MB XML corpus ZIP.

The known endpoints returned direct HTTP `200` responses when reviewed on 2026-08-27. Capture still disables redirects. If an endpoint begins redirecting, review the destination and update the explicit seed rather than enabling unrestricted redirect following.

The historical web capture is intentionally bounded to recursion depth 2, the exact host, a 250 MiB quota, a 250 KiB/s rate, two attempts, and randomized waits. Review `text-preserver capture etcsl --source historical-web --dry-run` before a supervised capture.

## Inventory

The complete catalogue at `?text=all` contains 394 unique transliteration records and 381 translation records. Thirteen category-0 catalogue compositions intentionally have no translation:

```text
0.1.1 0.1.2
0.2.01 0.2.02 0.2.03 0.2.04 0.2.05 0.2.06 0.2.07
0.2.08 0.2.11 0.2.12 0.2.13
```

Run the fixture-based extractor directly with:

```bash
python collections/etcsl/inventory.py collections/etcsl/fixtures/catalogue.html \
  --expected-work-count 4 \
  --known-untranslated 0.1.1
```

The adapter parses query parameters rather than complete URLs because display parameters and their ordering are presentation details. Identifiers remain strings so leading zeros and alphabetic suffixes are preserved.

`rules/preservation.pl` provides the first optional Ciao completeness rules. It requires a transliteration for every composition and a translation for every composition not listed as intentionally untranslated.

Capture-integrated analysis is available through:

```bash
text-preserver analyze preservation etcsl /path/to/capture -c collections.toml
```

The canonical ZIP contains 394 transliteration XML files, 381 translation XML files, and 27 TEI support files. All 775 text XML shells parse when their named entities are safely stubbed, and their root IDs match their filenames. None of the text XML files has a `DOCTYPE`, so all 82 named entities used by those documents are unbound even where similarly named declarations exist elsewhere in the deposit. The reachable `tei2.dtd` support graph also omits `etcsl-extensions.ent`, `etcsl-extensions.dtd`, and conditional TEI modules. The report therefore distinguishes complete work/representation mapping from incomplete XML dependencies.

## Static reader

Generate a local access site from the newest verified canonical-deposit capture:

```bash
text-preserver derive reader etcsl -c collections.toml
text-preserver open reader etcsl -c collections.toml
```

The recipe's `reader_source = "ota-dataset"` setting resolves the capture through `LATEST-ota-dataset`; an explicit capture path remains supported. The command writes `reader/index.html`, 394 composition pages, and `metadata.json` under the capture's directory in `derived/`, then atomically updates the collection-level `reader` symlink after a usable build. Pages contain responsive transliteration and translation columns, manuscript sections, line or paragraph labels, notes, gaps, and editorial milestones. They contain no JavaScript or external assets and can be opened in EWW or a normal browser. Use `open reader --print-only` to print the stable path without launching a browser.

This first reader is not a replica of the historical ETCSL website. It omits bibliography linking and advanced stand-off annotations. ETCSL-specific letters, subscripts, editorial signs, determinatives, and horizontal rulings are decoded before standard HTML entities. This precedence matters because ETCSL's `&aleph;` and `&mu;` names otherwise collide with unrelated HTML entities. Determinatives are shown as semantic superscripts, while any genuinely unknown name remains visible as its source token.

The mapping is based on `etcsl-sux.ent` from the official OTA all-files package at `https://ota.bodleian.ox.ac.uk/repository/xmlui/handle/20.500.12024/2518/allzip`. That package also contains `etcsl-extensions.dtd` and `etcsl-extensions.ent`; the inner `etcsl.zip` captured as the canonical text payload omits all three files. The entity file's character descriptions are corroborated by the historical ETCSL CGI's Unicode rendering. The output records capture, manifest, configuration, recipe, renderer, and canonical archive hashes.

## Rights

This public recipe records URLs and technical observations only. It does not grant rights to redistribute captured site or corpus content. Captures and private operator configuration remain outside Git.
