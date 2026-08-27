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

## Rights

This public recipe records URLs and technical observations only. It does not grant rights to redistribute captured site or corpus content. Captures and private operator configuration remain outside Git.
