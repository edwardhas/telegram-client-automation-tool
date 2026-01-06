<template>
  <div>
    <div class="topbar">
      <div class="row">
        <strong>Telegram Automation</strong>
        <span class="pill muted">Admin</span>
      </div>
      <div class="nav">
        <RouterLink to="/">Dashboard</RouterLink>
        <RouterLink to="/create">Create</RouterLink>
        <RouterLink to="/chats">Chats</RouterLink>
        <RouterLink to="/campaigns">Campaigns</RouterLink>
        <RouterLink to="/settings">Settings</RouterLink>
      </div>
    </div>

    <div class="container">
      <RouterView />
    </div>
  </div>
</template>
