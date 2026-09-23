use super::*;

pub const SCIENTIFIC_AGENT_HARNESS_VERSION: &str = "pride-scp-scientific-workspace-agent-v1.3";
const SCIENTIFIC_AGENT_TASK_EVIDENCE_LIMIT: usize = 20;
const SCIENTIFIC_AGENT_CONTEXT_READ_RADIUS: usize = 6000;
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
    #[serde(rename = "observed_value", alias = "value")]
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
struct ScientificClaimUpsert {
    concept_type: String,
    value: String,
    scope: String,
    branch_id: String,
    status: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
    /// Empty means this is not an explicit supersession. When non-empty it
    /// must match the currently active value for the stable claim identity.
    supersedes_value: String,
}

impl ScientificClaimUpsert {
    fn as_claim(&self) -> ScientificClaim {
        ScientificClaim {
            concept_type: self.concept_type.clone(),
            value: self.value.clone(),
            scope: self.scope.clone(),
            branch_id: self.branch_id.clone(),
            status: self.status.clone(),
            evidence_refs: self.evidence_refs.clone(),
            confidence: self.confidence.clone(),
            reason: self.reason.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificClaimRetraction {
    concept_type: String,
    scope: String,
    branch_id: String,
    /// Empty means retract the current value for this identity. A non-empty
    /// value protects against retracting a claim that has already changed.
    expected_value: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct WorkspaceConflict {
    concept_type: String,
    scope: String,
    branch_id: String,
    existing_value: String,
    proposed_value: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    reason: String,
    #[serde(default)]
    turn: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct WorkspaceConflictResolution {
    concept_type: String,
    scope: String,
    branch_id: String,
    resolved_value: String,
    status: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct WorkspaceDelta {
    turn: usize,
    task_id: String,
    task_status: String,
    #[serde(default)]
    branch_upserts: Vec<AgentBranch>,
    #[serde(default)]
    claim_upserts: Vec<ScientificClaimUpsert>,
    #[serde(default)]
    claim_retractions: Vec<ScientificClaimRetraction>,
    #[serde(default)]
    open_question_additions: Vec<String>,
    #[serde(default)]
    open_question_resolutions: Vec<String>,
    #[serde(default)]
    conflict_additions: Vec<WorkspaceConflict>,
    #[serde(default)]
    conflict_resolutions: Vec<WorkspaceConflictResolution>,
    #[serde(default)]
    next_evidence_actions: Vec<AgentEvidenceAction>,
    next_step: String,
    notes: String,
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
    #[serde(rename = "observed_value", alias = "proposed_value")]
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
    #[serde(default)]
    evidence_refs: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificObservationUpsert {
    concept_type: String,
    observed_value: String,
    scope: String,
    branch_id: String,
    status: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
    /// Empty means the observation is not explicitly replacing a prior
    /// observation for the same stable identity. This compares observations,
    /// never Rust-owned canonical SDRF vocabulary values.
    supersedes_observed_value: String,
}

impl ScientificObservationUpsert {
    fn as_claim_upsert(&self) -> ScientificClaimUpsert {
        ScientificClaimUpsert {
            concept_type: self.concept_type.clone(),
            value: self.observed_value.clone(),
            scope: self.scope.clone(),
            branch_id: self.branch_id.clone(),
            status: self.status.clone(),
            evidence_refs: self.evidence_refs.clone(),
            confidence: self.confidence.clone(),
            reason: self.reason.clone(),
            supersedes_value: self.supersedes_observed_value.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificObservationRetraction {
    concept_type: String,
    scope: String,
    branch_id: String,
    expected_observed_value: String,
    reason: String,
}

impl ScientificObservationRetraction {
    fn as_claim_retraction(&self) -> ScientificClaimRetraction {
        ScientificClaimRetraction {
            concept_type: self.concept_type.clone(),
            scope: self.scope.clone(),
            branch_id: self.branch_id.clone(),
            expected_value: self.expected_observed_value.clone(),
            reason: self.reason.clone(),
        }
    }
}

pub const SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_MODE: &str = "study_graph_v2_stage1";
pub const SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_VERSION: &str =
    "pride-scp-scientific-workspace-agent-v2-stage1";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct StudyGraphBranchProposal {
    label: String,
    biological_material: String,
    experimental_role: String,
    isolation_context: String,
    acquisition_context: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    #[serde(default)]
    linked_raw_files: Vec<String>,
    linkage_status: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct StudyGraphProposal {
    decision: String,
    #[serde(default)]
    branches: Vec<StudyGraphBranchProposal>,
    #[serde(default)]
    open_questions: Vec<String>,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AcceptedStudyGraphBranch {
    id: String,
    label: String,
    biological_material: String,
    experimental_role: String,
    isolation_context: String,
    acquisition_context: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    #[serde(default)]
    linked_raw_files: Vec<String>,
    linkage_status: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct StudyGraphRejectedBranch {
    label: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct StudyGraphRemovedRawLink {
    branch_label: String,
    raw_file: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct StudyGraphAcceptance {
    harness_version: String,
    accession: String,
    status: String,
    #[serde(default)]
    branches: Vec<AcceptedStudyGraphBranch>,
    #[serde(default)]
    open_questions: Vec<String>,
    #[serde(default)]
    rejected_branches: Vec<StudyGraphRejectedBranch>,
    #[serde(default)]
    removed_raw_links: Vec<StudyGraphRemovedRawLink>,
    reason: String,
    model_calls: usize,
}

pub const SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_MODE: &str = "study_factor_graph_v2_stage1";
pub const SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION: &str =
    "pride-scp-scientific-workspace-agent-v2-factor-stage1";

const FACTOR_GRAPH_STAGE1_DECISION_CONTRACT: &str = "\
DECISION CONTRACT:\n\
- Return propose_graph whenever trusted evidence supports a safe conceptual graph with at least one Material node and one Experimental Regime node. Acquisition nodes, exact RAW links, and complete answers to every open question are NOT prerequisites for propose_graph.\n\
- open_questions may coexist with propose_graph. Use human_review only when no source-grounded conceptual Material + Regime graph can be expressed safely after preserving the distinctions that trusted evidence actually establishes.\n\
- Missing exact RAW-to-factor linkage is never, by itself, a reason for human_review. Do not treat repository filenames or absent file mappings as a requirement for conceptual graph construction.\n\
- Distinct Material nodes MAY share the same source-supported Regime and/or Acquisition node. A shared workflow does not collapse biological material identity, and you must not duplicate a Regime merely because multiple materials use it.\n\
- Preserve source-defined biological source cohorts or states that are central to the study and cannot be represented by the Regime or Acquisition axes. For example, explicitly distinguished GV, IVM, and IVO oocyte maturation states should remain distinct Material nodes while sharing a common preparation Regime when the source says the workflow is shared. Do not split Material nodes merely for technical processing conditions.\n\
- NODE-TEXT SOURCE FIDELITY IS STRICT: every substantive organism, material, biological state, isolation/loading method, acquisition method, platform, and named technology written into a node's core fields must be explicitly stated by, or directly entailed by, that node's cited E#### evidence. Never add a method, platform, acronym, or modality from model memory, filename wording, another dataset, or an uncited inference.\n\
- When evidence supports a narrower statement than you initially considered, use the narrower source-faithful wording. If acquisition details are not safely supported, omit the Acquisition node rather than inventing them; a supported Material + Regime graph may still be proposed.\n\
- The reason field MUST agree with decision. If reason says the graph is valid, safe, constructible, source-grounded, or that no conflicting evidence prevents construction, decision must be propose_graph unless the reason also identifies a concrete conceptual Material/Regime ambiguity that makes the graph unsafe.\n";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FactorMaterialProposal {
    local_id: String,
    label: String,
    organism: String,
    biological_material: String,
    experimental_role: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FactorRegimeProposal {
    local_id: String,
    label: String,
    experimental_role: String,
    isolation_or_loading_method: String,
    input_or_cell_count_regime: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FactorAcquisitionProposal {
    local_id: String,
    label: String,
    acquisition_method_or_platform: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FactorRelationProposal {
    source_id: String,
    relation_type: String,
    target_id: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FactorRawLinkProposal {
    node_id: String,
    raw_file: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct StudyFactorGraphProposal {
    decision: String,
    #[serde(default)]
    materials: Vec<FactorMaterialProposal>,
    #[serde(default)]
    regimes: Vec<FactorRegimeProposal>,
    #[serde(default)]
    acquisitions: Vec<FactorAcquisitionProposal>,
    #[serde(default)]
    relations: Vec<FactorRelationProposal>,
    #[serde(default)]
    raw_links: Vec<FactorRawLinkProposal>,
    #[serde(default)]
    open_questions: Vec<String>,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AcceptedFactorMaterial {
    id: String,
    label: String,
    organism: String,
    biological_material: String,
    experimental_role: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AcceptedFactorRegime {
    id: String,
    label: String,
    experimental_role: String,
    isolation_or_loading_method: String,
    input_or_cell_count_regime: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AcceptedFactorAcquisition {
    id: String,
    label: String,
    acquisition_method_or_platform: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AcceptedFactorRelation {
    source_id: String,
    relation_type: String,
    target_id: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AcceptedFactorRawLink {
    node_id: String,
    raw_file: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct FactorGraphRejectedItem {
    item_type: String,
    local_id: String,
    label: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct FactorGraphRemovedRawLink {
    node_id: String,
    raw_file: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct StudyFactorGraphAcceptance {
    harness_version: String,
    accession: String,
    status: String,
    #[serde(default)]
    materials: Vec<AcceptedFactorMaterial>,
    #[serde(default)]
    regimes: Vec<AcceptedFactorRegime>,
    #[serde(default)]
    acquisitions: Vec<AcceptedFactorAcquisition>,
    #[serde(default)]
    relations: Vec<AcceptedFactorRelation>,
    #[serde(default)]
    raw_links: Vec<AcceptedFactorRawLink>,
    #[serde(default)]
    open_questions: Vec<String>,
    #[serde(default)]
    rejected_items: Vec<FactorGraphRejectedItem>,
    #[serde(default)]
    removed_raw_links: Vec<FactorGraphRemovedRawLink>,
    reason: String,
    model_calls: usize,
}

pub const SCIENTIFIC_AGENT_FACTOR_PHASE_B_MODE: &str = "study_factor_graph_v2_phase_b";
pub const SCIENTIFIC_AGENT_FACTOR_PHASE_B_VERSION: &str =
    "pride-scp-scientific-workspace-agent-v2-factor-phase-b";
pub const SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_MODE: &str =
    "study_factor_graph_v2_deterministic_bridge";
pub const SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_VERSION: &str =
    "pride-scp-scientific-workspace-agent-v2-factor-deterministic-bridge";
pub const SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_MODE: &str =
    "study_factor_graph_v2_canonicalization_hardened";
pub const SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION: &str =
    "pride-scp-scientific-workspace-agent-v2-factor-canonicalization-hardened";
pub const SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_MODE: &str =
    "study_factor_graph_v2_semantic_fidelity_hardened";
pub const SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION: &str =
    "pride-scp-scientific-workspace-agent-v2-factor-semantic-fidelity-hardened";
pub const SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_MODE: &str =
    "study_factor_graph_v2_row_role_hardened";
pub const SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION: &str =
    "pride-scp-scientific-workspace-agent-v2-factor-row-role-hardened";
const SCIENTIFIC_AGENT_FACTOR_GRAPH_ROOT_ENV: &str = "PRIDE_SCP_FACTOR_GRAPH_STAGE1_ROOT";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FactorObservationProposal {
    target_factor_id: String,
    concept_type: String,
    observed_value: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FactorObservationBundleProposal {
    decision: String,
    #[serde(default)]
    observations: Vec<FactorObservationProposal>,
    #[serde(default)]
    open_questions: Vec<String>,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct AcceptedFactorObservation {
    target_factor_id: String,
    target_factor_kind: String,
    concept_type: String,
    observed_value: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    projection_scope: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct RejectedFactorObservation {
    target_factor_id: String,
    concept_type: String,
    observed_value: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct FactorPhaseBAcceptance {
    harness_version: String,
    accession: String,
    status: String,
    #[serde(default)]
    observations: Vec<AcceptedFactorObservation>,
    #[serde(default)]
    rejected_observations: Vec<RejectedFactorObservation>,
    #[serde(default)]
    open_questions: Vec<String>,
    reason: String,
    model_calls: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
enum AgentCommand {
    ReadEvidence {
        task_id: String,
        evidence_refs: Vec<String>,
        reason: String,
    },
    SearchEvidence {
        task_id: String,
        actions: Vec<AgentEvidenceAction>,
        reason: String,
    },
    EditStudyStructure {
        task_id: String,
        branch_upserts: Vec<AgentBranch>,
        #[serde(default)]
        open_question_additions: Vec<String>,
        #[serde(default)]
        open_question_resolutions: Vec<String>,
        notes: String,
    },
    EditScientificObservation {
        task_id: String,
        observation_upserts: Vec<ScientificObservationUpsert>,
        #[serde(default)]
        observation_retractions: Vec<ScientificObservationRetraction>,
        #[serde(default)]
        open_question_additions: Vec<String>,
        #[serde(default)]
        open_question_resolutions: Vec<String>,
        notes: String,
    },
    Escalate {
        task_id: String,
        reason: String,
    },
}

#[derive(Debug, Clone)]
struct WorkspaceEditPayload {
    task_id: String,
    branch_upserts: Vec<AgentBranch>,
    observation_upserts: Vec<ScientificObservationUpsert>,
    observation_retractions: Vec<ScientificObservationRetraction>,
    open_question_additions: Vec<String>,
    open_question_resolutions: Vec<String>,
    notes: String,
}

impl AgentCommand {
    fn task_id(&self) -> &str {
        match self {
            AgentCommand::ReadEvidence { task_id, .. }
            | AgentCommand::SearchEvidence { task_id, .. }
            | AgentCommand::EditStudyStructure { task_id, .. }
            | AgentCommand::EditScientificObservation { task_id, .. }
            | AgentCommand::Escalate { task_id, .. } => task_id,
        }
    }

    fn workspace_edit_payload(&self) -> Option<WorkspaceEditPayload> {
        match self {
            AgentCommand::EditStudyStructure {
                task_id,
                branch_upserts,
                open_question_additions,
                open_question_resolutions,
                notes,
            } => Some(WorkspaceEditPayload {
                task_id: task_id.clone(),
                branch_upserts: branch_upserts.clone(),
                observation_upserts: Vec::new(),
                observation_retractions: Vec::new(),
                open_question_additions: open_question_additions.clone(),
                open_question_resolutions: open_question_resolutions.clone(),
                notes: notes.clone(),
            }),
            AgentCommand::EditScientificObservation {
                task_id,
                observation_upserts,
                observation_retractions,
                open_question_additions,
                open_question_resolutions,
                notes,
            } => Some(WorkspaceEditPayload {
                task_id: task_id.clone(),
                branch_upserts: Vec::new(),
                observation_upserts: observation_upserts.clone(),
                observation_retractions: observation_retractions.clone(),
                open_question_additions: open_question_additions.clone(),
                open_question_resolutions: open_question_resolutions.clone(),
                notes: notes.clone(),
            }),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificTask {
    id: String,
    concept_type: String,
    sdrf_field: String,
    status: String,
    #[serde(default)]
    error_codes: Vec<String>,
    error_count: usize,
    #[serde(default)]
    representative_rows: Vec<usize>,
    #[serde(default)]
    representative_messages: Vec<String>,
    objective: String,
    #[serde(default)]
    evidence_candidates: Vec<String>,
    #[serde(default)]
    evidence_reads: Vec<String>,
    #[serde(default)]
    decision_required: bool,
    #[serde(default)]
    search_blocked: bool,
    attempts: usize,
    notes: String,
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
    conflicts: Vec<WorkspaceConflict>,
    #[serde(default)]
    tasks: Vec<ScientificTask>,
    active_task_id: String,
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
struct AdjudicationSnapshot {
    turn: usize,
    phase: String,
    evidence_items: usize,
    #[serde(default)]
    records: Vec<ClaimAdjudicationRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ScientificAgentTrace {
    harness_version: String,
    accession: String,
    #[serde(default)]
    states: Vec<ScientificWorkspaceState>,
    #[serde(default)]
    commands: Vec<AgentCommand>,
    #[serde(default)]
    deltas: Vec<WorkspaceDelta>,
    #[serde(default)]
    adjudication_history: Vec<AdjudicationSnapshot>,
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

fn scientific_agent_schema(
    allow_read_evidence: bool,
    allow_search_evidence: bool,
    study_structure_task: bool,
) -> Value {
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
    let branch = json!({
        "type":"object",
        "description":"A source-supported conceptual study branch. Exact RAW linkage is optional: when the branch is scientifically supported but exact RAW basenames are not source-linked, use linked_raw_files=[] and linkage_status='unresolved'.",
        "properties":{
            "id":{"type":"string","maxLength":80},
            "label":{"type":"string","maxLength":240},
            "status":{"type":"string","enum":["supported","hypothesis","rejected"]},
            "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"minItems":1,"maxItems":16},
            "linked_raw_files":{"type":"array","items":{"type":"string","maxLength":300},"maxItems":128,"description":"Only exact RAW basenames explicitly linked by trusted source evidence. Use [] when linkage is unresolved."},
            "linkage_status":{"type":"string","enum":["supported","partial","unresolved"],"description":"Use unresolved when the conceptual branch is supported but exact RAW linkage is absent."},
            "notes":{"type":"string","maxLength":700}
        },
        "required":["id","label","status","evidence_refs","linked_raw_files","linkage_status","notes"],
        "additionalProperties":false
    });
    let observation = json!({
        "type":"object",
        "description":"A source-faithful scientific observation. Record what the trusted source explicitly says or names; Rust, not the model, owns SDRF canonicalization.",
        "properties":{
            "concept_type":{"type":"string","enum":concepts.clone()},
            "observed_value":{"type":"string","maxLength":800},
            "scope":{"type":"string","enum":["project","branch","row","unresolved"]},
            "branch_id":{"type":"string","maxLength":80},
            "status":{"type":"string","enum":["supported","hypothesis","unresolved","rejected"]},
            "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"minItems":1,"maxItems":16},
            "confidence":{"type":"string","enum":["high","medium","low"]},
            "reason":{"type":"string","maxLength":800},
            "supersedes_observed_value":{"type":"string","maxLength":800}
        },
        "required":["concept_type","observed_value","scope","branch_id","status","evidence_refs","confidence","reason","supersedes_observed_value"],
        "additionalProperties":false
    });
    let observation_retraction = json!({
        "type":"object",
        "properties":{
            "concept_type":{"type":"string","enum":concepts.clone()},
            "scope":{"type":"string","enum":["project","branch","row","unresolved"]},
            "branch_id":{"type":"string","maxLength":80},
            "expected_observed_value":{"type":"string","maxLength":800},
            "reason":{"type":"string","maxLength":600}
        },
        "required":["concept_type","scope","branch_id","expected_observed_value","reason"],
        "additionalProperties":false
    });
    let search_action = json!({
        "type":"object",
        "description":"Search only registered/trusted publication, supplement, structured-design, repository metadata, exact RAW-name, knowledge-graph, or conflict-evidence sources. This is not arbitrary web browsing.",
        "properties":{
            "action":{"type":"string","enum":["SEARCH_PUBLICATION","SEARCH_SUPPLEMENT","SEARCH_STRUCTURED_DESIGN","SEARCH_REPOSITORY_METADATA","SEARCH_EXACT_RAW_NAME","EXPAND_EVIDENCE_CONTEXT","LOOKUP_KG_TERM","COMPARE_CONFLICTING_EVIDENCE"]},
            "reason":{"type":"string","maxLength":500},
            "target_concepts":{"type":"array","items":{"type":"string","enum":concepts.clone()},"maxItems":8},
            "queries":{"type":"array","items":query,"minItems":1,"maxItems":8},
            "evidence_refs":{"type":"array","maxItems":0}
        },
        "required":["action","reason","target_concepts","queries","evidence_refs"],
        "additionalProperties":false
    });
    let read_command = json!({
        "type":"object",
        "properties":{
            "command":{"type":"string","enum":["read_evidence"]},
            "task_id":{"type":"string","maxLength":100},
            "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"minItems":1,"maxItems":4},
            "reason":{"type":"string","maxLength":600}
        },
        "required":["command","task_id","evidence_refs","reason"],
        "additionalProperties":false
    });
    let search_command = json!({
        "type":"object",
        "properties":{
            "command":{"type":"string","enum":["search_evidence"]},
            "task_id":{"type":"string","maxLength":100},
            "actions":{"type":"array","items":search_action,"minItems":1,"maxItems":4},
            "reason":{"type":"string","maxLength":600}
        },
        "required":["command","task_id","actions","reason"],
        "additionalProperties":false
    });
    let study_structure_edit_command = json!({
        "type":"object",
        "description":"Commit source-supported conceptual study branches for task:study_structure. This command CAN create branches even when exact RAW linkage is unresolved.",
        "properties":{
            "command":{"type":"string","enum":["edit_study_structure"]},
            "task_id":{"type":"string","enum":["task:study_structure"]},
            "branch_upserts":{"type":"array","items":branch,"minItems":1,"maxItems":24},
            "open_question_additions":{"type":"array","items":{"type":"string","maxLength":500},"maxItems":24},
            "open_question_resolutions":{"type":"array","items":{"type":"string","maxLength":500},"maxItems":24},
            "notes":{"type":"string","maxLength":1600}
        },
        "required":["command","task_id","branch_upserts","open_question_additions","open_question_resolutions","notes"],
        "additionalProperties":false
    });
    let observation_edit_command = json!({
        "type":"object",
        "description":"Commit at least one source-faithful scientific observation for the active validator-backed field task. Do not choose SDRF vocabulary; Rust canonicalizes after this edit.",
        "properties":{
            "command":{"type":"string","enum":["edit_scientific_observation"]},
            "task_id":{"type":"string","maxLength":100},
            "observation_upserts":{"type":"array","items":observation,"minItems":1,"maxItems":48},
            "observation_retractions":{"type":"array","items":observation_retraction,"maxItems":24},
            "open_question_additions":{"type":"array","items":{"type":"string","maxLength":500},"maxItems":24},
            "open_question_resolutions":{"type":"array","items":{"type":"string","maxLength":500},"maxItems":24},
            "notes":{"type":"string","maxLength":1600}
        },
        "required":["command","task_id","observation_upserts","observation_retractions","open_question_additions","open_question_resolutions","notes"],
        "additionalProperties":false
    });
    let escalate_command = json!({
        "type":"object",
        "properties":{
            "command":{"type":"string","enum":["escalate"]},
            "task_id":{"type":"string","maxLength":100},
            "reason":{"type":"string","maxLength":1000}
        },
        "required":["command","task_id","reason"],
        "additionalProperties":false
    });
    let mut commands = Vec::new();
    if allow_read_evidence {
        commands.push(read_command);
    }
    if allow_search_evidence {
        commands.push(search_command);
    }
    if study_structure_task {
        commands.push(study_structure_edit_command);
    } else {
        commands.push(observation_edit_command);
    }
    commands.push(escalate_command);
    json!({"oneOf": commands})
}

fn concept_for_repair_field(field: &str) -> Option<&'static str> {
    match field {
        "single_cell_isolation_method" => Some("isolation_method"),
        "proteomics_data_acquisition_method" => Some("acquisition_mode"),
        "organism" => Some("organism"),
        "individual" => Some("individual"),
        _ => None,
    }
}

fn task_relevance_score(field: &str, item: &EvidenceItem) -> usize {
    let mut score = if evidence_relevant_to_field(field, item) {
        10
    } else {
        0
    };
    let hay = format!("{} {}", item.source_label, item.text).to_ascii_lowercase();
    let direct_terms: &[&str] = match field {
        "study_structure" => &[
            "single-cell",
            "single cell",
            "organism",
            "cell line",
            "hela",
            "xenopus",
            "mouse",
            "human",
            "control",
            "treatment",
            "condition",
            "replicate",
            "data-independent",
            "data-dependent",
            "dia",
            "dda",
            "isolation",
            "microwell",
            "microfluid",
            "aspirat",
            "hydrodynamic",
        ],
        "single_cell_isolation_method" => &[
            "hydrodynamic",
            "on-capillary",
            "on capillary",
            "capillary lysis",
            "esi injection",
            "single-cell injection",
            "single cell injection",
            "microinjection",
            "micropipette",
            "microaspirat",
            "aspirat",
            "manual pick",
            "manual isolat",
            "microfluid",
            "microwell",
            "nanowell",
            "cellenone",
            "facs",
            "flow cytometry",
            "laser capture",
            "evdisco",
        ],
        "proteomics_data_acquisition_method" => &[
            "data-independent",
            "data independent",
            "dia-pasef",
            "diapasef",
            "data-dependent",
            "data dependent",
            "dda",
            "dia",
        ],
        "organism" => &[
            "homo sapiens",
            "mus musculus",
            "xenopus",
            "danio rerio",
            "species",
            "organism",
        ],
        "individual" => &["donor", "patient", "subject", "individual", "animal id"],
        _ => &[],
    };
    for term in direct_terms {
        if hay.contains(term) {
            score += 8;
        }
    }
    let source_kind = item.source_kind.to_ascii_lowercase();
    if source_kind.contains("publication") || source_kind.contains("manuscript") {
        score += 3;
    }
    if source_kind.contains("supplement") || source_kind.contains("annotation") {
        score += 2;
    }
    score
}

fn task_evidence_candidates(evidence: &DatasetEvidence, field: &str, limit: usize) -> Vec<String> {
    let mut ranked = evidence
        .evidence
        .iter()
        .filter_map(|item| {
            let score = task_relevance_score(field, item);
            (score > 0).then_some((score, item.id.clone()))
        })
        .collect::<Vec<_>>();
    ranked.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    ranked.dedup_by(|a, b| a.1 == b.1);
    ranked.into_iter().take(limit).map(|(_, id)| id).collect()
}

fn build_scientific_tasks(
    evidence: &DatasetEvidence,
    issues: &[ValidationIssue],
) -> Vec<ScientificTask> {
    let mut tasks = Vec::new();
    if !issues.is_empty() {
        tasks.push(ScientificTask {
            id: "task:study_structure".into(),
            concept_type: "study_structure".into(),
            sdrf_field: "study_structure".into(),
            status: "open".into(),
            error_codes: vec!["scientific_study_structure_review".into()],
            error_count: 1,
            representative_rows: Vec::new(),
            representative_messages: Vec::new(),
            objective: "Establish a source-grounded study model before field repair: biological/experimental branches, acquisition cardinality, field scope, and only source-supported RAW linkage. Preserve unresolved linkage rather than infer biological identity from filenames.".into(),
            evidence_candidates: task_evidence_candidates(
                evidence,
                "study_structure",
                SCIENTIFIC_AGENT_TASK_EVIDENCE_LIMIT,
            ),
            evidence_reads: Vec::new(),
            decision_required: false,
            search_blocked: false,
            attempts: 0,
            notes: String::new(),
        });
    }
    tasks.extend(
        cluster_validator_repair_tasks(issues)
            .into_iter()
            .filter_map(|task| {
                let concept_type = concept_for_repair_field(&task.field)?.to_string();
                let objective = format!(
                    "Resolve {} from trusted source evidence without guessing; {} current validator error(s)",
                    task.field, task.error_count
                );
                Some(ScientificTask {
                    id: format!("task:{}", task.field),
                    concept_type,
                    sdrf_field: task.field.clone(),
                    status: "open".into(),
                    error_codes: task.error_codes,
                    error_count: task.error_count,
                    representative_rows: task.representative_rows,
                    representative_messages: task.representative_messages,
                    objective,
                    evidence_candidates: task_evidence_candidates(
                        evidence,
                        &task.field,
                        SCIENTIFIC_AGENT_TASK_EVIDENCE_LIMIT,
                    ),
                    evidence_reads: Vec::new(),
                    decision_required: false,
                    search_blocked: false,
                    attempts: 0,
                    notes: String::new(),
                })
            }),
    );
    tasks
}

fn task_is_terminal(task: &ScientificTask) -> bool {
    matches!(task.status.as_str(), "resolved" | "human_review")
}

fn ensure_active_task(state: &mut ScientificWorkspaceState) {
    let active_still_valid = state.tasks.iter().any(|task| {
        task.id == state.active_task_id && !task_is_terminal(task) && task.error_count > 0
    });
    if active_still_valid {
        return;
    }
    state.active_task_id = state
        .tasks
        .iter()
        .find(|task| !task_is_terminal(task) && task.error_count > 0)
        .map(|task| task.id.clone())
        .unwrap_or_default();
    for task in &mut state.tasks {
        if task.id == state.active_task_id && task.status == "open" {
            task.status = "investigating".into();
        }
    }
}

fn refresh_scientific_tasks(
    evidence: &DatasetEvidence,
    state: &mut ScientificWorkspaceState,
    issues: &[ValidationIssue],
) {
    if state.tasks.is_empty() {
        state.tasks = build_scientific_tasks(evidence, issues);
        ensure_active_task(state);
        return;
    }

    let current = cluster_validator_repair_tasks(issues)
        .into_iter()
        .map(|task| (task.field.clone(), task))
        .collect::<BTreeMap<_, _>>();
    for task in &mut state.tasks {
        if task.sdrf_field == "study_structure" {
            task.evidence_candidates = task_evidence_candidates(
                evidence,
                "study_structure",
                SCIENTIFIC_AGENT_TASK_EVIDENCE_LIMIT,
            );
            continue;
        }
        if let Some(now) = current.get(&task.sdrf_field) {
            task.error_codes = now.error_codes.clone();
            task.error_count = now.error_count;
            task.representative_rows = now.representative_rows.clone();
            task.representative_messages = now.representative_messages.clone();
            let mut candidates = task_evidence_candidates(
                evidence,
                &task.sdrf_field,
                SCIENTIFIC_AGENT_TASK_EVIDENCE_LIMIT,
            );
            let mut seen = candidates.iter().cloned().collect::<BTreeSet<_>>();
            for read in &task.evidence_reads {
                if seen.insert(read.clone()) {
                    candidates.push(read.clone());
                }
            }
            task.evidence_candidates = candidates;
            if task.status == "resolved" {
                task.status = "open".into();
            }
        } else {
            task.error_count = 0;
            task.status = "resolved".into();
        }
    }

    for new_task in build_scientific_tasks(evidence, issues) {
        if !state.tasks.iter().any(|task| task.id == new_task.id) {
            state.tasks.push(new_task);
        }
    }
    state.tasks.sort_by(|a, b| {
        b.error_count
            .cmp(&a.error_count)
            .then_with(|| a.id.cmp(&b.id))
    });
    ensure_active_task(state);
}

fn active_task<'a>(state: &'a ScientificWorkspaceState) -> Option<&'a ScientificTask> {
    state
        .tasks
        .iter()
        .find(|task| task.id == state.active_task_id)
}

fn mark_active_task_human_review(state: &mut ScientificWorkspaceState, note: &str) {
    if let Some(task) = state
        .tasks
        .iter_mut()
        .find(|task| task.id == state.active_task_id)
    {
        task.status = "human_review".into();
        if !note.trim().is_empty() {
            task.notes = note.trim().to_string();
        }
    }
    state.active_task_id.clear();
    ensure_active_task(state);
}

fn task_board_block(state: &ScientificWorkspaceState) -> String {
    if state.tasks.is_empty() {
        return "no unresolved scientific tasks were derived from validation".into();
    }
    state
        .tasks
        .iter()
        .map(|task| {
            format!(
                "- {} status={} concept={} errors={} codes={:?} candidates={} reads={} decision_required={} search_blocked={} attempts={} objective={}",
                task.id,
                task.status,
                task.concept_type,
                task.error_count,
                task.error_codes,
                task.evidence_candidates.len(),
                task.evidence_reads.len(),
                task.decision_required,
                task.search_blocked,
                task.attempts,
                task.objective
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn task_evidence_block(evidence: &DatasetEvidence, task: Option<&ScientificTask>) -> String {
    let Some(task) = task else {
        return "none".into();
    };
    let read_refs = task.evidence_reads.iter().cloned().collect::<BTreeSet<_>>();
    let mut refs = task.evidence_reads.clone();
    let mut seen = refs.iter().cloned().collect::<BTreeSet<_>>();
    for candidate in &task.evidence_candidates {
        if seen.insert(candidate.clone()) {
            refs.push(candidate.clone());
        }
    }
    refs.into_iter()
        .filter_map(|id| {
            let item = evidence.evidence.iter().find(|item| item.id == id)?;
            let read = read_refs.contains(&id) || item.source_kind == "agent_read_context";
            let max_chars = if read { 5000 } else { 1400 };
            let text = item.text.replace('\0', " ");
            let text = if text.chars().count() > max_chars {
                text.chars().take(max_chars).collect::<String>() + "..."
            } else {
                text
            };
            Some(format!(
                "{} [{}:{}] {}{}",
                item.id,
                item.source_kind,
                item.source_label,
                if read { "[READ] " } else { "[CANDIDATE] " },
                text
            ))
        })
        .take(SCIENTIFIC_AGENT_TASK_EVIDENCE_LIMIT)
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn write_workspace_notebook(
    workspace_dir: &Path,
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    adjudications: &[ClaimAdjudicationRecord],
    validations: &[AgentValidationCycle],
) -> Result<()> {
    let active = active_task(state);
    let active_json = serde_json::to_string_pretty(&active).unwrap_or_else(|_| "null".into());
    let claims_json = serde_json::to_string_pretty(&state.claims).unwrap_or_else(|_| "[]".into());
    let adjudications_json =
        serde_json::to_string_pretty(adjudications).unwrap_or_else(|_| "[]".into());
    let validation_json = validations
        .last()
        .map(|cycle| serde_json::to_string_pretty(cycle).unwrap_or_else(|_| "{}".into()))
        .unwrap_or_else(|| "none".into());
    let notebook = format!(
        "# PRIDE-SCP scientific workspace: {}\n\n## Task board\n{}\n\n## Active task\n{}\n\n## Focused evidence\n{}\n\n## Scientific observations (source-faithful; not canonical SDRF values)\n{}\n\n## Rust adjudications\n{}\n\n## Latest validation\n{}\n",
        state.accession,
        task_board_block(state),
        active_json,
        task_evidence_block(evidence, active),
        claims_json,
        adjudications_json,
        validation_json,
    );
    fs::write(workspace_dir.join("NOTEBOOK.md"), notebook)?;
    fs::write(
        workspace_dir.join("TASKS.json"),
        serde_json::to_string_pretty(&state.tasks)?,
    )?;
    Ok(())
}

fn scientific_agent_prompt(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
    workspace: &ScientificWorkspaceState,
    actions: &[EvidenceActionResult],
    validations: &[AgentValidationCycle],
    changed_harness_feedback: &[String],
    turn: usize,
) -> String {
    let files = evidence
        .raw_files
        .iter()
        .take(opts.max_files_in_prompt.min(32))
        .map(|f| format!("- {}", f.file_name))
        .collect::<Vec<_>>()
        .join("\n");
    let active = active_task(workspace);
    let active_json = serde_json::to_string_pretty(&active).unwrap_or_else(|_| "null".into());
    let compact_workspace = json!({
        "relation": &workspace.relation,
        "branches": &workspace.branches,
        "scientific_observations": &workspace.claims,
        "open_questions": &workspace.open_questions,
        "observation_conflicts": &workspace.conflicts,
    });
    let workspace_json =
        serde_json::to_string_pretty(&compact_workspace).unwrap_or_else(|_| "{}".into());
    let recent_actions = actions.iter().rev().take(12).cloned().collect::<Vec<_>>();
    let action_json = serde_json::to_string_pretty(&recent_actions).unwrap_or_else(|_| "[]".into());
    let recent_validations = validations
        .iter()
        .rev()
        .take(2)
        .cloned()
        .collect::<Vec<_>>();
    let validation_json =
        serde_json::to_string_pretty(&recent_validations).unwrap_or_else(|_| "[]".into());
    let feedback_json =
        serde_json::to_string_pretty(changed_harness_feedback).unwrap_or_else(|_| "[]".into());
    let study_structure_active = active.is_some_and(|task| task.id == "task:study_structure");
    let edit_command = if study_structure_active {
        "edit_study_structure"
    } else {
        "edit_scientific_observation"
    };
    let edit_contract = if study_structure_active {
        "edit_study_structure: commit one or more source-supported conceptual branches. This command CAN create branches. Exact RAW linkage is not required: when the branch is supported but the sources do not explicitly map exact RAW basenames, use linked_raw_files=[] and linkage_status='unresolved'."
    } else {
        "edit_scientific_observation: commit one or more source-faithful scientific observations for the active field task. A source explicitly naming or defining an isolation method/platform is sufficient as an observation even when deeper mechanical detail is absent; Rust decides canonical SDRF mapping/template-gap/unresolved."
    };
    let read_policy = match active {
        Some(task) if task.decision_required && task.search_blocked => format!(
            "DECISION ONLY: prior evidence gathering produced no new source material. Both read_evidence and search_evidence are disabled. Commit a supported {edit_command} or escalate."
        ),
        Some(task) if task.decision_required => format!(
            "READ LOCK ACTIVE: Rust has already returned source context for this task. read_evidence is disabled for this turn. Use {edit_command} if the evidence supports a material finding, search_evidence if a genuinely different trusted source/query is still needed, or escalate only if the evidence is insufficient/ambiguous after reasonable trusted search."
        ),
        _ => {
            "GATHER AVAILABLE: You may read promising unread E#### refs or issue a targeted trusted search. After Rust returns context, the next turn enters a decision step before another read is allowed.".to_string()
        }
    };
    format!(
        "You are the scientific workspace agent for PRIDE single-cell proteomics dataset {acc}.\n\n\
Your environment behaves like a coding/research workspace. Rust owns persistent state, provenance, controlled-vocabulary canonicalization, task status, compilation, validation, trusted RAW linkage, and all safety gates. You inspect source evidence, build a study model, and record source-faithful scientific observations. Work ONE active task deeply before moving to another task.\n\n\
SCIENTIFIC WORKSPACE AGENT CONTRACT (v1.3):\n\
- Return exactly ONE executable top-level command for this turn: read_evidence, search_evidence, {edit_command}, or escalate. Do not narrate a future tool action inside notes; if you need to read E####, the command itself must be read_evidence.\n\
- Never request the same evidence ref twice. Rust records requested refs as read even when multiple refs resolve to the same materialized source window.\n\
- After a successful read, Rust enters a decision step: do not keep reading by inertia. Commit a supported study/observation edit, search a genuinely different source/query, or escalate.\n\
CURRENT COMMAND POLICY: {read_policy}\n\
- task_id must exactly equal the ACTIVE TASK id. Rust derives task status and chooses when compilation/validation is useful. You do NOT request compile/finish or mark validator-backed tasks resolved.\n\
- read_evidence: use when an existing promising E#### excerpt is insufficient. Rust reads a larger bounded window from only registered trusted sources and returns it on the next turn.\n\
- search_evidence: use only when focused candidates plus already-read context do not answer the task. It searches registered/trusted publication, supplement, structured-design, repository metadata, exact-RAW-name, KG, or conflict-evidence sources only; it is NOT arbitrary web browsing.\n\
- {edit_contract}\n\
- escalate: use only after relevant focused evidence/context and reasonable trusted search are exhausted or the judgment truly requires human review. Do not escalate merely because exact RAW linkage is unresolved or because Rust still needs to canonicalize an observation.\n\n\
SCIENTIFIC OBSERVATION -> RUST CANONICALIZATION CONTRACT:\n\
- observation_upserts record what the source says the experiment actually did. observed_value is an evidence-faithful scientific description, NOT an SDRF controlled-vocabulary answer.\n\
- For isolation_method, record the most specific source-faithful method description available. An explicit source-defined method/platform name such as 'evDISCO (ex vivo-digital microfluidic isolation of single cells for -Omics)' is sufficient as an observation even if the excerpt does not spell out every mechanical substep. When the source provides the physical operation, preserve it (for example, 'an individual intact cell was manually loaded into the separation capillary using hydrodynamic pressure'). Do not invent pseudo-vocabulary such as hydrodynamic_loading. Do not choose 'manual picking' merely because you think the validator wants it unless that exact phrase is what the source says. Rust independently maps the cited observation/evidence to a canonical SDRF value, template_gap, unresolved, or conflict.\n\
- For acquisition_mode, describe the source-supported acquisition regime; Rust owns the canonical serialization.\n\
- Stable observation identity is (concept_type, scope, branch_id). Same evidence-compatible meaning merges. If you intentionally replace a genuinely different prior observation, set supersedes_observed_value exactly to the old observed_value.\n\
- A branch existing does NOT automatically make every observation branch-scoped. Use project scope only when the trusted source supports one invariant value across all relevant study material and no heterogeneous branch evidence contradicts it. Use branch scope when the method/biology actually differs by branch. Rust masks unsafe project broadcasts whenever heterogeneous branch evidence exists.\n\n\
STUDY-STRUCTURE CONTRACT:\n\
- task:study_structure comes first. Establish source-grounded biological/experimental branches, acquisition cardinality, and scope before field repair. Use edit_study_structure to create the supported conceptual branches. Conceptual branches may be supported while linked_raw_files remains empty and linkage_status is unresolved; unresolved RAW linkage is NOT a reason to avoid creating the branch.\n\
- linked_raw_files require trusted source evidence explicitly linking exact RAW basenames. Filename words are search hints/contradiction detectors, never biological identity.\n\
- Never collapse multiple organisms, cell populations, isolation regimes, or acquisition regimes into one project observation.\n\n\
HARD SAFETY CONTRACT:\n\
1. Never use GT labels or hidden benchmark truth.\n\
2. Preserve project/branch/row/unresolved scope distinctions.\n\
3. Structural fields (cell identifier, fraction identifier, technical replicate) are compiler-owned and never scientific observations.\n\
4. For isolation_method, downstream lysis, digestion, generic LC/ESI injection, or processing of already isolated material is not cell isolation by itself.\n\
5. Model confidence/status are advisory. Rust owns evidence admissibility, template-gap precedence, canonical vocabulary, branch masking, trusted linkage, SDRF serialization, and validation.\n\n\
COMMAND CHOICE:\n\
- If you need more context around an existing E#### candidate -> read_evidence.\n\
- If the candidate set lacks the needed evidence -> search_evidence.\n\
- If evidence is sufficient to change the active task state -> {edit_command}.\n\
- If evidence is exhausted/ambiguous beyond safe automation -> escalate.\n\n\
TASK BOARD:\n{task_board}\n\n\
ACTIVE TASK:\n{active_json}\n\n\
FOCUSED EVIDENCE FOR ACTIVE TASK:\n{task_evidence}\n\n\
ACCEPTED DETERMINISTIC RELATION HINT (cardinality only):\n\
mode={relation}; confidence={relation_confidence}; refs={relation_refs:?}; repository_file_mode={repo_mode}; note={design_note}\n\n\
RAW FILE COUNT: {nfiles}\nRAW FILE SAMPLE (search hints only):\n{files}\n\n\
PERSISTENT WORKSPACE (read-only; mutate only via the task-specific typed edit command):\n{workspace_json}\n\n\
RECENT EXECUTED TOOL/ACTION HISTORY:\n{action_json}\n\n\
RECENT VALIDATION HISTORY:\n{validation_json}\n\n\
CHANGED RUST FEEDBACK SINCE THE PREVIOUS TURN:\n{feedback_json}\n\n\
This is turn {turn}. Return ONLY one AgentCommand object matching the JSON schema. Do not restate unchanged workspace state.",
        acc = evidence.accession,
        edit_command = edit_command,
        edit_contract = edit_contract,
        task_board = task_board_block(workspace),
        active_json = active_json,
        task_evidence = task_evidence_block(evidence, active),
        relation = evidence.study_design.relation_mode_hint,
        relation_confidence = evidence.study_design.relation_confidence,
        relation_refs = evidence.study_design.relation_evidence_refs,
        repo_mode = evidence.study_design.repository_file_mode,
        design_note = evidence.study_design.notes,
        nfiles = evidence.raw_files.len(),
        files = files,
        workspace_json = workspace_json,
        action_json = action_json,
        validation_json = validation_json,
        feedback_json = feedback_json,
        read_policy = read_policy,
        turn = turn,
    )
}

async fn call_scientific_agent(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
    workspace: &ScientificWorkspaceState,
    actions: &[EvidenceActionResult],
    validations: &[AgentValidationCycle],
    changed_harness_feedback: &[String],
    turn: usize,
) -> Result<AgentCommand> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": scientific_agent_prompt(
            opts,
            evidence,
            workspace,
            actions,
            validations,
            changed_harness_feedback,
            turn,
        ),
        "stream": false,
        "think": false,
        "format": scientific_agent_schema(
            active_task(workspace).is_some_and(|task| !task.decision_required),
            active_task(workspace).is_some_and(|task| !task.search_blocked),
            active_task(workspace).is_some_and(|task| task.id == "task:study_structure"),
        ),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| {
            format!(
                "Ollama scientific-workspace-agent request for {} turn {}",
                evidence.accession, turn
            )
        })?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .context("decode Ollama scientific-workspace-agent response")?;
    if !status.is_success() {
        bail!("Ollama scientific-workspace-agent HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty scientific-workspace-agent response");
    }
    serde_json::from_str(raw).context("parse structured scientific-workspace-agent command")
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

fn study_graph_stage1_schema() -> Value {
    let branch = json!({
        "type":"object",
        "properties":{
            "label":{"type":"string","minLength":1,"maxLength":240},
            "biological_material":{"type":"string","minLength":1,"maxLength":700},
            "experimental_role":{"type":"string","minLength":1,"maxLength":500},
            "isolation_context":{"type":"string","maxLength":800},
            "acquisition_context":{"type":"string","maxLength":800},
            "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"minItems":1,"maxItems":16},
            "linked_raw_files":{"type":"array","items":{"type":"string","maxLength":300},"maxItems":128},
            "linkage_status":{"type":"string","enum":["supported","partial","unresolved"]},
            "notes":{"type":"string","maxLength":1000}
        },
        "required":["label","biological_material","experimental_role","isolation_context","acquisition_context","evidence_refs","linked_raw_files","linkage_status","notes"],
        "additionalProperties":false
    });
    json!({
        "type":"object",
        "properties":{
            "decision":{"type":"string","enum":["propose_graph","human_review"]},
            "branches":{"type":"array","items":branch,"maxItems":16},
            "open_questions":{"type":"array","items":{"type":"string","maxLength":700},"maxItems":24},
            "reason":{"type":"string","maxLength":1600}
        },
        "required":["decision","branches","open_questions","reason"],
        "additionalProperties":false
    })
}

fn study_graph_stage1_evidence_block(
    evidence: &DatasetEvidence,
    max_items: usize,
    max_chars: usize,
) -> String {
    let refs = task_evidence_candidates(evidence, "study_structure", max_items.max(1));
    let mut rendered = Vec::new();
    let mut used = 0usize;
    for id in refs {
        let Some(item) = evidence.evidence.iter().find(|item| item.id == id) else {
            continue;
        };
        let remaining = max_chars.saturating_sub(used);
        if remaining == 0 {
            break;
        }
        let source = format!("{} [{}:{}] ", item.id, item.source_kind, item.source_label);
        let available = remaining.saturating_sub(source.chars().count());
        if available == 0 {
            break;
        }
        let text = item.text.replace('\0', " ");
        let body = if text.chars().count() > available {
            text.chars().take(available).collect::<String>() + "..."
        } else {
            text
        };
        let line = source + &body;
        used += line.chars().count();
        rendered.push(line);
    }
    if rendered.is_empty() {
        "none".into()
    } else {
        rendered.join("\n\n")
    }
}

fn study_graph_stage1_prompt(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
) -> String {
    let evidence_block = study_graph_stage1_evidence_block(
        evidence,
        opts.max_evidence_items.min(32),
        opts.max_evidence_chars.min(60000),
    );
    format!(
        "You are constructing the prerequisite StudyGraph for PRIDE single-cell proteomics dataset {acc}.\n\n\
This is a ONE-SHOT, BOUNDED STRUCTURAL SYNTHESIS. There is no conversational repair loop. Return either a source-grounded conceptual study graph or human_review.\n\n\
STUDYGRAPH CONTRACT:\n\
- A branch is a biologically or experimentally distinct material/regime that must not be silently collapsed into another branch.\n\
- Use the trusted E#### evidence below. Every proposed branch requires at least one supporting E#### ref.\n\
- Conceptual branch creation does NOT require exact RAW linkage. If the branch is supported but exact RAW basenames are not explicitly linked by trusted source text, use linked_raw_files=[] and linkage_status='unresolved'.\n\
- Exact RAW linkage is allowed only when the cited trusted evidence explicitly names that RAW basename in connection with the branch. Filename words alone are never biological identity.\n\
- Do not infer organism, cell line, cell type, isolation regime, acquisition regime, or branch membership from filenames.\n\
- biological_material and experimental_role must summarize what the cited source actually supports. isolation_context and acquisition_context may be empty when not established.\n\
- Do not choose SDRF controlled-vocabulary values and do not repair any SDRF field in this phase.\n\
- Use human_review only when the conceptual study structure itself cannot be established safely from the registered evidence. Unresolved RAW linkage alone is NOT a reason for human_review.\n\
- Prefer a small number of defensible branches over speculative fine-grained branches.\n\n\
DETERMINISTIC CARDINALITY HINT (not biological identity):\n\
relation_mode={relation}; confidence={confidence}; refs={refs:?}; repository_file_mode={repo_mode}; note={note}\n\n\
TRUSTED STUDY-STRUCTURE EVIDENCE:\n{evidence_block}\n\n\
Return ONLY the StudyGraphProposal JSON object matching the schema.",
        acc = evidence.accession,
        relation = evidence.study_design.relation_mode_hint,
        confidence = evidence.study_design.relation_confidence,
        refs = evidence.study_design.relation_evidence_refs,
        repo_mode = evidence.study_design.repository_file_mode,
        note = evidence.study_design.notes,
        evidence_block = evidence_block,
    )
}

async fn call_study_graph_stage1(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
) -> Result<StudyGraphProposal> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": study_graph_stage1_prompt(opts, evidence),
        "stream": false,
        "think": false,
        "format": study_graph_stage1_schema(),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| {
            format!(
                "Ollama StudyGraph v2-stage1 request for {}",
                evidence.accession
            )
        })?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .context("decode Ollama StudyGraph v2-stage1 response")?;
    if !status.is_success() {
        bail!("Ollama StudyGraph v2-stage1 HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty StudyGraph v2-stage1 response");
    }
    serde_json::from_str(raw).context("parse structured StudyGraph v2-stage1 proposal")
}

fn normalize_study_graph_text(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim()
        .to_string()
}

fn study_graph_branch_key(branch: &StudyGraphBranchProposal) -> String {
    format!(
        "{}|{}|{}|{}|{}",
        normalize_study_graph_text(&branch.label).to_ascii_lowercase(),
        normalize_study_graph_text(&branch.biological_material).to_ascii_lowercase(),
        normalize_study_graph_text(&branch.experimental_role).to_ascii_lowercase(),
        normalize_study_graph_text(&branch.isolation_context).to_ascii_lowercase(),
        normalize_study_graph_text(&branch.acquisition_context).to_ascii_lowercase(),
    )
}

fn accept_study_graph_stage1(
    evidence: &DatasetEvidence,
    proposal: &StudyGraphProposal,
) -> StudyGraphAcceptance {
    if proposal.decision == "human_review" {
        return StudyGraphAcceptance {
            harness_version: SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_VERSION.into(),
            accession: evidence.accession.clone(),
            status: "human_review".into(),
            open_questions: proposal.open_questions.clone(),
            reason: normalize_study_graph_text(&proposal.reason),
            model_calls: 1,
            ..Default::default()
        };
    }
    if proposal.decision != "propose_graph" {
        return StudyGraphAcceptance {
            harness_version: SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_VERSION.into(),
            accession: evidence.accession.clone(),
            status: "human_review".into(),
            reason: format!("unsupported StudyGraph decision '{}'", proposal.decision),
            model_calls: 1,
            ..Default::default()
        };
    }

    let raw_names = evidence
        .raw_files
        .iter()
        .map(|raw| raw.file_name.to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    let mut rejected_branches = Vec::new();
    let mut removed_raw_links = Vec::new();
    let mut accepted = Vec::<(String, StudyGraphBranchProposal, Vec<String>, Vec<String>)>::new();
    let mut seen = BTreeSet::new();

    for mut branch in proposal.branches.clone() {
        branch.label = normalize_study_graph_text(&branch.label);
        branch.biological_material = normalize_study_graph_text(&branch.biological_material);
        branch.experimental_role = normalize_study_graph_text(&branch.experimental_role);
        branch.isolation_context = normalize_study_graph_text(&branch.isolation_context);
        branch.acquisition_context = normalize_study_graph_text(&branch.acquisition_context);
        branch.notes = normalize_study_graph_text(&branch.notes);
        let refs = valid_evidence_refs(evidence, &branch.evidence_refs);
        if branch.label.is_empty()
            || branch.biological_material.is_empty()
            || branch.experimental_role.is_empty()
            || refs.is_empty()
        {
            rejected_branches.push(StudyGraphRejectedBranch {
                label: branch.label.clone(),
                reason: "branch rejected: label, biological_material, experimental_role and at least one valid trusted evidence ref are required".into(),
            });
            continue;
        }
        let key = study_graph_branch_key(&branch);
        if !seen.insert(key.clone()) {
            rejected_branches.push(StudyGraphRejectedBranch {
                label: branch.label.clone(),
                reason: "branch rejected: semantic duplicate of an already proposed branch".into(),
            });
            continue;
        }
        let mut linked = Vec::new();
        for raw in &branch.linked_raw_files {
            let trimmed = raw.trim();
            let Some(canonical) = evidence
                .raw_files
                .iter()
                .find(|candidate| candidate.file_name.eq_ignore_ascii_case(trimmed))
                .map(|candidate| candidate.file_name.clone())
            else {
                removed_raw_links.push(StudyGraphRemovedRawLink {
                    branch_label: branch.label.clone(),
                    raw_file: trimmed.to_string(),
                    reason: "RAW basename is not present in the repository inventory".into(),
                });
                continue;
            };
            if !raw_names.contains(&canonical.to_ascii_lowercase())
                || !branch_file_is_source_grounded(evidence, &refs, &canonical)
            {
                removed_raw_links.push(StudyGraphRemovedRawLink {
                    branch_label: branch.label.clone(),
                    raw_file: canonical,
                    reason: "exact RAW linkage was not explicitly supported by the cited trusted evidence; linkage was removed rather than inferred from filename semantics".into(),
                });
                continue;
            }
            linked.push(canonical);
        }
        linked.sort();
        linked.dedup();
        accepted.push((key, branch, refs, linked));
    }

    accepted.sort_by(|a, b| a.0.cmp(&b.0));
    let branches = accepted
        .into_iter()
        .enumerate()
        .map(|(i, (_, branch, refs, linked))| AcceptedStudyGraphBranch {
            id: format!("B{:03}", i + 1),
            label: branch.label,
            biological_material: branch.biological_material,
            experimental_role: branch.experimental_role,
            isolation_context: branch.isolation_context,
            acquisition_context: branch.acquisition_context,
            evidence_refs: refs,
            linkage_status: if linked.is_empty() {
                "unresolved".into()
            } else if branch.linkage_status == "supported" {
                "supported".into()
            } else {
                "partial".into()
            },
            linked_raw_files: linked,
            notes: branch.notes,
        })
        .collect::<Vec<_>>();

    let status = if branches.is_empty() {
        "human_review"
    } else {
        "accepted"
    };
    let reason = if branches.is_empty() {
        "Rust could not accept any source-grounded conceptual branch from the one-shot StudyGraph proposal".into()
    } else {
        format!(
            "Rust accepted {} source-grounded conceptual branch(es); exact RAW linkage remains unresolved wherever trusted evidence did not explicitly support it",
            branches.len()
        )
    };
    StudyGraphAcceptance {
        harness_version: SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_VERSION.into(),
        accession: evidence.accession.clone(),
        status: status.into(),
        branches,
        open_questions: proposal.open_questions.clone(),
        rejected_branches,
        removed_raw_links,
        reason,
        model_calls: 1,
    }
}

fn write_study_graph_stage1_review(path: &Path, acceptance: &StudyGraphAcceptance) -> Result<()> {
    let mut out = String::new();
    out.push_str(&format!(
        "# PRIDE-SCP StudyGraph v2 Stage 1: {}\n\n",
        acceptance.accession
    ));
    out.push_str(&format!("Status: **{}**\n\n", acceptance.status));
    out.push_str(&format!("{}\n\n", acceptance.reason));
    out.push_str("## Accepted conceptual branches\n\n");
    if acceptance.branches.is_empty() {
        out.push_str("None. Accession stops at human review before downstream annotation.\n\n");
    } else {
        for branch in &acceptance.branches {
            out.push_str(&format!(
                "- **{} — {}**\n  - biological material: {}\n  - experimental role: {}\n  - isolation context: {}\n  - acquisition context: {}\n  - evidence: {:?}\n  - linked RAW files: {:?}\n  - linkage status: {}\n  - notes: {}\n",
                branch.id,
                branch.label,
                branch.biological_material,
                branch.experimental_role,
                branch.isolation_context,
                branch.acquisition_context,
                branch.evidence_refs,
                branch.linked_raw_files,
                branch.linkage_status,
                branch.notes,
            ));
        }
        out.push('\n');
    }
    if !acceptance.open_questions.is_empty() {
        out.push_str("## Open structural questions\n\n");
        for question in &acceptance.open_questions {
            out.push_str(&format!("- {}\n", question));
        }
        out.push('\n');
    }
    if !acceptance.removed_raw_links.is_empty() {
        out.push_str("## RAW links removed by Rust\n\n");
        for removed in &acceptance.removed_raw_links {
            out.push_str(&format!(
                "- {}: {} — {}\n",
                removed.branch_label, removed.raw_file, removed.reason
            ));
        }
        out.push('\n');
    }
    fs::write(path, out)?;
    Ok(())
}

async fn run_one_study_graph_stage1(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let evidence = build_evidence(&annotate_opts, accession)?;
    let root = opts.output_dir.join("study_graphs").join(accession);
    fs::create_dir_all(&root)?;
    let evidence_path = root.join("evidence.json");
    let proposal_path = root.join("proposal.json");
    let accepted_path = root.join("accepted_graph.json");
    let review_path = root.join("REVIEW.md");
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;

    let proposal = call_study_graph_stage1(opts, &evidence).await?;
    fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    let acceptance = accept_study_graph_stage1(&evidence, &proposal);
    fs::write(&accepted_path, serde_json::to_string_pretty(&acceptance)?)?;
    write_study_graph_stage1_review(&review_path, &acceptance)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: if acceptance.status == "accepted" {
            "study_graph_accepted".into()
        } else {
            "study_graph_human_review".into()
        },
        turns: 1,
        tool_actions: 0,
        validator_cycles: 0,
        branches: acceptance.branches.len(),
        open_questions: acceptance.open_questions.len(),
        relation_mode: evidence.study_design.relation_mode_hint.clone(),
        locally_valid: false,
        validation_errors: 0,
        draft_path: accepted_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: root.display().to_string(),
        error: String::new(),
    })
}

async fn run_study_graph_stage1(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts.output_dir.join("study_graph_stage1_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!(
                "[{}/{}] {} StudyGraph v2-stage1",
                i + 1,
                accessions.len(),
                accession
            );
        }
        match run_one_study_graph_stage1(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> terminal={} model_calls={} branches={} open_questions={}",
                        row.terminal_status, row.turns, row.branches, row.open_questions
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> StudyGraph v2-stage1 error: {err:#}");
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
    let successful = rows.iter().filter(|row| row.status == "success").count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts: 0,
        incomplete_drafts: 0,
        total_validation_errors: 0,
        total_agent_turns: rows.iter().map(|row| row.turns).sum(),
        total_tool_actions: 0,
        total_validator_cycles: 0,
        results_tsv: results_path.display().to_string(),
        workspace_root: opts.output_dir.join("study_graphs").display().to_string(),
    };
    let rendered = serde_json::to_string_pretty(&summary)?;
    fs::write(
        opts.output_dir.join("study_graph_stage1_summary.json"),
        &rendered,
    )?;
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        rendered,
    )?;
    Ok(summary)
}

fn factor_graph_stage1_schema() -> Value {
    let evidence_refs = || {
        json!({
            "type":"array",
            "items":{"type":"string","pattern":"^E[0-9]{4}$"},
            "minItems":1,
            "maxItems":16
        })
    };
    let material = json!({
        "type":"object",
        "properties":{
            "local_id":{"type":"string","pattern":"^M[0-9]{1,3}$"},
            "label":{"type":"string","minLength":1,"maxLength":240},
            "organism":{"type":"string","maxLength":300},
            "biological_material":{"type":"string","minLength":1,"maxLength":700},
            "experimental_role":{"type":"string","maxLength":500},
            "evidence_refs":evidence_refs(),
            "notes":{"type":"string","maxLength":1000}
        },
        "required":["local_id","label","organism","biological_material","experimental_role","evidence_refs","notes"],
        "additionalProperties":false
    });
    let regime = json!({
        "type":"object",
        "properties":{
            "local_id":{"type":"string","pattern":"^R[0-9]{1,3}$"},
            "label":{"type":"string","minLength":1,"maxLength":240},
            "experimental_role":{"type":"string","minLength":1,"maxLength":500},
            "isolation_or_loading_method":{"type":"string","maxLength":800},
            "input_or_cell_count_regime":{"type":"string","maxLength":500},
            "evidence_refs":evidence_refs(),
            "notes":{"type":"string","maxLength":1000}
        },
        "required":["local_id","label","experimental_role","isolation_or_loading_method","input_or_cell_count_regime","evidence_refs","notes"],
        "additionalProperties":false
    });
    let acquisition = json!({
        "type":"object",
        "properties":{
            "local_id":{"type":"string","pattern":"^A[0-9]{1,3}$"},
            "label":{"type":"string","minLength":1,"maxLength":240},
            "acquisition_method_or_platform":{"type":"string","minLength":1,"maxLength":800},
            "evidence_refs":evidence_refs(),
            "notes":{"type":"string","maxLength":1000}
        },
        "required":["local_id","label","acquisition_method_or_platform","evidence_refs","notes"],
        "additionalProperties":false
    });
    let relation = json!({
        "type":"object",
        "properties":{
            "source_id":{"type":"string","pattern":"^[MRA][0-9]{1,3}$"},
            "relation_type":{"type":"string","enum":["material_to_regime","regime_to_acquisition","material_to_acquisition"]},
            "target_id":{"type":"string","pattern":"^[MRA][0-9]{1,3}$"},
            "evidence_refs":evidence_refs(),
            "notes":{"type":"string","maxLength":800}
        },
        "required":["source_id","relation_type","target_id","evidence_refs","notes"],
        "additionalProperties":false
    });
    let raw_link = json!({
        "type":"object",
        "properties":{
            "node_id":{"type":"string","pattern":"^[MRA][0-9]{1,3}$"},
            "raw_file":{"type":"string","minLength":1,"maxLength":300},
            "evidence_refs":evidence_refs(),
            "notes":{"type":"string","maxLength":800}
        },
        "required":["node_id","raw_file","evidence_refs","notes"],
        "additionalProperties":false
    });
    json!({
        "type":"object",
        "properties":{
            "decision":{"type":"string","enum":["propose_graph","human_review"]},
            "materials":{"type":"array","items":material,"maxItems":24},
            "regimes":{"type":"array","items":regime,"maxItems":24},
            "acquisitions":{"type":"array","items":acquisition,"maxItems":16},
            "relations":{"type":"array","items":relation,"maxItems":64},
            "raw_links":{"type":"array","items":raw_link,"maxItems":128},
            "open_questions":{"type":"array","items":{"type":"string","maxLength":700},"maxItems":24},
            "reason":{"type":"string","maxLength":1800}
        },
        "required":["decision","materials","regimes","acquisitions","relations","raw_links","open_questions","reason"],
        "additionalProperties":false
    })
}

fn factor_graph_stage1_prompt(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
) -> String {
    let evidence_block = study_graph_stage1_evidence_block(
        evidence,
        opts.max_evidence_items.min(40),
        opts.max_evidence_chars.min(60000),
    );
    format!(
        "You are constructing the prerequisite evidence-backed scientific FACTOR GRAPH for PRIDE single-cell proteomics dataset {acc}.\n\n\
This is a ONE-SHOT, BOUNDED STRUCTURAL SYNTHESIS. There is no conversational repair loop. Return either a source-grounded factor graph or human_review.\n\n\
WHY A FACTOR GRAPH:\n\
- Do NOT force organism/material, experimental regime, isolation/loading method, experimental role, and acquisition into one monolithic branch.\n\
- Preserve orthogonal distinctions as separate nodes even when they share the same material or acquisition workflow.\n\n\
MATERIAL NODES (M#):\n\
- Represent distinct biological/material entities directly supported by trusted evidence: organism, cell line/type, tissue, commercial reference digest, etc.\n\
- Distinct explicit organisms or materially different source materials must not be silently collapsed merely because they share a workflow.\n\n\
EXPERIMENTAL REGIME NODES (R#):\n\
- Represent distinct experimental roles, isolation/loading/sampling operations, or input/cell-count regimes.\n\
- If trusted evidence distinguishes single-cell manual/hydrodynamic loading from low-number spray-voltage injection, they MUST be separate regime nodes even if both use HeLa and the same CE-MS/MS acquisition.\n\
- A named source-defined isolation technology such as evDISCO/tDISCO or capillary microsampling is a valid regime description.\n\n\
ACQUISITION NODES (A#):\n\
- Represent distinct mass-spectrometry acquisition/platform workflows when source-supported.\n\n\
RELATIONS:\n\
- Use only material_to_regime, regime_to_acquisition, or material_to_acquisition.\n\
- A relation requires source-grounded E#### evidence. Do not create a relation just because two nodes coexist in the project.\n\n\
RAW LINKAGE:\n\
- Exact RAW linkage is OPTIONAL and separate from the scientific factor graph.\n\
- Add raw_links only when trusted cited source text explicitly names that RAW basename in connection with the target node.\n\
- Filename words alone are never evidence for organism, cell type, regime, condition, or role.\n\
- Lack of exact RAW linkage is NOT a reason to merge nodes and is NOT a reason for human_review.\n\n\
SAFETY AND SCOPE:\n\
- Every node and relation requires at least one supporting E#### ref.\n\
- Do not choose SDRF controlled-vocabulary values and do not repair SDRF fields in this phase.\n\
- Use human_review only when the conceptual factor graph itself cannot be established safely.\n\n\
{decision_contract}\n\
DETERMINISTIC CARDINALITY HINT (not biological identity):\n\
relation_mode={relation}; confidence={confidence}; refs={refs:?}; repository_file_mode={repo_mode}; note={note}\n\n\
TRUSTED STUDY-STRUCTURE EVIDENCE:\n{evidence_block}\n\n\
Return ONLY the StudyFactorGraphProposal JSON object matching the schema.",
        acc = evidence.accession,
        relation = evidence.study_design.relation_mode_hint,
        confidence = evidence.study_design.relation_confidence,
        refs = evidence.study_design.relation_evidence_refs,
        repo_mode = evidence.study_design.repository_file_mode,
        note = evidence.study_design.notes,
        decision_contract = FACTOR_GRAPH_STAGE1_DECISION_CONTRACT,
        evidence_block = evidence_block,
    )
}

async fn call_factor_graph_stage1(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
) -> Result<StudyFactorGraphProposal> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": factor_graph_stage1_prompt(opts, evidence),
        "stream": false,
        "think": false,
        "format": factor_graph_stage1_schema(),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| {
            format!(
                "Ollama factor-graph v2-stage1 request for {}",
                evidence.accession
            )
        })?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .context("decode Ollama factor-graph v2-stage1 response")?;
    if !status.is_success() {
        bail!("Ollama factor-graph v2-stage1 HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty factor-graph v2-stage1 response");
    }
    serde_json::from_str(raw).context("parse structured factor-graph v2-stage1 proposal")
}

fn normalize_factor_local_id(value: &str) -> String {
    normalize_study_graph_text(value).to_ascii_uppercase()
}

fn factor_local_id_has_prefix(value: &str, prefix: char) -> bool {
    let value = normalize_factor_local_id(value);
    let mut chars = value.chars();
    chars.next() == Some(prefix)
        && chars.clone().count() >= 1
        && chars.count() <= 3
        && value.chars().skip(1).all(|ch| ch.is_ascii_digit())
}

fn relation_type_matches_local_ids(relation_type: &str, source: &str, target: &str) -> bool {
    match relation_type {
        "material_to_regime" => {
            factor_local_id_has_prefix(source, 'M') && factor_local_id_has_prefix(target, 'R')
        }
        "regime_to_acquisition" => {
            factor_local_id_has_prefix(source, 'R') && factor_local_id_has_prefix(target, 'A')
        }
        "material_to_acquisition" => {
            factor_local_id_has_prefix(source, 'M') && factor_local_id_has_prefix(target, 'A')
        }
        _ => false,
    }
}

fn accept_factor_graph_stage1(
    evidence: &DatasetEvidence,
    proposal: &StudyFactorGraphProposal,
) -> StudyFactorGraphAcceptance {
    if proposal.decision == "human_review" {
        return StudyFactorGraphAcceptance {
            harness_version: SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION.into(),
            accession: evidence.accession.clone(),
            status: "human_review".into(),
            open_questions: proposal.open_questions.clone(),
            reason: normalize_study_graph_text(&proposal.reason),
            model_calls: 1,
            ..Default::default()
        };
    }
    if proposal.decision != "propose_graph" {
        return StudyFactorGraphAcceptance {
            harness_version: SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION.into(),
            accession: evidence.accession.clone(),
            status: "human_review".into(),
            reason: format!("unsupported factor-graph decision '{}'", proposal.decision),
            model_calls: 1,
            ..Default::default()
        };
    }

    let mut rejected_items = Vec::new();
    let mut local_to_canonical = BTreeMap::<String, String>::new();
    let mut seen_local = BTreeSet::<String>::new();

    let mut material_rows = Vec::<(String, String, FactorMaterialProposal, Vec<String>)>::new();
    let mut seen_materials = BTreeSet::<String>::new();
    for mut node in proposal.materials.clone() {
        node.local_id = normalize_factor_local_id(&node.local_id);
        node.label = normalize_study_graph_text(&node.label);
        node.organism = normalize_study_graph_text(&node.organism);
        node.biological_material = normalize_study_graph_text(&node.biological_material);
        node.experimental_role = normalize_study_graph_text(&node.experimental_role);
        node.notes = normalize_study_graph_text(&node.notes);
        let refs = valid_evidence_refs(evidence, &node.evidence_refs);
        if !factor_local_id_has_prefix(&node.local_id, 'M')
            || node.label.is_empty()
            || node.biological_material.is_empty()
            || refs.is_empty()
        {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "material".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "material rejected: M# local_id, label, biological_material and trusted evidence are required".into(),
            });
            continue;
        }
        if !seen_local.insert(node.local_id.clone()) {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "material".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "material rejected: duplicate local_id".into(),
            });
            continue;
        }
        let key = format!(
            "{}|{}|{}|{}",
            node.label.to_ascii_lowercase(),
            node.organism.to_ascii_lowercase(),
            node.biological_material.to_ascii_lowercase(),
            node.experimental_role.to_ascii_lowercase(),
        );
        if !seen_materials.insert(key.clone()) {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "material".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "material rejected: semantic duplicate".into(),
            });
            continue;
        }
        material_rows.push((key, node.local_id.clone(), node, refs));
    }
    material_rows.sort_by(|a, b| a.0.cmp(&b.0));
    let mut materials = Vec::new();
    for (i, (_, local_id, node, refs)) in material_rows.into_iter().enumerate() {
        let id = format!("M{:03}", i + 1);
        local_to_canonical.insert(local_id, id.clone());
        materials.push(AcceptedFactorMaterial {
            id,
            label: node.label,
            organism: node.organism,
            biological_material: node.biological_material,
            experimental_role: node.experimental_role,
            evidence_refs: refs,
            notes: node.notes,
        });
    }

    let mut regime_rows = Vec::<(String, String, FactorRegimeProposal, Vec<String>)>::new();
    let mut seen_regimes = BTreeSet::<String>::new();
    for mut node in proposal.regimes.clone() {
        node.local_id = normalize_factor_local_id(&node.local_id);
        node.label = normalize_study_graph_text(&node.label);
        node.experimental_role = normalize_study_graph_text(&node.experimental_role);
        node.isolation_or_loading_method =
            normalize_study_graph_text(&node.isolation_or_loading_method);
        node.input_or_cell_count_regime =
            normalize_study_graph_text(&node.input_or_cell_count_regime);
        node.notes = normalize_study_graph_text(&node.notes);
        let refs = valid_evidence_refs(evidence, &node.evidence_refs);
        if !factor_local_id_has_prefix(&node.local_id, 'R')
            || node.label.is_empty()
            || node.experimental_role.is_empty()
            || refs.is_empty()
        {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "regime".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "regime rejected: R# local_id, label, experimental_role and trusted evidence are required".into(),
            });
            continue;
        }
        if !seen_local.insert(node.local_id.clone()) {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "regime".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "regime rejected: duplicate local_id".into(),
            });
            continue;
        }
        let key = format!(
            "{}|{}|{}|{}",
            node.label.to_ascii_lowercase(),
            node.experimental_role.to_ascii_lowercase(),
            node.isolation_or_loading_method.to_ascii_lowercase(),
            node.input_or_cell_count_regime.to_ascii_lowercase(),
        );
        if !seen_regimes.insert(key.clone()) {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "regime".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "regime rejected: semantic duplicate".into(),
            });
            continue;
        }
        regime_rows.push((key, node.local_id.clone(), node, refs));
    }
    regime_rows.sort_by(|a, b| a.0.cmp(&b.0));
    let mut regimes = Vec::new();
    for (i, (_, local_id, node, refs)) in regime_rows.into_iter().enumerate() {
        let id = format!("R{:03}", i + 1);
        local_to_canonical.insert(local_id, id.clone());
        regimes.push(AcceptedFactorRegime {
            id,
            label: node.label,
            experimental_role: node.experimental_role,
            isolation_or_loading_method: node.isolation_or_loading_method,
            input_or_cell_count_regime: node.input_or_cell_count_regime,
            evidence_refs: refs,
            notes: node.notes,
        });
    }

    let mut acquisition_rows =
        Vec::<(String, String, FactorAcquisitionProposal, Vec<String>)>::new();
    let mut seen_acquisitions = BTreeSet::<String>::new();
    for mut node in proposal.acquisitions.clone() {
        node.local_id = normalize_factor_local_id(&node.local_id);
        node.label = normalize_study_graph_text(&node.label);
        node.acquisition_method_or_platform =
            normalize_study_graph_text(&node.acquisition_method_or_platform);
        node.notes = normalize_study_graph_text(&node.notes);
        let refs = valid_evidence_refs(evidence, &node.evidence_refs);
        if !factor_local_id_has_prefix(&node.local_id, 'A')
            || node.label.is_empty()
            || node.acquisition_method_or_platform.is_empty()
            || refs.is_empty()
        {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "acquisition".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "acquisition rejected: A# local_id, label, acquisition_method_or_platform and trusted evidence are required".into(),
            });
            continue;
        }
        if !seen_local.insert(node.local_id.clone()) {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "acquisition".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "acquisition rejected: duplicate local_id".into(),
            });
            continue;
        }
        let key = format!(
            "{}|{}",
            node.label.to_ascii_lowercase(),
            node.acquisition_method_or_platform.to_ascii_lowercase(),
        );
        if !seen_acquisitions.insert(key.clone()) {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "acquisition".into(),
                local_id: node.local_id.clone(),
                label: node.label.clone(),
                reason: "acquisition rejected: semantic duplicate".into(),
            });
            continue;
        }
        acquisition_rows.push((key, node.local_id.clone(), node, refs));
    }
    acquisition_rows.sort_by(|a, b| a.0.cmp(&b.0));
    let mut acquisitions = Vec::new();
    for (i, (_, local_id, node, refs)) in acquisition_rows.into_iter().enumerate() {
        let id = format!("A{:03}", i + 1);
        local_to_canonical.insert(local_id, id.clone());
        acquisitions.push(AcceptedFactorAcquisition {
            id,
            label: node.label,
            acquisition_method_or_platform: node.acquisition_method_or_platform,
            evidence_refs: refs,
            notes: node.notes,
        });
    }

    let mut relations = Vec::new();
    let mut seen_relations = BTreeSet::new();
    for relation in &proposal.relations {
        let source_local = normalize_factor_local_id(&relation.source_id);
        let target_local = normalize_factor_local_id(&relation.target_id);
        let refs = valid_evidence_refs(evidence, &relation.evidence_refs);
        let relation_type =
            normalize_study_graph_text(&relation.relation_type).to_ascii_lowercase();
        if refs.is_empty()
            || !relation_type_matches_local_ids(&relation_type, &source_local, &target_local)
        {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "relation".into(),
                local_id: format!("{}->{}", source_local, target_local),
                label: relation_type.clone(),
                reason: "relation rejected: supported relation type, compatible node types and trusted evidence are required".into(),
            });
            continue;
        }
        let (Some(source_id), Some(target_id)) = (
            local_to_canonical.get(&source_local),
            local_to_canonical.get(&target_local),
        ) else {
            rejected_items.push(FactorGraphRejectedItem {
                item_type: "relation".into(),
                local_id: format!("{}->{}", source_local, target_local),
                label: relation_type.clone(),
                reason: "relation rejected: source or target node was not accepted by Rust".into(),
            });
            continue;
        };
        let key = format!("{}|{}|{}", source_id, relation_type, target_id);
        if !seen_relations.insert(key) {
            continue;
        }
        relations.push(AcceptedFactorRelation {
            source_id: source_id.clone(),
            relation_type,
            target_id: target_id.clone(),
            evidence_refs: refs,
            notes: normalize_study_graph_text(&relation.notes),
        });
    }
    relations.sort_by(|a, b| {
        (&a.source_id, &a.relation_type, &a.target_id).cmp(&(
            &b.source_id,
            &b.relation_type,
            &b.target_id,
        ))
    });

    let raw_names = evidence
        .raw_files
        .iter()
        .map(|raw| raw.file_name.to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    let mut raw_links = Vec::new();
    let mut removed_raw_links = Vec::new();
    for raw_link in &proposal.raw_links {
        let local_id = normalize_factor_local_id(&raw_link.node_id);
        let Some(node_id) = local_to_canonical.get(&local_id) else {
            removed_raw_links.push(FactorGraphRemovedRawLink {
                node_id: local_id,
                raw_file: raw_link.raw_file.trim().to_string(),
                reason: "RAW link removed: target factor node was not accepted by Rust".into(),
            });
            continue;
        };
        let refs = valid_evidence_refs(evidence, &raw_link.evidence_refs);
        let trimmed = raw_link.raw_file.trim();
        let Some(canonical) = evidence
            .raw_files
            .iter()
            .find(|candidate| candidate.file_name.eq_ignore_ascii_case(trimmed))
            .map(|candidate| candidate.file_name.clone())
        else {
            removed_raw_links.push(FactorGraphRemovedRawLink {
                node_id: node_id.clone(),
                raw_file: trimmed.to_string(),
                reason: "RAW link removed: basename is not present in the repository inventory"
                    .into(),
            });
            continue;
        };
        if refs.is_empty()
            || !raw_names.contains(&canonical.to_ascii_lowercase())
            || !branch_file_is_source_grounded(evidence, &refs, &canonical)
        {
            removed_raw_links.push(FactorGraphRemovedRawLink {
                node_id: node_id.clone(),
                raw_file: canonical,
                reason: "RAW link removed: exact basename linkage was not explicitly supported by cited trusted evidence; filename semantics were not used".into(),
            });
            continue;
        }
        raw_links.push(AcceptedFactorRawLink {
            node_id: node_id.clone(),
            raw_file: canonical,
            evidence_refs: refs,
            notes: normalize_study_graph_text(&raw_link.notes),
        });
    }
    raw_links.sort_by(|a, b| (&a.node_id, &a.raw_file).cmp(&(&b.node_id, &b.raw_file)));
    raw_links.dedup_by(|a, b| a.node_id == b.node_id && a.raw_file == b.raw_file);

    let status = if materials.is_empty() || regimes.is_empty() {
        "human_review"
    } else {
        "accepted"
    };
    let reason = if status == "accepted" {
        format!(
            "Rust accepted an evidence-backed factor graph with {} material node(s), {} experimental-regime node(s), {} acquisition node(s), {} relation(s), and {} exact RAW link(s)",
            materials.len(), regimes.len(), acquisitions.len(), relations.len(), raw_links.len()
        )
    } else {
        "Rust could not accept the minimum safe factor graph: at least one material node and one experimental-regime node are required".into()
    };

    StudyFactorGraphAcceptance {
        harness_version: SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION.into(),
        accession: evidence.accession.clone(),
        status: status.into(),
        materials,
        regimes,
        acquisitions,
        relations,
        raw_links,
        open_questions: proposal.open_questions.clone(),
        rejected_items,
        removed_raw_links,
        reason,
        model_calls: 1,
    }
}

fn write_factor_graph_stage1_review(
    path: &Path,
    acceptance: &StudyFactorGraphAcceptance,
) -> Result<()> {
    let mut out = String::new();
    out.push_str(&format!(
        "# PRIDE-SCP Factor Graph v2 Stage 1: {}\n\n",
        acceptance.accession
    ));
    out.push_str(&format!(
        "Status: **{}**\n\n{}\n\n",
        acceptance.status, acceptance.reason
    ));
    out.push_str("## Materials\n\n");
    for node in &acceptance.materials {
        out.push_str(&format!(
            "- **{} — {}**\n  - organism: {}\n  - biological material: {}\n  - experimental role: {}\n  - evidence: {:?}\n  - notes: {}\n",
            node.id, node.label, node.organism, node.biological_material, node.experimental_role, node.evidence_refs, node.notes
        ));
    }
    out.push_str("\n## Experimental regimes\n\n");
    for node in &acceptance.regimes {
        out.push_str(&format!(
            "- **{} — {}**\n  - role: {}\n  - isolation/loading: {}\n  - input/cell-count regime: {}\n  - evidence: {:?}\n  - notes: {}\n",
            node.id, node.label, node.experimental_role, node.isolation_or_loading_method, node.input_or_cell_count_regime, node.evidence_refs, node.notes
        ));
    }
    out.push_str("\n## Acquisitions\n\n");
    for node in &acceptance.acquisitions {
        out.push_str(&format!(
            "- **{} — {}**\n  - method/platform: {}\n  - evidence: {:?}\n  - notes: {}\n",
            node.id,
            node.label,
            node.acquisition_method_or_platform,
            node.evidence_refs,
            node.notes
        ));
    }
    out.push_str("\n## Relations\n\n");
    for relation in &acceptance.relations {
        out.push_str(&format!(
            "- {} --{}--> {} | evidence={:?} | {}\n",
            relation.source_id,
            relation.relation_type,
            relation.target_id,
            relation.evidence_refs,
            relation.notes
        ));
    }
    out.push_str("\n## Exact RAW links\n\n");
    if acceptance.raw_links.is_empty() {
        out.push_str(
            "None. Exact RAW linkage remains unresolved and is not inferred from filenames.\n",
        );
    } else {
        for link in &acceptance.raw_links {
            out.push_str(&format!(
                "- {} -> {} | evidence={:?}\n",
                link.node_id, link.raw_file, link.evidence_refs
            ));
        }
    }
    out.push_str("\n## Open questions\n\n");
    for question in &acceptance.open_questions {
        out.push_str(&format!("- {}\n", question));
    }
    fs::write(path, out)?;
    Ok(())
}

async fn run_one_factor_graph_stage1(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let evidence = build_evidence(&annotate_opts, accession)?;
    let root = opts.output_dir.join("study_factor_graphs").join(accession);
    fs::create_dir_all(&root)?;
    let evidence_path = root.join("evidence.json");
    let proposal_path = root.join("proposal.json");
    let accepted_path = root.join("accepted_graph.json");
    let review_path = root.join("REVIEW.md");
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;

    let proposal = call_factor_graph_stage1(opts, &evidence).await?;
    fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    let acceptance = accept_factor_graph_stage1(&evidence, &proposal);
    fs::write(&accepted_path, serde_json::to_string_pretty(&acceptance)?)?;
    write_factor_graph_stage1_review(&review_path, &acceptance)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: if acceptance.status == "accepted" {
            "factor_graph_accepted".into()
        } else {
            "factor_graph_human_review".into()
        },
        turns: 1,
        tool_actions: 0,
        validator_cycles: 0,
        branches: acceptance.materials.len()
            + acceptance.regimes.len()
            + acceptance.acquisitions.len(),
        open_questions: acceptance.open_questions.len(),
        relation_mode: evidence.study_design.relation_mode_hint.clone(),
        locally_valid: false,
        validation_errors: 0,
        draft_path: accepted_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: root.display().to_string(),
        error: String::new(),
    })
}

async fn run_factor_graph_stage1(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts
        .output_dir
        .join("study_factor_graph_stage1_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!(
                "[{}/{}] {} FactorGraph v2-stage1",
                i + 1,
                accessions.len(),
                accession
            );
        }
        match run_one_factor_graph_stage1(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> terminal={} model_calls={} accepted_nodes={} open_questions={}",
                        row.terminal_status, row.turns, row.branches, row.open_questions
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> FactorGraph v2-stage1 error: {err:#}");
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
    let successful = rows.iter().filter(|row| row.status == "success").count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts: 0,
        incomplete_drafts: 0,
        total_validation_errors: 0,
        total_agent_turns: rows.iter().map(|row| row.turns).sum(),
        total_tool_actions: 0,
        total_validator_cycles: 0,
        results_tsv: results_path.display().to_string(),
        workspace_root: opts
            .output_dir
            .join("study_factor_graphs")
            .display()
            .to_string(),
    };
    let rendered = serde_json::to_string_pretty(&summary)?;
    fs::write(
        opts.output_dir
            .join("study_factor_graph_stage1_summary.json"),
        &rendered,
    )?;
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        rendered,
    )?;
    Ok(summary)
}

fn factor_phase_b_concept_types() -> Vec<&'static str> {
    vec![
        "organism_part",
        "disease",
        "cell_type",
        "cell_line",
        "sample_type",
        "isolation_method",
        "acquisition_mode",
        "labeling",
        "instrument",
        "cleavage_agent",
        "control_role",
        "biological_condition",
    ]
}

fn factor_phase_b_schema() -> Value {
    json!({
        "type":"object",
        "properties":{
            "decision":{"type":"string","enum":["propose_observations","human_review"]},
            "observations":{
                "type":"array",
                "maxItems":48,
                "items":{
                    "type":"object",
                    "properties":{
                        "target_factor_id":{"type":"string","pattern":"^[MRA][0-9]{3}$"},
                        "concept_type":{"type":"string","enum":factor_phase_b_concept_types()},
                        "observed_value":{"type":"string","minLength":1,"maxLength":1200},
                        "evidence_refs":{
                            "type":"array",
                            "items":{"type":"string","pattern":"^E[0-9]{4}$"},
                            "minItems":1,
                            "maxItems":16
                        },
                        "confidence":{"type":"string","enum":["high","medium","low"]},
                        "notes":{"type":"string","maxLength":1000}
                    },
                    "required":["target_factor_id","concept_type","observed_value","evidence_refs","confidence","notes"],
                    "additionalProperties":false
                }
            },
            "open_questions":{"type":"array","items":{"type":"string","maxLength":700},"maxItems":24},
            "reason":{"type":"string","maxLength":1800}
        },
        "required":["decision","observations","open_questions","reason"],
        "additionalProperties":false
    })
}

fn factor_graph_node_kind(graph: &StudyFactorGraphAcceptance, id: &str) -> Option<&'static str> {
    if graph.materials.iter().any(|node| node.id == id) {
        Some("material")
    } else if graph.regimes.iter().any(|node| node.id == id) {
        Some("regime")
    } else if graph.acquisitions.iter().any(|node| node.id == id) {
        Some("acquisition")
    } else {
        None
    }
}

fn factor_concept_allowed_for_kind(kind: &str, concept: &str) -> bool {
    match kind {
        "material" => matches!(
            concept,
            "organism_part"
                | "disease"
                | "cell_type"
                | "cell_line"
                | "sample_type"
                | "control_role"
                | "biological_condition"
        ),
        "regime" => matches!(
            concept,
            "isolation_method"
                | "sample_type"
                | "labeling"
                | "cleavage_agent"
                | "control_role"
                | "biological_condition"
        ),
        "acquisition" => matches!(concept, "acquisition_mode" | "instrument"),
        _ => false,
    }
}

fn factor_observation_project_safe(
    graph: &StudyFactorGraphAcceptance,
    target_factor_id: &str,
    concept_type: &str,
) -> bool {
    match factor_graph_node_kind(graph, target_factor_id) {
        Some("material") => {
            if graph.materials.len() != 1 {
                return false;
            }
            if matches!(concept_type, "sample_type" | "control_role") {
                graph.regimes.len() <= 1
            } else {
                true
            }
        }
        Some("regime") => {
            if graph.regimes.len() != 1 {
                return false;
            }
            let material_ids = graph
                .materials
                .iter()
                .map(|node| node.id.as_str())
                .collect::<BTreeSet<_>>();
            let related_materials = graph
                .relations
                .iter()
                .filter(|relation| {
                    relation.relation_type == "material_to_regime"
                        && relation.target_id == target_factor_id
                })
                .map(|relation| relation.source_id.as_str())
                .collect::<BTreeSet<_>>();
            material_ids.is_empty() || related_materials == material_ids
        }
        Some("acquisition") => {
            if graph.acquisitions.len() != 1 {
                return false;
            }
            let regime_ids = graph
                .regimes
                .iter()
                .map(|node| node.id.as_str())
                .collect::<BTreeSet<_>>();
            let related_regimes = graph
                .relations
                .iter()
                .filter(|relation| {
                    relation.relation_type == "regime_to_acquisition"
                        && relation.target_id == target_factor_id
                })
                .map(|relation| relation.source_id.as_str())
                .collect::<BTreeSet<_>>();
            regime_ids.is_empty() || related_regimes == regime_ids
        }
        _ => false,
    }
}

fn factor_phase_b_prompt(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
    graph: &StudyFactorGraphAcceptance,
) -> String {
    let evidence_block = study_graph_stage1_evidence_block(
        evidence,
        opts.max_evidence_items.min(48),
        opts.max_evidence_chars.min(60000),
    );
    let graph_json = serde_json::to_string_pretty(graph).unwrap_or_else(|_| "{}".into());
    format!(
        "You are performing bounded PHASE B scientific observation synthesis for PRIDE single-cell proteomics dataset {acc}.\n\n\
The prerequisite factor graph below has already been accepted by Rust. You MUST use its canonical M###, R###, and A### IDs exactly. You cannot create, rename, merge, or infer factor IDs.\n\n\
THIS IS ONE SHOT. There is no conversational repair loop. Return source-faithful observations or human_review.\n\n\
OBSERVATION CONTRACT:\n\
- Record what the trusted source actually says. Do NOT choose SDRF controlled-vocabulary values. Rust owns canonicalization, template-gap detection, conflicts, row construction, and validation.\n\
- Preserve source terminology. Do not expand or normalize acronyms unless the cited source explicitly supplies that expansion.\n\
- target_factor_id MUST be an existing accepted factor ID.\n\
- isolation_method observations target R### experimental-regime nodes. Preserve a source-faithful cell isolation/loading/sampling operation represented by the regime even when Rust may later classify it unresolved or template_gap. A prepared digest being injected into CE/LC is NOT a single-cell isolation method.\n\
- acquisition_mode means the proteomics acquisition strategy (for example DIA/DDA), not a metabolomics-only direct ESI workflow. It targets A### nodes.\n\
- instrument observations target A### nodes.\n\
- organism_part, disease, cell_type, cell_line and biological_condition observations target M### material nodes.\n\
- sample_type may target the material or regime only when the cited source directly supports that scope.\n\
- Do not restate organism identity merely because it is already present in the factor graph.\n\
- Do not infer any RAW-file mapping from filenames. RAW mapping is outside Phase B.\n\
- Every observation requires field-relevant E#### evidence. If a field cannot be supported, omit it rather than guessing.\n\n\
ACCEPTED FACTOR GRAPH:\n{graph_json}\n\n\
TRUSTED EVIDENCE:\n{evidence_block}\n\n\
Return ONLY the FactorObservationBundleProposal JSON object matching the schema.",
        acc = evidence.accession,
        graph_json = graph_json,
        evidence_block = evidence_block,
    )
}

async fn call_factor_phase_b(
    opts: &SdrfScientificAgentOptions,
    evidence: &DatasetEvidence,
    graph: &StudyFactorGraphAcceptance,
) -> Result<FactorObservationBundleProposal> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": factor_phase_b_prompt(opts, evidence, graph),
        "stream": false,
        "think": false,
        "format": factor_phase_b_schema(),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| format!("Ollama factor Phase-B request for {}", evidence.accession))?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .context("decode Ollama factor Phase-B response")?;
    if !status.is_success() {
        bail!("Ollama factor Phase-B HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty factor Phase-B response");
    }
    serde_json::from_str(raw).context("parse structured factor Phase-B observation bundle")
}

fn accept_factor_phase_b_observations(
    evidence: &DatasetEvidence,
    graph: &StudyFactorGraphAcceptance,
    proposal: &FactorObservationBundleProposal,
) -> FactorPhaseBAcceptance {
    if proposal.decision == "human_review" {
        return FactorPhaseBAcceptance {
            harness_version: SCIENTIFIC_AGENT_FACTOR_PHASE_B_VERSION.into(),
            accession: evidence.accession.clone(),
            status: "human_review".into(),
            observations: Vec::new(),
            rejected_observations: Vec::new(),
            open_questions: proposal.open_questions.clone(),
            reason: proposal.reason.clone(),
            model_calls: 1,
        };
    }

    let allowed_concepts = factor_phase_b_concept_types()
        .into_iter()
        .collect::<BTreeSet<_>>();
    let mut prelim = Vec::new();
    let mut rejected = Vec::new();

    for obs in &proposal.observations {
        let target = obs.target_factor_id.trim().to_ascii_uppercase();
        let concept = obs.concept_type.trim().to_ascii_lowercase();
        let value = normalize_study_graph_text(&obs.observed_value);
        let Some(kind) = factor_graph_node_kind(graph, &target) else {
            rejected.push(RejectedFactorObservation {
                target_factor_id: target,
                concept_type: concept,
                observed_value: value,
                reason: "observation rejected: target factor ID is not present in the accepted Rust-owned factor graph".into(),
            });
            continue;
        };
        if !allowed_concepts.contains(concept.as_str())
            || !factor_concept_allowed_for_kind(kind, &concept)
        {
            rejected.push(RejectedFactorObservation {
                target_factor_id: target,
                concept_type: concept,
                observed_value: value,
                reason: format!(
                    "observation rejected: concept is not valid for {kind} factor scope"
                ),
            });
            continue;
        }
        if value.is_empty() || canonical_reserved_alias(&value).is_some() {
            rejected.push(RejectedFactorObservation {
                target_factor_id: target,
                concept_type: concept,
                observed_value: value,
                reason: "observation rejected: source-faithful value is empty or only a reserved placeholder".into(),
            });
            continue;
        }
        let refs = field_relevant_claim_refs(evidence, &concept, &obs.evidence_refs);
        if refs.is_empty() {
            rejected.push(RejectedFactorObservation {
                target_factor_id: target,
                concept_type: concept,
                observed_value: value,
                reason: "observation rejected: no field-relevant trusted evidence refs survived Rust validation".into(),
            });
            continue;
        }
        let confidence = match obs.confidence.trim().to_ascii_lowercase().as_str() {
            "high" => "high",
            "medium" => "medium",
            "low" => "low",
            _ => "low",
        };
        let projection_scope = if factor_observation_project_safe(graph, &target, &concept) {
            "project".into()
        } else {
            "factor".into()
        };
        prelim.push(AcceptedFactorObservation {
            target_factor_id: target.clone(),
            target_factor_kind: kind.into(),
            concept_type: concept,
            observed_value: value,
            evidence_refs: refs,
            confidence: confidence.into(),
            projection_scope,
            notes: normalize_study_graph_text(&obs.notes),
        });
    }

    let mut grouped: BTreeMap<(String, String), Vec<AcceptedFactorObservation>> = BTreeMap::new();
    for obs in prelim {
        grouped
            .entry((obs.target_factor_id.clone(), obs.concept_type.clone()))
            .or_default()
            .push(obs);
    }
    let mut accepted = Vec::new();
    for ((_target, _concept), mut values) in grouped {
        let distinct = values
            .iter()
            .map(|obs| obs.observed_value.trim().to_ascii_lowercase())
            .collect::<BTreeSet<_>>();
        if distinct.len() > 1 {
            for obs in values {
                rejected.push(RejectedFactorObservation {
                    target_factor_id: obs.target_factor_id,
                    concept_type: obs.concept_type,
                    observed_value: obs.observed_value,
                    reason: "observation rejected: one factor/concept identity received conflicting source-faithful values in the same one-shot bundle".into(),
                });
            }
            continue;
        }
        let mut first = values.remove(0);
        for duplicate in values {
            first.evidence_refs.extend(duplicate.evidence_refs);
        }
        first.evidence_refs.sort();
        first.evidence_refs.dedup();
        accepted.push(first);
    }
    accepted.sort_by(|a, b| {
        (&a.target_factor_id, &a.concept_type, &a.observed_value).cmp(&(
            &b.target_factor_id,
            &b.concept_type,
            &b.observed_value,
        ))
    });

    let status = if accepted.is_empty() {
        "human_review"
    } else {
        "accepted"
    };
    let reason = if status == "accepted" {
        format!(
            "Rust accepted {} factor-scoped source-faithful observation(s) from one bounded model call",
            accepted.len()
        )
    } else {
        "Rust could not accept any source-faithful Phase-B observations; stop at human review"
            .into()
    };

    FactorPhaseBAcceptance {
        harness_version: SCIENTIFIC_AGENT_FACTOR_PHASE_B_VERSION.into(),
        accession: evidence.accession.clone(),
        status: status.into(),
        observations: accepted,
        rejected_observations: rejected,
        open_questions: proposal.open_questions.clone(),
        reason,
        model_calls: 1,
    }
}

fn factor_graph_as_agent_branches(graph: &StudyFactorGraphAcceptance) -> Vec<AgentBranch> {
    let mut result = Vec::new();
    let mut add = |id: &str, label: &str, refs: &[String]| {
        let mut raw_files = graph
            .raw_links
            .iter()
            .filter(|link| link.node_id == id)
            .map(|link| link.raw_file.clone())
            .collect::<Vec<_>>();
        raw_files.sort();
        raw_files.dedup();
        result.push(AgentBranch {
            id: id.into(),
            label: label.into(),
            status: "supported".into(),
            evidence_refs: refs.to_vec(),
            linked_raw_files: raw_files.clone(),
            linkage_status: if raw_files.is_empty() {
                "unresolved".into()
            } else {
                "supported".into()
            },
            notes: "Rust-owned factor node exposed to the deterministic compiler as scoped scientific structure".into(),
        });
    };
    for node in &graph.materials {
        add(&node.id, &node.label, &node.evidence_refs);
    }
    for node in &graph.regimes {
        add(&node.id, &node.label, &node.evidence_refs);
    }
    for node in &graph.acquisitions {
        add(&node.id, &node.label, &node.evidence_refs);
    }
    result
}

fn factor_observation_as_claim(obs: &AcceptedFactorObservation) -> ScientificClaim {
    let project = obs.projection_scope == "project";
    ScientificClaim {
        concept_type: obs.concept_type.clone(),
        value: obs.observed_value.clone(),
        scope: if project {
            "project".into()
        } else {
            "branch".into()
        },
        branch_id: if project {
            String::new()
        } else {
            obs.target_factor_id.clone()
        },
        status: "supported".into(),
        evidence_refs: obs.evidence_refs.clone(),
        confidence: obs.confidence.clone(),
        reason: format!(
            "factor-scoped Phase-B observation on {}: {}",
            obs.target_factor_id, obs.notes
        ),
    }
}

fn load_factor_graph_for_phase_b(accession: &str) -> Result<(PathBuf, StudyFactorGraphAcceptance)> {
    let root = std::env::var(SCIENTIFIC_AGENT_FACTOR_GRAPH_ROOT_ENV).with_context(|| {
        format!(
            "{} must point to a completed v2 factor Stage-1 output root",
            SCIENTIFIC_AGENT_FACTOR_GRAPH_ROOT_ENV
        )
    })?;
    let path = PathBuf::from(root)
        .join("study_factor_graphs")
        .join(accession)
        .join("accepted_graph.json");
    if !path.is_file() {
        bail!(
            "accepted factor graph not found for {accession}: {}",
            path.display()
        );
    }
    let graph: StudyFactorGraphAcceptance = serde_json::from_str(&fs::read_to_string(&path)?)
        .with_context(|| format!("parse accepted factor graph {}", path.display()))?;
    if graph.harness_version != SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION {
        bail!(
            "factor graph for {accession} has harness_version='{}', expected '{}'",
            graph.harness_version,
            SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION
        );
    }
    if graph.accession != accession {
        bail!(
            "factor graph accession mismatch: expected {accession}, found {}",
            graph.accession
        );
    }
    if graph.status != "accepted" {
        bail!(
            "factor graph for {accession} is not accepted: status={}",
            graph.status
        );
    }
    Ok((path, graph))
}

fn write_factor_phase_b_review(
    path: &Path,
    graph: &StudyFactorGraphAcceptance,
    acceptance: &FactorPhaseBAcceptance,
    adjudications: &[ClaimAdjudicationRecord],
    validation: &AgentValidationCycle,
    terminal_status: &str,
) -> Result<()> {
    let mut out = String::new();
    out.push_str(&format!(
        "# PRIDE-SCP v2 Factor Phase B: {}\n\n",
        acceptance.accession
    ));
    out.push_str(&format!("Terminal status: **{}**\n\n", terminal_status));
    out.push_str(&format!(
        "Accepted factor graph: materials={}, regimes={}, acquisitions={}, relations={}\n\n",
        graph.materials.len(),
        graph.regimes.len(),
        graph.acquisitions.len(),
        graph.relations.len()
    ));
    out.push_str("## Accepted source-faithful observations\n\n");
    if acceptance.observations.is_empty() {
        out.push_str("None.\n");
    } else {
        for obs in &acceptance.observations {
            out.push_str(&format!(
                "- {} [{}] {}='{}' | scope={} | evidence={:?} | confidence={}\n",
                obs.target_factor_id,
                obs.target_factor_kind,
                obs.concept_type,
                obs.observed_value,
                obs.projection_scope,
                obs.evidence_refs,
                obs.confidence
            ));
        }
    }
    out.push_str("\n## Rust adjudications\n\n");
    out.push_str(&serde_json::to_string_pretty(adjudications)?);
    out.push_str("\n\n## One-pass validation\n\n");
    out.push_str(&format!(
        "errors={} warnings={}\n\n",
        validation.validation_errors, validation.validation_warnings
    ));
    for message in &validation.representative_messages {
        out.push_str(&format!("- {}\n", message));
    }
    if !acceptance.open_questions.is_empty() {
        out.push_str("\n## Open questions\n\n");
        for question in &acceptance.open_questions {
            out.push_str(&format!("- {}\n", question));
        }
    }
    fs::write(path, out)?;
    Ok(())
}

fn factor_text_has_token(text: &str, token: &str) -> bool {
    text.split(|ch: char| !ch.is_ascii_alphanumeric())
        .any(|part| part.eq_ignore_ascii_case(token))
}

fn regime_is_source_cell_handling(regime: &AcceptedFactorRegime) -> bool {
    let text = format!(
        "{} {} {} {} {}",
        regime.label,
        regime.experimental_role,
        regime.isolation_or_loading_method,
        regime.input_or_cell_count_regime,
        regime.notes
    )
    .to_ascii_lowercase();
    let cellular = [
        "single-cell",
        "single cell",
        "single cells",
        "intact cell",
        "intact cells",
        "low-number cell",
        "cells",
        "subcellular",
    ]
    .iter()
    .any(|needle| text.contains(needle));
    let handling = [
        "isolat",
        "hydrodynamic",
        "spray voltage",
        "microsampl",
        "aspirat",
        "microwell",
        "pick",
        "microfluidic",
        "disco",
    ]
    .iter()
    .any(|needle| text.contains(needle));
    cellular && handling
}

fn regime_supports_single_cell_sample_type(regime: &AcceptedFactorRegime) -> bool {
    let value = regime
        .input_or_cell_count_regime
        .trim()
        .to_ascii_lowercase();
    (value.contains("single-cell")
        || value.contains("single cell")
        || value.contains("single cells"))
        && !value.contains("subcellular")
}

fn material_role_is_reference_or_control(role: &str) -> bool {
    let value = role.to_ascii_lowercase();
    value.contains("reference") || value.contains("control") || value.contains("validation")
}

fn acquisition_exposes_proteomics_mode(acquisition: &AcceptedFactorAcquisition) -> bool {
    let value = acquisition
        .acquisition_method_or_platform
        .to_ascii_lowercase();
    value.contains("data independent")
        || value.contains("data-independent")
        || value.contains("data dependent")
        || value.contains("data-dependent")
        || factor_text_has_token(&value, "dia")
        || factor_text_has_token(&value, "dda")
}

fn push_deterministic_factor_observation(
    evidence: &DatasetEvidence,
    graph: &StudyFactorGraphAcceptance,
    observations: &mut Vec<AcceptedFactorObservation>,
    rejected: &mut Vec<RejectedFactorObservation>,
    target_factor_id: &str,
    target_factor_kind: &str,
    concept_type: &str,
    observed_value: &str,
    evidence_refs: &[String],
    notes: &str,
) {
    let value = normalize_study_graph_text(observed_value);
    let refs = valid_evidence_refs(evidence, evidence_refs);
    if value.is_empty() || refs.is_empty() {
        rejected.push(RejectedFactorObservation {
            target_factor_id: target_factor_id.into(),
            concept_type: concept_type.into(),
            observed_value: value,
            reason: "deterministic bridge rejected factor property because it lacked a concrete value or valid accepted-factor evidence refs".into(),
        });
        return;
    }
    let projection_scope = if factor_observation_project_safe(graph, target_factor_id, concept_type)
    {
        "project".into()
    } else {
        "factor".into()
    };
    observations.push(AcceptedFactorObservation {
        target_factor_id: target_factor_id.into(),
        target_factor_kind: target_factor_kind.into(),
        concept_type: concept_type.into(),
        observed_value: value,
        evidence_refs: refs,
        confidence: "high".into(),
        projection_scope,
        notes: normalize_study_graph_text(notes),
    });
}

fn derive_factor_graph_observations(
    evidence: &DatasetEvidence,
    graph: &StudyFactorGraphAcceptance,
) -> FactorPhaseBAcceptance {
    let mut observations = Vec::new();
    let mut rejected = Vec::new();

    for material in &graph.materials {
        push_deterministic_factor_observation(
            evidence,
            graph,
            &mut observations,
            &mut rejected,
            &material.id,
            "material",
            "organism",
            &material.organism,
            &material.evidence_refs,
            "deterministically derived from accepted MaterialNode.organism",
        );
        if material_role_is_reference_or_control(&material.experimental_role) {
            push_deterministic_factor_observation(
                evidence,
                graph,
                &mut observations,
                &mut rejected,
                &material.id,
                "material",
                "control_role",
                &material.experimental_role,
                &material.evidence_refs,
                "deterministically derived from an accepted material experimental role explicitly describing reference/control/validation use",
            );
        }
    }

    for regime in &graph.regimes {
        if regime_is_source_cell_handling(regime) {
            push_deterministic_factor_observation(
                evidence,
                graph,
                &mut observations,
                &mut rejected,
                &regime.id,
                "regime",
                "isolation_method",
                &regime.isolation_or_loading_method,
                &regime.evidence_refs,
                "deterministically derived from accepted ExperimentalRegimeNode.isolation_or_loading_method; no second LLM reinterpretation",
            );
        }
        if regime_supports_single_cell_sample_type(regime) {
            push_deterministic_factor_observation(
                evidence,
                graph,
                &mut observations,
                &mut rejected,
                &regime.id,
                "regime",
                "sample_type",
                "single cell",
                &regime.evidence_refs,
                "deterministically derived from an accepted explicit single-cell input regime",
            );
        }
    }

    for acquisition in &graph.acquisitions {
        if acquisition_exposes_proteomics_mode(acquisition) {
            push_deterministic_factor_observation(
                evidence,
                graph,
                &mut observations,
                &mut rejected,
                &acquisition.id,
                "acquisition",
                "acquisition_mode",
                &acquisition.acquisition_method_or_platform,
                &acquisition.evidence_refs,
                "deterministically derived from accepted AcquisitionNode acquisition text containing an explicit DIA/DDA mode",
            );
        }
    }

    observations.sort_by(|a, b| {
        (&a.target_factor_id, &a.concept_type, &a.observed_value).cmp(&(
            &b.target_factor_id,
            &b.concept_type,
            &b.observed_value,
        ))
    });
    observations.dedup_by(|a, b| {
        a.target_factor_id == b.target_factor_id
            && a.concept_type == b.concept_type
            && a.observed_value.eq_ignore_ascii_case(&b.observed_value)
    });

    let status = if observations.is_empty() {
        "human_review"
    } else {
        "accepted"
    };
    FactorPhaseBAcceptance {
        harness_version: SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_VERSION.into(),
        accession: evidence.accession.clone(),
        status: status.into(),
        observations,
        rejected_observations: rejected,
        open_questions: graph.open_questions.clone(),
        reason: if status == "accepted" {
            "Rust deterministically derived source-faithful annotation observations from the already accepted factor graph; no Phase-B LLM call was made".into()
        } else {
            "accepted factor graph contained no safely bridgeable source-faithful annotation properties; stop at human review without invoking another model".into()
        },
        model_calls: 0,
    }
}

fn write_factor_deterministic_bridge_review(
    path: &Path,
    graph: &StudyFactorGraphAcceptance,
    acceptance: &FactorPhaseBAcceptance,
    adjudications: &[ClaimAdjudicationRecord],
    validation: &AgentValidationCycle,
    terminal_status: &str,
) -> Result<()> {
    let mut out = String::new();
    out.push_str(&format!(
        "# PRIDE-SCP v2 Deterministic Factor Bridge: {}\n\n",
        acceptance.accession
    ));
    out.push_str(&format!("Terminal status: **{}**\n\n", terminal_status));
    out.push_str("Model calls in this stage: **0**\n\n");
    out.push_str(&format!(
        "Frozen accepted factor graph: materials={}, regimes={}, acquisitions={}, relations={}\n\n",
        graph.materials.len(),
        graph.regimes.len(),
        graph.acquisitions.len(),
        graph.relations.len()
    ));
    out.push_str("## Deterministically derived source observations\n\n");
    if acceptance.observations.is_empty() {
        out.push_str("None.\n");
    } else {
        for obs in &acceptance.observations {
            out.push_str(&format!(
                "- {} [{}] {}='{}' | scope={} | evidence={:?}\n",
                obs.target_factor_id,
                obs.target_factor_kind,
                obs.concept_type,
                obs.observed_value,
                obs.projection_scope,
                obs.evidence_refs
            ));
        }
    }
    out.push_str("\n## Rust adjudications\n\n");
    out.push_str(&serde_json::to_string_pretty(adjudications)?);
    out.push_str("\n\n## One-pass validation\n\n");
    out.push_str(&format!(
        "errors={} warnings={}\n\n",
        validation.validation_errors, validation.validation_warnings
    ));
    for message in &validation.representative_messages {
        out.push_str(&format!("- {}\n", message));
    }
    fs::write(path, out)?;
    Ok(())
}

async fn run_one_factor_phase_b(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let evidence = build_evidence(&annotate_opts, accession)?;
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
    let (graph_path, graph) = load_factor_graph_for_phase_b(accession)?;
    let root = opts.output_dir.join("phase_b").join(accession);
    fs::create_dir_all(&root)?;
    let evidence_path = root.join("evidence.json");
    let proposal_path = root.join("observation_bundle.proposal.json");
    let accepted_path = root.join("accepted_observations.json");
    let adjudications_path = root.join("adjudications.json");
    let draft_path = root.join(format!("{}.phase_b.sdrf.tsv", accession));
    let validation_path = root.join("VALIDATION.md");
    let review_path = root.join("REVIEW.md");
    let result_path = root.join("result.json");
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
    fs::copy(&graph_path, root.join("accepted_factor_graph.json"))?;

    let proposal = call_factor_phase_b(opts, &evidence, &graph).await?;
    fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    let acceptance = accept_factor_phase_b_observations(&evidence, &graph, &proposal);
    fs::write(&accepted_path, serde_json::to_string_pretty(&acceptance)?)?;

    let claims = acceptance
        .observations
        .iter()
        .map(factor_observation_as_claim)
        .collect::<Vec<_>>();
    let state = ScientificWorkspaceState {
        harness_version: SCIENTIFIC_AGENT_FACTOR_PHASE_B_VERSION.into(),
        accession: accession.into(),
        turn: 1,
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
        branches: factor_graph_as_agent_branches(&graph),
        claims,
        open_questions: acceptance.open_questions.clone(),
        active_task_id: String::new(),
        next_step: "compile".into(),
        notes: "bounded factor-scoped Phase-B state; no conversational retry is permitted".into(),
        ..Default::default()
    };

    let compiled = compile_workspace(&evidence, &state, &explicit_mappings)?;
    write_sdrf(&draft_path, &compiled.headers, &compiled.rows)?;
    write_validation_review(&validation_path, &compiled.issues)?;
    let validation = validation_cycle(1, &compiled.issues);
    let adjudications = workspace_adjudications(&evidence, &state);
    fs::write(
        &adjudications_path,
        serde_json::to_string_pretty(&adjudications)?,
    )?;

    let has_template_gap = adjudications
        .iter()
        .any(|record| matches!(&record.adjudication, ClaimAdjudication::TemplateGap { .. }));
    let terminal_status = if validation.validation_errors == 0 {
        "locally_valid"
    } else if has_template_gap {
        "partial_template_gap"
    } else if acceptance.status == "accepted" && !acceptance.observations.is_empty() {
        "partial_human_review"
    } else {
        "human_review"
    };

    write_factor_phase_b_review(
        &review_path,
        &graph,
        &acceptance,
        &adjudications,
        &validation,
        terminal_status,
    )?;
    let result = json!({
        "harness_version": SCIENTIFIC_AGENT_FACTOR_PHASE_B_VERSION,
        "accession": accession,
        "factor_graph_source": graph_path,
        "model_calls": 1,
        "tool_actions": 0,
        "validator_cycles": 1,
        "terminal_status": terminal_status,
        "accepted_observation_count": acceptance.observations.len(),
        "rejected_observation_count": acceptance.rejected_observations.len(),
        "accepted_observations": &acceptance.observations,
        "rejected_observations": &acceptance.rejected_observations,
        "adjudications": &adjudications,
        "validation": &validation,
        "compiled_fingerprint": &compiled.fingerprint,
        "draft_path": draft_path.display().to_string(),
        "review_path": review_path.display().to_string()
    });
    fs::write(&result_path, serde_json::to_string_pretty(&result)?)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: terminal_status.into(),
        turns: 1,
        tool_actions: 0,
        validator_cycles: 1,
        branches: graph.materials.len() + graph.regimes.len() + graph.acquisitions.len(),
        open_questions: acceptance.open_questions.len(),
        relation_mode: compiled.proposal.relation_mode,
        locally_valid: validation.validation_errors == 0,
        validation_errors: validation.validation_errors,
        draft_path: draft_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: root.display().to_string(),
        error: String::new(),
    })
}

async fn run_factor_phase_b(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts.output_dir.join("factor_phase_b_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!(
                "[{}/{}] {} FactorGraph v2 Phase B",
                i + 1,
                accessions.len(),
                accession
            );
        }
        match run_one_factor_phase_b(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> terminal={} model_calls={} validators={} valid={} errors={}",
                        row.terminal_status,
                        row.turns,
                        row.validator_cycles,
                        row.locally_valid,
                        row.validation_errors
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> FactorGraph v2 Phase-B error: {err:#}");
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
    let successful = rows.iter().filter(|row| row.status == "success").count();
    let locally_valid_drafts = rows.iter().filter(|row| row.locally_valid).count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_FACTOR_PHASE_B_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts,
        incomplete_drafts: successful.saturating_sub(locally_valid_drafts),
        total_validation_errors: rows.iter().map(|row| row.validation_errors).sum(),
        total_agent_turns: rows.iter().map(|row| row.turns).sum(),
        total_tool_actions: 0,
        total_validator_cycles: rows.iter().map(|row| row.validator_cycles).sum(),
        results_tsv: results_path.display().to_string(),
        workspace_root: opts.output_dir.join("phase_b").display().to_string(),
    };
    let rendered = serde_json::to_string_pretty(&summary)?;
    fs::write(
        opts.output_dir.join("factor_phase_b_summary.json"),
        &rendered,
    )?;
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        rendered,
    )?;
    Ok(summary)
}

async fn run_one_factor_deterministic_bridge(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let evidence = build_evidence(&annotate_opts, accession)?;
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
    let (graph_path, graph) = load_factor_graph_for_phase_b(accession)?;
    let root = opts.output_dir.join("deterministic_bridge").join(accession);
    fs::create_dir_all(&root)?;
    let evidence_path = root.join("evidence.json");
    let accepted_path = root.join("derived_observations.json");
    let adjudications_path = root.join("adjudications.json");
    let draft_path = root.join(format!("{}.deterministic_bridge.sdrf.tsv", accession));
    let validation_path = root.join("VALIDATION.md");
    let review_path = root.join("REVIEW.md");
    let result_path = root.join("result.json");
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
    fs::copy(&graph_path, root.join("accepted_factor_graph.json"))?;

    let acceptance = derive_factor_graph_observations(&evidence, &graph);
    fs::write(&accepted_path, serde_json::to_string_pretty(&acceptance)?)?;

    let claims = acceptance
        .observations
        .iter()
        .map(factor_observation_as_claim)
        .collect::<Vec<_>>();
    let state = ScientificWorkspaceState {
        harness_version: SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_VERSION.into(),
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
        branches: factor_graph_as_agent_branches(&graph),
        claims,
        open_questions: acceptance.open_questions.clone(),
        active_task_id: String::new(),
        next_step: "compile".into(),
        notes: "deterministic factor-to-observation bridge; no LLM call and no conversational retry are permitted".into(),
        ..Default::default()
    };

    let compiled = compile_workspace(&evidence, &state, &explicit_mappings)?;
    write_sdrf(&draft_path, &compiled.headers, &compiled.rows)?;
    write_validation_review(&validation_path, &compiled.issues)?;
    let validation = validation_cycle(1, &compiled.issues);
    let adjudications = workspace_adjudications(&evidence, &state);
    fs::write(
        &adjudications_path,
        serde_json::to_string_pretty(&adjudications)?,
    )?;

    let has_template_gap = adjudications
        .iter()
        .any(|record| matches!(&record.adjudication, ClaimAdjudication::TemplateGap { .. }));
    let terminal_status = if validation.validation_errors == 0 {
        "locally_valid"
    } else if has_template_gap {
        "partial_template_gap"
    } else if !acceptance.observations.is_empty() {
        "partial_human_review"
    } else {
        "human_review"
    };

    write_factor_deterministic_bridge_review(
        &review_path,
        &graph,
        &acceptance,
        &adjudications,
        &validation,
        terminal_status,
    )?;
    let result = json!({
        "harness_version": SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_VERSION,
        "accession": accession,
        "factor_graph_source": graph_path,
        "model_calls": 0,
        "tool_actions": 0,
        "validator_cycles": 1,
        "terminal_status": terminal_status,
        "derived_observation_count": acceptance.observations.len(),
        "rejected_observation_count": acceptance.rejected_observations.len(),
        "derived_observations": &acceptance.observations,
        "rejected_observations": &acceptance.rejected_observations,
        "adjudications": &adjudications,
        "validation": &validation,
        "compiled_fingerprint": &compiled.fingerprint,
        "draft_path": draft_path.display().to_string(),
        "review_path": review_path.display().to_string()
    });
    fs::write(&result_path, serde_json::to_string_pretty(&result)?)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: terminal_status.into(),
        turns: 0,
        tool_actions: 0,
        validator_cycles: 1,
        branches: graph.materials.len() + graph.regimes.len() + graph.acquisitions.len(),
        open_questions: acceptance.open_questions.len(),
        relation_mode: compiled.proposal.relation_mode,
        locally_valid: validation.validation_errors == 0,
        validation_errors: validation.validation_errors,
        draft_path: draft_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: root.display().to_string(),
        error: String::new(),
    })
}

async fn run_factor_deterministic_bridge(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts
        .output_dir
        .join("factor_deterministic_bridge_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!(
                "[{}/{}] {} FactorGraph v2 deterministic bridge",
                i + 1,
                accessions.len(),
                accession
            );
        }
        match run_one_factor_deterministic_bridge(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> terminal={} model_calls=0 validators={} valid={} errors={}",
                        row.terminal_status,
                        row.validator_cycles,
                        row.locally_valid,
                        row.validation_errors
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> FactorGraph deterministic bridge error: {err:#}");
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
    let successful = rows.iter().filter(|row| row.status == "success").count();
    let locally_valid_drafts = rows.iter().filter(|row| row.locally_valid).count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts,
        incomplete_drafts: successful.saturating_sub(locally_valid_drafts),
        total_validation_errors: rows.iter().map(|row| row.validation_errors).sum(),
        total_agent_turns: 0,
        total_tool_actions: 0,
        total_validator_cycles: rows.iter().map(|row| row.validator_cycles).sum(),
        results_tsv: results_path.display().to_string(),
        workspace_root: opts
            .output_dir
            .join("deterministic_bridge")
            .display()
            .to_string(),
    };
    let rendered = serde_json::to_string_pretty(&summary)?;
    fs::write(
        opts.output_dir
            .join("factor_deterministic_bridge_summary.json"),
        &rendered,
    )?;
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        rendered,
    )?;
    Ok(summary)
}

async fn run_one_factor_canonical_hardened(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let evidence = build_evidence(&annotate_opts, accession)?;
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
    let (graph_path, graph) = load_factor_graph_for_phase_b(accession)?;
    let root = opts
        .output_dir
        .join("canonicalization_hardened")
        .join(accession);
    fs::create_dir_all(&root)?;
    let evidence_path = root.join("evidence.json");
    let accepted_path = root.join("derived_observations.json");
    let adjudications_path = root.join("adjudications.json");
    let draft_path = root.join(format!("{}.canonicalization_hardened.sdrf.tsv", accession));
    let validation_path = root.join("VALIDATION.md");
    let review_path = root.join("REVIEW.md");
    let result_path = root.join("result.json");
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
    fs::copy(&graph_path, root.join("accepted_factor_graph.json"))?;

    let mut acceptance = derive_factor_graph_observations(&evidence, &graph);
    acceptance.harness_version = SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into();
    fs::write(&accepted_path, serde_json::to_string_pretty(&acceptance)?)?;

    let claims = acceptance
        .observations
        .iter()
        .map(factor_observation_as_claim)
        .collect::<Vec<_>>();
    let state = ScientificWorkspaceState {
        harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
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
        branches: factor_graph_as_agent_branches(&graph),
        claims,
        open_questions: acceptance.open_questions.clone(),
        active_task_id: String::new(),
        next_step: "compile".into(),
        notes: "deterministic factor-to-observation bridge with observation-local canonicalization fidelity hardening; no LLM call and no conversational retry are permitted".into(),
        ..Default::default()
    };

    let compiled = compile_workspace(&evidence, &state, &explicit_mappings)?;
    write_sdrf(&draft_path, &compiled.headers, &compiled.rows)?;
    write_validation_review(&validation_path, &compiled.issues)?;
    let validation = validation_cycle(1, &compiled.issues);
    let adjudications = workspace_adjudications(&evidence, &state);
    fs::write(
        &adjudications_path,
        serde_json::to_string_pretty(&adjudications)?,
    )?;

    let has_template_gap = adjudications
        .iter()
        .any(|record| matches!(&record.adjudication, ClaimAdjudication::TemplateGap { .. }));
    let terminal_status = if validation.validation_errors == 0 {
        "locally_valid"
    } else if has_template_gap {
        "partial_template_gap"
    } else if !acceptance.observations.is_empty() {
        "partial_human_review"
    } else {
        "human_review"
    };

    write_factor_deterministic_bridge_review(
        &review_path,
        &graph,
        &acceptance,
        &adjudications,
        &validation,
        terminal_status,
    )?;
    let result = json!({
        "harness_version": SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION,
        "accession": accession,
        "factor_graph_source": graph_path,
        "model_calls": 0,
        "tool_actions": 0,
        "validator_cycles": 1,
        "terminal_status": terminal_status,
        "derived_observation_count": acceptance.observations.len(),
        "rejected_observation_count": acceptance.rejected_observations.len(),
        "derived_observations": &acceptance.observations,
        "rejected_observations": &acceptance.rejected_observations,
        "adjudications": &adjudications,
        "validation": &validation,
        "compiled_fingerprint": &compiled.fingerprint,
        "draft_path": draft_path.display().to_string(),
        "review_path": review_path.display().to_string()
    });
    fs::write(&result_path, serde_json::to_string_pretty(&result)?)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: terminal_status.into(),
        turns: 0,
        tool_actions: 0,
        validator_cycles: 1,
        branches: graph.materials.len() + graph.regimes.len() + graph.acquisitions.len(),
        open_questions: acceptance.open_questions.len(),
        relation_mode: compiled.proposal.relation_mode,
        locally_valid: validation.validation_errors == 0,
        validation_errors: validation.validation_errors,
        draft_path: draft_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: root.display().to_string(),
        error: String::new(),
    })
}

async fn run_factor_canonical_hardened(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts
        .output_dir
        .join("factor_canonicalization_hardened_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!(
                "[{}/{}] {} FactorGraph v2 canonicalization-hardened bridge",
                i + 1,
                accessions.len(),
                accession
            );
        }
        match run_one_factor_canonical_hardened(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> terminal={} model_calls=0 validators={} valid={} errors={}",
                        row.terminal_status,
                        row.validator_cycles,
                        row.locally_valid,
                        row.validation_errors
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> FactorGraph canonicalization-hardened bridge error: {err:#}");
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
    let successful = rows.iter().filter(|row| row.status == "success").count();
    let locally_valid_drafts = rows.iter().filter(|row| row.locally_valid).count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts,
        incomplete_drafts: successful.saturating_sub(locally_valid_drafts),
        total_validation_errors: rows.iter().map(|row| row.validation_errors).sum(),
        total_agent_turns: 0,
        total_tool_actions: 0,
        total_validator_cycles: rows.iter().map(|row| row.validator_cycles).sum(),
        results_tsv: results_path.display().to_string(),
        workspace_root: opts
            .output_dir
            .join("canonicalization_hardened")
            .display()
            .to_string(),
    };
    let rendered = serde_json::to_string_pretty(&summary)?;
    fs::write(
        opts.output_dir
            .join("factor_canonicalization_hardened_summary.json"),
        &rendered,
    )?;
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        rendered,
    )?;
    Ok(summary)
}

async fn run_one_factor_semantic_fidelity_hardened(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let evidence = build_evidence(&annotate_opts, accession)?;
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
    let (graph_path, graph) = load_factor_graph_for_phase_b(accession)?;
    let root = opts
        .output_dir
        .join("semantic_fidelity_hardened")
        .join(accession);
    fs::create_dir_all(&root)?;
    let evidence_path = root.join("evidence.json");
    let accepted_path = root.join("derived_observations.json");
    let adjudications_path = root.join("adjudications.json");
    let draft_path = root.join(format!("{}.semantic_fidelity_hardened.sdrf.tsv", accession));
    let validation_path = root.join("VALIDATION.md");
    let review_path = root.join("REVIEW.md");
    let result_path = root.join("result.json");
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
    fs::copy(&graph_path, root.join("accepted_factor_graph.json"))?;

    let mut acceptance = derive_factor_graph_observations(&evidence, &graph);
    acceptance.harness_version = SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into();
    fs::write(&accepted_path, serde_json::to_string_pretty(&acceptance)?)?;

    let claims = acceptance
        .observations
        .iter()
        .map(factor_observation_as_claim)
        .collect::<Vec<_>>();
    let state = ScientificWorkspaceState {
        harness_version: SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into(),
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
        branches: factor_graph_as_agent_branches(&graph),
        claims,
        open_questions: acceptance.open_questions.clone(),
        active_task_id: String::new(),
        next_step: "compile".into(),
        notes: "deterministic factor-to-observation bridge with observation-local isolation and acquisition semantic-fidelity hardening; no LLM call and no conversational retry are permitted".into(),
        ..Default::default()
    };

    let compiled = compile_workspace(&evidence, &state, &explicit_mappings)?;
    write_sdrf(&draft_path, &compiled.headers, &compiled.rows)?;
    write_validation_review(&validation_path, &compiled.issues)?;
    let validation = validation_cycle(1, &compiled.issues);
    let adjudications = workspace_adjudications(&evidence, &state);
    fs::write(
        &adjudications_path,
        serde_json::to_string_pretty(&adjudications)?,
    )?;

    let has_template_gap = adjudications
        .iter()
        .any(|record| matches!(&record.adjudication, ClaimAdjudication::TemplateGap { .. }));
    let terminal_status = if validation.validation_errors == 0 {
        "locally_valid"
    } else if has_template_gap {
        "partial_template_gap"
    } else if !acceptance.observations.is_empty() {
        "partial_human_review"
    } else {
        "human_review"
    };

    write_factor_deterministic_bridge_review(
        &review_path,
        &graph,
        &acceptance,
        &adjudications,
        &validation,
        terminal_status,
    )?;
    let result = json!({
        "harness_version": SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION,
        "accession": accession,
        "factor_graph_source": graph_path,
        "model_calls": 0,
        "tool_actions": 0,
        "validator_cycles": 1,
        "terminal_status": terminal_status,
        "derived_observation_count": acceptance.observations.len(),
        "rejected_observation_count": acceptance.rejected_observations.len(),
        "derived_observations": &acceptance.observations,
        "rejected_observations": &acceptance.rejected_observations,
        "adjudications": &adjudications,
        "validation": &validation,
        "compiled_fingerprint": &compiled.fingerprint,
        "draft_path": draft_path.display().to_string(),
        "review_path": review_path.display().to_string()
    });
    fs::write(&result_path, serde_json::to_string_pretty(&result)?)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: terminal_status.into(),
        turns: 0,
        tool_actions: 0,
        validator_cycles: 1,
        branches: graph.materials.len() + graph.regimes.len() + graph.acquisitions.len(),
        open_questions: acceptance.open_questions.len(),
        relation_mode: compiled.proposal.relation_mode,
        locally_valid: validation.validation_errors == 0,
        validation_errors: validation.validation_errors,
        draft_path: draft_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: root.display().to_string(),
        error: String::new(),
    })
}

async fn run_factor_semantic_fidelity_hardened(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts
        .output_dir
        .join("factor_semantic_fidelity_hardened_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!(
                "[{}/{}] {} FactorGraph v2 semantic-fidelity-hardened bridge",
                i + 1,
                accessions.len(),
                accession
            );
        }
        match run_one_factor_semantic_fidelity_hardened(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> terminal={} model_calls=0 validators={} valid={} errors={}",
                        row.terminal_status,
                        row.validator_cycles,
                        row.locally_valid,
                        row.validation_errors
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> FactorGraph semantic-fidelity-hardened bridge error: {err:#}");
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
    let successful = rows.iter().filter(|row| row.status == "success").count();
    let locally_valid_drafts = rows.iter().filter(|row| row.locally_valid).count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts,
        incomplete_drafts: successful.saturating_sub(locally_valid_drafts),
        total_validation_errors: rows.iter().map(|row| row.validation_errors).sum(),
        total_agent_turns: 0,
        total_tool_actions: 0,
        total_validator_cycles: rows.iter().map(|row| row.validator_cycles).sum(),
        results_tsv: results_path.display().to_string(),
        workspace_root: opts
            .output_dir
            .join("semantic_fidelity_hardened")
            .display()
            .to_string(),
    };
    let rendered = serde_json::to_string_pretty(&summary)?;
    fs::write(
        opts.output_dir
            .join("factor_semantic_fidelity_hardened_summary.json"),
        &rendered,
    )?;
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        rendered,
    )?;
    Ok(summary)
}

async fn run_one_factor_row_role_hardened(
    opts: &SdrfScientificAgentOptions,
    accession: &str,
) -> Result<ScientificAgentResultRow> {
    let annotate_opts = opts.annotate_options();
    let evidence = build_evidence(&annotate_opts, accession)?;
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
    let (graph_path, graph) = load_factor_graph_for_phase_b(accession)?;
    let root = opts.output_dir.join("row_role_hardened").join(accession);
    fs::create_dir_all(&root)?;
    let evidence_path = root.join("evidence.json");
    let accepted_path = root.join("derived_observations.json");
    let adjudications_path = root.join("adjudications.json");
    let draft_path = root.join(format!("{}.row_role_hardened.sdrf.tsv", accession));
    let validation_path = root.join("VALIDATION.md");
    let review_path = root.join("REVIEW.md");
    let result_path = root.join("result.json");
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
    fs::copy(&graph_path, root.join("accepted_factor_graph.json"))?;

    let mut acceptance = derive_factor_graph_observations(&evidence, &graph);
    acceptance.harness_version = SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION.into();
    fs::write(&accepted_path, serde_json::to_string_pretty(&acceptance)?)?;

    let claims = acceptance
        .observations
        .iter()
        .map(factor_observation_as_claim)
        .collect::<Vec<_>>();
    let state = ScientificWorkspaceState {
        harness_version: SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION.into(),
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
        branches: factor_graph_as_agent_branches(&graph),
        claims,
        open_questions: acceptance.open_questions.clone(),
        active_task_id: String::new(),
        next_step: "compile".into(),
        notes: "deterministic factor-to-observation bridge with observation-local isolation and acquisition semantic-fidelity plus row-role hardening; no LLM call and no conversational retry are permitted".into(),
        ..Default::default()
    };

    let trusted_partial_sdrf = opts
        .resolved_sdrf_dir
        .as_ref()
        .map(|dir| dir.join(format!("{accession}.sdrf.tsv")))
        .filter(|path| {
            evidence.existing_sdrf_path.is_empty()
                && existing_sdrf_is_strict_repository_subset(path, &evidence.raw_files)
        });
    let compiled = compile_workspace_with_trusted_partial_sdrf(
        &evidence,
        &state,
        &explicit_mappings,
        trusted_partial_sdrf.as_deref(),
    )?;
    write_sdrf(&draft_path, &compiled.headers, &compiled.rows)?;
    write_validation_review(&validation_path, &compiled.issues)?;
    let validation = validation_cycle(1, &compiled.issues);
    let adjudications = workspace_adjudications(&evidence, &state);
    fs::write(
        &adjudications_path,
        serde_json::to_string_pretty(&adjudications)?,
    )?;

    let has_template_gap = adjudications
        .iter()
        .any(|record| matches!(&record.adjudication, ClaimAdjudication::TemplateGap { .. }));
    let terminal_status = if validation.validation_errors == 0 {
        "locally_valid"
    } else if has_template_gap {
        "partial_template_gap"
    } else if !acceptance.observations.is_empty() {
        "partial_human_review"
    } else {
        "human_review"
    };

    write_factor_deterministic_bridge_review(
        &review_path,
        &graph,
        &acceptance,
        &adjudications,
        &validation,
        terminal_status,
    )?;
    let result = json!({
        "harness_version": SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION,
        "accession": accession,
        "factor_graph_source": graph_path,
        "model_calls": 0,
        "tool_actions": 0,
        "validator_cycles": 1,
        "terminal_status": terminal_status,
        "derived_observation_count": acceptance.observations.len(),
        "rejected_observation_count": acceptance.rejected_observations.len(),
        "derived_observations": &acceptance.observations,
        "rejected_observations": &acceptance.rejected_observations,
        "adjudications": &adjudications,
        "validation": &validation,
        "compiled_fingerprint": &compiled.fingerprint,
        "draft_path": draft_path.display().to_string(),
        "review_path": review_path.display().to_string()
    });
    fs::write(&result_path, serde_json::to_string_pretty(&result)?)?;

    Ok(ScientificAgentResultRow {
        accession: accession.into(),
        status: "success".into(),
        terminal_status: terminal_status.into(),
        turns: 0,
        tool_actions: 0,
        validator_cycles: 1,
        branches: graph.materials.len() + graph.regimes.len() + graph.acquisitions.len(),
        open_questions: acceptance.open_questions.len(),
        relation_mode: compiled.proposal.relation_mode,
        locally_valid: validation.validation_errors == 0,
        validation_errors: validation.validation_errors,
        draft_path: draft_path.display().to_string(),
        review_path: review_path.display().to_string(),
        workspace_path: root.display().to_string(),
        error: String::new(),
    })
}

async fn run_factor_row_role_hardened(
    opts: SdrfScientificAgentOptions,
) -> Result<SdrfScientificAgentSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used for one accession");
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts.output_dir.join("factor_row_role_hardened_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!(
                "[{}/{}] {} FactorGraph v2 row-role-hardened bridge",
                i + 1,
                accessions.len(),
                accession
            );
        }
        match run_one_factor_row_role_hardened(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> terminal={} model_calls=0 validators={} valid={} errors={}",
                        row.terminal_status,
                        row.validator_cycles,
                        row.locally_valid,
                        row.validation_errors
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> FactorGraph row-role-hardened bridge error: {err:#}");
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
    let successful = rows.iter().filter(|row| row.status == "success").count();
    let locally_valid_drafts = rows.iter().filter(|row| row.locally_valid).count();
    let summary = SdrfScientificAgentSummary {
        harness_version: SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION.into(),
        generator_version: GENERATOR_VERSION.into(),
        accessions_requested: accessions.len(),
        successful,
        errors: rows.len().saturating_sub(successful),
        locally_valid_drafts,
        incomplete_drafts: successful.saturating_sub(locally_valid_drafts),
        total_validation_errors: rows.iter().map(|row| row.validation_errors).sum(),
        total_agent_turns: 0,
        total_tool_actions: 0,
        total_validator_cycles: rows.iter().map(|row| row.validator_cycles).sum(),
        results_tsv: results_path.display().to_string(),
        workspace_root: opts
            .output_dir
            .join("row_role_hardened")
            .display()
            .to_string(),
    };
    let rendered = serde_json::to_string_pretty(&summary)?;
    fs::write(
        opts.output_dir
            .join("factor_row_role_hardened_summary.json"),
        &rendered,
    )?;
    fs::write(
        opts.output_dir.join("scientific_agent_summary.json"),
        rendered,
    )?;
    Ok(summary)
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

fn dedup_strings(values: &mut Vec<String>) {
    let mut seen = BTreeSet::new();
    values.retain(|value| {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            return false;
        }
        seen.insert(trimmed.to_ascii_lowercase())
    });
}

fn claim_identity_parts(
    concept_type: &str,
    scope: &str,
    branch_id: &str,
) -> (String, String, String) {
    (
        concept_type.trim().to_ascii_lowercase(),
        scope.trim().to_ascii_lowercase(),
        branch_id.trim().to_string(),
    )
}

fn claim_identity(claim: &ScientificClaim) -> (String, String, String) {
    claim_identity_parts(&claim.concept_type, &claim.scope, &claim.branch_id)
}

fn conflict_identity(conflict: &WorkspaceConflict) -> (String, String, String) {
    claim_identity_parts(&conflict.concept_type, &conflict.scope, &conflict.branch_id)
}

fn merge_same_value_claim(existing: &mut ScientificClaim, incoming: ScientificClaim) {
    existing.evidence_refs.extend(incoming.evidence_refs);
    existing.evidence_refs.sort();
    existing.evidence_refs.dedup();
    if status_rank(&incoming.status) > status_rank(&existing.status) {
        existing.status = incoming.status;
    }
    if confidence_rank(&incoming.confidence) > confidence_rank(&existing.confidence) {
        existing.confidence = incoming.confidence;
    }
    if incoming.reason.len() > existing.reason.len() {
        existing.reason = incoming.reason;
    }
}

fn normalize_claim(
    evidence: &DatasetEvidence,
    branch_ids: &BTreeSet<&str>,
    claim: &mut ScientificClaim,
) -> bool {
    let allowed = scientific_concept_types()
        .into_iter()
        .collect::<BTreeSet<_>>();
    claim.concept_type = claim.concept_type.trim().to_ascii_lowercase();
    claim.value = claim.value.trim().to_string();
    claim.scope = claim.scope.trim().to_ascii_lowercase();
    claim.branch_id = claim.branch_id.trim().to_string();
    claim.evidence_refs = valid_evidence_refs(evidence, &claim.evidence_refs);
    claim.reason = claim.reason.trim().to_string();

    if !allowed.contains(claim.concept_type.as_str()) || claim.status == "rejected" {
        return false;
    }
    if !matches!(
        claim.scope.as_str(),
        "project" | "branch" | "row" | "unresolved"
    ) {
        claim.scope = "unresolved".into();
        claim.status = "unresolved".into();
        claim.value.clear();
        claim.branch_id.clear();
    }
    if claim.scope == "branch" && !branch_ids.contains(claim.branch_id.as_str()) {
        claim.scope = "unresolved".into();
        claim.status = "unresolved".into();
        claim.value.clear();
        claim.branch_id.clear();
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
    true
}

fn push_workspace_conflict(
    evidence: &DatasetEvidence,
    conflicts: &mut Vec<WorkspaceConflict>,
    mut conflict: WorkspaceConflict,
) {
    conflict.concept_type = conflict.concept_type.trim().to_ascii_lowercase();
    conflict.scope = conflict.scope.trim().to_ascii_lowercase();
    conflict.branch_id = conflict.branch_id.trim().to_string();
    conflict.existing_value = conflict.existing_value.trim().to_string();
    conflict.proposed_value = conflict.proposed_value.trim().to_string();
    conflict.evidence_refs = valid_evidence_refs(evidence, &conflict.evidence_refs);
    conflict.reason = conflict.reason.trim().to_string();
    let key = (
        conflict_identity(&conflict),
        conflict.existing_value.to_ascii_lowercase(),
        conflict.proposed_value.to_ascii_lowercase(),
    );
    if let Some(existing) = conflicts.iter_mut().find(|existing| {
        (
            conflict_identity(existing),
            existing.existing_value.to_ascii_lowercase(),
            existing.proposed_value.to_ascii_lowercase(),
        ) == key
    }) {
        existing.evidence_refs.extend(conflict.evidence_refs);
        existing.evidence_refs.sort();
        existing.evidence_refs.dedup();
        if conflict.reason.len() > existing.reason.len() {
            existing.reason = conflict.reason;
        }
        if existing.turn == 0 || (conflict.turn != 0 && conflict.turn < existing.turn) {
            existing.turn = conflict.turn;
        }
        return;
    }
    conflicts.push(conflict);
    conflicts.sort_by(|a, b| {
        conflict_identity(a)
            .cmp(&conflict_identity(b))
            .then_with(|| a.existing_value.cmp(&b.existing_value))
            .then_with(|| a.proposed_value.cmp(&b.proposed_value))
    });
}

fn reduce_scientific_claims(
    evidence: &DatasetEvidence,
    branch_ids: &BTreeSet<&str>,
    claims: &mut Vec<ScientificClaim>,
    conflicts: &mut Vec<WorkspaceConflict>,
    turn: usize,
) {
    let mut reduced: BTreeMap<(String, String, String), ScientificClaim> = BTreeMap::new();
    for mut claim in claims.drain(..) {
        if !normalize_claim(evidence, branch_ids, &mut claim) {
            continue;
        }
        let key = claim_identity(&claim);
        match reduced.get_mut(&key) {
            None => {
                reduced.insert(key, claim);
            }
            Some(existing) if existing.value.eq_ignore_ascii_case(&claim.value) => {
                merge_same_value_claim(existing, claim);
            }
            Some(existing)
                if adjudication_equivalence_key(evidence, existing).is_some()
                    && adjudication_equivalence_key(evidence, existing)
                        == adjudication_equivalence_key(evidence, &claim) =>
            {
                // Different source-faithful phrasings that independently lead
                // Rust to the same canonical/template-gap outcome are not a
                // scientific conflict. Preserve the richer observation while
                // merging provenance.
                let incoming_value = claim.value.clone();
                let incoming_is_richer = incoming_value.len() > existing.value.len();
                merge_same_value_claim(existing, claim);
                if incoming_is_richer {
                    existing.value = incoming_value;
                }
                conflicts.retain(|conflict| conflict_identity(conflict) != key);
            }
            Some(existing) => {
                push_workspace_conflict(
                    evidence,
                    conflicts,
                    WorkspaceConflict {
                        concept_type: key.0.clone(),
                        scope: key.1.clone(),
                        branch_id: key.2.clone(),
                        existing_value: existing.value.clone(),
                        proposed_value: claim.value.clone(),
                        evidence_refs: claim.evidence_refs.clone(),
                        reason: "canonical workspace contained multiple scientifically non-equivalent observations for one stable identity; retained the existing observation and recorded an explicit conflict".into(),
                        turn,
                    },
                );
            }
        }
    }
    *claims = reduced.into_values().collect();
    claims.sort_by(|a, b| claim_identity(a).cmp(&claim_identity(b)));
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
    action.evidence_refs = valid_evidence_refs(evidence, &action.evidence_refs);

    if action.action == "READ_EVIDENCE_CONTEXT" {
        action.queries.clear();
        action.evidence_refs.truncate(8);
        return;
    }
    action.evidence_refs.clear();

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

fn registered_source_paths(evidence: &DatasetEvidence) -> BTreeSet<String> {
    let mut paths = evidence
        .manuscript_sources
        .iter()
        .chain(evidence.annotation_sources.iter())
        .cloned()
        .collect::<BTreeSet<_>>();
    if !evidence.project_json_path.trim().is_empty() {
        paths.insert(evidence.project_json_path.clone());
    }
    if !evidence.files_json_path.trim().is_empty() {
        paths.insert(evidence.files_json_path.clone());
    }
    paths
}

fn registered_source_candidates(evidence: &DatasetEvidence, item: &EvidenceItem) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let direct = PathBuf::from(item.source_label.trim());
    if direct.is_file() {
        paths.push(direct);
    }
    let kind = item.source_kind.to_ascii_lowercase();
    if kind.contains("manuscript") && !kind.contains("semantic") {
        paths.extend(
            evidence
                .manuscript_sources
                .iter()
                .map(|path| PathBuf::from(path.as_str())),
        );
        paths.extend(
            evidence
                .annotation_sources
                .iter()
                .map(|path| PathBuf::from(path.as_str())),
        );
    } else if kind.contains("annotation") || kind.contains("semantic") {
        paths.extend(
            evidence
                .annotation_sources
                .iter()
                .map(|path| PathBuf::from(path.as_str())),
        );
        paths.extend(
            evidence
                .manuscript_sources
                .iter()
                .map(|path| PathBuf::from(path.as_str())),
        );
    } else {
        paths.extend(
            evidence
                .manuscript_sources
                .iter()
                .map(|path| PathBuf::from(path.as_str())),
        );
        paths.extend(
            evidence
                .annotation_sources
                .iter()
                .map(|path| PathBuf::from(path.as_str())),
        );
    }
    if !evidence.project_json_path.trim().is_empty() {
        paths.push(PathBuf::from(&evidence.project_json_path));
    }
    if !evidence.files_json_path.trim().is_empty() {
        paths.push(PathBuf::from(&evidence.files_json_path));
    }
    let registered = registered_source_paths(evidence);
    paths.retain(|path| path.is_file() && registered.contains(&path.display().to_string()));
    paths.sort_by_key(|path| {
        let display = path.display().to_string();
        if display == item.source_label {
            0usize
        } else {
            1usize
        }
    });
    paths.dedup();
    paths
}

fn context_anchor(source: &str, excerpt: &str) -> Option<usize> {
    let lower = source.to_ascii_lowercase();
    let exact = excerpt.trim().to_ascii_lowercase();
    if !exact.is_empty() {
        if let Some(pos) = lower.find(&exact) {
            return Some(pos);
        }
    }
    let mut terms = excerpt
        .split(|c: char| !c.is_ascii_alphanumeric() && c != '-')
        .map(str::trim)
        .filter(|term| term.len() >= 7)
        .map(|term| term.to_ascii_lowercase())
        .collect::<Vec<_>>();
    terms.sort_by_key(|term| std::cmp::Reverse(term.len()));
    terms.dedup();
    terms
        .into_iter()
        .take(24)
        .find_map(|term| lower.find(&term))
}

fn registered_context_for_item(
    evidence: &DatasetEvidence,
    item: &EvidenceItem,
) -> Option<(PathBuf, String, usize)> {
    for path in registered_source_candidates(evidence, item) {
        let Ok(source) = fs::read_to_string(&path) else {
            continue;
        };
        if let Some(anchor) = context_anchor(&source, &item.text) {
            return Some((path, source, anchor));
        }
    }
    None
}

fn source_context_window(source: &str, anchor: usize, radius: usize) -> String {
    let mut start = anchor.saturating_sub(radius);
    let mut end = (anchor + radius).min(source.len());
    while start > 0 && !source.is_char_boundary(start) {
        start -= 1;
    }
    while end < source.len() && !source.is_char_boundary(end) {
        end += 1;
    }
    source[start..end].replace('\0', " ")
}

fn read_evidence_context(
    evidence: &mut DatasetEvidence,
    evidence_ref: &str,
    target_fields: &[String],
    reason: &str,
    turn: usize,
    attempted: &mut BTreeSet<String>,
) -> EvidenceActionResult {
    let query = EvidenceQuery {
        match_kind: "identifier".into(),
        value: evidence_ref.into(),
        terms: Vec::new(),
        document_hint: String::new(),
    };
    let key = format!(
        "READ_EVIDENCE_CONTEXT:{}",
        evidence_ref.to_ascii_uppercase()
    );
    if !attempted.insert(key) {
        return EvidenceActionResult {
            round: turn,
            action: "READ_EVIDENCE_CONTEXT".into(),
            target_fields: target_fields.to_vec(),
            query,
            outcome: "duplicate_skipped".into(),
            summary: format!(
                "evidence context {} was already read; use the existing expanded E#### context or choose a different source; reason={}",
                evidence_ref, reason
            ),
            ..EvidenceActionResult::default()
        };
    }
    let Some(item) = evidence
        .evidence
        .iter()
        .find(|item| item.id.eq_ignore_ascii_case(evidence_ref))
        .cloned()
    else {
        return EvidenceActionResult {
            round: turn,
            action: "READ_EVIDENCE_CONTEXT".into(),
            target_fields: target_fields.to_vec(),
            query,
            outcome: "invalid_query".into(),
            summary: format!(
                "evidence ref {} is not in the trusted inventory",
                evidence_ref
            ),
            ..EvidenceActionResult::default()
        };
    };
    let Some((path, source, anchor)) = registered_context_for_item(evidence, &item) else {
        return EvidenceActionResult {
            round: turn,
            action: "READ_EVIDENCE_CONTEXT".into(),
            target_fields: target_fields.to_vec(),
            query,
            outcome: "context_unavailable".into(),
            matched_evidence_refs: vec![item.id.clone()],
            summary: format!(
                "{} could not be anchored in any registered trusted source document; the inline trusted excerpt remains available; source_label={}; reason={}",
                item.id, item.source_label, reason
            ),
            ..EvidenceActionResult::default()
        };
    };
    let context = source_context_window(&source, anchor, SCIENTIFIC_AGENT_CONTEXT_READ_RADIUS);
    let context_label = path.display().to_string();
    if let Some(existing) = evidence.evidence.iter().find(|existing| {
        existing.source_kind == "agent_read_context"
            && existing.source_label == context_label
            && existing.text == context
    }) {
        return EvidenceActionResult {
            round: turn,
            action: "READ_EVIDENCE_CONTEXT".into(),
            target_fields: target_fields.to_vec(),
            query,
            outcome: "matched".into(),
            matched_evidence_refs: vec![existing.id.clone()],
            summary: format!(
                "expanded trusted source context for {} was already materialized as {}; source={}; reason={}",
                item.id, existing.id, path.display(), reason
            ),
            ..EvidenceActionResult::default()
        };
    }
    let expanded_id = next_agent_evidence_id(evidence);
    evidence.evidence.push(EvidenceItem {
        id: expanded_id.clone(),
        source_kind: "agent_read_context".into(),
        source_label: context_label,
        text: context,
    });
    EvidenceActionResult {
        round: turn,
        action: "READ_EVIDENCE_CONTEXT".into(),
        target_fields: target_fields.to_vec(),
        query,
        outcome: "matched".into(),
        matched_evidence_refs: vec![expanded_id.clone()],
        summary: format!(
            "expanded {} into {} with a bounded surrounding context window from registered trusted source {}; reason={}",
            item.id, expanded_id, path.display(), reason
        ),
        ..EvidenceActionResult::default()
    }
}

fn execute_scientific_agent_actions(
    evidence: &mut DatasetEvidence,
    actions: &[AgentEvidenceAction],
    turn: usize,
    attempted: &mut BTreeSet<String>,
) -> Vec<EvidenceActionResult> {
    let mut out = Vec::new();
    for action in actions {
        let target_fields = action
            .target_concepts
            .iter()
            .map(|concept| {
                concept_to_sdrf_field(concept)
                    .unwrap_or(concept)
                    .to_string()
            })
            .collect::<Vec<_>>();
        if action.action == "READ_EVIDENCE_CONTEXT" {
            for evidence_ref in &action.evidence_refs {
                out.push(read_evidence_context(
                    evidence,
                    evidence_ref,
                    &target_fields,
                    &action.reason,
                    turn,
                    attempted,
                ));
            }
            continue;
        }
        let request = agent_action_to_request(action);
        out.extend(execute_evidence_actions(
            evidence,
            &[request],
            turn,
            attempted,
        ));
    }
    out
}

fn record_task_reads(
    state: &mut ScientificWorkspaceState,
    task_id: &str,
    requested_refs: &[String],
    results: &[EvidenceActionResult],
) {
    let Some(task) = state.tasks.iter_mut().find(|task| task.id == task_id) else {
        return;
    };
    task.evidence_reads.extend(requested_refs.iter().cloned());
    for result in results
        .iter()
        .filter(|result| result.action == "READ_EVIDENCE_CONTEXT")
    {
        let requested = result.query.value.trim();
        if requested.starts_with('E') {
            task.evidence_reads.push(requested.to_string());
        }
        task.evidence_reads
            .extend(result.matched_evidence_refs.iter().cloned());
    }
    task.evidence_reads.sort();
    task.evidence_reads.dedup();
    task.decision_required = true;
    task.search_blocked = false;
}

fn unread_evidence_refs_for_task(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    task_id: &str,
    refs: &[String],
) -> Vec<String> {
    let already_read = state
        .tasks
        .iter()
        .find(|task| task.id == task_id)
        .map(|task| task.evidence_reads.iter().cloned().collect::<BTreeSet<_>>())
        .unwrap_or_default();
    valid_evidence_refs(evidence, refs)
        .into_iter()
        .filter(|evidence_ref| !already_read.contains(evidence_ref))
        .collect()
}

fn evidence_action_count(results: &[EvidenceActionResult]) -> usize {
    results
        .iter()
        .filter(|result| result.outcome != "duplicate_skipped")
        .count()
}

fn set_task_decision_required(state: &mut ScientificWorkspaceState, task_id: &str, value: bool) {
    if let Some(task) = state.tasks.iter_mut().find(|task| task.id == task_id) {
        task.decision_required = value;
    }
}

fn set_task_search_blocked(state: &mut ScientificWorkspaceState, task_id: &str, value: bool) {
    if let Some(task) = state.tasks.iter_mut().find(|task| task.id == task_id) {
        task.search_blocked = value;
    }
}

fn normalize_workspace_branches(evidence: &DatasetEvidence, branches: &mut Vec<AgentBranch>) {
    let raw_names = evidence
        .raw_files
        .iter()
        .map(|f| f.file_name.to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    let mut branch_map: BTreeMap<String, AgentBranch> = BTreeMap::new();
    for mut branch in branches.drain(..) {
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
                if branch.label.len() > existing.label.len() {
                    existing.label = branch.label;
                }
                if branch.notes.len() > existing.notes.len() {
                    existing.notes = branch.notes;
                }
            }
        }
    }
    *branches = branch_map.into_values().collect();
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

    normalize_workspace_branches(evidence, &mut state.branches);
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
        turn,
    );

    let old_conflicts = std::mem::take(&mut state.conflicts);
    for mut conflict in old_conflicts {
        if conflict.turn == 0 {
            conflict.turn = turn;
        } else {
            conflict.turn = conflict.turn.min(turn);
        }
        push_workspace_conflict(evidence, &mut state.conflicts, conflict);
    }

    dedup_strings(&mut state.open_questions);
    for task in &mut state.tasks {
        task.evidence_candidates = valid_evidence_refs(evidence, &task.evidence_candidates);
        task.evidence_reads = valid_evidence_refs(evidence, &task.evidence_reads);
    }
    ensure_active_task(state);
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

fn apply_workspace_delta(
    evidence: &DatasetEvidence,
    state: &mut ScientificWorkspaceState,
    delta: &WorkspaceDelta,
    turn: usize,
) -> Vec<String> {
    let mut events = Vec::new();

    if !state.active_task_id.is_empty() && delta.task_id != state.active_task_id {
        events.push(format!(
            "turn {turn} reducer rejected delta for task '{}' because active task is '{}'; inspect and work the active task only",
            delta.task_id, state.active_task_id
        ));
        state.next_evidence_actions.clear();
        state.next_step = "compile".into();
        return events;
    }
    if let Some(task) = state
        .tasks
        .iter_mut()
        .find(|task| task.id == state.active_task_id)
    {
        task.attempts += 1;
        if task.status == "open" {
            task.status = "investigating".into();
        }
        if !delta.notes.trim().is_empty() {
            task.notes = delta.notes.trim().to_string();
        }
        match delta.task_status.as_str() {
            "resolved" if task.sdrf_field == "study_structure" => {
                task.status = "resolved".into();
            }
            // Field tasks are only resolved by Rust after compile/validation
            // proves that their validator failures disappeared. Model
            // `resolved` means "ready to compile/test", not permission to hide
            // an outstanding error.
            "resolved" => task.status = "investigating".into(),
            "human_review" => task.status = "human_review".into(),
            _ => task.status = "investigating".into(),
        }
    }

    // Branches are Rust-owned canonical state. Upserts merge into the existing
    // branch set; omission never deletes a branch.
    state.branches.extend(delta.branch_upserts.clone());
    normalize_workspace_branches(evidence, &mut state.branches);
    let branch_ids = state
        .branches
        .iter()
        .map(|b| b.id.as_str())
        .collect::<BTreeSet<_>>();

    // Explicit retractions are the only direct deletion path for active claims.
    for retraction in &delta.claim_retractions {
        let identity = claim_identity_parts(
            &retraction.concept_type,
            &retraction.scope,
            &retraction.branch_id,
        );
        if let Some(index) = state
            .claims
            .iter()
            .position(|claim| claim_identity(claim) == identity)
        {
            let current = state.claims[index].value.clone();
            let expected = retraction.expected_value.trim();
            if expected.is_empty() || current.eq_ignore_ascii_case(expected) {
                state.claims.remove(index);
                state
                    .conflicts
                    .retain(|conflict| conflict_identity(conflict) != identity);
                events.push(format!(
                    "turn {turn} reducer explicitly retracted claim identity {:?} value '{}' ({})",
                    identity,
                    current,
                    retraction.reason.trim()
                ));
            } else {
                events.push(format!(
                    "turn {turn} reducer ignored stale retraction for claim identity {:?}: expected '{}' but active value is '{}'",
                    identity, expected, current
                ));
            }
        }
    }

    // An explicit conflict resolution may keep the current value or replace it
    // with one of the values that participated in the recorded conflict.
    for resolution in &delta.conflict_resolutions {
        let identity = claim_identity_parts(
            &resolution.concept_type,
            &resolution.scope,
            &resolution.branch_id,
        );
        let candidates = state
            .conflicts
            .iter()
            .filter(|conflict| conflict_identity(conflict) == identity)
            .flat_map(|conflict| {
                [
                    conflict.existing_value.to_ascii_lowercase(),
                    conflict.proposed_value.to_ascii_lowercase(),
                ]
            })
            .collect::<BTreeSet<_>>();
        let resolved = resolution.resolved_value.trim();
        if candidates.is_empty() || !candidates.contains(&resolved.to_ascii_lowercase()) {
            events.push(format!(
                "turn {turn} reducer ignored conflict resolution for claim identity {:?}: resolved value '{}' was not part of an active conflict",
                identity, resolved
            ));
            continue;
        }
        let mut claim = ScientificClaim {
            concept_type: identity.0.clone(),
            value: resolved.to_string(),
            scope: identity.1.clone(),
            branch_id: identity.2.clone(),
            status: resolution.status.clone(),
            evidence_refs: resolution.evidence_refs.clone(),
            confidence: resolution.confidence.clone(),
            reason: resolution.reason.clone(),
        };
        if !normalize_claim(evidence, &branch_ids, &mut claim) {
            continue;
        }
        if let Some(index) = state
            .claims
            .iter()
            .position(|existing| claim_identity(existing) == identity)
        {
            if state.claims[index].value.eq_ignore_ascii_case(&claim.value) {
                merge_same_value_claim(&mut state.claims[index], claim);
            } else {
                state.claims[index] = claim;
            }
        } else {
            state.claims.push(claim);
        }
        state
            .conflicts
            .retain(|conflict| conflict_identity(conflict) != identity);
        events.push(format!(
            "turn {turn} reducer explicitly resolved conflict for claim identity {:?} to '{}'",
            identity, resolved
        ));
    }

    // Claim upserts mutate one stable identity at a time. Omitted identities are
    // untouched. A changed value must explicitly name the value it supersedes;
    // otherwise the proposal becomes a conflict and the active value survives.
    for upsert in &delta.claim_upserts {
        let mut incoming = upsert.as_claim();
        if !normalize_claim(evidence, &branch_ids, &mut incoming) {
            continue;
        }
        let identity = claim_identity(&incoming);
        if let Some(index) = state
            .claims
            .iter()
            .position(|claim| claim_identity(claim) == identity)
        {
            let existing_value = state.claims[index].value.clone();
            if existing_value.eq_ignore_ascii_case(&incoming.value) {
                merge_same_value_claim(&mut state.claims[index], incoming);
                continue;
            }
            let equivalent = adjudication_equivalence_key(evidence, &state.claims[index]).is_some()
                && adjudication_equivalence_key(evidence, &state.claims[index])
                    == adjudication_equivalence_key(evidence, &incoming);
            if equivalent {
                let incoming_value = incoming.value.clone();
                let incoming_is_richer = incoming_value.len() > state.claims[index].value.len();
                merge_same_value_claim(&mut state.claims[index], incoming);
                if incoming_is_richer {
                    state.claims[index].value = incoming_value.clone();
                }
                state
                    .conflicts
                    .retain(|conflict| conflict_identity(conflict) != identity);
                events.push(format!(
                    "turn {turn} reducer merged evidence-compatible observation variant for identity {:?}: '{}' ~= '{}' under the same Rust adjudication",
                    identity, existing_value, incoming_value
                ));
                continue;
            }
            let supersedes = upsert.supersedes_value.trim();
            if !supersedes.is_empty() && existing_value.eq_ignore_ascii_case(supersedes) {
                let new_value = incoming.value.clone();
                state.claims[index] = incoming;
                state
                    .conflicts
                    .retain(|conflict| conflict_identity(conflict) != identity);
                events.push(format!(
                    "turn {turn} reducer explicitly superseded claim identity {:?}: '{}' -> '{}'",
                    identity, existing_value, new_value
                ));
            } else {
                let reason = if supersedes.is_empty() {
                    "model proposed a scientifically non-equivalent observation without explicit supersession"
                } else {
                    "model supersedes_observed_value did not match the active observation"
                };
                push_workspace_conflict(
                    evidence,
                    &mut state.conflicts,
                    WorkspaceConflict {
                        concept_type: identity.0.clone(),
                        scope: identity.1.clone(),
                        branch_id: identity.2.clone(),
                        existing_value: existing_value.clone(),
                        proposed_value: incoming.value.clone(),
                        evidence_refs: incoming.evidence_refs.clone(),
                        reason: format!("{reason}; {}", incoming.reason),
                        turn,
                    },
                );
                events.push(format!(
                    "turn {turn} reducer retained claim identity {:?} value '{}' and recorded conflicting proposal '{}'",
                    identity, existing_value, incoming.value
                ));
            }
        } else {
            state.claims.push(incoming);
        }
    }

    for mut conflict in delta.conflict_additions.clone() {
        conflict.turn = turn;
        push_workspace_conflict(evidence, &mut state.conflicts, conflict);
    }

    state
        .open_questions
        .extend(delta.open_question_additions.iter().cloned());
    for resolved in &delta.open_question_resolutions {
        let needle = resolved.trim().to_ascii_lowercase();
        state
            .open_questions
            .retain(|question| question.trim().to_ascii_lowercase() != needle);
    }

    state.next_evidence_actions = delta.next_evidence_actions.clone();
    for action in &mut state.next_evidence_actions {
        normalize_scientific_agent_action(evidence, action);
    }
    state.next_evidence_actions.truncate(8);
    state.next_step = delta.next_step.clone();
    if !delta.notes.trim().is_empty() {
        state.notes = delta.notes.trim().to_string();
    }

    normalize_workspace_state(evidence, state, turn);
    if matches!(delta.task_status.as_str(), "resolved" | "human_review") {
        state.next_evidence_actions.clear();
        state.next_step = "compile".into();
    }
    events
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
            reason:
                "observation is explicitly unresolved or has no source-faithful scientific value"
                    .into(),
        };
    }

    let relevant_refs =
        field_relevant_claim_refs(evidence, &claim.concept_type, &claim.evidence_refs);
    if relevant_refs.is_empty() {
        return ClaimAdjudication::Unresolved {
            reason: "observation has no field-relevant trusted evidence refs".into(),
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

fn normalize_semantic_mapping_phrase(value: &str) -> String {
    value
        .to_ascii_lowercase()
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { ' ' })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn isolation_canonicalization_is_observation_faithful(
    observed_value: &str,
    canonical_value: &str,
) -> bool {
    let observed = normalize_semantic_mapping_phrase(observed_value);
    let canonical = normalize_semantic_mapping_phrase(canonical_value);
    if observed.is_empty() || canonical.is_empty() {
        return false;
    }
    if observed == canonical || observed.contains(&canonical) || canonical.contains(&observed) {
        return true;
    }

    // Keep aliasing intentionally tiny and meaning-preserving. The previous
    // evidence-wide scaffold could map any hydrodynamic/spray-voltage regime
    // to `manual picking` simply because picking language appeared somewhere
    // in the cited evidence. Hardened mode only accepts that canonical value
    // when the observation itself explicitly describes a picking operation.
    match canonical.as_str() {
        "manual picking" => {
            observed.contains("manual picking")
                || observed.contains("manual pick")
                || observed.contains("manually picked")
                || observed.contains("picked single cell")
                || observed.contains("single cell picked")
                || observed.contains("cell picking")
        }
        _ => false,
    }
}

fn acquisition_mode_semantic_class(value: &str) -> Option<&'static str> {
    let normalized = normalize_semantic_mapping_phrase(value);
    if normalized.is_empty() {
        return None;
    }
    let tokens = normalized.split_whitespace().collect::<Vec<_>>();
    let has_token = |token: &str| tokens.iter().any(|value| *value == token);

    let dda = normalized.contains("data dependent")
        || normalized.contains("dda pasef")
        || normalized.contains("ddapasef")
        || (has_token("dda")
            && (normalized.contains(" ms")
                || normalized.starts_with("ms ")
                || normalized.contains("acquisition")
                || normalized.contains("pasef")));

    let dia_nn_only = normalized.contains("dia nn")
        && !normalized.contains("data independent")
        && !normalized.contains("dia pasef")
        && !normalized.contains("diapasef")
        && !normalized.contains("dia mode")
        && !normalized.contains("swath");
    let dia = normalized.contains("data independent")
        || normalized.contains("dia pasef")
        || normalized.contains("diapasef")
        || normalized.contains("swath")
        || (!dia_nn_only
            && has_token("dia")
            && (normalized.contains(" ms")
                || normalized.starts_with("ms ")
                || normalized.contains("acquisition")
                || normalized.contains("mode")
                || normalized.contains("pasef")));

    match (dda, dia) {
        (true, false) => Some("dda"),
        (false, true) => Some("dia"),
        _ => None,
    }
}

fn acquisition_canonical_value(mode: &str) -> Option<String> {
    match mode {
        "dda" => Some("NT=data-dependent acquisition;AC=PRIDE:0000627".into()),
        "dia" => Some("Data-independent acquisition".into()),
        _ => None,
    }
}

fn acquisition_canonicalization_is_observation_faithful(
    observed_value: &str,
    canonical_value: &str,
) -> bool {
    match (
        acquisition_mode_semantic_class(observed_value),
        acquisition_mode_semantic_class(canonical_value),
    ) {
        (Some(observed), Some(canonical)) => observed == canonical,
        _ => false,
    }
}

fn factor_isolation_hardening_enabled(state: &ScientificWorkspaceState) -> bool {
    state.harness_version == SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION
        || state.harness_version == SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION
        || state.harness_version == SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION
}

fn harden_factor_isolation_project_baseline(
    proposal: &mut SdrfProposal,
    state: &ScientificWorkspaceState,
    issues: &mut Vec<ValidationIssue>,
) {
    if !factor_isolation_hardening_enabled(state) {
        return;
    }
    let current = proposal.single_cell_isolation_method.trim().to_string();
    if current.is_empty() || canonical_reserved_alias(&current).is_some() {
        return;
    }
    let observations = state
        .claims
        .iter()
        .filter(|claim| {
            claim.concept_type == "isolation_method"
                && matches!(claim.status.as_str(), "supported" | "hypothesis")
                && !claim.value.trim().is_empty()
        })
        .collect::<Vec<_>>();
    if observations.is_empty() {
        return;
    }
    if observations
        .iter()
        .all(|claim| isolation_canonicalization_is_observation_faithful(&claim.value, &current))
    {
        return;
    }

    proposal.single_cell_isolation_method = "not available".into();
    proposal
        .evidence_refs
        .remove("single_cell_isolation_method");
    issues.push(ValidationIssue {
        level: "warning".into(),
        code: "scientific_agent_factor_canonicalization_hardened_baseline_mask".into(),
        row: 0,
        column: SC_ISOLATION_METHOD.into(),
        message: format!(
            "canonicalization-hardened factor mode removed project isolation value '{}' because it is not semantically faithful to the accepted factor-scoped source observations",
            current
        ),
    });
}

fn adjudicate_hardened_factor_isolation_claim(
    evidence: &DatasetEvidence,
    claim: &ScientificClaim,
) -> ClaimAdjudication {
    match adjudicate_claim(evidence, claim) {
        ClaimAdjudication::Canonical {
            value,
            evidence_refs,
        } => {
            if isolation_canonicalization_is_observation_faithful(&claim.value, &value) {
                ClaimAdjudication::Canonical {
                    value,
                    evidence_refs,
                }
            } else {
                ClaimAdjudication::TemplateGap {
                    observed_value: claim.value.trim().to_string(),
                    evidence_refs,
                    reason: format!(
                        "hardened factor canonicalization rejected unrelated isolation vocabulary substitution: source-faithful observation '{}' does not directly support canonical value '{}'; retain the observation and fail closed instead of choosing a validator-compatible surrogate",
                        claim.value.trim(), value
                    ),
                }
            }
        }
        other => other,
    }
}

fn harden_factor_acquisition_project_baseline(
    proposal: &mut SdrfProposal,
    state: &ScientificWorkspaceState,
    issues: &mut Vec<ValidationIssue>,
) {
    if state.harness_version != SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION
        && state.harness_version != SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION
    {
        return;
    }
    let current = proposal
        .proteomics_data_acquisition_method
        .trim()
        .to_string();
    if current.is_empty() || canonical_reserved_alias(&current).is_some() {
        return;
    }
    let observations = state
        .claims
        .iter()
        .filter(|claim| {
            claim.concept_type == "acquisition_mode"
                && matches!(claim.status.as_str(), "supported" | "hypothesis")
                && !claim.value.trim().is_empty()
        })
        .collect::<Vec<_>>();
    if observations.is_empty() {
        return;
    }
    if observations
        .iter()
        .all(|claim| acquisition_canonicalization_is_observation_faithful(&claim.value, &current))
    {
        return;
    }

    proposal.proteomics_data_acquisition_method = "not available".into();
    proposal
        .evidence_refs
        .remove("proteomics_data_acquisition_method");
    issues.push(ValidationIssue {
        level: "warning".into(),
        code: "scientific_agent_factor_acquisition_semantic_fidelity_baseline_mask".into(),
        row: 0,
        column: "comment[proteomics data acquisition method]".into(),
        message: format!(
            "semantic-fidelity-hardened factor mode removed project acquisition value '{}' because it is not semantically faithful to the accepted factor-scoped acquisition observations",
            current
        ),
    });
}

fn adjudicate_semantic_fidelity_factor_acquisition_claim(
    evidence: &DatasetEvidence,
    claim: &ScientificClaim,
) -> ClaimAdjudication {
    let observed_mode = acquisition_mode_semantic_class(&claim.value);
    match adjudicate_claim(evidence, claim) {
        ClaimAdjudication::Canonical {
            value,
            evidence_refs,
        } => {
            let canonical_mode = acquisition_mode_semantic_class(&value);
            match (observed_mode, canonical_mode) {
                (Some(observed), Some(canonical)) if observed == canonical => {
                    ClaimAdjudication::Canonical {
                        value,
                        evidence_refs,
                    }
                }
                (Some(observed), Some(_)) => {
                    let refs = field_relevant_claim_refs(
                        evidence,
                        &claim.concept_type,
                        &claim.evidence_refs,
                    );
                    match acquisition_canonical_value(observed) {
                        Some(corrected) if !refs.is_empty() => ClaimAdjudication::Canonical {
                            value: corrected,
                            evidence_refs: refs,
                        },
                        _ => ClaimAdjudication::Conflict {
                            reason: format!(
                                "semantic-fidelity hardening rejected acquisition canonicalization '{}' for source-faithful observation '{}'",
                                value,
                                claim.value.trim()
                            ),
                        },
                    }
                }
                _ => ClaimAdjudication::Conflict {
                    reason: format!(
                        "semantic-fidelity hardening could not prove that acquisition observation '{}' supports canonical value '{}'",
                        claim.value.trim(),
                        value
                    ),
                },
            }
        }
        ClaimAdjudication::Conflict { reason } => {
            if let Some(observed) = observed_mode {
                let refs =
                    field_relevant_claim_refs(evidence, &claim.concept_type, &claim.evidence_refs);
                if let Some(corrected) = acquisition_canonical_value(observed) {
                    if !refs.is_empty() {
                        return ClaimAdjudication::Canonical {
                            value: corrected,
                            evidence_refs: refs,
                        };
                    }
                }
            }
            ClaimAdjudication::Conflict { reason }
        }
        other => other,
    }
}

fn adjudication_equivalence_key(
    evidence: &DatasetEvidence,
    claim: &ScientificClaim,
) -> Option<String> {
    match adjudicate_claim(evidence, claim) {
        ClaimAdjudication::Canonical { value, .. } => {
            Some(format!("canonical:{}", value.trim().to_ascii_lowercase()))
        }
        ClaimAdjudication::TemplateGap { observed_value, .. } => Some(format!(
            "template_gap:{}",
            observed_value.trim().to_ascii_lowercase()
        )),
        ClaimAdjudication::SupportedConcept { value, .. } => {
            Some(format!("supported:{}", value.trim().to_ascii_lowercase()))
        }
        ClaimAdjudication::Unresolved { .. } | ClaimAdjudication::Conflict { .. } => None,
    }
}

fn adjudicate_workspace_claim(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    claim: &ScientificClaim,
) -> ClaimAdjudication {
    let identity = claim_identity(claim);
    if let Some(conflict) = state
        .conflicts
        .iter()
        .find(|conflict| conflict_identity(conflict) == identity)
    {
        return ClaimAdjudication::Conflict {
            reason: format!(
                "stable claim identity has an unresolved workspace conflict: active='{}', proposed='{}'; {}",
                conflict.existing_value, conflict.proposed_value, conflict.reason
            ),
        };
    }
    if factor_isolation_hardening_enabled(state) && claim.concept_type == "isolation_method" {
        return adjudicate_hardened_factor_isolation_claim(evidence, claim);
    }
    if (state.harness_version == SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION
        || state.harness_version == SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION)
        && claim.concept_type == "acquisition_mode"
    {
        return adjudicate_semantic_fidelity_factor_acquisition_claim(evidence, claim);
    }
    adjudicate_claim(evidence, claim)
}

fn adjudication_record(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    claim: &ScientificClaim,
) -> ClaimAdjudicationRecord {
    ClaimAdjudicationRecord {
        concept_type: claim.concept_type.clone(),
        scope: claim.scope.clone(),
        branch_id: claim.branch_id.clone(),
        model_status: claim.status.clone(),
        proposed_value: claim.value.clone(),
        evidence_refs: claim.evidence_refs.clone(),
        adjudication: adjudicate_workspace_claim(evidence, state, claim),
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

fn canonical_workspace_claim_value(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    claim: &ScientificClaim,
) -> Option<(String, Vec<String>)> {
    match adjudicate_workspace_claim(evidence, state, claim) {
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
            adjudicate_workspace_claim(evidence, state, claim),
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
        .filter_map(
            |claim| match adjudicate_workspace_claim(evidence, state, claim) {
                ClaimAdjudication::Canonical { value, .. } => {
                    Some(format!("canonical:{}", value.trim().to_ascii_lowercase()))
                }
                ClaimAdjudication::TemplateGap { observed_value, .. } => Some(format!(
                    "template_gap:{}",
                    observed_value.trim().to_ascii_lowercase()
                )),
                ClaimAdjudication::SupportedConcept { value, .. } => {
                    Some(format!("supported:{}", value.trim().to_ascii_lowercase()))
                }
                ClaimAdjudication::Unresolved { .. } | ClaimAdjudication::Conflict { .. } => None,
            },
        )
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
        match adjudicate_workspace_claim(evidence, state, claim) {
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
        let Some((value, refs)) = canonical_workspace_claim_value(evidence, state, claim) else {
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

fn set_row_value(
    row: &mut [String],
    header_index: &HashMap<&str, usize>,
    header: &str,
    value: String,
) {
    let Some(&idx) = header_index.get(header) else {
        return;
    };
    if idx < row.len() {
        row[idx] = value;
    }
}

fn scientific_agent_raw_file_role(name: &str, row_role_hardened: bool) -> RawFileRole {
    let role = raw_file_role(name);
    if !row_role_hardened || role != RawFileRole::Unknown {
        return role;
    }

    // The shared role classifier already treats explicit pg/ng amount tokens as
    // reference/bulk inputs when they are delimiter-bounded. PRIDE archive names
    // also commonly concatenate the amount directly with a material token
    // (e.g. `200pgHeLa_raw.zip`). Treat that same explicit mass-amount signal as
    // a bulk/reference input rather than silently assuming a single cell.
    let normalized = name
        .trim()
        .to_ascii_lowercase()
        .replace('-', "_")
        .replace('.', "_")
        .replace(' ', "_");
    let compact_amount = Regex::new(r"(?i)(?:^|[_])\d+(?:\.\d+)?(?:pg|ng)[a-z]").unwrap();
    if compact_amount.is_match(&normalized) {
        return RawFileRole::Bulk;
    }

    RawFileRole::Unknown
}

fn enforce_deterministic_row_scaffold(
    headers: &[String],
    rows: &mut [Vec<String>],
    evidence: &DatasetEvidence,
    relation_mode: &str,
    row_role_hardened: bool,
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

    for (row_offset, row) in rows.iter_mut().enumerate() {
        let raw = row.get(data_file_idx).cloned().unwrap_or_default();
        if raw.trim().is_empty() {
            continue;
        }
        let stem = safe_identifier_from_file(&raw);
        let detected_role = scientific_agent_raw_file_role(&raw, row_role_hardened);
        let role = if row_role_hardened {
            detected_role
        } else {
            match detected_role {
                RawFileRole::Unknown => RawFileRole::SingleCell,
                role => role,
            }
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
            RawFileRole::SingleCell => {
                if row_role_hardened {
                    set_row_value(row, &header_index, SC_SAMPLE_TYPE, "single cell".into());
                    set_row_value(row, &header_index, SC_CELL_IDENTIFIER, stem);
                    set_row_value(row, &header_index, SC_CELLS_PER_WELL, "1".into());
                } else {
                    set_row_if_unresolved(row, &header_index, SC_SAMPLE_TYPE, "single cell".into());
                    set_row_if_unresolved(row, &header_index, SC_CELL_IDENTIFIER, stem);
                    set_row_if_unresolved(row, &header_index, SC_CELLS_PER_WELL, "1".into());
                }
            }
            RawFileRole::Unknown => {
                if row_role_hardened {
                    // Earlier proposal/bootstrap stages may already have broadcast a
                    // project-level `single cell` scaffold. Once the file role is
                    // deterministically unknown, that scaffold is no longer safe.
                    // Generated rows are therefore reset to explicit unresolved values.
                    // Existing/deposited SDRFs never enter this scaffold path.
                    set_row_value(row, &header_index, SC_SAMPLE_TYPE, "not available".into());
                    set_row_value(
                        row,
                        &header_index,
                        SC_ISOLATION_METHOD,
                        "not available".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELL_IDENTIFIER,
                        "not available".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELLS_PER_WELL,
                        "not available".into(),
                    );
                }
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "scientific_agent_raw_file_role_unresolved".into(),
                    row: row_offset + 1,
                    column: "comment[data file]".into(),
                    message: format!(
                        "RAW/archive '{}' has no deterministic single-cell/reference/QC/blank/few-cell role; row-role-hardened mode preserves the row as unresolved instead of coercing an unknown file into sample type 'single cell'",
                        raw
                    ),
                });
            }
            RawFileRole::FewCell(n) => {
                if row_role_hardened {
                    set_row_value(row, &header_index, SC_SAMPLE_TYPE, "not available".into());
                    set_row_value(
                        row,
                        &header_index,
                        SC_ISOLATION_METHOD,
                        "not applicable".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELL_IDENTIFIER,
                        "not applicable".into(),
                    );
                    set_row_value(row, &header_index, SC_CELLS_PER_WELL, n.to_string());
                } else {
                    set_row_if_unresolved(
                        row,
                        &header_index,
                        SC_SAMPLE_TYPE,
                        "not available".into(),
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
                    set_row_if_unresolved(row, &header_index, SC_CELLS_PER_WELL, n.to_string());
                }
            }
            RawFileRole::Blank => {
                if row_role_hardened {
                    set_row_value(row, &header_index, SC_SAMPLE_TYPE, "empty".into());
                    set_row_value(
                        row,
                        &header_index,
                        SC_ISOLATION_METHOD,
                        "not applicable".into(),
                    );
                    set_row_value(row, &header_index, SC_CELL_IDENTIFIER, "empty".into());
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELLS_PER_WELL,
                        "not applicable".into(),
                    );
                } else {
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
            }
            RawFileRole::QualityControl => {
                if row_role_hardened {
                    set_row_value(
                        row,
                        &header_index,
                        SC_SAMPLE_TYPE,
                        "quality control sample".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_ISOLATION_METHOD,
                        "not applicable".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELL_IDENTIFIER,
                        "not applicable".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELLS_PER_WELL,
                        "not applicable".into(),
                    );
                } else {
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
            }
            RawFileRole::Bulk => {
                if row_role_hardened {
                    set_row_value(row, &header_index, SC_SAMPLE_TYPE, "bulk control".into());
                    set_row_value(
                        row,
                        &header_index,
                        SC_ISOLATION_METHOD,
                        "not applicable".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELL_IDENTIFIER,
                        "not applicable".into(),
                    );
                    set_row_value(
                        row,
                        &header_index,
                        SC_CELLS_PER_WELL,
                        "not applicable".into(),
                    );
                } else {
                    set_row_if_unresolved(
                        row,
                        &header_index,
                        SC_SAMPLE_TYPE,
                        "bulk control".into(),
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

fn trusted_partial_sdrf_header_occurrences(headers: &[String]) -> Vec<(String, usize)> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    headers
        .iter()
        .map(|header| {
            let occurrence = counts.entry(header.clone()).or_insert(0);
            *occurrence += 1;
            (header.clone(), *occurrence)
        })
        .collect()
}

fn unique_named_header_index_for_trusted_partial_sdrf(
    headers: &[String],
    name: &str,
    context: &str,
) -> Result<usize> {
    let matches = headers
        .iter()
        .enumerate()
        .filter_map(|(index, header)| (header == name).then_some(index))
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [index] => Ok(*index),
        [] => bail!("{} is missing {}", context, name),
        _ => bail!(
            "trusted partial SDRF fusion requires exactly one '{}'; found {} in {}",
            name,
            matches.len(),
            context
        ),
    }
}

fn canonical_repository_raw_for_value(value: &str, raw_files: &[RawFile]) -> Result<String> {
    let aliases = normalized_data_file_aliases(value);
    let matches = raw_files
        .iter()
        .filter(|file| {
            normalized_data_file_aliases(&file.file_name)
                .iter()
                .any(|alias| aliases.contains(alias))
        })
        .map(|file| file.file_name.trim().to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    if matches.len() != 1 {
        bail!(
            "trusted partial SDRF data file '{}' resolves to {} repository RAW candidates: {:?}",
            value,
            matches.len(),
            matches
        );
    }
    Ok(matches.into_iter().next().unwrap_or_default())
}

fn fuse_trusted_partial_sdrf_rows(
    current_headers: &[String],
    current_rows: &[Vec<String>],
    evidence: &DatasetEvidence,
    partial_path: &Path,
) -> Result<(Vec<String>, Vec<Vec<String>>, ValidationIssue)> {
    if !existing_sdrf_is_strict_repository_subset(partial_path, &evidence.raw_files) {
        bail!(
            "trusted partial SDRF is not a strict repository subset: {}",
            partial_path.display()
        );
    }

    let (deposited_headers, deposited_rows) = read_existing_sdrf_table(partial_path)?;
    let deposited_data_idx = unique_named_header_index_for_trusted_partial_sdrf(
        &deposited_headers,
        "comment[data file]",
        "deposited SDRF",
    )?;
    let current_data_idx = unique_named_header_index_for_trusted_partial_sdrf(
        current_headers,
        "comment[data file]",
        "current generated SDRF",
    )?;

    let deposited_tokens = trusted_partial_sdrf_header_occurrences(&deposited_headers);
    let current_tokens = trusted_partial_sdrf_header_occurrences(current_headers);
    let mut union_tokens = deposited_tokens.clone();
    let mut union_seen = union_tokens.iter().cloned().collect::<BTreeSet<_>>();
    for token in current_tokens {
        if union_seen.insert(token.clone()) {
            union_tokens.push(token);
        }
    }
    let union_headers = union_tokens
        .iter()
        .map(|(header, _)| header.clone())
        .collect::<Vec<_>>();
    let union_index = union_tokens
        .iter()
        .cloned()
        .enumerate()
        .map(|(index, token)| (token, index))
        .collect::<HashMap<_, _>>();

    let project_row = |headers: &[String], row: &[String]| -> Vec<String> {
        let source_tokens = trusted_partial_sdrf_header_occurrences(headers);
        let mut out = vec!["not available".to_string(); union_headers.len()];
        for (source_idx, token) in source_tokens.iter().enumerate() {
            if let Some(&target_idx) = union_index.get(token) {
                if source_idx < row.len() {
                    out[target_idx] = row[source_idx].clone();
                }
            }
        }
        out
    };

    let mut deposited_by_raw: BTreeMap<String, Vec<Vec<String>>> = BTreeMap::new();
    for row in &deposited_rows {
        let value = row
            .get(deposited_data_idx)
            .map(String::as_str)
            .unwrap_or_default();
        let raw = canonical_repository_raw_for_value(value, &evidence.raw_files)?;
        deposited_by_raw
            .entry(raw)
            .or_default()
            .push(project_row(&deposited_headers, row));
    }

    let mut current_by_raw: BTreeMap<String, Vec<Vec<String>>> = BTreeMap::new();
    for row in current_rows {
        let value = row
            .get(current_data_idx)
            .map(String::as_str)
            .unwrap_or_default();
        let raw = canonical_repository_raw_for_value(value, &evidence.raw_files)?;
        current_by_raw
            .entry(raw)
            .or_default()
            .push(project_row(current_headers, row));
    }

    let mut fused_rows = Vec::new();
    let mut mapped_raws = 0usize;
    let mut retained_raws = 0usize;
    for file in &evidence.raw_files {
        let raw = file.file_name.trim().to_ascii_lowercase();
        if let Some(rows) = deposited_by_raw.get(&raw) {
            mapped_raws += 1;
            fused_rows.extend(rows.iter().cloned());
            continue;
        }
        let rows = current_by_raw.get(&raw).ok_or_else(|| {
            anyhow!(
                "generated SDRF has no fallback row for repository RAW {} while fusing {}",
                file.file_name,
                partial_path.display()
            )
        })?;
        retained_raws += 1;
        fused_rows.extend(rows.iter().cloned());
    }

    if mapped_raws == 0 || retained_raws == 0 {
        bail!(
            "trusted partial SDRF fusion expected strict subset coverage but observed mapped_raws={} retained_raws={}",
            mapped_raws,
            retained_raws
        );
    }

    let issue = ValidationIssue {
        level: "warning".into(),
        code: "scientific_agent_trusted_partial_sdrf_mapping_applied".into(),
        row: 0,
        column: "comment[data file]".into(),
        message: format!(
            "trusted resolved SDRF {} supplied explicit multiplex rows for {} repository RAW file(s); deterministic generated rows were retained for the remaining {} repository RAW file(s); deposited_rows={} fused_rows={}",
            partial_path.display(),
            mapped_raws,
            retained_raws,
            deposited_rows.len(),
            fused_rows.len()
        ),
    };
    Ok((union_headers, fused_rows, issue))
}

fn unresolved_de_novo_multiplex_mapping(
    evidence: &DatasetEvidence,
    relation_mode: &str,
    explicit_mappings: &[ExplicitRowMapping],
    trusted_partial_mapping_applied: bool,
) -> bool {
    evidence.existing_sdrf_path.is_empty()
        && explicit_mappings.is_empty()
        && !trusted_partial_mapping_applied
        && relation_mode == "multiplexed_cells_per_data_file"
        && evidence
            .study_design
            .multiplex_mapping_status
            .ends_with("mapping_unresolved")
}

fn consensus_canonical_isolation_for_single_cell_regimes(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
) -> Option<(String, Vec<String>, Vec<String>)> {
    if state.harness_version != SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION {
        return None;
    }

    let mut single_cell_regimes = BTreeSet::new();
    for claim in &state.claims {
        if claim.concept_type != "sample_type"
            || claim.scope != "branch"
            || claim.branch_id.trim().is_empty()
        {
            continue;
        }
        let value = match adjudicate_workspace_claim(evidence, state, claim) {
            ClaimAdjudication::Canonical { value, .. }
            | ClaimAdjudication::SupportedConcept { value, .. } => value,
            ClaimAdjudication::TemplateGap { .. }
            | ClaimAdjudication::Unresolved { .. }
            | ClaimAdjudication::Conflict { .. } => continue,
        };
        if value.trim().eq_ignore_ascii_case("single cell") {
            single_cell_regimes.insert(claim.branch_id.clone());
        }
    }
    if single_cell_regimes.is_empty() {
        return None;
    }

    let mut consensus_key: Option<String> = None;
    let mut consensus_value: Option<String> = None;
    let mut consensus_refs = BTreeSet::new();

    for regime_id in &single_cell_regimes {
        let isolation_claims = state
            .claims
            .iter()
            .filter(|claim| {
                claim.concept_type == "isolation_method"
                    && claim.scope == "branch"
                    && claim.branch_id == *regime_id
            })
            .collect::<Vec<_>>();
        if isolation_claims.is_empty() {
            return None;
        }

        let mut regime_key: Option<String> = None;
        let mut regime_value: Option<String> = None;
        for claim in isolation_claims {
            let (value, refs) = match adjudicate_workspace_claim(evidence, state, claim) {
                ClaimAdjudication::Canonical {
                    value,
                    evidence_refs,
                } => (value, evidence_refs),
                ClaimAdjudication::TemplateGap { .. }
                | ClaimAdjudication::SupportedConcept { .. }
                | ClaimAdjudication::Unresolved { .. }
                | ClaimAdjudication::Conflict { .. } => return None,
            };
            let key = value.trim().to_ascii_lowercase();
            if key.is_empty() {
                return None;
            }
            if regime_key.as_ref().is_some_and(|current| current != &key) {
                return None;
            }
            regime_key = Some(key);
            regime_value.get_or_insert(value);
            consensus_refs.extend(refs);
        }

        let key = regime_key?;
        let value = regime_value?;
        if consensus_key
            .as_ref()
            .is_some_and(|current| current != &key)
        {
            return None;
        }
        consensus_key = Some(key);
        consensus_value.get_or_insert(value);
    }

    Some((
        consensus_value?,
        consensus_refs.into_iter().collect(),
        single_cell_regimes.into_iter().collect(),
    ))
}

fn apply_consensus_single_cell_isolation_projection(
    headers: &[String],
    rows: &mut [Vec<String>],
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    relation_mode: &str,
    explicit_mappings: &[ExplicitRowMapping],
    has_unresolved_raw_roles: bool,
    trusted_partial_mapping_applied: bool,
) -> Vec<ValidationIssue> {
    if state.harness_version != SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION
        || !evidence.existing_sdrf_path.is_empty()
        || !explicit_mappings.is_empty()
        || has_unresolved_raw_roles
        || unresolved_de_novo_multiplex_mapping(
            evidence,
            relation_mode,
            explicit_mappings,
            trusted_partial_mapping_applied,
        )
    {
        return Vec::new();
    }

    let Some((isolation_value, evidence_refs, regime_ids)) =
        consensus_canonical_isolation_for_single_cell_regimes(evidence, state)
    else {
        return Vec::new();
    };

    let header_index = headers
        .iter()
        .enumerate()
        .map(|(index, header)| (header.as_str(), index))
        .collect::<HashMap<_, _>>();
    let Some(&sample_type_idx) = header_index.get(SC_SAMPLE_TYPE) else {
        return Vec::new();
    };
    let Some(&isolation_idx) = header_index.get(SC_ISOLATION_METHOD) else {
        return Vec::new();
    };

    let mut projected_rows = 0usize;
    for row in rows {
        if sample_type_idx >= row.len() || isolation_idx >= row.len() {
            continue;
        }
        if row[sample_type_idx]
            .trim()
            .eq_ignore_ascii_case("single cell")
            && row_value_is_unresolved(&row[isolation_idx])
        {
            row[isolation_idx] = isolation_value.clone();
            projected_rows += 1;
        }
    }

    if projected_rows == 0 {
        return Vec::new();
    }

    vec![ValidationIssue {
        level: "warning".into(),
        code: "scientific_agent_consensus_single_cell_isolation_projected".into(),
        row: 0,
        column: SC_ISOLATION_METHOD.into(),
        message: format!(
            "projected canonical isolation '{}' to {} deterministically classified single-cell row(s) because all accepted single-cell regimes {:?} independently adjudicate to the same canonical isolation value; refs={:?}",
            isolation_value, projected_rows, regime_ids, evidence_refs
        ),
    }]
}

fn validate_scientific_workspace_rows(
    headers: &[String],
    rows: &[Vec<String>],
    evidence: &DatasetEvidence,
    relation_mode: &str,
    explicit_mappings: &[ExplicitRowMapping],
    trusted_partial_mapping_applied: bool,
) -> Vec<ValidationIssue> {
    if unresolved_de_novo_multiplex_mapping(
        evidence,
        relation_mode,
        explicit_mappings,
        trusted_partial_mapping_applied,
    ) {
        // Preserve the mature SDRF-generator contract: when the acquisition is
        // reporter-multiplexed but the source does not provide an exact
        // sample/channel mapping, do not validate the one-row-per-RAW skeleton
        // as though it were a finalized per-channel SDRF. Doing so creates a
        // misleading row-error storm for one causal mapping gap.
        validate_incomplete_mapping_scaffold(headers, rows, evidence, relation_mode, false)
    } else {
        validate_annotation_draft(headers, rows, evidence, false)
    }
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
    compile_workspace_with_trusted_partial_sdrf(evidence, state, explicit_mappings, None)
}

fn compile_workspace_with_trusted_partial_sdrf(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    explicit_mappings: &[ExplicitRowMapping],
    trusted_partial_sdrf: Option<&Path>,
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
    harden_factor_isolation_project_baseline(&mut proposal, state, &mut issues);
    harden_factor_acquisition_project_baseline(&mut proposal, state, &mut issues);
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

    let (mut headers, mut rows, mut generation_mode) =
        draft_rows_with_explicit_mappings(&proposal, evidence, explicit_mappings)?;
    let row_scaffold_issues = enforce_deterministic_row_scaffold(
        &headers,
        &mut rows,
        evidence,
        &proposal.relation_mode,
        state.harness_version == SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION,
    );
    let has_unresolved_raw_roles = row_scaffold_issues
        .iter()
        .any(|issue| issue.code == "scientific_agent_raw_file_role_unresolved");
    issues.extend(row_scaffold_issues);

    let mut trusted_partial_mapping_applied = false;
    if state.harness_version == SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION
        && proposal.relation_mode == "multiplexed_cells_per_data_file"
        && evidence.existing_sdrf_path.is_empty()
        && explicit_mappings.is_empty()
    {
        if let Some(partial_path) = trusted_partial_sdrf {
            let (fused_headers, fused_rows, issue) =
                fuse_trusted_partial_sdrf_rows(&headers, &rows, evidence, partial_path)?;
            headers = fused_headers;
            rows = fused_rows;
            generation_mode = "generated_trusted_partial_sdrf_fusion".into();
            trusted_partial_mapping_applied = true;
            issues.push(issue);
        }
    }

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
                let Some((value, _)) = canonical_workspace_claim_value(evidence, state, claim)
                else {
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

    issues.extend(apply_consensus_single_cell_isolation_projection(
        &headers,
        &mut rows,
        evidence,
        state,
        &proposal.relation_mode,
        explicit_mappings,
        has_unresolved_raw_roles,
        trusted_partial_mapping_applied,
    ));

    issues.extend(validate_scientific_workspace_rows(
        &headers,
        &rows,
        evidence,
        &proposal.relation_mode,
        explicit_mappings,
        trusted_partial_mapping_applied,
    ));
    let adjudications = state
        .claims
        .iter()
        .map(|claim| adjudication_record(evidence, state, claim))
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

fn workspace_adjudications(
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
) -> Vec<ClaimAdjudicationRecord> {
    let mut records = state
        .claims
        .iter()
        .map(|claim| adjudication_record(evidence, state, claim))
        .collect::<Vec<_>>();

    // Template-gap safety is compiler-owned, not contingent on the model
    // successfully restating an observation. Keep a fail-closed evidence-level
    // adjudication visible when trusted inventory proves an unsupported
    // isolation method but no active observation currently exposes that gap.
    let has_isolation_template_gap = records.iter().any(|record| {
        record.concept_type == "isolation_method"
            && matches!(&record.adjudication, ClaimAdjudication::TemplateGap { .. })
    });
    if !has_isolation_template_gap {
        if let Some(gap) = infer_isolation_template_gap(&evidence.evidence) {
            records.push(ClaimAdjudicationRecord {
                concept_type: "isolation_method".into(),
                scope: "unresolved".into(),
                branch_id: "rust:evidence_template_gap".into(),
                model_status: "rust_evidence_bootstrap".into(),
                proposed_value: gap.observed_value.clone(),
                evidence_refs: gap.evidence_refs.clone(),
                adjudication: ClaimAdjudication::TemplateGap {
                    observed_value: gap.observed_value,
                    evidence_refs: gap.evidence_refs,
                    reason: gap.reason,
                },
            });
        }
    }
    records
}

fn adjudication_feedback_signature(record: &ClaimAdjudicationRecord) -> String {
    let outcome = serde_json::to_string(&record.adjudication).unwrap_or_default();
    format!(
        "{}|{}|{}",
        record.proposed_value.trim().to_ascii_lowercase(),
        record.evidence_refs.join(","),
        outcome
    )
}

fn adjudication_feedback_message(
    record: &ClaimAdjudicationRecord,
    turn: usize,
    phase: &str,
) -> Option<String> {
    if !matches!(
        record.concept_type.as_str(),
        "isolation_method" | "acquisition_mode"
    ) {
        return None;
    }
    Some(match &record.adjudication {
        ClaimAdjudication::Canonical {
            value,
            evidence_refs,
        } => format!(
            "turn {} {} compiler adjudicated {} observation '{}' -> canonical '{}' from refs {:?}",
            turn, phase, record.concept_type, record.proposed_value, value, evidence_refs
        ),
        ClaimAdjudication::TemplateGap {
            observed_value,
            evidence_refs,
            ..
        } => format!(
            "turn {} {} compiler adjudicated {} observation '{}' as template gap '{}' from refs {:?}; do not substitute a nearby allowed value",
            turn,
            phase,
            record.concept_type,
            record.proposed_value,
            observed_value,
            evidence_refs
        ),
        ClaimAdjudication::Unresolved { reason } | ClaimAdjudication::Conflict { reason } => {
            format!(
                "turn {} {} compiler could not publish {} observation '{}' from refs {:?}: {}; search for direct field-specific method evidence or leave unresolved",
                turn,
                phase,
                record.concept_type,
                record.proposed_value,
                record.evidence_refs,
                reason
            )
        }
        ClaimAdjudication::SupportedConcept { .. } => return None,
    })
}

fn append_changed_adjudication_feedback(
    trace_feedback: &mut Vec<String>,
    pending_feedback: &mut Vec<String>,
    last_signatures: &mut BTreeMap<(String, String, String), String>,
    records: &[ClaimAdjudicationRecord],
    turn: usize,
    phase: &str,
) {
    let active = records
        .iter()
        .map(|record| claim_identity_parts(&record.concept_type, &record.scope, &record.branch_id))
        .collect::<BTreeSet<_>>();
    last_signatures.retain(|identity, _| active.contains(identity));

    for record in records {
        let identity = claim_identity_parts(&record.concept_type, &record.scope, &record.branch_id);
        let signature = adjudication_feedback_signature(record);
        if last_signatures
            .get(&identity)
            .is_some_and(|previous| previous == &signature)
        {
            continue;
        }
        last_signatures.insert(identity, signature);
        if let Some(message) = adjudication_feedback_message(record, turn, phase) {
            trace_feedback.push(message.clone());
            pending_feedback.push(message);
        }
    }
}

fn push_harness_feedback(
    trace_feedback: &mut Vec<String>,
    pending_feedback: &mut Vec<String>,
    message: String,
) {
    trace_feedback.push(message.clone());
    pending_feedback.push(message);
}

fn record_adjudication_snapshot(
    trace: &mut ScientificAgentTrace,
    evidence: &DatasetEvidence,
    state: &ScientificWorkspaceState,
    turn: usize,
    phase: &str,
) -> Vec<ClaimAdjudicationRecord> {
    let records = workspace_adjudications(evidence, state);
    trace.adjudication_history.push(AdjudicationSnapshot {
        turn,
        phase: phase.into(),
        evidence_items: evidence.evidence.len(),
        records: records.clone(),
    });
    records
}

fn command_matches_active_task(state: &ScientificWorkspaceState, command: &AgentCommand) -> bool {
    if state.active_task_id.is_empty() || command.task_id() != state.active_task_id {
        return false;
    }
    match command {
        AgentCommand::EditStudyStructure { .. } => state.active_task_id == "task:study_structure",
        AgentCommand::EditScientificObservation { .. } => {
            state.active_task_id != "task:study_structure"
        }
        _ => true,
    }
}

fn record_non_edit_task_attempt(state: &mut ScientificWorkspaceState, task_id: &str, note: &str) {
    if let Some(task) = state.tasks.iter_mut().find(|task| task.id == task_id) {
        task.attempts += 1;
        if task.status == "open" {
            task.status = "investigating".into();
        }
        if !note.trim().is_empty() {
            task.notes = note.trim().to_string();
        }
    }
}

fn task_target_concepts(state: &ScientificWorkspaceState, task_id: &str) -> Vec<String> {
    state
        .tasks
        .iter()
        .find(|task| task.id == task_id)
        .and_then(|task| {
            scientific_concept_types()
                .contains(&task.concept_type.as_str())
                .then_some(vec![task.concept_type.clone()])
        })
        .unwrap_or_default()
}

fn apply_edit_workspace_command(
    evidence: &DatasetEvidence,
    state: &mut ScientificWorkspaceState,
    task_id: &str,
    branch_upserts: &[AgentBranch],
    observation_upserts: &[ScientificObservationUpsert],
    observation_retractions: &[ScientificObservationRetraction],
    open_question_additions: &[String],
    open_question_resolutions: &[String],
    notes: &str,
    turn: usize,
) -> (Vec<String>, bool) {
    let publishable_edit = !branch_upserts.is_empty()
        || !observation_upserts.is_empty()
        || !observation_retractions.is_empty();
    let delta = WorkspaceDelta {
        turn,
        task_id: task_id.to_string(),
        task_status: "continue".into(),
        branch_upserts: branch_upserts.to_vec(),
        claim_upserts: observation_upserts
            .iter()
            .map(ScientificObservationUpsert::as_claim_upsert)
            .collect(),
        claim_retractions: observation_retractions
            .iter()
            .map(ScientificObservationRetraction::as_claim_retraction)
            .collect(),
        open_question_additions: open_question_additions.to_vec(),
        open_question_resolutions: open_question_resolutions.to_vec(),
        conflict_additions: Vec::new(),
        conflict_resolutions: Vec::new(),
        next_evidence_actions: Vec::new(),
        next_step: "compile".into(),
        notes: notes.to_string(),
    };
    let mut events = apply_workspace_delta(evidence, state, &delta, turn);

    if task_id == "task:study_structure" && publishable_edit {
        if let Some(task) = state.tasks.iter_mut().find(|task| task.id == task_id) {
            task.status = "resolved".into();
            if !notes.trim().is_empty() {
                task.notes = notes.trim().to_string();
            }
        }
        state.active_task_id.clear();
        ensure_active_task(state);
        events.push(format!(
            "turn {turn} Rust marked task:study_structure resolved after a material source-grounded workspace edit and advanced the task board"
        ));
    }
    state.next_evidence_actions.clear();
    state.next_step = "compile".into();
    (events, publishable_edit)
}

fn search_actions_for_command(
    evidence: &DatasetEvidence,
    actions: &[AgentEvidenceAction],
) -> Vec<AgentEvidenceAction> {
    let allowed = [
        "SEARCH_PUBLICATION",
        "SEARCH_SUPPLEMENT",
        "SEARCH_STRUCTURED_DESIGN",
        "SEARCH_REPOSITORY_METADATA",
        "SEARCH_EXACT_RAW_NAME",
        "EXPAND_EVIDENCE_CONTEXT",
        "LOOKUP_KG_TERM",
        "COMPARE_CONFLICTING_EVIDENCE",
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    let mut out = actions.to_vec();
    for action in &mut out {
        normalize_scientific_agent_action(evidence, action);
    }
    out.retain(|action| allowed.contains(action.action.as_str()) && !action.queries.is_empty());
    out.truncate(4);
    out
}

fn trim_actions_to_budget(
    actions: &[AgentEvidenceAction],
    remaining: usize,
) -> Vec<AgentEvidenceAction> {
    let mut left = remaining;
    let mut out = Vec::new();
    for action in actions {
        if left == 0 {
            break;
        }
        let mut action = action.clone();
        if action.action == "ABSTAIN" {
            action.queries.clear();
            action.evidence_refs.clear();
            out.push(action);
            left = left.saturating_sub(1);
            continue;
        }
        if action.action == "READ_EVIDENCE_CONTEXT" {
            action.evidence_refs.truncate(left.min(8));
            action.queries.clear();
            if !action.evidence_refs.is_empty() {
                left = left.saturating_sub(action.evidence_refs.len());
                out.push(action);
            }
            continue;
        }
        action.queries.truncate(left.min(8));
        action.evidence_refs.clear();
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
    fs::write(workspace_dir.join("action_history.json"), "[]\n")?;
    fs::write(workspace_dir.join("command_history.json"), "[]\n")?;
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

    let mut pending_harness_feedback = Vec::new();
    let mut last_adjudication_signatures: BTreeMap<(String, String, String), String> =
        BTreeMap::new();

    // Codex-style bootstrap: compile and validate the deterministic baseline
    // before asking the model to reason. The first agent turn therefore sees
    // the actual unresolved scientific tasks instead of inventing a parallel
    // replacement for already-working row structure.
    let baseline = compile_workspace(&evidence, &state, &explicit_mappings)?;
    fs::write(
        workspace_dir.join("adjudications.json"),
        serde_json::to_string_pretty(&baseline.adjudications)?,
    )?;
    trace.adjudication_history.push(AdjudicationSnapshot {
        turn: 0,
        phase: "baseline_compile".into(),
        evidence_items: evidence.evidence.len(),
        records: baseline.adjudications.clone(),
    });
    write_sdrf(&draft_path, &baseline.headers, &baseline.rows)?;
    write_validation_review(&review_path, &baseline.issues)?;
    trace.validator_cycles_completed = 1;
    let baseline_cycle = validation_cycle(1, &baseline.issues);
    let baseline_errors = baseline_cycle.validation_errors;
    trace.validation_history.push(baseline_cycle);
    refresh_scientific_tasks(&evidence, &mut state, &baseline.issues);
    trace.states[0] = state.clone();
    fs::write(
        workspace_dir.join("state.turn00.json"),
        serde_json::to_string_pretty(&state)?,
    )?;
    fs::write(
        workspace_dir.join("state.json"),
        serde_json::to_string_pretty(&state)?,
    )?;
    write_workspace_notebook(
        &workspace_dir,
        &evidence,
        &state,
        &baseline.adjudications,
        &trace.validation_history,
    )?;
    push_harness_feedback(
        &mut trace.harness_feedback,
        &mut pending_harness_feedback,
        format!(
            "deterministic baseline compiled before agent turn 1 with {} validation error(s); preserve working baseline fields and focus only on unresolved scientific concepts",
            baseline_errors
        ),
    );
    fs::write(
        workspace_dir.join("validation_history.json"),
        serde_json::to_string_pretty(&trace.validation_history)?,
    )?;
    fs::write(
        workspace_dir.join("adjudication_history.json"),
        serde_json::to_string_pretty(&trace.adjudication_history)?,
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
            ensure_active_task(&mut state);
            if state.active_task_id.is_empty() {
                trace.terminal_status = "partial_no_active_scientific_task".into();
                break;
            }

            let command = call_scientific_agent(
                opts,
                &evidence,
                &state,
                &trace.evidence_action_results,
                &trace.validation_history,
                &pending_harness_feedback,
                turn,
            )
            .await?;
            pending_harness_feedback.clear();

            fs::write(
                workspace_dir.join(format!("command.turn{turn:02}.json")),
                serde_json::to_string_pretty(&command)?,
            )?;
            trace.commands.push(command.clone());
            fs::write(
                workspace_dir.join("command_history.json"),
                serde_json::to_string_pretty(&trace.commands)?,
            )?;
            trace.turns_completed = turn;
            state.turn = turn;

            if !command_matches_active_task(&state, &command) {
                push_harness_feedback(
                    &mut trace.harness_feedback,
                    &mut pending_harness_feedback,
                    format!(
                        "turn {turn} command rejected because task_id='{}' does not match active task '{}'; issue exactly one command for the active task",
                        command.task_id(), state.active_task_id
                    ),
                );
                trace.states.push(state.clone());
                continue;
            }

            match &command {
                AgentCommand::ReadEvidence {
                    task_id,
                    evidence_refs,
                    reason,
                } => {
                    record_non_edit_task_attempt(&mut state, task_id, reason);
                    let remaining = max_actions.saturating_sub(trace.tool_actions_completed);
                    if remaining == 0 {
                        mark_active_task_human_review(
                            &mut state,
                            "evidence/tool budget exhausted before requested source context could be read",
                        );
                        trace.states.push(state.clone());
                        if state.active_task_id.is_empty() {
                            trace.terminal_status = "evidence_exhausted".into();
                            break;
                        }
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            "tool budget exhausted; Rust routed the previous task to human review and advanced the task board".into(),
                        );
                        continue;
                    }
                    if active_task(&state).is_some_and(|task| task.decision_required) {
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            format!(
                                "turn {turn} read_evidence was rejected because the task is in a decision step after a prior read. Use the task-specific typed edit command, search_evidence with a genuinely different trusted query/source, or escalate."
                            ),
                        );
                        trace.states.push(state.clone());
                        continue;
                    }
                    let valid_requested = valid_evidence_refs(&evidence, evidence_refs);
                    let mut refs =
                        unread_evidence_refs_for_task(&evidence, &state, task_id, &valid_requested);
                    refs.truncate(remaining.min(4));
                    if refs.is_empty() {
                        set_task_decision_required(&mut state, task_id, true);
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            format!(
                                "turn {turn} read_evidence contained only invalid or already-read E#### refs. Rust has locked further reads for this task until you use the task-specific typed edit command, run a genuinely new trusted search_evidence query, or escalate."
                            ),
                        );
                        trace.states.push(state.clone());
                        continue;
                    }
                    let read_action = AgentEvidenceAction {
                        action: "READ_EVIDENCE_CONTEXT".into(),
                        reason: reason.clone(),
                        target_concepts: task_target_concepts(&state, task_id),
                        queries: Vec::new(),
                        evidence_refs: refs.clone(),
                    };
                    let round_results = execute_scientific_agent_actions(
                        &mut evidence,
                        &[read_action],
                        turn,
                        &mut attempted,
                    );
                    trace.tool_actions_completed += evidence_action_count(&round_results);
                    record_task_reads(&mut state, task_id, &refs, &round_results);
                    trace.evidence_action_results.extend(round_results);
                    if let Some(current) = compiled.as_ref() {
                        refresh_scientific_tasks(&evidence, &mut state, &current.issues);
                    }
                    normalize_workspace_state(&evidence, &mut state, turn);
                    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
                    fs::write(
                        workspace_dir.join("action_history.json"),
                        serde_json::to_string_pretty(&trace.evidence_action_results)?,
                    )?;
                    let adjudications = record_adjudication_snapshot(
                        &mut trace,
                        &evidence,
                        &state,
                        turn,
                        "post_read",
                    );
                    append_changed_adjudication_feedback(
                        &mut trace.harness_feedback,
                        &mut pending_harness_feedback,
                        &mut last_adjudication_signatures,
                        &adjudications,
                        turn,
                        "post-read",
                    );
                    fs::write(
                        workspace_dir.join("adjudications.json"),
                        serde_json::to_string_pretty(&adjudications)?,
                    )?;
                    fs::write(
                        workspace_dir.join("adjudication_history.json"),
                        serde_json::to_string_pretty(&trace.adjudication_history)?,
                    )?;
                    trace.states.push(state.clone());
                    fs::write(
                        workspace_dir.join(format!("state.turn{turn:02}.json")),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    fs::write(
                        workspace_dir.join("state.json"),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    write_workspace_notebook(
                        &workspace_dir,
                        &evidence,
                        &state,
                        &adjudications,
                        &trace.validation_history,
                    )?;
                    continue;
                }
                AgentCommand::SearchEvidence {
                    task_id,
                    actions,
                    reason,
                } => {
                    record_non_edit_task_attempt(&mut state, task_id, reason);
                    let remaining = max_actions.saturating_sub(trace.tool_actions_completed);
                    if remaining == 0 {
                        mark_active_task_human_review(
                            &mut state,
                            "evidence/tool budget exhausted before requested targeted search could run",
                        );
                        trace.states.push(state.clone());
                        if state.active_task_id.is_empty() {
                            trace.terminal_status = "evidence_exhausted".into();
                            break;
                        }
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            "tool budget exhausted; Rust routed the previous task to human review and advanced the task board".into(),
                        );
                        continue;
                    }
                    let normalized = search_actions_for_command(&evidence, actions);
                    let executable = trim_actions_to_budget(&normalized, remaining);
                    if executable.is_empty() {
                        set_task_decision_required(&mut state, task_id, true);
                        set_task_search_blocked(&mut state, task_id, true);
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            format!(
                                "turn {turn} search_evidence contained no executable normalized search query. Rust has closed further evidence gathering for this task; use the task-specific typed edit command with current evidence or escalate."
                            ),
                        );
                        trace.states.push(state.clone());
                        continue;
                    }
                    let round_results = execute_scientific_agent_actions(
                        &mut evidence,
                        &executable,
                        turn,
                        &mut attempted,
                    );
                    let executed_actions = evidence_action_count(&round_results);
                    let produced_new_evidence = round_results.iter().any(|result| {
                        result.outcome == "matched" && !result.matched_evidence_refs.is_empty()
                    });
                    trace.tool_actions_completed += executed_actions;
                    set_task_decision_required(&mut state, task_id, !produced_new_evidence);
                    set_task_search_blocked(&mut state, task_id, !produced_new_evidence);
                    trace.evidence_action_results.extend(round_results);
                    if let Some(current) = compiled.as_ref() {
                        refresh_scientific_tasks(&evidence, &mut state, &current.issues);
                    }
                    normalize_workspace_state(&evidence, &mut state, turn);
                    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
                    fs::write(
                        workspace_dir.join("action_history.json"),
                        serde_json::to_string_pretty(&trace.evidence_action_results)?,
                    )?;
                    let adjudications = record_adjudication_snapshot(
                        &mut trace,
                        &evidence,
                        &state,
                        turn,
                        "post_search",
                    );
                    append_changed_adjudication_feedback(
                        &mut trace.harness_feedback,
                        &mut pending_harness_feedback,
                        &mut last_adjudication_signatures,
                        &adjudications,
                        turn,
                        "post-search",
                    );
                    fs::write(
                        workspace_dir.join("adjudications.json"),
                        serde_json::to_string_pretty(&adjudications)?,
                    )?;
                    fs::write(
                        workspace_dir.join("adjudication_history.json"),
                        serde_json::to_string_pretty(&trace.adjudication_history)?,
                    )?;
                    trace.states.push(state.clone());
                    fs::write(
                        workspace_dir.join(format!("state.turn{turn:02}.json")),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    fs::write(
                        workspace_dir.join("state.json"),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    write_workspace_notebook(
                        &workspace_dir,
                        &evidence,
                        &state,
                        &adjudications,
                        &trace.validation_history,
                    )?;
                    continue;
                }
                AgentCommand::Escalate { task_id, reason } => {
                    record_non_edit_task_attempt(&mut state, task_id, reason);
                    mark_active_task_human_review(&mut state, reason);
                    let adjudications = record_adjudication_snapshot(
                        &mut trace,
                        &evidence,
                        &state,
                        turn,
                        "post_escalation",
                    );
                    trace.states.push(state.clone());
                    fs::write(
                        workspace_dir.join(format!("state.turn{turn:02}.json")),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    fs::write(
                        workspace_dir.join("state.json"),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    write_workspace_notebook(
                        &workspace_dir,
                        &evidence,
                        &state,
                        &adjudications,
                        &trace.validation_history,
                    )?;
                    if state.active_task_id.is_empty() {
                        trace.terminal_status = "human_review".into();
                        break;
                    }
                    push_harness_feedback(
                        &mut trace.harness_feedback,
                        &mut pending_harness_feedback,
                        "previous task escalated to human review; Rust advanced to the next scientific task".into(),
                    );
                    continue;
                }
                AgentCommand::EditStudyStructure { .. }
                | AgentCommand::EditScientificObservation { .. } => {
                    let edit = command
                        .workspace_edit_payload()
                        .expect("typed edit command must expose a workspace edit payload");
                    set_task_decision_required(&mut state, &edit.task_id, false);
                    set_task_search_blocked(&mut state, &edit.task_id, false);
                    let (reducer_events, publishable_edit) = apply_edit_workspace_command(
                        &evidence,
                        &mut state,
                        &edit.task_id,
                        &edit.branch_upserts,
                        &edit.observation_upserts,
                        &edit.observation_retractions,
                        &edit.open_question_additions,
                        &edit.open_question_resolutions,
                        &edit.notes,
                        turn,
                    );
                    for event in reducer_events {
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            event,
                        );
                    }

                    let post_edit_adjudications = record_adjudication_snapshot(
                        &mut trace,
                        &evidence,
                        &state,
                        turn,
                        "post_edit",
                    );
                    append_changed_adjudication_feedback(
                        &mut trace.harness_feedback,
                        &mut pending_harness_feedback,
                        &mut last_adjudication_signatures,
                        &post_edit_adjudications,
                        turn,
                        "post-edit",
                    );
                    fs::write(
                        workspace_dir.join("adjudications.json"),
                        serde_json::to_string_pretty(&post_edit_adjudications)?,
                    )?;
                    fs::write(
                        workspace_dir.join("adjudication_history.json"),
                        serde_json::to_string_pretty(&trace.adjudication_history)?,
                    )?;
                    trace.states.push(state.clone());
                    fs::write(
                        workspace_dir.join(format!("state.turn{turn:02}.json")),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    fs::write(
                        workspace_dir.join("state.json"),
                        serde_json::to_string_pretty(&state)?,
                    )?;

                    if !publishable_edit {
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            format!(
                                "turn {turn} typed edit changed no branch or scientific observation; Rust skipped compilation/validation"
                            ),
                        );
                        write_workspace_notebook(
                            &workspace_dir,
                            &evidence,
                            &state,
                            &post_edit_adjudications,
                            &trace.validation_history,
                        )?;
                        continue;
                    }

                    let compiled_now = compile_workspace(&evidence, &state, &explicit_mappings)?;
                    fs::write(
                        workspace_dir.join("adjudications.json"),
                        serde_json::to_string_pretty(&compiled_now.adjudications)?,
                    )?;
                    trace.adjudication_history.push(AdjudicationSnapshot {
                        turn,
                        phase: "compile_after_edit".into(),
                        evidence_items: evidence.evidence.len(),
                        records: compiled_now.adjudications.clone(),
                    });
                    append_changed_adjudication_feedback(
                        &mut trace.harness_feedback,
                        &mut pending_harness_feedback,
                        &mut last_adjudication_signatures,
                        &compiled_now.adjudications,
                        turn,
                        "compile-after-edit",
                    );
                    fs::write(
                        workspace_dir.join("adjudication_history.json"),
                        serde_json::to_string_pretty(&trace.adjudication_history)?,
                    )?;
                    refresh_scientific_tasks(&evidence, &mut state, &compiled_now.issues);

                    if last_compile_fingerprint
                        .as_deref()
                        .is_some_and(|previous| previous == compiled_now.fingerprint.as_str())
                    {
                        let adjudications = compiled_now.adjudications.clone();
                        compiled = Some(compiled_now);
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            format!(
                                "turn {turn} edit produced the same deterministic SDRF fingerprint as the previous validated compile; Rust skipped a validator cycle. Read/search more evidence, refine the scientific observation/scope, or escalate."
                            ),
                        );
                        fs::write(
                            workspace_dir.join("state.json"),
                            serde_json::to_string_pretty(&state)?,
                        )?;
                        write_workspace_notebook(
                            &workspace_dir,
                            &evidence,
                            &state,
                            &adjudications,
                            &trace.validation_history,
                        )?;
                        continue;
                    }

                    if trace.validator_cycles_completed >= max_validator_cycles {
                        push_harness_feedback(
                            &mut trace.harness_feedback,
                            &mut pending_harness_feedback,
                            "validator-cycle budget exhausted before the new edited draft could be validated".into(),
                        );
                        trace.terminal_status = "validation_exhausted".into();
                        break;
                    }

                    last_compile_fingerprint = Some(compiled_now.fingerprint.clone());
                    write_sdrf(&draft_path, &compiled_now.headers, &compiled_now.rows)?;
                    write_validation_review(&review_path, &compiled_now.issues)?;
                    trace.validator_cycles_completed += 1;
                    let cycle =
                        validation_cycle(trace.validator_cycles_completed, &compiled_now.issues);
                    let errors = cycle.validation_errors;
                    trace.validation_history.push(cycle);
                    fs::write(
                        workspace_dir.join("validation_history.json"),
                        serde_json::to_string_pretty(&trace.validation_history)?,
                    )?;
                    refresh_scientific_tasks(&evidence, &mut state, &compiled_now.issues);
                    let adjudications = compiled_now.adjudications.clone();
                    compiled = Some(compiled_now);
                    fs::write(
                        workspace_dir.join("state.json"),
                        serde_json::to_string_pretty(&state)?,
                    )?;
                    write_workspace_notebook(
                        &workspace_dir,
                        &evidence,
                        &state,
                        &adjudications,
                        &trace.validation_history,
                    )?;

                    if errors == 0 {
                        trace.terminal_status = "resolved".into();
                        break;
                    }
                    if state.active_task_id.is_empty() {
                        trace.terminal_status = "partial_no_active_scientific_task".into();
                        break;
                    }
                    if trace.validator_cycles_completed >= max_validator_cycles {
                        trace.terminal_status = "validation_exhausted".into();
                        break;
                    }
                }
            }
        }
    }

    // `compiled` always contains at least the deterministic baseline. Final
    // audit adjudications below are recomputed from canonical state so a
    // retained claim remains visible even if the final turn was a search and
    // no additional validator cycle was requested.
    if trace.terminal_status.is_empty() {
        trace.terminal_status = if trace.turns_completed >= max_turns {
            "turn_budget_exhausted".into()
        } else {
            "partial".into()
        };
    }
    if let Some(last_state) = trace.states.last_mut() {
        *last_state = state.clone();
    }
    fs::write(
        workspace_dir.join("state.json"),
        serde_json::to_string_pretty(&state)?,
    )?;
    let compiled = compiled.unwrap();
    let validation_errors = compiled
        .issues
        .iter()
        .filter(|issue| issue.level == "error")
        .count();
    let locally_valid = validation_errors == 0;
    let final_adjudications = workspace_adjudications(&evidence, &state);
    fs::write(
        workspace_dir.join("adjudications.json"),
        serde_json::to_string_pretty(&final_adjudications)?,
    )?;
    fs::write(
        workspace_dir.join("adjudication_history.json"),
        serde_json::to_string_pretty(&trace.adjudication_history)?,
    )?;
    write_workspace_notebook(
        &workspace_dir,
        &evidence,
        &state,
        &final_adjudications,
        &trace.validation_history,
    )?;

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
        "scientific_observations": state.claims.clone(),
        "observation_adjudications": final_adjudications.clone(),
        // Backward-compatible aliases for existing audit tooling. v1.3 model
        // commands and prompts use observation terminology exclusively.
        "claims": state.claims.clone(),
        "claim_adjudications": final_adjudications,
        "open_questions": state.open_questions.clone(),
        "conflicts": state.conflicts.clone(),
        "tasks": state.tasks.clone(),
        "active_task_id": state.active_task_id.clone(),
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
    let requested_mode = std::env::var("PRIDE_SCP_SCIENTIFIC_AGENT_MODE").unwrap_or_default();
    if requested_mode == SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_MODE {
        return run_factor_row_role_hardened(opts).await;
    }
    if requested_mode == SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_MODE {
        return run_factor_semantic_fidelity_hardened(opts).await;
    }
    if requested_mode == SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_MODE {
        return run_factor_canonical_hardened(opts).await;
    }
    if requested_mode == SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_MODE {
        return run_factor_deterministic_bridge(opts).await;
    }
    if requested_mode == SCIENTIFIC_AGENT_FACTOR_PHASE_B_MODE {
        return run_factor_phase_b(opts).await;
    }
    if requested_mode == SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_MODE {
        return run_factor_graph_stage1(opts).await;
    }
    if requested_mode == SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_MODE {
        return run_study_graph_stage1(opts).await;
    }
    if !requested_mode.trim().is_empty() {
        bail!(
            "unsupported PRIDE_SCP_SCIENTIFIC_AGENT_MODE='{}'; expected '{}', '{}', '{}', '{}', '{}', '{}', or '{}' or unset for the v1.3 workspace agent",
            requested_mode,
            SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_MODE,
            SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_MODE,
            SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_MODE,
            SCIENTIFIC_AGENT_FACTOR_DETERMINISTIC_BRIDGE_MODE,
            SCIENTIFIC_AGENT_FACTOR_PHASE_B_MODE,
            SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_MODE,
            SCIENTIFIC_AGENT_STUDY_GRAPH_STAGE1_MODE
        );
    }
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
    fn evidence_equivalent_observation_phrasings_merge_without_workspace_conflict() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "an individual intact cell was manually loaded into the separation capillary using hydrodynamic pressure".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            claims: vec![claim("isolation_method", "manual picking", "project", "")],
            ..Default::default()
        };
        let observation = ScientificObservationUpsert {
            concept_type: "isolation_method".into(),
            observed_value: "an individual intact cell was manually loaded into the separation capillary using hydrodynamic pressure".into(),
            scope: "project".into(),
            branch_id: String::new(),
            status: "supported".into(),
            evidence_refs: vec!["E0001".into()],
            confidence: "high".into(),
            reason: "source-faithful description of the physical isolation/loading operation".into(),
            supersedes_observed_value: String::new(),
        };
        let (_events, publishable) = apply_edit_workspace_command(
            &evidence,
            &mut state,
            "",
            &[],
            &[observation],
            &[],
            &[],
            &[],
            "refine observation wording without changing Rust canonical outcome",
            1,
        );
        assert!(publishable);
        assert!(state.conflicts.is_empty());
        assert!(state.claims[0].value.contains("hydrodynamic pressure"));
        assert!(matches!(
            adjudicate_workspace_claim(&evidence, &state, &state.claims[0]),
            ClaimAdjudication::Canonical { ref value, .. } if value == "manual picking"
        ));
    }

    #[test]
    fn compiler_owned_template_gap_remains_visible_without_model_observation() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:sampleProcessingProtocol".into(),
                text: "The picked single-cell was immediately transferred into the microwell on the chip before extraction and digestion.".into(),
            }],
            vec!["runA.raw"],
        );
        let state = ScientificWorkspaceState::default();
        let records = workspace_adjudications(&evidence, &state);
        let record = records
            .iter()
            .find(|record| record.concept_type == "isolation_method")
            .expect("Rust evidence bootstrap should expose the template gap");
        assert_eq!(record.model_status, "rust_evidence_bootstrap");
        assert!(matches!(
            record.adjudication,
            ClaimAdjudication::TemplateGap { ref observed_value, .. }
                if observed_value == "microwell-chip single-cell transfer"
        ));
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
    fn conflicting_values_reduce_to_one_active_claim_and_explicit_conflict() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "organism".into(),
                text: "human and mouse organisms are both described".into(),
            }],
            vec!["runA.raw"],
        );
        let mut claims = vec![
            claim("organism", "Homo sapiens", "project", ""),
            claim("organism", "Mus musculus", "project", ""),
        ];
        let mut conflicts = Vec::new();
        reduce_scientific_claims(&evidence, &BTreeSet::new(), &mut claims, &mut conflicts, 1);
        assert_eq!(claims.len(), 1);
        assert_eq!(conflicts.len(), 1);
        let state = ScientificWorkspaceState {
            claims: claims.clone(),
            conflicts,
            ..Default::default()
        };
        assert!(matches!(
            adjudicate_workspace_claim(&evidence, &state, &state.claims[0]),
            ClaimAdjudication::Conflict { .. }
        ));
    }

    #[test]
    fn workspace_delta_omission_preserves_hydrodynamic_claim_and_allows_rust_adjudication() {
        let evidence = evidence_with(
            vec![
                EvidenceItem {
                    id: "E0021".into(),
                    source_kind: "manuscript_semantic_evidence".into(),
                    source_label: "single cell isolation method".into(),
                    text: "individual single cells were introduced into the capillary by hydrodynamic injection".into(),
                },
                EvidenceItem {
                    id: "E0029".into(),
                    source_kind: "manuscript_semantic_evidence".into(),
                    source_label: "single cell isolation method".into(),
                    text: "hydrodynamic loading was followed by on-capillary lysis".into(),
                },
            ],
            vec!["runA.raw"],
        );
        let mut persistent = claim_with_status(
            "isolation_method",
            "Hydrodynamic/ESI injection + On-capillary lysis",
            "project",
            "",
            "hypothesis",
        );
        persistent.evidence_refs = vec!["E0021".into(), "E0029".into()];
        let mut state = ScientificWorkspaceState {
            claims: vec![persistent],
            next_step: "compile".into(),
            ..Default::default()
        };
        normalize_workspace_state(&evidence, &mut state, 1);

        // The next model turn deliberately says nothing about isolation.
        let delta = WorkspaceDelta {
            turn: 2,
            next_step: "finish".into(),
            notes: "no isolation update".into(),
            ..Default::default()
        };
        apply_workspace_delta(&evidence, &mut state, &delta, 2);

        assert_eq!(state.claims.len(), 1);
        assert_eq!(
            state.claims[0].value,
            "Hydrodynamic/ESI injection + On-capillary lysis"
        );
        assert_eq!(state.claims[0].evidence_refs, vec!["E0021", "E0029"]);
        match adjudicate_workspace_claim(&evidence, &state, &state.claims[0]) {
            ClaimAdjudication::Canonical { value, .. } => assert_eq!(value, "manual picking"),
            other => panic!("expected preserved isolation claim to be adjudicated, got {other:?}"),
        }
    }

    #[test]
    fn same_value_delta_merges_and_deduplicates_evidence() {
        let evidence = evidence_with(
            vec![
                EvidenceItem {
                    id: "E0001".into(),
                    source_kind: "manuscript_semantic_evidence".into(),
                    source_label: "single cell isolation method".into(),
                    text: "single cells were loaded hydrodynamically".into(),
                },
                EvidenceItem {
                    id: "E0002".into(),
                    source_kind: "manuscript_semantic_evidence".into(),
                    source_label: "single cell isolation method".into(),
                    text: "hydrodynamic capillary loading of individual cells".into(),
                },
            ],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            claims: vec![claim_with_status(
                "isolation_method",
                "hydrodynamic loading",
                "project",
                "",
                "hypothesis",
            )],
            next_step: "compile".into(),
            ..Default::default()
        };
        let delta = WorkspaceDelta {
            turn: 2,
            claim_upserts: vec![ScientificClaimUpsert {
                concept_type: "isolation_method".into(),
                value: "hydrodynamic loading".into(),
                scope: "project".into(),
                branch_id: String::new(),
                status: "hypothesis".into(),
                evidence_refs: vec!["E0001".into(), "E0002".into(), "E0002".into()],
                confidence: "high".into(),
                reason: "additional direct method evidence".into(),
                supersedes_value: String::new(),
            }],
            next_step: "compile".into(),
            ..Default::default()
        };
        apply_workspace_delta(&evidence, &mut state, &delta, 2);
        assert_eq!(state.claims.len(), 1);
        assert_eq!(state.claims[0].evidence_refs, vec!["E0001", "E0002"]);
    }

    #[test]
    fn changed_value_without_supersession_records_conflict_and_retains_active_claim() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "organism".into(),
                text: "Homo sapiens cells were analyzed".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            claims: vec![claim("organism", "Homo sapiens", "project", "")],
            next_step: "compile".into(),
            ..Default::default()
        };
        let delta = WorkspaceDelta {
            turn: 2,
            claim_upserts: vec![ScientificClaimUpsert {
                concept_type: "organism".into(),
                value: "Mus musculus".into(),
                scope: "project".into(),
                branch_id: String::new(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "alternative interpretation".into(),
                supersedes_value: String::new(),
            }],
            next_step: "compile".into(),
            ..Default::default()
        };
        apply_workspace_delta(&evidence, &mut state, &delta, 2);
        assert_eq!(state.claims.len(), 1);
        assert_eq!(state.claims[0].value, "Homo sapiens");
        assert_eq!(state.conflicts.len(), 1);
        assert!(matches!(
            adjudicate_workspace_claim(&evidence, &state, &state.claims[0]),
            ClaimAdjudication::Conflict { .. }
        ));
    }

    #[test]
    fn explicit_supersession_replaces_value_and_clears_identity_conflict() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "organism".into(),
                text: "Mus musculus cells were analyzed".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            claims: vec![claim("organism", "Homo sapiens", "project", "")],
            conflicts: vec![WorkspaceConflict {
                concept_type: "organism".into(),
                scope: "project".into(),
                branch_id: String::new(),
                existing_value: "Homo sapiens".into(),
                proposed_value: "Mus musculus".into(),
                evidence_refs: vec!["E0001".into()],
                reason: "test conflict".into(),
                turn: 1,
            }],
            next_step: "compile".into(),
            ..Default::default()
        };
        let delta = WorkspaceDelta {
            turn: 2,
            claim_upserts: vec![ScientificClaimUpsert {
                concept_type: "organism".into(),
                value: "Mus musculus".into(),
                scope: "project".into(),
                branch_id: String::new(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "explicitly supersede after reviewing direct evidence".into(),
                supersedes_value: "Homo sapiens".into(),
            }],
            next_step: "compile".into(),
            ..Default::default()
        };
        apply_workspace_delta(&evidence, &mut state, &delta, 2);
        assert_eq!(state.claims.len(), 1);
        assert_eq!(state.claims[0].value, "Mus musculus");
        assert!(state.conflicts.is_empty());
    }

    #[test]
    fn explicit_retraction_is_required_to_remove_persistent_claim() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "organism".into(),
                text: "Homo sapiens cells were analyzed".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            claims: vec![claim("organism", "Homo sapiens", "project", "")],
            next_step: "compile".into(),
            ..Default::default()
        };
        let no_change = WorkspaceDelta {
            turn: 2,
            next_step: "finish".into(),
            ..Default::default()
        };
        apply_workspace_delta(&evidence, &mut state, &no_change, 2);
        assert_eq!(state.claims.len(), 1);

        let retract = WorkspaceDelta {
            turn: 3,
            claim_retractions: vec![ScientificClaimRetraction {
                concept_type: "organism".into(),
                scope: "project".into(),
                branch_id: String::new(),
                expected_value: "Homo sapiens".into(),
                reason: "direct evidence invalidated the proposal".into(),
            }],
            next_step: "finish".into(),
            ..Default::default()
        };
        apply_workspace_delta(&evidence, &mut state, &retract, 3);
        assert!(state.claims.is_empty());
    }

    #[test]
    fn branch_omission_in_delta_does_not_delete_canonical_branch() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "paper.txt".into(),
                text: "HeLa experimental branch".into(),
            }],
            vec!["runA.raw"],
        );
        let mut state = ScientificWorkspaceState {
            branches: vec![AgentBranch {
                id: "hela".into(),
                label: "HeLa".into(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                linked_raw_files: Vec::new(),
                linkage_status: "unresolved".into(),
                notes: "conceptual branch only".into(),
            }],
            next_step: "compile".into(),
            ..Default::default()
        };
        let delta = WorkspaceDelta {
            turn: 2,
            next_step: "finish".into(),
            ..Default::default()
        };
        apply_workspace_delta(&evidence, &mut state, &delta, 2);
        assert_eq!(state.branches.len(), 1);
        assert_eq!(state.branches[0].id, "hela");
        assert!(state.branches[0].linked_raw_files.is_empty());
        assert_eq!(state.branches[0].linkage_status, "unresolved");
    }

    #[test]
    fn unchanged_adjudication_feedback_is_emitted_only_once() {
        let record = ClaimAdjudicationRecord {
            concept_type: "isolation_method".into(),
            scope: "project".into(),
            branch_id: String::new(),
            model_status: "hypothesis".into(),
            proposed_value: "hydrodynamic loading".into(),
            evidence_refs: vec!["E0001".into()],
            adjudication: ClaimAdjudication::Canonical {
                value: "manual picking".into(),
                evidence_refs: vec!["E0001".into()],
            },
        };
        let mut trace_feedback = Vec::new();
        let mut pending = Vec::new();
        let mut signatures = BTreeMap::new();
        append_changed_adjudication_feedback(
            &mut trace_feedback,
            &mut pending,
            &mut signatures,
            &[record.clone()],
            1,
            "post-delta",
        );
        append_changed_adjudication_feedback(
            &mut trace_feedback,
            &mut pending,
            &mut signatures,
            &[record],
            2,
            "post-search",
        );
        assert_eq!(trace_feedback.len(), 1);
        assert_eq!(pending.len(), 1);
    }

    #[test]
    fn task_board_surfaces_direct_hydrodynamic_evidence_ahead_of_noise() {
        let mut items = (1..=30)
            .map(|i| EvidenceItem {
                id: format!("E{i:04}"),
                source_kind: "repository_metadata".into(),
                source_label: format!("noise-{i}"),
                text: "generic project metadata with no method description".into(),
            })
            .collect::<Vec<_>>();
        items.push(EvidenceItem {
            id: "E0031".into(),
            source_kind: "manuscript_semantic_evidence".into(),
            source_label: "stage04_semantic".into(),
            text: "individual single cells were introduced by hydrodynamic injection and processed by on-capillary lysis".into(),
        });
        let evidence = evidence_with(items, vec!["runA.raw"]);
        let issues = vec![ValidationIssue {
            level: "error".into(),
            code: "single_cell_isolation_unresolved".into(),
            row: 1,
            column: SC_ISOLATION_METHOD.into(),
            message: "missing isolation method".into(),
        }];
        let tasks = build_scientific_tasks(&evidence, &issues);
        assert_eq!(tasks.len(), 2);
        assert_eq!(tasks[0].id, "task:study_structure");
        assert_eq!(tasks[1].concept_type, "isolation_method");
        assert_eq!(
            tasks[1].evidence_candidates.first().map(String::as_str),
            Some("E0031")
        );
    }

    #[test]
    fn read_evidence_context_materializes_larger_window_from_registered_source() {
        let dir = std::env::temp_dir().join(format!(
            "pride-scp-workspace-agent-read-context-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let source_path = dir.join("semantic.json");
        let phrase = "individual single cells were introduced by hydrodynamic injection";
        let source = format!(
            "{} {} {}",
            "prefix ".repeat(1200),
            phrase,
            "suffix ".repeat(1200)
        );
        fs::write(&source_path, &source).unwrap();

        let mut evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0021".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "stage04_semantic".into(),
                text: phrase.into(),
            }],
            vec!["runA.raw"],
        );
        evidence.annotation_sources = vec![source_path.display().to_string()];
        let mut attempted = BTreeSet::new();
        let result = read_evidence_context(
            &mut evidence,
            "E0021",
            &["single_cell_isolation_method".into()],
            "inspect direct isolation evidence",
            1,
            &mut attempted,
        );
        assert_eq!(result.outcome, "matched");
        assert_eq!(result.matched_evidence_refs.len(), 1);
        let expanded = evidence
            .evidence
            .iter()
            .find(|item| item.id == result.matched_evidence_refs[0])
            .unwrap();
        assert_eq!(expanded.source_kind, "agent_read_context");
        assert_eq!(expanded.source_label, source_path.display().to_string());
        assert!(expanded.text.contains(phrase));
        assert!(expanded.text.len() > phrase.len());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_evidence_context_does_not_open_unregistered_arbitrary_path() {
        let dir = std::env::temp_dir().join(format!(
            "pride-scp-workspace-agent-unregistered-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let unregistered = dir.join("secret.txt");
        fs::write(&unregistered, "hydrodynamic injection of single cells").unwrap();
        let mut evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: unregistered.display().to_string(),
                text: "hydrodynamic injection".into(),
            }],
            vec!["runA.raw"],
        );
        let mut attempted = BTreeSet::new();
        let result = read_evidence_context(
            &mut evidence,
            "E0001",
            &["single_cell_isolation_method".into()],
            "test trust boundary",
            1,
            &mut attempted,
        );
        assert_eq!(result.outcome, "context_unavailable");
        assert_eq!(evidence.evidence.len(), 1);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn human_review_task_rotation_advances_to_next_open_task() {
        let mut state = ScientificWorkspaceState {
            tasks: vec![
                ScientificTask {
                    id: "task:single_cell_isolation_method".into(),
                    concept_type: "isolation_method".into(),
                    sdrf_field: "single_cell_isolation_method".into(),
                    status: "investigating".into(),
                    error_count: 15,
                    ..Default::default()
                },
                ScientificTask {
                    id: "task:organism".into(),
                    concept_type: "organism".into(),
                    sdrf_field: "organism".into(),
                    status: "open".into(),
                    error_count: 4,
                    ..Default::default()
                },
            ],
            active_task_id: "task:single_cell_isolation_method".into(),
            ..Default::default()
        };
        mark_active_task_human_review(&mut state, "evidence exhausted");
        assert_eq!(state.tasks[0].status, "human_review");
        assert_eq!(state.active_task_id, "task:organism");
        assert_eq!(state.tasks[1].status, "investigating");
    }

    #[test]
    fn study_structure_task_is_first_and_rust_resolves_after_material_edit() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "stage04_semantic".into(),
                text:
                    "HeLa cells and Xenopus oocytes were described as distinct experimental systems"
                        .into(),
            }],
            vec!["runA.raw"],
        );
        let issues = vec![ValidationIssue {
            level: "error".into(),
            code: "single_cell_isolation_unresolved".into(),
            row: 1,
            column: SC_ISOLATION_METHOD.into(),
            message: "missing isolation method".into(),
        }];
        let mut state = ScientificWorkspaceState {
            tasks: build_scientific_tasks(&evidence, &issues),
            ..Default::default()
        };
        ensure_active_task(&mut state);
        assert_eq!(state.active_task_id, "task:study_structure");

        let (_events, publishable) = apply_edit_workspace_command(
            &evidence,
            &mut state,
            "task:study_structure",
            &[AgentBranch {
                id: "hela".into(),
                label: "HeLa experimental system".into(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                linked_raw_files: Vec::new(),
                linkage_status: "unresolved".into(),
                notes: "conceptual branch; no source-grounded RAW mapping".into(),
            }],
            &[],
            &[],
            &[],
            &[],
            "source-grounded structure established",
            1,
        );
        assert!(publishable);
        refresh_scientific_tasks(&evidence, &mut state, &issues);
        assert_eq!(
            state
                .tasks
                .iter()
                .find(|task| task.id == "task:study_structure")
                .map(|task| task.status.as_str()),
            Some("resolved")
        );
        assert_eq!(state.active_task_id, "task:single_cell_isolation_method");
        assert_eq!(state.branches.len(), 1);
        assert!(state.branches[0].linked_raw_files.is_empty());
    }

    #[test]
    fn v13_study_structure_schema_exposes_only_typed_structure_edit() {
        let schema = scientific_agent_schema(true, true, true);
        let variants = schema["oneOf"].as_array().unwrap();
        let commands = variants
            .iter()
            .filter_map(|variant| variant["properties"]["command"]["enum"][0].as_str())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            commands,
            [
                "read_evidence",
                "search_evidence",
                "edit_study_structure",
                "escalate",
            ]
            .into_iter()
            .collect::<BTreeSet<_>>()
        );
        assert!(!commands.contains("edit_workspace"));
        assert!(!commands.contains("edit_scientific_observation"));
        let edit = variants
            .iter()
            .find(|variant| {
                variant["properties"]["command"]["enum"][0].as_str() == Some("edit_study_structure")
            })
            .unwrap();
        let properties = edit["properties"].as_object().unwrap();
        assert!(properties.contains_key("branch_upserts"));
        assert!(!properties.contains_key("observation_upserts"));
        assert_eq!(
            edit["properties"]["branch_upserts"]["minItems"].as_u64(),
            Some(1)
        );
        assert_eq!(
            edit["properties"]["task_id"]["enum"][0],
            "task:study_structure"
        );
    }

    #[test]
    fn v13_field_schema_exposes_only_typed_observation_edit() {
        let schema = scientific_agent_schema(true, true, false);
        let variants = schema["oneOf"].as_array().unwrap();
        let commands = variants
            .iter()
            .filter_map(|variant| variant["properties"]["command"]["enum"][0].as_str())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            commands,
            [
                "read_evidence",
                "search_evidence",
                "edit_scientific_observation",
                "escalate",
            ]
            .into_iter()
            .collect::<BTreeSet<_>>()
        );
        assert!(!commands.contains("edit_workspace"));
        assert!(!commands.contains("edit_study_structure"));
        let edit = variants
            .iter()
            .find(|variant| {
                variant["properties"]["command"]["enum"][0].as_str()
                    == Some("edit_scientific_observation")
            })
            .unwrap();
        let properties = edit["properties"].as_object().unwrap();
        assert!(properties.contains_key("observation_upserts"));
        assert!(properties.contains_key("observation_retractions"));
        assert!(!properties.contains_key("branch_upserts"));
        assert_eq!(
            edit["properties"]["observation_upserts"]["minItems"].as_u64(),
            Some(1)
        );
    }

    #[test]
    fn v13_decision_step_schema_disables_repeated_reads_but_keeps_typed_edit() {
        let study_schema = scientific_agent_schema(false, true, true);
        let study_commands = study_schema["oneOf"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|variant| variant["properties"]["command"]["enum"][0].as_str())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            study_commands,
            ["search_evidence", "edit_study_structure", "escalate"]
                .into_iter()
                .collect::<BTreeSet<_>>()
        );
        assert!(!study_commands.contains("read_evidence"));

        let field_schema = scientific_agent_schema(false, true, false);
        let field_commands = field_schema["oneOf"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|variant| variant["properties"]["command"]["enum"][0].as_str())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            field_commands,
            ["search_evidence", "edit_scientific_observation", "escalate"]
                .into_iter()
                .collect::<BTreeSet<_>>()
        );
    }

    #[test]
    fn v13_stalled_search_schema_forces_task_specific_edit_or_escalate() {
        let study_schema = scientific_agent_schema(false, false, true);
        let study_commands = study_schema["oneOf"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|variant| variant["properties"]["command"]["enum"][0].as_str())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            study_commands,
            ["edit_study_structure", "escalate"]
                .into_iter()
                .collect::<BTreeSet<_>>()
        );

        let field_schema = scientific_agent_schema(false, false, false);
        let field_commands = field_schema["oneOf"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|variant| variant["properties"]["command"]["enum"][0].as_str())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            field_commands,
            ["edit_scientific_observation", "escalate"]
                .into_iter()
                .collect::<BTreeSet<_>>()
        );
    }

    #[test]
    fn v13_runtime_rejects_edit_type_for_wrong_task() {
        let study_state = ScientificWorkspaceState {
            active_task_id: "task:study_structure".into(),
            ..Default::default()
        };
        let wrong_study_edit = AgentCommand::EditScientificObservation {
            task_id: "task:study_structure".into(),
            observation_upserts: vec![ScientificObservationUpsert::default()],
            observation_retractions: Vec::new(),
            open_question_additions: Vec::new(),
            open_question_resolutions: Vec::new(),
            notes: String::new(),
        };
        assert!(!command_matches_active_task(
            &study_state,
            &wrong_study_edit
        ));

        let field_state = ScientificWorkspaceState {
            active_task_id: "task:single_cell_isolation_method".into(),
            ..Default::default()
        };
        let wrong_field_edit = AgentCommand::EditStudyStructure {
            task_id: "task:single_cell_isolation_method".into(),
            branch_upserts: vec![AgentBranch::default()],
            open_question_additions: Vec::new(),
            open_question_resolutions: Vec::new(),
            notes: String::new(),
        };
        assert!(!command_matches_active_task(
            &field_state,
            &wrong_field_edit
        ));
    }

    #[test]
    fn v13_read_receipt_records_requested_ref_and_enters_decision_step() {
        let mut state = ScientificWorkspaceState {
            tasks: vec![ScientificTask {
                id: "task:study_structure".into(),
                status: "investigating".into(),
                error_count: 1,
                ..Default::default()
            }],
            active_task_id: "task:study_structure".into(),
            ..Default::default()
        };
        let requested = vec!["E0021".to_string()];
        let result = EvidenceActionResult {
            action: "READ_EVIDENCE_CONTEXT".into(),
            outcome: "matched".into(),
            query: EvidenceQuery {
                match_kind: "identifier".into(),
                value: "E0021".into(),
                ..Default::default()
            },
            matched_evidence_refs: vec!["E0040".into()],
            ..Default::default()
        };
        record_task_reads(&mut state, "task:study_structure", &requested, &[result]);
        let task = &state.tasks[0];
        assert_eq!(task.evidence_reads, vec!["E0021", "E0040"]);
        assert!(task.decision_required);
    }

    #[test]
    fn v13_unread_filter_blocks_already_consumed_context_refs() {
        let evidence = evidence_with(
            vec![
                EvidenceItem {
                    id: "E0001".into(),
                    source_kind: "pride_project".into(),
                    source_label: "project:projectDescription".into(),
                    text: "first".into(),
                },
                EvidenceItem {
                    id: "E0002".into(),
                    source_kind: "pride_project".into(),
                    source_label: "project:sampleProcessingProtocol".into(),
                    text: "second".into(),
                },
            ],
            Vec::new(),
        );
        let state = ScientificWorkspaceState {
            tasks: vec![ScientificTask {
                id: "task:study_structure".into(),
                evidence_reads: vec!["E0001".into()],
                ..Default::default()
            }],
            active_task_id: "task:study_structure".into(),
            ..Default::default()
        };
        assert_eq!(
            unread_evidence_refs_for_task(
                &evidence,
                &state,
                "task:study_structure",
                &["E0001".into(), "E0002".into()],
            ),
            vec!["E0002"]
        );
    }

    #[test]
    fn v13_duplicate_skipped_results_do_not_consume_tool_budget() {
        let results = vec![
            EvidenceActionResult {
                outcome: "matched".into(),
                ..Default::default()
            },
            EvidenceActionResult {
                outcome: "duplicate_skipped".into(),
                ..Default::default()
            },
        ];
        assert_eq!(evidence_action_count(&results), 1);
    }

    #[test]
    fn agent_command_parser_rejects_mixed_intents_and_full_workspace_fields() {
        let mixed = json!({
            "command": "read_evidence",
            "task_id": "task:single_cell_isolation_method",
            "evidence_refs": ["E0001"],
            "reason": "need context",
            "observation_upserts": []
        });
        assert!(serde_json::from_value::<AgentCommand>(mixed).is_err());

        let valid = json!({
            "command": "read_evidence",
            "task_id": "task:single_cell_isolation_method",
            "evidence_refs": ["E0001"],
            "reason": "need context"
        });
        assert!(matches!(
            serde_json::from_value::<AgentCommand>(valid).unwrap(),
            AgentCommand::ReadEvidence { .. }
        ));

        let legacy_edit = json!({
            "command": "edit_workspace",
            "task_id": "task:study_structure",
            "branch_upserts": [],
            "observation_upserts": [],
            "observation_retractions": [],
            "open_question_additions": [],
            "open_question_resolutions": [],
            "notes": "legacy generic edit"
        });
        assert!(serde_json::from_value::<AgentCommand>(legacy_edit).is_err());

        let typed_study_edit = json!({
            "command": "edit_study_structure",
            "task_id": "task:study_structure",
            "branch_upserts": [{
                "id": "branch:source_supported",
                "label": "source-supported study branch",
                "status": "supported",
                "evidence_refs": ["E0001"],
                "linked_raw_files": [],
                "linkage_status": "unresolved",
                "notes": "conceptual branch; exact RAW mapping remains unresolved"
            }],
            "open_question_additions": [],
            "open_question_resolutions": [],
            "notes": "record source-supported structure"
        });
        assert!(matches!(
            serde_json::from_value::<AgentCommand>(typed_study_edit).unwrap(),
            AgentCommand::EditStudyStructure { .. }
        ));

        let typed_observation_edit = json!({
            "command": "edit_scientific_observation",
            "task_id": "task:single_cell_isolation_method",
            "observation_upserts": [{
                "concept_type": "isolation_method",
                "observed_value": "source-defined microfluidic isolation platform",
                "scope": "unresolved",
                "branch_id": "",
                "status": "supported",
                "evidence_refs": ["E0001"],
                "confidence": "high",
                "reason": "the trusted source explicitly defines the platform as a single-cell isolation method",
                "supersedes_observed_value": ""
            }],
            "observation_retractions": [],
            "open_question_additions": [],
            "open_question_resolutions": [],
            "notes": "record source-faithful observation; Rust owns canonicalization"
        });
        assert!(matches!(
            serde_json::from_value::<AgentCommand>(typed_observation_edit).unwrap(),
            AgentCommand::EditScientificObservation { .. }
        ));
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
            evidence_refs: Vec::new(),
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
            evidence_refs: Vec::new(),
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
            false,
        );
        assert_eq!(rows[0][1], "single cell");
        assert_eq!(rows[0][2], "cellA");
        assert_eq!(rows[0][3], "1");
        assert_eq!(rows[0][4], "1");
        assert_eq!(rows[0][5], "1");
    }

    #[test]
    fn trusted_partial_sdrf_fusion_expands_mapped_raw_and_retains_unmapped_raw() {
        let root = std::env::temp_dir().join(format!(
            "pride-scp-trusted-partial-sdrf-fusion-expand-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let partial = root.join("partial.sdrf.tsv");
        fs::write(
            &partial,
            concat!(
                "source name\tassay name\ttechnology type\tcomment[data file]\tcomment[label]\tcomment[modification parameters]\tcomment[modification parameters]\tcomment[fraction identifier]\tcomment[technical replicate]\n",
                "cell_A\tassay_A\tproteomic profiling by mass spectrometry\tplex_1.raw\tTMT127N\tNT=Oxidation\tNT=Carbamidomethyl\t1\t1\n",
                "cell_B\tassay_B\tproteomic profiling by mass spectrometry\tplex_1.raw\tTMT128N\tNT=Oxidation\tNT=Carbamidomethyl\t1\t1\n"
            ),
        )
        .unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![
                RawFile {
                    file_name: "plex_1.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "qc_1.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
            ],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let current_headers = vec![
            "source name".into(),
            "assay name".into(),
            "technology type".into(),
            "comment[data file]".into(),
            "comment[label]".into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
        ];
        let current_rows = vec![
            vec![
                "run_1".into(),
                "plex_1".into(),
                "proteomic profiling by mass spectrometry".into(),
                "plex_1.raw".into(),
                "not available".into(),
                "not available".into(),
                "not available".into(),
            ],
            vec![
                "qc_1".into(),
                "qc_1".into(),
                "proteomic profiling by mass spectrometry".into(),
                "qc_1.raw".into(),
                "not available".into(),
                "1".into(),
                "1".into(),
            ],
        ];
        let (headers, rows, issue) =
            fuse_trusted_partial_sdrf_rows(&current_headers, &current_rows, &evidence, &partial)
                .unwrap();
        assert_eq!(rows.len(), 3);
        let at = |name: &str| headers.iter().position(|h| h == name).unwrap();
        let plex_rows = rows
            .iter()
            .filter(|row| row[at("comment[data file]")] == "plex_1.raw")
            .collect::<Vec<_>>();
        assert_eq!(plex_rows.len(), 2);
        assert_eq!(plex_rows[0][at("comment[label]")], "TMT127N");
        assert_eq!(plex_rows[1][at("comment[label]")], "TMT128N");
        let modification_columns = headers
            .iter()
            .enumerate()
            .filter_map(|(index, header)| {
                (header == "comment[modification parameters]").then_some(index)
            })
            .collect::<Vec<_>>();
        assert_eq!(modification_columns.len(), 2);
        assert_eq!(plex_rows[0][modification_columns[0]], "NT=Oxidation");
        assert_eq!(plex_rows[0][modification_columns[1]], "NT=Carbamidomethyl");
        assert!(rows.iter().any(|row| {
            row[at("comment[data file]")] == "qc_1.raw"
                && row[at("comment[technical replicate]")] == "1"
        }));
        assert_eq!(
            issue.code,
            "scientific_agent_trusted_partial_sdrf_mapping_applied"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn trusted_partial_sdrf_fusion_rejects_deposited_only_raw() {
        let root = std::env::temp_dir().join(format!(
            "pride-scp-trusted-partial-sdrf-fusion-reject-extra-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let partial = root.join("partial.sdrf.tsv");
        fs::write(
            &partial,
            "source name\tcomment[data file]\ncell_X\textra.raw\n",
        )
        .unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "plex_1.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let err = fuse_trusted_partial_sdrf_rows(
            &["source name".into(), "comment[data file]".into()],
            &[vec!["run_1".into(), "plex_1.raw".into()]],
            &evidence,
            &partial,
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("not a strict repository subset"), "{err}");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn v2_row_role_hardening_clears_prepopulated_single_cell_for_unknown_archive() {
        let evidence = evidence_with(Vec::new(), vec!["xenopus.zip"]);
        let headers = vec![
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
            SC_CELLS_PER_WELL.into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
        ];
        // Mirror the real compiler state: an earlier project-level scaffold has
        // already populated single-cell role fields before row-role hardening.
        let mut rows = vec![vec![
            "xenopus.zip".into(),
            "single cell".into(),
            "manual picking".into(),
            "xenopus".into(),
            "1".into(),
            "not available".into(),
            "not available".into(),
        ]];
        let issues = enforce_deterministic_row_scaffold(
            &headers,
            &mut rows,
            &evidence,
            "one_cell_per_data_file",
            true,
        );
        assert_eq!(rows[0][1], "not available");
        assert_eq!(rows[0][2], "not available");
        assert_eq!(rows[0][3], "not available");
        assert_eq!(rows[0][4], "not available");
        assert_eq!(rows[0][5], "1");
        assert_eq!(rows[0][6], "1");
        assert!(issues.iter().any(|issue| {
            issue.code == "scientific_agent_raw_file_role_unresolved" && issue.row == 1
        }));
    }

    #[test]
    fn v2_row_role_hardening_overrides_prepopulated_single_cell_for_compact_mass_bulk() {
        let evidence = evidence_with(Vec::new(), vec!["200pgHeLa_raw.zip"]);
        let headers = vec![
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
            SC_CELLS_PER_WELL.into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
        ];
        let mut rows = vec![vec![
            "200pgHeLa_raw.zip".into(),
            "single cell".into(),
            "manual picking".into(),
            "200pgHeLa_raw".into(),
            "1".into(),
            "not available".into(),
            "not available".into(),
        ]];
        let issues = enforce_deterministic_row_scaffold(
            &headers,
            &mut rows,
            &evidence,
            "one_cell_per_data_file",
            true,
        );
        assert_eq!(rows[0][1], "bulk control");
        assert_eq!(rows[0][2], "not applicable");
        assert_eq!(rows[0][3], "not applicable");
        assert_eq!(rows[0][4], "not applicable");
        assert_eq!(rows[0][5], "1");
        assert_eq!(rows[0][6], "1");
        assert!(!issues
            .iter()
            .any(|issue| { issue.code == "scientific_agent_raw_file_role_unresolved" }));
    }

    #[test]
    fn v2_row_role_hardening_recognizes_compact_mass_amount_archive_as_bulk() {
        assert_eq!(
            scientific_agent_raw_file_role("200pgHeLa_raw.zip", true),
            RawFileRole::Bulk
        );
        assert_eq!(
            scientific_agent_raw_file_role("500pgHeLa_raw.zip", true),
            RawFileRole::Bulk
        );
        assert_eq!(
            scientific_agent_raw_file_role("xenopus.zip", true),
            RawFileRole::Unknown
        );
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

    fn consensus_projection_evidence() -> DatasetEvidence {
        evidence_with(
            vec![
                EvidenceItem {
                    id: "E0001".into(),
                    source_kind: "manuscript_semantic_evidence".into(),
                    source_label: "paper.txt".into(),
                    text: "Single cells were isolated by FACS flow cytometry sorting.".into(),
                },
                EvidenceItem {
                    id: "E0002".into(),
                    source_kind: "manuscript_semantic_evidence".into(),
                    source_label: "paper.txt".into(),
                    text: "Individual single cells were isolated by manual picking.".into(),
                },
                EvidenceItem {
                    id: "E0003".into(),
                    source_kind: "manuscript_semantic_evidence".into(),
                    source_label: "paper.txt".into(),
                    text: "Single-cell samples were processed for proteomics.".into(),
                },
            ],
            vec!["singlecell_A.raw", "blank_A.raw"],
        )
    }

    fn row_role_state_with_claims(claims: Vec<ScientificClaim>) -> ScientificWorkspaceState {
        ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_ROW_ROLE_HARDENED_VERSION.into(),
            claims,
            ..Default::default()
        }
    }

    fn branch_claim_with_ref(
        concept_type: &str,
        value: &str,
        branch_id: &str,
        evidence_ref: &str,
    ) -> ScientificClaim {
        let mut result = claim(concept_type, value, "branch", branch_id);
        result.evidence_refs = vec![evidence_ref.into()];
        result
    }

    #[test]
    fn trusted_partial_mapping_disables_global_multiplex_mapping_error() {
        let mut evidence = evidence_with(Vec::new(), vec!["plex.raw"]);
        evidence.study_design.relation_mode_hint = "multiplexed_cells_per_data_file".into();
        evidence.study_design.relation_confidence = "high".into();
        evidence.study_design.multiplex_chemistry_hint = "TMTpro".into();
        evidence.study_design.multiplex_mapping_status =
            "chemistry_detected_channel_mapping_unresolved".into();
        evidence.study_design.repository_file_mode = "direct_acquisitions".into();
        let headers = vec![
            "source name".into(),
            "assay name".into(),
            "technology type".into(),
            "comment[data file]".into(),
            "comment[label]".into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
        ];
        let rows = vec![vec![
            "cell_A".into(),
            "assay_A".into(),
            "proteomic profiling by mass spectrometry".into(),
            "plex.raw".into(),
            "TMT127N".into(),
            "1".into(),
            "1".into(),
        ]];
        let issues = validate_scientific_workspace_rows(
            &headers,
            &rows,
            &evidence,
            "multiplexed_cells_per_data_file",
            &[],
            true,
        );
        assert!(!issues
            .iter()
            .any(|issue| issue.code == "sample_to_channel_mapping_unresolved"));
    }

    #[test]
    fn v2_consensus_isolation_projects_one_canonical_single_cell_regime() {
        let evidence = consensus_projection_evidence();
        let state = row_role_state_with_claims(vec![
            branch_claim_with_ref("sample_type", "single cell", "R001", "E0001"),
            branch_claim_with_ref(
                "isolation_method",
                "FACS flow cytometry sorting",
                "R001",
                "E0001",
            ),
        ]);
        let headers = vec![
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
        ];
        let mut rows = vec![
            vec![
                "singlecell_A.raw".into(),
                "single cell".into(),
                "not available".into(),
            ],
            vec![
                "blank_A.raw".into(),
                "empty".into(),
                "not applicable".into(),
            ],
        ];

        let issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &[],
            false,
            false,
        );

        assert_eq!(rows[0][2], "FACS");
        assert_eq!(rows[1][2], "not applicable");
        assert!(issues.iter().any(|issue| {
            issue.code == "scientific_agent_consensus_single_cell_isolation_projected"
        }));
    }

    #[test]
    fn v2_consensus_isolation_projects_when_multiple_single_cell_regimes_agree() {
        let evidence = consensus_projection_evidence();
        let state = row_role_state_with_claims(vec![
            branch_claim_with_ref("sample_type", "single cell", "R001", "E0001"),
            branch_claim_with_ref("sample_type", "single cell", "R002", "E0001"),
            branch_claim_with_ref("isolation_method", "FACS sorting", "R001", "E0001"),
            branch_claim_with_ref("isolation_method", "FACS sorting", "R002", "E0001"),
        ]);
        let headers = vec![SC_SAMPLE_TYPE.into(), SC_ISOLATION_METHOD.into()];
        let mut rows = vec![vec!["single cell".into(), "not available".into()]];

        let issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &[],
            false,
            false,
        );

        assert_eq!(rows[0][1], "FACS");
        assert_eq!(issues.len(), 1);
    }

    #[test]
    fn v2_consensus_isolation_fails_closed_when_single_cell_regimes_conflict() {
        let evidence = consensus_projection_evidence();
        let state = row_role_state_with_claims(vec![
            branch_claim_with_ref("sample_type", "single cell", "R001", "E0001"),
            branch_claim_with_ref("sample_type", "single cell", "R002", "E0002"),
            branch_claim_with_ref("isolation_method", "FACS sorting", "R001", "E0001"),
            branch_claim_with_ref("isolation_method", "manual picking", "R002", "E0002"),
        ]);
        let headers = vec![SC_SAMPLE_TYPE.into(), SC_ISOLATION_METHOD.into()];
        let mut rows = vec![vec!["single cell".into(), "not available".into()]];

        let issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &[],
            false,
            false,
        );

        assert_eq!(rows[0][1], "not available");
        assert!(issues.is_empty());
    }

    #[test]
    fn v2_consensus_isolation_fails_closed_when_any_single_cell_regime_is_unresolved() {
        let evidence = consensus_projection_evidence();
        let state = row_role_state_with_claims(vec![
            branch_claim_with_ref("sample_type", "single cell", "R001", "E0001"),
            branch_claim_with_ref("sample_type", "single cell", "R002", "E0003"),
            branch_claim_with_ref("isolation_method", "FACS sorting", "R001", "E0001"),
            branch_claim_with_ref("isolation_method", "unknown isolation", "R002", "E0003"),
        ]);
        let headers = vec![SC_SAMPLE_TYPE.into(), SC_ISOLATION_METHOD.into()];
        let mut rows = vec![vec!["single cell".into(), "not available".into()]];

        let issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &[],
            false,
            false,
        );

        assert_eq!(rows[0][1], "not available");
        assert!(issues.is_empty());
    }

    #[test]
    fn v2_consensus_isolation_does_not_touch_non_single_cell_rows() {
        let evidence = consensus_projection_evidence();
        let state = row_role_state_with_claims(vec![
            branch_claim_with_ref("sample_type", "single cell", "R001", "E0001"),
            branch_claim_with_ref("isolation_method", "FACS sorting", "R001", "E0001"),
        ]);
        let headers = vec![SC_SAMPLE_TYPE.into(), SC_ISOLATION_METHOD.into()];
        let mut rows = vec![
            vec!["empty".into(), "not applicable".into()],
            vec!["bulk control".into(), "not applicable".into()],
            vec!["quality control sample".into(), "not applicable".into()],
        ];
        let before = rows.clone();

        let issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &[],
            false,
            false,
        );

        assert_eq!(rows, before);
        assert!(issues.is_empty());
    }

    #[test]
    fn v2_consensus_isolation_excludes_unresolved_multiplex_and_raw_role_states() {
        let mut evidence = consensus_projection_evidence();
        evidence.study_design.relation_mode_hint = "multiplexed_cells_per_data_file".into();
        evidence.study_design.multiplex_mapping_status =
            "chemistry_detected_channel_mapping_unresolved".into();
        let state = row_role_state_with_claims(vec![
            branch_claim_with_ref("sample_type", "single cell", "R001", "E0001"),
            branch_claim_with_ref("isolation_method", "FACS sorting", "R001", "E0001"),
        ]);
        let headers = vec![SC_SAMPLE_TYPE.into(), SC_ISOLATION_METHOD.into()];
        let mut multiplex_rows = vec![vec!["single cell".into(), "not available".into()]];

        let multiplex_issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut multiplex_rows,
            &evidence,
            &state,
            "multiplexed_cells_per_data_file",
            &[],
            false,
            false,
        );
        assert_eq!(multiplex_rows[0][1], "not available");
        assert!(multiplex_issues.is_empty());

        evidence.study_design.relation_mode_hint = "one_cell_per_data_file".into();
        evidence.study_design.multiplex_mapping_status.clear();
        let mut unresolved_raw_rows = vec![vec!["single cell".into(), "not available".into()]];
        let unresolved_raw_issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut unresolved_raw_rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &[],
            true,
            false,
        );
        assert_eq!(unresolved_raw_rows[0][1], "not available");
        assert!(unresolved_raw_issues.is_empty());
    }

    #[test]
    fn v2_consensus_isolation_excludes_existing_sdrf_and_explicit_mapping() {
        let mut evidence = consensus_projection_evidence();
        let state = row_role_state_with_claims(vec![
            branch_claim_with_ref("sample_type", "single cell", "R001", "E0001"),
            branch_claim_with_ref("isolation_method", "FACS sorting", "R001", "E0001"),
        ]);
        let headers = vec![SC_SAMPLE_TYPE.into(), SC_ISOLATION_METHOD.into()];

        evidence.existing_sdrf_path = "/tmp/existing.sdrf.tsv".into();
        let mut existing_rows = vec![vec!["single cell".into(), "not available".into()]];
        let existing_issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut existing_rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &[],
            false,
            false,
        );
        assert_eq!(existing_rows[0][1], "not available");
        assert!(existing_issues.is_empty());

        evidence.existing_sdrf_path.clear();
        let explicit_mappings = vec![ExplicitRowMapping {
            accession: "PXDTEST".into(),
            raw_file: "singlecell_A.raw".into(),
            source_name: "source_A".into(),
            cell_identifier: "cell_A".into(),
            biological_replicate: "1".into(),
            technical_replicate: "1".into(),
            sample_type: "single cell".into(),
            cells_per_well: "1".into(),
            label: "not applicable".into(),
            carrier_channel: "not applicable".into(),
            reference_channel: "not applicable".into(),
            design_source: "explicit test mapping".into(),
            design_ref: "E0001".into(),
            mapping_key: "singlecell_A.raw".into(),
            mapping_confidence: "high".into(),
        }];
        let mut mapped_rows = vec![vec!["single cell".into(), "not available".into()]];
        let mapped_issues = apply_consensus_single_cell_isolation_projection(
            &headers,
            &mut mapped_rows,
            &evidence,
            &state,
            "one_cell_per_data_file",
            &explicit_mappings,
            false,
            false,
        );
        assert_eq!(mapped_rows[0][1], "not available");
        assert!(mapped_issues.is_empty());
    }

    #[test]
    fn v2_unresolved_multiplex_uses_mapping_level_validation() {
        let mut evidence = evidence_with(Vec::new(), vec!["plex.raw"]);
        evidence.study_design.relation_mode_hint = "multiplexed_cells_per_data_file".into();
        evidence.study_design.relation_confidence = "high".into();
        evidence.study_design.multiplex_chemistry_hint = "TMTpro".into();
        evidence.study_design.multiplex_mapping_status =
            "chemistry_detected_channel_mapping_unresolved".into();
        evidence.study_design.repository_file_mode = "direct_acquisitions".into();

        let headers = vec![
            "source name".into(),
            "assay name".into(),
            "technology type".into(),
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
            SC_CELLS_PER_WELL.into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
        ];
        let rows = vec![vec![
            "run_0001".into(),
            "run_0001".into(),
            "proteomic profiling by mass spectrometry".into(),
            "plex.raw".into(),
            "single cell".into(),
            "not available".into(),
            "not available".into(),
            "not available".into(),
            "not available".into(),
            "not available".into(),
        ]];

        let issues = validate_scientific_workspace_rows(
            &headers,
            &rows,
            &evidence,
            "multiplexed_cells_per_data_file",
            &[],
            false,
        );

        let errors = issues
            .iter()
            .filter(|issue| issue.level == "error")
            .collect::<Vec<_>>();

        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].code, "sample_to_channel_mapping_unresolved");
        assert!(!issues.iter().any(|issue| {
            matches!(
                issue.code.as_str(),
                "single_cell_isolation_unresolved"
                    | "cell_identifier_invalid_or_unresolved"
                    | "required_integer_invalid"
            )
        }));
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
            evidence_refs: Vec::new(),
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

    #[test]
    fn v2_stage1_schema_allows_supported_branch_with_unresolved_raw_linkage() {
        let schema = study_graph_stage1_schema();
        let text = serde_json::to_string(&schema).unwrap();
        assert!(text.contains("propose_graph"));
        assert!(text.contains("biological_material"));
        assert!(text.contains("linked_raw_files"));
        assert!(text.contains("unresolved"));
        assert!(!text.contains("edit_scientific_observation"));
    }

    #[test]
    fn v2_stage1_acceptance_assigns_rust_branch_ids_and_preserves_unresolved_linkage() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "Single HeLa cells were manually loaded by hydrodynamic pressure while a separate low-input material was introduced by spray voltage.".into(),
            }],
            vec!["one.raw", "two.raw"],
        );
        let proposal = StudyGraphProposal {
            decision: "propose_graph".into(),
            branches: vec![
                StudyGraphBranchProposal {
                    label: "single-cell HeLa".into(),
                    biological_material: "single HeLa cells".into(),
                    experimental_role: "single-cell proteomics".into(),
                    isolation_context: "manual loading by hydrodynamic pressure".into(),
                    acquisition_context: String::new(),
                    evidence_refs: vec!["E0001".into()],
                    linked_raw_files: Vec::new(),
                    linkage_status: "unresolved".into(),
                    notes: String::new(),
                },
                StudyGraphBranchProposal {
                    label: "spray-voltage low-input".into(),
                    biological_material: "low-input material".into(),
                    experimental_role: "method comparison".into(),
                    isolation_context: "spray voltage introduction".into(),
                    acquisition_context: String::new(),
                    evidence_refs: vec!["E0001".into()],
                    linked_raw_files: Vec::new(),
                    linkage_status: "unresolved".into(),
                    notes: String::new(),
                },
            ],
            open_questions: vec!["exact RAW linkage is unresolved".into()],
            reason: "two source-supported experimental regimes".into(),
        };
        let accepted = accept_study_graph_stage1(&evidence, &proposal);
        assert_eq!(accepted.status, "accepted");
        assert_eq!(accepted.branches.len(), 2);
        assert_eq!(accepted.branches[0].id, "B001");
        assert_eq!(accepted.branches[1].id, "B002");
        assert!(accepted
            .branches
            .iter()
            .all(|branch| branch.linked_raw_files.is_empty()
                && branch.linkage_status == "unresolved"));
    }

    #[test]
    fn v2_stage1_strips_unsupported_filename_linkage_instead_of_inferencing_identity() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "The study contains a Xenopus embryo branch.".into(),
            }],
            vec!["xenopus_D11.raw"],
        );
        let proposal = StudyGraphProposal {
            decision: "propose_graph".into(),
            branches: vec![StudyGraphBranchProposal {
                label: "Xenopus embryo".into(),
                biological_material: "Xenopus embryo material".into(),
                experimental_role: "embryo microsampling".into(),
                isolation_context: String::new(),
                acquisition_context: String::new(),
                evidence_refs: vec!["E0001".into()],
                linked_raw_files: vec!["xenopus_D11.raw".into()],
                linkage_status: "supported".into(),
                notes: String::new(),
            }],
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_study_graph_stage1(&evidence, &proposal);
        assert_eq!(accepted.status, "accepted");
        assert!(accepted.branches[0].linked_raw_files.is_empty());
        assert_eq!(accepted.branches[0].linkage_status, "unresolved");
        assert_eq!(accepted.removed_raw_links.len(), 1);
    }

    #[test]
    fn v2_stage1_human_review_stops_without_downstream_structure() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "ambiguous study".into(),
            }],
            vec!["run.raw"],
        );
        let proposal = StudyGraphProposal {
            decision: "human_review".into(),
            branches: Vec::new(),
            open_questions: vec![
                "study branches cannot be separated from available evidence".into()
            ],
            reason: "insufficient structural evidence".into(),
        };
        let accepted = accept_study_graph_stage1(&evidence, &proposal);
        assert_eq!(accepted.status, "human_review");
        assert!(accepted.branches.is_empty());
        assert_eq!(accepted.model_calls, 1);
    }

    #[test]
    fn v2_factor_stage1_decision_contract_encodes_decision4_safety_rules() {
        let text = FACTOR_GRAPH_STAGE1_DECISION_CONTRACT;
        assert!(text.contains("open_questions may coexist with propose_graph"));
        assert!(text.contains("Missing exact RAW-to-factor linkage is never"));
        assert!(text.contains("Distinct Material nodes MAY share"));
        assert!(text.contains("GV, IVM, and IVO"));
        assert!(text.contains("NODE-TEXT SOURCE FIDELITY IS STRICT"));
        assert!(text.contains("omit the Acquisition node rather than inventing"));
        assert!(text.contains("reason field MUST agree with decision"));
    }

    #[test]
    fn v2_factor_stage1_decision_contract_keeps_minimum_safe_graph_at_material_plus_regime() {
        let text = FACTOR_GRAPH_STAGE1_DECISION_CONTRACT;
        assert!(text.contains("at least one Material node and one Experimental Regime node"));
        assert!(text.contains("Acquisition nodes, exact RAW links, and complete answers"));
        assert!(!text.contains("automatically accept"));
    }

    #[test]
    fn v2_factor_stage1_schema_separates_material_regime_and_acquisition_axes() {
        let schema = factor_graph_stage1_schema();
        let text = serde_json::to_string(&schema).unwrap();
        assert!(text.contains("materials"));
        assert!(text.contains("regimes"));
        assert!(text.contains("acquisitions"));
        assert!(text.contains("material_to_regime"));
        assert!(!text.contains("linked_raw_files"));
        assert!(!text.contains("edit_scientific_observation"));
    }

    #[test]
    fn v2_factor_stage1_preserves_two_regimes_for_one_material() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "HeLa single cells were manually loaded by hydrodynamic pressure, while low-number cells were introduced by spray voltage; both used CE-MS/MS.".into(),
            }],
            vec!["single.raw", "spray.raw"],
        );
        let proposal = StudyFactorGraphProposal {
            decision: "propose_graph".into(),
            materials: vec![FactorMaterialProposal {
                local_id: "M1".into(),
                label: "HeLa".into(),
                organism: "Homo sapiens".into(),
                biological_material: "HeLa cells".into(),
                experimental_role: "single-cell and low-input material".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            regimes: vec![
                FactorRegimeProposal {
                    local_id: "R1".into(),
                    label: "single-cell hydrodynamic loading".into(),
                    experimental_role: "single-cell proteomics".into(),
                    isolation_or_loading_method: "manual loading by hydrodynamic pressure".into(),
                    input_or_cell_count_regime: "single cell".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
                FactorRegimeProposal {
                    local_id: "R2".into(),
                    label: "spray-voltage low-input".into(),
                    experimental_role: "low-number cell method comparison".into(),
                    isolation_or_loading_method: "spray voltage injection".into(),
                    input_or_cell_count_regime: "low-number cells".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
            ],
            acquisitions: vec![FactorAcquisitionProposal {
                local_id: "A1".into(),
                label: "CE-MS/MS".into(),
                acquisition_method_or_platform: "CE-MS/MS top-down proteomics".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            relations: vec![
                FactorRelationProposal {
                    source_id: "M1".into(),
                    relation_type: "material_to_regime".into(),
                    target_id: "R1".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
                FactorRelationProposal {
                    source_id: "M1".into(),
                    relation_type: "material_to_regime".into(),
                    target_id: "R2".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
                FactorRelationProposal {
                    source_id: "R1".into(),
                    relation_type: "regime_to_acquisition".into(),
                    target_id: "A1".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
                FactorRelationProposal {
                    source_id: "R2".into(),
                    relation_type: "regime_to_acquisition".into(),
                    target_id: "A1".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
            ],
            raw_links: Vec::new(),
            open_questions: Vec::new(),
            reason: "separate source-supported regimes".into(),
        };
        let accepted = accept_factor_graph_stage1(&evidence, &proposal);
        assert_eq!(accepted.status, "accepted");
        assert_eq!(accepted.materials.len(), 1);
        assert_eq!(accepted.regimes.len(), 2);
        assert_eq!(accepted.acquisitions.len(), 1);
        assert_eq!(accepted.relations.len(), 4);
        assert_eq!(accepted.materials[0].id, "M001");
        assert!(accepted
            .regimes
            .iter()
            .any(|node| node.isolation_or_loading_method.contains("hydrodynamic")));
        assert!(accepted
            .regimes
            .iter()
            .any(|node| node.isolation_or_loading_method.contains("spray voltage")));
    }

    #[test]
    fn v2_factor_stage1_preserves_distinct_explicit_organisms_as_material_nodes() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "Human HeLa and mouse HT22 cells were studied with a microwell single-cell workflow.".into(),
            }],
            vec!["run.raw"],
        );
        let proposal = StudyFactorGraphProposal {
            decision: "propose_graph".into(),
            materials: vec![
                FactorMaterialProposal {
                    local_id: "M1".into(),
                    label: "HeLa".into(),
                    organism: "Homo sapiens".into(),
                    biological_material: "HeLa cells".into(),
                    experimental_role: "single-cell material".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
                FactorMaterialProposal {
                    local_id: "M2".into(),
                    label: "HT22".into(),
                    organism: "Mus musculus".into(),
                    biological_material: "HT22 cells".into(),
                    experimental_role: "single-cell material".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
            ],
            regimes: vec![FactorRegimeProposal {
                local_id: "R1".into(),
                label: "microwell single-cell processing".into(),
                experimental_role: "single-cell multi-omics".into(),
                isolation_or_loading_method: "picked single-cell transferred into microwell".into(),
                input_or_cell_count_regime: "single cell".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            acquisitions: Vec::new(),
            relations: Vec::new(),
            raw_links: Vec::new(),
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_factor_graph_stage1(&evidence, &proposal);
        assert_eq!(accepted.status, "accepted");
        assert_eq!(accepted.materials.len(), 2);
        assert!(accepted
            .materials
            .iter()
            .any(|node| node.organism == "Homo sapiens"));
        assert!(accepted
            .materials
            .iter()
            .any(|node| node.organism == "Mus musculus"));
    }

    #[test]
    fn v2_factor_stage1_relations_use_rust_assigned_ids() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "Xenopus cells were sampled by aspiration.".into(),
            }],
            vec!["run.raw"],
        );
        let proposal = StudyFactorGraphProposal {
            decision: "propose_graph".into(),
            materials: vec![FactorMaterialProposal {
                local_id: "M7".into(),
                label: "Xenopus".into(),
                organism: "Xenopus laevis".into(),
                biological_material: "embryo cell".into(),
                experimental_role: "single-cell sample".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            regimes: vec![FactorRegimeProposal {
                local_id: "R9".into(),
                label: "aspiration".into(),
                experimental_role: "single-cell sampling".into(),
                isolation_or_loading_method: "capillary aspiration".into(),
                input_or_cell_count_regime: "single cell".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            acquisitions: Vec::new(),
            relations: vec![FactorRelationProposal {
                source_id: "M7".into(),
                relation_type: "material_to_regime".into(),
                target_id: "R9".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            raw_links: Vec::new(),
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_factor_graph_stage1(&evidence, &proposal);
        assert_eq!(accepted.relations.len(), 1);
        assert_eq!(accepted.relations[0].source_id, "M001");
        assert_eq!(accepted.relations[0].target_id, "R001");
    }

    #[test]
    fn v2_factor_stage1_strips_unsupported_raw_links() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "The project includes Xenopus cells.".into(),
            }],
            vec!["xenopus_D11.raw"],
        );
        let proposal = StudyFactorGraphProposal {
            decision: "propose_graph".into(),
            materials: vec![FactorMaterialProposal {
                local_id: "M1".into(),
                label: "Xenopus".into(),
                organism: "Xenopus laevis".into(),
                biological_material: "embryo cell".into(),
                experimental_role: "single-cell sample".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            regimes: vec![FactorRegimeProposal {
                local_id: "R1".into(),
                label: "single-cell".into(),
                experimental_role: "single-cell sampling".into(),
                isolation_or_loading_method: String::new(),
                input_or_cell_count_regime: "single cell".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            acquisitions: Vec::new(),
            relations: Vec::new(),
            raw_links: vec![FactorRawLinkProposal {
                node_id: "M1".into(),
                raw_file: "xenopus_D11.raw".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_factor_graph_stage1(&evidence, &proposal);
        assert!(accepted.raw_links.is_empty());
        assert_eq!(accepted.removed_raw_links.len(), 1);
    }

    #[test]
    fn v2_factor_stage1_human_review_is_terminal_before_phase_b() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:projectDescription".into(),
                text: "ambiguous study".into(),
            }],
            vec!["run.raw"],
        );
        let proposal = StudyFactorGraphProposal {
            decision: "human_review".into(),
            materials: Vec::new(),
            regimes: Vec::new(),
            acquisitions: Vec::new(),
            relations: Vec::new(),
            raw_links: Vec::new(),
            open_questions: vec!["structure unresolved".into()],
            reason: "insufficient evidence".into(),
        };
        let accepted = accept_factor_graph_stage1(&evidence, &proposal);
        assert_eq!(accepted.status, "human_review");
        assert_eq!(accepted.model_calls, 1);
        assert!(accepted.materials.is_empty());
        assert!(accepted.regimes.is_empty());
    }

    fn phase_b_test_factor_graph() -> StudyFactorGraphAcceptance {
        StudyFactorGraphAcceptance {
            harness_version: SCIENTIFIC_AGENT_FACTOR_GRAPH_STAGE1_VERSION.into(),
            accession: "PXDTEST".into(),
            status: "accepted".into(),
            materials: vec![AcceptedFactorMaterial {
                id: "M001".into(),
                label: "material".into(),
                organism: "Homo sapiens".into(),
                biological_material: "test cells".into(),
                experimental_role: "source material".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            regimes: vec![
                AcceptedFactorRegime {
                    id: "R001".into(),
                    label: "hydrodynamic".into(),
                    experimental_role: "single-cell".into(),
                    isolation_or_loading_method: "manual hydrodynamic loading".into(),
                    input_or_cell_count_regime: "single cell".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
                AcceptedFactorRegime {
                    id: "R002".into(),
                    label: "spray".into(),
                    experimental_role: "low input".into(),
                    isolation_or_loading_method: "spray voltage injection".into(),
                    input_or_cell_count_regime: "few cells".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
            ],
            acquisitions: vec![AcceptedFactorAcquisition {
                id: "A001".into(),
                label: "DIA".into(),
                acquisition_method_or_platform: "DIA-MS".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            }],
            relations: vec![
                AcceptedFactorRelation {
                    source_id: "M001".into(),
                    relation_type: "material_to_regime".into(),
                    target_id: "R001".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
                AcceptedFactorRelation {
                    source_id: "M001".into(),
                    relation_type: "material_to_regime".into(),
                    target_id: "R002".into(),
                    evidence_refs: vec!["E0001".into()],
                    notes: String::new(),
                },
            ],
            raw_links: Vec::new(),
            open_questions: Vec::new(),
            rejected_items: Vec::new(),
            removed_raw_links: Vec::new(),
            reason: String::new(),
            model_calls: 1,
        }
    }

    #[test]
    fn v2_factor_phase_b_schema_uses_rust_factor_ids_without_model_scope() {
        let schema = factor_phase_b_schema();
        let text = serde_json::to_string(&schema).unwrap();
        assert!(text.contains("target_factor_id"));
        assert!(text.contains("isolation_method"));
        assert!(!text.contains("branch_id"));
        assert!(!text.contains("projection_scope"));
        assert!(!text.contains("canonical_value"));
    }

    #[test]
    fn v2_factor_phase_b_rejects_orphan_factor_ids() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods".into(),
                text: "Single cells were manually loaded by hydrodynamic pressure.".into(),
            }],
            vec!["run.raw"],
        );
        let graph = phase_b_test_factor_graph();
        let proposal = FactorObservationBundleProposal {
            decision: "propose_observations".into(),
            observations: vec![FactorObservationProposal {
                target_factor_id: "R999".into(),
                concept_type: "isolation_method".into(),
                observed_value: "manual hydrodynamic pressure loading".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                notes: String::new(),
            }],
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_factor_phase_b_observations(&evidence, &graph, &proposal);
        assert_eq!(accepted.status, "human_review");
        assert!(accepted.observations.is_empty());
        assert_eq!(accepted.rejected_observations.len(), 1);
    }

    #[test]
    fn v2_factor_phase_b_rejects_cross_kind_scientific_scope() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods".into(),
                text: "Single cells were manually loaded by hydrodynamic pressure.".into(),
            }],
            vec!["run.raw"],
        );
        let graph = phase_b_test_factor_graph();
        let proposal = FactorObservationBundleProposal {
            decision: "propose_observations".into(),
            observations: vec![FactorObservationProposal {
                target_factor_id: "M001".into(),
                concept_type: "isolation_method".into(),
                observed_value: "manual hydrodynamic pressure loading".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                notes: String::new(),
            }],
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_factor_phase_b_observations(&evidence, &graph, &proposal);
        assert!(accepted.observations.is_empty());
        assert!(accepted.rejected_observations[0]
            .reason
            .contains("not valid for material"));
    }

    #[test]
    fn v2_factor_phase_b_keeps_multi_regime_observations_factor_scoped() {
        let evidence = evidence_with(
            vec![EvidenceItem { id:"E0001".into(), source_kind:"publication".into(), source_label:"methods".into(), text:"Single cells were manually loaded by hydrodynamic pressure and low-input samples used spray voltage injection.".into() }],
            vec!["run.raw"],
        );
        let graph = phase_b_test_factor_graph();
        let proposal = FactorObservationBundleProposal {
            decision: "propose_observations".into(),
            observations: vec![
                FactorObservationProposal {
                    target_factor_id: "R001".into(),
                    concept_type: "isolation_method".into(),
                    observed_value: "manual hydrodynamic pressure loading".into(),
                    evidence_refs: vec!["E0001".into()],
                    confidence: "high".into(),
                    notes: String::new(),
                },
                FactorObservationProposal {
                    target_factor_id: "R002".into(),
                    concept_type: "isolation_method".into(),
                    observed_value: "spray voltage injection".into(),
                    evidence_refs: vec!["E0001".into()],
                    confidence: "high".into(),
                    notes: String::new(),
                },
            ],
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_factor_phase_b_observations(&evidence, &graph, &proposal);
        assert_eq!(accepted.status, "accepted");
        assert_eq!(accepted.observations.len(), 2);
        assert!(accepted
            .observations
            .iter()
            .all(|obs| obs.projection_scope == "factor"));
        let claims = accepted
            .observations
            .iter()
            .map(factor_observation_as_claim)
            .collect::<Vec<_>>();
        assert!(claims.iter().all(|claim| claim.scope == "branch"));
        assert!(claims.iter().any(|claim| claim.branch_id == "R001"));
        assert!(claims.iter().any(|claim| claim.branch_id == "R002"));
    }

    #[test]
    fn v2_factor_phase_b_projects_only_unique_factor_scope_project_wide() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods".into(),
                text: "Single cells were isolated using evDISCO.".into(),
            }],
            vec!["run.raw"],
        );
        let mut graph = phase_b_test_factor_graph();
        graph.regimes.truncate(1);
        let proposal = FactorObservationBundleProposal {
            decision: "propose_observations".into(),
            observations: vec![FactorObservationProposal {
                target_factor_id: "R001".into(),
                concept_type: "isolation_method".into(),
                observed_value: "evDISCO".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                notes: String::new(),
            }],
            open_questions: Vec::new(),
            reason: String::new(),
        };
        let accepted = accept_factor_phase_b_observations(&evidence, &graph, &proposal);
        assert_eq!(accepted.observations.len(), 1);
        assert_eq!(accepted.observations[0].projection_scope, "project");
        let claim = factor_observation_as_claim(&accepted.observations[0]);
        assert_eq!(claim.scope, "project");
        assert!(claim.branch_id.is_empty());
    }

    #[test]
    fn v2_factor_phase_b_synthetic_compiler_branches_preserve_rust_ids_and_unresolved_raw_linkage()
    {
        let graph = phase_b_test_factor_graph();
        let branches = factor_graph_as_agent_branches(&graph);
        assert_eq!(branches.len(), 4);
        assert!(branches.iter().any(|branch| branch.id == "M001"));
        assert!(branches.iter().any(|branch| branch.id == "R001"));
        assert!(branches.iter().any(|branch| branch.id == "R002"));
        assert!(branches.iter().any(|branch| branch.id == "A001"));
        assert!(branches
            .iter()
            .all(|branch| branch.linkage_status == "unresolved"));
        assert!(branches
            .iter()
            .all(|branch| branch.linked_raw_files.is_empty()));
    }
    #[test]
    fn v2_deterministic_bridge_derives_regime_isolation_without_model_call() {
        let evidence = evidence_with(
            vec![EvidenceItem { id:"E0001".into(), source_kind:"publication".into(), source_label:"methods".into(), text:"Single cells were manually loaded by hydrodynamic pressure and low-input intact cells were introduced by spray voltage injection.".into() }],
            vec!["run.raw"],
        );
        let graph = phase_b_test_factor_graph();
        let accepted = derive_factor_graph_observations(&evidence, &graph);
        assert_eq!(accepted.model_calls, 0);
        assert_eq!(accepted.status, "accepted");
        assert!(accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "R001"
                && obs.concept_type == "isolation_method"
                && obs
                    .observed_value
                    .to_ascii_lowercase()
                    .contains("hydrodynamic")));
        assert!(accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "R002"
                && obs.concept_type == "isolation_method"
                && obs.observed_value.to_ascii_lowercase().contains("spray")));
    }

    #[test]
    fn v2_deterministic_bridge_does_not_turn_commercial_digest_loading_into_isolation() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods".into(),
                text: "A commercial reference digest was loaded for validation.".into(),
            }],
            vec!["run.raw"],
        );
        let mut graph = phase_b_test_factor_graph();
        graph.regimes = vec![AcceptedFactorRegime {
            id: "R001".into(),
            label: "reference".into(),
            experimental_role: "Validation/Method Development".into(),
            isolation_or_loading_method: "Standard loading of commercial digest".into(),
            input_or_cell_count_regime: "Commercial digest input".into(),
            evidence_refs: vec!["E0001".into()],
            notes: String::new(),
        }];
        let accepted = derive_factor_graph_observations(&evidence, &graph);
        assert!(!accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "R001" && obs.concept_type == "isolation_method"));
    }

    #[test]
    fn v2_deterministic_bridge_preserves_distinct_material_organisms() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "repository_metadata".into(),
                source_label: "organism".into(),
                text: "Homo sapiens and Mus musculus".into(),
            }],
            vec!["run.raw"],
        );
        let mut graph = phase_b_test_factor_graph();
        graph.materials = vec![
            AcceptedFactorMaterial {
                id: "M001".into(),
                label: "human".into(),
                organism: "Homo sapiens".into(),
                biological_material: "human cells".into(),
                experimental_role: "source".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            },
            AcceptedFactorMaterial {
                id: "M002".into(),
                label: "mouse".into(),
                organism: "Mus musculus".into(),
                biological_material: "mouse cells".into(),
                experimental_role: "source".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            },
        ];
        let accepted = derive_factor_graph_observations(&evidence, &graph);
        assert!(accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "M001"
                && obs.concept_type == "organism"
                && obs.observed_value == "Homo sapiens"));
        assert!(accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "M002"
                && obs.concept_type == "organism"
                && obs.observed_value == "Mus musculus"));
        assert!(accepted
            .observations
            .iter()
            .filter(|obs| obs.concept_type == "organism")
            .all(|obs| obs.projection_scope == "factor"));
    }

    #[test]
    fn v2_deterministic_bridge_only_derives_explicit_dia_or_dda_acquisition_mode() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "acquisition".into(),
                text: "DIA-MS acquisition was used.".into(),
            }],
            vec!["run.raw"],
        );
        let mut graph = phase_b_test_factor_graph();
        graph.acquisitions = vec![
            AcceptedFactorAcquisition {
                id: "A001".into(),
                label: "dia".into(),
                acquisition_method_or_platform: "CE-ESI-MS/MS with DIA".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            },
            AcceptedFactorAcquisition {
                id: "A002".into(),
                label: "esi".into(),
                acquisition_method_or_platform: "Direct ESI-MS".into(),
                evidence_refs: vec!["E0001".into()],
                notes: String::new(),
            },
        ];
        let accepted = derive_factor_graph_observations(&evidence, &graph);
        assert!(accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "A001" && obs.concept_type == "acquisition_mode"));
        assert!(!accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "A002" && obs.concept_type == "acquisition_mode"));
    }

    #[test]
    fn v2_deterministic_bridge_single_cell_sample_type_requires_explicit_input_regime() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods".into(),
                text: "Single cells were isolated.".into(),
            }],
            vec!["run.raw"],
        );
        let mut graph = phase_b_test_factor_graph();
        graph.regimes.truncate(1);
        graph.regimes[0].input_or_cell_count_regime = "Single cells".into();
        let accepted = derive_factor_graph_observations(&evidence, &graph);
        assert!(accepted
            .observations
            .iter()
            .any(|obs| obs.target_factor_id == "R001"
                && obs.concept_type == "sample_type"
                && obs.observed_value == "single cell"));
    }

    #[test]
    fn v2_deterministic_bridge_factor_claims_keep_rust_ids_and_unresolved_linkage() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods".into(),
                text: "Single cells were manually loaded by hydrodynamic pressure.".into(),
            }],
            vec!["run.raw"],
        );
        let graph = phase_b_test_factor_graph();
        let accepted = derive_factor_graph_observations(&evidence, &graph);
        let claims = accepted
            .observations
            .iter()
            .map(factor_observation_as_claim)
            .collect::<Vec<_>>();
        assert!(claims
            .iter()
            .filter(|claim| claim.scope == "branch")
            .all(|claim| claim.branch_id.starts_with('M')
                || claim.branch_id.starts_with('R')
                || claim.branch_id.starts_with('A')));
        let branches = factor_graph_as_agent_branches(&graph);
        assert!(branches
            .iter()
            .all(|branch| branch.linkage_status == "unresolved"));
    }

    #[test]
    fn v2_canonical_hardening_rejects_spray_voltage_to_manual_picking() {
        let evidence = evidence_with(
            vec![EvidenceItem { id:"E0001".into(), source_kind:"manuscript_semantic_evidence".into(), source_label:"single cell isolation method".into(), text:"Single cells were also manually picked in a comparison experiment; low-input intact cells were introduced by spray voltage injection.".into() }],
            vec!["run.raw"],
        );
        let claim = claim(
            "isolation_method",
            "Spray voltage injection",
            "branch",
            "R001",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        match adjudicate_workspace_claim(&evidence, &state, &claim) {
            ClaimAdjudication::TemplateGap { observed_value, .. } => {
                assert_eq!(observed_value, "Spray voltage injection")
            }
            ClaimAdjudication::Unresolved { .. } => {}
            ClaimAdjudication::Canonical { value, .. } => {
                panic!("hardened mode must not canonicalize spray-voltage injection to {value:?}")
            }
            other => panic!("expected fail-closed template gap/unresolved outcome, got {other:?}"),
        }
    }

    #[test]
    fn v2_canonical_hardening_rejects_hydrodynamic_to_manual_picking() {
        let evidence = evidence_with(
            vec![EvidenceItem { id:"E0001".into(), source_kind:"manuscript_semantic_evidence".into(), source_label:"single cell isolation method".into(), text:"Individual cells were manually loaded by hydrodynamic pressure; another section mentions manually picked cells.".into() }],
            vec!["run.raw"],
        );
        let claim = claim(
            "isolation_method",
            "Manual hydrodynamic pressure loading",
            "branch",
            "R002",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        match adjudicate_workspace_claim(&evidence, &state, &claim) {
            ClaimAdjudication::TemplateGap { observed_value, .. } => {
                assert_eq!(observed_value, "Manual hydrodynamic pressure loading")
            }
            other => panic!("expected hardened template gap, got {other:?}"),
        }
    }

    #[test]
    fn v2_canonical_hardening_allows_explicit_manual_picking() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "Single cells were isolated by manual picking.".into(),
            }],
            vec!["run.raw"],
        );
        let claim = claim("isolation_method", "manual picking", "project", "");
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        assert!(
            matches!(adjudicate_workspace_claim(&evidence, &state, &claim), ClaimAdjudication::Canonical { ref value, .. } if value == "manual picking")
        );
    }

    #[test]
    fn v2_canonical_hardening_preserves_capillary_template_gap() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "Proteome was collected by capillary microsampling from an identified cell."
                    .into(),
            }],
            vec!["run.raw"],
        );
        let claim = claim(
            "isolation_method",
            "Capillary microsampling and electrokinetic injection",
            "branch",
            "R002",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        assert!(matches!(
            adjudicate_workspace_claim(&evidence, &state, &claim),
            ClaimAdjudication::TemplateGap { .. } | ClaimAdjudication::Unresolved { .. }
        ));
    }

    #[test]
    fn v2_canonical_hardening_never_promotes_tdisco_to_manual_picking() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single cell isolation method".into(),
                text: "Single-cell isolation used tDISCO/evDISCO digital microfluidic isolation."
                    .into(),
            }],
            vec!["run.raw"],
        );
        let claim = claim(
            "isolation_method",
            "tDISCO (transient digital microfluidic isolation)",
            "project",
            "",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        assert!(
            !matches!(adjudicate_workspace_claim(&evidence, &state, &claim), ClaimAdjudication::Canonical { ref value, .. } if value == "manual picking")
        );
    }

    #[test]
    fn v2_canonical_hardening_leaves_acquisition_canonicalization_unchanged() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "acquisition".into(),
                text: "Data-independent acquisition (DIA) was used.".into(),
            }],
            vec!["run.raw"],
        );
        let claim = claim("acquisition_mode", "DIA-MS/MS", "project", "");
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        assert!(
            matches!(adjudicate_workspace_claim(&evidence, &state, &claim), ClaimAdjudication::Canonical { ref value, .. } if value == "Data-independent acquisition")
        );
    }

    #[test]
    fn v2_semantic_fidelity_hardening_maps_explicit_dda_pasef_to_dda() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "acquisition".into(),
                text: "Library fractions were measured using DDA-PASEF on timsTOF SCP.".into(),
            }],
            vec!["run.raw"],
        );
        let claim = claim(
            "acquisition_mode",
            "DDA-PASEF on timsTOF SCP",
            "branch",
            "A002",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        assert!(
            matches!(adjudicate_workspace_claim(&evidence, &state, &claim), ClaimAdjudication::Canonical { ref value, .. } if value == "NT=data-dependent acquisition;AC=PRIDE:0000627")
        );
    }

    #[test]
    fn v2_semantic_fidelity_hardening_preserves_explicit_dia_pasef() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "acquisition".into(),
                text: "Single-fiber peptides were measured by DIA-PASEF.".into(),
            }],
            vec!["run.raw"],
        );
        let claim = claim(
            "acquisition_mode",
            "DIA-PASEF on timsTOF SCP",
            "branch",
            "A001",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        assert!(
            matches!(adjudicate_workspace_claim(&evidence, &state, &claim), ClaimAdjudication::Canonical { ref value, .. } if value == "Data-independent acquisition")
        );
    }

    #[test]
    fn v2_semantic_fidelity_hardening_never_flips_dda_observation_to_dia() {
        let evidence = evidence_with(
            vec![EvidenceItem { id:"E0001".into(), source_kind:"manuscript_semantic_evidence".into(), source_label:"acquisition".into(), text:"The experiment contains DIA-PASEF single-fiber runs and DDA-PASEF library fractions.".into() }],
            vec!["run.raw"],
        );
        let claim = claim(
            "acquisition_mode",
            "DDA-PASEF on timsTOF SCP",
            "branch",
            "A002",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        match adjudicate_workspace_claim(&evidence, &state, &claim) {
            ClaimAdjudication::Canonical { value, .. } => {
                assert_eq!(value, "NT=data-dependent acquisition;AC=PRIDE:0000627")
            }
            other => panic!("expected explicit DDA observation to remain DDA, got {other:?}"),
        }
    }

    #[test]
    fn v2_semantic_fidelity_hardening_does_not_promote_dia_nn_search_to_dia() {
        let evidence = evidence_with(
            vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "acquisition".into(),
                text: "DIA-NN library-free search was used for data processing.".into(),
            }],
            vec!["run.raw"],
        );
        let claim = claim(
            "acquisition_mode",
            "DIA-NN library-free search on EvoSep One system",
            "project",
            "",
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into(),
            claims: vec![claim.clone()],
            ..Default::default()
        };
        assert!(!matches!(
            adjudicate_workspace_claim(&evidence, &state, &claim),
            ClaimAdjudication::Canonical { .. }
        ));
    }

    #[test]
    fn v2_semantic_fidelity_hardening_masks_unfaithful_acquisition_baseline() {
        let mut proposal = SdrfProposal::default();
        proposal.proteomics_data_acquisition_method = "Data-independent acquisition".into();
        proposal.evidence_refs.insert(
            "proteomics_data_acquisition_method".into(),
            vec!["E0001".into()],
        );
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_SEMANTIC_FIDELITY_HARDENED_VERSION.into(),
            claims: vec![ScientificClaim {
                concept_type: "acquisition_mode".into(),
                value: "DDA-PASEF on timsTOF SCP".into(),
                scope: "branch".into(),
                branch_id: "A002".into(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "accepted factor observation".into(),
            }],
            ..Default::default()
        };
        let mut issues = Vec::new();
        harden_factor_acquisition_project_baseline(&mut proposal, &state, &mut issues);
        assert_eq!(proposal.proteomics_data_acquisition_method, "not available");
        assert!(!proposal
            .evidence_refs
            .contains_key("proteomics_data_acquisition_method"));
        assert!(issues.iter().any(|issue| issue.code
            == "scientific_agent_factor_acquisition_semantic_fidelity_baseline_mask"));
    }

    #[test]
    fn v2_canonical_hardening_masks_unfaithful_bootstrap_before_projection() {
        let mut proposal = SdrfProposal::default();
        proposal.single_cell_isolation_method = "manual picking".into();
        proposal
            .evidence_refs
            .insert("single_cell_isolation_method".into(), vec!["E0001".into()]);
        let state = ScientificWorkspaceState {
            harness_version: SCIENTIFIC_AGENT_FACTOR_CANONICAL_HARDENED_VERSION.into(),
            claims: vec![ScientificClaim {
                concept_type: "isolation_method".into(),
                value: "Spray voltage injection".into(),
                scope: "branch".into(),
                branch_id: "R001".into(),
                status: "supported".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "accepted factor observation".into(),
            }],
            ..Default::default()
        };
        let mut issues = Vec::new();
        harden_factor_isolation_project_baseline(&mut proposal, &state, &mut issues);
        assert_eq!(proposal.single_cell_isolation_method, "not available");
        assert!(!proposal
            .evidence_refs
            .contains_key("single_cell_isolation_method"));
        assert!(issues
            .iter()
            .any(|issue| issue.code
                == "scientific_agent_factor_canonicalization_hardened_baseline_mask"));
    }
}
