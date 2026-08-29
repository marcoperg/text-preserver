# Internet Sacred Text Archive collection recipe

This recipe starts preservation of the Internet Sacred Text Archive (ISTA) from a fixed historical WARC rather than crawling the live publisher site without authorization. The collection is currently incomplete.

## Current baseline

The required `internet-archive-2021` source preserves the complete Internet Archive item `sacred_texts_com_2021_10_09`:

- the 1,334,243,087-byte `sappho.warc.gz` captured on 2021-10-09;
- its WARC CDX and item CDX indexes;
- the item metadata XML, SQLite database, file manifest, and torrent;
- the Internet Archive metadata API response observed at capture time.

The item contains 154,080 indexed WARC data records. That number counts HTTP capture records, not books or intellectual works. Reviewed CDX counts include 145,850 HTML records, 7,345 JPEGs, 368 gzip files, 307 plain-text files, 93 ZIPs, 54 GIFs, 26 MIDI files, 24 PNGs, and six PDFs. Its recorded responses include redirects and failures, so it is an independently useful historical baseline rather than a complete ISTA corpus.

Capture and analyze it with:

```bash
text-preserver capture sacred-texts -c collections.toml --dry-run
text-preserver capture sacred-texts -c collections.toml
text-preserver analyze preservation sacred-texts -c collections.toml
```

The source uses the Internet Archive item's current direct storage host because repository download aliases redirect and capture intentionally disables redirects. Review the item metadata before changing that explicit host.

### Downloads-page recovery

The 2021 WARC successfully captured 348 of the 358 payloads advertised by `download.htm`: 340 of 350 gzip ebooks, both featured ZIPs, and all six ZIPs linked by the Bible data index. Of the ten failures, Wayback retained successful historical gzip responses for three that returned `500` in the WARC. The required `wayback-download-recovery` source preserves those exact responses:

- `earth/sym/sym.txt.gz` (`Symzonia; Voyage of Discovery`);
- `sym/mosy/mosy.txt.gz` (`The Migration of Symbols`);
- `sym/bot/bot.txt.gz` (`The Book of Talismans`).

Together, verified captures now preserve 351 of 358 advertised payloads (98.0%). The remaining seven exact gzip files returned `404` in the WARC and have no successful captures in the Internet Archive or Arquivo.pt indexes checked on 2026-08-29:

- `aor/darwin/origin/or.txt.gz`;
- `chr/bunyan/pp.txt.gz`;
- `chr/aquinas/summa/sum.txt.gz`;
- `etc/fwe/fwe.txt.gz`;
- `pag/iwd/iwd.txt.gz`;
- `sro/mmm/mmm.txt.gz`;
- `sro/hkt/hkt.txt.gz`.

Their underlying seven works are not absent: the WARC has successful HTML response sets for every corresponding book directory, including 19, 23, 657, 18, 16, 14, and 22 HTML pages respectively. The unresolved gap is the exact publisher-generated single-file representation. It remains pending publisher-media acquisition or a permitted recovery source; this project will not exceed ISTA's one-text-per-day robot limit.

## Missing publisher media

ISTA's official DVD-ROM/USB 9.0 is the best known fixed corpus distribution. The publisher describes it as containing all site content through mid-October 2009, including downloadable plain-text editions not consistently present in the WARC. Its published inventory is:

- 2,988,233,761 logical bytes;
- 173,566 files;
- 2,884 directories;
- about 2,000 books.

The 1,334,243,087-byte compressed WARC cannot be compared directly with the media's logical byte count. The inventories nevertheless differ materially, and the WARC contains failed responses for some advertised downloadable representations. Therefore preservation analysis reports `incomplete` until a legitimately acquired 9.0 image or extracted byte-for-byte file tree, its filesystem manifest, and fixity metadata are captured.

This recipe deliberately analyzes earlier captures with the current inventory adapter so newly established collection-level gaps can downgrade an old artifact-level result. The adapter embedded in each immutable capture remains preserved for reproducing the historical assessment.

The authoritative publisher references are the [9.0 inventory and upgrade description](https://sacred-texts.com/cdshop/dvd90up/order.htm), [product comparison](https://sacred-texts.com/cdshop/compare.htm), [title list](https://sacred-texts.com/cdshop/dvd90/titl90.htm), and [subject list](https://sacred-texts.com/cdshop/dvd90/subj90.htm). The title and subject lists are useful reconciliation inputs but do not enumerate every short document or representation.

## Live capture blocker

A comprehensive unattended live crawl is not currently authorized. Section 10 of `https://sacred-texts.com/tos.htm` permits robots only for search indexing or one text download per day, while `robots.txt` excludes ZIPs and operational paths. Cloudflare also challenges GNU Wget. This project will not bypass those controls or impersonate a browser.

The live source can be added after ISTA provides written permission for one preservation snapshot, an agreed request rate, access to downloadable representations, and Cloudflare allowlisting. Historical interface recreation, commerce, analytics, external embeds, and the separate shop host are deferred.

## Complete boundary

Completeness first requires the official 9.0 media distribution. Any authorized later-site capture must then be reconciled as a temporal supplement, not treated as a substitute. Every eligible work and locally hosted representation needs an auditable disposition: original HTML bytes, text and compressed editions, illustrations and content-bearing glyphs, bibliographic and attribution context, structural links, encoding evidence, and item-level rights notices. Restricted, missing, external-only, or culturally sensitive objects must remain represented in an exception ledger rather than silently disappearing.

The current sitemap index advertises four shards with roughly 143,000 HTML URLs but omits images and downloadable representations. The catalog, scanning bibliography, downloads page, media title list, change ledger, section indexes, and sitemap union must therefore be reconciled; no one publisher inventory is sufficient.

## Rights

Rights vary across public-domain source works, ISTA-produced transcriptions, modern contributed texts, copyrighted introductions, software, and images. The Internet Archive item supplies preservation bytes but no redistribution licence. Captures remain private, attribution and rights notices must stay attached, and any public access layer requires item-level review.
