import type {
  AIStatus,
  CsrfResponse,
  JobOut,
  Question,
  QuestionAnswer,
  RunOut,
  RunSummary,
  ErrorResponse,
  EvidenceItem,
  EvidenceUpdate,
  FieldError,
  HealthResponse,
  JobInputPreview,
  JobInputRequest,
  MasterVersion,
  Profile,
  ProfileUpdate,
  SessionResponse,
  UploadResult,
  User,
} from './types'

const API_BASE = '/api/v1'
const DEFAULT_TIMEOUT_MS = 15_000
const UPLOAD_TIMEOUT_MS = 90_000
// Cookie names differ by environment (__Host- prefix over HTTPS).
const CSRF_COOKIES = ['__Host-rt_csrf', 'rt_csrf']
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** Every failed request becomes an ApiError, whether the server answered or not. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly fields: FieldError[]

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null = null,
    fields: FieldError[] = [],
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.fields = fields
  }
}

function isErrorResponse(data: unknown): data is ErrorResponse {
  if (typeof data !== 'object' || data === null || !('error' in data)) return false
  const error = (data as { error: unknown }).error
  return (
    typeof error === 'object' &&
    error !== null &&
    typeof (error as { code?: unknown }).code === 'string' &&
    typeof (error as { message?: unknown }).message === 'string'
  )
}

export function readCookie(name: string): string | null {
  for (const part of document.cookie.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return decodeURIComponent(rest.join('='))
  }
  return null
}

// --- Session-ended notifications (the auth context listens) -------------------

type Listener = () => void
const sessionEndedListeners = new Set<Listener>()

export function onSessionEnded(listener: Listener): () => void {
  sessionEndedListeners.add(listener)
  return () => sessionEndedListeners.delete(listener)
}

// --- Core request ------------------------------------------------------------

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
  timeoutMs?: number
  /** Refresh an expired session once and retry. */
  retryOnExpired?: boolean
}

let csrfPromise: Promise<string> | null = null
let refreshPromise: Promise<void> | null = null

async function csrfToken(): Promise<string> {
  for (const name of CSRF_COOKIES) {
    const value = readCookie(name)
    if (value) return value
  }
  csrfPromise ??= send<CsrfResponse>('/auth/csrf', {})
    .then((response) => response.csrf_token)
    .finally(() => {
      csrfPromise = null
    })
  return csrfPromise
}

function refreshSession(): Promise<void> {
  refreshPromise ??= send<SessionResponse>('/auth/refresh', { method: 'POST', retryOnExpired: false })
    .then(() => undefined)
    .finally(() => {
      refreshPromise = null
    })
  return refreshPromise
}

async function send<T>(path: string, options: RequestOptions): Promise<T> {
  const { method = 'GET', body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options
  const timeout = AbortSignal.timeout(timeoutMs)
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout

  const headers: Record<string, string> = { Accept: 'application/json' }
  let payload: BodyInit | undefined
  if (body instanceof FormData) {
    payload = body // the browser sets the multipart boundary itself
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  if (UNSAFE_METHODS.has(method)) headers['X-CSRF-Token'] = await csrfToken()

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: payload,
      credentials: 'same-origin',
      signal: combined,
    })
  } catch (error) {
    if (signal?.aborted) throw error // the caller cancelled; let it decide what to do
    if (timeout.aborted) {
      throw new ApiError(0, 'timeout', 'The server took too long to respond. Please try again.')
    }
    throw new ApiError(0, 'network_error', 'Could not reach the server. Check your connection and try again.')
  }

  const requestId = response.headers.get('X-Request-ID')
  if (response.status === 204) return undefined as T
  const isJson = response.headers.get('Content-Type')?.includes('application/json') ?? false
  const data: unknown = isJson ? await response.json().catch(() => null) : null

  if (!response.ok) {
    if (isErrorResponse(data)) {
      const { code, message, request_id, fields } = data.error
      throw new ApiError(response.status, code, message, request_id ?? requestId, fields ?? [])
    }
    throw new ApiError(response.status, 'http_error', `The request failed (status ${response.status}).`, requestId)
  }
  if (data === null) {
    throw new ApiError(response.status, 'invalid_response', 'The server sent an unexpected response.', requestId)
  }
  return data as T
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const retry = options.retryOnExpired ?? !path.startsWith('/auth/')
  try {
    return await send<T>(path, options)
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) throw error
    if (retry && error.code === 'session_expired') {
      try {
        await refreshSession()
      } catch (refreshError) {
        sessionEndedListeners.forEach((listener) => listener())
        throw refreshError
      }
      return send<T>(path, options)
    }
    if (!path.startsWith('/auth/')) sessionEndedListeners.forEach((listener) => listener())
    throw error
  }
}

// --- Endpoints ---------------------------------------------------------------

export const getHealth = (signal?: AbortSignal) =>
  apiRequest<HealthResponse>('/health', { signal, timeoutMs: 5_000 })

export const previewJobInput = (input: JobInputRequest, signal?: AbortSignal) =>
  apiRequest<JobInputPreview>('/jobs/preview', { method: 'POST', body: input, signal })

export const getMe = async (signal?: AbortSignal): Promise<User> => {
  try {
    return await apiRequest<User>('/auth/me', { signal })
  } catch (error) {
    // An expired access token with a valid refresh token: renew, then ask again.
    if (error instanceof ApiError && error.code === 'session_expired') {
      await refreshSession()
      return apiRequest<User>('/auth/me', { signal })
    }
    throw error
  }
}

export const signIn = (email: string, password: string) =>
  apiRequest<SessionResponse>('/auth/login', { method: 'POST', body: { email, password } })

export const signUp = (email: string, password: string) =>
  apiRequest<SessionResponse>('/auth/signup', { method: 'POST', body: { email, password } })

export const signOut = () => apiRequest<void>('/auth/logout', { method: 'POST' })

export const requestPasswordReset = (email: string) =>
  apiRequest<{ status: 'accepted'; message: string }>('/auth/password-reset', { method: 'POST', body: { email } })

export const confirmPasswordReset = (tokenHash: string, password: string) =>
  apiRequest<SessionResponse>('/auth/password-reset/confirm', {
    method: 'POST',
    body: { token_hash: tokenHash, password },
  })

export const getProfile = (signal?: AbortSignal) => apiRequest<Profile>('/profile', { signal })

export const updateProfile = (update: ProfileUpdate) =>
  apiRequest<Profile>('/profile', { method: 'PUT', body: update })

export const listMasters = (signal?: AbortSignal) => apiRequest<MasterVersion[]>('/masters', { signal })

export const uploadMaster = (file: File) => {
  const form = new FormData()
  form.append('file', file)
  return apiRequest<UploadResult>('/masters', { method: 'POST', body: form, timeoutMs: UPLOAD_TIMEOUT_MS })
}

export const masterFileUrl = (id: string) => `${API_BASE}/masters/${encodeURIComponent(id)}/file`

export const listEvidence = (masterVersionId?: string, signal?: AbortSignal) =>
  apiRequest<EvidenceItem[]>(
    masterVersionId ? `/evidence?master_version_id=${encodeURIComponent(masterVersionId)}` : '/evidence',
    { signal },
  )

export const addEvidenceVersion = (id: string, update: EvidenceUpdate) =>
  apiRequest<EvidenceItem>(`/evidence/${encodeURIComponent(id)}/versions`, { method: 'POST', body: update })

// Model calls run inside these requests for now, so allow them more time.
const AI_TIMEOUT_MS = 180_000

export const getAIStatus = (signal?: AbortSignal) => apiRequest<AIStatus>('/ai/status', { signal })

export const createJob = (input: JobInputRequest) => apiRequest<JobOut>('/jobs', { method: 'POST', body: input })

export const listRuns = (signal?: AbortSignal) => apiRequest<RunSummary[]>('/runs', { signal })

export const startRun = (jobId: string, masterVersionId?: string) =>
  apiRequest<RunOut>('/runs', {
    method: 'POST',
    body: { job_id: jobId, ...(masterVersionId ? { master_version_id: masterVersionId } : {}) },
    timeoutMs: AI_TIMEOUT_MS,
  })

export const getRun = (id: string, signal?: AbortSignal) =>
  apiRequest<RunOut>(`/runs/${encodeURIComponent(id)}`, { signal })

export const answerQuestion = (runId: string, questionId: string, answer: QuestionAnswer) =>
  apiRequest<Question>(`/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(questionId)}`, {
    method: 'POST',
    body: answer,
  })

export const generateDraft = (runId: string) =>
  apiRequest<RunOut>(`/runs/${encodeURIComponent(runId)}/draft`, { method: 'POST', timeoutMs: AI_TIMEOUT_MS })

export const cancelRun = (runId: string) =>
  apiRequest<RunOut>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' })
