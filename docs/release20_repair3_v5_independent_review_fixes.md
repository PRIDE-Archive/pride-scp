# Release20 repair3 v5 — independent exact-hash review fixes

Date: 2026-09-11

The repair3-v4 exact-hash review rejected all three projected hashes after source-level adversarial review. These were scientific/source-grounding defects, not schema/validator failures.

## PXD019515

Source evidence distinguishes cultured HeLa cells, cell-free supernatant blanks, and primary spinal-tissue neurons captured by LCM. The selected single-cell method used HCD at 30% NCE; database-search precursor/fragment tolerances were <5 ppm / 20 ppm.

v5 repairs:
- blanks: material type and LCM microscope model -> `not applicable`;
- HeLa rows: material type -> `cell line`; LCM microscope model -> `not applicable`;
- motor/interneurons: material type -> `cell`; LCM microscope model -> `Zeiss PALM MicroBeam`;
- all rows: HCD, `30% NCE`, precursor tolerance `5 ppm` upper-bound representation.

Evidence:
- https://doi.org/10.1039/D0SC03636F
- https://pmc.ncbi.nlm.nih.gov/articles/PMC8178986/

## PXD019958

The biological material is U-87MG ATCC (CVCL_0022), but Cellosaurus states age at sampling is unspecified and the ATCC line's original anatomical origin is unknown. Zero-cell controls contain no biological cell.

v5 repairs:
- biological U-87MG rows: organism part -> `not available`; developmental stage -> `not available`;
- zero-cell rows: clear cell-line/Cellosaurus/material/disease/sex/developmental/anatomical/cell-identifier leakage using `not applicable` / `empty` as appropriate;
- preserve source-supported U-87MG/CVCL_0022, glioblastoma, and male sex on biological rows.

Evidence:
- https://doi.org/10.1038/s41467-020-19394-5
- https://www.cellosaurus.org/CVCL_0022

## PXD062702

The project contains Q Exactive Plus Eco-DDA HeLa/Xenopus runs and Fusion Lumos nanoLC-WWA comparator runs. The Supporting Information explicitly states:
- QE+ Top-N acquisition is DDA, HCD, 28% NCE;
- Lumos WWA is DDA with wider precursor isolation windows, HCD, 30% NCE;
- the HeLa benchmark material is a commercial HeLa proteome digest standard.

v5 repairs:
- all WWA rows: DDA, not DIA;
- QE+ DDAtop/Xenopus rows: HCD at 28% NCE;
- Fusion Lumos WWA rows: HCD at 30% NCE;
- HeLa digest standard material type -> `not available` rather than `cell`.

Evidence:
- https://doi.org/10.1002/anie.202510692
- Supporting Information: https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fanie.202510692&file=anie202510692-sup-0001-SuppMat.pdf
- WWA semantics: https://doi.org/10.1002/anie.202303415

## Generic hardening

Scientific guard v0.3 and Rust draft validation now fail closed on:
- WWA filenames labeled DIA;
- Q Exactive-family rows labeled generic CID (`MS:1000133`);
- concrete LCM microscope models on non-LCM rows;
- zero-cell controls carrying concrete cell-line / Cellosaurus / biological material / cell identifier identity.

Contextual biological metadata on zero-cell controls (organism part, disease, developmental stage, sex) is surfaced as a warning rather than a universal hard failure, avoiding over-generalization while still requiring review.

The v0.5.14.3 compatibility architecture is unchanged.
