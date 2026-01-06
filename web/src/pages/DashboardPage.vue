<template>
  <div class="page">
    <div class="head">
      <h1>Dashboard</h1>
      <div class="actions">
        <button class="btn" @click="refresh" :disabled="loading">Refresh</button>
      </div>
    </div>

    <p class="hint">Shows the newest scheduled messages in the database.</p>

    <div v-if="error" class="alert error">{{ error }}</div>

    <div class="grid">
      <div v-for="m in messages" :key="m.id" class="card">
        <div class="cardHead">
          <div>
            <div class="title">{{ m.title }}</div>
            <div class="meta">
              <span class="chip">{{ m.status }}</span>
              <span class="chip" v-if="m.enabled">enabled</span>
              <span class="chip" v-else>disabled</span>
            </div>
          </div>
          <div class="right">
            <button class="btn small" @click="runNow(m.id)">Run now</button>
          </div>
        </div>

        <div class="row">
          <div class="k">Schedule</div>
          <div class="v">
            <div>{{ scheduleLabel(m) }}</div>
            <div class="sub" v-if="m.scheduleType === 'cron'">
              cron: <code>{{ m.cron }}</code>
            </div>
            <div class="sub" v-if="m.endAt">ends: {{ fmtUtc(m.endAt) }}</div>
            <div class="sub">tz: {{ m.tz }}</div>
          </div>
        </div>

        <div class="row">
          <div class="k">Next run</div>
          <div class="v">{{ m.nextRunAt ? fmtUtc(m.nextRunAt) : '—' }}</div>
        </div>

        <div class="row">
          <div class="k">Targets</div>
          <div class="v">
            <span v-if="m.targets === 'all'">all chats</span>
            <span v-else>custom: {{ (m.targetChatIds || []).join(', ') }}</span>
          </div>
        </div>

        <div class="row">
          <div class="k">Text</div>
          <div class="v pre">{{ m.text }}</div>
        </div>

        <div class="row" v-if="m.images && m.images.length">
          <div class="k">Images</div>
          <div class="v">
            <div v-for="(u, idx) in m.images" :key="idx" class="imgLine">
              <a :href="u" target="_blank" rel="noreferrer">{{ u }}</a>
            </div>
          </div>
        </div>
      </div>

      <div v-if="loading" class="card">
        Loading...
      </div>
      <div v-if="!loading && !messages.length" class="card">
        No messages yet.
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import client from '../api/client'

const messages = ref([])
const loading = ref(false)
const error = ref('')

function fmtUtc(dt) {
  try {
    const d = new Date(dt)
    return d.toISOString().replace('T', ' ').replace('Z', ' UTC')
  } catch {
    return String(dt)
  }
}

function scheduleLabel(m) {
  if (m.scheduleType === 'once') {
    return `Once at ${m.runAt ? fmtUtc(m.runAt) : '—'}`
  }

  const cron = String(m.cron || '').trim()
  const parts = cron.split(/\s+/)
  if (parts.length !== 5) return 'Recurring'

  const [min, hour, dom, mon, dow] = parts

  // every N minutes
  if (min.startsWith('*/') && hour === '*' && dom === '*' && mon === '*' && dow === '*') {
    return `Every ${min.slice(2)} minutes`
  }

  // every N hours
  if (min === '0' && hour.startsWith('*/') && dom === '*' && mon === '*' && dow === '*') {
    return `Every ${hour.slice(2)} hours`
  }

  // daily
  if (dom === '*' && mon === '*' && dow === '*') {
    return `Daily at ${String(hour).padStart(2, '0')}:${String(min).padStart(2, '0')}`
  }

  // weekly
  if (dom === '*' && mon === '*' && dow !== '*') {
    return `Weekly on ${dow} at ${String(hour).padStart(2, '0')}:${String(min).padStart(2, '0')}`
  }

  // monthly
  if (dom !== '*' && mon === '*' && dow === '*') {
    return `Monthly on day ${dom} at ${String(hour).padStart(2, '0')}:${String(min).padStart(2, '0')}`
  }

  return 'Recurring'
}

async function refresh() {
  error.value = ''
  loading.value = true
  try {
    const res = await client.get('/api/messages?limit=100&skip=0')
    messages.value = res.data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

async function runNow(id) {
  error.value = ''
  try {
    await client.post(`/api/messages/${id}/run`)
    await refresh()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || 'Failed to run'
  }
}

onMounted(refresh)
</script>

<style scoped>
.page {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.actions {
  display: flex;
  gap: 8px;
}

.hint {
  color: #6b7280;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 14px;
  margin-top: 14px;
}

.card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 14px;
  color: black;
}

.cardHead {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 10px;
}

.title {
  font-weight: 800;
  font-size: 16px;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.chip {
  font-size: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  padding: 2px 8px;
  color: #374151;
}

.row {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 10px;
  margin-top: 10px;
}

.k {
  color: #6b7280;
}

.v {
  color: #111827;
}

.sub {
  color: #6b7280;
  font-size: 12px;
  margin-top: 4px;
}

.pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.imgLine {
  margin-top: 4px;
  overflow-wrap: anywhere;
}

.alert {
  padding: 10px;
  border-radius: 10px;
  margin-top: 10px;
}

.alert.error {
  border: 1px solid #fecaca;
  background: #fef2f2;
  color: #991b1b;
}

.btn {
  border: 1px solid #d1d5db;
  background: #fff;
  border-radius: 10px;
  padding: 8px 12px;
  cursor: pointer;
}

.btn.small {
  padding: 6px 10px;
  font-size: 13px;
}
</style>
