# MassIVE native curation bridge (M2-C0)

M2-C0 generalizes the frozen `v19-shadow-2.7.1` evidence interface to native `MSV...` candidates without changing its prompt, biological definition, deterministic decision policy, or GT usage.

The accepted identity baseline is the M2-B.1 run: 537 emitted candidates, 118 newly retained native candidates, 49 evidence-safe MSV/PXD collapses, zero lost baseline candidates, and MassIVE GT65 recall restored to 65/65.

`build_massive_native_curation_bridge.py` reads only accepted discovery outputs plus repository-derived snapshot/cache data. It writes a provenance-preserving evidence sidecar for each of the 118 new native candidates and an augmented candidate JSONL. Evidence may come from the normalized MassIVE native record, cached MassIVE PROXI response, cached detailed `QueryDatasets` response, cached ProteomeCentral records, and the source-derived identity object. It records publication identifiers, file/metadata hints, sample/MS-method evidence, and nonqualifying context. It does not classify candidates and does not read GT.

The frozen v19 runner now accepts both `PXDdddddd` and `MSVddddddddd` targets. For an MSV target, publication annotations may be routed only through trusted PXD aliases already present in the source-derived identity object. Free-text PXD tokens are not trusted routes. Native bridge evidence is added to the bounded evidence packet and deterministic evidence pool with explicit source labels.

`evaluate_massive_native_m2c_source_audit.py` is evaluation-only. It joins the already-built source audit to the frozen-GT status stored in the independent delta audit after evidence normalization is complete. GT never changes evidence extraction or review priority.

The first run is intentionally `--packets-only`. Review evidence coverage and the 79 non-GT candidate queue before spending model inference time or changing source acquisition. The next stage is to decide whether repository evidence is sufficient for a representative native-MSV curation smoke, or whether publication/file-detail retrieval needs one more generic enrichment pass.
