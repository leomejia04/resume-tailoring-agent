import { ApiError } from './client'

/** Map server field errors (loc: ['body', name]) to a { name: message } object. */
export function fieldErrors(error: unknown): Record<string, string> {
  const result: Record<string, string> = {}
  if (error instanceof ApiError) {
    for (const field of error.fields) {
      const name = field.loc[field.loc.length - 1]
      if (typeof name === 'string') result[name] = field.message.charAt(0).toUpperCase() + field.message.slice(1)
    }
  }
  return result
}

export function messageOf(error: unknown): { message: string; requestId: string | null } {
  if (error instanceof ApiError) return { message: error.message, requestId: error.requestId }
  return { message: 'Something unexpected happened. Please try again.', requestId: null }
}
