<template>
  <section class="page" data-module="dispatch">
    <header class="page-head">
      <div>
        <h2>防汛事件分拨台</h2>
        <p class="page-desc">
          水位告警、泵站排水离线、边坡偏离、道路巡查统一接入：按事件指纹合并重复告警，按版本重放升级链，
          按前置件展开处置树；升级结论由分拨器统一写入防汛台账、排水设施清单与养护工程汇总。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="toggleIngest">{{ showIngest ? '收起信号接入' : '登记上游信号' }}</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="showIngest" class="filter-bar" @submit.prevent="submitSignal">
      <label class="filter-item">
        <span>信号来源</span>
        <select v-model="signalForm.source">
          <option v-for="src in sources" :key="src.code" :value="src.code">{{ src.label }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>对象编号</span>
        <input v-model="signalForm.ref" :placeholder="refPlaceholder" />
      </label>
      <label class="filter-item">
        <span>位置 / 路段</span>
        <input v-model="signalForm.location" placeholder="如：滨江南路低洼段" />
      </label>
      <label class="filter-item">
        <span>{{ metricLabel }}</span>
        <input v-model="signalForm.measure" :placeholder="measurePlaceholder" />
      </label>
      <label class="filter-item">
        <span>告警类型</span>
        <input v-model="signalForm.kind" :placeholder="kindPlaceholder" />
      </label>
      <label class="filter-item">
        <span>申报级别（可空，自动定级）</span>
        <select v-model="signalForm.level">
          <option value="">自动定级</option>
          <option v-for="lv in levels" :key="lv" :value="lv">{{ lv }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>信号版本</span>
        <input v-model.number="signalForm.version" type="number" min="1" />
      </label>
      <button class="btn primary" type="submit">统一写法接入</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>事件编号</th>
          <th>来源</th>
          <th>事件类型 / 位置</th>
          <th>已确认级别</th>
          <th>升级结论</th>
          <th>现场管制（指挥部最新命令）</th>
          <th>处置进度</th>
          <th>告警次数</th>
          <th>事件版本</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="event in events" :key="event.id" :class="{ active: selectedId === event.id }">
          <td>{{ event.编号 }}</td>
          <td>{{ event.来源 }}</td>
          <td>{{ event.incident_type }}<br /><small>{{ event.location }}</small></td>
          <td>{{ event.confirmed_level }}</td>
          <td><strong>{{ event.升级结论 }}</strong></td>
          <td>{{ event.管制结论 }}</td>
          <td>{{ event.tasks.length - event.待处置节点 }}/{{ event.tasks.length }}</td>
          <td>{{ event.occurrences }}</td>
          <td>v{{ event.version }}</td>
          <td>{{ event.status }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="selectEvent(event.id)">处置树</button>
          </td>
        </tr>
        <tr v-if="!events.length">
          <td colspan="11" class="empty-state">暂无防汛事件，先在上方登记一条上游信号</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selected" class="detail-panel">
      <header class="detail-head">
        <h3>{{ selected.编号 }} · 处置树与升级链</h3>
        <span>指纹 {{ selected.fingerprint }} · 首次 {{ selected.first_seen_at }}</span>
      </header>

      <div class="detail-cols">
        <div>
          <h4>按前置件展开的处置树</h4>
          <table class="data-table">
            <thead>
              <tr><th>节点</th><th>前置件</th><th>状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="task in selected.tasks" :key="task.id">
                <td>{{ task.name }}</td>
                <td>{{ task.前置件 }}<br v-if="task.阻断原因" /><small class="error-text">{{ task.阻断原因 }}</small></td>
                <td>{{ task.status }}<small v-if="task.assignee"> · {{ task.assignee }}</small></td>
                <td class="row-actions">
                  <button
                    v-if="task.status === '待启动'"
                    class="link"
                    type="button"
                    :disabled="!task.可启动"
                    :title="task.阻断原因 || '启动该节点'"
                    @click="runTask(task.id, '启动')"
                  >启动</button>
                  <button
                    v-if="task.status === '进行中'"
                    class="link"
                    type="button"
                    @click="runTask(task.id, '完成')"
                  >完成</button>
                </td>
              </tr>
            </tbody>
          </table>

          <div class="command-bar">
            <h4>指挥部命令（与预警升级冲突时以最新命令为准）</h4>
            <label class="filter-item">
              <span>命令级别</span>
              <select v-model="command.level">
                <option v-for="lv in levels" :key="lv" :value="lv">{{ lv }}</option>
              </select>
            </label>
            <label class="filter-item">
              <span>现场管制动作</span>
              <input v-model="command.control" placeholder="封闭交通 / 限速通行 / 解除管制" />
            </label>
            <button class="btn primary" type="button" @click="submitCommand">
              下达命令（带版本 v{{ selected.version }}）
            </button>
            <button class="btn" type="button" @click="replayOld">
              重放旧低级信号
            </button>
            <button
              class="btn"
              type="button"
              :disabled="selected.待处置节点 > 0 || selected.status !== '进行中'"
              :title="selected.待处置节点 > 0 ? '处置树尚有节点未完成' : ''"
              @click="closeEvent"
            >结束响应</button>
          </div>
        </div>

        <div>
          <h4>升级链（只升不降，历史事件定格）</h4>
          <ol class="chain-list">
            <li v-for="item in selected.chain" :key="item.seq" :class="`chain-${item.kind}`">
              <strong>{{ item.level }} · {{ item.action }}</strong>
              <p>{{ item.basis }}</p>
              <small>{{ item.at }}</small>
            </li>
          </ol>
          <h4>合并的信号（{{ selected.signals.length }}）</h4>
          <ul class="signal-list">
            <li v-for="s in selected.signals" :key="s.seq">
              <span>{{ s.source_label }} {{ s.source_ref || s.location }} · {{ s.level }} · v{{ s.version }}</span>
              <em v-if="s.replay" class="tag replay">乱序重放</em>
              <em v-else-if="s.merged" class="tag merged">指纹合并</em>
              <small>{{ s.occurred_at }}{{ s.measure ? ' · ' + s.measure + s.metric : '' }}</small>
            </li>
          </ul>
        </div>
      </div>
    </section>

    <footer class="page-foot">
      <span v-if="notice" :class="notice.ok ? '' : 'error-text'">{{ notice.text }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type EventDetail = Record<string, any>
type Ack = {
  ok: boolean
  message: string
  event?: EventDetail
  merged?: boolean
  replay?: boolean
  downgraded?: boolean
  before_level?: string
  after_level?: string
}

const levels = ['蓝色', '黄色', '橙色', '红色']
const sources = [
  { code: 'water_level', label: '水位告警', ref: '水位计编号', metric: '当前水位(m)', kind: '阈值类型', kindHint: '超警戒水位 / 超保证水位 / 漫溢' },
  { code: 'pump_offline', label: '泵站排水离线', ref: '泵站编号', metric: '离线时长(分钟)', kind: '告警类型', kindHint: '可留空' },
  { code: 'slope_deviation', label: '边坡偏离', ref: '边坡编号', metric: '位移量(mm)', kind: '告警类型', kindHint: '可留空，按位移量自动定级' },
  { code: 'road_patrol', label: '道路巡查', ref: '巡查编号', metric: '积水深度(cm)', kind: '险情类型', kindHint: '道路积水险情 / 道路塌方' },
]

const events = ref<EventDetail[]>([])
const selectedId = ref<number | null>(null)
const showIngest = ref(false)
const notice = ref<{ ok: boolean; text: string } | null>(null)

const signalForm = reactive({
  source: 'water_level',
  ref: '',
  location: '',
  measure: '',
  kind: '',
  level: '',
  version: 1,
})
const command = reactive({ level: '红色', control: '' })

const sourceMeta = computed(() => sources.find((item) => item.code === signalForm.source)!)
const refPlaceholder = computed(() => sourceMeta.value.ref)
const metricLabel = computed(() => sourceMeta.value.metric)
const measurePlaceholder = computed(() => '按' + sourceMeta.value.metric + '填写')
const kindPlaceholder = computed(() => sourceMeta.value.kindHint)

const stats = computed(() => [
  { label: '进行中事件', value: events.value.filter((e) => e.status === '进行中').length },
  { label: '橙/红事件', value: events.value.filter((e) => ['橙色', '红色'].includes(e.升级结论)).length },
  { label: '待处置节点', value: events.value.reduce((sum, e) => sum + (e.待处置节点 ?? 0), 0) },
  { label: '已结束事件', value: events.value.filter((e) => e.status === '已结束').length },
])

const selected = computed(() => events.value.find((e) => e.id === selectedId.value) ?? null)

async function reload(keepSelected = true) {
  notice.value = null
  const response = await request('/api/dispatch/events')
  if (!response.ok) throw new Error('事件分拨列表读取失败')
  const payload = await response.json()
  events.value = payload.items ?? []
  if (keepSelected && selectedId.value) {
    if (!events.value.some((e) => e.id === selectedId.value)) selectedId.value = null
  }
}

async function post(path: string, body: unknown): Promise<Ack> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const data = (await response.json().catch(() => ({}))) as Ack & { detail?: string }
  if (!response.ok) {
    throw new Error(data.detail || data.message || '分拨操作未生效')
  }
  return data
}

function fieldNames() {
  const meta = sourceMeta.value
  const values: Record<string, string | number> = { source_version: signalForm.version }
  if (signalForm.ref) values[meta.ref] = signalForm.ref
  if (signalForm.location) {
    values[meta.code === 'pump_offline' || meta.code === 'slope_deviation' ? '所属路段' : '影响路段'] = signalForm.location
  }
  if (signalForm.measure) {
    const measureField = meta.code === 'water_level' ? '水位'
      : meta.code === 'pump_offline' ? '离线时长'
      : meta.code === 'slope_deviation' ? '偏离量'
      : '积水深度'
    values[measureField] = signalForm.measure
  }
  if (signalForm.kind) values[meta.kind] = signalForm.kind
  if (signalForm.level) values.预警级别 = signalForm.level
  return values
}

async function submitSignal() {
  try {
    const meta = sourceMeta.value
    const ack = await post(`/api/dispatch/signals/${meta.code.split('_').join('-')}`, { values: fieldNames() })
    notice.value = { ok: true, text: ack.message }
    await reload()
    if (ack.event) selectedId.value = ack.event.id
  } catch (error) {
    notice.value = { ok: false, text: error instanceof Error ? error.message : '信号接入失败' }
  }
}

async function submitCommand() {
  if (!selected.value) return
  try {
    const ack = await post(`/api/dispatch/events/${selected.value.id}/commands`, {
      级别: command.level,
      管制动作: command.control,
      expected_version: selected.value.version,
    })
    notice.value = { ok: true, text: ack.message }
    await reload()
  } catch (error) {
    notice.value = { ok: false, text: error instanceof Error ? error.message : '命令下达失败' }
  }
}

async function runTask(taskId: number, action: string) {
  if (!selected.value) return
  try {
    const ack = await post(
      `/api/dispatch/events/${selected.value.id}/tasks/${taskId}/actions`,
      { values: { action } },
    )
    notice.value = { ok: true, text: ack.message }
    await reload()
  } catch (error) {
    notice.value = { ok: false, text: error instanceof Error ? error.message : '处置节点操作失败' }
  }
}

async function replayOld() {
  if (!selected.value) return
  try {
    const ack = await post(`/api/dispatch/events/${selected.value.id}/replay`, {})
    notice.value = {
      ok: !ack.downgraded,
      text: `${ack.message}（级别 ${ack.before_level} → ${ack.after_level}）`,
    }
    await reload()
  } catch (error) {
    notice.value = { ok: false, text: error instanceof Error ? error.message : '重放失败' }
  }
}

async function closeEvent() {
  if (!selected.value) return
  try {
    const ack = await post(`/api/dispatch/events/${selected.value.id}/close`, {})
    notice.value = { ok: true, text: ack.message }
    await reload()
  } catch (error) {
    notice.value = { ok: false, text: error instanceof Error ? error.message : '结束响应失败' }
  }
}

function selectEvent(id: number) {
  selectedId.value = id
  notice.value = null
}

function toggleIngest() {
  showIngest.value = !showIngest.value
}

onMounted(() => {
  void reload(false)
})
</script>

<style scoped>
.detail-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; }
.detail-head { display: flex; justify-content: space-between; align-items: baseline; color: var(--muted); }
.detail-head h3 { margin: 4px 0; color: #1f2937; }
.detail-cols { display: grid; grid-template-columns: 1.2fr 1fr; gap: 20px; }
tr.active { background: #eef4ff; }
.command-bar { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.command-bar h4 { flex-basis: 100%; margin: 8px 0 0; }
.chain-list { list-style: none; padding: 0; margin: 0; max-height: 320px; overflow: auto; }
.chain-list li { border-left: 3px solid var(--border); padding: 6px 10px; margin-bottom: 6px; background: #f8fafc; }
.chain-list li.chain-signal { border-color: #d97706; }
.chain-list li.chain-command { border-color: #b42318; }
.chain-list li.chain-replay { border-color: #94a3b8; opacity: 0.75; }
.chain-list p { margin: 2px 0; font-size: 12px; color: var(--muted); }
.signal-list { list-style: none; padding: 0; margin: 0; }
.signal-list li { font-size: 12px; padding: 4px 0; border-bottom: 1px dashed var(--border); }
.tag { font-style: normal; font-size: 11px; border-radius: 4px; padding: 0 6px; margin: 0 6px; }
.tag.merged { background: #fef3c7; color: #92400e; }
.tag.replay { background: #e2e8f0; color: #475569; }
button:disabled { color: #94a3b8; cursor: not-allowed; }
select { padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px; }
</style>
