// Mirrors backend/app/schemas. Keep in sync with the OpenAPI schema at /api/openapi.json.

export interface FieldError {
  loc: (string | number)[]
  message: string
  type: string
}

export interface ErrorResponse {
  error: {
    code: string
    message: string
    request_id: string
    fields: FieldError[]
  }
}

export interface HealthResponse {
  status: 'ok'
  version: string
}

export interface JobInputRequest {
  title?: string
  company?: string
  source_url?: string
  description: string
}

export type SectionKind =
  | 'required'
  | 'preferred'
  | 'responsibilities'
  | 'about_role'
  | 'about_company'
  | 'benefits'

export interface DetectedSection {
  kind: SectionKind
  heading: string
  line: number
}

export interface PreviewWarning {
  code: 'short_description' | 'no_requirements_heading'
  message: string
}

export interface JobInputPreview {
  title: string | null
  company: string | null
  source_url: string | null
  character_count: number
  word_count: number
  sections: DetectedSection[]
  warnings: PreviewWarning[]
  saved: false
}

// Server-side limits, repeated here only for friendlier early feedback.
export const JOB_LIMITS = {
  titleMax: 200,
  companyMax: 200,
  descriptionMin: 40,
  descriptionMax: 50_000,
} as const

// --- Accounts, profile, masters, evidence (mirrors backend/app/schemas) -------

export interface User {
  id: string
  email: string | null
}

export interface SessionResponse {
  status: 'signed_in' | 'confirmation_required'
  user: User | null
}

export interface CsrfResponse {
  csrf_token: string
}

export interface Profile {
  email: string | null
  display_name: string | null
  filename_prefix: string | null
  lock_version: number
}

export interface ProfileUpdate {
  expected_version: number
  display_name: string | null
  filename_prefix: string | null
}

export interface MasterVersion {
  id: string
  version_number: number
  file_kind: 'docx' | 'pdf'
  size_bytes: number
  sha256: string
  original_filename: string
  extraction_status: 'complete' | 'partial' | 'failed'
  extraction_notes: string[]
  evidence_count: number
  created_at: string
}

export interface UploadResult {
  master: MasterVersion
  duplicate: boolean
}

export type ExperienceType = 'professional' | 'internship' | 'academic' | 'personal' | 'other' | 'unclassified'
export type VerificationStatus = 'unverified' | 'verified' | 'rejected'

export interface EvidenceItem {
  id: string
  master_version_id: string | null
  origin: 'master' | 'confirmation'
  position: number
  version: number
  text: string
  source_text: string
  section: string | null
  entry: string | null
  date_text: string | null
  metrics: string[]
  experience_type: ExperienceType
  suggested_type: ExperienceType
  verification_status: VerificationStatus
  change_source: 'extracted' | 'user' | 'confirmation'
  updated_at: string
}

export interface EvidenceUpdate {
  expected_version: number
  text?: string
  experience_type: ExperienceType
  verification_status: VerificationStatus
}

export const ACCOUNT_LIMITS = {
  passwordMin: 12,
  passwordMax: 128,
  uploadMaxBytes: 5 * 1024 * 1024,
  filenamePrefixMax: 40,
  displayNameMax: 100,
  evidenceTextMax: 2000,
} as const

// --- Jobs, tailoring runs, drafts (mirrors backend/app/schemas/tailoring.py) ---

export interface JobOut {
  id: string
  title: string | null
  company: string | null
  source_url: string | null
  version: number
  created_at: string
}

export type RequirementCategory = 'required' | 'preferred' | 'responsibility' | 'terminology'

export interface Requirement {
  id: string
  category: RequirementCategory
  text: string
  terms: string[]
  mandatory: boolean
}

export interface AnalysisContent {
  role_title: string
  seniority: string | null
  requirements: Requirement[]
}

export interface Question {
  id: string
  requirement_id: string
  requirement_text: string
  term: string
  question: string
  status: 'open' | 'confirmed' | 'declined' | 'dismissed'
  evidence_id: string | null
}

export interface QuestionAnswer {
  answer: 'yes' | 'no' | 'skip'
  experience_type?: Exclude<ExperienceType, 'unclassified'>
  entry_evidence_id?: string
  description?: string
}

export interface DraftBullet {
  text: string
  evidence_ids: string[]
  requirement_ids: string[]
}

export interface DraftEntry {
  ref: string
  section: string | null
  entry: string | null
  date_text: string | null
  experience_type: string
  bullets: DraftBullet[]
}

export interface DraftContent {
  headline: string
  summary: string | null
  entries: DraftEntry[]
  skills: { text: string; evidence_ids: string[] }[]
  omitted: { evidence_id: string; reason: 'not_selected' | 'failed_validation' }[]
  format?: FormatPreferences
}

export interface ValidationIssue {
  code: string
  severity: 'error' | 'warning'
  where: string
  message: string
}

export interface ValidationReport {
  rule_version: string
  issues: ValidationIssue[]
  removed: string[]
  repaired: boolean
  passed: boolean
}

export interface DraftOut {
  version: number
  content: DraftContent
  validation: ValidationReport
  created_by: string
  model: string | null
  prompt_version: string | null
  created_at: string
  sources: Record<string, { text: string; entry: string | null; experience_type: string }>
}

export type RunStatus =
  | 'queued'
  | 'analyzing'
  | 'awaiting_confirmation'
  | 'ready'
  | 'drafting'
  | 'validating'
  | 'awaiting_review'
  | 'approved'
  | 'failed'
  | 'cancelled'

export interface RunOut {
  id: string
  status: RunStatus
  error_code: string | null
  lock_version: number
  job: JobOut
  master_version_id: string | null
  analysis: AnalysisContent | null
  analysis_model: string | null
  questions: Question[]
  draft: DraftOut | null
  artifact?: ArtifactOut | null
  approval?: CurrentApproval | null
  ats?: AtsOverview | null
  jobs?: RunJob[]
  usage: { calls: number; cost_usd: string; input_tokens: number; output_tokens: number }
  created_at: string
  updated_at: string
}

export interface RunSummary {
  id: string
  status: RunStatus
  job_title: string | null
  company: string | null
  created_at: string
}

export interface AIStatus {
  provider: 'openai' | 'demo' | 'disabled'
  model: string | null
  reasoning_effort: string | null
  disclosure: string
  user_daily_budget_usd: string
  spent_today_usd: string
}

// --- Review, rendering, approval (mirrors backend/app/api/v1/review_routes.py) ---

export interface FormatPreferences {
  max_bullets_per_entry: number
  project_bullets: number
  bold_project_technologies: boolean
  languages_on_own_line: boolean
  show_headline: boolean
  locked_entries: string[]
}

export interface ArtifactOut {
  id: string
  draft_version: number
  sha256: string
  page_count: number
  layout: 'template' | 'reconstructed'
  passed: boolean
  checks: {
    page_count: number
    missing_phrases: string[]
    out_of_order: string[]
    clipped: string[]
    dropped_to_fit: string[]
    capped_by_preference: string[]
    notes: string[]
    searchable: boolean
  }
  filename: string
  created_at: string
}

export interface CurrentApproval {
  id: string
  draft_version: number
  artifact_id: string
  filename: string
  approved_at: string
}

export interface VersionSummary {
  version: number
  created_by: string
  created_at: string
  passed: boolean
  model: string | null
  ats_status?: AtsStatus
  ats_score?: number | null
}

export interface VersionOut extends VersionSummary {
  content: DraftContent
  validation: ValidationReport
}

export type EditOperation =
  | { op: 'update_bullet'; entry_ref: string; index: number; text: string }
  | { op: 'remove_bullet'; entry_ref: string; index: number }
  | { op: 'move_bullet'; entry_ref: string; index: number; to: number }
  | { op: 'add_fact'; evidence_id: string }
  | { op: 'update_skill'; index: number; text: string }
  | { op: 'remove_skill'; index: number }
  | { op: 'update_headline'; text: string }
  | { op: 'update_summary'; text: string | null }
  | { op: 'update_format'; format: FormatPreferences }

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  draft_version: number | null
  created_at: string
}

export interface ApprovalOut {
  id: string
  run_id: string
  job_title: string | null
  company: string | null
  filename: string
  draft_version: number
  artifact_sha256: string
  approved_at: string
}

// --- ATS match estimate (mirrors backend/app/ats/scoring.py and app/schemas/ats.py) ---

export type AtsStatus = 'pending' | 'complete' | 'failed' | 'stale'
export type RequirementStatus = 'direct' | 'related' | 'missing' | 'uncertain'

export interface AtsMatch {
  kind: 'direct' | 'related' | 'negated'
  basis: 'exact_term' | 'draft_link' | 'shared_wording'
  where: string
  snippet: string
  term: string | null
}

export interface AtsRequirement {
  id: string
  merged_ids: string[]
  category: RequirementCategory
  text: string
  mandatory: boolean
  scored: boolean
  status: RequirementStatus
  credit: number
  matched_terms: string[]
  missing_terms: string[]
  excluded_terms: string[]
  notes: string[]
  matches: AtsMatch[]
}

export interface AtsResult {
  rubric_version: string
  analysis_fingerprint: string
  score: number | null
  categories: { category: RequirementCategory; requirements: number; weight: number; score: number }[]
  requirements: AtsRequirement[]
  mandatory_gaps: string[]
  mandatory_partial: string[]
  counts: Record<RequirementStatus, number>
  repeated_terms: { term: string; mentions: number }[]
}

export interface AtsComparison {
  compatible: boolean
  reason: string | null
  baseline: number | null
  tailored: number | null
  delta: number | null
  improved: string[]
  declined: string[]
}

export interface AtsReport {
  kind: 'master_baseline' | 'draft' | 'final_pdf'
  status: AtsStatus
  score: number | null
  result: AtsResult | null
  rubric_version: string | null
  draft_version: number | null
  artifact_id: string | null
  master_version_id: string | null
  checked_at: string | null
  message: string | null
}

export interface AtsOverview {
  rubric_version: string
  baseline: AtsReport | null
  draft: AtsReport | null
  final_pdf: AtsReport | null
  draft_vs_baseline: AtsComparison | null
  pdf_vs_baseline: AtsComparison | null
}

// --- Durable jobs (mirrors JobStatusOut in backend/app/schemas/tailoring.py) ---

export type JobKind = 'analyze' | 'draft' | 'regenerate' | 'chat' | 'render' | 'ats_check'
export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface RunJob {
  id: string
  kind: JobKind
  status: JobState
  attempts: number
  max_attempts: number
  error_code: string | null
  result: Record<string, unknown> | null
  created_at: string
  finished_at: string | null
}
