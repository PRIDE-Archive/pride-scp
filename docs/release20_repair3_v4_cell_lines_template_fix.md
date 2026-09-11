# Release20 repair3 v4: whole-file `cell-lines` template derivation

The readiness layer previously treated `cell-lines` as reusable file metadata rather than a row-derived whole-file contract. This can incorrectly retain a stale `cell-lines` declaration for mixed designs containing genuine cell-line samples and zero-cell/blank/control rows where `characteristics[cell line] = not applicable`.

Current `sdrf-pipelines` validation requires actual cell-line values for the `cell-lines` template. Therefore the readiness projection now:

- treats `cell-lines` as row-derived, alongside organism and DIA whole-file templates;
- selects it automatically only when every observed `characteristics[cell line]` value is concrete;
- drops stale historical `cell-lines` declarations for mixed files;
- preserves an explicit CLI `--template cell-lines` override for deliberate operator testing;
- regression-tests both mixed and all-cell-line designs.

This is a representation/template-selection fix. It does not change biological row annotations.
