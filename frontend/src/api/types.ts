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
  master_version_id: string
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
  change_source: 'extracted' | 'user'
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
