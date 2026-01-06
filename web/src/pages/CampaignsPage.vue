<template>
  <div class="grid" style="gap: 18px;">
    <div class="card">
      <div class="row" style="justify-content: space-between; align-items:flex-start;">
        <div>
          <h2 style="margin:0 0 4px;">Saved Campaigns</h2>
          <div class="muted" style="font-size: 13px;">Optional templates you can reuse. This MVP only stores them; scheduling-from-template can be added next.</div>
        </div>
        <button class="btn secondary" @click="load">Refresh</button>
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 10px;">Create Campaign</h3>
      <div class="grid two">
        <div>
          <label>Code (unique)</label>
          <input v-model="form.code" placeholder="e.g. winter-drop" />
        </div>
        <div>
          <label>Title</label>
          <input v-model="form.title" placeholder="e.g. Winter Drop" />
        </div>
      </div>
      <label>Description</label>
      <textarea v-model="form.description" rows="3"></textarea>
      <label>Image URLs (one per line)</label>
      <textarea v-model="imagesRaw" rows="2"></textarea>
      <div class="row" style="justify-content:flex-end; margin-top: 12px;">
        <button class="btn" @click="create">Save</button>
      </div>
      <div v-if="ok" class="muted" style="margin-top: 8px;">Saved: {{ ok }}</div>
      <div v-if="err" class="muted" style="margin-top: 8px; color:#fca5a5;">{{ err }}</div>
    </div>

    <div v-for="c in items" :key="c.id" class="card">
      <div class="row" style="justify-content: space-between; align-items:flex-start;">
        <div>
          <div class="row" style="gap:10px; flex-wrap: wrap;">
            <strong>{{ c.title }}</strong>
            <span class="pill">{{ c.code }}</span>
          </div>
          <div class="muted" style="margin-top:6px; white-space:pre-wrap;">{{ c.description }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { api } from '../api/client'

export default {
  data() {
    return {
      items: [],
      imagesRaw: '',
      err: '',
      ok: '',
      form: {
        code: '',
        title: '',
        description: ''
      }
    }
  },
  mounted() { this.load() },
  methods: {
    splitLines(v) {
      return (v || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean)
    },
    async load() {
      this.err = ''
      try {
        this.items = await api('/api/campaigns')
      } catch (e) {
        this.err = e.message || String(e)
      }
    },
    async create() {
      this.err = ''
      this.ok = ''
      try {
        const created = await api('/api/campaigns', {
          method: 'POST',
          body: JSON.stringify({
            code: this.form.code,
            title: this.form.title,
            description: this.form.description,
            imageUrls: this.splitLines(this.imagesRaw)
          })
        })
        this.ok = created.code
        await this.load()
      } catch (e) {
        this.err = e.message || String(e)
      }
    }
  }
}
</script>
