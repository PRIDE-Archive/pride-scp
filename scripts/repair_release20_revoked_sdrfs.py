#!/usr/bin/env python3
"""Deterministic source-grounded repairs for release20 hashes revoked by upstream review.

This is a curation repair lane, not a compatibility normalizer.  Every accession-specific change is
bound to explicit repository/publication evidence and the script refuses to run unless the input file
matches the originally reviewed release20 SHA-256.
"""
from __future__ import annotations

import argparse, csv, hashlib, json, shutil
from pathlib import Path
from typing import Callable

from sdrf_scientific_guard import analyze as analyze_guard

EXPECTED = {
    "PXD019515": "afc12f7907b0c516f0713506447ee90cbbc9c886656f1d862eee61accd38a9e4",
    "PXD019958": "6c113d38c309aa76e224623e93ee0ecb8a46e850f602889bad75752efc5ad072",
    "PXD062702": "ef887fa1392c2375c36c65524362ac82e06ed2a35f48a759b7c37c664c45972d",
}

EVIDENCE = {
    "PXD019515": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD019515",
        "https://doi.org/10.1039/D0SC03636F",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC8178986/",
        "upstream review #462",
    ],
    "PXD019958": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD019958",
        "https://doi.org/10.1038/s41467-020-19394-5",
        "https://www.cellosaurus.org/CVCL_0022",
        "upstream review #463",
        "SDRF v1.1 single-cell reserved-word semantics",
    ],
    "PXD062702": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD062702",
        "https://doi.org/10.1002/anie.202510692",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC12582007/",
        "https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fanie.202510692&file=anie202510692-sup-0001-SuppMat.pdf",
        "https://doi.org/10.1002/anie.202303415",
        "upstream review #476",
    ],
}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        r = csv.DictReader(fh, delimiter="\t")
        return list(r.fieldnames or []), [{k: str(v or "") for k, v in row.items()} for row in r]


def write(path: Path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def setv(row, key, value, changes, reason):
    old = row.get(key, "")
    if old != value:
        row[key] = value
        changes.append({"data_file": row.get("comment[data file]", ""), "field": key, "old": old, "new": value, "reason": reason})


def repair_019515(rows, changes):
    for row in rows:
        fn = row.get("comment[data file]", "")
        # The publication explicitly distinguishes cultured HeLa cells, human spinal-tissue neurons,
        # and cell-free blanks. Do not project HeLa identity onto neuronal/blank rows.
        if "Blank" in fn:
            for h in ("characteristics[organism part]", "characteristics[cell type]", "characteristics[cell line]",
                      "characteristics[individual]", "characteristics[single cell isolation protocol]"):
                setv(row, h, "not applicable", changes, "cell-free blank control")
            setv(row, "characteristics[material type]", "not applicable", changes,
                 "cell-free supernatant blank contains no tissue/cell material")
            setv(row, "comment[lcm microscope model]", "not applicable", changes,
                 "blank control was not collected by laser capture microdissection")
        elif "Single_HeLa" in fn:
            setv(row, "characteristics[organism part]", "not applicable", changes, "cell-line sample; no source-backed anatomical organism part")
            setv(row, "characteristics[cell line]", "HeLa", changes, "publication: single HeLa cells")
            setv(row, "characteristics[cell type]", "not available", changes, "no more-specific cell type supplied")
            setv(row, "characteristics[individual]", "not applicable", changes, "cell line is not an individual/donor")
            setv(row, "characteristics[single cell isolation protocol]", "manual aspiration", changes,
                 "publication: single HeLa cells were aspirated")
            setv(row, "characteristics[material type]", "cell line", changes,
                 "publication: cultured HeLa cell-line material")
            setv(row, "comment[lcm microscope model]", "not applicable", changes,
                 "HeLa cells were aspirated, not laser-capture microdissected")
        elif "SingleMotorNeuron" in fn:
            setv(row, "characteristics[organism part]", "spinal cord", changes, "publication: human spinal tissue")
            setv(row, "characteristics[cell type]", "motor neuron", changes, "publication/file identity")
            setv(row, "characteristics[cell line]", "not applicable", changes, "primary neuron is not HeLa")
            setv(row, "characteristics[individual]", "not available", changes, "deidentified tissue; donor not supplied")
            setv(row, "characteristics[single cell isolation protocol]", "laser capture microdissection", changes,
                 "publication: neurons excised by LCM")
            setv(row, "characteristics[material type]", "cell", changes,
                 "publication: individually excised primary motor neuron")
            setv(row, "comment[lcm microscope model]", "Zeiss PALM MicroBeam", changes,
                 "publication: neuron selected and excised with Zeiss PALM MicroBeam")
        elif "SingleInterNeuron" in fn:
            setv(row, "characteristics[organism part]", "spinal cord", changes, "publication: human spinal tissue")
            setv(row, "characteristics[cell type]", "interneuron", changes, "publication/file identity")
            setv(row, "characteristics[cell line]", "not applicable", changes, "primary neuron is not HeLa")
            setv(row, "characteristics[individual]", "not available", changes, "deidentified tissue; donor not supplied")
            setv(row, "characteristics[single cell isolation protocol]", "laser capture microdissection", changes,
                 "publication: neurons excised by LCM")
            setv(row, "characteristics[material type]", "cell", changes,
                 "publication: individually excised primary interneuron")
            setv(row, "comment[lcm microscope model]", "Zeiss PALM MicroBeam", changes,
                 "publication: neuron selected and excised with Zeiss PALM MicroBeam")
        # The publication states that the selected two-CV method used HCD fragmentation at 30% NCE.
        if "comment[dissociation method]" in row:
            setv(row, "comment[dissociation method]", "HCD", changes,
                 "publication: selected two-CV single-cell method used HCD fragmentation")
        if "comment[collision energy]" in row:
            setv(row, "comment[collision energy]", "30% NCE", changes,
                 "publication: selected HCD method used 30% normalized collision energy")
        if "comment[precursor mass tolerance]" in row:
            setv(row, "comment[precursor mass tolerance]", "5 ppm", changes,
                 "publication reports precursor search mass tolerance <5 ppm; encode the reported upper tolerance boundary")
        # Source describes a nanoPOTS chip but does not provide a chip *version* identifier.
        if "comment[nanopots chip version]" in row:
            setv(row, "comment[nanopots chip version]", "not available", changes,
                 "source supplies chip type/workflow but no version identifier")


def repair_019958(rows, changes):
    for row in rows:
        setv(row, "comment[sample preparation batch]", "not available", changes,
             "batch exists conceptually but is not reported")
        setv(row, "comment[carrier channel]", "not applicable", changes, "label-free experiment")
        setv(row, "comment[reference channel]", "not applicable", changes, "label-free experiment")
        # U87_donor is a synthetic cell-line-derived identifier, not a source-backed donor.
        setv(row, "characteristics[individual]", "not applicable", changes, "cell-line study; no donor identifier reported")
        # The publication identifies the material as the U87 human glioblastoma cell line, but does
        # not establish an astrocyte cell-type annotation. Preserve the explicit cell-line field and
        # fail closed on the unsupported cell-type enrichment.
        setv(row, "characteristics[cell type]", "not applicable", changes,
             "publication supports U87 glioblastoma cell line, not astrocyte cell-type assignment")
        # Cellosaurus CVCL_0022 supports U-87MG ATCC, male sex, and glioblastoma, but explicitly
        # reports age at sampling as unspecified and notes that the ATCC-distributed line's original
        # anatomical origin is unknown. Do not synthesize brain/adult metadata from the disease name.
        setv(row, "characteristics[organism part]", "not available", changes,
             "Cellosaurus CVCL_0022: original anatomical origin is unknown")
        setv(row, "characteristics[developmental stage]", "not available", changes,
             "Cellosaurus CVCL_0022: age at sampling is unspecified")

        # Zero-cell controls contain no biological cell. Do not project cell-line identity, disease,
        # sex, anatomical metadata, or a concrete cell identifier onto the empty control merely
        # because it belongs to the same preparation series.
        role = str(row.get("characteristics[sample type]", "")).strip().lower()
        cells = str(row.get("characteristics[cells per well]", "")).strip().lower()
        if role in {"empty", "blank", "negative control"} or cells == "0":
            for h in (
                "characteristics[organism part]",
                "characteristics[cell type]",
                "characteristics[cell line]",
                "characteristics[cellosaurus accession]",
                "characteristics[cellosaurus name]",
                "characteristics[material type]",
                "characteristics[developmental stage]",
                "characteristics[sex]",
                "characteristics[disease]",
                "characteristics[individual]",
                "characteristics[single cell isolation protocol]",
            ):
                setv(row, h, "not applicable", changes, "zero-cell control contains no biological cell material")
            setv(row, "characteristics[cell identifier]", "empty", changes,
                 "single-cell template representation for a zero-cell control")
        # Keep current-spec HCD representation; do not restore deprecated PRIDE:0000590.


def repair_062702(rows, changes):
    for row in rows:
        fn = row.get("comment[data file]", "")
        hela = "HeLa" in fn
        if hela:
            setv(row, "characteristics[organism]", "Homo sapiens", changes, "HeLa proteome digest standard")
            setv(row, "characteristics[organism part]", "not applicable", changes, "HeLa digest standard has no source-backed anatomical organism part")
            setv(row, "characteristics[developmental stage]", "not applicable", changes, "HeLa standard is not an embryo")
            setv(row, "characteristics[individual]", "not applicable", changes, "cell line has no embryo/donor identity")
            setv(row, "characteristics[cell type]", "not applicable", changes, "proteome digest standard")
            setv(row, "characteristics[cell line]", "HeLa", changes, "HeLa digest standard")
            setv(row, "characteristics[material type]", "not available", changes,
                 "publication/SI: commercial HeLa proteome digest standard, not an intact cell")
            setv(row, "characteristics[sample type]", "not available", changes, "generic study-role term is not currently validator-backed; preserve role as unavailable rather than inventing a PRIDE ontology value")
            setv(row, "characteristics[single cell isolation protocol]", "not applicable", changes, "proteome digest standard")
            setv(row, "characteristics[cell identifier]", "not applicable", changes, "not a biological single cell")
            setv(row, "characteristics[cells per well]", "not applicable", changes, "proteome digest standard")
            if "DDA" in fn:
                setv(row, "comment[proteomics data acquisition method]", "NT=Data-dependent acquisition;AC=PRIDE:0000627", changes,
                     "explicit DDAtop token and Eco-DDA publication evidence")
                setv(row, "comment[instrument]", "NT=Q Exactive Plus;AC=MS:1002634", changes,
                     "publication: Real-Time Eco-AI DDA on Q Exactive Plus")
                setv(row, "comment[dissociation method]", "HCD", changes,
                     "supporting information: QE+ Top-N Eco-DDA used the HCD cell")
                setv(row, "comment[collision energy]", "28% NCE", changes,
                     "supporting information: QE+ Top-N Eco-DDA used HCD at 28% NCE")
            elif "WWA" in fn:
                setv(row, "comment[proteomics data acquisition method]", "NT=Data-dependent acquisition;AC=PRIDE:0000627", changes,
                     "publication/SI and WWA literature: WWA is wide-window DDA, not DIA")
                setv(row, "comment[instrument]", "NT=Orbitrap Fusion Lumos;AC=MS:1002732", changes,
                     "publication: nanoLC-WWA reference on Fusion Lumos")
                setv(row, "comment[dissociation method]", "HCD", changes,
                     "supporting information: Lumos WWA precursor ions fragmented in HCD cell")
                setv(row, "comment[collision energy]", "30% NCE", changes,
                     "supporting information: nanoLC-Lumos DDA/WWA used HCD at 30% NCE")
        else:
            # Sixteen D/V files are the biological blastula single-cell cohort.
            setv(row, "characteristics[organism]", "Xenopus laevis", changes, "publication: Xenopus laevis blastula cells")
            setv(row, "characteristics[organism part]", "embryo", changes, "publication: blastula-stage embryos")
            setv(row, "characteristics[developmental stage]", "blastula stage", changes, "publication: blastula stage (stage 8)")
            setv(row, "characteristics[individual]", "not available", changes, "embryo/donor identity not supplied")
            setv(row, "characteristics[cell type]", "early embryonic cell", changes, "PRIDE/publication evidence")
            setv(row, "characteristics[cell line]", "not applicable", changes, "primary Xenopus blastomere")
            setv(row, "characteristics[sample type]", "single cell", changes, "publication: n=16 single cells")
            setv(row, "characteristics[single cell isolation protocol]", "manual picking", changes,
                 "cells isolated under stereomicroscope following embryo dissociation")
            setv(row, "characteristics[cells per well]", "1", changes, "single-cell biological cohort")
            setv(row, "comment[proteomics data acquisition method]", "NT=Data-dependent acquisition;AC=PRIDE:0000627", changes,
                 "publication: Real-Time Eco-DDA")
            setv(row, "comment[instrument]", "NT=Q Exactive Plus;AC=MS:1002634", changes,
                 "publication: Xenopus Real-Time Eco-AI on Q Exactive Plus")
            setv(row, "comment[dissociation method]", "HCD", changes,
                 "supporting information: Xenopus Top-10 Eco-DDA used the QE+ HCD cell")
            setv(row, "comment[collision energy]", "28% NCE", changes,
                 "supporting information: QE+ Top-N Eco-DDA used HCD at 28% NCE")


REPAIRS: dict[str, Callable] = {
    "PXD019515": repair_019515,
    "PXD019958": repair_019958,
    "PXD062702": repair_062702,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release-dir", required=True, help="Frozen PRIDE_SCP_SUBMISSION_RELEASE20_2026-09-11 directory")
    ap.add_argument("--repo", required=True, help="PRIDE-SCP repo root (for snapshot project metadata)")
    ap.add_argument("--output", required=True)
    ap.add_argument("--accession", action="append", choices=sorted(REPAIRS))
    ns = ap.parse_args()

    release = Path(ns.release_dir)
    repo = Path(ns.repo)
    out = Path(ns.output)
    selected = ns.accession or sorted(REPAIRS)
    summary = []

    for acc in selected:
        src = release / "datasets" / acc / f"{acc}.sdrf.tsv"
        if not src.is_file():
            raise SystemExit(f"{acc}: missing frozen release input {src}")
        old_sha = sha256(src)
        if old_sha != EXPECTED[acc]:
            raise SystemExit(f"{acc}: input SHA mismatch: expected {EXPECTED[acc]}, got {old_sha}")
        headers, rows = load(src)
        changes = []
        REPAIRS[acc](rows, changes)
        dst = out / "candidates" / acc / f"{acc}.sdrf.tsv"
        write(dst, headers, rows)
        new_sha = sha256(dst)
        project = repo / "data" / "snapshot" / "projects" / f"{acc}.json"
        guard = analyze_guard(dst, project if project.is_file() else None)
        if guard.blockers:
            raise SystemExit(f"{acc}: repaired candidate still fails scientific guard: {guard.blockers}")
        manifest = {
            "accession": acc,
            "source_release_sha256": old_sha,
            "repaired_sha256": new_sha,
            "changes": changes,
            "evidence": EVIDENCE[acc],
            "scientific_guard": {"blockers": guard.blockers, "warnings": guard.warnings, "details": guard.details},
            "compatibility_architecture_changed": False,
            "requires_new_hash_bound_independent_review": True,
        }
        mp = out / "manifests" / f"{acc}.repair.json"
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        summary.append((acc, old_sha, new_sha, len(changes)))

    sp = out / "repair_summary.tsv"
    sp.parent.mkdir(parents=True, exist_ok=True)
    with sp.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["accession", "old_sha256", "repaired_sha256", "changes"])
        w.writerows(summary)
    for row in summary:
        print("\t".join(map(str, row)))
    print(f"summary={sp}")

if __name__ == "__main__":
    main()
