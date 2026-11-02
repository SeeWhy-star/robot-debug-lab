export type Pose = { x: number; y: number; theta: number; speed: number };
export type Robot = {
  id: string;
  pose?: Pose;
  trace: number[][];
  controller_alive: boolean;
  busy: boolean;
  runtime?: string;
  scan?: { raw_scan_hz: number; nav2_scan_hz: number; scan_forwarding: boolean };
  localization?: { status: string; position_error: number | null; threshold?: number };
};
export type Event = {
  id: string;
  time: number;
  kind: string;
  title: string;
  data: any;
  schema_version?: number;
  step_id?: string | null;
};
export type Verdict = {
  success: boolean;
  error: number;
  elapsed: number;
  message: string;
};
export type Diagnosis = {
  // v1 aliases, always present
  cause: string;
  summary: string;
  evidence_ids: string[];
  repair_rationale: string;
  // v2
  schema_version?: number;
  strategy?: string;
  classification?: string;
  known_cause?: string | null;
  suspected_subsystem?: string;
  cited_evidence?: string[];
  ruled_out?: { hypothesis: string; reason: string; evidence_ids: string[] }[];
  repair_requested?: boolean;
  requested_repair?: string | null;
  recommended_action?: string;
};
export type Authorization = {
  decision: "authorized" | "denied" | "not_applicable";
  known_cause: string | null;
  requested_operation: string | null;
  authorized_operation: string | null;
  rule_version: string;
  max_evidence_age_seconds: number;
  evidence_ids: string[];
  checks: { name: string; passed: boolean; expected: unknown; actual: unknown }[];
  reason: string;
};
export type MissionStep = {
  id: string;
  action: string;
  description: string;
  destination_id?: string;
  x?: number;
  y?: number;
  duration_seconds?: number;
  auto_repair: boolean;
  status: string;
};
export type Usage = { prompt_tokens: number; completion_tokens: number };
export type AssistantPlan = {
  id: string;
  status: string;
  message: string;
  steps?: MissionStep[];
  usage?: Usage;
  run_ids: string[];
  prompt?: string;
  auto_repair_ceiling?: boolean;
  turns?: { role: string; content: string }[];
  robot_id?: string;
  plan_type?: string;
  assignments?: { robot_id: string; rationale: string; steps: MissionStep[] }[];
  handoffs?: {
    source_robot_id: string;
    target_robot_id: string;
    handoff_run_id: string;
    status: string;
    final_gazebo_error?: number;
  }[];
  reason?: string;
  suggested_requests?: string[];
  spatial_analysis?: {
    objective: string;
    metrics: { target_separation: number; total_nav2_path_length: number; requested_clearance: number };
    evidence: {
      map: string;
      map_fingerprint: string;
      safe_candidate_count: number;
      route_validation: string;
      algorithm: string;
    };
  };
  fleet_acceptance?: {
    success: boolean;
    source: string;
    target_separation?: number;
    actual_separation?: number;
    separation_error?: number;
    position_threshold?: number;
  };
  handoff_policy_reason?: string;
};
export type Run = {
  id: string;
  robot_id: string;
  mode: string;
  status: string;
  phase?: string;
  phase_context?: { reason: string | null; activity: string | null; step_id: string | null };
  terminal?: boolean;
  created: number;
  finished?: number | null;
  events: Event[];
  target?: number[];
  task_type?: string;
  plan?: AssistantPlan;
  backend?: string;
  model?: string;
  prompt_version?: string;
  diagnosis?: Diagnosis | null;
  repair_authorization?: Authorization | null;
  baseline?: Verdict;
  validation?: Verdict | null;
  usage: Usage;
  error?: string;
  active_target?: number[] | null;
  steps?: MissionStep[];
};
export type State = {
  plans?: AssistantPlan[];
  runtime?: string;
  simulation?: string;
  robots: Robot[];
  runs: Run[];
  active_targets?: Record<string, number[]>;
  traffic?: {
    active: { robot_id: string; step_id: string; path_mode: string }[];
    queued: number;
    clearance: number;
  };
  model: string;
  ai_configured: boolean;
  token: string;
  ros_domain: string;
};
export type StrategyRow = {
  strategy: string;
  model: string;
  backend: string;
  prompt_version: string;
  trials: number;
  known_fault_accuracy: number;
  unknown_fault_classification_rate: number;
  unknown_subsystem_accuracy: number;
  escalation_rate: number;
  repair_requests: number;
  repairs_authorized: number;
  repairs_denied: number;
  wrong_repairs: number;
  average_diagnostic_tool_calls: number;
  average_authorization_tool_calls: number;
  average_investigation_seconds: number;
  average_total_tokens: number;
};
export type Metrics = {
  sample: {
    run_count: number;
    experiment_ids: string[];
    unlabeled_runs_excluded: number;
    small_sample_warning: boolean;
    notes: string;
  };
  overall: Record<string, number>;
  diagnosis_by_strategy: StrategyRow[];
};
