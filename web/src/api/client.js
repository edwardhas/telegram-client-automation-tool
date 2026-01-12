// web/src/api/client.js

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";
const TOKEN_KEY = "ADMIN_TOKEN";

export function getAdminToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setAdminToken(token) {
  const t = (token || "").trim();
  if (!t) localStorage.removeItem(TOKEN_KEY);
  else localStorage.setItem(TOKEN_KEY, t);
}

async function request(path, opts = {}) {
  const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
  const headers = {
    ...(opts.headers || {}),
  };

  // If sending JSON body, ensure Content-Type
  if (opts.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const token = getAdminToken();
  if (token) headers["X-Admin-Token"] = token;

  const res = await fetch(url, { ...opts, headers });

  // Try to parse JSON if possible
  const contentType = res.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const data = isJson ? await res.json().catch(() => null) : await res.text().catch(() => "");

  if (!res.ok) {
    const msg =
      (data && typeof data === "object" && (data.detail || data.message)) ||
      (typeof data === "string" && data) ||
      `Request failed: ${res.status}`;
    throw new Error(msg);
  }

  return data;
}

// api object
export const api = {
  get: (path) => request(path, { method: "GET" }),
  post: (path, body) => request(path, { method: "POST", body: JSON.stringify(body) }),
  put: (path, body) => request(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: (path, body) => request(path, { method: "PATCH", body: JSON.stringify(body) }),
  del: (path) => request(path, { method: "DELETE" }),
};

// default export for older imports: import api from '../api/client'
export default api;
