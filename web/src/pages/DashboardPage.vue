<template>
  <div class="page">
    <header class="header">
      <h1>Dashboard</h1>
      <div class="tabs">
        <button
          class="tab"
          :class="{ active: tab === 'scheduled' }"
          @click="setTab('scheduled')"
        >
          Scheduled
        </button>
        <button
          class="tab"
          :class="{ active: tab === 'history' }"
          @click="setTab('history')"
        >
          History
        </button>
      </div>
    </header>

    <div v-if="error" class="error">
      {{ error }}
    </div>

    <div v-if="loading" class="loading">Loading…</div>

    <div v-else>
      <!-- Scheduled -->
      <section v-if="tab === 'scheduled'">
        <div v-if="scheduled.length === 0" class="empty">
          No scheduled messages yet.
        </div>

        <div v-else class="list">
          <article v-for="m in scheduled" :key="m._id" class="card color-black">
            <div class="row">
              <div>
                <div class="title">{{ m.title }}</div>
                <div class="meta">
                  <span class="pill">{{ m.status || 'scheduled' }}</span>
                  <span class="meta-item">Next: <b>{{ fmtWhen(m.nextRunAt, m.tz) }}</b></span>
                  <span v-if="m.scheduleType === 'cron' && m.endAt" class="meta-item">
                    End: <b>{{ fmtWhen(m.endAt, m.tz) }}</b>
                  </span>
                </div>
              </div>

              <div class="actions">
                <button class="btn color-black" @click="runNow(m)" :disabled="busyId === m._id">Run now</button>
                <button class="btn" @click="toggleEnabled(m)" :disabled="busyId === m._id">
                  {{ m.enabled ? 'Disable' : 'Enable' }}
                </button>
                <button class="btn danger color-black" @click="deleteMsg(m)" :disabled="busyId === m.id">Delete</button>
              </div>
            </div>

            <div v-if="m.description" class="body" v-html="m.description"></div>

            <div v-if="(m.imageUrls || []).length" class="images">
              <a v-for="(u, idx) in m.imageUrls" :key="idx" :href="u" target="_blank" rel="noreferrer">
                {{ shortUrl(u) }}
              </a>
            </div>

            <div class="meta2">
              <div>
                Targets:
                <b v-if="m.targetsMode === 'all'">All chats</b>
                <b v-else>Specific chats</b>
                <span v-if="m.targetsMode !== 'all' && (m.targetChatIds || []).length" class="muted">
                  ({{ (m.targetChatIds || []).join(', ') }})
                </span>
              </div>
              <div class="muted">Created: {{ fmtWhen(m.createdAt, m.tz) }}</div>
            </div>
          </article>
        </div>
      </section>

      <!-- History -->
      <section v-else>
        <div class="toolbar">
          <label class="muted">
            Show:
            <select v-model="historyFilter" @change="load()">
              <option value="all">All</option>
              <option value="done">Done</option>
              <option value="ended">Ended</option>
              <option value="error">Error</option>
              <option value="no_targets">No targets</option>
            </select>
          </label>
        </div>

        <div v-if="history.length === 0" class="empty">
          No history yet.
        </div>

        <div v-else class="list">
          <article v-for="m in history" :key="m._id" class="card">
            <div class="row">
              <div>
                <div class="title">{{ m.title }}</div>
                <div class="meta">
                  <span class="pill">{{ m.status }}</span>
                  <span v-if="m.lastRunAt" class="meta-item">Last run: <b>{{ fmtWhen(m.lastRunAt, m.tz) }}</b></span>
                  <span v-else-if="m.nextRunAt" class="meta-item">Next: <b>{{ fmtWhen(m.nextRunAt, m.tz) }}</b></span>
                </div>
              </div>

              <div class="actions">
                <button class="btn" @click="openRerun(m)" :disabled="busyId === m._id">Run again</button>
                <button class="btn danger" @click="deleteMsg(m)" :disabled="busyId === m._id">Delete</button>
              </div>
            </div>

            <div v-if="m.status === 'error' && m.error" class="errorbox">
              {{ m.error }}
            </div>


            <div v-if="m.description" class="body" v-html="m.description"></div>

            <div v-if="(m.imageUrls || []).length" class="images">
              <a v-for="(u, idx) in m.imageUrls" :key="idx" :href="u" target="_blank" rel="noreferrer">
                {{ shortUrl(u) }}
              </a>
            </div>

            <div v-if="rerunOpenId === m.id" class="rerun">
              <div class="muted">Pick a time to run this message again:</div>
              <div class="rerun-row">
                <input type="datetime-local" v-model="rerunAt" />
                <button class="btn" @click="confirmRerun(m)" :disabled="busyId === m.id || !rerunAt">
                  Schedule
                </button>
                <button class="btn" @click="closeRerun()" :disabled="busyId === m.id">Cancel</button>
              </div>
            </div>

            <div class="meta2">
              <div class="muted">Created: {{ fmtWhen(m.createdAt, m.tz) }}</div>
            </div>
            <div class="meta2">
              <div class="muted">Message ID: {{ fmtWhen(m._id) }}</div>
            </div>
          </article>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api/client'

const tab = ref('scheduled')
const loading = ref(false)
const error = ref('')
const busyId = ref(null)

const messages = ref([])
const historyFilter = ref('all')

const rerunOpenId = ref(null)
const rerunAt = ref('')

const scheduled = computed(() => {
  return (messages.value || []).filter((m) => m.enabled)
})

const history = computed(() => {
  if (tab.value !== 'history') return []
  let list = (messages.value || []).filter((m) => !m.enabled || ['done', 'ended', 'error', 'no_targets', 'sent'].includes(m.status))
  if (historyFilter.value !== 'all') {
    list = list.filter((m) => m.status === historyFilter.value)
  }
  return list
})

function setTab(t) {
  tab.value = t
  // One list fetch is enough, but reload so filters apply fast if DB changed.
  load()
}

function shortUrl(u) {
  if (!u) return ''
  if (u.startsWith('http')) return u.replace(/^https?:\/\//, '').slice(0, 60) + (u.length > 60 ? '…' : '')
  // local path: show tail
  const parts = u.split('/')
  return parts.slice(-2).join('/')
}

function fmtWhen(dt, tz) {
  if (!dt) return '—'
  const d = new Date(dt)
  if (Number.isNaN(d.getTime())) return String(dt)

  const now = new Date()
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate()

  const opts = sameDay
    ? { hour: 'numeric', minute: '2-digit', timeZone: tz || 'America/Los_Angeles' }
    : { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: tz || 'America/Los_Angeles' }

  return new Intl.DateTimeFormat(undefined, opts).format(d)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // backend supports bucket=active/history/all. We want "all" so we can compute both tabs locally.
    messages.value = await api.get('/api/messages?limit=200&bucket=all')
    console.log(messages.value)
  } catch (e) {
    console.error(e)
    error.value = e?.message || 'Failed to load messages'
    messages.value = []
  } finally {
    loading.value = false
    console.log(loading.value)
  }
}

async function runNow(m) {
  busyId.value = m.id
  error.value = ''
  try {
    await api.post(`/api/messages/${m.id}/run`, {})
    await load()
  } catch (e) {
    console.error(e)
    error.value = e?.message || 'Failed to run message'
  } finally {
    busyId.value = null
  }
}

async function toggleEnabled(m) {
  busyId.value = m.id
  error.value = ''
  try {
    await api.patch(`/api/messages/${m.id}`, { enabled: !m.enabled })
    await load()
  } catch (e) {
    console.error(e)
    error.value = e?.message || 'Failed to update message'
  } finally {
    busyId.value = null
  }
}

async function deleteMsg(m) {
  if (!confirm('Delete this message?')) return
  busyId.value = m.id
  error.value = ''
  try {
    await api.del(`/api/messages/${m.id}`)
    await load()
  } catch (e) {
    console.error(e)
    error.value = e?.message || 'Failed to delete message'
  } finally {
    busyId.value = null
  }
}

function openRerun(m) {
  rerunOpenId.value = m.id
  // default = now + 10 minutes (local)
  const d = new Date(Date.now() + 10 * 60 * 1000)
  // datetime-local wants "YYYY-MM-DDTHH:MM"
  const pad = (n) => String(n).padStart(2, '0')
  rerunAt.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function closeRerun() {
  rerunOpenId.value = null
  rerunAt.value = ''
}

async function confirmRerun(m) {
  if (!rerunAt.value) return
  busyId.value = m.id
  error.value = ''
  try {
    await api.post(`/api/messages/${m.id}/clone`, {
      scheduleType: 'once',
      runAt: rerunAt.value,
      tz: m.tz || 'America/Los_Angeles',
      enabled: true,
    })
    closeRerun()
    await load()
    tab.value = 'scheduled'
  } catch (e) {
    console.error(e)
    error.value = e?.message || 'Failed to schedule rerun'
  } finally {
    busyId.value = null
  }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 16px; max-width: 1100px; margin: 0 auto; }
.header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.tabs { display: flex; gap: 8px; }
.tab { padding: 8px 10px; border: 1px solid #ddd; border-radius: 10px; background: #fff; cursor: pointer; }
.tab.active { border-color: #111; }
.loading { padding: 12px; }
.error { background: #fee; border: 1px solid #fbb; padding: 10px; border-radius: 10px; margin: 10px 0; color:black}
.errorbox { background: #fff5f5; border: 1px solid #fbb; padding: 10px; border-radius: 10px; margin-top: 10px; }
.empty { padding: 18px; border: 1px dashed #ddd; border-radius: 12px; color: #666; }
.list { display: grid; gap: 12px; }
.card { border: 1px solid #eee; border-radius: 14px; padding: 14px; background: #fff; color:black}
.row { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.title { font-weight: 700; font-size: 16px; }
.meta { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 6px; color: #444; font-size: 13px; }
.meta-item { white-space: nowrap; }
.pill { padding: 2px 8px; border-radius: 999px; background: #f5f5f5; border: 1px solid #e7e7e7; font-size: 12px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.btn { padding: 8px 10px; border: 1px solid #ddd; border-radius: 10px; background: #fff; cursor: pointer; color:black;}
.btn:disabled { opacity: 0.6; cursor: not-allowed; }
.btn.danger { border-color: #f2b4b4; }
.body { margin-top: 10px; white-space: pre-wrap; }
.images { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.images a { font-size: 13px; color: inherit; text-decoration: underline; }
.meta2 { margin-top: 10px; display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
.muted { color: #666; }
.toolbar { display: flex; justify-content: flex-end; margin-bottom: 10px; }
.rerun { margin-top: 12px; padding-top: 12px; border-top: 1px solid #eee; }
.rerun-row { display: flex; gap: 8px; align-items: center; margin-top: 8px; flex-wrap: wrap; }
</style>
