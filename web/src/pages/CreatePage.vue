<template>
  <div class="page">
    <h1>Create Scheduled Message</h1>

    <div class="card" style="background: #404040">
      <div class="row">
        <label>Title</label>
        <input v-model.trim="form.title" placeholder="Campaign title" />
      </div>

      <div class="row">
        <label>Text (HTML allowed)</label>
        <textarea v-model="form.text" rows="6" placeholder="Message body"></textarea>
      </div>

      <div class="row">
        <label>Image URLs (one per line)</label>
        <textarea v-model="imagesRaw" rows="5" placeholder="https://...\nhttps://..."></textarea>
        <small class="hint">Local paths usually won't work</small>
      </div>

      <div class="row two">
        <div>
          <label>Parse mode</label>
          <select v-model="form.parseMode">
            <option value="HTML">HTML</option>
            <option value="Markdown">Markdown</option>
            <option value="MarkdownV2">MarkdownV2</option>
          </select>
        </div>
        <div class="checkbox">
          <label>
            <input type="checkbox" v-model="form.disablePreview" />
            Disable link preview
          </label>
        </div>
      </div>

      <hr />

      <div class="row two">
        <div>
          <label>Targets</label>
          <select v-model="form.targets">
            <option value="all">All chats</option>
            <option value="custom">Custom chat IDs</option>
          </select>
        </div>
        <div v-if="form.targets === 'custom'">
          <label>Chat IDs (comma separated)</label>
          <input v-model.trim="chatIdsRaw" placeholder="-100123, 3159..." />
        </div>
      </div>

      <hr />

      <div class="row two">
        <div>
          <label>Schedule type</label>
          <select v-model="form.scheduleType">
            <option value="once">Once</option>
            <option value="cron">Recurring</option>
          </select>
        </div>
        <div>
          <label>Timezone</label>
          <input v-model.trim="form.tz" placeholder="America/Los_Angeles" />
          <small class="hint">Don't change this.</small>
        </div>
      </div>

      <div v-if="form.scheduleType === 'once'" class="row">
        <label>Run at (local to timezone above)</label>
        <input type="datetime-local" v-model="runAtLocal" />
      </div>

      <div v-else class="card inner" style="background: #404040">
        <div class="row two">
          <div>
            <label>Repeat</label>
            <select v-model="builder.mode">
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
              <option value="everyMinutes">Every N minutes</option>
              <option value="everyHours">Every N hours</option>
            </select>
          </div>
          <div class="checkbox">
            <label>
              <input type="checkbox" v-model="builder.advanced" />
              Advanced (enter cron)
            </label>
          </div>
        </div>

        <div v-if="!builder.advanced">
          <div class="row two" v-if="builder.mode === 'daily' || builder.mode === 'weekly' || builder.mode === 'monthly'">
            <div>
              <label>Time</label>
              <input type="time" v-model="builder.time" />
            </div>

            <div v-if="builder.mode === 'monthly'">
              <label>Day of month</label>
              <input type="number" min="1" max="31" v-model.number="builder.dayOfMonth" />
            </div>
          </div>

          <div v-if="builder.mode === 'weekly'" class="row">
            <label>Days of week</label>
            <div class="dow">
              <label v-for="d in weekdays" :key="d.value" class="dowItem">
                <input type="checkbox" :value="d.value" v-model="builder.daysOfWeek" />
                {{ d.label }}
              </label>
            </div>
            <small class="hint">If you choose none, we'll default to every day.</small>
          </div>

          <div v-if="builder.mode === 'everyMinutes'" class="row two">
            <div>
              <label>Every</label>
              <input type="number" min="1" max="1440" v-model.number="builder.everyN" />
            </div>
            <div class="hintBox">Example: 15 = every 15 minutes</div>
          </div>

          <div v-if="builder.mode === 'everyHours'" class="row two">
            <div>
              <label>Every</label>
              <input type="number" min="1" max="168" v-model.number="builder.everyN" />
            </div>
            <div class="hintBox">Example: 6 = every 6 hours</div>
          </div>

          <div class="row">
            <label>Generated cron</label>
            <input :value="generatedCron" readonly />
          </div>
        </div>

        <div v-else class="row">
          <label>Cron (5-part)</label>
          <input v-model.trim="form.cron" placeholder="m h dom mon dow" />
          <small class="hint">Example: <code>0 9 * * 1-5</code> (weekdays at 9:00)</small>
        </div>

        <div class="row">
          <label>End at (optional)</label>
          <input type="datetime-local" v-model="endAtLocal" />
          <small class="hint">After this time, the schedule will automatically stop</small>
        </div>
      </div>

      <hr />

      <div class="row actions">
        <button :disabled="saving" @click="submit">
          {{ saving ? 'Saving...' : 'Create' }}
        </button>
        <span class="error" v-if="error">{{ error }}</span>
        <span class="ok" v-if="ok">Created ✔</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import client from '../api/client'

const saving = ref(false)
const error = ref('')
const ok = ref(false)

const form = reactive({
  title: '',
  text: '',
  images: [],
  parseMode: 'HTML',
  disablePreview: true,
  targets: 'all',
  targetChatIds: [],
  scheduleType: 'once',
  runAt: null,
  cron: '',
  endAt: null,
  tz: 'America/Los_Angeles',
})

const imagesRaw = ref('')
const chatIdsRaw = ref('')
const runAtLocal = ref('')
const endAtLocal = ref('')

const weekdays = [
  { label: 'Mon', value: 1 },
  { label: 'Tue', value: 2 },
  { label: 'Wed', value: 3 },
  { label: 'Thu', value: 4 },
  { label: 'Fri', value: 5 },
  { label: 'Sat', value: 6 },
  { label: 'Sun', value: 0 },
]

const builder = reactive({
  advanced: false,
  mode: 'daily',
  time: '09:00',
  daysOfWeek: [1, 2, 3, 4, 5],
  dayOfMonth: 1,
  everyN: 15,
})

const generatedCron = computed(() => {
  const [hh, mm] = (builder.time || '09:00').split(':').map(Number)
  const minute = Number.isFinite(mm) ? mm : 0
  const hour = Number.isFinite(hh) ? hh : 9

  if (builder.mode === 'daily') {
    return `${minute} ${hour} * * *`
  }

  if (builder.mode === 'weekly') {
    const dows = Array.isArray(builder.daysOfWeek) && builder.daysOfWeek.length
      ? builder.daysOfWeek.slice().sort((a, b) => a - b).join(',')
      : '*'
    return `${minute} ${hour} * * ${dows}`
  }

  if (builder.mode === 'monthly') {
    const dom = Math.min(31, Math.max(1, Number(builder.dayOfMonth || 1)))
    return `${minute} ${hour} ${dom} * *`
  }

  if (builder.mode === 'everyMinutes') {
    const n = Math.min(1440, Math.max(1, Number(builder.everyN || 1)))
    return `*/${n} * * * *`
  }

  if (builder.mode === 'everyHours') {
    const n = Math.min(168, Math.max(1, Number(builder.everyN || 1)))
    return `0 */${n} * * *`
  }

  return `${minute} ${hour} * * *`
})

watch(
  () => [form.scheduleType, builder.advanced, builder.mode, builder.time, builder.daysOfWeek, builder.dayOfMonth, builder.everyN],
  () => {
    if (form.scheduleType === 'cron' && !builder.advanced) {
      form.cron = generatedCron.value
    }
  },
  { deep: true }
)

function parseImages() {
  const lines = imagesRaw.value
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
  form.images = lines
}

function parseChatIds() {
  const ids = chatIdsRaw.value
    // allow comma-separated or newline-separated lists
    .split(/[\s,]+/)
    .map(s => s.trim())
    .filter(Boolean)
    .map(s => Number(s))
    .filter(n => Number.isFinite(n))
  form.targetChatIds = ids
}

async function submit() {
  error.value = ''
  ok.value = false

  parseImages()
  if (form.targets === 'custom') {
    parseChatIds()
    if (!form.targetChatIds.length) {
      error.value = 'Please provide at least one chat ID (comma or newline separated).'
      return
    }
  } else {
    form.targetChatIds = []
  }

  if (!form.title) {
    error.value = 'Title is required.'
    return
  }

  const payload = {
    title: form.title,
    // Backend uses "description" as the message body (HTML/Markdown/None)
    description: form.text,
    imageUrls: form.images,
    parseMode: form.parseMode,
    disablePreview: form.disablePreview,
    targetsMode: form.targets === 'custom' ? 'explicit' : 'all',
    targetChatIds: form.targets === 'custom' ? form.targetChatIds : [],
    scheduleType: form.scheduleType,
    tz: form.tz,
  }

  if (form.scheduleType === 'once') {
    if (!runAtLocal.value) {
      error.value = 'Please select a run time.'
      return
    }
    payload.runAt = runAtLocal.value
  } else {
    const cron = (builder.advanced ? form.cron : generatedCron.value).trim()
    if (!cron) {
      error.value = 'Cron is required for recurring schedules.'
      return
    }
    payload.cron = cron
    payload.endAt = endAtLocal.value ? endAtLocal.value : null
  }

  try {
    saving.value = true
    await client.post('/api/messages', payload)
    ok.value = true

    // reset a few fields
    form.title = ''
    form.text = ''
    imagesRaw.value = ''
    chatIdsRaw.value = ''
    runAtLocal.value = ''
    endAtLocal.value = ''
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || 'Failed to create'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.page {
  max-width: 980px;
  margin: 0 auto;
  padding: 24px;
}

.card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 16px;
}

.inner {
  margin-top: 12px;
  background: #fafafa;
}

.row {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}

.row.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

label {
  font-weight: 600;
}

input, textarea, select {
  width: 100%;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  padding: 10px;
  font-size: 14px;
}

.hint {
  color: #6b7280;
}

.hintBox {
  display: flex;
  align-items: flex-end;
  color: #6b7280;
  padding-bottom: 8px;
}

.dow {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.dowItem {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  background: white;
}

.checkbox {
  display: flex;
  align-items: flex-end;
}

.actions {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 12px;
}

button {
  background: #111827;
  color: white;
  border: none;
  border-radius: 10px;
  padding: 10px 14px;
  cursor: pointer;
}

button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.error {
  color: #b91c1c;
}

.ok {
  color: #047857;
}

hr {
  border: none;
  border-top: 1px solid #e5e7eb;
  margin: 16px 0;
}
</style>
