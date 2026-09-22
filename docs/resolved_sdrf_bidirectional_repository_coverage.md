# Resolved SDRF bidirectional repository-coverage gate

Resolved or snapshot SDRFs enter the preservation-first path only when they are usable **and** cover the complete current PRIDE RAW inventory bidirectionally.

The gate requires:

- at least one current repository RAW file;
- a usable SDRF containing mapped `comment[data file]` rows;
- zero SDRF data files unmatched to the repository RAW inventory;
- the number of unique matched SDRF data files equals the number of current repository RAW files; and
- the number of unique SDRF data files equals the number of current repository RAW files.

This is intentionally stricter than the auditor's historical `repository_linkage_status=complete`, which only establishes that all SDRF data files map into the repository. A partial historical SDRF can therefore be link-complete in that one direction while covering only a subset of the current repository.

The rule is fail-closed. A rejected resolved SDRF does not overwrite or infer row biology; the accession remains on the existing de-novo FactorGraph path. Archive-wrapper aliases already supported by repository linkage (for example `.d` versus `.d.zip`) remain valid.

The gate establishes row/file coverage only. Existing biological-field validators remain active after preservation; complete repository coverage does not suppress contradictions such as an organism conflict.
