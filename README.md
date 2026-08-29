# text-preserver

**Preserve vulnerable digital text collections and the context that makes them intelligible.**

> **Status: early implementation.** Configuration validation, installable ETCSL and GRETIL recipes, diagnostics, guarded Wget capture, fixity verification, conservative `LATEST` pointers, collection-specific completeness analysis, and local static readers for ETCSL and GRETIL are available.

`text-preserver` is a local-first system for preserving scholarly corpora, digital editions, historical text archives, specialist bibliographies, and old academic websites that may become unavailable or difficult to reconstruct.

It is designed to preserve textual collections rather than merely copy websites. A collection may include HTML, TEI/XML, plain text, PDFs, EPUBs, scans, metadata, database exports, schemas, software, and the historical interface needed to understand the material.

## Why

Important digital collections often depend on aging infrastructure, temporary funding, or a small number of maintainers. They can disappear through server migrations, obsolete software, expired domains, institutional redesigns, or loss of their canonical datasets.

A recursive download is useful, but it does not by itself provide:

- explicit and reproducible capture scope;
- original HTTP exchanges;
- reliable failure records;
- provenance and tool versions;
- cryptographic integrity checks;
- collection-specific completeness checks;
- links between a website and canonical deposits hosted elsewhere.

`text-preserver` aims to turn a collection-specific capture plan into a verifiable preservation package.

## Preservation Model

The central boundary is:

```text
preservation masters  ->  derived data  ->  personal workspace
       immutable           rebuildable          editable
```

- `archive/` contains original captures, WARC files, source deposits, logs, metadata, and fixity manifests.
- `derived/` contains rebuildable inventories, normalized text, indexes, reports, and generated access data.
- `workspace/` contains personal annotations, claims, saved searches, and links to research notes.

Analysis and research tools must never silently alter preservation masters. The archive should remain useful if every derived index, frontend, annotation, or optional analysis engine is removed.

## Planned Capture

The first preservation engine will use Python and GNU Wget to support:

- recursive capture of public HTTP/HTTPS collections;
- nonrecursive capture of canonical files and repository deposits;
- WARC and CDX output;
- locally browsable mirrors;
- conservative host scope, delays, retries, and quotas;
- exact seeds, commands, configuration, and environment metadata;
- source-level and collection-level capture status;
- SHA-256 manifests and later fixity verification;
- retention of partial and interrupted captures for diagnosis.

Browser-based capture for JavaScript-dependent collections is planned for a later phase.

## Current CLI

Create the project-local Conda environment and install the package in editable mode:

```bash
conda env create --prefix ./.venv --file environment.yml
conda activate "$PWD/.venv"
python -m pip install --editable .
```

Create a local configuration and inspect it without making network requests or archive changes:

```bash
cp collections.example.toml collections.toml
text-preserver doctor --config collections.toml
text-preserver collections list --config collections.toml
text-preserver collections show etcsl --config collections.toml
text-preserver collections show gretil --config collections.toml
```

The example configuration references the public ETCSL and GRETIL recipes as `public:etcsl` and `public:gretil`. Local configuration owns operator identity, storage paths, and capture defaults; recipes own public collection metadata, sources, scope, and analysis settings. Relative recipe paths remain supported. Recipe files are preserved with captures for provenance.

`doctor` validates collection and source structure, rejects unsafe scope and credential-bearing arguments, checks GNU Wget WARC support, checks storage locations, and reports available space. `collections list/show` display the fully resolved configuration without making requests.

Inspect the exact Wget commands and paths for a collection without writing files or making network requests:

```bash
text-preserver capture COLLECTION_ID \
  --config collections.toml \
  --dry-run
```

Use `--source SOURCE_ID` to select individual sources and `--json` for a machine-readable plan. The test suite executes these plans only against a localhost fixture and verifies recursive mirror, WARC, CDX, log, and redirect behavior.

After reviewing the dry run, execute a supervised capture:

```bash
text-preserver capture COLLECTION_ID \
  --config collections.toml \
  --note "Supervised exploratory capture"
```

Execution creates a new capture directory atomically, prevents overlapping captures of the same collection, preserves the input and resolved configuration plus configured analysis assets, records the environment and exact commands, and writes source and collection status records. Failed and interrupted captures remain available for diagnosis. Completed attempts receive `manifest-sha256.json` and `SHA256SUMS`; interrupted attempts remain unfinalized for honest recovery.

Verify a finalized capture:

```bash
text-preserver verify /path/to/capture
```

Verification checks object types, sizes, SHA-256 hashes, missing objects, and unexpected additions. Symlinks are rejected. A complete, verified, unfiltered capture updates the collection's portable `LATEST` pointer; warning, partial, failed, interrupted, and source-filtered captures never do. Each successful source also updates a portable `LATEST-SOURCE_ID` pointer after whole-capture fixity verification, so independently captured sources remain convenient to locate.

Run collection-specific completeness analysis after fixity verification:

```bash
text-preserver analyze preservation etcsl /path/to/capture \
  --config collections.toml
```

Analysis reads the immutable capture and writes `completeness.json` only under `derived/`. It prefers the adapter snapshot preserved with the capture and records the adapter SHA-256; older captures without a snapshot use the current recipe adapter with an explicit warning. The ETCSL adapter compares catalogue IDs with deposited XML filenames, checks ZIP safety and CRCs, performs entity-stubbed XML-shell parsing, verifies root IDs, and reports missing entity and DTD dependencies separately from work-level completeness.

Build a local static reader from a verified capture:

```bash
text-preserver derive reader etcsl /path/to/capture \
  --config collections.toml
text-preserver derive reader gretil --config collections.toml
```

The capture path may be omitted. A recipe's `reader_source` selects its newest verified source capture through `LATEST-SOURCE_ID`, with collection `LATEST` as the fallback. Reader generations are written under the capture-scoped `derived/` directory; a successful build atomically updates `derived/collections/COLLECTION_ID/reader` to the immutable current generation. Incomplete builds remain inspectable but never replace that pointer.

Open the current reader in the default browser, or print its stable local path for another program such as Emacs EWW:

```bash
text-preserver open reader etcsl --config collections.toml
text-preserver open reader etcsl --config collections.toml --print-only
text-preserver open reader gretil --config collections.toml
```

The ETCSL reader links all compositions to responsive side-by-side transliteration and translation pages. The GRETIL reader streams all 801 reviewed TEI texts into full local work pages with item-level rights, source metadata, and package provenance. Both work without JavaScript or network access; unresolved or simplified source constructs remain visibly annotated rather than being silently discarded.

Initial Wget plans ignore ambient proxies and do not follow redirects because Wget's domain filter does not constrain redirect targets. Redirect destinations must be reviewed and configured as explicit seeds until redirect-aware host enforcement is implemented.

## Initial Collections

The first vertical slice targets the [Electronic Text Corpus of Sumerian Literature](https://etcsl.orinst.ox.ac.uk/) (ETCSL).

ETCSL demonstrates why a collection is not equivalent to one website:

```text
ETCSL
|-- Oxford Text Archive record
|-- deposited catalogue and documentation
|-- translations and transliterations
`-- canonical XML corpus and support files
```

The current milestone preserves the complete Oxford Text Archive deposit and its repository record. The historical website interface, search, and CGI behavior are explicitly deferred and are not part of the active completeness boundary.

The ETCSL recipe includes fixture-tested preservation analysis, a derived static reader, and optional Ciao representation rules. The deposited catalogue and canonical corpus contain 394 transliterations and 381 translations; 13 category-0 catalogue compositions are intentionally untranslated. Sibling deposit files supply the ETCSL entity and extension declarations omitted from the inner ZIP, while source-authored XML limitations remain explicit warnings. See [`collections/etcsl/README.md`](collections/etcsl/README.md) for scope and usage.

[GRETIL](https://gretil.sub.uni-goettingen.de/) is the second collection and generalization test. Its recipe separates the current register, eight cumulative corpus packages, 21 separately published dictionaries, and frozen Unicode/CSX/REE documentation; all four reviewed source groups now have verified local captures. Fixture-backed analysis pins the 801-identifier TEI inventory, checks published representation lineages and exact package sets, and validates ZIP safety and CRCs without extraction. Its derived reader renders the complete reviewed TEI corpus one record at a time, preserving item-level rights, source descriptions, mixed content, apparatus, and package provenance. The remaining direct corpus, legacy encoded payloads, historical interfaces, and repository migrations are staged follow-up work; OPAC/eDocs crawling is excluded by their robots policy. See [`collections/gretil/README.md`](collections/gretil/README.md).

The [Internet Sacred Text Archive](https://sacred-texts.com/) is the third collection and scale test. Its first bounded source preserves the complete eight-file 2021 Internet Archive item, including a 1.33 GB WARC and indexes covering 154,080 HTTP records. The collection itself remains incomplete: ISTA's official 9.0 media inventory contains 2,988,233,761 logical bytes across 173,566 files, including representations absent from the WARC. A comprehensive current-site crawl is also deferred because ISTA's terms do not authorize preservation robots and Cloudflare challenges GNU Wget; the project will not bypass either control. See [`collections/sacred-texts/README.md`](collections/sacred-texts/README.md).

## Principles

- Preserve original bytes and derive everything else.
- Preserve canonical data and publication context when practical.
- Keep different editions, translations, formats, and representations distinct.
- Make crawl scope explicit and treat old servers gently.
- Preserve failures rather than presenting partial captures as complete.
- Keep finalized captures immutable.
- Make normalized passages and interpretations traceable to source artifacts.
- Prefer local operation, open formats, and replaceable components.

## Scope

The project is focused on collections whose primary value is textual, scholarly, historical, literary, linguistic, or documentary. Supporting images, audio, metadata, and software are in scope when they are necessary to interpret such a collection.

It is not intended to be a whole-web crawler, social-media archiver, general backup system, piracy tool, or replacement for institutional preservation services. It will not bypass authentication, paywalls, access controls, or rate limits.

## Planned Technology

- Python 3.11+ for configuration, capture coordination, manifests, verification, normalization, and local access.
- GNU Wget with WARC support for the first capture engine.
- SQLite FTS5 for rebuildable catalogues and full-text search.
- Browsertrix for future browser-based capture.
- Optional Ciao Prolog for declarative completeness validation and provenance-aware research queries.
- Optional Org mode integration for linking personal notes to stable preserved passages.

Preservation and verification will not require Ciao, Org mode, embeddings, or an LLM.

## Development

The near-term work is deliberately narrower than the full design:

1. Define capture, manifest, and collection-recipe schemas.
2. Build the Python CLI and GNU Wget capture engine.
3. Implement manifests, fixity verification, statuses, and collection locks.
4. Complete the ETCSL preservation vertical slice.
5. Add derived cataloguing and access features without coupling them to the archive.

The complete architecture, data model, collection studies, testing strategy, and phased roadmap are in [`docs/design.md`](docs/design.md).

## Legal and Ethical Use

This project does not grant rights to captured material. Public access is not automatically permission to republish. Operators are responsible for applicable law, licences, contracts, robots.txt, rate limits, privacy, and collection-specific rights.

Actual captures, private configuration, credentials, logs containing personal information, and personal research workspaces should remain outside the public source repository.

## License

The project source and documentation are licensed under the [MIT License](LICENSE). This licence does not apply to captured material.
