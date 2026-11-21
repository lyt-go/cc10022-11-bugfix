<template>
  <section class="page" data-module="address_dispatch">
    <header class="page-head">
      <div>
        <h2>客服地址改派</h2>
        <p class="page-desc">
          按黄金ID读取已生效的街道/区县组合。只有街道与区县来自同一个已发布版本时才可用于改派。
        </p>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="loadCurrent">
      <label class="filter-item">
        <span>黄金ID</span>
        <input v-model="goldenId" data-testid="address-golden-id" placeholder="如 GR-CUST-0001" />
      </label>
      <button class="btn primary" data-testid="address-query" type="submit">读取地址</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="address">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">街道</span>
          <strong class="stat-value">{{ address.街道 || '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">区县</span>
          <strong class="stat-value">{{ address.区县 || '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前版本</span>
          <strong class="stat-value">{{ address.current_version || '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可改派</span>
          <strong class="stat-value" :data-testid="`address-ready-${address.ready_for_dispatch}`">
            {{ address.ready_for_dispatch ? '是' : '否' }}
          </strong>
        </article>
      </div>

      <p v-if="!address.ready_for_dispatch" class="error-text" data-testid="address-incomplete">
        地址不完整，缺少：{{ address.missing_roles.join('、') }}，暂不可用于改派。
      </p>
      <p v-else class="page-desc" data-testid="address-ready">
        整套地址来自版本 {{ address.version_evidence?.版本号 ?? address.current_version }}，
        可直接用于客服改派。
      </p>
    </template>

    <section v-if="versions.length" class="timeline-wrap" data-testid="address-versions">
      <h3>版本字段组合</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>版本号</th>
            <th>状态</th>
            <th>街道</th>
            <th>区县</th>
            <th>地址完整</th>
            <th>字段证据</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="version in versions"
            :key="String(version.版本编号)"
            :data-testid="`address-version-${version.版本号}`"
          >
            <td>{{ version.版本号 }}</td>
            <td>{{ version.status }}</td>
            <td>{{ version.字段快照?.['街道'] ?? '—' }}</td>
            <td>{{ version.字段快照?.['区县'] ?? '—' }}</td>
            <td>{{ version.地址完整 ? '是' : '否' }}</td>
            <td>{{ version.字段证据完整 ? version.字段证据来源 : '证据不齐（仅可查询）' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  loadDispatchAddress,
  loadVersionHistory,
  type DispatchAddress,
  type VersionRow,
} from '@/api/backend-service'

const route = useRoute()
const goldenId = ref(String(route.query.golden_id ?? 'GR-CUST-0001'))
const address = ref<DispatchAddress | null>(null)
const versions = ref<VersionRow[]>([])
const errorMessage = ref('')

async function loadCurrent() {
  errorMessage.value = ''
  try {
    address.value = await loadDispatchAddress(goldenId.value.trim())
    versions.value = await loadVersionHistory(goldenId.value.trim())
  } catch (error) {
    address.value = null
    versions.value = []
    errorMessage.value = error instanceof Error ? error.message : '地址读取失败'
  }
}

onMounted(loadCurrent)
</script>
