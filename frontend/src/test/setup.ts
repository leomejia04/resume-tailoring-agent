import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach } from 'vitest'

export const TEST_CSRF = 'test-csrf-token-0123456789-abcdefghijklmn'

beforeEach(() => {
  // The server sets this readable cookie; requests echo it in X-CSRF-Token.
  document.cookie = `rt_csrf=${TEST_CSRF}; path=/`
})

afterEach(() => {
  cleanup()
  document.cookie = 'rt_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT'
})
