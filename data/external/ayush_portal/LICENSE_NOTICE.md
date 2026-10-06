# Ayush Research Portal license notice

Checked on 6 October 2026 from <https://arp.ayush.gov.in/termsandcondition>
and the disclaimer text in the portal page source. The portal is the Ayush
Research Portal of the Ministry of Ayush, Government of India, developed by
Ayush Grid, with content associated with NIIMH Hyderabad (CCRAS). It is a
bibliographic index. This pipeline stores bibliographic fields only.

## What the portal says

The terms page is a liability disclaimer. It says the site was developed to
provide information to the general public, that the content should not be
construed as a statement of law, and that the Ministry of Ayush is not liable
for loss from use of the data. It does not state a scraping rule, a rate
limit, or a copyright licence.

A disclaimer carried on the portal (and shown on the legacy site) says the
material is for disseminating the knowledge of Ayush systems and current
research updates and is **purely meant for academic purpose**. The Ministry
is not responsible for findings or claims published from other sources and
cited on the portal.

Publisher abstracts and uploaded PDFs are third-party copyright. This
pipeline does not store abstracts and does not download PDFs. GODL-India is
not treated as covering the portal: no ARP dataset was found on data.gov.in.

## Usage basis

Herbenzo Ayurvedic and Herbal Pvt Ltd (MSME) may use the portal for research.
On 6 October 2026 in Delhi, Srikanth, Deputy Director of CCRAS, told Shivasai
that there is no permission requirement for research use and that Herbenzo
may use the repository. That verbal confirmation is the basis recorded here.
It is not a permission-email gate. The client stays off unless
`HERBENZO_AYUSH_PORTAL_ENABLED` is set, which is an operational switch, not a
licence block.

Defaults:

- `HERBENZO_AYUSH_PORTAL_LICENSE_BASIS=verbal_authorization`
- `HERBENZO_AYUSH_PORTAL_PERMISSION_REF=CCRAS Deputy Director Srikanth, Delhi, 2026-10-06; research use permitted for Herbenzo Ayurvedic and Herbal Pvt Ltd`

Every stored bibliographic record copies those values and an attribution line.
The switch does not have to be cleared by a further approval step before a
research lookup.

## Attribution

Use this form, with the record permalink, the ARP ID, and the UTC retrieval date:

```text
Source: Ayush Research Portal, Ministry of Ayush, Government of India — <record URL> (ARP ID <id>), retrieved <date>
```

When a record has a PMID, cite PubMed. When it has a DOI and no PMID, cite the
DOI. Records with neither stay low-confidence until a reviewer accepts them.
The portal record remains the discovery pointer, not a substitute for the
publisher text.

## Contacts

NIIMH Hyderabad (CCRAS): `ayushportal-ccras@gov.in`, `niimh-hyderabad@gov.in`.
Ministry of Ayush / Ayush Grid: `ayush-grid@gov.in`.
Portal: <https://arp.ayush.gov.in/>.
