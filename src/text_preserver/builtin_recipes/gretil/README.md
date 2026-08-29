# GRETIL collection recipe

This recipe preserves the publisher-manifested static corpus of the Goettingen Register of Electronic Texts in Indian Languages (GRETIL). The site is available from SUB Goettingen but appears operationally frozen at update 498 from 10 September 2020.

The reviewed boundary has four independently auditable sources:

- `current-register`: the catalogue, history, Atom feed, project context, and interface assets;
- `bulk-packages`: eight cumulative ZIP files published for the language corpora and secondary resources;
- `dictionaries`: 21 XDXF files linked by the catalogue but omitted from `6_sres.zip`;
- `frozen-register`: the 2019 UTF-8, CSX, and REE indexes and their encoding documentation.

The OPAC and migrated eDocs repository are deliberately excluded. Both publish `robots.txt` rules disallowing automated access, and eDocs presented an anti-bot challenge during review. The recipe preserves the GRETIL e-library description but does not crawl catalogue sessions, resolver targets, or PDF payloads.

## Source study

A bounded read-only review on 2026-08-28 found:

- 801 TEI corpus identifiers in `gretil.html`;
- 802 analytic HTML identifiers, including one without a TEI counterpart;
- 801 plain-text transformation identifiers;
- 21 XDXF dictionaries;
- eight cumulative ZIP packages totalling about 392.5 MiB compressed and 1.49 GiB expanded;
- no publisher checksums, sitemap, or directory indexes;
- static files available over HTTPS with byte-range support;
- no redirect from the parallel HTTP site and no HSTS header on sampled responses.

The bulk packages are useful publisher artifacts, not a clean reconstruction of the public tree. They mix languages, use different internal paths, contain duplicates and leaked build paths, omit the XDXF dictionaries, and include representation mismatches. Preserve the ZIP bytes and validate them without extracting into a shared directory.

## Capture

Inspect each stage before capture:

```bash
text-preserver capture gretil --source current-register -c collections.toml --dry-run
text-preserver capture gretil --source bulk-packages -c collections.toml --dry-run
text-preserver capture gretil --source dictionaries -c collections.toml --dry-run
text-preserver capture gretil --source frozen-register -c collections.toml --dry-run
```

The static origin is limited to one request stream, randomized one-second waits, two attempts, a 2 MiB/s rate limit, explicit URLs, disabled redirects, and source-specific quotas. The seed files are documentary copies; `collection.toml` remains the executable source of capture scope.

As of 2026-08-28, verified local captures exist for all four reviewed source groups: the current register and bulk packages together, plus separate dictionary and frozen-register captures. The latter sources were captured separately to avoid downloading the unchanged 392.5 MiB package set solely to produce an aggregate report. Source-specific `LATEST-*` pointers identify each preserved group, and preservation analysis combines their verified source directories into one immutable collection-level validation.

Run completeness analysis from `LATEST` and all source pointers, or provide one or more captures explicitly:

```bash
text-preserver analyze preservation gretil -c collections.toml
text-preserver analyze preservation gretil /path/to/capture-a /path/to/capture-b -c collections.toml
```

The `validator.py` adapter checks source outcomes, selected context assets, register representation counts and lineage gaps, the exact bulk-package and dictionary sets, frozen-register files, ZIP paths, entry counts, expansion limits, encryption, symlinks, compression ratios, CRCs, TEI roots, and internal `xml:id` values. It never extracts archive members. Seventeen reviewed package filenames use longer section-qualified identifiers than the register; those mappings are explicit, while one bulk-only record and one cross-package duplicate remain warnings rather than being silently discarded.

`fixtures/reviewed-tei-ids.txt` was generated from the 1,033,721-byte `gretil.html` observed on 2026-08-28 with SHA-256 `4a510b91da2ddc98d341d3e161999f7be208dc0249faecff1735918a1945ec7f`. Tests require its 801 sorted identifiers to reproduce the adapter's baseline digest.

## Local reader

Build and open the static reader from the newest verified `bulk-packages` source capture:

```bash
text-preserver derive reader gretil -c collections.toml
text-preserver open reader gretil -c collections.toml
```

The `reader.py` adapter uses `1_sanskr.zip` as the publisher's aggregate TEI container, requires all 801 reviewed register identifiers, and explicitly excludes the one bulk-only record. It parses and writes one work at a time so the complete corpus does not need to be held in memory. The resulting reader contains full text, selected TEI header metadata, exact item-level availability and source statements, language and category labels, filename/root/register identity mappings, capture provenance, and responsive previous/next navigation.

Reader output is rebuildable derived data, not a new preservation master or a claim of collection-wide redistribution rights. It loads no scripts or remote resources. TEI mixed content, verse, page and line breaks, apparatus, supplied or unclear text, gaps, and common renditions remain visible; stand-off references and unrecognized source rendition tokens are retained as inert annotations rather than resolved externally.

## Formats and rights

Current TEI, generated HTML, plain text, and XDXF are UTF-8. Frozen CSX and REE files are legacy 8-bit byte streams and must not be decoded using the operator's locale or silently normalized. Current TEI files reference a mutable TEI release schema, while XDXF files can reference a DTD on a GitHub `master` branch; later dependency captures must record those resources as observed-at-capture rather than original historical schemas.

There is no defensible collection-wide redistribution licence. Sampled TEI files state CC BY-NC-SA 4.0, while legacy texts defer to source-file terms and dictionaries contain source-specific notices. Preserve rights and attribution statements per item and representation.

## Deferred work

- Capture the remaining publisher-linked direct corpus objects at low request rate and compare them with ZIP members.
- Capture legacy CSX and REE payloads from the frozen indexes as opaque bytes.
- Map legacy files to TEI and generated HTML without conflating versions.
- Review TextGrid and DARIAH-DE repository lineages separately.
- Revisit historical interface preservation separately from the textual-data boundary.
- Add full-text search and stable passage-level anchors without weakening source traceability.
