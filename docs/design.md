# text-preserver design document

**Preserve vulnerable digital text collections and the context that makes them intelligible.**

> **Status: early implementation.** Configuration validation, installable public recipes, `doctor`, `collections list/show`, guarded Wget capture, fixity verification, conservative latest-capture pointers, ETCSL catalogue/deposit completeness analysis, and a localhost integration harness exist. Other command examples describe the target interface unless explicitly marked as implemented.

`text-preserver` is a preservation-first system for scholarly corpora, digital editions, historical text archives, specialist bibliographies, old academic websites, and other text-centered collections that may become unavailable or difficult to reconstruct.

Its primary responsibility is to create **verifiable, reproducible, and intelligible preservation packages**. Search, normalization, logical validation, a unified reader, personal annotations, and semantic analysis are valuable secondary layers, but they must remain downstream of the preservation masters.

The central invariant is:

```text
preservation masters  →  derived data  →  personal workspace
       immutable          rebuildable         editable
```

Nothing in the analysis or research layers may silently alter the preserved evidence.

---

## Contents

- [Mission](#mission)
- [Why this project exists](#why-this-project-exists)
- [Why “text-preserver”?](#why-text-preserver)
- [Goals](#goals)
- [Non-goals](#non-goals)
- [Guiding principles](#guiding-principles)
- [Core model](#core-model)
- [System architecture](#system-architecture)
- [Preservation layer](#preservation-layer)
- [Derived and analysis layers](#derived-and-analysis-layers)
- [Unified reader](#unified-reader)
- [Org mode and personal knowledge](#org-mode-and-personal-knowledge)
- [Python and Ciao Prolog](#python-and-ciao-prolog)
- [Planned command-line interface](#planned-command-line-interface)
- [Configuration model](#configuration-model)
- [Preservation package layout](#preservation-package-layout)
- [Collection recipes](#collection-recipes)
- [Initial collections](#initial-collections)
  - [ETCSL: first vertical slice](#etcsl-first-vertical-slice)
  - [GRETIL: second collection and generalization test](#gretil-second-collection-and-generalization-test)
  - [Sacred Texts and later candidates](#sacred-texts-and-later-candidates)
- [Preservation analysis](#preservation-analysis)
- [Research analysis](#research-analysis)
- [Normalized text model](#normalized-text-model)
- [Annotations and claims](#annotations-and-claims)
- [Integrity, provenance, and completeness](#integrity-provenance-and-completeness)
- [Special cases and limitations](#special-cases-and-limitations)
- [Requirements](#requirements)
- [Proposed repository structure](#proposed-repository-structure)
- [Development plan](#development-plan)
- [Testing strategy](#testing-strategy)
- [Storage, retention, and backup](#storage-retention-and-backup)
- [Legal and ethical use](#legal-and-ethical-use)
- [Security and privacy](#security-and-privacy)
- [Public repository policy](#public-repository-policy)
- [Contributing](#contributing)
- [License](#license)
- [References](#references)

---

## Mission

> **Preserve original digital text collections, verify what was captured, and make the preserved material usable without confusing access derivatives or personal interpretation with the archival record.**

The project has three progressively optional responsibilities:

```text
1. Preserve
   Capture original sites, files, datasets, metadata, and publication context.

2. Understand
   Inventory collections, identify works and representations, validate
   completeness, and derive searchable structure.

3. Research
   Read, search, compare, annotate, connect passages to personal notes,
   and reason over explicitly represented claims.
```

Only the first responsibility is required for the project to succeed.

A collection must remain preservable and verifiable even when:

- the frontend is removed;
- the search index is deleted;
- the normalizer is replaced;
- Ciao is not installed;
- an embedding model is discontinued;
- the user's private Org repository is unavailable.

---

## Why this project exists

Many important digital collections are maintained by one scholar, a small research group, or an old university department. They may remain online for decades and then disappear because of:

- an expired domain or abandoned hosting account;
- retirement or death of the original maintainer;
- the end of project funding;
- obsolete PHP, CGI, Perl, Java, database, or server dependencies;
- a university redesign that removes old paths;
- incomplete migration to a new repository;
- broken character encodings or missing auxiliary files;
- accidental deletion, corruption, or ransomware;
- a live interface surviving after its canonical dataset has become hard to find;
- a dataset surviving while its documentation, navigation, or editorial context disappears.

A recursive download is useful, but a directory produced by `wget -r` is not yet a preservation system. It may:

- miss texts exposed only through a catalogue or form;
- crawl an unbounded space of query parameters;
- leave no precise record of its scope or configuration;
- modify links without preserving the original HTTP responses;
- fail partially without a trustworthy status;
- conflate several representations of one intellectual work;
- omit a canonical XML or ZIP deposit hosted elsewhere;
- provide no fixity manifest;
- provide no way to determine whether a later capture lost material.

`text-preserver` treats preservation as a repeatable, inspectable process rather than as a one-off download.

---

## Why “text-preserver”?

The project preserves **textual collections**, not merely websites.

“Text” here identifies the intellectual object, not a file extension. A textual collection may contain:

- HTML;
- plain text;
- TEI/XML or other XML vocabularies;
- SGML;
- PDF and EPUB editions;
- scans and manuscript images;
- transliterations and translations;
- dictionaries, concordances, and catalogues;
- bibliographic metadata;
- CSS, fonts, JavaScript, and interface images;
- schemas, stylesheets, and software needed to interpret the data;
- WARC records representing the historical web publication.

The web is one acquisition source. It is not the conceptual boundary of the project.

This is why the fundamental unit is a **collection**, not a website:

```text
Collection
├── historical website
├── canonical dataset
├── institutional repository record
├── source-code repository
├── documentation
└── auxiliary representations
```

---

## Goals

### Preservation goals

- Capture public text-centered collections conservatively and reproducibly.
- Preserve both canonical data and publication context when possible.
- Store original HTTP exchanges in WARC, not only converted mirrors.
- Preserve direct files such as TEI/XML, ZIP, PDF, EPUB, CSV, or database exports.
- Record the exact seeds, scope, commands, tool versions, timestamps, and outcomes.
- Produce cryptographic fixity manifests.
- Retain partial and interrupted captures honestly.
- Compare captures without rewriting older preservation packages.
- Permit collection-specific completeness rules.
- Keep archive masters independent from every later analysis tool.

### Access and research goals

- Build a unified local catalogue across heterogeneous collections.
- Search normalized text while retaining links back to the exact source artifact.
- Present translations, transliterations, editions, and source formats together.
- Support stable passage identifiers.
- Attach annotations and personal Org notes to preserved passages.
- Represent structured scholarly statements as provenance-aware claims.
- Allow optional Ciao rules to validate preservation and query the claim graph.
- Provide a local-first reader that can be rebuilt from preserved data.

---

## Non-goals

`text-preserver` is not intended to be:

- a crawler for the entire public web;
- a replacement for Internet Archive, national libraries, or institutional repositories;
- a mass piracy or republication system;
- a general backup tool for arbitrary personal files;
- a social-media archiver;
- a video-preservation platform;
- a universal ontology for the humanities;
- an automatic system for deciding whether a historical claim is true;
- an LLM application that treats generated summaries as archival evidence;
- a tool for bypassing authentication, paywalls, access controls, or rate limits.

The project may preserve images, audio, or software when they are necessary parts of a textual collection, but it should not lose its text-centered scope.

---

## Guiding principles

### 1. Preservation comes first

The archive must remain useful without the analysis layer. Features are prioritized in this order:

```text
capture → fixity → provenance → completeness → access → analysis
```

### 2. Preserve originals and derive everything else

Original downloaded bytes, WARC records, repository deposits, logs, and capture metadata belong to the preservation package.

Normalized text, database indexes, generated HTML, extracted entities, embeddings, and Ciao fact files are derivatives. They must be reproducible and replaceable.

### 3. Preserve both data and context

A canonical TEI corpus may be more important than the historical website, but the website may preserve:

- editorial explanations;
- navigation;
- bibliographies;
- relationships between texts;
- project history;
- search conventions;
- generated translations or transliterations;
- evidence about how the corpus was originally presented.

When practical, preserve both.

### 4. Do not flatten different representations

A TEI source, an HTML transformation, a translation, a transliteration, a PDF edition, and a scan are not interchangeable files.

They should be connected as representations of a work or version, not collapsed into a single “document.”

### 5. Make scope explicit

Recursive crawling should remain inside exact allowed hosts and URL patterns. Cross-host crawling must be opt-in. Institutional repositories, CDNs, and source deposits should usually become separate sources rather than broadening one crawl indiscriminately.

### 6. Treat old servers gently

The default policy should use:

- a descriptive User-Agent and contact address;
- one conservative request stream;
- delays and randomized waiting;
- finite retries;
- finite exploratory quotas;
- robots.txt compliance;
- supervised first captures.

Completing a crawl quickly is never more important than keeping a fragile origin server healthy.

### 7. Preserve failures honestly

A nonzero crawler exit code, missing required source, invalid XML document, or incomplete inventory must not be converted silently into success.

Partial captures may still have evidential value and should normally be retained.

### 8. Keep finalized captures immutable

A capture is a dated preservation event. Do not manually “fix” files inside it. Improvements belong in a new capture or in rebuildable derived data.

### 9. Make every interpretation traceable

A normalized passage, annotation, extracted entity, logical fact, or LLM-generated proposal should point back to:

- a collection;
- a work or representation;
- a capture;
- a source artifact hash;
- a source locator or quotation.

### 10. Prefer local-first and open formats

The core archive should use ordinary files, WARC, JSON, TOML, XML, SQLite, and other documented formats. The project should not require a hosted service for preservation or local reading.

---

## Core model

### Collection

A **collection** is the logical preservation target.

Examples:

- the Electronic Text Corpus of Sumerian Literature;
- GRETIL;
- the Internet Sacred Text Archive;
- a digital critical edition;
- an old university corpus and its institutional deposit.

A collection may span several domains and repositories.

### Source

A **source** is an independently capturable component or representation of a collection.

Examples:

- a recursive public website;
- a CGI catalogue;
- a direct TEI/XML ZIP;
- a repository landing page;
- a Git repository;
- an IIIF manifest;
- an OAI-PMH endpoint;
- a browser-rendered application.

Each source has its own:

- seeds;
- engine;
- scope;
- rate limits;
- rights notes;
- required/optional status;
- validation rules;
- capture result.

### Capture

A **capture** is a dated attempt to preserve all or selected sources of a collection.

Proposed statuses:

```text
complete
complete_with_warnings
partial
interrupted
failed
```

A source-filtered or partial capture must not silently replace the latest successful full capture.

### Artifact

An **artifact** is a captured file or WARC record with identity and provenance.

Typical properties include:

```text
artifact ID
source ID
capture ID
original URL
retrieval timestamp
media type
byte length
SHA-256
HTTP status and headers
local preservation path
```

### Work

A **work** is an intellectual object represented in the collection.

Examples:

- one Sumerian composition;
- the Sāṃkhyakārikā;
- one hymn;
- one bibliographic record;
- one dictionary entry, when the collection models entries independently.

### Text version

A **text version** is a particular edition, recension, translation, transliteration, transcription, or commentary.

### Representation

A **representation** is a concrete encoding or publication of a version:

- TEI/XML;
- HTML;
- plain text;
- PDF;
- scan;
- EPUB;
- database record.

### Segment

A **segment** is an addressable part of a representation:

- book;
- chapter;
- tablet;
- hymn;
- verse;
- line;
- paragraph;
- entry;
- generated text span.

### Annotation and claim

An **annotation** connects a body—such as a note, tag, or comment—to a target passage or artifact.

A **claim** is a structured assertion with provenance, status, and evidence. Claims belong to the research workspace, not to the preservation master.

---

## System architecture

```text
                            collection recipes
                      TOML + seeds + adapters + rules
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        PRESERVATION CORE                            │
│                              Python                                │
│                                                                     │
│  configuration → scope → capture engines → logs → manifests        │
│                         │                                           │
│            ┌────────────┴────────────┐                              │
│            ▼                         ▼                              │
│        GNU Wget                  Browsertrix                        │
│      web + direct files        dynamic web, future                 │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
                     immutable archive masters
               WARC / original files / metadata / hashes
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         DERIVED LAYER                               │
│                              Python                                │
│                                                                     │
│ inventory → normalization → passage map → SQLite FTS → facts       │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
             ┌──────────────────┴──────────────────┐
             ▼                                     ▼
┌────────────────────────────┐        ┌──────────────────────────────┐
│   PRESERVATION ANALYSIS    │        │      RESEARCH WORKSPACE      │
│ Python + optional Ciao     │        │ Org notes + annotations      │
│ completeness, consistency │        │ claims, saved queries        │
└──────────────┬─────────────┘        └──────────────┬───────────────┘
               └──────────────────┬──────────────────┘
                                  ▼
                         unified local reader
                 catalogue / search / compare / cite
```

The persistent local data is divided into three roots:

```text
archive/
    Immutable preservation masters.

derived/
    Rebuildable catalogues, normalized text, indexes, facts,
    reports, generated access files, and embeddings.

workspace/
    Editable personal annotations, Org mappings, claims,
    reading state, and saved searches.
```

The allowed dependency direction is:

```text
archive → derived → workspace
```

The archive must never depend on the derived or workspace layers.

---

## Preservation layer

The preservation layer is the permanent core of the project.

### Initial capture engines

#### GNU Wget

The first engine should support:

- recursive HTTP/HTTPS capture;
- nonrecursive direct-file capture;
- local mirror generation;
- WARC output;
- CDX generation;
- conservative rate and scope controls;
- crawler logs;
- explicit seeds;
- capture metadata.

GNU Wget is suitable for older, server-rendered websites such as ETCSL.

#### Browsertrix

Browsertrix should be added later for sites that require:

- JavaScript execution;
- client-side routing;
- scrolling or interactions;
- dynamic API calls;
- service workers;
- browser-based replay quality assurance.

The Browsertrix adapter must produce the same collection-level provenance and status model as the Wget adapter.

### Preservation objects

A complete capture may include:

- compressed WARC files;
- CDX or CDXJ indexes;
- locally browsable mirrors;
- direct source deposits;
- repository landing pages;
- original XML/TEI and schemas;
- seed files;
- crawler logs;
- resolved configuration;
- environment and tool versions;
- capture and source result records;
- SHA-256 manifests.

The WARC is the primary preservation object for web capture. A converted mirror is a convenient access derivative included in the preservation package, but it is not a byte-identical substitute for the original HTTP responses.

---

## Derived and analysis layers

Derived data is useful but disposable. It should always record enough provenance to be rebuilt.

Examples:

```text
collection inventory
work/version/representation catalogue
normalized Unicode text
segment boundaries
TEI validation reports
HTML-to-source mappings
full-text index
concordance tables
cross-reference graph
Ciao fact files
change reports
embeddings
generated reader pages
```

Every derived object should record at least:

```text
input artifact hashes
capture ID
normalizer or analyzer version
configuration version
creation timestamp
```

Deleting `derived/` must not damage the archive.

---

## Unified reader

A unified frontend is a natural long-term access layer, provided that it unifies **operations**, not source formats.

The reader should not pretend that all collections share the same textual structure. Instead, it should expose a common model while preserving collection-specific detail.

### Proposed common hierarchy

```text
Collection
└── Work
    └── TextVersion
        └── Representation
            └── Segment
                └── Artifact
```

### Initial reader features

- browse collections and works;
- full-text search across selected collections;
- filter by language, genre, author, collection, version, or representation;
- open the exact archived source;
- replay the corresponding WARC record;
- show capture date, URL, hash, licence note, and provenance;
- display normalized text with stable segment links;
- show translation and transliteration in parallel;
- compare editions or captures;
- export a citation;
- create an Org note from a selected passage;
- display linked annotations and claims.

A conceptual view:

```text
┌──────────────────────────────────────────────────────────────────┐
│ Sāṃkhyakārikā — kārikā 1                                        │
├──────────────────────────────┬───────────────────────────────────┤
│ Sanskrit / transliteration   │ Translation                       │
│                              │                                   │
│ duḥkhatrayābhighātāj ...     │ From the torment caused by ...   │
├──────────────────────────────┴───────────────────────────────────┤
│ Collection: GRETIL · Representation: TEI · Capture: 2026-...     │
│ Source hash: ... · [Original XML] [Legacy HTML] [Compare]        │
├──────────────────────────────────────────────────────────────────┤
│ Personal notes · related claims · cited passages                 │
└──────────────────────────────────────────────────────────────────┘
```

### Frontend constraints

- It should run locally and bind to `127.0.0.1` by default.
- Archive files should be mounted read-only.
- The first version should work without JavaScript-heavy client infrastructure.
- Search and rendering indexes should live in `derived/`.
- User annotations should live in `workspace/`.
- The reader must visibly distinguish original text, normalized text, personal notes, and machine-generated proposals.

---

## Org mode and personal knowledge

A personal Org repository can become a valuable research layer without being absorbed into the archive.

The Org files should remain the user's editable source of truth. Import into `text-preserver` should be explicit and opt-in.

### Stable links from Org to preserved passages

A custom Org link type could use identifiers such as:

```org
[[tp:etcsl/work/c.1.1.1/translation/segment/12]
 [ETCSL c.1.1.1, translation, segment 12]]
```

Following the link could open the local reader at the exact preserved passage.

The link resolver should use a durable internal identifier, not only the original live URL.

### Capture from the reader into Org

A “Create Org note” action could invoke `org-protocol` and prefill:

```org
* %^{Title}
:PROPERTIES:
:TP_TARGET:     tp:etcsl/work/c.1.1.1/translation/segment/12
:TP_CAPTURE:    2026...
:TP_ARTIFACT:   sha256:...
:TP_SOURCE_URL: https://...
:END:

#+begin_quote
Selected passage
#+end_quote
```

This creates bidirectional navigation:

```text
preserved passage → Org note
Org note → preserved passage
```

### Selective export

Only entries marked for export should enter the structured workspace:

```org
* The three forms of duḥkha
:PROPERTIES:
:ID:         7db2...
:TP_EXPORT:  annotation
:TP_TARGET:  tp:gretil/work/samkhyakarika/version/main/segment/1
:END:

My note...
```

The system should never infer that every Org heading is an ontological entity.

---

## Python and Ciao Prolog

The project should use **Python as the implementation language of the preservation system** and **Ciao as an optional logical analysis engine**.

### Why Python owns the core

The capture and access layers require mature support for:

- filesystems and processes;
- HTTP tools and browser automation;
- WARC/WACZ libraries;
- TOML, JSON, XML, HTML, and TEI processing;
- SQLite;
- hashing;
- web application frameworks;
- NLP and embeddings;
- cross-platform packaging.

Reimplementing that infrastructure in Prolog would slow preservation work without improving the archive.

### Where Ciao adds value

Preservation completeness and scholarly relationships are naturally declarative.

Python can extract facts:

```prolog
composition('c.1.1.1').
captured_representation('c.1.1.1', translation).
captured_representation('c.1.1.1', transliteration).
captured_representation('c.1.1.1', xml).
```

Ciao can express collection-specific rules:

```prolog
required_representation(C, translation) :-
    composition(C).

required_representation(C, transliteration) :-
    composition(C).

missing_representation(C, Kind) :-
    required_representation(C, Kind),
    \+ captured_representation(C, Kind).
```

A collection recipe can then ask:

```prolog
?- missing_representation(Composition, Kind).
```

This is more maintainable than adding collection-specific `if` statements to the crawler.

### Optional dependency

Ciao must not be required for:

```text
capture
manifest generation
fixity verification
basic inventory
local replay
```

It may be required for:

```text
collection-specific logical validation
explainable completeness reports
claim-graph queries
consistency and dependency checks
```

### Two different reasoning regimes

Preservation validation is often closed-world:

```prolog
missing(Item) :-
    expected(Item),
    \+ captured(Item).
```

If a finite collection inventory says an item should exist and it is absent, that is a preservation failure.

Historical and philosophical reasoning should not use the same assumption. Failure to prove a claim is not proof of its negation.

Use explicit, attributed claims:

```prolog
claim(c1, person(laozi), author_of, work(daodejing)).
status(c1, traditional_attribution).
evidence(c1, passage(...)).

claim(c2, work(daodejing), composition_type, composite).
status(c2, scholarly_hypothesis).
evidence(c2, passage(...)).

contrasts_with(c1, c2).
```

Ciao can identify relationships and tensions without pretending to settle them automatically.

---

## Planned command-line interface

The CLI foundation, `doctor`, `collections list/show`, sequential `capture` execution, and `verify` exist. The remaining commands define the intended interface.

The initial Wget planner disables ambient proxies and redirects. GNU Wget's domain filter does not constrain redirect destinations, so redirect following cannot be enabled until every redirect hop can be checked against the source allowlist.

### Environment and configuration

```bash
text-preserver doctor -c collections.toml
text-preserver collections list -c collections.toml
text-preserver collections show etcsl -c collections.toml
```

### Capture and verification

```bash
text-preserver capture etcsl -c collections.toml
text-preserver capture etcsl --source ota-dataset
text-preserver capture --all
text-preserver capture etcsl --dry-run

text-preserver verify archive/collections/etcsl
text-preserver compare etcsl CAPTURE_A CAPTURE_B
```

### Derived catalogue and analysis

```bash
text-preserver catalog build etcsl
text-preserver normalize etcsl
text-preserver analyze preservation etcsl
text-preserver analyze logic etcsl
text-preserver search "kingship"
```

### Reader and workspace

```bash
text-preserver serve
text-preserver org export /path/to/notes
text-preserver workspace validate
```

### Intended command guarantees

- `capture` writes only to a new capture directory.
- `verify` never modifies a capture.
- `normalize` writes only to `derived/`.
- `serve` treats `archive/` as read-only.
- `org export` imports only explicitly marked entries.
- destructive retention commands, if added, must require explicit confirmation and understand WARC dependency chains.

---

## Configuration model

Configuration should use TOML and distinguish collections from sources.

A private operator configuration may reference public collection recipes:

```toml
recipes = ["public:etcsl"]
```

`public:ID` references resolve recipes shipped with the source or installed distribution. Filesystem recipe paths are resolved relative to the operator configuration. A recipe contains one `[collection]` table and is validated with the same inheritance and strict-key rules as an inline `[[collections]]` table. Captures preserve the operator input, selected recipe input, and configured recipe-relative analysis assets so later analysis can use the same adapter bytes.

```toml
[project]
archive_root = "./data/archive"
derived_root = "./data/derived"
workspace_root = "./data/workspace"

operator = "Your name or organization"
contact = "mailto:you@example.org"
user_agent = "text-preserver/0.1 (+mailto:you@example.org)"

[defaults.capture]
engine = "wget"
robots = true
wait = 1.0
random_wait = true
timeout = 30
tries = 3
retry_connrefused = true
span_hosts = false
warc = true
warc_cdx = true
mirror = true
quota = "2G"
```

### Collection

```toml
[[collections]]
id = "example-corpus"
title = "Example Corpus"
homepage = "https://example.org/"
description = "A scholarly corpus and its historical publication interface."
risk_note = "Why the collection may be vulnerable or hard to reconstruct."
rights_note = "Known collection-level rights information; do not overgeneralize."
tags = ["philology", "digital-humanities"]
enabled = false
```

### Recursive web source

```toml
[[collections.sources]]
id = "web"
kind = "web"
title = "Historical public website"
required = true
engine = "wget"

seeds = [
  "https://example.org/",
  "https://example.org/catalogue.html",
]

allowed_hosts = ["example.org"]

[collections.sources.capture]
recursive = true
level = "inf"
page_requisites = true
convert_links = true
adjust_extension = true
quota = "2G"
```

### Direct canonical deposit

```toml
[[collections.sources]]
id = "canonical-dataset"
kind = "http-file"
title = "Canonical institutional deposit"
required = true

seeds = [
  "https://repository.example.org/bitstream/corpus.zip",
]

allowed_hosts = ["repository.example.org"]

[collections.sources.capture]
recursive = false
page_requisites = false
convert_links = false
content_disposition = true
quota = "500M"
```

### Collection-specific analysis

```toml
[collections.analysis]
inventory_adapter = "collections/example-corpus/inventory.py"
normalizer = "collections/example-corpus/normalize.py"
ciao_rules = "collections/example-corpus/rules/preservation.pl"

expected_work_count = 100
required_representation_kinds = ["source", "metadata"]
```

The exact schema will evolve during implementation, but the hierarchy should remain:

```text
project defaults
    ↓
collection settings
    ↓
source settings
```

Collection-specific knowledge should live in recipes, adapters, and rules rather than in hard-coded branches in the generic engine.

---

## Preservation package layout

A proposed full capture:

```text
data/
├── archive/
│   ├── locks/
│   │   └── etcsl.lock
│   ├── runs/
│   │   └── 20260827T120000Z-a1b2c3.json
│   └── collections/
│       └── etcsl/
│           ├── collection.json
│           ├── LATEST
│           ├── latest -> captures/20260827T120000Z-a1b2c3
│           └── captures/
│               └── 20260827T120000Z-a1b2c3/
│                   ├── capture.json
│                   ├── manifest-sha256.json
│                   ├── SHA256SUMS
│                   ├── metadata/
│                   │   ├── environment.json
│                   │   ├── input-config.toml
│                   │   └── resolved-collection.json
│                   └── sources/
│                       ├── web/
│                       │   ├── seeds.txt
│                       │   ├── mirror/
│                       │   ├── warc/
│                       │   ├── logs/
│                       │   │   └── crawler.log
│                       │   └── metadata/
│                       │       ├── command.json
│                       │       ├── resolved-source.json
│                       │       └── result.json
│                       └── canonical-dataset/
│                           └── ...
│
├── derived/
│   └── collections/
│       └── etcsl/
│           ├── catalogue.sqlite
│           ├── normalized/
│           ├── passage-map.jsonl
│           ├── facts/
│           │   └── capture.pl
│           └── reports/
│               ├── completeness.json
│               └── changes.html
│
└── workspace/
    ├── annotations/
    ├── claims/
    ├── org-index/
    └── saved-searches/
```

Capture IDs should use UTC plus a collision-resistant suffix:

```text
20260827T120000Z-a1b2c3
```

The `LATEST` pointer is a convenience. It is not the archive.

---

## Collection recipes

A collection recipe contains the knowledge needed to capture and understand one collection without contaminating the generic engine.

Proposed layout:

```text
collections/
└── etcsl/
    ├── collection.toml
    ├── README.md
    ├── seeds/
    │   ├── web.txt
    │   └── repository.txt
    ├── inventory.py
    ├── normalize.py
    ├── checks.toml
    ├── rules/
    │   ├── preservation.pl
    │   └── claims.pl
    └── fixtures/
```

A recipe may define:

- authoritative descriptions and identifiers;
- capture sources and scope;
- known catalogues or sitemaps;
- expected item counts;
- URL exclusions;
- representation mappings;
- encodings;
- rights notes;
- inventory extraction;
- source-to-work mappings;
- normalization rules;
- completeness assertions;
- local tests and fixtures.

Public recipes should never contain credentials or copyrighted capture payloads.

---

## Initial collections

### ETCSL: first vertical slice

The [Electronic Text Corpus of Sumerian Literature](https://etcsl.orinst.ox.ac.uk/) is the ideal first implementation target.

Its site describes a selection of nearly 400 Sumerian literary compositions with transliterations, English prose translations, and bibliographic information. It also states that project funding ended in 2006 and that no current editorial work is being done on the site. The [ETCSL manual](https://etcsl.orinst.ox.ac.uk/edition2/etcslmanual.php) states that the XML source files can be downloaded from the Oxford Text Archive.

ETCSL therefore demonstrates the collection/source distinction:

```text
ETCSL collection
├── historical web interface
│   ├── catalogue
│   ├── translations
│   ├── transliterations
│   ├── search and navigation
│   └── project documentation
└── canonical deposited corpus
    ├── XML/TEI source
    ├── entities and schemas
    └── repository metadata
```

#### ETCSL preservation questions

- Did every catalogue composition produce a captured work?
- Does every composition have a translation?
- Does every composition have a transliteration?
- Are bibliographic pages and project documentation present?
- Can every XML source still be parsed?
- Are all required entity files available?
- Can each web representation be mapped to its source record?
- Which URLs or records changed between captures?

#### ETCSL vertical-slice acceptance criteria

The first meaningful release should be able to:

1. capture the website conservatively;
2. capture the canonical Oxford deposit separately;
3. create WARC, mirror, logs, and manifests;
4. extract the composition inventory;
5. map translations and transliterations to compositions;
6. generate a completeness report;
7. export facts for a small Ciao validation program;
8. normalize at least one representation into stable segments;
9. index those segments in SQLite;
10. serve a local reader page;
11. create a stable Org link to a passage.

This one collection can validate the entire architecture without prematurely building a universal system.

---

### GRETIL: second collection and generalization test

The second target should be **GRETIL — the Göttingen Register of Electronic Texts in Indian Languages**.

GRETIL is especially valuable as an architectural test because it contains heterogeneous text traditions, languages, encodings, formats, and publication histories.

The current [GRETIL TextGrid project page](https://textgridrep.org/project/TGPR-2ba9cb1b-9602-202d-71ce-67e63a29de55) explains that:

- GRETIL began as a register and became a platform for securing and documenting machine-readable texts;
- older non-Unicode CSX and REE formats were discontinued;
- work on integration into long-term repositories began in 2024;
- existing TEI files were validated and enriched;
- remaining HTML files were converted to TEI;
- the new TextGrid structure preserves the collection organization;
- the previous cumulative ZIP files were deposited separately in DARIAH-DE;
- those legacy ZIPs intentionally preserve the older corpus rather than the newer improvements;
- the former e-library is being handled separately while the legal status of individual files is reviewed.

That makes GRETIL more than “another website to mirror.” It is a collection with several preservation lineages:

```text
GRETIL collection
├── legacy public website
├── legacy HTML and historical encodings
├── current TEI representations
├── HTML generated from TEI
├── TextGrid repository objects
├── DARIAH-DE cumulative legacy ZIPs
├── metadata mappings and vocabularies
└── e-library records and files with separate rights review
```

Individual transformed GRETIL pages explicitly identify their source XML and record the legacy file from which they were mass-converted. This is exactly the type of provenance chain that `text-preserver` should preserve rather than flatten.

#### Why GRETIL is the right second collection

ETCSL is relatively regular. GRETIL tests whether the model survives:

- multiple languages and scripts;
- Unicode and legacy encodings;
- many genres and subcollections;
- heterogeneous metadata quality;
- one work represented in legacy HTML, TEI, generated HTML, and ZIP deposits;
- repository migrations;
- per-item rights and provenance;
- mappings to external authority vocabularies;
- incomplete or ongoing modernization.

#### GRETIL preservation questions

- Which legacy files correspond to which current TEI files?
- Has every legacy work been migrated?
- Which files exist only in a cumulative ZIP?
- Which TEI documents validate against the declared schema?
- Does generated HTML identify its source XML?
- Which metadata fields were added during migration?
- Which original headers were retained for transparency?
- Which representations have explicit licences?
- Which e-library files remain metadata-only pending legal review?
- Are different files representations of one work or genuinely different versions?

#### Proposed GRETIL recipe

```text
collections/gretil/
├── collection.toml
├── README.md
├── seeds/
│   ├── legacy-site.txt
│   ├── textgrid.txt
│   └── dariah-de.txt
├── inventory.py
├── normalize_tei.py
├── map_legacy_to_tei.py
├── encodings.py
├── checks.toml
├── rules/
│   └── preservation.pl
└── fixtures/
```

Potential sources should be enabled gradually:

```text
gretil
├── legacy-web
├── textgrid-project
├── dariah-legacy-zips
├── generated-html
└── edocs-e-library
```

The first GRETIL milestone should not attempt to capture everything immediately. It should select one subcollection and prove the mapping between:

```text
legacy file → TEI source → generated HTML → repository metadata
```

GRETIL is no longer an example of a wholly abandoned resource: its migration into long-term infrastructure reduces the immediate risk. It is nevertheless an excellent preservation target because the historical representations, migration lineage, legacy ZIPs, and heterogeneous rights information are precisely the parts that a simplistic “download the latest files” strategy could lose.

---

### Sacred Texts and later candidates

The [Internet Sacred Text Archive](https://www.sacred-texts.com/) remains a strong later target because of its scale, historical interface, broad collection, and mixture of public-domain source texts with potentially distinct rights in translations, introductions, scans, organization, and site assets.

It should follow ETCSL and the first GRETIL subcollection because it presents a larger scope and rights-review problem.

Other useful future test collections include:

- **CELT — Corpus of Electronic Texts**, for multiple representations of large historical and literary corpora;
- old university-hosted philosophy editions;
- individual scholars' corpora and bibliographies;
- digital epigraphy projects;
- historical language dictionaries and concordances;
- collections with IIIF or OAI-PMH exports;
- small sites that combine scans, transcriptions, and commentary.

Candidate selection should consider:

```text
scholarly value
risk of disappearance
uniqueness
existing institutional redundancy
capture difficulty
estimated size
rights and privacy
availability of canonical deposits
```

---

## Preservation analysis

The first analysis features should serve preservation itself.

### Inventory and completeness

The system should answer:

- How many works were expected?
- How many were captured?
- Which expected identifiers are missing?
- Which works lack required representations?
- Which source was optional, and which was required?
- Did the crawler stop because of quota, errors, or scope?
- Did a repository deposit change without a new version identifier?

### Technical validity

- Can XML and TEI documents be parsed?
- Do they validate against an available schema?
- Does the declared encoding match the bytes?
- Are character entities and referenced schemas present?
- Are ZIP and TAR files structurally valid?
- Are internal links resolvable?
- Are there suspiciously small HTML error pages saved with status 200?
- Are MIME types and file extensions consistent?

### Change analysis

Between captures:

- added URLs and artifacts;
- removed URLs and artifacts;
- changed payload hashes;
- changed HTTP status;
- changed redirects;
- changed titles or metadata;
- changed work/representation mappings;
- passages cited by notes that no longer align;
- canonical files replaced in place.

### Explainable status

A report should provide reasons:

```text
Collection status: PARTIAL

Reason:
  required source "ota-dataset" failed

Additional warnings:
  3 catalogue URLs returned 404
  1 XML document failed validation
  2 compositions lack a captured translation
```

Ciao can make this explanation declarative, but the result should also be exported as ordinary JSON for other tools.

---

## Research analysis

Research functionality belongs downstream of successful preservation.

### Useful early analysis

- exact and phrase search;
- language-aware tokenization;
- concordances;
- word-frequency and n-gram views;
- parallel translation/transliteration alignment;
- cross-references between works;
- entity and name indexes;
- bibliographic links;
- comparison of versions and captures;
- navigation through explicit structural divisions.

SQLite FTS5 is sufficient for the first full-text index.

### Semantic search

Embeddings may later support:

- conceptually related passages;
- cross-language retrieval;
- links between Org notes and preserved texts;
- clustering;
- suggested related works.

Embeddings are derivatives and must record:

```text
input artifact hash
normalizer version
segmentation version
model and model version
creation date
```

They must never become the only index or the only route to a passage.

### LLM-assisted work

An LLM may propose:

- summaries;
- entities;
- keywords;
- passage links;
- alternative translations;
- candidate claims;
- possible contradictions;
- mappings between personal notes and texts.

Every result must be marked as machine-generated, linked to its input passages, and treated as a proposal until reviewed.

The archive must remain fully usable without an LLM.

---

## Normalized text model

Normalization should preserve enough common structure for search and reading without pretending that all sources are equivalent.

A minimal normalized record:

```json
{
  "collection_id": "gretil",
  "work_id": "samkhyakarika",
  "version_id": "main-sanskrit",
  "representation_id": "tei-sa_IzvarakRSNa-sAMkhyakArikA",
  "title": "Sāṃkhyakārikā",
  "languages": ["san"],
  "representation_kind": "tei",
  "source_artifact": "sha256:...",
  "capture_id": "20260827T120000Z-a1b2c3",
  "segments": [
    {
      "id": "karika-1",
      "label": "Kārikā 1",
      "text": "duḥkhatrayābhighātāj ...",
      "source_locator": {
        "type": "xml-id",
        "value": "..."
      }
    }
  ]
}
```

### Stable internal identifiers

Possible identifier forms:

```text
tp:etcsl:work:c.1.1.1
tp:etcsl:version:c.1.1.1:translation-en
tp:etcsl:segment:c.1.1.1:translation-en:12

tp:gretil:work:samkhyakarika
tp:gretil:representation:samkhyakarika:tei-main
tp:gretil:segment:samkhyakarika:tei-main:karika-1
```

The identifier should remain stable across captures when the intellectual object remains the same.

A segment locator should retain several anchors when possible:

```text
stable segment ID
source artifact hash
capture ID
xml:id or XPath
DOM or fragment selector
character offsets in normalized text
exact quotation
prefix and suffix context
```

No single anchor is reliable for every source. Combined anchors improve reattachment after a new capture or normalizer version.

TEI should receive first-class support because it combines encoded text with rich metadata and structural markup, but TEI must not become the required preservation format. Original source formats remain authoritative evidence.

---

## Annotations and claims

The annotation model should be compatible with the [W3C Web Annotation Data Model](https://www.w3.org/TR/annotation-model/), which represents an annotation as a body related to one or more targets and supports selectors for identifying parts of resources.

### Annotation example

```json
{
  "id": "urn:uuid:...",
  "type": "Annotation",
  "motivation": "commenting",
  "body": {
    "type": "TextualBody",
    "value": "Compare this account with ..."
  },
  "target": {
    "source": "tp:gretil:representation:samkhyakarika:tei-main",
    "selector": [
      {
        "type": "TextQuoteSelector",
        "exact": "duḥkhatrayābhighātāj ...",
        "prefix": "...",
        "suffix": "..."
      },
      {
        "type": "XPathSelector",
        "value": "..."
      }
    ]
  }
}
```

### Claims, not automatic truths

A personal ontology should begin as a provenance-aware claim graph.

A claim should record:

```text
identifier
subject
predicate
object
author or asserting agent
status
certainty
evidence
counterevidence
creation and modification time
source Org note
```

Example:

```prolog
claim('8ba1',
      concept(duhkha),
      classified_as,
      concept(threefold)).

evidence('8ba1',
         passage(gretil,
                 samkhyakarika,
                 'karika-1')).

status('8ba1', asserted).
certainty('8ba1', explicit).
asserted_in('8ba1', org_note('7db2...')).
```

Useful statuses may include:

```text
explicit_in_source
traditional_attribution
editorial_interpretation
scholarly_hypothesis
personal_hypothesis
machine_suggestion
disputed
retracted
```

An ontology can emerge gradually when stable concepts and relation types become clear. The project should not force the user's informal notes into a rigid schema prematurely.

---

## Integrity, provenance, and completeness

### Fixity

Every finalized capture should receive:

- `manifest-sha256.json`, the authoritative structured manifest;
- `SHA256SUMS`, a conventional interoperability file.

Verification should detect:

- missing files;
- modified bytes;
- changed object types;
- unexpected files added to a finalized package;
- broken manifest references.

Fixity proves that the bytes still match the finalized capture. It does not prove that the collection was complete or authentic when captured.

### Provenance

Capture metadata should include:

- input configuration;
- resolved configuration after inheritance;
- seed URLs;
- allowed hosts and URL rules;
- exact command arguments;
- working directory;
- crawler and runtime versions;
- operator and contact;
- UTC start and end times;
- host operating-system information;
- source exit status;
- redirects and HTTP failures;
- downloaded object and byte counts;
- logs;
- previous indexes used for deduplication;
- operator notes.

### Completeness

Completeness is collection-specific.

No generic crawler can prove that all intended texts were captured. A collection may hide items behind forms, APIs, undocumented identifiers, or JavaScript.

A recipe should compare the capture with evidence such as:

- a catalogue;
- expected identifiers;
- published counts;
- a repository manifest;
- a sitemap;
- required URLs;
- required representation types;
- known checksums;
- XML schema validity;
- mappings between legacy and current files.

### Deduplication

WARC-level revisit records may save space but introduce dependencies on earlier captures. They should be disabled by default.

Storage-level deduplication with restic, Borg, ZFS, APFS clones, or another backup layer is generally safer because each preservation package can remain logically self-contained.

---

## Special cases and limitations

### JavaScript applications

GNU Wget does not execute JavaScript. Use Browsertrix when content appears only after:

- client-side rendering;
- scrolling;
- clicking;
- API requests;
- route transitions;
- service-worker activity.

### CGI interfaces and forms

A crawler can follow explicit query links but cannot invent every valid form submission.

For database-like sites:

- find a complete catalogue;
- generate explicit seed lists;
- preserve the seed-generation script;
- capture a canonical export separately;
- record expected identifiers.

### Infinite URL spaces

Calendars, faceted search, sort orders, print views, session IDs, and query permutations can create infinite crawls.

Use:

- finite exploratory depth;
- strict quotas;
- exact host allowlists;
- include/exclude regular expressions;
- explicit seed files;
- source-specific crawling rules.

### External domains

Do not allow an entire institutional or CDN domain merely because one asset is hosted there.

Prefer separate sources and exact host/URL rules.

### Legacy encodings

Collections such as GRETIL may include historical non-Unicode encodings.

Preserve the original bytes and encoding declarations. Normalized Unicode text belongs in `derived/`, together with:

- the conversion table;
- converter version;
- warnings;
- source hash;
- round-trip or loss report when possible.

### Canonical repositories

The live website may not be the canonical source.

Always look for:

- TEI/XML or SGML deposits;
- ZIP or TAR releases;
- CSV, RDF, JSON, or SQL exports;
- Git repositories;
- schemas and stylesheets;
- repository landing pages and persistent identifiers;
- checksums;
- migration notes.

### Authentication

Authenticated capture is outside the first release.

Credentials, tokens, cookies, and authorization headers may leak through:

- process listings;
- copied configuration;
- WARC request records;
- crawler logs;
- replay software;
- backups.

Supporting private collections requires a separate threat model and secret-handling design.

### Localization and content negotiation

A server may return different material by language, cookie, IP region, User-Agent, or device.

One capture records the received representation. Important variants should become explicitly configured sources.

### Replay is not execution safety

Archived HTML, JavaScript, PDF, Office files, and compressed archives are untrusted content. Replay and extraction must be performed cautiously.

---

## Requirements

### Preservation core

Planned baseline:

- macOS or Linux;
- Python 3.11 or newer;
- GNU Wget with WARC support;
- a POSIX-style filesystem and file locking;
- SQLite with FTS5;
- sufficient local storage.

### Optional

- Ciao Prolog for declarative validation and claim queries;
- Browsertrix Crawler for dynamic sites;
- ReplayWeb.page or pywb for WARC replay;
- restic or BorgBackup for deduplicated backups;
- Emacs and Org mode for personal-note integration;
- TEI validation tooling;
- WACZ packaging tools.

The project should remain useful with only Python and GNU Wget.

---

## Proposed repository structure

```text
text-preserver/
├── README.md
├── LICENSE
├── pyproject.toml
├── src/
│   └── text_preserver/
│       ├── cli.py
│       ├── config.py
│       ├── models.py
│       ├── capture/
│       │   ├── coordinator.py
│       │   ├── manifests.py
│       │   ├── verify.py
│       │   └── engines/
│       │       ├── base.py
│       │       ├── wget.py
│       │       └── browsertrix.py
│       ├── catalog/
│       │   ├── database.py
│       │   ├── inventory.py
│       │   └── identifiers.py
│       ├── normalize/
│       │   ├── base.py
│       │   ├── html.py
│       │   └── tei.py
│       ├── analysis/
│       │   ├── preservation.py
│       │   ├── changes.py
│       │   ├── facts.py
│       │   └── ciao.py
│       ├── annotations/
│       │   ├── models.py
│       │   ├── selectors.py
│       │   └── org.py
│       └── web/
│           ├── app.py
│           ├── routes.py
│           └── templates/
├── collections/
│   ├── etcsl/
│   └── gretil/
├── schemas/
│   ├── collection.schema.json
│   ├── normalized-text.schema.json
│   ├── annotation.schema.json
│   └── claim.schema.json
├── emacs/
│   └── text-preserver-org.el
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── docs/
│   ├── architecture.md
│   ├── preservation-model.md
│   ├── identifiers.md
│   └── knowledge-model.md
├── collections.example.toml
└── .gitignore
```

Do not create every module before it is needed. This structure defines boundaries, not an instruction to build empty abstractions.

---

## Development plan

### Phase 0 — specification

- finalize terminology;
- add MIT `LICENSE`;
- add `.gitignore`;
- define capture and manifest schemas;
- define the collection recipe format;
- establish immutable/derived/workspace boundaries.

### Phase 1 — preservation core

- Python package and CLI;
- configuration loading and validation;
- Wget engine;
- recursive and direct-file sources;
- WARC, CDX, mirror, logs, and manifests;
- capture statuses;
- collection locks;
- `doctor`, `capture`, and `verify`.

### Phase 2 — ETCSL vertical slice

- ETCSL recipe;
- full catalogue seeds;
- Oxford deposit;
- inventory extraction;
- composition/translation/transliteration mapping;
- first completeness report;
- first Ciao rules;
- SQLite catalogue;
- minimal local reader;
- stable passage links.

### Phase 3 — GRETIL generalization

- one selected GRETIL subcollection;
- TextGrid and legacy source mapping;
- TEI normalization;
- legacy encoding preservation;
- migration-lineage checks;
- representation and rights modelling;
- generated HTML/source XML mapping.

### Phase 4 — access and Org integration

- full-text search;
- side-by-side representations;
- source and WARC access;
- custom Org links;
- `org-protocol` capture;
- W3C-compatible annotations.

### Phase 5 — advanced validation and reasoning

- collection-specific Ciao packages;
- explainable completeness reports;
- claim graph;
- query interface;
- conflict and dependency detection;
- change impact on cited passages.

### Phase 6 — dynamic web and portable replay

- Browsertrix adapter;
- browser behaviors;
- replay quality assurance;
- WACZ packaging;
- ReplayWeb.page integration.

### Phase 7 — optional semantic analysis

- pluggable embeddings;
- semantic search;
- LLM-assisted proposals;
- provenance and review workflow;
- no dependency of preservation on model availability.

---

## Testing strategy

### Unit tests

- configuration inheritance;
- safe identifiers and paths;
- scope construction;
- manifest generation;
- hash verification;
- status aggregation;
- stable identifiers;
- annotation selectors;
- Org export filtering.

### Local integration tests

Integration tests should use localhost fixtures and must not contact real collections.

They should cover:

- recursive HTML capture;
- direct-file capture;
- WARC and CDX output;
- link conversion;
- multi-source collection status;
- partial and interrupted captures;
- preservation of logs;
- full versus source-filtered `LATEST`;
- fixity verification;
- detection of modified and unexpected files;
- collection locks;
- quota and scope behavior;
- optional WARC deduplication.

### Collection recipe tests

Use small, redistributable fixtures representing:

- ETCSL catalogue/translation/transliteration relationships;
- GRETIL legacy HTML/TEI/generated HTML lineage;
- invalid XML;
- missing representations;
- legacy encodings;
- duplicate identifiers;
- changing source URLs.

### Live capture tests

Live captures should be manual or explicitly enabled. CI must not crawl old public servers on every push.

---

## Storage, retention, and backup

The preservation package is not safe merely because it exists on one disk.

A practical policy includes:

- the working archive on a normal filesystem;
- at least one copy on another physical device;
- an off-site copy when rights and privacy permit;
- versioned, deduplicated backup storage;
- periodic fixity verification;
- periodic restore tests;
- preservation of configuration and metadata with the captures;
- no manual edits inside finalized captures.

The common 3-2-1 rule—three copies, two storage types, one off-site—is a useful baseline.

### Retention

Retention should preserve:

- milestone captures;
- captures before and after a major migration;
- last known complete captures;
- captures referenced by notes or publications;
- captures required by WARC revisit dependencies.

Automated deletion should not be implemented until dependency-aware retention is designed.

---

## Legal and ethical use

`text-preserver` does not grant rights to captured material.

Operators are responsible for determining whether downloading, preserving, processing, replaying, or redistributing a collection is permitted under applicable law, licences, contracts, and institutional policies.

Important distinctions:

- public access is not automatically permission to republish;
- private preservation and public redistribution are separate decisions;
- a public-domain source work may coexist with copyrighted translations, introductions, scans, annotations, metadata, or site design;
- robots.txt is a technical instruction, not a complete statement of copyright;
- technical accessibility is not permission to bypass access controls;
- individual representations within one collection may have different rights;
- takedown, privacy, and sensitive-data concerns may affect later access.

The default must be to respect robots.txt and use conservative rates. Any override should be deliberate, source-specific, and preserved in metadata.

The software licence does not apply to captured content.

---

## Security and privacy

Archived material is untrusted and may remain active during replay.

- HTML and JavaScript may contact the live web.
- PDFs and office documents may contain malicious content.
- compressed files may contain path traversal or decompression bombs;
- WARC files may preserve cookies, headers, personal data, or local paths;
- operator metadata may reveal identity and filesystem structure;
- private Org notes may contain sensitive personal information.

Recommended safeguards:

- bind the reader to `127.0.0.1` by default;
- mount `archive/` read-only;
- do not execute downloaded programs;
- scan archives before extraction;
- isolate replay when appropriate;
- keep private workspaces encrypted and separate;
- review metadata before sharing;
- never store credentials in public configuration;
- avoid authenticated captures until a dedicated design exists.

---

## Public repository policy

The software and collection recipes can be public. Actual captures should normally remain outside Git.

### Commit

```text
source code
tests
documentation
schemas
public collection recipes
example configuration
small redistributable fixtures
```

### Do not commit

```text
private collections.toml
archive/
derived/
workspace/
*.warc
*.warc.gz
*.wacz
*.cdx
*.cdxj
credentials
cookies
private Org notes
logs containing personal information
copyrighted capture payloads
```

Recommended `.gitignore`:

```gitignore
# Local configuration
collections.toml

# Preservation and research data
data/
archive/
derived/
workspace/
captures/

# Web archive formats
*.warc
*.warc.gz
*.wacz
*.cdx
*.cdxj

# Python
__pycache__/
*.py[cod]
.pytest_cache/
.venv/

# Local state
*.log
.DS_Store
```

Public recipes may contain URLs, risk notes, expected inventories, conservative scopes, and validation logic. They should not imply that redistribution of the captured collection has been legally cleared.

---

## Contributing

Contributions are welcome when they improve preservation quality without weakening scope safety, provenance, or failure visibility.

Core contribution rules:

- do not hide capture failures;
- do not make robots.txt override the default;
- do not introduce unrestricted cross-domain crawling;
- do not store secrets in ordinary configuration;
- do not treat a converted mirror as a substitute for WARC;
- do not assume captured content can be redistributed;
- do not let normalized data overwrite preservation masters;
- add tests for changes to capture behavior;
- keep collection-specific exceptions in recipes whenever possible;
- document source mappings and rights uncertainty;
- distinguish original, derived, personal, and machine-generated content.

Particularly useful contributions include:

- collection recipes;
- inventory and completeness checks;
- WARC replay tests;
- TEI adapters;
- legacy encoding maps;
- Browsertrix integration;
- Ciao validation rules;
- Org integration;
- accessible local-reader design;
- migration and provenance modelling.

---

## License

The source code and project documentation are licensed under the **MIT License**. See [`LICENSE`](../LICENSE).

The software licence does not apply to websites, texts, images, datasets, metadata, or other material captured with the software.

---

## References

### Preservation and capture

- [GNU Wget manual](https://www.gnu.org/software/wget/manual/)
- [WARC format description — Library of Congress](https://www.loc.gov/preservation/digital/formats/fdd/fdd000236.shtml)
- [Recommended formats for web archives — Library of Congress](https://www.loc.gov/preservation/resources/rfs/webarchives.html)
- [Browsertrix Crawler documentation](https://crawler.docs.browsertrix.com/)
- [WACZ format description — Library of Congress](https://www.loc.gov/preservation/digital/formats/fdd/fdd000586.shtml)

### Text encoding, annotations, and search

- [TEI P5 Guidelines](https://www.tei-c.org/release/doc/tei-p5-doc/en/html/)
- [W3C Web Annotation Data Model](https://www.w3.org/TR/annotation-model/)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)

### Research integration

- [Ciao Prolog](https://ciao-lang.org/)
- [Ciao assertion language](https://ciao-lang.org/ciao/build/doc/ciao.html/AssrtLang.html)
- [Org custom hyperlink types](https://orgmode.org/manual/Adding-Hyperlink-Types.html)
- [Org capture protocol](https://orgmode.org/manual/The-capture-protocol.html)

### Initial collections

- [ETCSL](https://etcsl.orinst.ox.ac.uk/)
- [ETCSL manual](https://etcsl.orinst.ox.ac.uk/edition2/etcslmanual.php)
- [GRETIL legacy portal](https://gretil.sub.uni-goettingen.de/)
- [GRETIL in TextGrid](https://textgridrep.org/project/TGPR-2ba9cb1b-9602-202d-71ce-67e63a29de55)
- [Internet Sacred Text Archive](https://www.sacred-texts.com/)
