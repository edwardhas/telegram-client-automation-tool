<template>
  <div class="grid" style="gap: 18px;">
    <div class="card">
      <div class="row" style="justify-content: space-between;">
        <div>
          <h2 style="margin: 0 0 4px;">Chats</h2>
          <div class="muted" style="font-size: 13px;">These are auto-discovered by the worker (dialogs + incoming messages). Use them for explicit targeting.</div>
        </div>
        <button class="btn secondary" @click="load">Refresh</button>
      </div>
    </div>

    <div v-if="err" class="card" style="border-color:#7f1d1d;">
      <strong>Error</strong>
      <div class="muted" style="margin-top:6px;">{{ err }}</div>
    </div>

    <div v-if="items.length === 0" class="card">
      <div class="muted">No chats in DB yet. Make sure the worker is running and is a member of groups.</div>
    </div>

    <div class="card" v-for="c in items" :key="c.id">
      <div class="row" style="justify-content: space-between;">
        <div>
          <div class="row" style="gap: 10px; flex-wrap: wrap;">
            <strong>{{ c.title || '(no title)' }}</strong>
            <span class="pill">{{ c.isActive ? 'active' : 'inactive' }}</span>
          </div>
          <div class="muted" style="margin-top: 6px; font-size: 13px;">
            <div><strong>ID:</strong> {{ c.chatId }}</div>
            <div v-if="c.type"><strong>Type:</strong> {{ c.type }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { api } from '../api/client'

export default {
  data() {
    return { items: [], err: '' }
  },
  mounted() { this.load() },
  methods: {
    async load() {
      this.err = ''
      try {
        this.items = await api('/api/chats?active=true')
      } catch (e) {
        this.err = e.message || String(e)
      }
    }
  }
}
</script>
