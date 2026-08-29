# text-preserver

**Preserve vulnerable digital text collections and the context that makes them intelligible.**

> **Status: early implementation.** Configuration validation, installable API 2 recipes, guarded Wget capture, fixity verification, isolated recipe execution, lifecycle state, deterministic local readers, explicit payload roles, and BagIt/WACZ exports are available.

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

## Guarded Capture

The first preservation engine uses Python and GNU Wget to support:

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
text-preserver collections status etcsl --config collections.toml --json
```

The example configuration references the public recipes as `public:etcsl`, `public:gretil`, and `public:sacred-texts`. Local configuration owns operator identity, storage paths, and capture defaults; recipes own public collection metadata, sources, scope, and analysis settings. External recipe files require supported top-level `recipe_api = 1` or `recipe_api = 2`; inline `[[collections]]` retain version-1 behavior without declaring an API.

An external recipe bundle is bounded by the directory containing its selected recipe file. Capture recursively preserves every regular file under that directory, including TOML, adapters, fixtures, seeds, rules, templates, and documentation. Inline collections instead preserve only declared recipe-relative `inventory_adapter`, `reader_adapter`, `normalizer`, and `ciao_rules` assets; they never sweep the operator configuration directory. Both paths reject symlinks, special files, escapes, more than 1,000 files, files larger than 16 MiB, or more than 64 MiB total. The only exclusions are `__pycache__` directories and `.pyc`, `.pyo`, and `.DS_Store` files. Built-ins are recursively packaged under `text_preserver.builtin_recipes` and resolved with `importlib.resources`.

For `recipe_api = 1`, an inventory adapter exports `analyze_capture(...)`; an optional reader adapter exports `write_static_reader(...)` or `render_static_reader(...)`. Reader generation falls back to `inventory_adapter` when `reader_adapter` is omitted, preserving inline, small, and captured historical recipes. API 2 declares `validator_adapter` and an optional independent `reader_adapter`; they export `validate(ValidationContext) -> ValidationReport` and `build_reader(ReaderContext) -> ReaderReport`. API 2 never infers a reader from its validator. Corpus parsing, package mappings, and source-specific assumptions remain in recipe-local modules.

`doctor` validates collection and source structure, rejects unsafe scope and credential-bearing arguments, checks GNU Wget WARC support, checks storage locations, and reports available space. `collections list/show` display the fully resolved configuration without making requests.

The dependency-free runtime validator remains authoritative for inherited settings, filesystem safety, URL/host relationships, and cross-field references. The distributed JSON Schemas describe the portable document structure. The optional `test` dependencies run representative valid and invalid documents through both implementations to detect structural drift.

Inspect the exact Wget commands and paths for a collection without writing files or making network requests:

```bash
text-preserver capture COLLECTION_ID \
  --config collections.toml \
  --dry-run
```

Use `--source SOURCE_ID` to select individual sources and `--json` for a machine-readable plan. The test suite executes these plans only against a localhost fixture and verifies recursive mirror, WARC, CDX, log, and redirect behavior.

GitHub Actions runs the suite on Linux and macOS with Python 3.11, builds wheel and source distributions, installs the wheel into a clean virtual environment, verifies every packaged public recipe asset, and runs a localhost-only capture → fixity → export → validation → reader flow against the installed package. A development-only Linux job runs Ruff and mypy; built-in recipe bodies are excluded from mypy because their collection-specific configuration payloads remain dynamic and are covered by contract and fixture tests.

After reviewing the dry run, execute a supervised capture:

```bash
text-preserver capture COLLECTION_ID \
  --config collections.toml \
  --note "Supervised exploratory capture"
```

Execution creates a new capture directory atomically, prevents overlapping captures of the same collection, preserves the resolved collection plus `metadata/recipe-bundle/`, records the portable environment and commands, and writes source and collection status records. Schema 3 assigns every file one explicit role: WARC containers and direct deposits are `preservation_original`, converted web mirrors are `capture_derivative`, and fixity/provenance files are `metadata`. Raw configuration, exact executable and filesystem paths, hostname, process ID, operator/contact values, and notes are retained only below `metadata/private/`; portable shareable counterparts remain outside that directory. Each source records regular mirror files/bytes separately from WARC container files/bytes, CDX files and indexed records, and response/resource evidence. The legacy `downloaded_files` and `downloaded_bytes` fields remain aliases for mirror files and bytes only. `metadata/recipe-bundle-manifest.json` records schema and recipe API versions, collection ID, deterministically POSIX-sorted file paths, sizes and SHA-256 hashes, and a canonical bundle SHA-256. Failed and interrupted captures remain available for diagnosis. Completed attempts receive `manifest-sha256.json` and `SHA256SUMS`; interrupted attempts remain unfinalized for honest recovery.

An accepted Wget exit is successful only when at least one mirror file or a WARC response/resource is retained. A nonaccepted exit with such payload is `partial`; without it the source is `failed`. Accepted empty or metadata-only WARC attempts are also `failed`, while interruption remains `interrupted` and still records retained payload metrics.

Verify a finalized capture:

```bash
text-preserver verify /path/to/capture
```

Verification checks object types, sizes, SHA-256 hashes, missing objects, and unexpected additions. Symlinks are rejected. A complete, verified, unfiltered capture updates the collection's portable `LATEST-ACQUIRED` pointer; warning, partial, failed, interrupted, and source-filtered captures never do. The Python/result field `latest_updated` is retained for compatibility and means that this canonical acquisition pointer was updated. Existing `LATEST` files remain readable when `LATEST-ACQUIRED` is absent, but are never rewritten or deleted; malformed canonical pointers fail closed. Each successful source also updates a portable `LATEST-SOURCE_ID` pointer after whole-capture fixity verification, so independently captured sources remain convenient to locate.

Create interoperable exports only after source fixity verification:

```bash
text-preserver export bagit /path/to/capture /path/to/private.bag --profile private
text-preserver export bagit /path/to/capture /path/to/public.bag --profile public
text-preserver export validate-bagit /path/to/public.bag

text-preserver export wacz /path/to/capture /path/to/replay.wacz \
  --profile public --main-page-url https://example.org/
text-preserver export validate-wacz /path/to/replay.wacz
```

The private BagIt profile preserves every capture byte and directory, including the original fixity manifests. The public profile is an explicit built-in allowlist covering preservation payloads, capture derivatives, the recipe manifest/bundle, and portable provenance; it omits source capture/fixity manifests because those enumerate private paths and hashes, and never selects `metadata/private/` or capture logs. Its export record carries the source-manifest digest and a portable capture summary. Both profiles record a capture-to-bag mapping, export-tool version, role, size, and SHA-256, and are validated before atomic publication. WACZ is a derived offline replay package built only from preservation-original WARC containers. It copies WARC bytes exactly, generates bounded CDXJ and page metadata without network access, never modifies the source capture, and has an independent validator that resolves each index entry back to its WARC record.

Run collection-specific completeness analysis after fixity verification:

```bash
text-preserver validate etcsl \
  --config collections.toml
text-preserver validate gretil /path/to/capture-a /path/to/capture-b \
  --config collections.toml
```

With no explicit path, validation verifies and deduplicates `LATEST-ACQUIRED` (or legacy `LATEST` only when the canonical pointer is absent) plus existing pointers for configured source IDs, then deterministically chooses one provider per source, preferring successful sources and newer capture IDs. Arbitrary `LATEST-*` files are not interpreted as source pointers. One or more explicit paths restrict the candidate set to exactly those captures. Cross-capture validation exposes only the selected verified source directories through a temporary aggregate view and uses the current recipe adapter with an explicit warning; single-capture validation prefers the adapter from a verified captured bundle. Its `__file__` remains inside that complete bundle, so recipe-relative runtime fixtures and templates continue to work. Immutable old captures containing only `metadata/recipe-assets/` remain analyzable through an explicitly identified legacy fallback with no bundle digest or recipe API. `text-preserver analyze preservation` remains a deprecated version-0.1 alias with identical output and exit codes; its warning is written only to stderr.

Reports are immutable and input-addressed at `derived/collections/COLLECTION_ID/validations/VALIDATION_ID/report.json`. The identity covers contributing capture manifests, source selection, adapter, effective analysis settings, configuration, the used recipe bundle digest/API, and the execution policy. Preserved adapters verify and identify the captured manifest; current external recipes rescan their complete directory; current inline code rescans only declared assets. Reader metadata records the same current-code identity. Malformed or mismatched manifests are rejected. An identical invocation safely reuses the existing report. `validations/LATEST` records the latest invoked validation, including an incomplete one. Collection-level `LATEST-VALIDATED` advances only for `complete` or `complete_with_warnings` results, so an incomplete run cannot replace the latest usable validation. The ETCSL adapter compares catalogue IDs with deposited XML filenames, checks ZIP safety and CRCs, performs entity-stubbed XML-shell parsing, verifies root IDs, and reports missing entity and DTD dependencies separately from work-level completeness.

Recipe adapters are executable Python and are not a complete sandbox boundary. They run in a separate process through a versioned, bounded JSON protocol with a dedicated output directory, timeout, live output-tree limits, process-group termination, and supported operating-system resource limits. Complete external and captured recipe bundles are copied into a verified temporary snapshot before import, so sibling imports execute the bytes represented by the recorded bundle digest. The worker denies common Python/POSIX socket, subprocess, and capture-write operations by default and reports those language-runtime guards as best effort rather than OS-enforced. These controls substantially limit ordinary adapters, but Python monkey-patching is not equivalent to kernel-enforced network or filesystem isolation; third-party recipe discovery remains disabled.

Build a local static reader from a verified capture:

```bash
text-preserver derive reader etcsl /path/to/capture \
  --config collections.toml
text-preserver derive reader gretil --config collections.toml
```

The capture path may be omitted. Selection prefers `LATEST-ACQUIRED`, then legacy `LATEST` only when canonical is absent, then a configured `reader_source` pointer when no full-capture pointer exists. Reader metadata schema 3 records canonical semantic `build_inputs`, their full SHA-256 `build_key`, and a canonical output-tree digest and counts. The key covers collection and reader schema, the capture manifest, selected source IDs, recipe API/bundle digest, renderer bytes and entry point, expected work count, and the versioned shared access-model and shell sources for API 2 readers; it excludes timestamps, capture IDs, paths, raw configuration, and operator/storage settings. The adapter still runs on every invocation. Matching output reuses the immutable full-key generation; a same-key mismatch or corrupt generation moves the candidate to read-only `reader-quarantine/` with a reproducibility report and changes no pointers.

The unified reader is a composable library rather than one compulsory collection template. ETCSL and GRETIL share the inert document envelope, local base assets, navigation, status, provenance, artifact-reference, and citation components, then add their own layouts and rendering rules. ETCSL keeps its side-by-side transliteration and translation; GRETIL keeps its streamed TEI mixed content, apparatus, item rights, and metadata sidebar. Each generation also contains `access.json`, a typed graph of collection, item, representation, source-supported segment, artifact, relation, rights, citation, and stable route records for future catalogue, search, and research clients. Large segment sets may be referenced from the graph and streamed into a bounded `access-segments.jsonl` index instead of being accumulated in memory.

Every published or reused generation updates the capture-scoped `reader` link. A usable build also updates regular text pointer `derived/collections/COLLECTION_ID/LATEST-READER` and the stable `reader` symlink used by `open reader`; incomplete builds update neither collection indicator. Reading current access prefers and validates `LATEST-READER`, requires it to agree with the symlink, and falls back to the legacy symlink only when canonical metadata is absent.

`text-preserver collections status COLLECTION_ID` is read-only and runs no adapters. It reports acquisition, fixity, validation, and access independently, with no aggregate state; `--json` emits the stable schema-version-1 document.

Open the current reader in the default browser, or print its stable local path for another program such as Emacs EWW:

```bash
text-preserver open reader etcsl --config collections.toml
text-preserver open reader etcsl --config collections.toml --print-only
text-preserver open reader gretil --config collections.toml
```

The ETCSL reader links all compositions to responsive side-by-side transliteration and translation pages. The GRETIL reader streams all 801 reviewed TEI texts into full local work pages with item-level rights, source metadata, and package provenance. Both work without JavaScript or network access; unresolved or simplified source constructs remain visibly annotated rather than being silently discarded.

Sacred Texts provides a deliberately incomplete preservation-status reader rather than WARC replay or invented work records. Because incomplete readers never replace a current usable reader, build it against an explicit verified capture and open the reported capture-scoped path:

```bash
text-preserver derive reader sacred-texts /path/to/capture --config collections.toml
```

After building compatible current readers, derive and use the immutable common catalogue and its representation-level SQLite FTS5 index:

```bash
text-preserver derive catalogue --config collections.toml
text-preserver open catalogue --config collections.toml
text-preserver search '"divine kingship"' --config collections.toml
text-preserver search 'buddha*' --collection gretil --language sa-Latn --limit 20
```

The catalogue records unavailable configured collections without treating them as complete. Search indexes only text inside each recipe-declared representation route, excluding shared navigation, provenance sidebars, citations, scripts, and styles. Results link to the exact immutable reader generation used by the catalogue. `catalogue.sqlite`, `catalogue.json`, and the static catalogue are rebuildable derivatives under `derived/catalogue-generations/`; `LATEST-CATALOGUE` advances only for a usable catalogue.

Wget plans ignore ambient proxies and native redirects because Wget's domain filter does not constrain redirect targets. Redirect responses retained in WARC are recorded as proposals without being followed. A follow-up request occurs only when the exact `(from, to)` pair is declared in that source's `reviewed_redirects`; every hop is bounded, checked independently, and executed as another redirect-disabled request. Unreviewed and unsafe targets remain provenance only.

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

The ETCSL recipe includes fixture-tested preservation analysis, a derived static reader, and optional Ciao representation rules. The deposited catalogue and canonical corpus contain 394 transliterations and 381 translations; 13 category-0 catalogue compositions are intentionally untranslated. Sibling deposit files supply the ETCSL entity and extension declarations omitted from the inner ZIP, while source-authored XML limitations remain explicit warnings. See [`src/text_preserver/builtin_recipes/etcsl/README.md`](src/text_preserver/builtin_recipes/etcsl/README.md) for scope and usage.

[GRETIL](https://gretil.sub.uni-goettingen.de/) is the second collection and generalization test. Its recipe separates the current register, eight cumulative corpus packages, 21 separately published dictionaries, and frozen Unicode/CSX/REE documentation; all four reviewed source groups now have verified local captures. Fixture-backed analysis pins the 801-identifier TEI inventory, checks published representation lineages and exact package sets, and validates ZIP safety and CRCs without extraction. Its derived reader renders the complete reviewed TEI corpus one record at a time, preserving item-level rights, source descriptions, mixed content, apparatus, and package provenance. The remaining direct corpus, legacy encoded payloads, historical interfaces, and repository migrations are staged follow-up work; OPAC/eDocs crawling is excluded by their robots policy. See [`src/text_preserver/builtin_recipes/gretil/README.md`](src/text_preserver/builtin_recipes/gretil/README.md).

The [Internet Sacred Text Archive](https://sacred-texts.com/) is the third collection and scale test. Its first bounded source preserves the complete eight-file 2021 Internet Archive item, including a 1.33 GB WARC and indexes covering 154,080 HTTP records. The collection itself remains incomplete: ISTA's official 9.0 media inventory contains 2,988,233,761 logical bytes across 173,566 files, including representations absent from the WARC. A comprehensive current-site crawl is also deferred because ISTA's terms do not authorize preservation robots and Cloudflare challenges GNU Wget; the project will not bypass either control. See [`src/text_preserver/builtin_recipes/sacred-texts/README.md`](src/text_preserver/builtin_recipes/sacred-texts/README.md).

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

## Technology

- Python 3.11+ for configuration, capture coordination, manifests, verification, normalization, and local access.
- GNU Wget with WARC support for the first capture engine.
- SQLite FTS5 for rebuildable representation-level catalogues and full-text search.
- Browsertrix for future browser-based capture.
- Optional Ciao Prolog for declarative completeness validation and provenance-aware research queries.
- Optional Org mode integration for linking personal notes to stable preserved passages.

Preservation and verification will not require Ciao, Org mode, embeddings, or an LLM.

## Development

Development is preservation-gated: stabilize preservation contracts first, build deterministic access on those contracts, and add research clients only after stable identifiers exist.

The complete architecture, data model, collection studies, and testing strategy are in [`docs/design.md`](docs/design.md). The canonical status-tracked implementation sequence, exit criteria, critique traceability, and version 0.1 release gate are in [`docs/roadmap.md`](docs/roadmap.md).

## Legal and Ethical Use

This project does not grant rights to captured material. Public access is not automatically permission to republish. Operators are responsible for applicable law, licences, contracts, robots.txt, rate limits, privacy, and collection-specific rights.

Actual captures, private configuration, credentials, logs containing personal information, and personal research workspaces should remain outside the public source repository.

## License

The project source and documentation are licensed under the [MIT License](LICENSE). This licence does not apply to captured material.
