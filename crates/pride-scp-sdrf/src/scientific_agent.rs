use super::*;

pub const SCIENTIFIC_AGENT_HARNESS_VERSION: &str = "pride-scp-scientific-agent-v0.3";
#[derive(Debug, Clone)]
pub struct SdrfScientificAgentOptions {
    pub snapshot_dir: PathBuf,
    pub annotations_dir: PathBuf,
    pub publication_manifest: Option<PathBuf>,
    pub manuscript_text_paths: Vec<PathBuf>,
    pub output_dir: PathBuf,
    pub accessions: Vec<String>,
    pub accessions_file: Option<PathBuf>,
    pub resolved_sdrf_dir: Option<PathBuf>,
    pub explicit_row_mapping_manifest: Option<PathBuf>,
    pub model: String,
    pub ollama_url: String,
    pub timeout_seconds: u64,
    pub max_evidence_items: usize,
    pub max_evidence_chars: usize,
    pub max_files_in_prompt: usize,
    pub max_agent_turns: usize,
    pub max_tool_actions: usize,
    pub max_validator_cycles: usize,
    pub force: bool,
    pub progress: bool,
}

impl SdrfScientificAgentOptions {
    fn annotate_options(&self) -> SdrfAnnotateOptions {
        SdrfAnnotateOptions {
            snapshot_dir: self.snapshot_dir.clone(),
            annotations_dir: self.annotations_dir.clone(),
            publication_manifest: self.publication_manifest.clone(),
            manuscript_text_paths: self.manuscript_text_paths.clone(),
            output_dir: self.output_dir.clone(),
            accessions: self.accessions.clone(),
            accessions_file: self.accessions_file.clone(),
            resolved_sdrf_dir: self.resolved_sdrf_dir.clone(),
            explicit_row_mapping_manifest: self.explicit_row_mapping_manifest.clone(),
            model: self.model.clone(),
            ollama_url: self.ollama_url.clone(),
            timeout_seconds: self.timeout_seconds,
            max_evidence_items: self.max_evidence_items,
            max_evidence_chars: self.max_evidence_chars,
            max_files_in_prompt: self.max_files_in_prompt,
            force: self.force,
            progress: self.progress,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SdrfScientificAgentSummary {
    pub harness_version: String,
    pub generator_version: String,
    pub accessions_requested: usize,
    pub successful: usize,
    pub errors: usize,
    pub locally_valid_drafts: usize,
    pub incomplete_drafts: usize,
    pub total_validation_errors: usize,
    pub total_agent_turns: usize,
    pub total_tool_actions: usize,
    pub total_validator_cycles: usize,
    pub results_tsv: String,
    pub workspace_root: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ScientificAgentResultRow {
    accession: String,
    status: String,
    terminal_status: String,
    turns: usize,
    tool_actions: usize,
    validator_cycles: usize,
    branches: usize,
    open_questions: usize,
    relation_mode: String,
    locally_valid: bool,
    validation_errors: usize,
    draft_path: String,
    review_path: String,
    workspace_path: String,
    error: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AgentRelation {
    mode: String,
    scope: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AgentBranch {
    id: String,
    label: String,
    status: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    #[serde(default)]
    linked_raw_files: Vec<String>,
    linkage_status: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificClaim {
    concept_type: String,
    value: String,
    scope: String,
    branch_id: String,
    status: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
enum ClaimAdjudication {
    Canonical {
        value: String,
        evidence_refs: Vec<String>,
    },
    TemplateGap {
        observed_value: String,
        evidence_refs: Vec<String>,
        reason: String,
    },
    SupportedConcept {
        value: String,
        evidence_refs: Vec<String>,
    },
    Unresolved {
        reason: String,
    },
    Conflict {
        reason: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ClaimAdjudicationRecord {
    concept_type: String,
    scope: String,
    branch_id: String,
    model_status: String,
    proposed_value: String,
    evidence_refs: Vec<String>,
    adjudication: ClaimAdjudication,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AgentEvidenceAction {
    action: String,
    reason: String,
    #[serde(default)]
    target_concepts: Vec<String>,
    #[serde(default)]
    queries: Vec<EvidenceQuery>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificWorkspaceState {
    harness_version: String,
    accession: String,
    turn: usize,
    relation: AgentRelation,
    #[serde(default)]
    branches: Vec<AgentBranch>,
    #[serde(default)]
    claims: Vec<ScientificClaim>,
    #[serde(default)]
    open_questions: Vec<String>,
    #[serde(default)]
    conflicts: Vec<String>,
    #[serde(default)]
    next_evidence_actions: Vec<AgentEvidenceAction>,
    next_step: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AgentValidationCycle {
    cycle: usize,
    validation_errors: usize,
    validation_warnings: usize,
    #[serde(default)]
    error_counts: BTreeMap<String, usize>,
    #[serde(default)]
    representative_messages: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificAgentTrace {
    harness_version: String,
    accession: String,
    #[serde(default)]
    states: Vec<ScientificWorkspaceState>,
    #[serde(default)]
    evidence_action_results: Vec<EvidenceActionResult>,
    #[serde(default)]
    validation_history: Vec<AgentValidationCycle>,
    #[serde(default)]
    harness_feedback: Vec<String>,
    terminal_status: String,
    turns_completed: usize,
    tool_actions_completed: usize,
    validator_cycles_completed: usize,
}

#[derive(Debug, Clone)]
struct CompiledWorkspace {
    proposal: SdrfProposal,
    headers: Vec<String>,
    rows: Vec<Vec<String>>,
    generation_mode: String,
    issues: Vec<ValidationIssue>,
    deterministic_repairs: Vec<String>,
    adjudications: Vec<ClaimAdjudicationRecord>,
    fingerprint: String,
}

fn scientific_concept_types() -> Vec<&'static str> {
    vec![
        "organism",
        "organism_part",
        "disease",
        "cell_type",
        "cell_line",
        "sample_type",
        "isolation_method",
        "individual",
        "sample_preparation",
        "acquisition_mode",
        "labeling",
        "instrument",
        "cleavage_agent",
        "control_role",
        "biological_condition",
    ]
}

fn concept_to_sdrf_field(concept: &str) -> Option<&'static str> {
    match concept {
        "organism" => Some("organism"),
        "organism_part" => Some("organism_part"),
        "disease" => Some("disease"),
        "cell_type" => Some("cell_type"),
        "sample_type" => Some("sample_type"),
        "isolation_method" => Some("single_cell_isolation_method"),
        "individual" => Some("individual"),
        "sample_preparation" => Some("sample_preparation_batch"),
        "acquisition_mode" => Some("proteomics_data_acquisition_method"),
        "labeling" => Some("label"),
        "instrument" => Some("instrument"),
        "cleavage_agent" => Some("cleavage_agent_details"),
        // cell_line, control_role, and biological_condition are first-class
        // scientific concepts in the workspace, but the current SdrfProposal
        // has no safe one-to-one project field for them. They therefore remain
        // scientific state until a deterministic compiler mapping exists.
        "cell_line" | "control_role" | "biological_condition" => None,
        _ => None,
    }
}

fn scientific_agent_schema() -> Value {
    let concepts = scientific_concept_types();
    let query = json!({
        "type":"object",
        "properties":{
            "match":{"type":"string","enum":["raw_exact","phrase","terms_all","terms_any","identifier","doi"]},
            "value":{"type":"string","maxLength":300},
            "terms":{"type":"array","items":{"type":"string","maxLength":120},"maxItems":12},
            "document_hint":{"type":"string","maxLength":240}
        },
        "required":["match","value","terms","document_hint"],
        "additionalProperties":false
    });
    json!({
        "type":"object",
        "properties":{
            "harness_version":{"type":"string","maxLength":80},
            "accession":{"type":"string","maxLength":32},
            "turn":{"type":"integer","minimum":1,"maximum":100},
            "relation":{"type":"object","properties":{
                "mode":{"type":"string","enum":["one_cell_per_data_file","multiplexed_cells_per_data_file","mixed","unresolved"]},
                "scope":{"type":"string","enum":["project","branch","unresolved"]},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":12},
                "confidence":{"type":"string","enum":["high","medium","low"]},
                "reason":{"type":"string","maxLength":500}
            },"required":["mode","scope","evidence_refs","confidence","reason"],"additionalProperties":false},
            "branches":{"type":"array","maxItems":24,"items":{"type":"object","properties":{
                "id":{"type":"string","maxLength":50},
                "label":{"type":"string","maxLength":200},
                "status":{"type":"string","enum":["supported","hypothesis","rejected"]},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":16},
                "linked_raw_files":{"type":"array","items":{"type":"string","maxLength":300},"maxItems":128},
                "linkage_status":{"type":"string","enum":["supported","partial","unresolved"]},
                "notes":{"type":"string","maxLength":600}
            },"required":["id","label","status","evidence_refs","linked_raw_files","linkage_status","notes"],"additionalProperties":false}},
            "claims":{"type":"array","maxItems":64,"items":{"type":"object","properties":{
                "concept_type":{"type":"string","enum":concepts.clone()},
                "value":{"type":"string","maxLength":500},
                "scope":{"type":"string","enum":["project","branch","row","unresolved"]},
                "branch_id":{"type":"string","maxLength":50},
                "status":{"type":"string","enum":["supported","hypothesis","unresolved","rejected"]},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":16},
                "confidence":{"type":"string","enum":["high","medium","low"]},
                "reason":{"type":"string","maxLength":600}
            },"required":["concept_type","value","scope","branch_id","status","evidence_refs","confidence","reason"],"additionalProperties":false}},
            "open_questions":{"type":"array","items":{"type":"string","maxLength":400},"maxItems":32},
            "conflicts":{"type":"array","items":{"type":"string","maxLength":400},"maxItems":32},
            "next_evidence_actions":{"type":"array","maxItems":8,"items":{"type":"object","properties":{
                "action":{"type":"string","enum":["SEARCH_PUBLICATION","SEARCH_SUPPLEMENT","SEARCH_STRUCTURED_DESIGN","SEARCH_REPOSITORY_METADATA","SEARCH_EXACT_RAW_NAME","EXPAND_EVIDENCE_CONTEXT","LOOKUP_KG_TERM","COMPARE_CONFLICTING_EVIDENCE","ABSTAIN"]},
                "reason":{"type":"string","maxLength":400},
                "target_concepts":{"type":"array","items":{"type":"string","enum":concepts.clone()},"maxItems":8},
                "queries":{"type":"array","items":query,"maxItems":8}
            },"required":["action","reason","target_concepts","queries"],"additionalProperties":false}},
            "next_step":{"type":"string","enum":["search","compile","finish","abstain"]},
            "notes":{"type":"string","maxLength":1400}
        },
        "required":["harness_version","accession","turn","relation","branches","claims","open_questions","conflicts","next_evidence_actions","next_step","notes"],
        "additionalProperties":false
    })
}

fn workspace_evidence_block(evidence: &DatasetEvidence, max_items: usize) -> String {
    evidence
        .evidence
        .iter()
        .take(max_items)
        .map(|item| {
            let text = item.text.replace('\n', " ");
            let text = if text.chars().count() > 1000 {
                text.chars().take(1000).collect::<String>() + "..."
            } else {
                text
            };
            format!(
                "{} [{}:{}] {}",
                item.id, item.source_kind, item.source_label, text
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn scientific_agent_prompt(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
    previous: &ScientificWorkspaceState,
    actions: &[EvidenceActionResult],
    validations: &[AgentValidationCycle],
    harness_feedback: &[String],
    turn: usize,
) -> String {
    let files = evidence
        .raw_files
        .iter()
        .take(opts.max_files_in_prompt)
        .map(|f| format!("- {}", f.file_name))
        .collect::<Vec<_>>()
        .join("\n");
    let previous_json = serde_json::to_string_pretty(previous).unwrap_or_else(|_| "{}".into());
    let action_json = serde_json::to_string_pretty(actions).unwrap_or_else(|_| "[]".into());
    let validation_json = serde_json::to_string_pretty(validations).unwrap_or_else(|_| "[]".into());
    let feedback_json =
        serde_json::to_string_pretty(harness_feedback).unwrap_or_else(|_| "[]".into());
    format!(
        "You are the scientific annotation agent for PRIDE single-cell proteomics dataset {acc}.\n\n\
Behave like a careful coding/research agent operating on a persistent scientific workspace. Inspect evidence, maintain branches and typed scientific claims, request tools when evidence is missing, compile only when the current study model has materially changed, inspect validator feedback, revise, and stop when the SDRF is valid or the evidence budget is exhausted.\n\n\
IMPORTANT COMPILER CONTRACT:\n\
- You are NOT rebuilding an SDRF from scratch. Rust starts from a proven deterministic baseline containing relation/cardinality, repository/file structure, row identifiers, fraction/technical replicate defaults, and deterministic metadata scaffolds.\n\
- Your claims are a semantic overlay. Omitting a field does NOT erase a valid deterministic baseline value.\n\
- If a field genuinely differs across biological/acquisition branches, represent branch-scoped claims. Rust will then mask an unsafe project-wide baseline value, but will serialize branch values only when file-to-branch linkage is source-grounded.\n\
- Use typed scientific concepts, not arbitrary SDRF columns. Rust owns the final mapping to SDRF fields and controlled vocabulary.\n\n\
SCIENTIFIC CONCEPTS:\n\
organism, organism_part, disease, cell_type, cell_line, sample_type, isolation_method, individual, sample_preparation, acquisition_mode, labeling, instrument, cleavage_agent, control_role, biological_condition.\n\n\
HARD SCIENTIFIC CONTRACT:\n\
1. Never use GT labels or hidden benchmark truth.\n\
2. Filename words are search hints and contradiction detectors, NOT biological identity. A branch may list linked_raw_files only when cited E#### source evidence explicitly names or otherwise source-links those exact RAW basenames.\n\
3. Keep project, branch, row, and unresolved scopes distinct. A project claim means the same scientific value holds across every relevant branch.\n\
4. Biological heterogeneity is separate from acquisition cardinality.\n\
5. Do not collapse multiple organisms or acquisition regimes into one project claim. Represent separate branches and leave file linkage unresolved when evidence is insufficient.\n\
6. For isolation and acquisition, describe the scientific intent faithfully and cite the strongest direct method evidence. Rust—not you—decides whether the refs canonicalize to one supported value, prove a template gap, conflict, or remain unresolved. Distinguish the act that isolates/selects a single cell from downstream lysis, digestion, droplet handling, or injection of an already isolated lysate; those downstream steps are not isolation evidence by themselves.\n\
7. Model status is advisory. For isolation_method and acquisition_mode you MAY return status='hypothesis' when the scientific interpretation is source-grounded but the exact controlled term is uncertain; Rust independently adjudicates the cited evidence and may canonicalize it. Other hypothesis concepts remain non-publishable.\n\
8. Do not use sample_preparation as a dumping ground for every method detail. Emit one concise claim only when the concept is genuinely needed for SDRF annotation; procedural detail belongs in notes unless it changes a typed scientific concept.\n\
9. Prefer explicit open_questions over guessed values.\n\
10. Use next_step='search' only with executable actions. Use 'compile' only after a material workspace change. Use 'finish' when no further safe retrieval is needed. Use 'abstain' when evidence cannot safely resolve remaining study design.\n\
11. Validator feedback is feedback about the compiled draft, not permission to invent metadata. Structural fields such as cell identifier, fraction identifier, and technical replicate are deterministic compiler responsibilities and should NOT become scientific claims.\n\
12. If HARNESS FEEDBACK says the previous compile was unchanged, do not request compile again without changing claims/branches; search, finish, or abstain instead.\n\n\
ACCEPTED DETERMINISTIC RELATION HINT (cardinality only):\n\
mode={relation}; confidence={relation_confidence}; refs={relation_refs:?}; repository_file_mode={repo_mode}; note={design_note}\n\n\
RAW FILE COUNT: {nfiles}\nRAW FILE SAMPLE (context/search hints only):\n{files}\n\n\
EVIDENCE INVENTORY:\n{evidence_block}\n\n\
PREVIOUS WORKSPACE STATE:\n{previous_json}\n\n\
TOOL/ACTION HISTORY:\n{action_json}\n\n\
VALIDATION HISTORY:\n{validation_json}\n\n\
HARNESS FEEDBACK:\n{feedback_json}\n\n\
This is agent turn {turn}. Return the COMPLETE updated workspace state, not a patch. Keep claims deduplicated, concise, and decision-oriented.",
        acc = evidence.accession,
        relation = evidence.study_design.relation_mode_hint,
        relation_confidence = evidence.study_design.relation_confidence,
        relation_refs = evidence.study_design.relation_evidence_refs,
        repo_mode = evidence.study_design.repository_file_mode,
        design_note = evidence.study_design.notes,
        nfiles = evidence.raw_files.len(),
        files = files,
        evidence_block = workspace_evidence_block(evidence, 48),
        previous_json = previous_json,
        action_json = action_json,
        validation_json = validation_json,
        feedback_json = feedback_json,
        turn = turn,
    )
}

async fn call_scientific_agent(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
    previous: &ScientificWorkspaceState,
    actions: &[EvidenceActionResult],
    validations: &[AgentValidationCycle],
    harness_feedback: &[String],
    turn: usize,
) -> Result<ScientificWorkspaceState> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": scientific_agent_prompt(
            opts,
            evidence,
            previous,
            actions,
            validations,
            harness_feedback,
            turn,
        ),
        "stream": false,
        "think": false,
        "format": scientific_agent_schema(),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| {
            format!(
                "Ollama scientific-agent request for {} turn {}",
                evidence.accession, turn
            )
        })?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .context("decode Ollama scientific-agent response")?;
    if !status.is_success() {
        bail!("Ollama scientific-agent HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty scientific-agent response");
    }
    let mut state: ScientificWorkspaceState =
        serde_json::from_str(raw).context("parse structured scientific-agent workspace")?;
    normalize_workspace_state(evidence, &mut state, turn);
    Ok(state)
}

fn valid_evidence_refs(evidence: &DatasetEvidence, refs: &[String]) -> Vec<String> {
    let valid = evidence
        .evidence
        .iter()
        .map(|item| item.id.as_str())
        .collect::<BTreeSet<_>>();
    let mut out = refs
        .iter()
        .filter(|id| valid.contains(id.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    out.sort();
    out.dedup();
    out
}

fn branch_file_is_source_grounded(
    evidence: &DatasetEvidence,
    evidence_refs: &[String],
    raw_file: &str,
) -> bool {
    evidence_refs.iter().any(|id| {
        evidence
            .evidence
            .iter()
            .find(|item| item.id == id.as_str())
            .map(|item| {
                let label = item.source_label.to_ascii_lowercase();
                let kind = item.source_kind.to_ascii_lowercase();
                let repository_manifest_only = label
                    == evidence.files_json_path.to_ascii_lowercase()
                    || label == evidence.project_json_path.to_ascii_lowercase()
                    || (kind.contains("repository") && !kind.contains("design"));
                if repository_manifest_only {
                    return false;
                }
                let hay = format!("{}\n{}", item.source_label, item.text).to_ascii_lowercase();
                hay.contains(&raw_file.to_ascii_lowercase())
            })
            .unwrap_or(false)
    })
}

fn claim_refs_are_relevant(
    evidence: &DatasetEvidence,
    concept_type: &str,
    refs: &[String],
) -> bool {
    if refs.is_empty() {
        return false;
    }
    match concept_to_sdrf_field(concept_type) {
        Some(field) => refs.iter().all(|id| {
            evidence
                .evidence
                .iter()
                .find(|item| item.id == id.as_str())
                .map(|item| evidence_relevant_to_field(field, item))
                .unwrap_or(false)
        }),
        None => refs
            .iter()
            .all(|id| evidence.evidence.iter().any(|item| item.id == id.as_str())),
    }
}

fn confidence_rank(value: &str) -> usize {
    match value {
        "high" => 3,
        "medium" => 2,
        "low" => 1,
        _ => 0,
    }
}

fn status_rank(value: &str) -> usize {
    match value {
        "supported" => 4,
        "hypothesis" => 3,
        "unresolved" => 2,
        "rejected" => 1,
        _ => 0,
    }
}

fn dedup_strings(values: &mut Vec<String>, max_items: usize) {
    let mut seen = BTreeSet::new();
    values.retain(|value| {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            return false;
        }
        seen.insert(trimmed.to_ascii_lowercase())
    });
    values.truncate(max_items);
}

fn reduce_scientific_claims(
    evidence: &DatasetEvidence,
    branch_ids: &BTreeSet<&str>,
    claims: &mut Vec<ScientificClaim>,
    conflicts: &mut Vec<String>,
) {
    let allowed = scientific_concept_types()
        .into_iter()
        .collect::<BTreeSet<_>>();
    for claim in claims.iter_mut() {
        claim.concept_type = claim.concept_type.trim().to_ascii_lowercase();
        claim.value = claim.value.trim().to_string();
        claim.branch_id = claim.branch_id.trim().to_string();
        claim.evidence_refs = valid_evidence_refs(evidence, &claim.evidence_refs);
        if !allowed.contains(claim.concept_type.as_str()) {
            claim.status = "rejected".into();
            claim.value.clear();
            continue;
        }
        if claim.scope == "branch" && !branch_ids.contains(claim.branch_id.as_str()) {
            claim.scope = "unresolved".into();
            claim.status = "unresolved".into();
            claim.value.clear();
        }
        if matches!(claim.scope.as_str(), "project" | "branch" | "row")
            && claim.status == "supported"
            && !claim_refs_are_relevant(evidence, &claim.concept_type, &claim.evidence_refs)
        {
            claim.status = "hypothesis".into();
        }
        if claim.status == "unresolved" || claim.scope == "unresolved" {
            claim.value.clear();
            claim.scope = "unresolved".into();
        }
    }
    claims.retain(|claim| claim.status != "rejected" && !claim.concept_type.is_empty());

    let mut reduced: BTreeMap<(String, String, String, String), ScientificClaim> = BTreeMap::new();
    for claim in claims.drain(..) {
        let key = (
            claim.concept_type.clone(),
            claim.scope.clone(),
            claim.branch_id.clone(),
            claim.value.to_ascii_lowercase(),
        );
        match reduced.get_mut(&key) {
            None => {
                reduced.insert(key, claim);
            }
            Some(existing) => {
                existing.evidence_refs.extend(claim.evidence_refs);
                existing.evidence_refs.sort();
                existing.evidence_refs.dedup();
                if status_rank(&claim.status) > status_rank(&existing.status) {
                    existing.status = claim.status;
                }
                if confidence_rank(&claim.confidence) > confidence_rank(&existing.confidence) {
                    existing.confidence = claim.confidence;
                }
                if claim.reason.len() > existing.reason.len() {
                    existing.reason = claim.reason;
                }
            }
        }
    }
    *claims = reduced.into_values().collect();

    let mut supported_values: BTreeMap<(String, String, String), BTreeSet<String>> =
        BTreeMap::new();
    for claim in claims.iter() {
        if claim.status != "supported" || claim.value.trim().is_empty() {
            continue;
        }
        supported_values
            .entry((
                claim.concept_type.clone(),
                claim.scope.clone(),
                claim.branch_id.clone(),
            ))
            .or_default()
            .insert(claim.value.trim().to_ascii_lowercase());
    }
    let conflicting_keys = supported_values
        .iter()
        .filter(|(_, values)| values.len() > 1)
        .map(|(key, values)| (key.clone(), values.clone()))
        .collect::<Vec<_>>();
    for ((concept, scope, branch), values) in conflicting_keys {
        conflicts.push(format!(
            "conflicting supported values for concept={} scope={} branch={}: {:?}",
            concept, scope, branch, values
        ));
        for claim in claims.iter_mut().filter(|claim| {
            claim.concept_type == concept && claim.scope == scope && claim.branch_id == branch
        }) {
            if claim.status == "supported" {
                claim.status = "hypothesis".into();
            }
        }
    }
    claims.sort_by(|a, b| {
        a.concept_type
            .cmp(&b.concept_type)
            .then_with(|| a.scope.cmp(&b.scope))
            .then_with(|| a.branch_id.cmp(&b.branch_id))
            .then_with(|| a.value.cmp(&b.value))
    });
    claims.truncate(48);
}

fn normalize_scientific_agent_action(evidence: &DatasetEvidence, action: &mut AgentEvidenceAction) {
    let allowed = scientific_concept_types()
        .into_iter()
        .collect::<BTreeSet<_>>();
    action.target_concepts = action
        .target_concepts
        .iter()
        .map(|value| value.trim().to_ascii_lowercase())
        .filter(|value| allowed.contains(value.as_str()))
        .collect();
    action.target_concepts.sort();
    action.target_concepts.dedup();

    for query in &mut action.queries {
        normalize_evidence_query(query);
        if query.match_kind == "raw_exact" {
            let exact = evidence
                .raw_files
                .iter()
                .any(|raw| raw.file_name.eq_ignore_ascii_case(query.value.trim()));
            if exact {
                action.action = "SEARCH_EXACT_RAW_NAME".into();
            } else {
                // A phrase such as "BS01-BS09 HeLa files" is a useful search
                // hint but is not an exact repository basename. Reformulate it
                // into a normal typed search rather than wasting a tool turn on
                // an impossible SEARCH_EXACT_RAW_NAME request.
                query.match_kind = if query.terms.is_empty() {
                    "phrase".into()
                } else {
                    "terms_all".into()
                };
                if query.value.split_whitespace().count() > 16 && !query.terms.is_empty() {
                    query.value.clear();
                }
            }
        }
        if action.action == "SEARCH_EXACT_RAW_NAME" && query.match_kind != "raw_exact" {
            action.action = "SEARCH_REPOSITORY_METADATA".into();
        }
    }
}

fn agent_action_to_request(action: &AgentEvidenceAction) -> EvidenceActionRequest {
    EvidenceActionRequest {
        action: action.action.clone(),
        reason: action.reason.clone(),
        target_fields: action
            .target_concepts
            .iter()
            .map(|concept| {
                concept_to_sdrf_field(concept)
                    .unwrap_or(concept)
                    .to_string()
            })
            .collect(),
        queries: action.queries.clone(),
    }
}

fn normalize_workspace_state(
    evidence: &DatasetEvidence,
    state: &mut ScientificWorkspaceState,
    turn: usize,
) {
    state.harness_version = SCIENTIFIC_AGENT_HARNESS_VERSION.into();
    state.accession = evidence.accession.clone();
    state.turn = turn;

    state.relation.evidence_refs = valid_evidence_refs(evidence, &state.relation.evidence_refs);
    if !matches!(
        state.relation.mode.as_str(),
        "one_cell_per_data_file" | "multiplexed_cells_per_data_file" | "mixed" | "unresolved"
    ) {
        state.relation.mode = "unresolved".into();
        state.relation.scope = "unresolved".into();
    }
    if study_design_has_assertive_relation_hint(&evidence.study_design) {
        state.relation.mode = evidence.study_design.relation_mode_hint.clone();
        state.relation.scope = "project".into();
        state.relation.evidence_refs = evidence.study_design.relation_evidence_refs.clone();
        state.relation.confidence = evidence.study_design.relation_confidence.clone();
        state.relation.reason = format!(
            "accepted deterministic acquisition-cardinality scaffold; {}",
            evidence.study_design.notes
        );
    }

    let raw_names = evidence
        .raw_files
        .iter()
        .map(|f| f.file_name.to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    let mut branch_map: BTreeMap<String, AgentBranch> = BTreeMap::new();
    for mut branch in state.branches.drain(..) {
        branch.id = branch.id.trim().to_string();
        if branch.id.is_empty() {
            continue;
        }
        branch.evidence_refs = valid_evidence_refs(evidence, &branch.evidence_refs);
        if branch.status == "supported" && branch.evidence_refs.is_empty() {
            branch.status = "hypothesis".into();
        }
        let mut linked = Vec::new();
        for raw in &branch.linked_raw_files {
            let Some(canonical) = evidence
                .raw_files
                .iter()
                .find(|f| f.file_name.eq_ignore_ascii_case(raw.trim()))
                .map(|f| f.file_name.clone())
            else {
                continue;
            };
            if raw_names.contains(&canonical.to_ascii_lowercase())
                && branch_file_is_source_grounded(evidence, &branch.evidence_refs, &canonical)
            {
                linked.push(canonical);
            }
        }
        linked.sort();
        linked.dedup();
        branch.linked_raw_files = linked;
        branch.linkage_status = if branch.linked_raw_files.is_empty() {
            "unresolved".into()
        } else if branch.status == "supported" {
            branch.linkage_status.clone()
        } else {
            "unresolved".into()
        };
        match branch_map.get_mut(&branch.id) {
            None => {
                branch_map.insert(branch.id.clone(), branch);
            }
            Some(existing) => {
                existing.evidence_refs.extend(branch.evidence_refs);
                existing.evidence_refs.sort();
                existing.evidence_refs.dedup();
                existing.linked_raw_files.extend(branch.linked_raw_files);
                existing.linked_raw_files.sort();
                existing.linked_raw_files.dedup();
                if status_rank(&branch.status) > status_rank(&existing.status) {
                    existing.status = branch.status;
                }
                if branch.notes.len() > existing.notes.len() {
                    existing.notes = branch.notes;
                }
            }
        }
    }
    state.branches = branch_map.into_values().take(24).collect();

    let branch_ids = state
        .branches
        .iter()
        .map(|b| b.id.as_str())
        .collect::<BTreeSet<_>>();
    reduce_scientific_claims(
        evidence,
        &branch_ids,
        &mut state.claims,
        &mut state.conflicts,
    );

    dedup_strings(&mut state.open_questions, 32);
    dedup_strings(&mut state.conflicts, 32);
    for action in &mut state.next_evidence_actions {
        normalize_scientific_agent_action(evidence, action);
    }
    state.next_evidence_actions.truncate(8);
    if !matches!(
        state.next_step.as_str(),
        "search" | "compile" | "finish" | "abstain"
    ) {
        state.next_step = if state.next_evidence_actions.is_empty() {
            "compile".into()
        } else {
            "search".into()
        };
    }
    if state.next_step == "search" && state.next_evidence_actions.is_empty() {
        state.next_step = "compile".into();
    }
}

fn evidence_subset(evidence: &DatasetEvidence, refs: &[String]) -> Vec<EvidenceItem> {
    refs.iter()
        .filter_map(|id| {
            evidence
                .evidence
                .iter()
                .find(|item| item.id == id.as_str())
                .cloned()
        })
        .collect()
}

fn field_relevant_claim_refs(
    evidence: &DatasetEvidence,
    concept_type: &str,
    refs: &[String],
) -> Vec<String> {
    let Some(field) = concept_to_sdrf_field(concept_type) else {
        return valid_evidence_refs(evidence, refs);
    };
    refs.iter()
        .filter_map(|id| {
            evidence
                .evidence
                .iter()
                .find(|item| item.id == id.as_str())
                .filter(|item| evidence_relevant_to_field(field, item))
                .map(|item| item.id.clone())
        })
        .collect()
}

fn adjudicate_claim(evidence: &DatasetEvidence, claim: &ScientificClaim) -> ClaimAdjudication {
    if claim.scope == "unresolved" || claim.status == "unresolved" || claim.value.trim().is_empty()
    {
        return ClaimAdjudication::Unresolved {
            reason: "claim is explicitly unresolved or has no proposed scientific value".into(),
        };
    }

    let relevant_refs =
        field_relevant_claim_refs(evidence, &claim.concept_type, &claim.evidence_refs);
    if relevant_refs.is_empty() {
        return ClaimAdjudication::Unresolved {
            reason: "claim has no field-relevant trusted evidence refs".into(),
        };
    }
    let subset = evidence_subset(evidence, &relevant_refs);

    if claim.scope == "project"
        && claim.concept_type == "isolation_method"
        && evidence_project_values(evidence, "organisms").len() > 1
    {
        return ClaimAdjudication::Unresolved {
            reason: "project contains multiple explicit organisms; isolation evidence must be represented at branch/row scope or explicitly prove the same method across every biological branch before project broadcast".into(),
        };
    }

    match claim.concept_type.as_str() {
        "isolation_method" => {
            // Scientific fidelity precedes validator convenience. If the cited
            // evidence describes a real isolation method outside the pinned
            // template vocabulary, record the gap instead of coercing it into
            // the nearest allowed value.
            if let Some(gap) = infer_isolation_template_gap(&subset) {
                return ClaimAdjudication::TemplateGap {
                    observed_value: gap.observed_value,
                    evidence_refs: gap.evidence_refs,
                    reason: gap.reason,
                };
            }
            if let Some((value, refs)) = infer_isolation_method_scaffold(&subset) {
                return ClaimAdjudication::Canonical {
                    value,
                    evidence_refs: refs,
                };
            }
            ClaimAdjudication::Unresolved {
                reason: "trusted isolation evidence does not deterministically map to one supported single-cell isolation vocabulary value".into(),
            }
        }
        "acquisition_mode" => {
            if let Some((value, refs)) = infer_acquisition_method_repair(&subset) {
                return ClaimAdjudication::Canonical {
                    value,
                    evidence_refs: refs,
                };
            }
            ClaimAdjudication::Conflict {
                reason: "trusted acquisition evidence is absent, ambiguous, or supports both DDA and DIA; keep acquisition scope unresolved".into(),
            }
        }
        _ => {
            // For concepts without a deterministic scientific canonicalizer,
            // the model may suggest hypotheses but cannot publish them. A
            // supported claim still needs all cited evidence to be relevant.
            if claim.status != "supported"
                || !claim_refs_are_relevant(evidence, &claim.concept_type, &claim.evidence_refs)
            {
                return ClaimAdjudication::Unresolved {
                    reason: "non-canonicalized concepts require a model-supported claim with field-relevant evidence".into(),
                };
            }
            let value = match canonical_reserved_alias(claim.value.trim()) {
                Some(value) => value.to_string(),
                None => claim.value.trim().to_string(),
            };
            if value.is_empty() || value == "not available" {
                ClaimAdjudication::Unresolved {
                    reason: "supported claim does not contain a concrete publishable value".into(),
                }
            } else {
                ClaimAdjudication::SupportedConcept {
                    value,
                    evidence_refs: claim.evidence_refs.clone(),
                }
            }
        }
    }
}

fn adjudication_record(
    evidence: &DatasetEvidence,
    claim: &ScientificClaim,
) -> ClaimAdjudicationRecord {
    ClaimAdjudicationRecord {
        concept_type: claim.concept_type.clone(),
        scope: claim.scope.clone(),
        branch_id: claim.branch_id.clone(),
        model_status: claim.status.clone(),
        proposed_value: claim.value.clone(),
        evidence_refs: claim.evidence_refs.clone(),
        adjudication: adjudicate_claim(evidence, claim),
    }
}

fn canonical_claim_value(
    evidence: &DatasetEvidence,
    claim: &ScientificClaim,
) -> Option<(String, Vec<String>)> {
    match adjudicate_claim(evidence, claim) {
        ClaimAdjudication::Canonical {
            value,
            evidence_refs,
        }
        | ClaimAdjudication::SupportedConcept {
            value,
            evidence_refs,
        } => Some((value, evidence_refs)),
        ClaimAdjudication::TemplateGap { .. }
        | ClaimAdjudication::Unresolved { .. }
        | ClaimAdjudication::Conflict { .. } => None,
    }
}

fn branch_claims_require_project_mask(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    concept_type: &str,
) -> bool {
    state.claims.iter().any(|claim| {
        if claim.concept_type != concept_type || !matches!(claim.scope.as_str(), "branch" | "row") {
            return false;
        }
        matches!(
            adjudicate_claim(evidence, claim),
            ClaimAdjudication::Canonical { .. }
                | ClaimAdjudication::TemplateGap { .. }
                | ClaimAdjudication::SupportedConcept { .. }
        )
    })
}

fn branch_claims_are_heterogeneous(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    concept_type: &str,
) -> bool {
    let values = state
        .claims
        .iter()
        .filter(|claim| {
            claim.concept_type == concept_type
                && claim.scope == "branch"
                && matches!(claim.status.as_str(), "supported" | "hypothesis")
                && !claim.value.trim().is_empty()
                && claim_refs_are_relevant(evidence, &claim.concept_type, &claim.evidence_refs)
        })
        .map(|claim| claim.value.trim().to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    values.len() > 1
}

fn clear_proposal_field(proposal: &mut SdrfProposal, field: &str) {
    if let Some(slot) = proposal_field_mut(proposal, field) {
        *slot = "not available".into();
    }
    proposal.evidence_refs.remove(field);
}

fn proposal_field_value<'a>(proposal: &'a SdrfProposal, field: &str) -> Option<&'a str> {
    match field {
        "organism" => Some(&proposal.organism),
        "organism_part" => Some(&proposal.organism_part),
        "disease" => Some(&proposal.disease),
        "cell_type" => Some(&proposal.cell_type),
        "sample_type" => Some(&proposal.sample_type),
        "single_cell_isolation_method" => Some(&proposal.single_cell_isolation_method),
        "individual" => Some(&proposal.individual),
        "sample_preparation_batch" => Some(&proposal.sample_preparation_batch),
        "cells_per_well" => Some(&proposal.cells_per_well),
        "proteomics_data_acquisition_method" => Some(&proposal.proteomics_data_acquisition_method),
        "label" => Some(&proposal.label),
        "instrument" => Some(&proposal.instrument),
        "cleavage_agent_details" => Some(&proposal.cleavage_agent_details),
        "fraction_identifier" => Some(&proposal.fraction_identifier),
        "technical_replicate" => Some(&proposal.technical_replicate),
        "carrier_channel" => Some(&proposal.carrier_channel),
        "reference_channel" => Some(&proposal.reference_channel),
        _ => None,
    }
    .map(String::as_str)
}

fn apply_dynamic_semantic_bootstrap(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
    issues: &mut Vec<ValidationIssue>,
) {
    let isolation_current = proposal.single_cell_isolation_method.trim();
    let isolation_unresolved =
        isolation_current.is_empty() || canonical_reserved_alias(isolation_current).is_some();
    if isolation_unresolved {
        if let Some(gap) = infer_isolation_template_gap(&evidence.evidence) {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "scientific_agent_semantic_bootstrap_template_gap".into(),
                row: 0,
                column: SC_ISOLATION_METHOD.into(),
                message: format!(
                    "dynamic trusted evidence supports isolation method '{}' outside the pinned template vocabulary; keep SDRF value unresolved rather than substitute a false allowed term; refs={:?}",
                    gap.observed_value, gap.evidence_refs
                ),
            });
        } else if evidence_project_values(evidence, "organisms").len() <= 1 {
            if let Some((value, refs)) = infer_isolation_method_scaffold(&evidence.evidence) {
                proposal.single_cell_isolation_method = value.clone();
                proposal
                    .evidence_refs
                    .insert("single_cell_isolation_method".into(), refs.clone());
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "scientific_agent_semantic_bootstrap_canonicalized".into(),
                    row: 0,
                    column: SC_ISOLATION_METHOD.into(),
                    message: format!(
                        "dynamic trusted evidence deterministically supplied single_cell_isolation_method='{}' from refs {:?}",
                        value, refs
                    ),
                });
            }
        }
    }

    let acquisition_current = proposal.proteomics_data_acquisition_method.trim();
    let acquisition_unresolved =
        acquisition_current.is_empty() || canonical_reserved_alias(acquisition_current).is_some();
    if acquisition_unresolved {
        if let Some((value, refs)) = infer_acquisition_method_repair(&evidence.evidence) {
            proposal.proteomics_data_acquisition_method = value.clone();
            proposal
                .evidence_refs
                .insert("proteomics_data_acquisition_method".into(), refs.clone());
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "scientific_agent_semantic_bootstrap_canonicalized".into(),
                row: 0,
                column: "comment[proteomics data acquisition method]".into(),
                message: format!(
                    "dynamic trusted evidence deterministically supplied proteomics_data_acquisition_method='{}' from refs {:?}",
                    value, refs
                ),
            });
        }
    }
}

fn apply_scientific_overlay(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    issues: &mut Vec<ValidationIssue>,
) {
    // Branch heterogeneity has precedence over a project-wide deterministic
    // metadata guess. Mask only the affected field; branch values are applied
    // later and only to source-linked RAW files.
    for concept in scientific_concept_types() {
        let Some(field) = concept_to_sdrf_field(concept) else {
            continue;
        };
        if branch_claims_require_project_mask(evidence, state, concept)
            || branch_claims_are_heterogeneous(evidence, state, concept)
        {
            let previous = proposal_field_value(proposal, field)
                .unwrap_or("")
                .to_string();
            clear_proposal_field(proposal, field);
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "scientific_agent_branch_scope_masks_project_value".into(),
                row: 0,
                column: field_existing_header(field).unwrap_or(field).into(),
                message: format!(
                    "typed branch-scoped '{}' claims prevent safe project-wide broadcast; masked baseline value '{}' until trusted file-to-branch linkage is available",
                    concept, previous
                ),
            });
        }
    }

    for claim in &state.claims {
        if claim.scope != "project" {
            continue;
        }
        if branch_claims_require_project_mask(evidence, state, &claim.concept_type)
            || branch_claims_are_heterogeneous(evidence, state, &claim.concept_type)
        {
            continue;
        }
        let Some(field) = concept_to_sdrf_field(&claim.concept_type) else {
            continue;
        };
        match adjudicate_claim(evidence, claim) {
            ClaimAdjudication::TemplateGap {
                observed_value,
                evidence_refs,
                reason,
            } => {
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "scientific_agent_claim_template_gap_adjudicated".into(),
                    row: 0,
                    column: field_existing_header(field).unwrap_or(field).into(),
                    message: format!(
                        "compiler adjudicated typed {} claim '{}' as a source-backed template gap using refs {:?}: {}",
                        claim.concept_type, observed_value, evidence_refs, reason
                    ),
                });
                continue;
            }
            ClaimAdjudication::Unresolved { ref reason }
            | ClaimAdjudication::Conflict { ref reason } => {
                if matches!(
                    claim.concept_type.as_str(),
                    "isolation_method" | "acquisition_mode"
                ) {
                    issues.push(ValidationIssue {
                        level: "warning".into(),
                        code: "scientific_agent_claim_adjudication_unresolved".into(),
                        row: 0,
                        column: field_existing_header(field).unwrap_or(field).into(),
                        message: format!(
                            "typed {} claim was not publishable after deterministic evidence adjudication: {}",
                            claim.concept_type, reason
                        ),
                    });
                }
                continue;
            }
            ClaimAdjudication::Canonical { .. } | ClaimAdjudication::SupportedConcept { .. } => {}
        }
        let Some((value, refs)) = canonical_claim_value(evidence, claim) else {
            continue;
        };
        let current = proposal_field_value(proposal, field)
            .unwrap_or("")
            .trim()
            .to_string();
        let current_reserved = canonical_reserved_alias(&current).is_some() || current.is_empty();
        if current_reserved {
            if let Some(slot) = proposal_field_mut(proposal, field) {
                *slot = value.clone();
            }
            proposal.evidence_refs.insert(field.into(), refs);
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "scientific_agent_supported_project_claim_applied".into(),
                row: 0,
                column: field_existing_header(field).unwrap_or(field).into(),
                message: format!(
                    "compiler-adjudicated project claim supplied {}='{}' over an unresolved deterministic baseline",
                    field, value
                ),
            });
        } else if current.eq_ignore_ascii_case(&value) {
            let entry = proposal.evidence_refs.entry(field.into()).or_default();
            entry.extend(refs);
            entry.sort();
            entry.dedup();
        } else {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "scientific_agent_project_claim_conflicts_with_deterministic_baseline".into(),
                row: 0,
                column: field_existing_header(field).unwrap_or(field).into(),
                message: format!(
                    "compiler-adjudicated project claim proposed {}='{}' but deterministic baseline already has source-backed value '{}'; baseline retained and conflict exposed for review",
                    field, value, current
                ),
            });
        }
    }
}

fn row_value_is_unresolved(value: &str) -> bool {
    let trimmed = value.trim();
    trimmed.is_empty() || trimmed.eq_ignore_ascii_case("not available")
}

fn set_row_if_unresolved(
    row: &mut [String],
    header_index: &HashMap<&str, usize>,
    header: &str,
    value: String,
) {
    let Some(&idx) = header_index.get(header) else {
        return;
    };
    if idx < row.len() && row_value_is_unresolved(&row[idx]) {
        row[idx] = value;
    }
}

fn enforce_deterministic_row_scaffold(
    headers: &[String],
    rows: &mut [Vec<String>],
    evidence: &DatasetEvidence,
    relation_mode: &str,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
    if relation_mode != "one_cell_per_data_file" || !evidence.existing_sdrf_path.is_empty() {
        return issues;
    }
    let header_index = headers
        .iter()
        .enumerate()
        .map(|(index, header)| (header.as_str(), index))
        .collect::<HashMap<_, _>>();
    let Some(&data_file_idx) = header_index.get("comment[data file]") else {
        return issues;
    };

    for row in rows.iter_mut() {
        let raw = row.get(data_file_idx).cloned().unwrap_or_default();
        if raw.trim().is_empty() {
            continue;
        }
        let stem = safe_identifier_from_file(&raw);
        let role = match raw_file_role(&raw) {
            RawFileRole::Unknown => RawFileRole::SingleCell,
            role => role,
        };
        set_row_if_unresolved(
            row,
            &header_index,
            "comment[fraction identifier]",
            "1".into(),
        );
        set_row_if_unresolved(
            row,
            &header_index,
            "comment[technical replicate]",
            "1".into(),
        );
        match role {
            RawFileRole::SingleCell | RawFileRole::Unknown => {
                set_row_if_unresolved(row, &header_index, SC_SAMPLE_TYPE, "single cell".into());
                set_row_if_unresolved(row, &header_index, SC_CELL_IDENTIFIER, stem);
                set_row_if_unresolved(row, &header_index, SC_CELLS_PER_WELL, "1".into());
            }
            RawFileRole::FewCell(n) => {
                set_row_if_unresolved(row, &header_index, SC_SAMPLE_TYPE, "not available".into());
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_ISOLATION_METHOD,
                    "not applicable".into(),
                );
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_CELL_IDENTIFIER,
                    "not applicable".into(),
                );
                set_row_if_unresolved(row, &header_index, SC_CELLS_PER_WELL, n.to_string());
            }
            RawFileRole::Blank => {
                set_row_if_unresolved(row, &header_index, SC_SAMPLE_TYPE, "empty".into());
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_ISOLATION_METHOD,
                    "not applicable".into(),
                );
                set_row_if_unresolved(row, &header_index, SC_CELL_IDENTIFIER, "empty".into());
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_CELLS_PER_WELL,
                    "not applicable".into(),
                );
            }
            RawFileRole::QualityControl => {
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_SAMPLE_TYPE,
                    "quality control sample".into(),
                );
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_ISOLATION_METHOD,
                    "not applicable".into(),
                );
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_CELL_IDENTIFIER,
                    "not applicable".into(),
                );
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_CELLS_PER_WELL,
                    "not applicable".into(),
                );
            }
            RawFileRole::Bulk => {
                set_row_if_unresolved(row, &header_index, SC_SAMPLE_TYPE, "bulk control".into());
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_ISOLATION_METHOD,
                    "not applicable".into(),
                );
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_CELL_IDENTIFIER,
                    "not applicable".into(),
                );
                set_row_if_unresolved(
                    row,
                    &header_index,
                    SC_CELLS_PER_WELL,
                    "not applicable".into(),
                );
            }
        }
    }
    issues.push(ValidationIssue {
        level: "warning".into(),
        code: "scientific_agent_deterministic_row_scaffold_preserved".into(),
        row: 0,
        column: "comment[data file]".into(),
        message: "preserved deterministic one-cell-per-file row identifiers and structural replicate fields independently of model claims".into(),
    });
    issues
}

fn compiled_workspace_fingerprint(
    proposal: &SdrfProposal,
    headers: &[String],
    rows: &[Vec<String>],
) -> String {
    serde_json::to_string(&json!({
        "proposal": proposal,
        "headers": headers,
        "rows": rows,
    }))
    .unwrap_or_default()
}

fn compile_workspace(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    explicit_mappings: &[ExplicitRowMapping],
) -> Result<CompiledWorkspace> {
    let mut proposal = SdrfProposal::default();
    let mut issues = Vec::new();
    let mut deterministic_repairs = Vec::new();

    // Baseline preservation: deterministic repository/publication scaffolds are
    // compiled without model scope arbitration. The agent is an overlay, not a
    // replacement for already-working row construction and metadata inference.
    if study_design_has_assertive_relation_hint(&evidence.study_design) {
        proposal.relation_mode = evidence.study_design.relation_mode_hint.clone();
        proposal.evidence_refs.insert(
            "relation_mode".into(),
            evidence.study_design.relation_evidence_refs.clone(),
        );
    } else if state.relation.mode != "unresolved" && !state.relation.evidence_refs.is_empty() {
        proposal.relation_mode = state.relation.mode.clone();
        proposal
            .evidence_refs
            .insert("relation_mode".into(), state.relation.evidence_refs.clone());
    } else {
        proposal.relation_mode = "uncertain".into();
    }

    issues.extend(apply_deterministic_metadata_scaffold(
        &mut proposal,
        evidence,
        None,
    ));
    apply_dynamic_semantic_bootstrap(&mut proposal, evidence, &mut issues);
    apply_scientific_overlay(&mut proposal, evidence, state, &mut issues);
    issues.extend(apply_publication_compatibility_normalization(&mut proposal));
    if let Some(issue) = sanitize_nonindividual_semantic_proposal(&mut proposal) {
        deterministic_repairs.push(issue.code.clone());
        issues.push(issue);
    }
    issues.extend(repair_proposal_provenance(&mut proposal, evidence));

    // The deterministic relation hint is frozen after all overlays. Scientific
    // concepts may alter branch scope, but biological heterogeneity must not
    // erase acquisition cardinality.
    if study_design_has_assertive_relation_hint(&evidence.study_design) {
        proposal.relation_mode = evidence.study_design.relation_mode_hint.clone();
        proposal.evidence_refs.insert(
            "relation_mode".into(),
            evidence.study_design.relation_evidence_refs.clone(),
        );
    }
    validate_proposal_refs(&proposal, evidence)?;

    let (headers, mut rows, generation_mode) =
        draft_rows_with_explicit_mappings(&proposal, evidence, explicit_mappings)?;
    issues.extend(enforce_deterministic_row_scaffold(
        &headers,
        &mut rows,
        evidence,
        &proposal.relation_mode,
    ));

    let header_index = headers
        .iter()
        .enumerate()
        .map(|(index, header)| (header.as_str(), index))
        .collect::<HashMap<_, _>>();
    let data_file_idx = header_index.get("comment[data file]").copied();

    let mut file_to_branch: BTreeMap<String, Option<&AgentBranch>> = BTreeMap::new();
    for branch in &state.branches {
        if branch.status != "supported" || branch.linkage_status == "unresolved" {
            continue;
        }
        for raw in &branch.linked_raw_files {
            let key = raw.to_ascii_lowercase();
            match file_to_branch.get(&key) {
                None => {
                    file_to_branch.insert(key, Some(branch));
                }
                Some(_) => {
                    file_to_branch.insert(key, None);
                }
            }
        }
    }

    if let Some(file_idx) = data_file_idx {
        for row in &mut rows {
            let raw = row.get(file_idx).cloned().unwrap_or_default();
            let Some(Some(branch)) = file_to_branch.get(&raw.to_ascii_lowercase()) else {
                continue;
            };
            for claim in state.claims.iter().filter(|claim| {
                claim.status == "supported"
                    && claim.scope == "branch"
                    && claim.branch_id == branch.id
            }) {
                let Some(field) = concept_to_sdrf_field(&claim.concept_type) else {
                    continue;
                };
                let Some(header) = field_existing_header(field) else {
                    continue;
                };
                let Some(&idx) = header_index.get(header) else {
                    continue;
                };
                let Some((value, _)) = canonical_claim_value(evidence, claim) else {
                    continue;
                };
                if idx < row.len() {
                    row[idx] = value.clone();
                    issues.push(ValidationIssue {
                        level: "warning".into(),
                        code: "scientific_agent_branch_value_applied".into(),
                        row: 0,
                        column: header.into(),
                        message: format!(
                            "source-grounded branch '{}' applied {}='{}' to RAW {}",
                            branch.id, field, value, raw
                        ),
                    });
                }
            }
        }
    }

    issues.extend(validate_annotation_draft(&headers, &rows, evidence, false));
    let adjudications = state
        .claims
        .iter()
        .map(|claim| adjudication_record(evidence, claim))
        .collect::<Vec<_>>();
    let fingerprint = compiled_workspace_fingerprint(&proposal, &headers, &rows);
    Ok(CompiledWorkspace {
        proposal,
        headers,
        rows,
        generation_mode,
        issues,
        deterministic_repairs,
        adjudications,
        fingerprint,
    })
}

fn validation_cycle(cycle: usize, issues: &[ValidationIssue]) -> AgentValidationCycle {
    let mut error_counts = BTreeMap::new();
    let mut representatives = Vec::new();
    for issue in issues {
        if issue.level == "error" {
            *error_counts.entry(issue.code.clone()).or_insert(0) += 1;
            if representatives.len() < 12 {
                representatives.push(format!(
                    "{} row={} column={}: {}",
                    issue.code, issue.row, issue.column, issue.message
                ));
            }
        }
    }
    AgentValidationCycle {
        cycle,
        validation_errors: issues.iter().filter(|i| i.level == "error").count(),
        validation_warnings: issues.iter().filter(|i| i.level == "warning").count(),
        error_counts,
        representative_messages: representatives,
    }
}

fn append_adjudication_feedback(
    feedback: &mut Vec<String>,
    compiled: &CompiledWorkspace,
    turn: usize,
) {
    for record in &compiled.adjudications {
        if !matches!(
            record.concept_type.as_str(),
            "isolation_method" | "acquisition_mode"
        ) {
            continue;
        }
        let message = match &record.adjudication {
            ClaimAdjudication::Canonical { value, evidence_refs } => format!(
                "turn {} compiler adjudicated {} claim '{}' -> canonical '{}' from refs {:?}",
                turn, record.concept_type, record.proposed_value, value, evidence_refs
            ),
            ClaimAdjudication::TemplateGap { observed_value, evidence_refs, .. } => format!(
                "turn {} compiler adjudicated {} claim '{}' as template gap '{}' from refs {:?}; do not substitute a nearby allowed value",
                turn, record.concept_type, record.proposed_value, observed_value, evidence_refs
            ),
            ClaimAdjudication::Unresolved { reason } | ClaimAdjudication::Conflict { reason } => format!(
                "turn {} compiler could not publish {} claim '{}' from refs {:?}: {}; search for direct field-specific method evidence or leave unresolved",
                turn, record.concept_type, record.proposed_value, record.evidence_refs, reason
            ),
            ClaimAdjudication::SupportedConcept { .. } => continue,
        };
        if !feedback.iter().any(|existing| existing == &message) {
            feedback.push(message);
        }
    }
}

fn trim_actions_to_budget(
    actions: &[AgentEvidenceAction],
    remaining: usize,
) -> Vec<EvidenceActionRequest> {
    let mut left = remaining;
    let mut out = Vec::new();
    for action in actions {
        if left == 0 {
            break;
        }
        let mut request = agent_action_to_request(action);
        if request.action == "ABSTAIN" {
            out.push(request);
            left = left.saturating_sub(1);
            continue;
        }
        request.queries.truncate(left.min(8));
        if !request.queries.is_empty() {
            left = left.saturating_sub(request.queries.len());
            out.push(request);
        }
    }
    out
}

fn workspace_paths(root: &Path, accession: &str) -> (PathBuf, PathBuf, PathBuf, PathBuf, PathBuf) {
    let workspace = root.join("workspaces").join(accession);
    (
        workspace.clone(),
        root.join("sdrf").join(format!("{accession}.sdrf.tsv")),
        root.join("review")
            .join(format!("{accession}.sdrf.review.tsv")),
        root.join("audit")
            .join(format!("{accession}.scientific_agent.json")),
        root.join("evidence")
            .join(format!("{accession}.evidence.json")),
    )
}

async fn run_one_scientific_agent(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let mut evidence = build_evidence(&annotate_opts, accession)?;
    let explicit_mappings = if let Some(path) = opts.explicit_row_mapping_manifest.as_deref() {
        if !path.is_file() {
            bail!(
                "explicit row-mapping manifest not found: {}",
                path.display()
            );
        }
        load_explicit_row_mappings(path, accession, &evidence.raw_files)?
    } else {
        Vec::new()
    };
    let (workspace_dir, draft_path, review_path, audit_path, evidence_path) =
        workspace_paths(&opts.output_dir, accession);
    let directories: [&Path; 5] = [
        workspace_dir.as_path(),
        draft_path.parent().unwrap(),
        review_path.parent().unwrap(),
        audit_path.parent().unwrap(),
        evidence_path.parent().unwrap(),
    ];
    for path in directories {
        fs::create_dir_all(path)?;
    }
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
    fs::write(workspace_dir.join("action_history.json"), "[]\n")?;
    fs::write(workspace_dir.join("validation_history.json"), "[]\n")?;

    let mut state = ScientificWorkspaceState {
        harness_version: SCIENTIFIC_AGENT_HARNESS_VERSION.into(),
        accession: accession.into(),
        turn: 0,
        relation: AgentRelation {
            mode: if study_design_has_assertive_relation_hint(&evidence.study_design) {
                evidence.study_design.relation_mode_hint.clone()
            } else {
                "unresolved".into()
            },
            scope: if study_design_has_assertive_relation_hint(&evidence.study_design) {
                "project".into()
            } else {
                "unresolved".into()
            },
            evidence_refs: evidence.study_design.relation_evidence_refs.clone(),
            confidence: evidence.study_design.relation_confidence.clone(),
            reason: evidence.study_design.notes.clone(),
        },
        next_step: "search".into(),
        ..Default::default()
    };
    let mut trace = ScientificAgentTrace {
        harness_version: SCIENTIFIC_AGENT_HARNESS_VERSION.into(),
        accession: accession.into(),
        ..Default::default()
    };
    trace.states.push(state.clone());
    fs::write(
        workspace_dir.join("state.turn00.json"),
        serde_json::to_string_pretty(&state)?,
    )?;
    fs::write(
        workspace_dir.join("state.json"),
        serde_json::to_string_pretty(&state)?,
    )?;
    let mut attempted = BTreeSet::new();
    let max_turns = opts.max_agent_turns.max(1);
    let max_actions = opts.max_tool_actions.max(1);
    let max_validator_cycles = opts.max_validator_cycles.max(1);

    // Codex-style bootstrap: compile and validate the deterministic baseline
    // before asking the model to reason. The first agent turn therefore sees
    // the actual unresolved scientific tasks instead of inventing a parallel
    // replacement for already-working row structure.
    let baseline = compile_workspace(&evidence, &state, &explicit_mappings)?;
    fs::write(
        workspace_dir.join("adjudications.json"),
        serde_json::to_string_pretty(&baseline.adjudications)?,
    )?;
    write_sdrf(&draft_path, &baseline.headers, &baseline.rows)?;
    write_validation_review(&review_path, &baseline.issues)?;
    trace.validator_cycles_completed = 1;
    let baseline_cycle = validation_cycle(1, &baseline.issues);
    let baseline_errors = baseline_cycle.validation_errors;
    trace.validation_history.push(baseline_cycle);
    trace.harness_feedback.push(format!(
        "deterministic baseline compiled before agent turn 1 with {} validation error(s); preserve working baseline fields and focus only on unresolved scientific concepts",
        baseline_errors
    ));
    fs::write(
        workspace_dir.join("validation_history.json"),
        serde_json::to_string_pretty(&trace.validation_history)?,
    )?;
    let mut last_compile_fingerprint: Option<String> = Some(baseline.fingerprint.clone());
    let mut compiled: Option<CompiledWorkspace> = Some(baseline);
    if baseline_errors == 0 {
        trace.terminal_status = "resolved_baseline".into();
        state.next_step = "finish".into();
        state.notes =
            "deterministic baseline validated without scientific-agent intervention".into();
        trace.states[0] = state.clone();
        fs::write(
            workspace_dir.join("state.turn00.json"),
            serde_json::to_string_pretty(&state)?,
        )?;
        fs::write(
            workspace_dir.join("state.json"),
            serde_json::to_string_pretty(&state)?,
        )?;
    }

    if trace.terminal_status.is_empty() {
        for turn in 1..=max_turns {
            let next = call_scientific_agent(
                opts,
                &evidence,
                &state,
                &trace.evidence_action_results,
                &trace.validation_history,
                &trace.harness_feedback,
                turn,
            )
            .await?;
            state = next;
            trace.states.push(state.clone());
            trace.turns_completed = turn;
            fs::write(
                workspace_dir.join(format!("state.turn{turn:02}.json")),
                serde_json::to_string_pretty(&state)?,
            )?;
            fs::write(
                workspace_dir.join("state.json"),
                serde_json::to_string_pretty(&state)?,
            )?;

            if state.next_step == "search" {
                let remaining = max_actions.saturating_sub(trace.tool_actions_completed);
                if remaining == 0 {
                    trace.terminal_status = "evidence_exhausted".into();
                    break;
                }
                let actions = trim_actions_to_budget(&state.next_evidence_actions, remaining);
                if actions.is_empty() {
                    trace.harness_feedback.push(
                    "search requested but no executable typed action remained after normalization; choose a different search, compile only after a material state change, finish, or abstain".into(),
                );
                    continue;
                }
                let round_results =
                    execute_evidence_actions(&mut evidence, &actions, turn, &mut attempted);
                trace.tool_actions_completed += round_results.len();
                trace.evidence_action_results.extend(round_results);
                fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
                fs::write(
                    workspace_dir.join("action_history.json"),
                    serde_json::to_string_pretty(&trace.evidence_action_results)?,
                )?;
                continue;
            }

            let compiled_now = compile_workspace(&evidence, &state, &explicit_mappings)?;
            fs::write(
                workspace_dir.join("adjudications.json"),
                serde_json::to_string_pretty(&compiled_now.adjudications)?,
            )?;
            append_adjudication_feedback(&mut trace.harness_feedback, &compiled_now, turn);
            if last_compile_fingerprint
                .as_deref()
                .is_some_and(|previous| previous == compiled_now.fingerprint.as_str())
            {
                compiled = Some(compiled_now);
                if state.next_step == "abstain" {
                    trace.terminal_status = "abstained".into();
                    break;
                }
                if state.next_step == "finish" {
                    trace.terminal_status = "partial".into();
                    break;
                }
                trace.harness_feedback.push(format!(
                "turn {} compile blocked: normalized scientific state produced the same deterministic draft as the previous validated compile; do not compile again without changing evidence-backed claims/branches",
                turn
            ));
                continue;
            }
            last_compile_fingerprint = Some(compiled_now.fingerprint.clone());
            write_sdrf(&draft_path, &compiled_now.headers, &compiled_now.rows)?;
            write_validation_review(&review_path, &compiled_now.issues)?;
            trace.validator_cycles_completed += 1;
            let cycle = validation_cycle(trace.validator_cycles_completed, &compiled_now.issues);
            let errors = cycle.validation_errors;
            trace.validation_history.push(cycle);
            fs::write(
                workspace_dir.join("validation_history.json"),
                serde_json::to_string_pretty(&trace.validation_history)?,
            )?;
            compiled = Some(compiled_now);

            if errors == 0 {
                trace.terminal_status = "resolved".into();
                break;
            }
            if state.next_step == "abstain" {
                trace.terminal_status = "abstained".into();
                break;
            }
            if state.next_step == "finish" {
                trace.terminal_status = "partial".into();
                break;
            }
            if trace.validator_cycles_completed >= max_validator_cycles {
                trace.terminal_status = "validation_exhausted".into();
                break;
            }
        }
    }

    if compiled.is_none() {
        let compiled_now = compile_workspace(&evidence, &state, &explicit_mappings)?;
        fs::write(
            workspace_dir.join("adjudications.json"),
            serde_json::to_string_pretty(&compiled_now.adjudications)?,
        )?;
        append_adjudication_feedback(
            &mut trace.harness_feedback,
            &compiled_now,
            trace.turns_completed,
        );
        write_sdrf(&draft_path, &compiled_now.headers, &compiled_now.rows)?;
        write_validation_review(&review_path, &compiled_now.issues)?;
        trace.validator_cycles_completed += 1;
        trace.validation_history.push(validation_cycle(
            trace.validator_cycles_completed,
            &compiled_now.issues,
        ));
        fs::write(
            workspace_dir.join("validation_history.json"),
            serde_json::to_string_pretty(&trace.validation_history)?,
        )?;
        compiled = Some(compiled_now);
    }
    if trace.terminal_status.is_empty() {
        trace.terminal_status = if trace.turns_completed >= max_turns {
            "turn_budget_exhausted".into()
        } else {
            "partial".into()
        };
    }
    let compiled = compiled.unwrap();
    let validation_errors = compiled
        .issues
        .iter()
        .filter(|issue| issue.level == "error")
        .count();
    let locally_valid = validation_errors == 0;

    fs::write(
        workspace_dir.join("trace.json"),
        serde_json::to_string_pretty(&trace)?,
    )?;
    fs::write(
        workspace_dir.join("proposal.json"),
        serde_json::to_string_pretty(&compiled.proposal)?,
    )?;
    let audit = json!({
        "harness_version": SCIENTIFIC_AGENT_HARNESS_VERSION,
        "generator_version": GENERATOR_VERSION,
        "accession": accession,
        "terminal_status": trace.terminal_status.clone(),
        "turns_completed": trace.turns_completed,
        "tool_actions_completed": trace.tool_actions_completed,
        "validator_cycles_completed": trace.validator_cycles_completed,
        "generation_mode": compiled.generation_mode,
        "relation_mode": compiled.proposal.relation_mode.clone(),
        "locally_valid": locally_valid,
        "validation_errors": validation_errors,
        "branches": state.branches.clone(),
        "claims": state.claims.clone(),
        "claim_adjudications": compiled.adjudications.clone(),
        "open_questions": state.open_questions.clone(),
        "conflicts": state.conflicts.clone(),
        "harness_feedback": trace.harness_feedback.clone(),
        "deterministic_repairs": compiled.deterministic_repairs,
        "draft_path": draft_path.display().to_string(),
        "review_path": review_path.display().to_string(),
        "workspace_path": workspace_dir.display().to_string()
    });
    fs::write(&audit_path, serde_json::to_string_pretty(&audit)?)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: trace.terminal_status,
        turns: trace.turns_completed,
        tool_actions: trace.tool_actions_completed,
        validator_cycles: trace.validator_cycles_completed,
        branches: state
            .branches
            .iter()
            .filter(|b| b.status == "supported")
            .count(),
        open_questions: state.open_questions.len(),
        relation_mode: compiled.proposal.relation_mode,
        locally_valid,
        validation_errors,
        draft_path: draft_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: workspace_dir.display().to_string(),
        error: String::new(),
    })
}

pub async fn run_scientific_sdrf_agent(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    if opts.max_agent_turns == 0 || opts.max_tool_actions == 0 || opts.max_validator_cycles == 0 {
        bail!("scientific-agent budgets must all be greater than zero");
    }
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts.output_dir.join("scientific_agent_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!("[{}/{}] {}", i + 1, accessions.len(), accession);
        }
        match run_one_scientific_agent(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> status={} terminal={} turns={} tools={} validators={} branches={} valid={} errors={}",
                        row.status,
                        row.terminal_status,
                        row.turns,
                        row.tool_actions,
                        row.validator_cycles,
                        row.branches,
                        row.locally_valid,
                        row.validation_errors
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> scientific-agent error: {err:#}");
                rows.push(ScientificAgentResultRow {
                    accession: accession.clone(),
                    status: "error".into(),
                    terminal_status: "error".into(),
                    turns: 0,
                    tool_actions: 0,
                    validator_cycles: 0,
                    branches: 0,
                    open_questions: 0,
                    relation_mode: String::new(),
                    locally_valid: false,
                    validation_errors: 0,
                    draft_path: String::new(),
                    review_path: String::new(),
                    workspace_path: String::new(),
                    error: format!("{err:#}"),
                });
            }
        }
    }
    let mut writer = WriterBuilder::new()
        .delimiter(b'\t')
        .from_path(&results_path)?;
    for row in &rows {
        writer.serialize(row)?;
    }
    writer.flush()?;
    let successful = rows.iter().filter(|r| r.status == "success").count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_HARNESS_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts: rows.iter().filter(|r| r.locally_valid).count(),
        incomplete_drafts: rows
            .iter()
            .filter(|r| r.status == "success" && !r.locally_valid)
            .count(),
        total_validation_errors: rows.iter().map(|r| r.validation_errors).sum(),
        total_agent_turns: rows.iter().map(|r| r.turns).sum(),
        total_tool_actions: rows.iter().map(|r| r.tool_actions).sum(),
        total_validator_cycles: rows.iter().map(|r| r.validator_cycles).sum(),
        results_tsv: results_path.display().to_string(),
        workspace_root: opts.output_dir.join("workspaces").display().to_string(),
    };
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        serde_json::to_string_pretty(&summary)?,
    )?;
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn evidence_with(items: Vec<EvidenceItem>, raw_files: Vec<&str>) -> DatasetEvidence {
        DatasetEvidence {
            accession: "PXDTEST".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: raw_files
                .into_iter()
                .map(|file_name| RawFile {
                    file_name: file_name.into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                })
                .collect(),
            study_design: StudyDesignScaffold {
                relation_mode_hint: "one_cell_per_data_file".into(),
                relation_confidence: "high".into(),
                relation_evidence_refs: vec!["E0001".into()],
                repository_file_mode: "direct_acquisition_files".into(),
                ..Default::default()
            },
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: items,
            manuscript_sources: Vec::new(),
            annotation_sources: Vec::new(),
        }
    }

    fn claim(concept_type: &str, value: &str, scope: &str, branch_id: &str) -> ScientificClaim {
        claim_with_status(concept_type, value, scope, branch_id, "supported")
    }

    fn claim_with_status(
        concept_type: &str,
        value: &str,
        scope: &str,
        branch_id: &str,
        status: &str,
    ) -> ScientificClaim {
        ScientificClaim {
            concept_type: concept_type.into(),
            value: value.into(),
            scope: scope.into(),
            branch_id: branch_id.into(),
            status: status.into(),
            evidence_refs: vec!["E0001".into()],
            confidence: "high".into(),
            reason: "test".into(),
        }
    }

    #[test]
    fn branch_linkage_requires_source_evidence_that_names_raw_file() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "paper.txt".into(),
                text: "HeLa branch uses runA.raw".into(),
            }],
            vec!["runA.raw", "runB.raw"],
        );
        let mut state = ScientificWorkspaceState {
            branches: vec![AgentBranch {
                id: "hela".into(),
                label: "HeLa".into(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                linked_raw_files: vec!["runA.raw".into(), "runB.raw".into()],
                linkage_status: "supported".into(),
                ..Default::default()
            }],
            next_step: "compile".into(),
            ..Default::default()
        };
        normalize_workspace_state(&evidence, &mut state, 1);
        assert_eq!(state.branches[0].linked_raw_files, vec!["runA.raw"]);
    }

    #[test]
    fn hydrodynamic_loading_is_canonicalized_before_serialization() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "paper.txt".into(),
                text: "isolated single cells were manually loaded into the capillary using hydrodynamic pressure".into(),
            }],
            vec!["runA.raw"],
        );
        let claim = claim("isolation_method", "capillary loading", "project", "");
        let (value, refs) = canonical_claim_value(&evidence, &claim).unwrap();
        assert_eq!(value, "manual picking");
        assert_eq!(refs, vec!["E0001"]);
    }

    #[test]
    fn hydrodynamic_hypothesis_is_promoted_by_rust_evidence_adjudication() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "single cells were loaded into the capillary by hydrodynamic injection"
                    .into(),
            }],
            vec!["runA.raw"],
        );
        let claim = claim_with_status(
            "isolation_method",
            "hydrodynamic capillary loading",
            "project",
            "",
            "hypothesis",
        );
        match adjudicate_claim(&evidence, &claim) {
            ClaimAdjudication::Canonical {
                value,
                evidence_refs,
            } => {
                assert_eq!(value, "manual picking");
                assert_eq!(evidence_refs, vec!["E0001"]);
            }
            other => panic!("expected canonical adjudication, got {other:?}"),
        }
    }

    #[test]
    fn source_backed_microwell_hypothesis_is_template_gap_not_false_canonical_value() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "single cells were transferred to the microwell chip for analysis".into(),
            }],
            vec!["runA.raw"],
        );
        let claim = claim_with_status(
            "isolation_method",
            "microwell chip",
            "project",
            "",
            "hypothesis",
        );
        match adjudicate_claim(&evidence, &claim) {
            ClaimAdjudication::TemplateGap { observed_value, .. } => {
                assert_eq!(observed_value, "microwell-chip single-cell transfer");
            }
            other => panic!("expected template gap, got {other:?}"),
        }
        assert!(canonical_claim_value(&evidence, &claim).is_none());
    }

    #[test]
    fn generic_hypothesis_is_not_promoted_without_deterministic_canonicalizer() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "organism".into(),
                text: "Homo sapiens cells were analyzed".into(),
            }],
            vec!["runA.raw"],
        );
        let claim = claim_with_status("organism", "Homo sapiens", "project", "", "hypothesis");
        assert!(matches!(
            adjudicate_claim(&evidence, &claim),
            ClaimAdjudication::Unresolved { .. }
        ));
    }

    #[test]
    fn dynamic_semantic_bootstrap_uses_newly_retrieved_isolation_evidence() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "single cells were loaded into the capillary using hydrodynamic pressure"
                    .into(),
            }],
            vec!["runA.raw"],
        );
        let mut proposal = SdrfProposal::default();
        let mut issues = Vec::new();
        apply_dynamic_semantic_bootstrap(&mut proposal, &evidence, &mut issues);
        assert_eq!(proposal.single_cell_isolation_method, "manual picking");
        assert_eq!(
            proposal.evidence_refs["single_cell_isolation_method"],
            vec!["E0001"]
        );
        assert!(issues
            .iter()
            .any(|issue| { issue.code == "scientific_agent_semantic_bootstrap_canonicalized" }));
    }

    #[test]
    fn adjudicated_branch_hypothesis_can_mask_unsafe_project_value() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "single cells were loaded by hydrodynamic injection".into(),
            }],
            vec!["runA.raw"],
        );
        let state = ScientificWorkspaceState {
            claims: vec![claim_with_status(
                "isolation_method",
                "hydrodynamic loading",
                "branch",
                "b1",
                "hypothesis",
            )],
            ..Default::default()
        };
        assert!(branch_claims_require_project_mask(
            &evidence,
            &state,
            "isolation_method"
        ));
    }

    #[test]
    fn canonical_state_reducer_deduplicates_repeated_claims() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "paper.txt".into(),
                text: "manual hydrodynamic loading of single cells".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            claims: vec![
                claim("isolation_method", "manual loading", "project", ""),
                claim("isolation_method", "manual loading", "project", ""),
                claim("isolation_method", "manual loading", "project", ""),
            ],
            next_step: "compile".into(),
            ..Default::default()
        };
        normalize_workspace_state(&evidence, &mut state, 1);
        assert_eq!(state.claims.len(), 1);
    }

    #[test]
    fn conflicting_supported_claims_become_hypotheses_and_explicit_conflict() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "paper.txt".into(),
                text: "human and mouse organisms are both described".into(),
            }],
            vec!["runA.raw"],
        );
        let mut claims = vec![
            claim("organism", "Homo sapiens", "project", ""),
            claim("organism", "Mus musculus", "project", ""),
        ];
        let mut conflicts = Vec::new();
        reduce_scientific_claims(&evidence, &BTreeSet::new(), &mut claims, &mut conflicts);
        assert!(claims.iter().all(|claim| claim.status != "supported"));
        assert_eq!(conflicts.len(), 1);
    }

    #[test]
    fn non_exact_raw_query_is_reformulated_instead_of_rejected() {
        let evidence = evidence_with(Vec::new(), vec!["runA.raw"]);
        let mut action = AgentEvidenceAction {
            action: "EXPAND_EVIDENCE_CONTEXT".into(),
            reason: "search branch pattern".into(),
            target_concepts: vec!["organism".into()],
            queries: vec![EvidenceQuery {
                match_kind: "raw_exact".into(),
                value: "BS01-BS09 HeLa files".into(),
                terms: vec!["BS01".into(), "HeLa".into()],
                ..Default::default()
            }],
        };
        normalize_scientific_agent_action(&evidence, &mut action);
        assert_eq!(action.action, "EXPAND_EVIDENCE_CONTEXT");
        assert_eq!(action.queries[0].match_kind, "terms_all");
    }

    #[test]
    fn exact_raw_query_is_routed_to_exact_raw_tool() {
        let evidence = evidence_with(Vec::new(), vec!["runA.raw"]);
        let mut action = AgentEvidenceAction {
            action: "EXPAND_EVIDENCE_CONTEXT".into(),
            reason: "exact lookup".into(),
            target_concepts: vec!["organism".into()],
            queries: vec![EvidenceQuery {
                match_kind: "raw_exact".into(),
                value: "runA.raw".into(),
                ..Default::default()
            }],
        };
        normalize_scientific_agent_action(&evidence, &mut action);
        assert_eq!(action.action, "SEARCH_EXACT_RAW_NAME");
    }

    #[test]
    fn deterministic_row_scaffold_restores_structural_fields() {
        let evidence = evidence_with(Vec::new(), vec!["cellA.raw"]);
        let headers = vec![
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_CELL_IDENTIFIER.into(),
            SC_CELLS_PER_WELL.into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
        ];
        let mut rows = vec![vec![
            "cellA.raw".into(),
            "not available".into(),
            "not available".into(),
            "not available".into(),
            "not available".into(),
            "not available".into(),
        ]];
        enforce_deterministic_row_scaffold(
            &headers,
            &mut rows,
            &evidence,
            "one_cell_per_data_file",
        );
        assert_eq!(rows[0][1], "single cell");
        assert_eq!(rows[0][2], "cellA");
        assert_eq!(rows[0][3], "1");
        assert_eq!(rows[0][4], "1");
        assert_eq!(rows[0][5], "1");
    }

    #[test]
    fn branch_heterogeneity_masks_project_baseline() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "paper.txt".into(),
                text: "human and Xenopus branches".into(),
            }],
            vec!["runA.raw"],
        );
        let state = ScientificWorkspaceState {
            claims: vec![
                claim("organism", "Homo sapiens", "branch", "human"),
                claim("organism", "Xenopus laevis", "branch", "xeno"),
            ],
            ..Default::default()
        };
        let mut proposal = SdrfProposal {
            organism: "Homo sapiens".into(),
            ..Default::default()
        };
        proposal
            .evidence_refs
            .insert("organism".into(), vec!["E0001".into()]);
        let mut issues = Vec::new();
        apply_scientific_overlay(&mut proposal, &evidence, &state, &mut issues);
        assert_eq!(proposal.organism, "not available");
        assert!(issues
            .iter()
            .any(|issue| issue.code == "scientific_agent_branch_scope_masks_project_value"));
    }

    #[test]
    fn validation_feedback_clusters_repeated_row_errors() {
        let issues = (1..=15)
            .map(|row| ValidationIssue {
                level: "error".into(),
                code: "single_cell_isolation_unresolved".into(),
                row,
                column: SC_ISOLATION_METHOD.into(),
                message: "missing".into(),
            })
            .collect::<Vec<_>>();
        let cycle = validation_cycle(1, &issues);
        assert_eq!(cycle.validation_errors, 15);
        assert_eq!(cycle.error_counts["single_cell_isolation_unresolved"], 15);
        assert!(cycle.representative_messages.len() <= 12);
    }

    #[test]
    fn action_budget_is_global_and_bounded() {
        let actions = vec![AgentEvidenceAction {
            action: "SEARCH_PUBLICATION".into(),
            reason: "test".into(),
            target_concepts: vec!["organism".into()],
            queries: (0..8)
                .map(|i| EvidenceQuery {
                    match_kind: "phrase".into(),
                    value: format!("query{i}"),
                    ..Default::default()
                })
                .collect(),
        }];
        let trimmed = trim_actions_to_budget(&actions, 3);
        assert_eq!(trimmed.len(), 1);
        assert_eq!(trimmed[0].queries.len(), 3);
    }

    #[test]
    fn deterministic_relation_hint_is_frozen_into_workspace() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "test".into(),
                source_label: "test".into(),
                text: "one acquisition unit per file".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            relation: AgentRelation {
                mode: "mixed".into(),
                scope: "branch".into(),
                ..Default::default()
            },
            next_step: "compile".into(),
            ..Default::default()
        };
        normalize_workspace_state(&evidence, &mut state, 1);
        assert_eq!(state.relation.mode, "one_cell_per_data_file");
        assert_eq!(state.relation.scope, "project");
    }

    #[test]
    fn unsupported_claim_without_field_relevant_refs_becomes_hypothesis() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project.json".into(),
                text: "instrument Orbitrap".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            claims: vec![claim("organism", "Homo sapiens", "project", "")],
            next_step: "compile".into(),
            ..Default::default()
        };
        normalize_workspace_state(&evidence, &mut state, 1);
        assert_eq!(state.claims[0].status, "hypothesis");
    }

    #[test]
    fn compiled_fingerprint_is_stable_for_identical_inputs() {
        let proposal = SdrfProposal::default();
        let headers = vec!["comment[data file]".to_string()];
        let rows = vec![vec!["runA.raw".to_string()]];
        assert_eq!(
            compiled_workspace_fingerprint(&proposal, &headers, &rows),
            compiled_workspace_fingerprint(&proposal, &headers, &rows)
        );
    }
}
