<template>
  <div class="grid" style="gap: 18px;">
    <div class="card">
      <h2 style="margin: 0 0 10px;">Settings</h2>
      <div class="muted" style="font-size: 13px;">Set the admin token used to call the API (header <code>X-Admin-Token</code>).</div>
    </div>

    <div class="card">
      <label>Admin Token</label>
      <input v-model="token" placeholder="paste your ADMIN_TOKEN here" />
      <div class="row" style="justify-content:flex-end; margin-top: 12px;">
        <button class="btn secondary" @click="clear">Clear</button>
        <button class="btn" @click="save">Save</button>
      </div>
      <div v-if="ok" class="muted" style="margin-top: 10px;">{{ ok }}</div>
    </div>
  </div>
</template>

<script>
import { getAdminToken, setAdminToken } from '../api/client'

export default {
  data() {
    return {
      token: getAdminToken(),
      ok: ''
    }
  },
  methods: {
    save() {
      setAdminToken(this.token)
      this.ok = 'Saved.'
      setTimeout(() => (this.ok = ''), 1500)
    },
    clear() {
      this.token = ''
      setAdminToken('')
      this.ok = 'Cleared.'
      setTimeout(() => (this.ok = ''), 1500)
    }
  }
}
</script>
