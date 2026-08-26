# Manual publication PDFs

This directory is a local fallback for publications that the automatic OA
resolver cannot download.

PDF files themselves are ignored by Git.  Place a manually downloaded article
here using either:

- `PXDxxxxxx.pdf`, or
- the suggested filename from `work/python/manual_pdf_queue.tsv`.

For shared publications or non-standard filenames, create
`manual_pdf_manifest.tsv` with columns such as:

```tsv
accession	publication_doi	publication_pmid	pdf_path	note
PXD003121			shared_oocyte_paper.pdf	shared publication
PXD004142			shared_oocyte_paper.pdf	shared publication
```

`pdf_path` may be relative to this directory.  Every supplied file is validated
for a real PDF signature before it is accepted.
