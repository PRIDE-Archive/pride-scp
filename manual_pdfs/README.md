# Manual publication PDFs

This directory is the local fallback for manuscripts that the automatic PDF
and Europe-PMC full-text XML resolvers cannot provide.

PDF binaries are ignored by Git.

## What should I download?

After running:

```bash
scripts/run_python_publication_enrichment.sh
```

inspect:

```text
work/python/manual_manuscripts/missing_pdf_accessions.txt
work/python/manual_manuscripts/missing_pdf_publications.tsv
work/python/manual_manuscripts/missing_manuscript_accessions.txt
work/python/manual_manuscripts/missing_manuscript_publications.tsv
```

`missing_pdf_*` lists publications with no usable PDF even if XML full text was
recovered. `missing_manuscript_*` is the smaller priority queue that still has
no usable publication content after the XML fallback.

## Simplest storage convention

Save a manually downloaded article in this directory as either:

```text
PXDxxxxxx.pdf
```

or with the `suggested_filename` from the manuscript queue.

Then rerun:

```bash
scripts/run_python_publication_enrichment.sh
```

Stage 02 validates PDF magic bytes before accepting the file.

## Explicitly linking a PDF to PXD accessions

The queue utility creates:

```text
work/python/manual_manuscripts/manual_pdf_manifest.template.tsv
```

Copy it to:

```text
manual_pdfs/manual_pdf_manifest.tsv
```

and edit `pdf_path` if the downloaded filenames differ. Supported columns are:

```text
accession
publication_doi
publication_pmid
publication_pmcid
pdf_path
note
```

`pdf_path` is relative to this directory unless an absolute path is supplied.
One PDF may be mapped to multiple PXD accessions without duplicating the file:

```tsv
accession	publication_doi	publication_pmid	publication_pmcid	pdf_path	note
PXD003121				shared_oocyte_paper.pdf	shared publication
PXD004142				shared_oocyte_paper.pdf	shared publication
```

The resolver also matches manual files by DOI-derived suggested filename and by
PMCID when a manifest is used.
