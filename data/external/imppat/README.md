# IMPPAT 3.0

IMPPAT (**Indian Medicinal Plants, Phytochemistry And Therapeutics**) is a
manually curated database built by the Computational Biology Group at
The Institute of Mathematical Sciences (IMSc), Chennai. Stage B uses a local
copy as advisory context after NCBI Taxonomy, and copies a matched context
onto an approved registry overlay row. The database files themselves are not
part of this repository.

- Site: <https://cb.imsc.res.in/imppat/>
- Downloads: <https://cb.imsc.res.in/imppat/download>
- Version checked: **3.0**, released **30 September 2026**. The homepage calls
  this the current version and dates the release 30 September 2026. The
  download table lists the same release date (`30/09/2026`) for every batch
  file, and the site footer says "Last updated: 30/09/2026".
- Checked on: 6 October 2026.

## License wording found on the site

The footer of <https://cb.imsc.res.in/imppat/> and
<https://cb.imsc.res.in/imppat/download> (the same block is on the help and
acknowledgement pages) says:

> This work is licensed under a Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International License.

The link is marked `rel="license"` and points to
<http://creativecommons.org/licenses/by-nc-nd/4.0/>, which resolves to
<https://creativecommons.org/licenses/by-nc-nd/4.0/>.
The badge alt text is "Creative Commons License".
Legal code: <https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode>.

Read [LICENSE_NOTICE.md](LICENSE_NOTICE.md) before downloading. It records the
usage basis for this repository.

The download page does not add a separate terms-of-use statement. The license
that applies to the batch files is the footer above. The homepage citation
block is the citation requirement (below). The help page also says to cite
IMPPAT, as specified on the home page, when publishing images exported from
IMPPAT-KG.

The IMPPAT 2.0 paper (ACS Omega, 2023) says the compiled information in
IMPPAT 2.0 was released under CC BY-NC 4.0. The live 3.0 site footer, checked
on 6 October 2026, states CC BY-NC-ND 4.0. Treat the current website footer as
the license for the 3.0 database.

## Usage basis

Herbenzo Ayurvedic and Herbal Pvt Ltd holds MSME registration. The IMPPAT team
confirmed by email on 6 October 2026 (per the owner; keep that email on file)
that an MSME entity can use IMPPAT without formal approval. IMPPAT may be used
directly in this pipeline.

The email covers use, not redistribution. Do not commit the batch files,
subsets, or a dump of the tables. `scripts/fetch_imppat.py` writes only to the
gitignored cache.

## Citation

The homepage citation section
(<https://cb.imsc.res.in/imppat/>, heading "CITATION") says:

> If you use our resource, please cite the following three research articles:

1. Karthikeyan Mohanraj, Bagavathy Shanmugam Karthikeyan, R.P. Vivek-Ananth,
   R.P. Bharath Chand, S.R. Aparna, P. Mangalapandi, and Areejit Samal.
   IMPPAT: A curated database of Indian Medicinal Plants, Phytochemistry And
   Therapeutics. *Scientific Reports* 8:4329 (2018).
   <https://www.nature.com/articles/s41598-018-22631-z>
2. R. P. Vivek-Ananth, Karthikeyan Mohanraj, Ajaya Kumar Sahoo, and Areejit Samal.
   IMPPAT 2.0: An Enhanced and Expanded Phytochemical Atlas of Indian Medicinal
   Plants. *ACS Omega* 8:8827–8845 (2023).
   <https://pubs.acs.org/doi/10.1021/acsomega.3c00156>
3. Shanmuga Priya Baskaran, Ajaya Kumar Sahoo, Priyotosh Sil, Rahul Tiwari,
   Nikhil Chivukula, Sabrina Elsa Eapen, Geetha Ranganathan, Preeti Semwal,
   and Areejit Samal.
   IMPPAT 3.0: An updated FAIR database of phytochemicals and formulations of
   Indian Medicinal plants. **submitted (2026)**.
   On 6 October 2026 the site linked this item to
   <https://cb.imsc.res.in/imppat> and listed no journal or DOI.

Approved overlay rows that carry IMPPAT fields store this source and these
citations with the row.

## Do not commit or redistribute the batch files

The cache directory is gitignored. A missing cache, a missing hit, or an
ambiguous hit does not block enrichment or approval.

Contact verified on the site: **Areejit Samal**, Computational Biology Group,
The Institute of Mathematical Sciences (IMSc), Chennai. The homepage
`<meta name="author">` is `Areejit Samal (asamal@imsc.res.in)`. The CONTACT
section links a Cloudflare-protected mailto for Areejit Samal that decodes to
the same address. The download-page contact line points at
<https://asamallab.github.io/contact.html>.

## Local download

`scripts/fetch_imppat.py` downloads the four TSVs below. It prints this
license and attribution notice on every run. `--accept-noncommercial-license`
is still accepted and does nothing.

```bash
python scripts/fetch_imppat.py
```

Files are written only to a local cache:

- default: `data/external/imppat/cache/` (gitignored)
- override: `--cache-dir ~/.cache/herbenzo/imppat`

When that cache exists, enrichment looks it up after NCBI Taxonomy.
`HERBENZO_IMPPAT_DIR=off` skips the lookup.

Chosen batch files (under
`https://cb.imsc.res.in/imppat/images/Batch_Download/`):

| File | Role on the download page |
| --- | --- |
| `Plant_Information_IMPPAT.tsv` | Information on medicinal plants |
| `IMPPAT_SingleHerbalFormulations.tsv` | Single herbal formulations |
| `IMPPAT_PolyHerbalFormulations.tsv` | Polyherbal formulations |
| `IMPPAT_Phytochemical_Plant_Association.tsv` | Plant–phytochemical association |
