# text-preserver

**Preserve vulnerable digital text collections and the context that makes them intelligible.**

> **Status: early implementation.** Configuration validation, environment diagnostics, dry-run planning, guarded sequential Wget capture, SHA-256 finalization, and verification are available. `LATEST` pointers and collection-specific completeness analysis are not implemented yet.

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
```

`doctor` validates collection and source structure, rejects unsafe scope and credential-bearing arguments, checks GNU Wget WARC support, checks storage locations, and reports available space.

Inspect the exact Wget commands and paths for a collection without writing files or making network requests:

```bash
text-preserver capture COLLECTION_ID \
  --config collections.toml \
  --dry-run
```

Use `--source SOURCE_ID` to select individual sources and `--json` for a machine-readable plan. The test suite executes these plans only against a localhost fixture and verifies recursive mirror, WARC, CDX, log, and redirect behavior.

After reviewing the dry run, execute a supervised capture:

```bash
text-preserver capture example-corpus \
  --config collections.toml \
  --note "Supervised exploratory capture"
```

Execution creates a new capture directory atomically, prevents overlapping captures of the same collection, preserves the input and resolved configuration, records the environment and exact commands, and writes source and collection status records. Failed and interrupted captures remain available for diagnosis. Completed attempts receive `manifest-sha256.json` and `SHA256SUMS`; interrupted attempts remain unfinalized for honest recovery.

Verify a finalized capture:

```bash
text-preserver verify /path/to/capture
```

Verification checks object types, sizes, SHA-256 hashes, missing objects, and unexpected additions. Symlinks are rejected. Captures do not yet update a latest-capture pointer.

Initial Wget plans ignore ambient proxies and do not follow redirects because Wget's domain filter does not constrain redirect targets. Redirect destinations must be reviewed and configured as explicit seeds until redirect-aware host enforcement is implemented.

## First Collection

The first vertical slice targets the [Electronic Text Corpus of Sumerian Literature](https://etcsl.orinst.ox.ac.uk/) (ETCSL).

ETCSL demonstrates why a collection is not equivalent to one website:

```text
ETCSL
|-- historical website and catalogue
|-- translations and transliterations
|-- project documentation
`-- canonical XML corpus deposit
```

The initial milestone is to capture the website and canonical deposit independently, preserve their provenance, verify their bytes, and report whether the expected compositions and representations were captured.

[GRETIL](https://gretil.sub.uni-goettingen.de/) is the proposed second collection and generalization test because it adds multiple languages, legacy encodings, TEI migrations, repository lineages, and representation-specific rights.

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
