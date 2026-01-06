const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

export function getAdminToken() {
  return localStorage.getItem('adminToken') || ''
}

export function setAdminToken(token) {
  localStorage.setItem('adminToken', token || '')
}

async function request(path, opts = {}) {
  const headers = { ...(opts.headers || {}) }

  // Only set JSON content-type when body is not FormData
  const isFormData = typeof FormData !== 'undefined' && opts.body instanceof FormData
  if (!isFormData && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json'
  }

  const token = getAdminToken()
  if (token) headers['X-Admin-Token'] = token

  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers })

  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      msg = body.detail || JSON.stringify(body)
    } catch (_) {
      // ignore
    }
    throw new Error(msg)
  }

  if (res.status === 204) return null
  return res.json()
}

/**
 * Universal client:
 * - client.get(...)
 * - client() -> returns itself so client().get(...) works
 * - client(path, opts) -> direct request
 */
function client(path, opts) {
  if (!path) return client
  return request(path, opts)
}

client.get = (path) => request(path, { method: 'GET' })
client.post = (path, body) =>
  request(path, { method: 'POST', body: body instanceof FormData ? body : JSON.stringify(body) })
client.put = (path, body) =>
  request(path, { method: 'PUT', body: body instanceof FormData ? body : JSON.stringify(body) })
client.del = (path) => request(path, { method: 'DELETE' })

// expose helpers too
client.api = request
client.getAdminToken = getAdminToken
client.setAdminToken = setAdminToken

export const api = request
export default client
