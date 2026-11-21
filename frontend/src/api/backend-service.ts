import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type DispatchAddress = {
  ok: boolean
  reason?: string
  golden_id: string
  entity_model?: string
  街道: string
  区县: string
  address: Record<string, string>
  address_complete: boolean
  ready_for_dispatch: boolean
  missing_roles: string[]
  current_version?: string
  version_id?: string
  version_evidence?: {
    版本编号: string
    版本号: string
    status: string
    字段证据来源: string | null
    字段证据完整: boolean
  } | null
  source_combination: Record<string, string>
}

export type VersionRow = EntryRow & {
  字段快照?: Record<string, string>
  字段证据完整?: boolean
  字段证据来源?: string | null
  回退字段数据?: Record<string, unknown> | null
}

export async function loadDispatchAddress(goldenId: string): Promise<DispatchAddress> {
  const response = await request(`/api/versions/${encodeURIComponent(goldenId)}/dispatch-address`)
  if (!response.ok) throw new Error('地址读取失败')
  return await response.json()
}

export async function loadVersionHistory(goldenId: string): Promise<VersionRow[]> {
  const response = await request(`/api/address/${encodeURIComponent(goldenId)}/versions`)
  if (!response.ok) throw new Error('版本历史读取失败')
  const payload = await response.json()
  return payload.versions as VersionRow[]
}

export async function rollbackVersion(goldenId: string, targetVersion: string): Promise<ActionResult> {
  const response = await request(`/api/versions/${encodeURIComponent(goldenId)}/rollback`, {
    method: 'POST',
    body: JSON.stringify({ target_version: targetVersion }),
  })
  const payload = await response.json()
  return { ok: Boolean(payload.ok), message: payload.ok ? '版本已回退并恢复字段' : String(payload.reason ?? '回退失败') }
}

export async function submitAddressCorrection(
  goldenId: string,
  fields: Record<string, string>,
  batch: string,
): Promise<ActionResult> {
  const response = await request(`/api/versions/${encodeURIComponent(goldenId)}/corrections`, {
    method: 'POST',
    body: JSON.stringify({ fields, batch }),
  })
  const payload = await response.json()
  return {
    ok: Boolean(payload.ok),
    message: payload.ok
      ? `已确认版本 ${payload.version?.版本号 ?? ''}${payload.address_complete ? '' : '（地址不完整，暂不可改派）'}`
      : String(payload.reason ?? '更正提交失败'),
  }
}
