<template>
  <div :class="['analysis-shell', `theme-${themeMode}`]">
    <!-- 固定右上角配置按钮 -->
    <div class="page-actions" :class="{ 'is-analysis': started }">
      <div class="memory-toggle">
        <span class="memory-label">开启记忆</span>
        <el-switch v-model="enableMemory" @change="saveEnableMemory" />
      </div>
      <el-radio-group v-model="themeMode" size="small" class="theme-switch" aria-label="风格切换">
        <el-radio-button label="ink">水墨</el-radio-button>
        <el-radio-button label="classic">经典</el-radio-button>
      </el-radio-group>
      <el-button class="config-button" @click="configVisible=true">配置 Cookie</el-button>
    </div>

    <div v-if="!started" class="hero">
      <div class="hero-main">
        <div class="brand">小红书舆情分析</div>
        <p class="hero-sub">观小红书真实声量，辨产品口碑起伏，生成可追溯的舆情分析报告</p>
        <div class="hero-search">
          <el-input
            v-model="query"
            placeholder="输入内容进行分析，如: iPhone16质量怎么样、冰岛旅游体验好吗、Gemini限额了"
            size="large"
            clearable
            @keyup.enter="startAnalysis"
            class="hero-input"
          />
          <el-button
            type="primary"
            size="large"
            :disabled="!query.trim()"
            @click="startAnalysis"
            class="hero-btn"
          >开始分析</el-button>
        </div>
      </div>

      <div v-if="visibleHotspotGroups.length" class="hotspot-arc" :class="hotspotLayoutClass">
        <section
          v-for="(group, gi) in visibleHotspotGroups"
          :key="group.title || gi"
          class="hotspot-block"
        >
          <div class="hotspot-title">{{ group.title }}</div>
          <button
            v-for="(item, ii) in group.items"
            :key="item.title || ii"
            type="button"
            class="hotspot-item"
            :disabled="loading"
            @click="startFromHotspot(item)"
          >
            <span class="hotspot-index">{{ ii + 1 }}</span>
            <span class="hotspot-text">{{ item.title }}</span>
          </button>
        </section>
      </div>
    </div>

    <div v-else class="analysis-page">
      <div class="top-bar">
        <div class="top-bar-inner">
          <span class="brand-mini" @click="resetToHero" style="cursor:pointer">小红书舆情分析</span>
          <el-input
            v-model="query"
            size="default"
            clearable
            placeholder="输入内容关键词"
            @keyup.enter="startAnalysis"
            class="top-query-input"
          />
          <el-button type="primary" :loading="loading" :disabled="!query.trim()" @click="startAnalysis">
            {{ loading ? '分析中…' : '重新分析' }}
          </el-button>
          <el-button v-if="loading" @click="cancelAnalysis">取消</el-button>
        </div>
      </div>

      <div v-if="loading || stages.length > 0" class="progress-area">
        <div class="progress-inner">
          <template v-for="s in stages" :key="s.stage">
            <div class="stage-item">
              <el-icon v-if="s.done" color="#10B981"><CircleCheckFilled /></el-icon>
              <el-icon v-else-if="s.error" color="#EF4444"><CircleCloseFilled /></el-icon>
              <el-icon v-else class="is-loading" :color="themeMode === 'ink' ? '#111827' : '#1E3A8A'"><Loading /></el-icon>
              <span class="stage-msg">{{ s.message }}</span>
              <el-progress
                v-if="!s.done && s.stage === 'analyze'"
                :percentage="analyzeProgress"
                :striped="true"
                :striped-flow="true"
                :duration="8"
                class="stage-progress"
                :show-text="false"
              />
            </div>

            <!-- 将读取框当做 retrieve 阶段的子消息条，锁定在原本位置 -->
            <div v-if="s.stage === 'retrieve' && postReadingList.length > 0"
                 class="post-reading-box"
                 :class="{ 'is-collapsed': isReportGenerating && !isPostReadingExpanded }"
                 @click="togglePostReading">
              <div v-if="isReportGenerating && !isPostReadingExpanded" class="post-reading-summary">
                已读取 {{ postReadingList.length }} 篇帖子详细内容 ↓
              </div>
              <div v-else class="post-reading-content">
                <div v-for="(item, i) in postReadingList" :key="i" class="post-reading-item">{{ item }}</div>
                <div v-if="isReportGenerating" class="collapse-btn" @click.stop="isPostReadingExpanded = false">收起详细记录</div>
              </div>
            </div>
          </template>
        </div>
      </div>

      <div v-if="reportBuffer || result" class="dashboard-layout">
        <!-- 主报告区 -->
        <div class="result-wrap">
          <div class="result-meta">
            <div class="meta-tags">
              <el-tag type="success">置信度 {{ ((result?.confidence_score || 0) * 100).toFixed(0) }}%</el-tag>
              <el-tag style="margin-left:8px">分析帖子 {{ result?.screened_count ?? '—' }} 篇</el-tag>
              <el-tag style="margin-left:8px">分析评论 {{ result?.comment_count ?? '—' }} 条</el-tag>
            </div>
            <div class="meta-actions">
              <el-button size="small" @click="copyMarkdown">复制 Markdown</el-button>
              <el-button size="small" @click="downloadWord">下载 Word</el-button>
              <el-button size="small" @click="downloadPdf" :loading="pdfLoading">下载 PDF</el-button>
            </div>
          </div>

          <!-- 流式加载中：显示累积报告文本 -->
          <div v-if="reportBuffer && !result" class="streaming-report">
            <div class="section-main" v-html="renderMd(reportBuffer)" />
            <div class="streaming-indicator">报告生成中…<span class="dot-dot-dot"><span>.</span><span>.</span><span>.</span></span></div>
          </div>

          <!-- 报告完成：按章节分栏 -->
          <div v-else-if="result" class="report-sections" @click="handleReportClick">
            <div v-for="(sec, si) in sectionRows" :key="si" class="section-row" :style="{ animationDelay: si * 0.06 + 's' }">
              <div class="section-main">
                <div v-html="renderMd(sec.raw)"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 侧边引用/证据区 -->
        <div class="evidence-sidebar" v-if="evidenceItems.length > 0">
          <div class="sidebar-header">参考原文与评论</div>
          <div class="evidence-list">
            <a
              v-for="(ref, ri) in evidenceItems"
              :key="ref.id || ri"
              class="evidence-card"
              :class="{ 'is-static': !ref.sourceUrl }"
              :href="ref.sourceUrl || '#'"
              target="_blank"
              rel="noopener"
              :id="'ref-card-' + ri"
              @click="!ref.sourceUrl && $event.preventDefault()"
            >
              <div class="evidence-card-header">
                <span class="card-badge">{{ ri + 1 }}</span>
                <div class="evidence-topic">{{ ref.topic }}</div>
              </div>
              <div class="evidence-meta">
                <el-tag :type="sentimentTag(ref.sentiment)" size="small">{{ ref.sentiment }}</el-tag>
                <span class="evidence-source">{{ truncateTitle(ref.sourceTitle) }}</span>
              </div>
              <div class="evidence-quotes" v-if="ref.quotes?.length">
                <div v-for="(q, qi) in ref.quotes.slice(0, 2)" :key="qi" class="evidence-quote-item">
                  "{{ q }}"
                </div>
              </div>
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Cookie 配置弹窗 -->
    <el-dialog v-model="configVisible" title="配置小红书 Cookie" width="520px" :close-on-click-modal="false">
      <el-input
        v-model="cookieInput"
        type="textarea"
        :rows="4"
        placeholder="请粘贴小红书 Cookie..."
      />
      <div class="config-tip">
        首先请登录小红书网页端，然后将自己的登录 Cookie 放入输入框中。<br>
        Cookie 获取方法：在浏览器按 F12 打开控制台 → 点击「网络」→ 点击「Fetch/XHR」
        → 找一个带有 cookie 的请求 → 复制 Request Headers 中的 cookie 字段值。
      </div>
      <img src="/config-guide.png" class="config-img" alt="配置说明图" />
      <template #footer>
        <el-button @click="configVisible=false">取消</el-button>
        <el-button type="primary" @click="saveCookie">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheckFilled, CircleCloseFilled, Loading } from '@element-plus/icons-vue'
import { getHotspotLayoutClass, getVisibleHotspotGroups } from '../../utils/homeHotspots'

const query = ref('')
const loading = ref(false)
const started = ref(false)
const stages = ref([])
const result = ref(null)
const reportBuffer = ref('')
let hasScrolledToReport = false
const homeHotspots = ref([])
const hotspotsStale = ref(false)
const visibleHotspotGroups = computed(() => getVisibleHotspotGroups(homeHotspots.value))
const hotspotLayoutClass = computed(() => getHotspotLayoutClass(visibleHotspotGroups.value))
const THEME_STORAGE_KEY = 'xhs_home_theme'
const savedTheme = localStorage.getItem(THEME_STORAGE_KEY)
const themeMode = ref(savedTheme === 'classic' ? 'classic' : 'ink')

watch(themeMode, (mode) => {
  localStorage.setItem(THEME_STORAGE_KEY, mode === 'classic' ? 'classic' : 'ink')
})

onMounted(() => {
  fetchHomeHotspots()
})

const isPostReadingExpanded = ref(false)
const isReportGenerating = computed(() => !!reportBuffer.value || !!result.value)

function togglePostReading() {
  if (isReportGenerating.value) {
    isPostReadingExpanded.value = !isPostReadingExpanded.value
  }
}

// 报告生成后自动滚动到结果区域（预留顶部空间避免被导航栏遮挡）
watch(result, async (val) => {
  if (!val) return
  await nextTick()
  const el = document.querySelector('.result-wrap')
  if (el) {
    const offset = 40 // 预留 80px 顶部空间
    const top = el.getBoundingClientRect().top + window.pageYOffset - offset
    window.scrollTo({ top, behavior: 'smooth' })
  }
})

// 流式报告出现时自动滚动（仅首次触发一次）
watch(reportBuffer, async (val) => {
  if (!val || hasScrolledToReport) return
  hasScrolledToReport = true
  await nextTick()
  const el = document.querySelector('.result-wrap')
  if (el) {
    const offset = 40
    const top = el.getBoundingClientRect().top + window.pageYOffset - offset
    window.scrollTo({ top, behavior: 'smooth' })
  }
}, { flush: 'post' })

let evtSource = null
const analyzeProgress = ref(60)
let _analyzeTimer = null
const postReadingList = ref([])
watch(postReadingList, async () => {
  await nextTick()
  const box = document.querySelector('.post-reading-box')
  if (box) box.scrollTop = box.scrollHeight
}, { deep: true })

const configVisible = ref(false)
const cookieInput = ref(localStorage.getItem('xhs_cookie') || '')
let _cookieChecked = false
let _currentRunId = null

function saveCookie() {
  const ck = cookieInput.value.trim()
  if (ck) {
    localStorage.setItem('xhs_cookie', ck)
  } else {
    localStorage.removeItem('xhs_cookie')
  }
  configVisible.value = false
  ElMessage.success('Cookie 已保存')
}

// 记忆开关状态（从localStorage加载，默认关闭）
const enableMemory = ref(localStorage.getItem('enable_memory') === 'true')

function getSessionId() {
  let sessionId = localStorage.getItem('xhs_session_id')
  if (!sessionId) {
    sessionId = window.crypto?.randomUUID
      ? window.crypto.randomUUID()
      : `session_${Date.now()}_${Math.random().toString(16).slice(2)}`
    localStorage.setItem('xhs_session_id', sessionId)
  }
  return sessionId
}

function saveEnableMemory(val) {
  if (val) {
    localStorage.setItem('enable_memory', 'true')
  } else {
    localStorage.setItem('enable_memory', 'false')
  }
  ElMessage.success(val ? '已开启记忆功能' : '已关闭记忆功能')
}

async function fetchHomeHotspots() {
  try {
    const resp = await fetch('/api/v1/hotspots/home')
    if (!resp.ok) return
    const data = await resp.json()
    hotspotsStale.value = !!data.stale
    homeHotspots.value = Array.isArray(data.groups) ? data.groups : []
  } catch {
    homeHotspots.value = []
  }
}

async function startFromHotspot(item) {
  if (loading.value) return
  const nextQuery = (item?.query || item?.title || '').trim()
  if (!nextQuery) return
  query.value = nextQuery
  await startAnalysis()
}

function resetToHero() {
  stopAnalysis()
  started.value = false
  result.value = null
  reportBuffer.value = ''
  hasScrolledToReport = false
  isPostReadingExpanded.value = false
  stages.value = []
  postReadingList.value = []
  _cookieChecked = false
}

function _upsertStage(stage, message, progress) {
  const idx = stages.value.findIndex(s => s.stage === stage)
  if (idx >= 0) {
    stages.value[idx] = { stage, message, progress, done: false }
    stages.value = stages.value.map((s, i) => i < idx ? { ...s, done: true } : s)
  } else {
    stages.value = stages.value.map(s => ({ ...s, done: true }))
    stages.value.push({ stage, message, progress, done: false })
  }
  // analyze阶段启动进度动画
  if (stage === 'analyze') {
    analyzeProgress.value = 60
    if (!_analyzeTimer) {
      _analyzeTimer = setInterval(() => {
        if (analyzeProgress.value < 95) analyzeProgress.value += 2
      }, 2000)
    }
  } else if (_analyzeTimer) {
    clearInterval(_analyzeTimer)
    _analyzeTimer = null
  }
}

function _markAllDone() {
  stages.value = stages.value.map(s => ({ ...s, done: true }))
  if (_analyzeTimer) { clearInterval(_analyzeTimer); _analyzeTimer = null }
  analyzeProgress.value = 100
}

function stopAnalysis() {
  if (evtSource) { evtSource.close(); evtSource = null }
  if (_analyzeTimer) { clearInterval(_analyzeTimer); _analyzeTimer = null }
  loading.value = false
}

async function cancelAnalysis() {
  if (_currentRunId) {
    try { await fetch(`/api/v1/analysis/cancel/${_currentRunId}`, { method: 'DELETE' }) } catch {}
  }
  _upsertStage('cancel', '分析任务已取消，可点击重新分析再次执行', 0)
  stages.value = stages.value.map(s => ({ ...s, done: false, error: s.stage === 'cancel' }))
  stopAnalysis()
}

async function startAnalysis() {
  if (!query.value.trim()) return

  if (!_cookieChecked) {
    _cookieChecked = true
    try {
      const ck = cookieInput.value.trim()
      const params = ck ? `?cookie=${encodeURIComponent(ck)}` : ''
      const r = await fetch(`/api/v1/analysis/check-cookie${params}`)
      const d = await r.json()
      if (!d.valid && !ck) {
        configVisible.value = true
        _cookieChecked = false
        return
      }
    } catch {
      // 网络错误不阻断分析
    }
  }

  started.value = true
  loading.value = true
  stages.value = []
  result.value = null
  reportBuffer.value = ''
  hasScrolledToReport = false
  postReadingList.value = []
  let run_id
  try {
    const resp = await fetch('/api/v1/analysis/product', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: query.value.trim(),
        session_id: getSessionId(),
        cookie: cookieInput.value.trim() || undefined,
        enable_memory: enableMemory.value
      }),
    })
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try { const err = await resp.json(); detail = err.detail || detail } catch {}
      throw new Error(detail)
    }
    const data = await resp.json()
    run_id = data.run_id
    _currentRunId = run_id
  } catch (e) {
    ElMessage.error(e.message || '请求失败')
    loading.value = false
    return
  }

  evtSource = new EventSource(`/api/v1/analysis/stream/${run_id}`)

  evtSource.addEventListener('progress', (e) => {
    const d = JSON.parse(e.data)
    _upsertStage(d.stage, d.message, d.progress)
  })

  evtSource.addEventListener('post_reading', (e) => {
    const d = JSON.parse(e.data)
    postReadingList.value.push(`第 ${d.index}/${d.total} 篇：${d.title}`)
  })

  // 流式报告内容
  evtSource.addEventListener('report_chunk', (e) => {
    const d = JSON.parse(e.data)
    reportBuffer.value = d.text
  })

  evtSource.addEventListener('result', (e) => {
    try {
      result.value = JSON.parse(e.data)
      _markAllDone()
    } catch (err) {
      console.error('渲染最终结果时出现异常：', err)
      ElMessage.error('处理分析结果时出错：' + err.message)
      stopAnalysis()
    }
  })

  evtSource.addEventListener('error', (e) => {
    try {
      const d = JSON.parse(e.data)
      if (d.code === 'COOKIE_EXPIRED') {
        stopAnalysis()
        _cookieChecked = false
        _upsertStage('retrieve', 'Cookie 已过期，请点击右上角「配置 Cookie」按钮重新配置后再试', 0)
        stages.value = stages.value.map(s => ({ ...s, done: false, error: s.stage === 'retrieve' }))
        configVisible.value = true
        return
      }
      ElMessage.error(d.message || '分析失败')
    } catch {
      ElMessage.error('分析过程中发生错误')
    }
    stopAnalysis()
  })

  evtSource.addEventListener('done', () => stopAnalysis())

  evtSource.onerror = () => {
    if (loading.value) ElMessage.error('连接中断，请重试')
    stopAnalysis()
  }
}

function renderMd(text) {
  if (!text) return ''
  return renderMarkdownTables(text)
    .replace(/\n{2,}(#{1,3} )/g, '\n$1')
    .replace(/(#{1,3} .+)\n{2,}/g, '$1\n')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>')
    .replace(/\n(?!<)/g, '<br>')
}

function isMarkdownTableLine(line) {
  return /^\s*\|.+\|\s*$/.test(line)
}

function isMarkdownTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line)
}

function splitMarkdownTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map(cell => cell.trim().replace(/\\\|/g, '|'))
}

function renderMarkdownTables(text) {
  const source = String(text ?? '')
  const lines = source.split('\n')
  const rendered = []

  for (let i = 0; i < lines.length; i += 1) {
    if (isMarkdownTableLine(lines[i]) && i + 1 < lines.length && isMarkdownTableSeparator(lines[i + 1])) {
      const headers = splitMarkdownTableRow(lines[i])
      const rows = []
      i += 2
      while (i < lines.length && isMarkdownTableLine(lines[i])) {
        rows.push(splitMarkdownTableRow(lines[i]))
        i += 1
      }
      i -= 1

      const thead = headers.map(cell => `<th>${escapeHtml(cell)}</th>`).join('')
      const tbody = rows.map(row => {
        const cells = headers.map((_, index) => `<td>${escapeHtml(row[index] ?? '')}</td>`).join('')
        return `<tr>${cells}</tr>`
      }).join('')
      rendered.push(`<table class="md-table"><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`)
      continue
    }
    rendered.push(lines[i])
  }

  return rendered.join('\n')
}

function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const hasReportIr = computed(() => {
  const sections = result.value?.report_ir?.sections
  return Array.isArray(sections) && sections.length > 0
})

const evidenceItems = computed(() => {
  const citations = result.value?.report_ir?.citations
  if (Array.isArray(citations) && citations.length > 0) {
    return citations.map((citation, index) => ({
      id: citation.id || `cit_${index}`,
      topic: citation.topic || '参考证据',
      sentiment: citation.sentiment || '中立',
      sourceTitle: citation.source_title || '用户原话',
      sourceUrl: citation.source_url || '',
      quotes: citation.quote ? [citation.quote] : [],
    }))
  }

  return (result.value?.references || []).map((ref, index) => ({
    id: `ref_${index}`,
    topic: ref.topic || '参考证据',
    sentiment: ref.sentiment || '中立',
    sourceTitle: ref.source_title || '用户原话',
    sourceUrl: ref.source_note_url || '',
    quotes: [...(ref.quotes || []), ...(ref.evidence_quotes || [])],
  }))
})

const citationIndexById = computed(() => {
  const map = new Map()
  evidenceItems.value.forEach((item, index) => {
    if (item.id) map.set(item.id, index)
  })
  return map
})

function citationBadges(citationIds = []) {
  const indexes = []
  citationIds.forEach((id) => {
    const index = citationIndexById.value.get(id)
    if (Number.isInteger(index) && !indexes.includes(index)) {
      indexes.push(index)
    }
  })
  if (!indexes.length) return ''
  const badges = indexes
    .sort((a, b) => a - b)
    .map(index => `<span class="inline-citation" data-ref-index="${index}">${index + 1}</span>`)
    .join('')
  return `<span class="inline-citations-wrap">${badges}</span>`
}

function referenceAnchor(index) {
  return `ref-${index + 1}`
}

function wordBookmarkId(index) {
  return `ref_${index + 1}`
}

function mdText(text) {
  return String(text ?? '').replace(/\r/g, '').trim()
}

function mdLinkText(text) {
  return mdText(text).replace(/\\/g, '\\\\').replace(/\]/g, '\\]')
}

const CHART_COLUMN_LABELS = {
  label: '指标',
  value: '当前表现',
  insight: '解读',
  basis: '依据',
  note: '说明',
}

function chartRowValue(row, key) {
  if (!row) return ''
  if (key === 'label') return row.label ?? row.name ?? ''
  if (key === 'value') return row.value ?? row.count ?? ''
  return row[key] ?? ''
}

function chartColumns(rows = []) {
  const columns = Object.entries(CHART_COLUMN_LABELS)
    .filter(([key]) => rows.some((row) => {
      const value = chartRowValue(row, key)
      return value !== '' && value !== null && value !== undefined
    }))
  const seen = new Set(columns.map(([key]) => key))
  rows.forEach((row) => {
    Object.keys(row || {}).forEach((key) => {
      if (key === 'name' || key === 'count' || seen.has(key)) return
      columns.push([key, key])
      seen.add(key)
    })
  })
  return columns.length ? columns : [['label', '指标'], ['value', '当前表现']]
}

function markdownTableCell(value) {
  return mdText(value).replace(/\n/g, ' ').replace(/\|/g, '\\|')
}

function chartMarkdownLines(chart) {
  const rows = Array.isArray(chart?.data) ? chart.data : []
  if (!rows.length) return []
  const columns = chartColumns(rows)
  const lines = [
    `| ${columns.map(([, title]) => markdownTableCell(title)).join(' | ')} |`,
    `| ${columns.map(() => '---').join(' | ')} |`,
  ]
  rows.forEach((row) => {
    lines.push(`| ${columns.map(([key]) => markdownTableCell(chartRowValue(row, key))).join(' | ')} |`)
  })
  return lines
}

function markdownCitationLinks(citationIds = []) {
  const indexes = []
  citationIds.forEach((id) => {
    const index = citationIndexById.value.get(id)
    if (Number.isInteger(index) && !indexes.includes(index)) indexes.push(index)
  })
  if (!indexes.length) return ''
  return indexes
    .sort((a, b) => a - b)
    .map(index => `[[${index + 1}]](#${referenceAnchor(index)})`)
    .join('')
}

function renderReportIrMarkdown(reportIr) {
  if (!reportIr) return ''
  const lines = []
  const meta = reportIr.metadata || {}

  if (reportIr.title) {
    lines.push(`# ${escapeHtml(reportIr.title)}`, '')
  }

  if (meta.post_count || meta.comment_count || meta.confidence_score) {
    const confidence = Number(meta.confidence_score || 0)
    lines.push(
      `> 样本：${meta.post_count || 0} 篇帖子，${meta.comment_count || 0} 条评论；置信度：${Math.round(confidence * 100)}%`,
      ''
    )
  }

  if (Array.isArray(reportIr.summary_cards) && reportIr.summary_cards.length > 0) {
    lines.push('## 关键摘要')
    reportIr.summary_cards.forEach((card) => {
      if (card?.label && card?.value) {
        lines.push(`- **${escapeHtml(card.label)}**：${escapeHtml(card.value)}`)
      }
    })
    lines.push('')
  }

  if (Array.isArray(reportIr.charts) && reportIr.charts.some(chart => chart?.data?.length)) {
    lines.push('## 数据概览')
    reportIr.charts.forEach((chart) => {
      if (!chart?.data?.length) return
      lines.push(`### ${escapeHtml(chart.title || '图表')}`)
      lines.push(...chartMarkdownLines(chart))
      lines.push('')
    })
  }

  ;(reportIr.sections || []).forEach((section) => {
    lines.push(`## ${escapeHtml(section.title || '未命名章节')}`, '')
    ;(section.blocks || []).forEach((block) => {
      const badges = citationBadges(block.citation_ids || [])
      if (block.type === 'subheading') {
        if (block.text) lines.push(`### ${escapeHtml(block.text)}`, '')
        return
      }

      if (block.type === 'list') {
        const items = block.items?.length ? block.items : (block.text ? [block.text] : [])
        items.forEach((item) => {
          if (item) lines.push(`- ${escapeHtml(item)}${badges}`)
        })
        lines.push('')
        return
      }

      if (block.text) {
        lines.push(`${escapeHtml(block.text)}${badges}`, '')
      }
    })
  })

  if (Array.isArray(meta.limitations) && meta.limitations.length > 0) {
    lines.push('## 局限性')
    meta.limitations.forEach((limitation) => {
      if (limitation) lines.push(`- ${escapeHtml(limitation)}`)
    })
    lines.push('')
  }

  return lines.join('\n').trim()
}

function buildExportMarkdown() {
  if (!result.value) return reportBuffer.value || ''
  const reportIr = result.value.report_ir
  if (!hasReportIr.value) {
    const base = result.value.final_answer || ''
    if (!evidenceItems.value.length) return base
    const refs = ['## 参考证据']
    evidenceItems.value.forEach((ref, index) => {
      refs.push(`<a id="${referenceAnchor(index)}"></a>`)
      const source = mdLinkText(ref.sourceTitle || '用户原话')
      const sourceText = ref.sourceUrl ? `[${source}](${ref.sourceUrl})` : source
      refs.push(`[${index + 1}] ${sourceText}：${mdText(ref.quotes?.[0] || '')}`)
      refs.push('')
    })
    return `${base.trim()}\n\n${refs.join('\n').trim()}`
  }

  const lines = []
  const meta = reportIr.metadata || {}
  if (reportIr.title) lines.push(`# ${mdText(reportIr.title)}`, '')
  if (meta.post_count || meta.comment_count || meta.confidence_score) {
    const confidence = Number(meta.confidence_score || 0)
    lines.push(`> 样本：${meta.post_count || 0} 篇帖子，${meta.comment_count || 0} 条评论；置信度：${Math.round(confidence * 100)}%`, '')
  }

  if (Array.isArray(reportIr.summary_cards) && reportIr.summary_cards.length > 0) {
    lines.push('## 关键摘要')
    reportIr.summary_cards.forEach((card) => {
      if (card?.label && card?.value) lines.push(`- **${mdText(card.label)}**：${mdText(card.value)}`)
    })
    lines.push('')
  }

  if (Array.isArray(reportIr.charts) && reportIr.charts.some(chart => chart?.data?.length)) {
    lines.push('## 数据概览')
    reportIr.charts.forEach((chart) => {
      if (!chart?.data?.length) return
      lines.push(`### ${mdText(chart.title || '图表')}`)
      lines.push(...chartMarkdownLines(chart))
      lines.push('')
    })
  }

  ;(reportIr.sections || []).forEach((section) => {
    lines.push(`## ${mdText(section.title || '未命名章节')}`, '')
    ;(section.blocks || []).forEach((block) => {
      const links = markdownCitationLinks(block.citation_ids || [])
      if (block.type === 'subheading') {
        if (block.text) lines.push(`### ${mdText(block.text)}`, '')
        return
      }
      if (block.type === 'list') {
        const items = block.items?.length ? block.items : (block.text ? [block.text] : [])
        items.forEach((item) => {
          if (item) lines.push(`- ${mdText(item)}${links}`)
        })
        lines.push('')
        return
      }
      if (block.text) lines.push(`${mdText(block.text)}${links}`, '')
    })
  })

  if (Array.isArray(meta.limitations) && meta.limitations.length > 0) {
    lines.push('## 局限性')
    meta.limitations.forEach((limitation) => {
      if (limitation) lines.push(`- ${mdText(limitation)}`)
    })
    lines.push('')
  }

  if (evidenceItems.value.length > 0) {
    lines.push('## 参考证据')
    evidenceItems.value.forEach((ref, index) => {
      lines.push(`<a id="${referenceAnchor(index)}"></a>`)
      const source = mdLinkText(ref.sourceTitle || '用户原话')
      const sourceText = ref.sourceUrl ? `[${source}](${ref.sourceUrl})` : source
      lines.push(`[${index + 1}] ${sourceText}：${mdText(ref.quotes?.[0] || '')}`)
      lines.push('')
    })
  }

  return lines.join('\n').trim()
}

function parseSections(markdown, inferCitations = false) {
  const lines = markdown.split('\n')
  const sections = []
  let cur = null
  for (const line of lines) {
    const m = line.match(/^(#{1,3}) (.+)/)
    if (m) {
      if (cur) sections.push(cur)
      cur = { title: m[2], level: m[1].length, body: '', raw: line + '\n' }
    } else if (cur) {
      cur.body += line + '\n'
      cur.raw += line + '\n'
    } else {
      if (!sections.length) sections.push({ title: '', level: 0, body: line + '\n', raw: line + '\n' })
      else {
        sections[0].body += line + '\n'
        sections[0].raw += line + '\n'
      }
    }
  }
  if (cur) sections.push(cur)

  // 挂载引用序号角标
  const refs = inferCitations ? evidenceItems.value : []
  if (refs.length > 0) {
    sections.forEach(sec => {
      const matchIndexes = new Set()
      const searchTarget = (sec.raw || '').toLowerCase()

      refs.forEach((ref, ri) => {
        let matched = false;

        // 1. Theme/Topic segment matching
        if (ref.topic) {
          const topicStr = ref.topic.replace(/[^\u4e00-\u9fa5a-zA-Z0-9]/g, '')
          if (topicStr.length > 0) {
            let words = []
            if (window.Intl && Intl.Segmenter) {
              const segmenter = new Intl.Segmenter('zh', { granularity: 'word' })
              words = Array.from(segmenter.segment(topicStr)).filter(s => s.isWordLike).map(s => s.segment)
            } else {
              // Fallback: 2-char sliding window
              for (let i = 0; i < topicStr.length - 1; i++) {
                words.push(topicStr.slice(i, i + 2))
              }
            }
            words = words.filter(w => w.length >= 2)
            if (words.length > 0) {
              let matchCount = 0
              words.forEach(w => {
                if (searchTarget.includes(w.toLowerCase())) matchCount++
              })
              // 收紧：匹配比例从 50% 提高到 75%，避免短小泛词造成大量误匹配
              // 例如：2个词需匹配2个，3个词需匹配3个，4个词需匹配3个...
              if (matchCount > 0 && matchCount >= Math.ceil(words.length * 0.75)) {
                matched = true
              }
            } else if (topicStr.length >= 4 && searchTarget.includes(topicStr.toLowerCase())) {
              // 补充校验：如果没有正常被分词，要求原长串至少包含4个及以上字符才算命中
              matched = true
            }
          }
        }

        // 2. Quotes sliding window matching
        if (!matched) {
          const quotes = ref.quotes || []
          for (const q of quotes) {
            const cleanQ = q.replace(/[^\u4e00-\u9fa5a-zA-Z0-9]/g, '').toLowerCase()

            // 收紧：原话滑动特征窗口从 5 提高到 8 个连续字符相连，拦截日常用语的雷同
            const WINDOW_SIZE = 8;

            if (cleanQ.length < WINDOW_SIZE) {
              // 如果原话本身就偏短，必须整句出现，且至少有 4 个字符以防误杀
              if (cleanQ.length >= 4 && searchTarget.includes(cleanQ)) {
                matched = true; break;
              }
            } else {
              // 8-char sliding window
              for (let i = 0; i <= cleanQ.length - WINDOW_SIZE; i++) {
                if (searchTarget.includes(cleanQ.slice(i, i + WINDOW_SIZE))) {
                  matched = true; break;
                }
              }
            }
            if (matched) break;
          }
        }

        if (matched) {
          matchIndexes.add(ri)
        }
      })

      if (matchIndexes.size > 0) {
        let badgesHtml = '<span class="inline-citations-wrap">'
        Array.from(matchIndexes).sort((a,b)=>a-b).forEach(ri => {
          badgesHtml += `<span class="inline-citation" data-ref-index="${ri}">${ri + 1}</span>`
        })
        badgesHtml += '</span>'
        sec.raw = sec.raw.trimEnd() + '&nbsp;' + badgesHtml + '\n\n'
      }
    })
  }

  return sections
}

const sectionRows = computed(() => {
  if (!result.value) return []
  if (hasReportIr.value) {
    return parseSections(renderReportIrMarkdown(result.value.report_ir), false)
  }
  return parseSections(result.value.final_answer || '', true)
})

function handleReportClick(e) {
  const target = e.target.closest('.inline-citation')
  if (!target) return
  const idx = parseInt(target.getAttribute('data-ref-index'), 10)
  const sidebar = document.querySelector('.evidence-sidebar')
  const card = document.getElementById('ref-card-' + idx)
  if (sidebar && card) {
    // 采用更可靠的原生 scrollIntoView，居中对齐能避免被上方吸顶表头遮挡，也不受边距乱算影响
    card.scrollIntoView({
      behavior: 'smooth',
      block: 'center'
    })

    // 重置并触发高亮动画
    card.classList.remove('flash-highlight')
    void card.offsetWidth // force reflow
    card.classList.add('flash-highlight')

    // 延迟 3 秒后移除动画类
    setTimeout(() => {
      if (card) card.classList.remove('flash-highlight')
    }, 3000)
  }
}

function truncateTitle(title) {
  if (!title || title === '无标题') return '查看原帖'
  return title.length > 12 ? title.slice(0, 12) + '…' : title
}

function sentimentTag(s) {
  return s === '正面' ? 'success' : s === '负面' ? 'danger' : 'info'
}

function sentimentClass(s) {
  if (s === '正面') return 'dot-success';
  if (s === '负面') return 'dot-danger';
  if (s === '中立') return 'dot-info';
  return 'dot-default';
}

const pdfLoading = ref(false)

async function copyMarkdown() {
  const content = result.value ? buildExportMarkdown() : reportBuffer.value
  if (!content) {
    ElMessage.warning('报告尚未生成');
    return;
  }
  
  try {
    await navigator.clipboard.writeText(content);
    ElMessage.success('已复制完整 Markdown');
  } catch (err) {
    ElMessage.error('复制失败');
    console.error(err);
  }
}

async function downloadPdf() {
  pdfLoading.value = true
  try {
    if (!hasReportIr.value) {
      ElMessage.warning('当前报告缺少结构化 Report IR，无法生成标准 PDF')
      return
    }
    const response = await fetch('/api/v1/export/pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report_ir: result.value.report_ir }),
    })

    if (!response.ok) {
      let message = `HTTP ${response.status}`
      try {
        const data = await response.json()
        message = data.detail || data.message || message
      } catch {
        // ignore non-json error body
      }
      throw new Error(message)
    }

    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${result.value.report_ir?.title || 'report'}.pdf`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error(`PDF 生成失败：${e.message || e}`)
  } finally {
    pdfLoading.value = false
  }
}

async function downloadWord() {
  try {
    const {
      Document,
      Packer,
      Paragraph,
      TextRun,
      HeadingLevel,
      ExternalHyperlink,
      InternalHyperlink,
      Bookmark,
      Table,
      TableRow,
      TableCell,
      WidthType,
    } = await import('docx')

    const linkRun = (text) => new TextRun({ text, style: 'Hyperlink', color: '2563EB', underline: {} })
    const citationRuns = (citationIds = []) => {
      const indexes = []
      citationIds.forEach((id) => {
        const index = citationIndexById.value.get(id)
        if (Number.isInteger(index) && !indexes.includes(index)) indexes.push(index)
      })
      return indexes.sort((a, b) => a - b).map(index => new InternalHyperlink({
        anchor: wordBookmarkId(index),
        children: [linkRun(`[${index + 1}]`)],
      }))
    }
    const textWithCitations = (text, citationIds = []) => [
      new TextRun({ text: mdText(text) }),
      ...citationRuns(citationIds),
    ]
    const pushReferenceSection = (children) => {
      if (!evidenceItems.value.length) return
      children.push(new Paragraph({ text: '参考证据', heading: HeadingLevel.HEADING_2 }))
      evidenceItems.value.forEach((ref, index) => {
        const sourceChild = ref.sourceUrl
          ? new ExternalHyperlink({
              link: ref.sourceUrl,
              children: [linkRun(ref.sourceTitle || '用户原话')],
            })
          : new TextRun({ text: ref.sourceTitle || '用户原话' })
        children.push(new Paragraph({
          children: [
            new Bookmark({
              id: wordBookmarkId(index),
              children: [new TextRun({ text: `[${index + 1}] `, bold: true })],
            }),
            sourceChild,
            new TextRun({ text: `：${ref.quotes?.[0] || ''}` }),
          ],
        }))
      })
    }

    let children = []
    if (hasReportIr.value) {
      const reportIr = result.value.report_ir
      const meta = reportIr.metadata || {}
      if (reportIr.title) children.push(new Paragraph({ text: reportIr.title, heading: HeadingLevel.HEADING_1 }))
      if (meta.post_count || meta.comment_count || meta.confidence_score) {
        const confidence = Number(meta.confidence_score || 0)
        children.push(new Paragraph({
          children: [new TextRun({ text: `样本：${meta.post_count || 0} 篇帖子，${meta.comment_count || 0} 条评论；置信度：${Math.round(confidence * 100)}%`, italics: true })],
        }))
      }
      if (Array.isArray(reportIr.summary_cards) && reportIr.summary_cards.length > 0) {
        children.push(new Paragraph({ text: '关键摘要', heading: HeadingLevel.HEADING_2 }))
        reportIr.summary_cards.forEach((card) => {
          if (card?.label && card?.value) {
            children.push(new Paragraph({
              bullet: { level: 0 },
              children: [
                new TextRun({ text: `${card.label}：`, bold: true }),
                new TextRun({ text: card.value }),
              ],
            }))
          }
        })
      }
      if (Array.isArray(reportIr.charts) && reportIr.charts.some(chart => chart?.data?.length)) {
        children.push(new Paragraph({ text: '数据概览', heading: HeadingLevel.HEADING_2 }))
        reportIr.charts.forEach((chart) => {
          if (!chart?.data?.length) return
          children.push(new Paragraph({ text: chart.title || '图表', heading: HeadingLevel.HEADING_3 }))
          const columns = chartColumns(chart.data)
          children.push(new Table({
            width: { size: 100, type: WidthType.PERCENTAGE },
            rows: [
              new TableRow({
                children: columns.map(([, title]) => new TableCell({
                  children: [new Paragraph({ children: [new TextRun({ text: title, bold: true })] })],
                })),
              }),
              ...chart.data.map(row => new TableRow({
                children: columns.map(([key]) => new TableCell({
                  children: [new Paragraph({ text: mdText(chartRowValue(row, key)) })],
                })),
              })),
            ],
          }))
        })
      }
      ;(reportIr.sections || []).forEach((section) => {
        children.push(new Paragraph({ text: section.title || '未命名章节', heading: HeadingLevel.HEADING_2 }))
        ;(section.blocks || []).forEach((block) => {
          if (block.type === 'subheading') {
            if (block.text) children.push(new Paragraph({ text: block.text, heading: HeadingLevel.HEADING_3 }))
            return
          }
          if (block.type === 'list') {
            const items = block.items?.length ? block.items : (block.text ? [block.text] : [])
            items.forEach((item) => {
              if (item) children.push(new Paragraph({ bullet: { level: 0 }, children: textWithCitations(item, block.citation_ids || []) }))
            })
            return
          }
          if (block.text) children.push(new Paragraph({ children: textWithCitations(block.text, block.citation_ids || []) }))
        })
      })
      if (Array.isArray(meta.limitations) && meta.limitations.length > 0) {
        children.push(new Paragraph({ text: '局限性', heading: HeadingLevel.HEADING_2 }))
        meta.limitations.forEach((limitation) => {
          if (limitation) children.push(new Paragraph({ text: limitation, bullet: { level: 0 } }))
        })
      }
      pushReferenceSection(children)
    } else {
      const lines = (result.value.final_answer || '').split('\n')
      children = lines.map(line => {
        if (line.startsWith('### ')) return new Paragraph({ text: line.slice(4), heading: HeadingLevel.HEADING_3 })
        if (line.startsWith('## ')) return new Paragraph({ text: line.slice(3), heading: HeadingLevel.HEADING_2 })
        if (line.startsWith('# ')) return new Paragraph({ text: line.slice(2), heading: HeadingLevel.HEADING_1 })
        if (line.startsWith('- ')) return new Paragraph({ text: line.slice(2), bullet: { level: 0 } })
        const parts = line.split(/\*\*(.+?)\*\*/)
        if (parts.length === 1) return new Paragraph({ text: line })
        const textRuns = parts.map((p, i) => new TextRun({ text: p, bold: i % 2 === 1 }))
        return new Paragraph({ children: textRuns })
      })
      pushReferenceSection(children)
    }

    const doc = new Document({ sections: [{ children }] })
    const blob = await Packer.toBlob(doc)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = 'report.docx'; a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error('Word 生成失败')
  }
}
</script>

<style scoped>
/* ── Keyframes ── */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(20px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes slideDown {
  from { opacity: 0; transform: translateY(-16px); }
  to   { opacity: 1; transform: translateY(0); }
}

.page-actions {
  position: fixed;
  top: 16px;
  right: 20px;
  z-index: 200;
  display: flex;
  align-items: center;
  gap: 12px;
}
.memory-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
}
.memory-label {
  font-size: 14px;
  color: #606266;
  white-space: nowrap;
}
.config-button {
  flex-shrink: 0;
}

.aside-header {
  background: #eefaff;  /* 浅灰色圆角横条 */
  color: #3c3c5e;       /* 文字深灰 */
  font-size: 14px;
  font-weight: 380;
  text-align: center;
  padding: 1px 0;
  margin-bottom: 8px;
  border-radius: 10px;
  letter-spacing: 0.5px;
}

/* ── Hero ── */
.hero {
  --hero-padding-top: clamp(96px, 18dvh, 196px);
  --hero-padding-bottom: 36px;
  min-height: 100vh;
  min-height: 100dvh;
  height: auto;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  background: linear-gradient(135deg, #DBEAFE 0%, #F3F4F6 60%, #D1FAE5 100%);
  background-image: linear-gradient(135deg, #DBEAFE 0%, #F3F4F6 60%, #D1FAE5 100%),
    radial-gradient(circle, rgba(30,58,138,0.06) 1px, transparent 1px);
  background-size: auto, 28px 28px;
  padding: var(--hero-padding-top) 20px var(--hero-padding-bottom);
  animation: fadeInUp 0.6s ease both;
}
.hero-main {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.brand {
  font-size: clamp(42px, 4.8vw, 64px);
  font-weight: 800;
  background: linear-gradient(135deg, #1E3A8A, #3B82F6);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 12px;
  letter-spacing: 3px;
}
.hero-sub {
  color: #6B7280;
  font-size: 15px;
  margin-bottom: 36px;
}
.hero-search {
  display: flex;
  gap: 12px;
  width: min(660px, 90vw);
  background: rgba(255,255,255,0.8);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255,255,255,0.7);
  box-shadow: 0 4px 24px rgba(30,58,138,0.08);
  border-radius: 14px;
  padding: 10px 12px;
  animation: slideDown 0.5s ease 0.15s both;
}
.hero-input { flex: 1; }
.hero-btn { white-space: nowrap; }
.hotspot-arc {
  width: min(1240px, 98vw);
  position: relative;
  height: 318px;
  margin-top: 34px;
  --hotspot-block-height: 202px;
  --hotspot-item-height: clamp(30px, 4.2dvh, 34px);
}
.hotspot-arc.hotspot-items-5 {
  --hotspot-block-height: 232px;
  --hotspot-item-height: clamp(28px, 3.8dvh, 32px);
}
.hotspot-arc.hotspot-items-4 {
  --hotspot-block-height: 202px;
  --hotspot-item-height: clamp(30px, 4.2dvh, 34px);
}
.hotspot-block {
  position: absolute;
  width: min(272px, 24vw);
  height: var(--hotspot-block-height);
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: rgba(255,255,255,0.78);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255,255,255,0.72);
  border-radius: 8px;
  padding: clamp(8px, 1.2dvh, 12px);
  box-shadow: 0 8px 28px rgba(31,41,55,0.08);
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
}
.hotspot-block:nth-child(1) {
  left: 0;
  top: 6px;
  transform: rotate(8deg);
  transform-origin: top left;
}
.hotspot-block:nth-child(2) {
  left: 26%;
  top: 43px;
  transform: rotate(3deg);
  transform-origin: top left;
}
.hotspot-block:nth-child(3) {
  right: 26%;
  top: 54px;
  transform: rotate(-3deg);
  transform-origin: top left;
}
.hotspot-block:nth-child(4) {
  right: 0;
  top: 37px;
  transform: rotate(-8deg);
  transform-origin: top left;
}
.hotspot-arc.hotspot-count-3 {
  width: min(1160px, 98vw);
}
.hotspot-arc.hotspot-count-3 .hotspot-block {
  width: min(300px, 29vw);
}
.hotspot-arc.hotspot-count-3 .hotspot-block:nth-child(1) {
  left: 2%;
  top: 12px;
  transform: rotate(7deg);
  transform-origin: top center;
}
.hotspot-arc.hotspot-count-3 .hotspot-block:nth-child(2) {
  left: 50%;
  top: 58px;
  right: auto;
  transform: translateX(-50%) rotate(0deg);
  transform-origin: top center;
}
.hotspot-arc.hotspot-count-3 .hotspot-block:nth-child(3) {
  right: 2%;
  top: 12px;
  transform: rotate(-7deg);
  transform-origin: top center;
}
.hotspot-block:hover {
  box-shadow: 0 14px 34px rgba(31,41,55,0.12);
  border-color: rgba(37,99,235,0.24);
}
.hotspot-title {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 20px;
  font-size: 12.5px;
  font-weight: 700;
  color: #2563EB;
  background: rgba(219,234,254,0.72);
  border: 1px solid rgba(147,197,253,0.45);
  border-radius: 999px;
  margin: 0 auto 10px;
  padding: 3px 10px;
  letter-spacing: 0.04em;
}
.hotspot-item {
  width: 100%;
  flex: 0 0 var(--hotspot-item-height);
  min-height: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 0;
  background: transparent;
  border-radius: 6px;
  padding: 0 6px;
  cursor: pointer;
  text-align: left;
  color: #374151;
  transition: background 0.14s ease, color 0.14s ease;
}
.hotspot-item:hover {
  background: rgba(239,246,255,0.95);
  color: #1D4ED8;
}
.hotspot-item:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.hotspot-index {
  flex: 0 0 auto;
  width: 19px;
  height: 19px;
  border-radius: 50%;
  background: #EEF2FF;
  color: #4F46E5;
  font-size: 11px;
  line-height: 19px;
  text-align: center;
  font-weight: 650;
}
.hotspot-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13.5px;
  font-weight: 450;
  line-height: 1.35;
  letter-spacing: 0;
}

/* ── Analysis page shell ── */
.analysis-page {
  min-height: 100vh;
  background: linear-gradient(160deg, #DBEAFE 0%, #F3F4F6 50%, #D1FAE5 100%);
  display: flex;
  flex-direction: column;
}

/* ── Top bar ── */
.top-bar {
  background: rgba(255,255,255,0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid rgba(0,0,0,0.06);
  position: sticky;
  top: 0;
  z-index: 100;
}
.top-bar-inner {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
  max-width: 900px;
  margin: 0 auto;
  width: 100%;
}
.top-query-input {
  flex: 1;
  max-width: 440px;
  min-width: 180px;
}
.brand-mini {
  font-size: 18px;
  font-weight: 800;
  background: linear-gradient(135deg, #1E3A8A, #3B82F6);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  white-space: nowrap;
}

/* ── Progress area ── */
.progress-area {
  max-width: 900px;
  margin: 0 auto;
  padding: 20px 20px 0;
  width: 100%;
}
.progress-inner {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.stage-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  color: #111827;
  background: rgba(255,255,255,0.8);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border: 1px solid rgba(255,255,255,0.6);
  box-shadow: 0 2px 12px rgba(0,0,0,0.05);
  border-radius: 10px;
  padding: 6px 14px;
  animation: fadeInUp 0.4s ease both;
}
.stage-msg {
  flex: 1;
  color: #374151;
}
.stage-progress {
  width: 120px;
  flex-shrink: 0;
}

/* ── Dashboard Layout & Sidebar ── */
.dashboard-layout {
  max-width: 1200px;
  margin: 24px auto;
  display: flex;
  gap: 20px;
  align-items: flex-start;
  padding: 0 20px;
}
.result-wrap {
  flex: 1;
  min-width: 0;
  margin: 0;
  background: rgba(255,255,255,0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255,255,255,0.65);
  border-radius: 16px;
  box-shadow: 0 4px 32px rgba(30,58,138,0.08);
  padding: 0 0 24px;
  overflow: hidden;
}

.evidence-sidebar {
  width: 340px;
  flex-shrink: 0;
  position: sticky;
  top: 80px;
  background: rgba(248, 250, 252, 0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(226, 232, 240, 0.8);
  border-radius: 16px;
  max-height: calc(100vh - 100px);
  overflow-y: auto;
  box-shadow: 0 4px 20px rgba(0,0,0,0.04);
  padding: 0;
  /* 自定义滚动条更美观 */
  scrollbar-width: thin;
  scrollbar-color: #cbd5e1 transparent;
}
.evidence-sidebar::-webkit-scrollbar {
  width: 6px;
}
.evidence-sidebar::-webkit-scrollbar-thumb {
  background-color: #cbd5e1;
  border-radius: 3px;
}
.sidebar-header {
  font-size: 15px;
  font-weight: 700;
  color: #334155;
  margin: 0;
  text-align: center;
  padding: 16px 16px 12px 16px;
  border-bottom: 1px solid #e2e8f0;
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgba(248, 250, 252, 0.95);
  backdrop-filter: blur(8px);
  border-top-left-radius: 16px;
  border-top-right-radius: 16px;
}
.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
}
.evidence-card {
  display: block;
  text-decoration: none;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 12px;
  transition: all 0.3s ease;
}
.evidence-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0,0,0,0.05);
  border-color: #cbd5e1;
}
.evidence-card.is-static {
  cursor: default;
}
.evidence-card.is-static:hover {
  transform: none;
}
.evidence-card-header {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
}
.card-badge {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #EFF6FF;
  border: 1px solid #BFDBFE;
  color: #2563EB;
  font-size: 12px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 1px;
}
.evidence-topic {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  color: #0f172a;
  line-height: 1.4;
}
.evidence-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.evidence-source {
  font-size: 12px;
  color: #64748b;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.evidence-quotes {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.evidence-quote-item {
  font-size: 13px;
  color: #475569;
  line-height: 1.5;
  padding: 8px 10px;
  background: #f8fafc;
  border-radius: 6px;
  border-left: 3px solid #cbd5e1;
  word-break: break-all;
}
.result-wrap::before {
  content: '';
  display: block;
  height: 4px;
  background: linear-gradient(90deg, #1E3A8A, #3B82F6, #10B981);
}
.result-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 8px;
  padding: 20px 28px 0;
}
.meta-tags { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.meta-actions { display: flex; gap: 6px; }
/* ── Streaming report ── */
.streaming-report {
  padding: 0 28px;
  min-height: 200px;
  line-height: 1.9;
  font-size: 15px;
  color: #111827;
}
.streaming-indicator {
  margin-top: 24px;
  color: #3B82F6;
  font-size: 14px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.dot-dot-dot span {
  animation: bounce 1.4s infinite ease-in-out both;
  display: inline-block;
}
.dot-dot-dot span:nth-child(1) { animation-delay: -0.32s; }
.dot-dot-dot span:nth-child(2) { animation-delay: -0.16s; }
@keyframes bounce {
  0%, 80%, 100% { transform: scale(0); }
  40% { transform: scale(1); }
}
.report-sections {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 0 28px;
}
.section-row {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  animation: fadeInUp 0.45s ease both;
}
.section-main {
  flex: 1;
  min-width: 0;
  line-height: 1.9;
  font-size: 15px;
  color: #111827;
}
.section-main :deep(h1) {
  font-size: 20px;
  margin: 8px 0 4px;
  color: #111827;
  text-align: center;
  font-weight: 800;
}
.section-main :deep(h2) {
  font-size: 16px;
  margin: 4px 0 2px;
  color: #2563EB;
  border-left: 3px solid #60A5FA;
  background: #EFF6FF;
  padding: 3px 8px;
  border-radius: 0 6px 6px 0;
  font-weight: 700;
}
.section-main :deep(h3) {
  margin: 2px 0 2px;
  font-size: 14px;
  color: #6B7280;
  font-weight: 600;
  padding-left: 10px;
  border-left: 2px solid #BFDBFE;
}
.section-main :deep(ul) {
  margin: 4px 0;
  padding-left: 20px;
}
.section-main :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 8px 0 14px;
  table-layout: fixed;
  font-size: 13.5px;
  background: #ffffff;
}
.section-main :deep(th),
.section-main :deep(td) {
  border: 1px solid #dbeafe;
  padding: 8px 10px;
  vertical-align: top;
  line-height: 1.65;
  word-break: break-word;
}
.section-main :deep(th) {
  background: #eff6ff;
  color: #1e40af;
  font-weight: 700;
  text-align: left;
}
.section-main :deep(td) {
  color: #334155;
}
/* ── Inline Citations ── */
.section-main :deep(.inline-citations-wrap) {
  display: inline-flex;
  gap: 4px;
  vertical-align: middle;
  margin-left: 4px;
}
.section-main :deep(.inline-citation) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #EFF6FF;
  border: 1px solid #BFDBFE;
  color: #2563EB;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  user-select: none;
  transition: all 0.2s ease;
  vertical-align: super;
}
.section-main :deep(.inline-citation:hover) {
  background: #2563EB;
  color: #FFF;
  transform: scale(1.1);
  box-shadow: 0 2px 6px rgba(37,99,235,0.3);
}
@keyframes flashBg {
  0%   { box-shadow: 0 0 0 0 rgba(37,99,235,0.5); border-color: #3B82F6; background: #EFF6FF; transform: scale(1); }
  15%  { box-shadow: 0 0 0 8px rgba(37,99,235,0.2); border-color: #2563EB; background: #DBEAFE; transform: scale(1.02); }
  30%  { box-shadow: 0 0 0 4px rgba(37,99,235,0.1); border-color: #3B82F6; background: #EFF6FF; transform: scale(1); }
  100% { box-shadow: 0 0 0 0 rgba(37,99,235,0); border-color: #e2e8f0; background: #ffffff; transform: scale(1); }
}
.flash-highlight {
  animation: flashBg 3s ease-out;
}
/* --- Source Chips 胶囊样式 --- */
.source-chips-container {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
  margin-bottom: 24px;
}
.source-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 20px;
  text-decoration: none;
  transition: all 0.2s ease;
}
.source-chip:hover {
  background: #f1f5f9;
  border-color: #cbd5e1;
  transform: translateY(-1px);
}
.chip-title {
  font-size: 12px;
  color: #475569;
  max-width: 200px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chip-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.dot-success { background: #10B981; }
.dot-danger  { background: #EF4444; }
.dot-info    { background: #F59E0B; }
.dot-default { background: #94A3B8; }
.popover-card {
  position: fixed;
  z-index: 9999;
  background: rgba(255,255,255,0.92);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255,255,255,0.7);
  border-radius: 12px;
  box-shadow: 0 4px 24px rgba(30,58,138,0.1);
  padding: 14px 16px;
  width: 260px;
  pointer-events: none;
}
.popover-topic {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
  margin-bottom: 6px;
}
.popover-empty {
  font-size: 13px;
  color: #6B7280;
}
.config-tip {
  margin-top: 12px;
  font-size: 13px;
  color: #6B7280;
  line-height: 1.7;
}
.config-img {
  margin-top: 12px;
  width: 100%;
  border-radius: 8px;
  border: 1px solid rgba(0,0,0,0.06);
}
.post-reading-box {
  margin-top: 8px;
  height: 110px; /* 固定高度，不会因为内容增加把下方布局往下顶 */
  overflow-y: auto;
  background: rgba(239,246,255,0.8);
  border: 1px solid rgba(191,219,254,0.6);
  border-radius: 16px; /* 椭圆框 */
  padding: 10px 20px;
  font-size: 13px;
  color: #374151;
  line-height: 1.6;
  transition: all 0.3s ease;
}
.post-reading-box.is-collapsed {
  height: 21px; /* 跟随 stage-item 类似的高度 */
  margin-top: 0;
  padding: 6px 14px;
  background: rgba(255,255,255,0.8);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border: 1px solid rgba(255,255,255,0.6);
  box-shadow: 0 2px 12px rgba(0,0,0,0.05);
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.post-reading-box.is-collapsed:hover {
  background: rgba(243, 244, 246, 0.8);
}
.post-reading-summary {
  color: #6B7280;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 0.5px;
}
.post-reading-content {
  display: flex;
  flex-direction: column;
}
.collapse-btn {
  margin-top: 8px;
  text-align: center;
  color: #2563EB;
  font-weight: 600;
  cursor: pointer;
  padding: 4px 0;
}
.collapse-btn:hover {
  text-decoration: underline;
}
.post-reading-item:last-child {
  color: #1E3A8A;
  font-weight: 500;
}

/* ── Element Plus button overrides ── */
:deep(.el-button) {
  border-radius: 8px;
}
.hero-btn :deep(.el-button),
.el-button--primary {
  background: linear-gradient(135deg, #2563EB, #60A5FA);
  border-color: transparent;
  border-radius: 8px;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.el-button--primary:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(37,99,235,0.28);
}

/* ── Sentiment tag color overrides ── */
:deep(.el-tag--success) {
  background: rgba(16,185,129,0.1);
  border-color: rgba(16,185,129,0.3);
  color: #059669;
  border-radius: 20px;
}
:deep(.el-tag--danger) {
  background: rgba(239,68,68,0.1);
  border-color: rgba(239,68,68,0.3);
  color: #DC2626;
  border-radius: 20px;
}
:deep(.el-tag--info) {
  background: rgba(245,158,11,0.1);
  border-color: rgba(245,158,11,0.3);
  color: #D97706;
  border-radius: 20px;
}

.analysis-shell {
  min-height: 100vh;
}

.theme-switch {
  flex-shrink: 0;
  white-space: nowrap;
}

.analysis-shell.theme-ink {
  --ink-rice: #ffffff;
  --ink-paper: #f7f8f8;
  --ink-paper-deep: #e5e7eb;
  --ink-mist: #f1f3f4;
  --ink-ash: #9ca3af;
  --ink-stone: #4b5563;
  --ink-black: #080808;
  --ink-deep: #111827;
  --ink-line: rgba(8, 8, 8, 0.18);
  --ink-line-strong: rgba(8, 8, 8, 0.34);
  --ink-wash-light: rgba(8, 8, 8, 0.06);
  --ink-wash-mid: rgba(8, 8, 8, 0.14);
  --ink-wash-heavy: rgba(8, 8, 8, 0.30);
  --ink-accent: #111111;
  --ink-accent-dark: #000000;
  --ink-accent-soft: rgba(17, 17, 17, 0.10);
  --brush-font: "Microsoft YaHei", "PingFang SC", "Noto Serif SC", "Source Han Serif SC", "SimHei", sans-serif;
  --readable-font: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Noto Sans SC", sans-serif;
  color: var(--ink-black);
}

.analysis-shell.theme-ink .page-actions {
  padding: 7px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.92), rgba(241, 243, 244, 0.86)),
    radial-gradient(circle at 16% 18%, rgba(8, 8, 8, 0.10), transparent 34%);
  border: 1px solid var(--ink-line);
  border-radius: 8px;
  box-shadow:
    0 14px 34px rgba(8, 8, 8, 0.14),
    inset 0 0 0 1px rgba(255, 255, 255, 0.32);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
}

.analysis-shell.theme-ink .memory-label {
  color: var(--ink-stone);
  font-weight: 600;
}

.analysis-shell.theme-ink .config-button {
  color: var(--ink-deep);
  background: rgba(255, 255, 255, 0.78);
  border-color: var(--ink-line);
  box-shadow: none;
}

.analysis-shell.theme-ink .config-button:hover {
  color: var(--ink-black);
  background: rgba(255, 255, 255, 0.96);
  border-color: rgba(8, 8, 8, 0.34);
}

.analysis-shell.theme-ink .theme-switch :deep(.el-radio-button__inner) {
  padding: 7px 11px;
  color: var(--ink-stone);
  background: rgba(255, 255, 255, 0.64);
  border-color: var(--ink-line);
  box-shadow: none;
  font-weight: 650;
}

.analysis-shell.theme-ink .theme-switch :deep(.el-radio-button:first-child .el-radio-button__inner) {
  border-radius: 999px 0 0 999px;
}

.analysis-shell.theme-ink .theme-switch :deep(.el-radio-button:last-child .el-radio-button__inner) {
  border-radius: 0 999px 999px 0;
}

.analysis-shell.theme-ink .theme-switch :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) {
  color: #ffffff;
  background: var(--ink-accent);
  border-color: var(--ink-accent);
  box-shadow:
    -1px 0 0 0 var(--ink-accent),
    inset 0 0 0 1px rgba(255, 255, 255, 0.22);
}

.analysis-shell.theme-ink .hero {
  position: relative;
  isolation: isolate;
  color: var(--ink-black);
  background:
    radial-gradient(ellipse at 10% 10%, rgba(17, 24, 39, 0.040), transparent 30%),
    radial-gradient(ellipse at 90% 12%, rgba(17, 24, 39, 0.032), transparent 32%),
    radial-gradient(ellipse at 50% 106%, rgba(17, 24, 39, 0.026), transparent 45%),
    linear-gradient(180deg, #ffffff 0%, #fbfcfd 58%, #f4f6f8 100%);
  background-size: auto;
}

.analysis-shell.theme-ink .hero::before,
.analysis-shell.theme-ink .hero::after {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
}

.analysis-shell.theme-ink .hero::before {
  display: block;
  inset: -18px -22% auto;
  height: 146px;
  background:
    linear-gradient(176deg, transparent 0 38%, rgba(38, 70, 83, 0.104) 39%, rgba(38, 70, 83, 0.040) 50%, transparent 61% 100%),
    radial-gradient(ellipse at 13% 70%, rgba(38, 70, 83, 0.052), transparent 31%),
    radial-gradient(ellipse at 42% 56%, rgba(17, 46, 50, 0.046), transparent 27%),
    radial-gradient(ellipse at 63% 65%, rgba(38, 70, 83, 0.042), transparent 32%),
    radial-gradient(ellipse at 81% 55%, rgba(17, 46, 50, 0.042), transparent 28%),
    radial-gradient(ellipse at 94% 68%, rgba(38, 70, 83, 0.036), transparent 24%);
  clip-path: polygon(0 69%, 4% 61%, 8% 66%, 13% 55%, 18% 62%, 23% 47%, 29% 58%, 34% 42%, 39% 50%, 44% 31%, 49% 46%, 54% 57%, 59% 50%, 64% 61%, 69% 44%, 74% 54%, 79% 43%, 84% 52%, 89% 59%, 93% 47%, 96% 63%, 100% 55%, 100% 100%, 0 100%);
  filter: blur(0.75px);
  opacity: 0.78;
}

.analysis-shell.theme-ink .hero::after {
  display: block;
  inset: 10px -18% auto;
  height: 168px;
  background:
    linear-gradient(175deg, transparent 0 39%, rgba(38, 70, 83, 0.155) 40%, rgba(38, 70, 83, 0.070) 49%, transparent 57% 100%),
    radial-gradient(ellipse at 11% 70%, rgba(38, 70, 83, 0.078), transparent 34%),
    radial-gradient(ellipse at 37% 61%, rgba(17, 46, 50, 0.070), transparent 31%),
    radial-gradient(ellipse at 72% 59%, rgba(38, 70, 83, 0.072), transparent 32%),
    radial-gradient(ellipse at 91% 72%, rgba(17, 46, 50, 0.078), transparent 30%);
  clip-path: polygon(0 67%, 4% 62%, 9% 66%, 15% 58%, 22% 61%, 30% 43%, 38% 56%, 45% 50%, 53% 36%, 60% 47%, 67% 55%, 72% 52%, 76% 57%, 81% 48%, 85% 44%, 88% 48%, 91% 57%, 94% 62%, 97% 56%, 100% 61%, 100% 100%, 0 100%);
  filter: blur(0.45px);
  opacity: 0.84;
}

.analysis-shell.theme-ink .hero-main,
.analysis-shell.theme-ink .hotspot-arc {
  position: relative;
  z-index: 1;
}

.analysis-shell.theme-ink .hero-main::before {
  content: '';
  position: absolute;
  left: -18vw;
  top: -124px;
  width: min(720px, 62vw);
  height: 132px;
  background:
    linear-gradient(172deg, transparent 0 41%, rgba(17, 46, 50, 0.120) 42%, rgba(38, 70, 83, 0.052) 52%, transparent 60% 100%),
    radial-gradient(ellipse at 22% 66%, rgba(17, 46, 50, 0.072), transparent 34%),
    radial-gradient(ellipse at 70% 62%, rgba(38, 70, 83, 0.060), transparent 30%);
  clip-path: polygon(0 72%, 8% 62%, 17% 67%, 26% 49%, 37% 59%, 48% 42%, 58% 54%, 69% 47%, 78% 58%, 88% 53%, 100% 66%, 100% 100%, 0 100%);
  filter: blur(0.35px);
  opacity: 0.72;
  pointer-events: none;
  z-index: -1;
}

.analysis-shell.theme-ink .brand {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  position: relative;
  color: var(--ink-black);
  background: none;
  -webkit-text-fill-color: currentColor;
  background-clip: initial;
  font-family: var(--brush-font);
  font-size: clamp(46px, 5.6vw, 72px);
  font-weight: 900;
  letter-spacing: 0.045em;
  line-height: 1.05;
  text-shadow: none;
}

.analysis-shell.theme-ink .brand::before {
  content: '';
  position: absolute;
  left: 3%;
  right: 9%;
  bottom: -11px;
  height: 9px;
  background:
    radial-gradient(ellipse at 9% 52%, rgba(8, 8, 8, 0.22), transparent 64%),
    linear-gradient(90deg, transparent 0%, rgba(8, 8, 8, 0.18) 15%, rgba(8, 8, 8, 0.28) 43%, rgba(8, 8, 8, 0.12) 72%, transparent 100%);
  border-radius: 999px;
  filter: blur(0.25px);
  transform: skewX(-10deg);
  opacity: 0.68;
}

.analysis-shell.theme-ink .brand::after {
  display: none;
}

.analysis-shell.theme-ink .hero-sub {
  position: relative;
  color: var(--ink-stone);
  font-family: "Noto Serif SC", "Songti SC", "SimSun", serif;
  letter-spacing: 0.08em;
  margin-top: 12px;
  text-shadow: 0 1px 0 rgba(255, 255, 255, 0.52);
}

.analysis-shell.theme-ink .hero-sub::after {
  content: '';
  display: block;
  width: 86px;
  height: 1px;
  margin: 14px auto 0;
  background: linear-gradient(90deg, transparent, rgba(8, 8, 8, 0.34), transparent);
}

.analysis-shell.theme-ink .hero-search {
  position: relative;
  overflow: hidden;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.92), rgba(245, 246, 246, 0.86)),
    radial-gradient(circle at 4% 50%, var(--ink-accent-soft), transparent 12%);
  border: 1px solid var(--ink-line-strong);
  border-radius: 7px;
  box-shadow:
    0 18px 42px rgba(8, 8, 8, 0.16),
    inset 0 1px 0 rgba(255, 255, 255, 0.58),
    inset 0 -1px 0 rgba(8, 8, 8, 0.15);
}

.analysis-shell.theme-ink .hero-search::before {
  content: '';
  position: absolute;
  left: 14px;
  right: 14px;
  top: 8px;
  height: calc(100% - 16px);
  border-top: 1px solid rgba(8, 8, 8, 0.32);
  border-bottom: 1px solid rgba(8, 8, 8, 0.24);
  background:
    linear-gradient(90deg, rgba(8, 8, 8, 0.22), transparent 1px) left center / 1px 72% no-repeat,
    linear-gradient(90deg, transparent, rgba(8, 8, 8, 0.07), transparent);
  pointer-events: none;
}

.analysis-shell.theme-ink .hero-input :deep(.el-input__wrapper),
.analysis-shell.theme-ink .top-query-input :deep(.el-input__wrapper) {
  background: rgba(255, 255, 255, 0.74);
  border-radius: 5px;
  box-shadow: inset 0 0 0 1px rgba(8, 8, 8, 0.08);
}

.analysis-shell.theme-ink .hero-input :deep(.el-input__wrapper.is-focus),
.analysis-shell.theme-ink .top-query-input :deep(.el-input__wrapper.is-focus) {
  box-shadow:
    inset 0 0 0 1px rgba(8, 8, 8, 0.40),
    0 0 0 4px rgba(8, 8, 8, 0.08);
}

.analysis-shell.theme-ink .hero-input :deep(.el-input__inner),
.analysis-shell.theme-ink .top-query-input :deep(.el-input__inner) {
  color: var(--ink-deep);
  font-family: "Noto Serif SC", "Songti SC", "SimSun", serif;
}

.analysis-shell.theme-ink .hero-input :deep(.el-input__inner::placeholder),
.analysis-shell.theme-ink .top-query-input :deep(.el-input__inner::placeholder) {
  color: rgba(75, 85, 99, 0.62);
}

.analysis-shell.theme-ink .el-button--primary {
  color: #ffffff;
  background:
    radial-gradient(circle at 24% 22%, rgba(255, 255, 255, 0.22), transparent 28%),
    linear-gradient(135deg, #2a2a2a, var(--ink-accent-dark));
  border-color: var(--ink-accent);
  border-radius: 6px;
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, 0.26),
    inset 0 -4px 0 rgba(0, 0, 0, 0.20),
    0 10px 22px rgba(0, 0, 0, 0.18);
  font-family: var(--readable-font);
  font-weight: 700;
  letter-spacing: 0.08em;
  transition: transform 0.12s ease, box-shadow 0.12s ease, filter 0.12s ease;
}

.analysis-shell.theme-ink .el-button--primary:hover,
.analysis-shell.theme-ink .el-button--primary:focus {
  color: #ffffff;
  background:
    radial-gradient(circle at 24% 22%, rgba(255, 255, 255, 0.18), transparent 28%),
    linear-gradient(135deg, #1a1a1a, #000000);
  border-color: var(--ink-accent-dark);
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, 0.20),
    inset 0 -3px 0 rgba(0, 0, 0, 0.24),
    0 8px 18px rgba(0, 0, 0, 0.20);
  transform: translateY(1px) scale(0.995);
  filter: saturate(1.02);
}

.analysis-shell.theme-ink .el-button--primary:active {
  transform: translateY(2px) scale(0.98);
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, 0.18),
    inset 0 2px 8px rgba(0, 0, 0, 0.24),
    0 3px 8px rgba(0, 0, 0, 0.16);
}

.analysis-shell.theme-ink .el-button--primary.is-disabled,
.analysis-shell.theme-ink .el-button--primary.is-disabled:hover {
  color: rgba(255, 255, 255, 0.72);
  background: rgba(17, 17, 17, 0.46);
  border-color: transparent;
  box-shadow: none;
  transform: none;
}

.analysis-shell.theme-ink .hotspot-block {
  overflow: hidden;
  color: var(--ink-deep);
  background: #ffffff;
  border-color: rgba(17, 24, 39, 0.14);
  border-radius: 5px;
  box-shadow: 0 8px 18px rgba(17, 24, 39, 0.07);
}

.analysis-shell.theme-ink .hotspot-block::before {
  display: none;
}

.analysis-shell.theme-ink .hotspot-block::after {
  display: none;
}

.analysis-shell.theme-ink .hotspot-block:hover {
  border-color: rgba(17, 24, 39, 0.24);
  box-shadow: 0 10px 22px rgba(17, 24, 39, 0.10);
}

.analysis-shell.theme-ink .hotspot-block:hover::after {
  transform: none;
}

.analysis-shell.theme-ink .hotspot-title {
  position: relative;
  z-index: 2;
  color: var(--ink-deep);
  background: #f3f4f6;
  border-color: rgba(17, 24, 39, 0.14);
  border-radius: 4px;
  font-family: var(--readable-font);
  font-weight: 800;
  box-shadow: none;
}

.analysis-shell.theme-ink .hotspot-item {
  position: relative;
  z-index: 2;
  color: rgba(31, 41, 55, 0.86);
}

.analysis-shell.theme-ink .hotspot-item:hover {
  color: var(--ink-black);
  background: #f6f7f8;
}

.analysis-shell.theme-ink .hotspot-index {
  color: #ffffff;
  background:
    radial-gradient(circle at 28% 24%, rgba(255, 255, 255, 0.22), transparent 28%),
    var(--ink-accent);
  border-radius: 45% 55% 49% 51%;
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, 0.18),
    0 2px 8px rgba(0, 0, 0, 0.16);
}

.analysis-shell.theme-ink .hotspot-text {
  color: inherit;
}

.analysis-shell.theme-ink .analysis-page {
  background:
    radial-gradient(ellipse at 12% 8%, rgba(8, 8, 8, 0.10), transparent 30%),
    radial-gradient(ellipse at 88% 20%, rgba(8, 8, 8, 0.07), transparent 34%),
    linear-gradient(145deg, #ffffff 0%, #eef0f2 100%);
}

.analysis-shell.theme-ink .top-bar {
  background: rgba(255, 255, 255, 0.88);
  border-bottom-color: var(--ink-line);
  box-shadow: 0 8px 28px rgba(8, 8, 8, 0.08);
}

.analysis-shell.theme-ink .brand-mini {
  color: var(--ink-black);
  background: none;
  -webkit-text-fill-color: currentColor;
  background-clip: initial;
  font-family: var(--readable-font);
  font-weight: 800;
  letter-spacing: 0.08em;
}

.analysis-shell.theme-ink .stage-item,
.analysis-shell.theme-ink .post-reading-box,
.analysis-shell.theme-ink .result-wrap,
.analysis-shell.theme-ink .evidence-sidebar,
.analysis-shell.theme-ink .popover-card {
  background: rgba(255, 255, 255, 0.86);
  border-color: var(--ink-line);
  box-shadow: 0 14px 34px rgba(8, 8, 8, 0.09);
}

.analysis-shell.theme-ink .stage-msg,
.analysis-shell.theme-ink .section-main,
.analysis-shell.theme-ink .evidence-topic {
  color: var(--ink-deep);
}

.analysis-shell.theme-ink .result-wrap::before {
  height: 3px;
  background: linear-gradient(90deg, transparent, rgba(8, 8, 8, 0.82), rgba(128, 128, 128, 0.48), transparent);
}

.analysis-shell.theme-ink .sidebar-header {
  color: var(--ink-deep);
  background: rgba(255, 255, 255, 0.94);
  border-bottom-color: var(--ink-line);
}

.analysis-shell.theme-ink .evidence-card,
.analysis-shell.theme-ink .source-chip {
  background: rgba(255, 255, 255, 0.92);
  border-color: var(--ink-line);
}

.analysis-shell.theme-ink .evidence-card:hover,
.analysis-shell.theme-ink .source-chip:hover {
  border-color: rgba(8, 8, 8, 0.28);
  box-shadow: 0 8px 22px rgba(8, 8, 8, 0.10);
}

.analysis-shell.theme-ink .card-badge,
.analysis-shell.theme-ink .section-main :deep(.inline-citation) {
  color: var(--ink-black);
  background: var(--ink-accent-soft);
  border-color: rgba(8, 8, 8, 0.22);
}

.analysis-shell.theme-ink .section-main :deep(.inline-citation:hover) {
  color: #ffffff;
  background: var(--ink-accent);
  border-color: var(--ink-accent);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.20);
}

.analysis-shell.theme-ink .section-main :deep(h1) {
  color: var(--ink-black);
  font-family: var(--readable-font);
  font-weight: 800;
}

.analysis-shell.theme-ink .section-main :deep(h2) {
  color: var(--ink-deep);
  background: rgba(8, 8, 8, 0.06);
  border-left-color: rgba(8, 8, 8, 0.44);
}

.analysis-shell.theme-ink .section-main :deep(h3) {
  color: var(--ink-stone);
  border-left-color: rgba(8, 8, 8, 0.28);
}

.analysis-shell.theme-ink .section-main :deep(th) {
  color: var(--ink-deep);
  background: rgba(229, 231, 235, 0.72);
}

.analysis-shell.theme-ink .section-main :deep(th),
.analysis-shell.theme-ink .section-main :deep(td) {
  border-color: rgba(8, 8, 8, 0.14);
}

.analysis-shell.theme-ink .collapse-btn,
.analysis-shell.theme-ink .post-reading-item:last-child,
.analysis-shell.theme-ink .streaming-indicator {
  color: var(--ink-black);
}

@media (max-width: 900px) {
  .analysis-shell.theme-ink .page-actions {
    border-radius: 0;
    border-left: 0;
    border-right: 0;
    padding: calc(env(safe-area-inset-top, 0px) + 8px) 12px 8px;
  }

  .analysis-shell.theme-ink .theme-switch :deep(.el-radio-button__inner) {
    padding: 7px 9px;
  }

  .analysis-shell.theme-ink .brand {
    font-size: clamp(38px, 8vw, 58px);
  }

}

@media (max-width: 640px) {
  .analysis-shell.theme-ink .brand {
    flex-wrap: wrap;
    gap: 8px;
    font-size: clamp(32px, 10vw, 44px);
    letter-spacing: 0.08em;
  }

  .analysis-shell.theme-ink .page-actions {
    align-items: center;
    flex-wrap: wrap;
  }

  .analysis-shell.theme-ink .theme-switch {
    order: 3;
    width: 100%;
    display: flex;
    justify-content: center;
  }
}

@media (max-width: 900px) {
  .page-actions {
    position: sticky;
    top: 0;
    left: 0;
    right: 0;
    z-index: 220;
    width: 100%;
    justify-content: space-between;
    gap: 10px;
    padding: calc(env(safe-area-inset-top, 0px) + 8px) 14px 8px;
    background: rgba(255,255,255,0.88);
    border-bottom: 1px solid rgba(226,232,240,0.85);
    box-shadow: 0 2px 16px rgba(31,41,55,0.06);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }
  .page-actions.is-analysis {
    position: static;
  }
  .memory-toggle {
    min-width: 0;
    gap: 8px;
  }
  .memory-label {
    font-size: 13px;
  }
  .config-button {
    min-height: 32px;
    padding: 7px 10px;
  }
  .hero {
    height: auto;
    min-height: calc(100vh - 56px);
    min-height: calc(100dvh - 56px);
    overflow-y: auto;
    overflow-x: hidden;
    padding: 44px 14px 28px;
  }
  .hero-main {
    max-width: 640px;
  }
  .brand {
    font-size: clamp(38px, 7vw, 50px);
    letter-spacing: 1.5px;
    text-align: center;
  }
  .hero-sub {
    max-width: 580px;
    text-align: center;
    line-height: 1.6;
    margin-bottom: 28px;
    padding: 0 4px;
  }
  .hero-search {
    width: min(620px, 100%);
  }
  .hotspot-arc {
    width: 100%;
    max-width: 760px;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
    height: auto;
    margin-top: 28px;
    padding-bottom: 24px;
  }
  .hotspot-block,
  .hotspot-block:nth-child(n) {
    position: static;
    width: 100%;
    height: auto;
    transform: none;
    padding: 12px;
  }
  .hotspot-title {
    display: flex;
    width: max-content;
    max-width: 100%;
    margin: 0 auto 10px;
  }
  .hotspot-item {
    flex-basis: 38px;
    min-height: 38px;
    padding: 0 8px;
  }
  .top-bar-inner {
    max-width: none;
    flex-wrap: wrap;
    gap: 8px;
    padding: 10px 14px;
  }
  .brand-mini {
    width: 100%;
    text-align: center;
    font-size: 17px;
  }
  .top-query-input {
    order: 2;
    flex: 1 1 100%;
    max-width: none;
    min-width: 0;
  }
  .top-bar-inner > .el-button {
    order: 3;
    flex: 1 1 0;
    margin-left: 0;
  }
  .progress-area {
    max-width: none;
    padding: 14px 12px 0;
  }
  .stage-item {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 8px;
    padding: 8px 12px;
  }
  .stage-msg {
    flex: 1 1 calc(100% - 32px);
    line-height: 1.45;
  }
  .stage-progress {
    flex: 1 1 100%;
    width: 100%;
    margin-left: 28px;
  }
  .dashboard-layout {
    width: 100%;
    max-width: none;
    flex-direction: column;
    gap: 12px;
    margin: 16px auto;
    padding: 0 12px;
  }
  .result-wrap,
  .evidence-sidebar {
    width: 100%;
    border-radius: 14px;
  }
  .evidence-sidebar {
    position: static;
    max-height: none;
    overflow: visible;
  }
  .result-meta {
    align-items: stretch;
    flex-direction: column;
    padding: 16px 16px 0;
  }
  .meta-tags {
    gap: 6px;
  }
  .meta-tags :deep(.el-tag) {
    margin-left: 0 !important;
  }
  .meta-actions {
    width: 100%;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }
  .meta-actions .el-button {
    width: 100%;
    margin-left: 0;
    padding-left: 8px;
    padding-right: 8px;
  }
  .streaming-report,
  .report-sections {
    padding: 0 16px;
  }
  .section-row {
    display: block;
  }
  .section-main {
    font-size: 14.5px;
    line-height: 1.8;
  }
  .section-main :deep(h1) {
    font-size: 18px;
  }
  .section-main :deep(h2) {
    font-size: 15px;
  }
  .section-main :deep(h3) {
    font-size: 13.5px;
  }
  .chip-title {
    max-width: min(180px, calc(100vw - 92px));
  }
  .popover-card {
    width: min(260px, calc(100vw - 24px));
  }
  .post-reading-box {
    height: 96px;
    padding: 10px 12px;
  }
  .post-reading-box.is-collapsed {
    height: auto;
    min-height: 36px;
    padding: 8px 12px;
  }
  .post-reading-summary {
    font-size: 13px;
    text-align: center;
  }
  :deep(.el-dialog) {
    width: calc(100vw - 24px) !important;
    margin-top: 8vh !important;
  }
}

@media (min-width: 901px) and (max-height: 760px) {
  .hero {
    --hero-padding-top: 72px;
    --hero-padding-bottom: 28px;
  }
  .hero-sub {
    margin-bottom: 24px;
  }
  .hotspot-arc {
    width: min(1240px, 96vw);
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px;
    height: auto;
    margin-top: 24px;
  }
  .hotspot-arc.hotspot-count-3 {
    width: min(960px, 96vw);
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .hotspot-block,
  .hotspot-block:nth-child(n),
  .hotspot-arc.hotspot-count-3 .hotspot-block,
  .hotspot-arc.hotspot-count-3 .hotspot-block:nth-child(n) {
    position: static;
    width: 100%;
    height: auto;
    transform: none;
    transform-origin: center;
  }
}

@media (min-width: 901px) and (max-width: 1100px), (min-width: 901px) and (max-height: 680px) {
  .hero {
    --hero-padding-top: 64px;
  }
  .hero-sub {
    margin-bottom: 18px;
  }
  .hotspot-arc {
    width: min(760px, 94vw);
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    height: auto;
    margin-top: 18px;
  }
  .hotspot-arc.hotspot-count-3 {
    width: min(940px, 94vw);
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .hotspot-block,
  .hotspot-block:nth-child(n),
  .hotspot-arc.hotspot-count-3 .hotspot-block,
  .hotspot-arc.hotspot-count-3 .hotspot-block:nth-child(n) {
    position: static;
    width: 100%;
    height: auto;
    transform: none;
    transform-origin: center;
  }
  .hotspot-item {
    flex-basis: 30px;
    min-height: 30px;
  }
}

@media (max-width: 640px) {
  .page-actions {
    gap: 8px;
    padding: calc(env(safe-area-inset-top, 0px) + 7px) 10px 7px;
  }
  .brand {
    font-size: clamp(36px, 8.5vw, 40px);
    letter-spacing: 1px;
  }
  .hero-sub {
    font-size: 14px;
    line-height: 1.6;
    margin-bottom: 20px;
  }
  .hero-search {
    width: 100%;
    flex-direction: column;
    border-radius: 12px;
    padding: 10px;
  }
  .hero-btn {
    width: 100%;
  }
  .hotspot-arc {
    grid-template-columns: 1fr;
    width: 100%;
    max-width: 520px;
    gap: 12px;
    margin-top: 22px;
  }
  .hotspot-arc.hotspot-count-3 {
    width: 100%;
    max-width: 520px;
    grid-template-columns: 1fr;
  }
  .hotspot-block {
    height: auto;
    padding: 12px 10px;
  }
  .hotspot-arc.hotspot-count-3 .hotspot-block,
  .hotspot-arc.hotspot-count-3 .hotspot-block:nth-child(n) {
    position: static;
    width: 100%;
    height: auto;
    transform: none;
    transform-origin: center;
  }
  .hotspot-item {
    flex-basis: 40px;
    min-height: 40px;
  }
  .hotspot-text {
    font-size: 13.5px;
  }
  .top-bar-inner {
    padding: 9px 12px;
  }
  .top-bar-inner > .el-button {
    flex: 1 1 calc(50% - 4px);
  }
  .dashboard-layout {
    margin: 12px auto;
    padding: 0 10px;
  }
  .result-wrap {
    border-radius: 12px;
  }
  .result-meta {
    padding: 14px 14px 0;
  }
  .meta-actions {
    grid-template-columns: 1fr;
  }
  .streaming-report,
  .report-sections {
    padding: 0 14px;
  }
  .evidence-list {
    gap: 10px;
    padding: 12px;
  }
}

@media (max-width: 380px) {
  .page-actions {
    padding-left: 8px;
    padding-right: 8px;
  }
  .memory-toggle {
    gap: 6px;
  }
  .memory-label {
    font-size: 12px;
  }
  .config-button {
    padding: 6px 8px;
    font-size: 12px;
  }
  .brand {
    font-size: 32px;
  }
  .hero-sub {
    font-size: 13px;
  }
  .hotspot-title {
    font-size: 12px;
  }
  .hotspot-item {
    gap: 6px;
    padding: 0 4px;
  }
  .hotspot-index {
    width: 18px;
    height: 18px;
    line-height: 18px;
  }
  .section-main {
    font-size: 14px;
  }
}
</style>
