# MassIVE M2-C2 canonical evidence fusion

M2-C2 is the **final bounded MassIVE curation-architecture pass**.

It exists to fix one remaining representation asymmetry observed in M2-C1:
26 GT rows recovered through `pxd_alias` were curated through 25 sparse PXD
representatives even though trusted source-derived MSV↔PXD edges were already
known.  The PXD packets contained only 2–4 evidence items in many cases, while
repository-native MassIVE records had richer structured evidence.

M2-C2 does **not** tune discovery or curation.  It does not change:

- discovery vocabulary or scores;
- `v19-shadow-2.7.1` prompts, thresholds, or deterministic decision rules;
- Stage05 (still disabled);
- GT196/GT179;
- MSV↔PXD identity edges.

## Fusion rule

For each PXD representative selected in the already-accepted M2-C1 GT65
benchmark, M2-C2 looks up linked MSV accessions only from the normalized
`data/snapshot/native/massive/massive_accessions.tsv` crosswalk.  Each edge must
also be present in the normalized native project `pxdAliases`/`sourceIdentity`.
Structured native evidence is then collected from the existing cached MassIVE
record, MassIVE PROXI/detail responses, and ProteomeCentral responses, preserving
the native accession in every evidence source path.

The resulting sidecar supplements the PXD candidate; it does not replace or
rewrite the PXD repository evidence.

GT defines the 65-row evaluation cohort only.  It never creates an identity edge,
chooses a native alias, supplies an expected decision, or enters the candidate
JSONL consumed by v19.

## Experimental isolation

Only the **25 unique PXD representatives** affected by fusion are rerun through
Qwen/v19.  The 38 unchanged native representatives reuse their accepted M2-C1
curation summaries.  This isolates the effect of canonical evidence fusion and
avoids rerun noise on unrelated candidates.

## Acceptance and stopping policy

M2-C2 passes when:

- accepted discovery remains 65/65;
- all 25 PXD representatives have trusted source-derived native aliases;
- GT is absent from runtime identity/fusion/candidate JSONL;
- affected curation has zero runtime errors;
- frozen curation version remains `v19-shadow-2.7.1`;
- all 65 rows evaluate successfully;
- automated excludes / hard false negatives remain zero.

There is **no target include rate**.  Review is an accepted conservative outcome.
If these safety gates hold, MassIVE discovery/identity/curation is frozen after
M2-C2 even if reviews remain.  Do not continue prompt/model/rule tuning merely to
increase automated includes.
