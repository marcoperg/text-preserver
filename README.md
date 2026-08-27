# text-preserver

**Preserve vulnerable digital text collections and the context that makes them intelligible.**

> **Project status:** experimental, pre-1.0. The current implementation is intended for careful, supervised preservation work rather than unattended large-scale crawling.

`text-preserver` is a configuration-driven tool for creating reproducible preservation captures of scholarly corpora, digital editions, historical text archives, specialist bibliographies, old academic projects, and similar collections that may otherwise disappear from the public web.

The project is about preserving **textual collections**, not merely copying websites. A collection may include HTML, plain text, TEI/XML, PDFs, EPUBs, database exports, scans, illustrations, bibliographic metadata, stylesheets, and the historical web interface needed to interpret or navigate the material.

The web is therefore one acquisition source—not the boundary of the project.

## Contents

- [Why this project exists](#why-this-project-exists)
- [What text-preserver preserves](#what-text-preserver-preserves)
- [Current capabilities](#current-capabilities)
- [Core preservation model](#core-preservation-model)
- [Architecture](#architecture)
- [Design principles](#design-principles)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Command-line interface](#command-line-interface)
- [Archive layout](#archive-layout)
- [Adding a collection](#adding-a-collection)
- [ETCSL example](#etcsl-example)
- [Special cases and limitations](#special-cases-and-limitations)
- [Integrity, completeness, and provenance](#integrity-completeness-and-provenance)
- [Replay and access](#replay-and-access)
- [Scheduling, retention, and backup](#scheduling-retention-and-backup)
- [Legal and ethical use](#legal-and-ethical-use)
- [Security considerations](#security-considerations)
- [Public repository policy](#public-repository-policy)
- [Development](#development)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## Why this project exists

Many valuable textual resources are maintained by one scholar, a small research group, or an old university department. They may continue working for decades and then vanish because of:

- an expired domain;
- a server migration;
- obsolete PHP, CGI, Perl, or database dependencies;
- the retirement of the original maintainer;
- loss of project funding;
- institutional redesigns that discard old paths;
- incomplete migration to a new platform;
- accidental deletion or corruption;
- the disappearance of a separate source-code or data deposit.

A simple `wget -r` command is useful, but it does not by itself provide a durable preservation workflow. It may omit generated records, wander into an unbounded URL space, fail without a clear status, lose the exact crawl settings, or leave no way to prove later that captured files have not changed.

`text-preserver` turns a collection-specific capture plan into a repeatable preservation package containing content, provenance, logs, indexes, and integrity manifests.

## What text-preserver preserves

The word **text** describes the intellectual object being preserved, not a file extension.

Appropriate collections include:

- scholarly text corpora;
- critical and diplomatic editions;
- collections of translations or transliterations;
- historical and religious text archives;
- digital humanities projects;
- old university-hosted research databases;
- specialist bibliographies and concordances;
- lexica and language resources;
- collections of public-domain books;
- source repositories or institutional deposits associated with those projects.

A textual collection may legitimately require non-textual supporting objects, such as:

- manuscript or inscription images;
- maps and diagrams;
- audio pronunciation files;
- CSS, fonts, and interface images;
- catalogues, metadata, and database exports;
- software or schemas needed to interpret the source data.

The project is **not** intended to become a universal digital-preservation platform for arbitrary video libraries, social-media accounts, software mirrors, or whole-domain internet crawling. Its scope is digital collections whose primary value is textual, scholarly, historical, literary, linguistic, or documentary.

## Current capabilities

The initial implementation uses Python and GNU Wget. It supports public, unauthenticated HTTP/HTTPS sources on macOS and Linux.

Each collection capture can contain:

- one or more independently configured sources;
- a locally browsable mirror;
- compressed WARC files containing the original HTTP exchanges;
- a CDX index over WARC records;
- direct downloads of canonical ZIP, XML, PDF, EPUB, or other deposited files;
- the exact seeds and fully resolved configuration;
- the exact crawler command and working directory;
- timestamps, host information, operator metadata, and tool versions;
- complete crawler logs and exit status;
- capture- and source-level status records;
- SHA-256 manifests for later fixity verification;
- protection against overlapping local captures of the same collection;
- conservative domain scope, delays, retries, quotas, and robots.txt behavior;
- dry-run inspection before any network request is made.

The initial engine does **not** execute JavaScript. Browser-based capture is part of the roadmap.

## Core preservation model

`text-preserver` uses four principal concepts.

### Collection

A **collection** is the logical intellectual object being preserved.

Examples:

- Electronic Text Corpus of Sumerian Literature;
- Internet Sacred Text Archive;
- a digital edition of a philosopher's works;
- a university corpus and its associated documentation.

A collection is not necessarily equivalent to one hostname or one website.

### Source

A **source** is one independently capturable representation or component of a collection.

Examples:

- the public website;
- a CGI catalogue that exposes otherwise hidden texts;
- a canonical TEI/XML ZIP in an institutional repository;
- a PDF export;
- a Git repository containing schemas and source data;
- a browser-rendered interface;
- an IIIF manifest or OAI-PMH endpoint.

The current Wget engine handles recursive web sources and nonrecursive HTTP file sources. Additional source adapters are planned.

### Capture

A **capture** is a dated attempt to preserve all or a selected subset of a collection's sources. Captures are immutable preservation events rather than folders intended for manual editing.

A capture may finish as:

- `complete`;
- `complete_with_warnings`;
- `partial`;
- `interrupted`;
- `failed`.

Partial and interrupted captures are retained for diagnosis and evidential value. They do not silently become the collection's latest successful full capture.

### Preservation package

A **preservation package** contains the captured objects plus the information needed to understand and verify the capture:

- WARC/CDX;
- mirror or direct files;
- logs;
- resolved settings;
- source descriptions;
- commands;
- environment metadata;
- status records;
- cryptographic manifests.

## Architecture

```text
                         collections.toml
                                │
                                ▼
                     ┌────────────────────┐
                     │   text-preserver   │
                     │    Python 3.11+    │
                     └─────────┬──────────┘
                               │
          validate / resolve / scope / lock / record provenance
                               │
             ┌─────────────────┴─────────────────┐
             ▼                                   ▼
     ┌────────────────┐                 future source engines
     │ GNU Wget engine│                 Browsertrix / Git / IIIF
     └───────┬────────┘                 OAI-PMH / repository APIs
             │
      ┌──────┼───────────────┐
      ▼      ▼               ▼
   mirror/  warc/           logs/
            .warc.gz
            .cdx
      └──────┬───────────────┘
             ▼
    source metadata and status
             │
             ▼
 collection capture metadata
             │
             ▼
 SHA-256 manifest and verification
```

The configuration hierarchy is:

```text
[defaults]
    ↓ overridden by
[[collections]].settings
    ↓ overridden by
[[collections.sources]].settings
```

Site-specific behavior belongs in configuration rather than hard-coded crawler branches.

## Design principles

### Preserve data and context

When possible, preserve both:

1. the canonical data or source files; and
2. the interface, documentation, organization, and editorial context through which the collection was published.

For ETCSL, that means preserving both the historical website and the deposited corpus source files.

### Prefer self-contained captures

Every successful capture should remain replayable without depending on an older capture. WARC revisit-record deduplication is therefore disabled by default.

Storage-level deduplication with tools such as restic or Borg is normally safer because it saves space without introducing replay dependencies between preservation packages.

### Make scope explicit

A recursive crawler must not be allowed to wander from an archive into unrelated domains. Seed hosts form the default boundary. Cross-host crawling requires an explicit domain allowlist.

### Treat old servers gently

The defaults favor one conservative request stream, a delay between requests, randomized waiting, finite retries, and robots.txt compliance. A working public server is more important than completing a capture quickly.

### Preserve failures honestly

A nonzero crawler exit code is not silently converted into success. Logs and partial results remain available for review.

### Preserve provenance

The archive should answer:

- what was requested;
- when it was requested;
- from where it was requested;
- which program and version were used;
- which settings and seeds were used;
- which resources succeeded or failed;
- whether the resulting bytes still match the finalized capture.

### Keep finalized captures immutable

Do not edit files inside a completed capture. Corrections to configuration or collection metadata should produce a new capture or an explicitly versioned metadata update, not an undocumented mutation of preservation evidence.

## Requirements

### Required

- macOS or Linux;
- Python **3.11 or newer**;
- GNU Wget built with WARC support;
- a POSIX-style filesystem and file locking;
- sufficient temporary and permanent storage;
- network access to the configured public sources.

Python 3.11 is required because the implementation uses the standard-library TOML reader, `tomllib`.

Check the environment with:

```bash
python3 --version
wget --version
wget --help | grep -E -- '--warc-file|--warc-cdx|--warc-tempdir'
```

### Optional

- [ReplayWeb.page](https://replayweb.page/) for browser-based WARC/WACZ replay;
- [pywb](https://pywb.readthedocs.io/) for a local Wayback-style replay service;
- [Browsertrix Crawler](https://crawler.docs.browsertrix.com/) for JavaScript-heavy sites;
- [restic](https://restic.net/) or [BorgBackup](https://www.borgbackup.org/) for versioned, deduplicated backups;
- WACZ tooling for portable indexed replay packages;
- an external disk, NAS, or object-storage target for redundant copies.

Docker is not required for the GNU Wget backend.

## Installation

Clone the repository and enter it:

```bash
git clone <repository-url>
cd text-preserver
```

Make the entry point executable:

```bash
chmod +x text-preserver
```

### macOS

macOS does not normally include GNU Wget. Install it with Homebrew:

```bash
brew install wget
```

Install a current Python if necessary:

```bash
brew install python
```

### Debian or Ubuntu

```bash
sudo apt update
sudo apt install python3 wget
```

Distribution packages differ. Run `doctor` after installation because a Wget build without WARC support is insufficient.

## Quick start

Create a private working configuration from the public example:

```bash
cp collections.example.toml collections.toml
```

At minimum, edit:

```toml
[project]
archive_root = "/path/to/your/archive"
operator = "Your name or organization"
contact = "mailto:you@example.org"
user_agent = "text-preserver/0.1 (+mailto:you@example.org)"
```

Validate the configuration and dependencies:

```bash
./text-preserver doctor -c collections.toml
```

List configured collections and sources:

```bash
./text-preserver list -c collections.toml
```

Inspect the commands without downloading anything:

```bash
./text-preserver capture -c collections.toml etcsl --dry-run
```

Capture one collection:

```bash
./text-preserver capture -c collections.toml etcsl
```

Capture only one source:

```bash
./text-preserver capture \
  -c collections.toml \
  etcsl \
  --source ota-dataset
```

Capture every enabled collection:

```bash
./text-preserver capture -c collections.toml --all
```

Verify the latest successful full capture:

```bash
./text-preserver verify archive/collections/etcsl
```

## Configuration

Configuration is stored in TOML. Keep the real `collections.toml` outside version control when it contains personal paths, contact information, private collection notes, or future credentials.

### Project settings

```toml
[project]
archive_root = "./archive"
operator = "Your name"
contact = "mailto:you@example.org"
user_agent = "text-preserver/0.1 (+mailto:you@example.org)"
wget_binary = "wget"
latest_symlink = true
```

- `archive_root` is resolved relative to the configuration file unless absolute.
- `operator` and `contact` become part of capture provenance.
- `user_agent` should identify the tool honestly and provide a contact route.
- `wget_binary` may be an executable name or absolute path.
- `latest_symlink` controls the convenience symlink; a portable `LATEST` text pointer is still preferred as authoritative metadata.

### Conservative defaults

```toml
[defaults]
engine = "wget"
recursive = true
level = "inf"
page_requisites = true
convert_links = true
adjust_extension = true
mirror = true
warc = true
warc_cdx = true
warc_max_size = "1G"
robots = true
wait = 1.0
random_wait = true
timeout = 30
tries = 3
retry_connrefused = true
span_hosts = false
quota = "2G"
```

A finite quota is strongly recommended during initial exploration. It is an emergency brake, not an estimate of completeness.

### Collection definition

```toml
[[collections]]
id = "example-corpus"
title = "Example Corpus"
description = "A scholarly corpus and its historical web interface."
homepage = "https://example.org/"
license_note = "Record known rights information without assuming redistribution permission."
risk_note = "Explain why this collection may be vulnerable or difficult to reconstruct."
tags = ["philology", "digital-humanities"]
enabled = false
```

A collection ID must be stable, filesystem-safe, and suitable for use in commands.

### Recursive web source

```toml
[[collections.sources]]
id = "web"
title = "Public web interface"
description = "Website, documentation, indexes, texts, and presentation assets."
required = true
seeds = [
  "https://example.org/",
  "https://example.org/sitemap.html",
]
domains = ["example.org"]

[collections.sources.settings]
recursive = true
level = "inf"
page_requisites = true
convert_links = true
quota = "2G"
```

### Direct-file source

Canonical ZIP, XML, PDF, EPUB, database export, or repository bitstream files should normally be separate nonrecursive sources:

```toml
[[collections.sources]]
id = "canonical-dataset"
title = "Canonical deposited dataset"
description = "Source files deposited in an institutional repository."
required = true
seeds = [
  "https://repository.example.org/path/corpus.zip",
]
domains = ["repository.example.org"]

[collections.sources.settings]
recursive = false
page_requisites = false
convert_links = false
adjust_extension = false
content_disposition = true
quota = "500M"
```

Separating the canonical data from the public interface gives each source independent provenance, scope, status, and retention rules.

### Important capture settings

| Setting | Purpose |
|---|---|
| `recursive` | Follow links from seed documents. |
| `level` | Maximum recursive depth, or `"inf"`. |
| `page_requisites` | Retrieve CSS, images, and resources needed to display a page. |
| `convert_links` | Rewrite mirror links for offline browsing. |
| `mirror` | Keep a normal directory-tree access copy. |
| `warc` | Store original HTTP exchanges in WARC. |
| `warc_cdx` | Create an external WARC index. |
| `domains` | Exact hostnames allowed to participate in the crawl. |
| `span_hosts` | Permit traversal between explicitly allowed hosts. |
| `no_parent` | Prevent recursion above a seed directory. |
| `accept` / `reject` | Restrict filename or suffix patterns. |
| `accept_regex` / `reject_regex` | Restrict complete URLs. |
| `wait` / `random_wait` | Control request spacing. |
| `limit_rate` | Optional transfer-rate limit such as `"500K"`. |
| `quota` | Maximum downloaded amount such as `"2G"`. |
| `robots` | Respect robots.txt; enabled by default. |
| `warc_max_size` | Segment large captures into manageable WARC files. |
| `warc_dedup_previous` | Use previous CDX data for revisit records; advanced and disabled by default. |
| `success_exit_codes` | Exit codes considered successful after site-specific review. |
| `extra_args` | Exceptional unmanaged Wget arguments; use sparingly. |

Source settings override collection settings, which override global defaults.

## Command-line interface

### Diagnose the environment

```bash
./text-preserver doctor -c collections.toml
```

Checks configuration validity, GNU Wget identity and WARC capabilities, archive-root writability, and available disk space.

### List configured material

```bash
./text-preserver list -c collections.toml
./text-preserver list -c collections.toml --json
```

### Capture

```bash
./text-preserver capture -c collections.toml COLLECTION_ID [COLLECTION_ID ...]
./text-preserver capture -c collections.toml --all
```

Useful options:

```text
--source SOURCE_ID       capture only a selected source; repeatable
--capture-id ID          provide an explicit safe capture ID
--note TEXT              preserve an operator note in metadata
--dry-run                show resolved commands without writing or downloading
--no-latest              do not update LATEST/latest pointers
--force-latest           allow a source-filtered capture to become latest
--allow-partial          return process status 0 for a partial capture
```

A source-filtered capture does not normally replace the latest successful **full** collection capture.

### Verify

```bash
./text-preserver verify PATH
```

`PATH` may identify a capture directory or a collection directory containing a `LATEST` pointer.

Verification checks:

- expected paths exist;
- object types match;
- byte sizes match;
- SHA-256 hashes match;
- unexpected files have not appeared inside the finalized capture.

## Archive layout

```text
archive/
├── locks/
│   └── etcsl.lock
├── runs/
│   └── 20260827T120000Z-a1b2c3.json
└── collections/
    └── etcsl/
        ├── collection.json
        ├── LATEST
        ├── latest -> captures/20260827T120000Z-a1b2c3
        └── captures/
            └── 20260827T120000Z-a1b2c3/
                ├── capture.json
                ├── manifest-sha256.json
                ├── SHA256SUMS
                ├── metadata/
                │   ├── environment.json
                │   ├── input-config.toml
                │   └── resolved-collection.json
                └── sources/
                    ├── web/
                    │   ├── seeds.txt
                    │   ├── mirror/
                    │   ├── warc/
                    │   ├── logs/
                    │   │   └── wget.log
                    │   └── metadata/
                    │       ├── command.json
                    │       ├── resolved-source.json
                    │       └── result.json
                    └── ota-dataset/
                        └── ...
```

Capture IDs use a UTC timestamp plus a short random suffix, for example:

```text
20260827T120000Z-a1b2c3
```

UTC makes captures unambiguous across time zones and daylight-saving changes.

The WARC is the primary web-preservation object. The converted mirror is an access derivative. Both are retained by default because they solve different problems.

## Adding a collection

A careful first capture should follow this process.

1. **Identify the intellectual object.** Decide what constitutes the collection and which parts are essential.
2. **Find every authoritative representation.** Look for source datasets, TEI/XML deposits, PDFs, Git repositories, documentation, schemas, and institutional records in addition to the public interface.
3. **Record rights and risk.** Document known licenses, copyright uncertainty, project status, maintainers, and why preservation matters.
4. **Map the URL space.** Find sitemaps, indexes, catalogues, hidden CGI entry points, pagination, alternate hostnames, and external repositories.
5. **Define separate sources.** Do not recursively crawl an entire institutional repository merely to obtain one ZIP file.
6. **Begin conservatively.** Use finite depth, a low quota, a long delay, and a narrow domain allowlist.
7. **Run a dry run.** Inspect every generated command before network activity.
8. **Supervise the first crawl.** Watch server errors, unexpected domains, query proliferation, and archive growth.
9. **Inspect the result.** Review logs, CDX entries, MIME types, mirror navigation, expected text counts, and WARC replay.
10. **Adjust and recapture.** Treat exploratory captures as evidence, not necessarily as the definitive preservation event.
11. **Verify and back up.** Run fixity verification and copy the finalized package to independent storage.
12. **Schedule only after scope is understood.** Unattended recurrence should follow a successful supervised capture.

## ETCSL example

The Electronic Text Corpus of Sumerian Literature illustrates why the project uses collections with multiple sources.

### Source 1: historical web interface

The website source should include explicit entry points such as:

- the homepage;
- the edition sitemap;
- the catalogue that links to all compositions;
- any surviving first-edition entry point;
- project documentation and editorial conventions.

A crawler follows links but does not invent search-form submissions. Explicitly seeding a complete catalogue is therefore important for old CGI-based archives.

### Source 2: canonical deposited corpus

The canonical corpus source should capture the direct Oxford Text Archive deposit as a nonrecursive file source rather than crawling the entire repository.

Conceptually:

```toml
[[collections]]
id = "etcsl"
title = "Electronic Text Corpus of Sumerian Literature"
homepage = "https://etcsl.orinst.ox.ac.uk/"
enabled = true

[[collections.sources]]
id = "web"
required = true
seeds = [
  "https://etcsl.orinst.ox.ac.uk/",
  "https://etcsl.orinst.ox.ac.uk/edition2/etcslsitemap.html",
  "https://etcsl.orinst.ox.ac.uk/cgi-bin/etcsl.cgi?text=all",
  "https://etcsl.orinst.ox.ac.uk/index1.htm",
]
domains = ["etcsl.orinst.ox.ac.uk"]

[collections.sources.settings]
recursive = true
quota = "2G"

[[collections.sources]]
id = "ota-dataset"
required = true
seeds = [
  "https://ota.bodleian.ox.ac.uk/repository/xmlui/bitstream/handle/20.500.12024/2518/etcsl.zip?isAllowed=y&sequence=11",
]
domains = ["ota.bodleian.ox.ac.uk"]

[collections.sources.settings]
recursive = false
page_requisites = false
convert_links = false
adjust_extension = false
content_disposition = true
quota = "100M"
```

Repository URLs can change. Confirm the current record and bitstream before turning a capture into an unattended schedule. Preserve the repository record page as an additional provenance source when appropriate.

## Special cases and limitations

### JavaScript-generated sites

GNU Wget reads returned HTML and CSS; it does not execute a modern web application. Content loaded only after JavaScript execution, scrolling, button presses, client-side navigation, or API calls may be absent.

Warning signs include:

- saved HTML contains little more than an application shell;
- records appear only after scrolling or interacting;
- navigation uses client-side routes;
- content is loaded through XHR or `fetch`;
- replayed pages are blank despite successful HTTP responses;
- service workers are essential to site behavior.

Use Browsertrix Crawler or ArchiveWeb.page for these collections until a browser engine is integrated directly.

### CGI interfaces and search forms

Wget can follow ordinary CGI links containing query parameters. It cannot infer every valid query or submit arbitrary search forms.

For a database exposed primarily through search:

- find a complete catalogue or sitemap;
- generate a UTF-8 seed file from record identifiers;
- capture a canonical database export separately;
- preserve the query or script used to derive the seeds;
- record expected record counts when known.

### Infinite URL spaces

Calendars, faceted search, sort permutations, session identifiers, print views, tags, and query combinations can create effectively infinite URL spaces.

Use:

- finite depth during exploration;
- strict quotas;
- `no_parent` for subdirectory captures;
- `reject_regex` for irrelevant queries or routes;
- explicit seed lists for finite record sets;
- independent sources for canonical exports.

A quota may stop a runaway crawl, but it does not guarantee that the important material was captured before the quota was exhausted.

### External domains and CDNs

Assets may be hosted on other domains. Do not solve this by allowing a broad parent domain such as `edu`, `ac.uk`, or a shared CDN host without careful restrictions.

Prefer:

- omitting nonessential external assets;
- allowing only exact required hosts;
- adding URL regex restrictions;
- treating an external repository as a separate source;
- using a browser crawler with more precise scoping behavior.

### Canonical deposits

The live interface may be only one representation of the collection. Always look for:

- TEI/XML or SGML source;
- CSV, JSON, RDF, or SQL exports;
- source-code repositories;
- schemas and documentation;
- PDF or EPUB editions;
- institutional repository records;
- stable identifiers and checksums.

Preserving the canonical source often matters more than preserving pixel-perfect interface behavior. Preserving both is better when practical.

### Authentication and private collections

Authenticated capture is intentionally unsupported in the initial release.

Do not pass passwords, bearer tokens, cookies, or session data through URLs, `extra_args`, headers, or ordinary configuration. They can leak through:

- process listings;
- copied input configuration;
- exact command metadata;
- WARC request records;
- logs;
- replay software;
- backups or later redistribution.

Authenticated preservation requires a separate threat model, secret redaction, encrypted storage, dedicated accounts, and explicit review of captured request headers.

### Content negotiation and localization

A server may vary content by language, cookie, IP region, encoding, User-Agent, or device. One capture preserves the representation received by that configured client, not every possible variant.

When multiple language versions matter, model them as separate reproducible sources or seed sets.

### TLS and obsolete servers

Do not casually disable certificate checking. An invalid certificate may indicate an old but legitimate server, a broken migration, or interception. Investigate and document any exception at the individual source level.

### Completeness cannot be automatic

No generic crawler can prove that it has captured every intended text. Important records may be hidden behind forms, APIs, scripts, or undocumented identifiers.

Collection-specific validation should compare the capture against known evidence such as:

- catalogue counts;
- expected identifiers;
- published tables of contents;
- repository manifests;
- checksums;
- required URLs;
- expected MIME types and file sizes.

Automated collection assertions are planned, but human review remains necessary.

## Integrity, completeness, and provenance

### WARC and mirror

A mirror and a WARC are complementary.

The mirror is convenient for:

- ordinary filesystem inspection;
- local browsing;
- text extraction and indexing;
- quick access without replay infrastructure.

The WARC is preferable for preservation because it retains HTTP-level evidence such as original response payloads, headers, redirects, request records, and crawl metadata. Link conversion may modify mirror HTML, so the mirror is not a byte-identical substitute for WARC.

Background:

- GNU Wget manual: <https://www.gnu.org/software/wget/manual/>
- WARC format overview: <https://www.loc.gov/preservation/digital/formats/fdd/fdd000236.shtml>
- IIPC web archive format primer: <https://iipc.github.io/warc-specifications/primers/web-archive-formats/>

### Fixity

Every finalized capture receives:

- `manifest-sha256.json`, the authoritative structured manifest;
- `SHA256SUMS`, a conventional interoperability file.

Fixity proves that the bytes still match the finalized capture. It does not prove that the origin server was authentic, that the collection was complete, or that the files were correct when captured.

### Provenance

Preserved metadata should include:

- input configuration;
- resolved configuration after inheritance;
- seeds;
- allowed domains;
- exact command arguments;
- current working directory;
- tool versions;
- operator and contact;
- UTC start and end times;
- machine and operating-system information;
- crawler exit status;
- source and collection status;
- downloaded byte and file counts;
- logs;
- any previous CDX used for revisit records.

### WARC deduplication

`warc_dedup_previous = true` may replace repeated payloads with revisit records referring to an older capture. This saves WARC space but creates a dependency chain.

When enabled:

- never delete the referenced older WARC casually;
- preserve dependency metadata;
- back up the whole collection as one unit;
- test replay before applying retention policies.

The default recommendation is self-contained captures plus deduplication in the backup layer.

## Replay and access

### Browse a mirror

For simple sites, serve the mirror from localhost:

```bash
cd archive/collections/etcsl/latest/sources/web/mirror
python3 -m http.server 8000 --bind 127.0.0.1
```

Open the relevant host-directory path under `http://127.0.0.1:8000/`.

Serving over localhost often works better than opening pages directly with `file://`, because browsers apply different origin and resource rules to local files.

### Replay WARC

Options include:

- opening the WARC in ReplayWeb.page;
- importing it into pywb;
- packaging it as WACZ for efficient portable replay.

A future release should generate replay instructions and optional WACZ packages automatically.

## Scheduling, retention, and backup

Supervise initial captures. Schedule them only after scope, server behavior, storage growth, and validation criteria are understood.

Example monthly cron entry:

```cron
20 3 1 * * /absolute/path/text-preserver capture -c /absolute/path/collections.toml etcsl >> /absolute/path/scheduler.log 2>&1
```

Use absolute paths because scheduled environments often have a minimal `PATH`.

A practical retention and backup policy includes:

- a working copy on a normal filesystem;
- at least one copy on another physical device;
- an off-site or geographically separate copy when rights and privacy permit;
- versioned, deduplicated backup storage;
- periodic `verify` runs;
- periodic backup-repository checks;
- preservation of configuration and metadata alongside content;
- no manual edits inside finalized captures.

The common 3-2-1 model—three copies, two storage types, one off-site—is a useful baseline.

Do not treat `latest` as the archive. It is only a convenience pointer to one dated preservation package.

## Legal and ethical use

`text-preserver` is a preservation tool. It does not grant rights to captured material.

Operators are responsible for determining whether downloading, preserving, processing, replaying, or redistributing a collection is permitted under applicable law, licenses, contracts, and institutional policies.

Important distinctions:

- public readability is not necessarily permission to republish;
- private preservation and public redistribution are separate decisions;
- public-domain source texts may coexist with copyrighted translations, introductions, annotations, scans, software, or database organization;
- robots.txt is a technical instruction, not a complete statement of copyright or legal rights;
- technical accessibility is not permission to bypass access controls;
- removal requests, privacy concerns, and sensitive data may affect later access decisions.

The default is to respect robots.txt. Any override must be source-specific, deliberate, and preserved in metadata.

For fragile or personally maintained servers:

- identify yourself with a clear User-Agent;
- provide a contact address;
- use long delays and low rates;
- avoid parallel requests;
- contact the maintainer before a large crawl when practical;
- stop when error rates or server behavior suggest distress.

The software license for this repository does **not** apply to captured content. Rights remain with the respective authors, publishers, institutions, and other rights holders.

## Security considerations

Archived web material is untrusted, potentially active content.

- HTML and JavaScript may execute during replay.
- A captured page may contact the live web and reveal that it is being viewed.
- PDFs, office documents, archives, and executables may be malicious.
- Old web applications may depend on insecure browser behavior.
- ZIP or TAR extraction may expose path traversal or decompression bombs.
- metadata can reveal operator identity and local filesystem paths.

Recommended precautions:

- replay only with an up-to-date browser;
- bind local servers to `127.0.0.1`;
- do not execute downloaded programs;
- scan archives before extraction;
- analyze copies rather than modifying preservation masters;
- keep private collections encrypted and separate;
- review metadata before sharing a capture;
- assume a WARC may contain active or sensitive content.

## Public repository policy

The **software project** can be public. The **captured collections** should normally remain outside Git.

Commit:

```text
README.md
LICENSE
text-preserver
collections.example.toml
tests/
docs/
Makefile or pyproject.toml
```

Do not commit:

```text
collections.toml
archive/
captures/
*.warc
*.warc.gz
*.wacz
*.cdx
*.cdxj
logs containing private information
credentials or cookies
```

Recommended `.gitignore`:

```gitignore
# Local configuration
collections.toml

# Preservation data
archive/
captures/
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

# Logs and local state
*.log
.DS_Store
```

Public example configurations may contain URLs, collection descriptions, risk notes, and conservative capture recipes. They should not contain copyrighted captured content, personal paths, secrets, or claims that redistribution has been legally cleared.

## Development

A compact initial repository may use:

```text
text-preserver/
├── README.md
├── LICENSE
├── text-preserver
├── collections.example.toml
├── Makefile
├── tests/
│   └── test_smoke.py
└── .gitignore
```

As the project grows, migrate to a package layout:

```text
text-preserver/
├── pyproject.toml
├── src/
│   └── text_preserver/
│       ├── cli.py
│       ├── config.py
│       ├── models.py
│       ├── capture.py
│       ├── manifest.py
│       ├── verify.py
│       └── engines/
│           ├── base.py
│           ├── wget.py
│           └── browsertrix.py
├── tests/
├── docs/
├── collections.example.toml
├── README.md
├── CONTRIBUTING.md
└── LICENSE
```

Run syntax and integration checks with:

```bash
python3 -m py_compile text-preserver
make check
make test
```

The integration test should use only localhost. It should verify at least:

- recursive capture;
- direct-file capture;
- mirror creation and link conversion;
- WARC and CDX generation;
- multi-source collection status;
- full versus source-filtered `latest` behavior;
- manifest generation;
- successful verification;
- detection of modified and unexpected files;
- interrupted or partial capture handling;
- lock behavior;
- scope validation;
- optional WARC deduplication.

## Roadmap

### v0.1 — conservative Wget preservation

- collection/source configuration model;
- recursive web and direct-file sources;
- mirror, WARC, and CDX output;
- provenance metadata;
- SHA-256 manifests and verification;
- collection locks and capture statuses;
- ETCSL and Sacred Texts example recipes.

### v0.2 — capture quality and validation

- required URL assertions;
- expected identifier and record-count checks;
- expected MIME and file-size rules;
- broken-link auditing;
- better progress reporting;
- free-space preflight thresholds;
- machine-readable collection reports;
- safer helpers for repository records and direct bitstreams.

### v0.3 — browser-based capture

- Browsertrix adapter;
- JavaScript execution;
- scrolling and browser behaviors;
- engine-specific configuration;
- unified provenance and status across Wget and browser captures;
- WACZ output.

### v0.4 — cataloguing and access

- SQLite collection catalogue;
- URL, MIME, language, size, and status indexes;
- local full-text search;
- ReplayWeb.page and pywb integration;
- generated replay instructions;
- collection-level reports and dashboards.

### Later work

- Git, IIIF, OAI-PMH, and repository-specific source adapters;
- signed manifests;
- scheduled fixity scans;
- WARC dependency graphs;
- retention policies aware of revisit records;
- exportable collection metadata;
- optional submission workflows for external preservation services;
- packaging collection recipes independently from private captures.

## Contributing

Contributions are welcome when they improve preservation quality without weakening scope safety, provenance, or failure visibility.

In particular:

- do not hide crawler failures;
- do not make robots.txt override the default;
- do not introduce unrestricted cross-domain crawling;
- do not store secrets casually;
- do not treat a mirror as a substitute for WARC;
- do not assume captured material can be redistributed;
- add tests for changes to capture behavior;
- document new source engines and their threat models;
- keep collection-specific exceptions in configuration whenever possible.

Useful contribution areas include:

- collection recipes;
- local integration tests;
- WARC replay testing;
- Browsertrix integration;
- completeness assertions;
- metadata schemas;
- accessibility and documentation;
- cross-platform packaging.

## License

The `text-preserver` source code is licensed under the MIT License. See [`LICENSE`](LICENSE).

This license applies only to the software and documentation in this repository. It does not apply to websites, texts, images, datasets, metadata, or other material captured with the software.
