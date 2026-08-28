# Internet Sacred Text Archive collection recipe

This recipe starts preservation of the Internet Sacred Text Archive (ISTA) from a fixed historical WARC rather than crawling the live publisher site without authorization.

## Current source

The required `internet-archive-2021` source preserves the complete Internet Archive item `sacred_texts_com_2021_10_09`:

- the 1,334,243,087-byte `sappho.warc.gz` captured on 2021-10-09;
- its WARC CDX and item CDX indexes;
- the item metadata XML, SQLite database, file manifest, and torrent;
- the Internet Archive metadata API response observed at capture time.

The item contains 154,080 indexed WARC data records. Reviewed CDX counts include 145,850 HTML records, 7,345 JPEGs, 368 gzip files, 307 plain-text files, 93 ZIPs, 54 GIFs, 26 MIDI files, 24 PNGs, and six PDFs. Its recorded responses include redirects and failures, so it is a strong historical baseline rather than proof that every live object existed in the crawl.

Capture and analyze it with:

```bash
text-preserver capture sacred-texts -c collections.toml --dry-run
text-preserver capture sacred-texts -c collections.toml
text-preserver analyze preservation sacred-texts -c collections.toml
```

The source uses the Internet Archive item's current direct storage host because repository download aliases redirect and capture intentionally disables redirects. Review the item metadata before changing that explicit host.

## Live capture blocker

A comprehensive unattended live crawl is not currently authorized. Section 10 of `https://sacred-texts.com/tos.htm` permits robots only for search indexing or one text download per day, while `robots.txt` excludes ZIPs and operational paths. Cloudflare also challenges GNU Wget. This project will not bypass those controls or impersonate a browser.

The live source can be added after ISTA provides written permission for one preservation snapshot, an agreed request rate, access to downloadable representations, and Cloudflare allowlisting. Historical interface recreation, commerce, analytics, external embeds, and the separate shop host are deferred.

## Intended complete boundary

When authorized, completeness means every eligible work and locally hosted representation has an auditable disposition: original HTML bytes, text and compressed editions, illustrations and content-bearing glyphs, bibliographic and attribution context, structural links, encoding evidence, and item-level rights notices. Restricted, missing, external-only, or culturally sensitive objects must remain represented in an exception ledger rather than silently disappearing.

The current sitemap index advertises four shards with roughly 143,000 HTML URLs but omits images and downloadable representations. The catalog, scanning bibliography, downloads page, media title list, change ledger, section indexes, and sitemap union must therefore be reconciled; no one publisher inventory is sufficient.

## Rights

Rights vary across public-domain source works, ISTA-produced transcriptions, modern contributed texts, copyrighted introductions, software, and images. The Internet Archive item supplies preservation bytes but no redistribution licence. Captures remain private, attribution and rights notices must stay attached, and any public access layer requires item-level review.
