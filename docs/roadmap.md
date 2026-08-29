# text-preserver implementation roadmap

This is the canonical execution roadmap for `text-preserver`. The design document
describes the long-term system; this document defines the order in which that design
is implemented and the conditions required to call each phase complete.

The roadmap is preservation-gated. Access features may develop alongside the core,
but research features cannot influence capture packages, fixity, or preservation
validation.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| Complete | Implemented, documented, and covered by the required tests. |
| In progress | Implementation exists in whole or in part but has not met every exit criterion. |
| Planned | Scope and dependencies are defined, but implementation has not started. |
| Deferred | Intentionally excluded until its stated prerequisites exist. |

## Architectural decisions

These decisions apply to every phase:

- Keep one repository and one Python distribution until independent consumers justify separate releases.
- Treat preservation, access, and research as dependency boundaries, not merely filesystem names.
- Allow dependencies only in the direction `preservation -> access -> research`, where arrows mean that the downstream layer consumes stable outputs from the upstream layer.
- Keep CLI and configuration code as application wiring above the workflow layers.
- Classify completeness validation as preservation work even though validation reports are rebuildable files under `derived/`.
- Keep finalized captures immutable. New schemas and contracts must not rewrite old captures.
- Retain a version-1 recipe interpreter because immutable captures already contain `recipe_api = 1` bundles.
- Do not create a research package until the first concrete research feature is implemented.
- Do not claim that subprocess separation is a complete cross-platform sandbox.
- Do not make live crawling part of routine CI.

## Current baseline

The following foundations are complete:

- guarded GNU Wget execution with WARC, CDX, mirror, logs, source metrics, and retained failure evidence;
- substantive WARC and mirror payload accounting, including correct WARC-only partial status;
- capture fixity manifests and immediate verification;
- collection and source capture pointers with conservative update rules;
- immutable, input-addressed, aggregate preservation validations and `LATEST-VALIDATED`;
- complete captured recipe bundles with canonical manifests and hashes;
- separate ETCSL and GRETIL reader adapters;
- the Item/Representation/Relation design model;
- runtime and JSON Schema conformance tests;
- Linux and macOS source, distribution, clean-wheel, and installed fixture-flow CI;
- explicit documentation that recipe adapters are trusted executable code;
- documentation distinguishing fixity from authenticity;
- ETCSL and GRETIL preservation vertical slices;
- a bounded Sacred Texts scale test whose remaining completeness gaps are explicitly recorded.

The preservation/access package boundary is implemented. Capture, fixity, recipe
bundles, and validation live under `text_preserver.preservation`; reader construction
lives under `text_preserver.access`. Recursive architecture tests enforce the allowed
dependency direction.

## Dependency order

```text
Phase 1: layer boundaries
        |
        v
Phase 2: lifecycle and deterministic derivations
        |
        v
Phase 3: recipe API and distribution
        |                         |
        v                         v
Phase 4: unified access     Phase 5: execution hardening
        |                         |
        v                         v
Phase 7: research clients  Phase 6: roles and interoperability
```

Phases may overlap only when the dependency and format contracts they consume are
already fixed by an earlier phase.

## Phase 1: enforce layer boundaries

**Status:** Complete

**Purpose:** Make preservation, access, and future research responsibilities visible
in the Python package and enforce their dependency direction.

**Deliverables**

- Keep preservation validation in `text_preserver.preservation.validation`.
- Keep reader construction and publication in `text_preserver.access.reader`.
- Move capture planning, capture execution, fixity, and recipe-bundle preservation under `text_preserver.preservation`.
- Keep neutral configuration, adapter loading, and bounded filesystem utilities outside access and research workflows.
- Remove the former mixed `text_preserver.analysis` module rather than retaining an undocumented compatibility facade.
- Keep `text_preserver.research` absent until Phase 7 begins.
- Add recursive AST-based tests that reject forbidden imports between layers.
- Document the ownership and write boundary of each layer.

**Exit criteria**

- Preservation modules do not import access or research modules.
- Access modules do not import research modules or capture-execution internals.
- Access operates on verified preservation outputs through explicit APIs.
- The CLI remains the composition root for capture, validation, and reader commands.
- Source tests, wheel tests, and the installed capture-to-reader flow pass without `text_preserver.analysis`.
- No capture schema, validation identity, pointer, or recipe behavior changes as part of the move.

## Phase 2: separate lifecycle state and make derivations deterministic

**Status:** Complete

**Purpose:** Give acquisition, fixity, validation, and access independent meanings,
then make every derivation from sealed bytes reproducible and input-addressed.

**Deliverables**

- Define explicit acquisition, fixity, validation, and access state fields in one read model without collapsing them into a single status.
- Add a machine-readable collection lifecycle status command backed by that read model.
- Introduce an explicit `LATEST-ACQUIRED` pointer for successful full acquisition.
- Retain safe reading of existing `LATEST` pointers because archives already contain them.
- Keep `LATEST-VALIDATED` limited to successful validation reports.
- Define `LATEST-READER` metadata while retaining the stable local reader path used by `open reader`.
- Add `text-preserver validate` as the preservation-facing command.
- Keep `analyze preservation` as a deprecated alias through version 0.1 because it is already documented and used by operator scripts; do not remove it before version 0.2.
- Define a reader build key from capture manifest digests, selected sources, recipe-bundle identity, renderer identity, canonical build options, and reader schema version.
- Exclude creation time and absolute local paths from build identities.
- Compute and record a canonical output-tree digest for every reader generation.
- Reuse an existing generation when both build key and tree digest match.
- Quarantine and report a reproducibility failure when the same build key produces a different output tree.

**Exit criteria**

- A machine-readable status command reports all four lifecycle dimensions independently.
- An incomplete validation cannot replace the acquired or validated pointer.
- An incomplete reader cannot replace the current reader pointer.
- Two identical reader builds produce the same build key and output-tree digest.
- A changed renderer, recipe bundle, capture manifest, or build option changes the build key.
- Tests cover legacy `LATEST`, all new pointers, deterministic reuse, and mismatched output detection.

## Phase 3: stabilize recipe API and built-in distribution

**Status:** Complete

**Purpose:** Separate recipe capabilities cleanly and remove fragile built-in recipe
file enumeration without invalidating captured version-1 recipes.

**Deliverables**

- Freeze the existing `recipe_api = 1` interpreter for immutable captured bundles.
- Define `recipe_api = 2` with explicit validator and reader capabilities.
- Replace the misleading current `inventory_adapter` role with a validator contract for new recipes.
- Define small typed contexts and reports for validation and reader derivation.
- Keep collection parsing, mappings, terminology, and source-specific assumptions inside recipe bundles.
- Permit recipe-local shared modules for parsers used by both validator and reader adapters.
- Migrate current ETCSL, GRETIL, and Sacred Texts recipes to version 2.
- Continue validating historical captures with their preserved version-1 recipe code.
- Package built-in recipe directories as importable resources located with `importlib.resources`.
- Remove manual per-file recipe enumeration and custom `sysconfig` discovery.
- Defer third-party entry-point discovery until Phase 5 provides process separation.

**Exit criteria**

- The core can select the correct interpreter from a captured recipe API version.
- Version-1 fixture captures still validate and build their supported access outputs.
- Current built-in recipes use version-2 validator and reader contracts.
- Adding a regular file to a built-in recipe does not require another `pyproject.toml` data-file entry.
- Source, wheel, sdist, and target-style installs expose identical complete recipe bundles.
- Contract tests reject undeclared capabilities and malformed adapter responses.

## Phase 4: build the unified access reader

**Status:** Complete

**Purpose:** Give every collection a consistent local access experience without
forcing different source formats into one scholarly hierarchy.

The shared reader is a library of contracts and composable components, not one
mandatory page template. Recipes retain collection-specific layouts, terminology,
text structures, and visual extensions while reusing the inert document envelope,
navigation, status, provenance, and asset foundations.

**Deliverables**

- Add a shared reader shell with common typography, navigation, responsive layout, and inert local assets.
- Define a minimal access model for Collection, Item, Representation, Relation, Segment, Artifact, rights, and provenance.
- Keep source-specific textual bodies and structures under recipe control.
- Provide shared components for breadcrumbs, collection status, validation warnings, rights, provenance, artifact links, and citations.
- Provide stable collection, item, representation, and segment URLs where the source supports those identities.
- Migrate ETCSL while retaining its side-by-side transliteration and translation presentation.
- Migrate GRETIL while retaining TEI mixed content, apparatus, item-level rights, and package provenance.
- Add a Sacred Texts collection and preservation-status view without presenting the incomplete collection as complete.
- Add a common collection catalogue.
- Add SQLite FTS5 as a rebuildable access index after the shared item contract is stable.
- Keep all generated assets under `derived/` and all reader builds governed by Phase 2 identities.

**Exit criteria**

- ETCSL and GRETIL use the same shell primitives and navigational conventions while retaining collection-specific layouts and visual extensions.
- Collection-specific pages retain their distinct scholarly structures.
- Readers work without JavaScript or network access and remain usable in EWW and normal browsers.
- Deleting the reader and search index does not affect archive verification or validation.
- Reader output contains stable source-artifact and representation links suitable for future research clients.
- Fixture tests cover common shell behavior and collection-specific rendering.

## Phase 5: harden execution, privacy, and network scope

**Status:** Complete

**Purpose:** Prepare for externally supplied recipes and shareable exports without
overstating sandbox guarantees.

**Deliverables**

- Define a structured JSON request/response protocol for adapter subprocesses.
- Run adapters outside the main process with a dedicated temporary output directory.
- Make capture input read-only to the adapter wherever the operating system permits.
- Disable adapter network access by default where enforceable and clearly report platforms where it is not enforceable.
- Add bounded execution time, output size, and memory controls where supported.
- Keep trusted in-process execution available only as an explicit policy for preserved version-1 adapters if required.
- Split shareable provenance from operator-private metadata such as hostname, process ID, executable path, and private filesystem paths.
- Add an export profile that omits or redacts operator-private metadata.
- Add redirect discovery that records each proposed target without following it.
- Add explicit reviewed redirect targets or a per-hop allowlist before redirects can be followed.
- Add lint and static type checks as development-only CI jobs.

**Exit criteria**

- A failing or malformed adapter cannot corrupt archive masters or already-published derived generations.
- Timeout, oversized output, invalid JSON, and process failure produce bounded normal error reports.
- Documentation distinguishes process separation, operating-system controls, and full sandboxing.
- A public export contains no configured private provenance fields.
- Redirect tests prove that unreviewed cross-host targets are never followed.
- Third-party recipe discovery remains disabled until these tests pass on Linux and macOS.

## Phase 6: classify payload roles and add interoperable exports

**Status:** Complete

**Purpose:** Make original evidence and capture-time derivatives machine-readable,
then provide portable exports without rewriting immutable captures.

This phase may begin once the private-provenance model from Phase 5 is fixed; it
does not wait for third-party adapter discovery or complete cross-platform process
isolation.

**Deliverables**

- Define payload roles such as `preservation_original`, `capture_derivative`, and `metadata` in the next capture schema.
- Classify WARC and direct deposits as preservation evidence and converted Wget mirrors as capture-time access derivatives.
- Keep support for existing schema versions whose role is inferred from their fixed paths and source metadata.
- Decide whether new captures need a clearer physical payload layout only after role metadata is proven sufficient or insufficient.
- Implement BagIt export for a complete verified capture package.
- Include clear mapping from capture files to BagIt payload and tag files.
- Preserve capture and recipe manifests in the export and record the export-tool version.
- Implement WACZ as a derived web-replay export after BagIt and WARC role classification are stable.
- Keep OCFL deferred unless the project becomes a repository managing long-lived object versions.

**Exit criteria**

- Every payload in a new capture has an explicit machine-readable role.
- Existing version-1 and version-2 captures remain verifiable without mutation.
- BagIt export followed by validation preserves every expected byte and relevant manifest.
- Export profiles apply the private-provenance policy from Phase 5.
- WACZ creation never replaces or modifies the source WARC.
- Round-trip and malformed-package tests run without network access.

## Phase 7: add research clients

**Status:** Deferred

**Purpose:** Add personal scholarship only after stable preservation and access
identifiers exist.

**Prerequisites**

- Phase 1 dependency boundaries are enforced.
- Phase 3 recipe contracts are stable.
- Phase 4 exposes stable item, representation, artifact, and segment identifiers.
- Workspace data can be deleted without affecting archive or access verification.

**Potential deliverables**

- W3C-compatible annotations stored under `workspace/`.
- Org links to stable access identifiers.
- Provenance-aware claims and evidence links.
- Optional Ciao consumers of canonical JSON or SQLite facts.
- Saved searches and reading state.
- Optional embeddings and semantic proposals after explicit review workflows exist.

**Exit criteria**

- Research modules depend only on documented preservation and access formats.
- Preservation and access do not import research modules.
- Removing `workspace/` does not alter captures, validations, readers, or indexes.
- Ciao, Org, embeddings, and model availability remain optional.

## Version 0.1 release gate

Version 0.1 is a preservation release. Access may ship as a preview, but research is
not part of the release gate.

The release requires:

- Phase 1 layer boundaries complete;
- explicit acquisition, fixity, and validation states from Phase 2;
- deterministic validation identities and reader identities for any access feature shipped;
- a frozen capture schema with explicit payload roles;
- versioned recipe interpretation and complete recipe-bundle preservation;
- current ETCSL and GRETIL recipes operating through the stable contracts;
- Sacred Texts reporting its known incomplete state honestly;
- separate public and operator-private provenance for exported packages;
- BagIt or an equivalent complete-package export;
- Linux and macOS source and clean-wheel end-to-end CI;
- documented trusted-code, privacy, redirect, fixity, authenticity, and rights boundaries.

The following do not block version 0.1:

- Browsertrix;
- WACZ replay export;
- full-text search;
- Org integration;
- annotations and claims;
- Ciao validation;
- embeddings or LLM-assisted features;
- complete Sacred Texts preservation while legitimate official media remains unavailable.

## Critique traceability

| Concern | Roadmap location | Current state |
| --- | --- | --- |
| Separate acquisition success from preservation completeness | Phase 2 | Complete. |
| Correct WARC-only status | Baseline | Complete. |
| Preserve versioned recipe bundles | Baseline and Phase 3 | Complete, including recursive built-in resource distribution. |
| Split adapter responsibilities | Phases 1 and 3 | Complete through API 2 validator and reader capabilities. |
| Treat recipe code as trusted or isolate it | Phase 5 | Complete with bounded subprocess isolation and explicit limits. |
| Immutable input-addressed validations | Baseline | Complete. |
| Deterministic reader generations and output digest | Phase 2 | Complete. |
| Clarify original payloads and capture-time derivatives | Phase 6 | Complete in capture schema 3 with legacy inference. |
| Weaker Item/Representation/Relation model | Baseline | Complete in the design. |
| Runtime and JSON Schema drift | Baseline | Conformance tests complete. |
| Split public and private provenance | Phase 5 | Complete with explicit private paths and public export allowlists. |
| Distinguish fixity from authenticity | Baseline | Complete in the design. |
| Safe redirect handling | Phase 5 | Complete with WARC discovery and exact reviewed per-hop targets. |
| Cross-platform clean-wheel CI | Baseline | Complete. |
| BagIt and WACZ interoperability | Phase 6 | Complete with offline creation and independent validation. |
| Unified reader with collection-specific semantics | Phase 4 | Complete with composable shell/model contracts, Sacred Texts status access, common catalogue, and SQLite FTS5. |
| Keep research downstream and optional | Phase 7 | Enforced as a deferral. |

## Roadmap maintenance

- Update phase status only when its exit criteria are met.
- Record intentionally deferred work instead of silently dropping it.
- Add new work to the earliest phase whose contracts it depends on.
- Do not add a feature to version 0.1 merely because it is interesting; it must strengthen preservation stability or be an explicitly non-blocking access preview.
- Keep collection acquisition blockers separate from software completion. Missing authorized source media is not fixed by weakening validation.
