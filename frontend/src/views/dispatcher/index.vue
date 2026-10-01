<template>
  <section class="page" data-module="dispatcher">
    <header class="page-head">
      <div>
        <h2>事件分拨中心</h2>
        <p class="page-desc">
          水位告警、泵站排水离线、边坡偏离、道路巡查统一接入，按事件指纹合并成事件，
          依前置件展开处置树；升级结论由分拨器统一裁决并同步防汛台账、排水设施清单与养护工程汇总。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="submitAlarm">
      <label class="filter-item">
        <span>告警来源</span>
        <select v-model="alarmForm.source_type" @change="resetAlarmValues">
          <option v-for="source in sources" :key="source.source_type" :value="source.source_type">
            {{ source.label }}
          </option>
        </select>
      </label>
      <label v-for="field in activeSource?.required ?? []" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="alarmForm.values[field]" :placeholder="`填写${field}`" />
      </label>
      <label class="filter-item">
        <span>告警编号（可选）</span>
        <input v-model="alarmForm.alarm_id" placeholder="缺省按设施+时间生成" />
      </label>
      <label class="filter-item">
        <span>发生时间（可选）</span>
        <input v-model="alarmForm.occurred_at" type="datetime-local" />
      </label>
      <label class="filter-item">
        <span>建议级别（可选）</span>
        <select v-model="alarmForm.suggested">
          <option value="">按规则定级</option>
          <option v-for="level in levels" :key="level" :value="level">{{ level }}</option>
        </select>
      </label>
      <button class="btn primary" type="submit">接入告警</button>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>事件编号</span>
        <input v-model="filters.keyword" placeholder="按事件编号检索" />
      </label>
      <label class="filter-item">
        <span>生效级别</span>
        <select v-model="filters.level">
          <option value="">全部级别</option>
          <option v-for="level in levels" :key="level" :value="level">{{ level }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>事件状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option value="处置中">处置中</option>
          <option value="已结束">已结束</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['事件编号'] }}</td>
          <td>{{ row['来源'] }}</td>
          <td>{{ row['路段'] }}</td>
          <td>{{ row['类别'] }}</td>
          <td><span class="level-badge" :data-level="row.level">{{ row.level }}</span></td>
          <td><span class="level-badge" :data-level="row.confirmed_level">{{ row.confirmed_level }}</span></td>
          <td>{{ row.version }}</td>
          <td>{{ row.alarm_count }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(Number(row.id))">处置树</button>
            <button v-if="row.status === '处置中'" class="link" type="button" @click="escalate(Number(row.id))">请求升级</button>
            <button v-if="row.status === '处置中'" class="link" type="button" @click="closeEvent(Number(row.id))">结束事件</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无分拨事件，可在上方接入告警</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <h3>{{ detail['事件编号'] }} · {{ detail['来源'] }} · {{ detail['路段'] }}</h3>
        <button class="btn ghost" type="button" @click="detail = null">收起</button>
      </header>
      <div class="detail-grid">
        <div>
          <h4>处置树（按前置件展开）</h4>
          <ol class="task-list">
            <li v-for="node in detail.disposal_tree" :key="node.key" class="task-node" :data-state="node.state">
              <span class="task-title">{{ node.title }}</span>
              <span v-if="node.requires_titles.length" class="task-requires">
                前置件：{{ node.requires_titles.join('、') }}
              </span>
              <span class="task-state">{{ node.state }}</span>
              <button
                v-if="node.state === '可执行' && detail.status === '处置中'"
                class="link"
                type="button"
                @click="completeTask(node.key)"
              >
                完成
              </button>
            </li>
          </ol>
        </div>
        <div>
          <h4>升级链</h4>
          <ul class="chain-list">
            <li v-for="link in detail.escalation_chain" :key="link.version">
              v{{ link.version }} · {{ link.from }} → {{ link.to }} · {{ link.basis }} · {{ link.reason }}
            </li>
          </ul>
          <h4>台账同步记录</h4>
          <ul class="chain-list">
            <li v-for="log in detail.sync_log" :key="log.version">
              v{{ log.version }} · {{ log.level }} · 防汛[{{ log.flood.join('、') || '无匹配' }}]
              排水[{{ log.drainage.join('、') || '无匹配' }}] 工程[{{ log.project.join('、') || '无匹配' }}]
            </li>
          </ul>
        </div>
      </div>
    </section>

    <section class="detail-panel">
      <header class="detail-head">
        <h3>指挥部命令</h3>
      </header>
      <form class="filter-bar" @submit.prevent="submitCommand">
        <label class="filter-item">
          <span>命令文号</span>
          <input v-model="commandForm['命令文号']" placeholder="如 ZD-2026-001" />
        </label>
        <label class="filter-item">
          <span>路段</span>
          <input v-model="commandForm['路段']" placeholder="与事件路段一致" />
        </label>
        <label class="filter-item">
          <span>类型</span>
          <select v-model="commandForm['类型']">
            <option v-for="type in commandTypes" :key="type" :value="type">{{ type }}</option>
          </select>
        </label>
        <label v-if="commandForm['类型'] !== '解除管制'" class="filter-item">
          <span>管控级别</span>
          <select v-model="commandForm['管控级别']">
            <option v-for="level in levels" :key="level" :value="level">{{ level }}</option>
          </select>
        </label>
        <button class="btn primary" type="submit">签发命令</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>命令文号</th><th>路段</th><th>类型</th><th>管控级别</th><th>命令序号</th><th>影响事件</th><th>签发时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="command in commands" :key="String(command.id)">
            <td>{{ command['命令文号'] }}</td>
            <td>{{ command['路段'] }}</td>
            <td>{{ command['类型'] }}</td>
            <td>{{ command['管控级别'] ?? '—' }}</td>
            <td>{{ command['命令序号'] }}</td>
            <td>{{ (command.affected_events ?? []).join('、') || '—' }}</td>
            <td>{{ command.issued_at }}</td>
          </tr>
          <tr v-if="!commands.length">
            <td colspan="7" class="empty-state">暂无指挥部命令</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条分拨事件</span>
      <span v-if="infoMessage" class="info-text">{{ infoMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface SourceSpec {
  source_type: string
  label: string
  category: string
  required: string[]
  road_field: string
  facility_field: string
}

interface TreeNode {
  key: string
  title: string
  requires: string[]
  requires_titles: string[]
  state: string
  unmet: string[]
  completed: boolean
}

interface ChainLink {
  version: number
  from: string
  to: string
  basis: string
  reason: string
  at: string
}

interface SyncLog {
  version: number
  level: string
  flood: string[]
  drainage: string[]
  project: string[]
}

interface EventDetail {
  id: number
  status: string
  disposal_tree: TreeNode[]
  escalation_chain: ChainLink[]
  sync_log: SyncLog[]
  [key: string]: any
}

interface CommandRow {
  id: number
  affected_events?: string[]
  [key: string]: any
}

const ENDPOINT = '/api/dispatcher'
const columns = ['事件编号', '来源', '路段', '类别', '生效级别', '已确认级别', '版本', '告警次数', '状态']

const levels = ref<string[]>([])
const commandTypes = ref<string[]>([])
const sources = ref<SourceSpec[]>([])
const rows = ref<Row[]>([])
const commands = ref<CommandRow[]>([])
const total = ref(0)
const detail = ref<EventDetail | null>(null)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({ keyword: '', level: '', status: '' })

const alarmForm = ref({
  source_type: 'water_level',
  values: {} as Record<string, string>,
  alarm_id: '',
  occurred_at: '',
  suggested: '',
})
const commandForm = ref<Record<string, string>>({ 命令文号: '', 路段: '', 类型: '现场管制', 管控级别: '黄色' })

const activeSource = computed(() => sources.value.find((item) => item.source_type === alarmForm.value.source_type))

const stats = computed(() => {
  const open = rows.value.filter((row) => row.status === '处置中')
  const top = levels.value.reduce((acc, level) => (rows.value.some((row) => row.level === level) ? level : acc), '—')
  const alarms = rows.value.reduce((sum, row) => sum + Number(row.alarm_count ?? 0), 0)
  return [
    { label: '处置中事件', value: open.length },
    { label: '最高生效级别', value: top },
    { label: '累计接入告警', value: alarms },
    { label: '指挥部命令', value: commands.value.length },
  ]
})

function resetAlarmValues() {
  alarmForm.value.values = {}
}

function resetFilters() {
  filters.value = { keyword: '', level: '', status: '' }
  void reload()
}

async function run(path: string, init?: RequestInit): Promise<Record<string, unknown>> {
  const response = await request(path, init)
  const payload = (await response.json()) as Record<string, unknown>
  if (!response.ok) {
    throw new Error(String(payload.detail ?? '分拨器接口返回异常'))
  }
  return payload
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(
    Object.fromEntries(Object.entries(filters.value).filter(([, value]) => value)),
  ).toString()
  try {
    const payload = await run(`${ENDPOINT}/events?${query}`)
    rows.value = (payload.items as Row[]) ?? []
    total.value = Number(payload.total ?? rows.value.length)
    const commandPayload = await run(`${ENDPOINT}/commands`)
    commands.value = (commandPayload.items as unknown as CommandRow[]) ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '事件列表读取失败'
  }
}

async function submitAlarm() {
  errorMessage.value = ''
  infoMessage.value = ''
  const values: Record<string, string> = { ...alarmForm.value.values }
  if (alarmForm.value.alarm_id) values['告警编号'] = alarmForm.value.alarm_id
  if (alarmForm.value.occurred_at) {
    values['发生时间'] = alarmForm.value.occurred_at.length === 16
      ? `${alarmForm.value.occurred_at}:00`
      : alarmForm.value.occurred_at
  }
  if (alarmForm.value.suggested) values['建议级别'] = alarmForm.value.suggested
  try {
    const payload = await run(`${ENDPOINT}/alarms`, {
      method: 'POST',
      body: JSON.stringify({ source_type: alarmForm.value.source_type, values }),
    })
    infoMessage.value = String(payload.message ?? '')
    if (payload.ok) {
      alarmForm.value.values = {}
      alarmForm.value.alarm_id = ''
      await reload()
    } else {
      errorMessage.value = infoMessage.value
      infoMessage.value = ''
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警接入失败'
  }
}

async function submitCommand() {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const payload = await run(`${ENDPOINT}/commands`, {
      method: 'POST',
      body: JSON.stringify({ values: commandForm.value }),
    })
    infoMessage.value = String(payload.message ?? '')
    if (payload.ok) {
      commandForm.value['命令文号'] = ''
      await reload()
      if (detail.value) await openDetail(Number(detail.value.id))
    } else {
      errorMessage.value = infoMessage.value
      infoMessage.value = ''
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '命令签发失败'
  }
}

async function openDetail(eventId: number) {
  errorMessage.value = ''
  try {
    detail.value = (await run(`${ENDPOINT}/events/${eventId}`)) as unknown as EventDetail
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '事件明细读取失败'
  }
}

async function escalate(eventId: number) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const payload = await run(`${ENDPOINT}/events/${eventId}/escalate`, {
      method: 'POST',
      body: JSON.stringify({ values: { reason: '人工请求升级' } }),
    })
    infoMessage.value = String(payload.message ?? '')
    if (!payload.ok) errorMessage.value = infoMessage.value
    await reload()
    if (detail.value && Number(detail.value.id) === eventId) await openDetail(eventId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '升级请求失败'
  }
}

async function closeEvent(eventId: number) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const payload = await run(`${ENDPOINT}/events/${eventId}/close`, { method: 'POST' })
    infoMessage.value = String(payload.message ?? '')
    await reload()
    if (detail.value && Number(detail.value.id) === eventId) await openDetail(eventId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结束事件失败'
  }
}

async function completeTask(taskKey: string) {
  if (!detail.value) return
  errorMessage.value = ''
  infoMessage.value = ''
  const eventId = Number(detail.value.id)
  try {
    const payload = await run(`${ENDPOINT}/events/${eventId}/tasks/${taskKey}`, { method: 'POST' })
    infoMessage.value = String(payload.message ?? '')
    if (!payload.ok) errorMessage.value = infoMessage.value
    await openDetail(eventId)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '节点推进失败'
  }
}

onMounted(async () => {
  try {
    const meta = await run(`${ENDPOINT}/sources`)
    levels.value = (meta.levels as string[]) ?? []
    commandTypes.value = (meta.command_types as string[]) ?? []
    sources.value = (meta.sources as SourceSpec[]) ?? []
    if (sources.value.length) alarmForm.value.source_type = sources.value[0].source_type
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '来源元数据读取失败'
  }
  await reload()
})
</script>

<style scoped>
.detail-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-top: 12px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.detail-head h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.detail-grid h4 {
  margin: 8px 0;
  font-size: 13px;
  color: var(--muted);
}
.task-list {
  margin: 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.task-node {
  font-size: 13px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.task-node[data-state='已完成'] .task-title {
  color: var(--muted);
  text-decoration: line-through;
}
.task-requires {
  color: var(--muted);
  font-size: 12px;
}
.task-state {
  font-size: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0 8px;
}
.task-node[data-state='可执行'] .task-state {
  border-color: var(--brand);
  color: var(--brand);
}
.chain-list {
  margin: 0 0 8px;
  padding-left: 18px;
  font-size: 12px;
  color: #374151;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.level-badge {
  display: inline-block;
  min-width: 32px;
  text-align: center;
  border-radius: 10px;
  padding: 1px 8px;
  font-size: 12px;
  color: #fff;
  background: #94a3b8;
}
.level-badge[data-level='蓝色'] { background: #1f6feb; }
.level-badge[data-level='黄色'] { background: #b45309; }
.level-badge[data-level='橙色'] { background: #ea580c; }
.level-badge[data-level='红色'] { background: #b42318; }
.info-text {
  color: #027a48;
}
select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 6px;
  background: #fff;
}
</style>
