use super::*;

pub const SCIENTIFIC_AGENT_HARNESS_VERSION: &str = "pride-scp-scientific-agent-v0.1";
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
struct AgentAssertion {
    field: String,
    value: String,
    scope: String,
    branch_id: String,
    status: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
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
    assertions: Vec<AgentAssertion>,
    #[serde(default)]
    open_questions: Vec<String>,
    #[serde(default)]
    conflicts: Vec<String>,
    #[serde(default)]
    next_evidence_actions: Vec<EvidenceActionRequest>,
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
}

fn canonical_agent_fields() -> Vec<&'static str> {
    vec![
        "organism",
        "organism_part",
        "disease",
        "cell_type",
        "sample_type",
        "single_cell_isolation_method",
        "individual",
        "sample_preparation_batch",
        "cells_per_well",
        "proteomics_data_acquisition_method",
        "label",
        "instrument",
        "cleavage_agent_details",
        "fraction_identifier",
        "technical_replicate",
        "carrier_channel",
        "reference_channel",
    ]
}

fn scientific_agent_schema() -> Value {
    let fields = canonical_agent_fields();
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
            "assertions":{"type":"array","maxItems":96,"items":{"type":"object","properties":{
                "field":{"type":"string","enum":fields.clone()},
                "value":{"type":"string","maxLength":500},
                "scope":{"type":"string","enum":["project","branch","row","unresolved"]},
                "branch_id":{"type":"string","maxLength":50},
                "status":{"type":"string","enum":["supported","hypothesis","unresolved","rejected"]},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":16},
                "confidence":{"type":"string","enum":["high","medium","low"]},
                "reason":{"type":"string","maxLength":600}
            },"required":["field","value","scope","branch_id","status","evidence_refs","confidence","reason"],"additionalProperties":false}},
            "open_questions":{"type":"array","items":{"type":"string","maxLength":400},"maxItems":32},
            "conflicts":{"type":"array","items":{"type":"string","maxLength":400},"maxItems":32},
            "next_evidence_actions":{"type":"array","maxItems":8,"items":{"type":"object","properties":{
                "action":{"type":"string","enum":["SEARCH_PUBLICATION","SEARCH_SUPPLEMENT","SEARCH_STRUCTURED_DESIGN","SEARCH_REPOSITORY_METADATA","SEARCH_EXACT_RAW_NAME","EXPAND_EVIDENCE_CONTEXT","LOOKUP_KG_TERM","COMPARE_CONFLICTING_EVIDENCE","ABSTAIN"]},
                "reason":{"type":"string","maxLength":400},
                "target_fields":{"type":"array","items":{"type":"string","enum":fields.clone()},"maxItems":8},
                "queries":{"type":"array","items":query,"maxItems":8}
            },"required":["action","reason","target_fields","queries"],"additionalProperties":false}},
            "next_step":{"type":"string","enum":["search","compile","finish","abstain"]},
            "notes":{"type":"string","maxLength":1400}
        },
        "required":["harness_version","accession","turn","relation","branches","assertions","open_questions","conflicts","next_evidence_actions","next_step","notes"],
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
    format!(
        "You are the scientific annotation agent for PRIDE single-cell proteomics dataset {acc}.\n\n\
Your job is to behave like a careful coding/research agent: inspect evidence, maintain a persistent study-design model, request tools when information is missing, compile only when the current design is coherent enough to test, inspect validator feedback, revise the study model, and stop when the SDRF is valid or the evidence/action budget is exhausted.\n\n\
You are NOT directly writing SDRF rows. Rust deterministically compiles your supported study-design assertions into SDRF after checking provenance, scope, exact file linkage, controlled vocabulary, and conflicts.\n\n\
HARD SCIENTIFIC CONTRACT:\n\
1. Never use GT labels or hidden benchmark truth.\n\
2. Filename words are search hints and contradiction detectors, NOT biological identity. A branch may list linked_raw_files only when cited E#### source evidence explicitly names or otherwise source-links those exact RAW basenames.\n\
3. Keep project, branch, row, and unresolved scopes distinct. A project assertion means the same scientific value holds across every relevant acquisition branch.\n\
4. Biological heterogeneity (organism, condition, sex, cell type) is separate from acquisition cardinality.\n\
5. Do not collapse multiple organisms or acquisition regimes into one project value. Represent separate branches and leave file linkage unresolved when evidence is insufficient.\n\
6. For isolation and acquisition, describe the scientific intent faithfully; Rust owns final SDRF controlled-vocabulary canonicalization.\n\
7. A supported assertion requires field-relevant E#### refs. Hypotheses may guide retrieval but cannot be serialized.\n\
8. Prefer explicit open_questions over guessed values.\n\
9. Use next_step='search' only with executable evidence actions. Use 'compile' when the current graph is coherent enough to test. Use 'finish' when no further safe retrieval is needed. Use 'abstain' when evidence cannot safely resolve the remaining design.\n\
10. Validator feedback is evidence about the draft/compiler state, not permission to invent metadata. If validation exposes a scientific ambiguity, return to evidence and update branches/assertions before compiling again.\n\n\
ACCEPTED DETERMINISTIC RELATION HINT (cardinality only):\n\
mode={relation}; confidence={relation_confidence}; refs={relation_refs:?}; repository_file_mode={repo_mode}; note={design_note}\n\n\
RAW FILE COUNT: {nfiles}\nRAW FILE SAMPLE (context/search hints only):\n{files}\n\n\
EVIDENCE INVENTORY:\n{evidence_block}\n\n\
PREVIOUS WORKSPACE STATE:\n{previous_json}\n\n\
TOOL/ACTION HISTORY:\n{action_json}\n\n\
VALIDATION HISTORY:\n{validation_json}\n\n\
This is agent turn {turn}. Return the COMPLETE updated workspace state, not a patch. Keep notes concise and decision-oriented.",
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
        turn = turn,
    )
}

async fn call_scientific_agent(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
    previous: &ScientificWorkspaceState,
    actions: &[EvidenceActionResult],
    validations: &[AgentValidationCycle],
    turn: usize,
) -> Result<ScientificWorkspaceState> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": scientific_agent_prompt(opts, evidence, previous, actions, validations, turn),
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

fn assertion_refs_are_field_relevant(
    evidence: &DatasetEvidence,
    field: &str,
    refs: &[String],
) -> bool {
    !refs.is_empty()
        && refs.iter().all(|id| {
            evidence
                .evidence
                .iter()
                .find(|item| item.id == id.as_str())
                .map(|item| evidence_relevant_to_field(field, item))
                .unwrap_or(false)
        })
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
    let mut seen_branch_ids = BTreeSet::new();
    state.branches.retain_mut(|branch| {
        branch.id = branch.id.trim().to_string();
        if branch.id.is_empty() || !seen_branch_ids.insert(branch.id.clone()) {
            return false;
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
        true
    });
    state.branches.truncate(24);

    let branch_ids = state
        .branches
        .iter()
        .map(|b| b.id.as_str())
        .collect::<BTreeSet<_>>();
    state.assertions.retain_mut(|assertion| {
        if !is_canonical_design_field(assertion.field.trim()) {
            return false;
        }
        assertion.field = assertion.field.trim().to_string();
        assertion.evidence_refs = valid_evidence_refs(evidence, &assertion.evidence_refs);
        if assertion.scope == "branch" && !branch_ids.contains(assertion.branch_id.as_str()) {
            assertion.scope = "unresolved".into();
            assertion.status = "unresolved".into();
            assertion.value.clear();
        }
        if matches!(assertion.scope.as_str(), "project" | "branch" | "row")
            && assertion.status == "supported"
            && !assertion_refs_are_field_relevant(
                evidence,
                &assertion.field,
                &assertion.evidence_refs,
            )
        {
            assertion.status = "hypothesis".into();
        }
        if assertion.status == "unresolved" || assertion.scope == "unresolved" {
            assertion.value.clear();
            assertion.scope = "unresolved".into();
        }
        true
    });
    state.assertions.truncate(96);

    let mut project_values: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for assertion in &state.assertions {
        if assertion.status == "supported" && assertion.scope == "project" {
            project_values
                .entry(assertion.field.clone())
                .or_default()
                .insert(assertion.value.trim().to_ascii_lowercase());
        }
    }
    let conflicting_fields = project_values
        .iter()
        .filter(|(_, values)| values.len() > 1)
        .map(|(field, _)| field.clone())
        .collect::<BTreeSet<_>>();
    for assertion in &mut state.assertions {
        if conflicting_fields.contains(&assertion.field) && assertion.scope == "project" {
            assertion.scope = "unresolved".into();
            assertion.status = "unresolved".into();
            assertion.value.clear();
        }
    }

    for action in &mut state.next_evidence_actions {
        action
            .target_fields
            .retain(|field| is_canonical_design_field(field));
        action.target_fields.sort();
        action.target_fields.dedup();
        for query in &mut action.queries {
            normalize_evidence_query(query);
        }
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

fn workspace_as_design_assessment(state: &ScientificWorkspaceState) -> DatasetDesignAssessment {
    let mut fields = BTreeSet::new();
    let mut field_scopes = Vec::new();
    for assertion in &state.assertions {
        if !fields.insert(assertion.field.clone()) {
            continue;
        }
        let relevant = state
            .assertions
            .iter()
            .filter(|a| a.field == assertion.field && a.status == "supported")
            .collect::<Vec<_>>();
        let scope = if relevant.iter().any(|a| a.scope == "row") {
            "row"
        } else if relevant.iter().any(|a| a.scope == "branch") {
            "group"
        } else if relevant.iter().any(|a| a.scope == "project") {
            "project"
        } else {
            "unresolved"
        };
        let refs = relevant
            .iter()
            .flat_map(|a| a.evidence_refs.iter().cloned())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        field_scopes.push(FieldScopeClaim {
            field: assertion.field.clone(),
            scope: scope.into(),
            evidence_refs: refs,
            confidence: "high".into(),
            reason: "scientific-agent workspace scope".into(),
            claim_origin: "model_explicit".into(),
        });
    }
    let candidate_groups = state
        .branches
        .iter()
        .map(|b| CandidateDesignGroup {
            id: b.id.clone(),
            description: b.label.clone(),
            status: if b.status == "supported" {
                "supported".into()
            } else {
                "search_hint".into()
            },
            source_basis: if b.status == "supported" {
                "source_evidence".into()
            } else {
                "filename_hint".into()
            },
            confidence: "high".into(),
            evidence_refs: b.evidence_refs.clone(),
            linked_raw_files: b.linked_raw_files.clone(),
            linkage_status: b.linkage_status.clone(),
        })
        .collect();
    DatasetDesignAssessment {
        agent_version: SCIENTIFIC_AGENT_HARNESS_VERSION.into(),
        design_homogeneous: state.branches.len() <= 1,
        relation_assessment: RelationAssessment {
            mode: state.relation.mode.clone(),
            scope: state.relation.scope.clone(),
            evidence_refs: state.relation.evidence_refs.clone(),
            confidence: state.relation.confidence.clone(),
            reason: state.relation.reason.clone(),
            claim_origin: "model_explicit".into(),
        },
        candidate_groups,
        field_scopes,
        conflicts: state.conflicts.clone(),
        missing_linkages: state.open_questions.clone(),
        terminal_status: "partial".into(),
        notes: state.notes.clone(),
        ..Default::default()
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

fn canonical_assertion_value(
    evidence: &DatasetEvidence,
    assertion: &AgentAssertion,
) -> Option<(String, Vec<String>)> {
    if assertion.status != "supported"
        || !assertion_refs_are_field_relevant(evidence, &assertion.field, &assertion.evidence_refs)
    {
        return None;
    }
    let subset = evidence_subset(evidence, &assertion.evidence_refs);
    match assertion.field.as_str() {
        "single_cell_isolation_method" => infer_isolation_method_scaffold(&subset),
        "proteomics_data_acquisition_method" => infer_acquisition_method_repair(&subset),
        _ => {
            let value = match canonical_reserved_alias(assertion.value.trim()) {
                Some(value) => value.to_string(),
                None => assertion.value.trim().to_string(),
            };
            if value.is_empty() || value == "not available" {
                None
            } else {
                Some((value, assertion.evidence_refs.clone()))
            }
        }
    }
}

fn branch_assertions_exist(state: &ScientificWorkspaceState, field: &str) -> bool {
    state.assertions.iter().any(|a| {
        a.field == field && a.status == "supported" && matches!(a.scope.as_str(), "branch" | "row")
    })
}

fn compile_workspace(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    explicit_mappings: &[ExplicitRowMapping],
) -> Result<CompiledWorkspace> {
    let mut proposal = SdrfProposal::default();
    let mut issues = Vec::new();
    let mut deterministic_repairs = Vec::new();

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

    let design = workspace_as_design_assessment(state);
    issues.extend(apply_deterministic_metadata_scaffold(
        &mut proposal,
        evidence,
        Some(&design),
    ));

    for assertion in &state.assertions {
        if assertion.scope != "project" || assertion.status != "supported" {
            continue;
        }
        if branch_assertions_exist(state, &assertion.field) {
            continue;
        }
        let Some((value, refs)) = canonical_assertion_value(evidence, assertion) else {
            continue;
        };
        if let Some(slot) = proposal_field_mut(&mut proposal, &assertion.field) {
            *slot = value;
            proposal.evidence_refs.insert(assertion.field.clone(), refs);
        }
    }

    issues.extend(apply_publication_compatibility_normalization(&mut proposal));
    if let Some(issue) = sanitize_nonindividual_semantic_proposal(&mut proposal) {
        deterministic_repairs.push(issue.code.clone());
        issues.push(issue);
    }
    issues.extend(repair_proposal_provenance(&mut proposal, evidence));
    validate_proposal_refs(&proposal, evidence)?;

    let (headers, mut rows, generation_mode) =
        draft_rows_with_explicit_mappings(&proposal, evidence, explicit_mappings)?;
    let header_index = headers
        .iter()
        .enumerate()
        .map(|(i, h)| (h.as_str(), i))
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
            for assertion in state.assertions.iter().filter(|a| {
                a.status == "supported" && a.scope == "branch" && a.branch_id == branch.id
            }) {
                let Some(header) = field_existing_header(&assertion.field) else {
                    continue;
                };
                let Some(&idx) = header_index.get(header) else {
                    continue;
                };
                let Some((value, _)) = canonical_assertion_value(evidence, assertion) else {
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
                            branch.id, assertion.field, value, raw
                        ),
                    });
                }
            }
        }
    }

    issues.extend(validate_annotation_draft(&headers, &rows, evidence, false));
    Ok(CompiledWorkspace {
        proposal,
        headers,
        rows,
        generation_mode,
        issues,
        deterministic_repairs,
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

fn trim_actions_to_budget(
    actions: &[EvidenceActionRequest],
    remaining: usize,
) -> Vec<EvidenceActionRequest> {
    let mut left = remaining;
    let mut out = Vec::new();
    for action in actions {
        if left == 0 {
            break;
        }
        if action.action == "ABSTAIN" {
            out.push(action.clone());
            left = left.saturating_sub(1);
            continue;
        }
        let mut action = action.clone();
        action.queries.truncate(left.min(8));
        if !action.queries.is_empty() {
            left = left.saturating_sub(action.queries.len());
            out.push(action);
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
    let mut attempted = BTreeSet::new();
    let mut compiled: Option<CompiledWorkspace> = None;
    let max_turns = opts.max_agent_turns.max(1);
    let max_actions = opts.max_tool_actions.max(1);
    let max_validator_cycles = opts.max_validator_cycles.max(1);

    for turn in 1..=max_turns {
        let next = call_scientific_agent(
            opts,
            &evidence,
            &state,
            &trace.evidence_action_results,
            &trace.validation_history,
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

    if compiled.is_none() {
        let compiled_now = compile_workspace(&evidence, &state, &explicit_mappings)?;
        write_sdrf(&draft_path, &compiled_now.headers, &compiled_now.rows)?;
        write_validation_review(&review_path, &compiled_now.issues)?;
        trace.validator_cycles_completed += 1;
        trace.validation_history.push(validation_cycle(
            trace.validator_cycles_completed,
            &compiled_now.issues,
        ));
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
        "open_questions": state.open_questions.clone(),
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
        let assertion = AgentAssertion {
            field: "single_cell_isolation_method".into(),
            value: "capillary loading".into(),
            scope: "project".into(),
            status: "supported".into(),
            evidence_refs: vec!["E0001".into()],
            ..Default::default()
        };
        let (value, refs) = canonical_assertion_value(&evidence, &assertion).unwrap();
        assert_eq!(value, "manual picking");
        assert_eq!(refs, vec!["E0001"]);
    }

    #[test]
    fn project_value_is_not_broadcast_when_branch_assertion_exists() {
        let state = ScientificWorkspaceState {
            assertions: vec![
                AgentAssertion {
                    field: "organism".into(),
                    value: "Homo sapiens".into(),
                    scope: "project".into(),
                    status: "supported".into(),
                    ..Default::default()
                },
                AgentAssertion {
                    field: "organism".into(),
                    value: "Xenopus laevis".into(),
                    scope: "branch".into(),
                    branch_id: "xeno".into(),
                    status: "supported".into(),
                    ..Default::default()
                },
            ],
            ..Default::default()
        };
        assert!(branch_assertions_exist(&state, "organism"));
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
        let actions = vec![EvidenceActionRequest {
            action: "SEARCH_PUBLICATION".into(),
            reason: "test".into(),
            target_fields: vec!["organism".into()],
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
    fn unsupported_assertion_without_field_relevant_refs_becomes_hypothesis() {
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
            assertions: vec![AgentAssertion {
                field: "organism".into(),
                value: "Homo sapiens".into(),
                scope: "project".into(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                ..Default::default()
            }],
            next_step: "compile".into(),
            ..Default::default()
        };
        normalize_workspace_state(&evidence, &mut state, 1);
        assert_eq!(state.assertions[0].status, "hypothesis");
    }
}
