<template>
  <div>
    <!-- 固定右上角配置按钮 -->
    <div style="position:fixed;top:16px;right:20px;z-index:200">
      <el-button size="small" @click="configVisible=true">配置 Cookie</el-button>
    </div>

    <div v-if="!started" class="hero">
      <div class="brand">小红书舆情分析</div>
      <p class="hero-sub">基于小红书真实用户数据，快速生成产品口碑报告</p>
      <div class="hero-search">
        <el-input
          v-model="query"
          placeholder="输入产品名称，例如：iPhone 16 质量怎么样"
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

    <div v-else class="analysis-page">
      <div class="top-bar">
        <div class="top-bar-inner">
          <span class="brand-mini" @click="resetToHero" style="cursor:pointer">小红书舆情分析</span>
          <el-input
            v-model="query"
            size="default"
            clearable
            placeholder="输入产品关键词"
            @keyup.enter="startAnalysis"
            style="max-width:480px;flex:1"
          />
          <el-button type="primary" :loading="loading" :disabled="!query.trim()" @click="startAnalysis">
            {{ loading ? '分析中…' : '重新分析' }}
          </el-button>
          <el-button v-if="loading" @click="stopAnalysis">取消</el-button>
        </div>
      </div>

      <div v-if="loading || stages.length > 0" class="progress-area">
        <div class="progress-inner">
          <div v-for="s in stages" :key="s.stage" class="stage-item">
            <el-icon v-if="s.done" color="#67c23a"><CircleCheckFilled /></el-icon>
            <el-icon v-else class="is-loading" color="#409eff"><Loading /></el-icon>
            <span class="stage-msg">{{ s.message }}</span>
            <el-progress
              v-if="!s.done && s.stage === 'analyze'"
              :percentage="analyzeProgress"
              :striped="true"
              :striped-flow="true"
              :duration="8"
              style="width:120px;flex-shrink:0"
              :show-text="false"
            />
          </div>
        </div>
      </div>

      <div v-if="result" class="result-wrap">
        <div class="result-meta">
          <div class="meta-tags">
            <el-tag type="success">置信度 {{ (result.confidence_score * 100).toFixed(0) }}%</el-tag>
            <el-tag style="margin-left:8px">分析帖子 {{ result.screened_count }} 篇</el-tag>
            <el-tag style="margin-left:8px">分析评论 {{ result.comment_count }} 条</el-tag>
          </div>
          <div class="meta-actions">
            <el-button size="small" @click="copyMarkdown">复制 Markdown</el-button>
            <el-button size="small" @click="downloadWord">下载 Word</el-button>
            <el-button size="small" type="primary" @click="downloadPdf" :loading="pdfLoading">下载 PDF</el-button>
          </div>
        </div>

        <div class="report-sections">
          <div v-for="(sec, si) in sectionRows" :key="si" class="section-row">
            <div class="section-main" v-html="renderMd(sec.raw)" />
            <div class="section-aside" v-if="sec.refs.length">
              <div
                v-for="(ref, ri) in sec.refs"
                :key="ri"
                class="aside-ref-row"
                @mouseenter="showPopover(ref, $event)"
                @mouseleave="hidePopover"
              >
                <el-tag :type="sentimentTag(ref.sentiment)" size="small" style="flex-shrink:0">{{ ref.sentiment }}</el-tag>
                <a :href="ref.source_note_url" target="_blank" rel="noopener" class="aside-link">{{ truncateTitle(ref.source_title) }} ↗</a>
              </div>
            </div>
            <div class="section-aside" v-else />
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

    <Teleport to="body">
      <div
        v-if="popover.visible"
        class="popover-card"
        :style="{ top: popover.top + 'px', left: popover.left + 'px' }"
      >
        <div class="popover-topic">{{ popover.data.topic }}</div>
        <el-tag :type="sentimentTag(popover.data.sentiment)" size="small" style="margin-bottom:8px">{{ popover.data.sentiment }}</el-tag>
        <p
          v-for="(q, qi) in (popover.data.quotes || popover.data.evidence_quotes || []).slice(0, 3)"
          :key="qi"
          class="popover-quote"
        >{{ q }}</p>
        <div v-if="!(popover.data.quotes?.length || popover.data.evidence_quotes?.length)" class="popover-empty">暂无评论引用</div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, computed, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheckFilled, Loading } from '@element-plus/icons-vue'

const query = ref('')
const loading = ref(false)
const started = ref(false)
const stages = ref([])
const result = ref(null)
let evtSource = null
const analyzeProgress = ref(60)
let _analyzeTimer = null

const popover = reactive({ visible: false, top: 0, left: 0, data: {} })

const configVisible = ref(false)
const cookieInput = ref('')
let _cookieChecked = false

function saveCookie() {
  configVisible.value = false
  ElMessage.success('Cookie 已保存')
}

function resetToHero() {
  stopAnalysis()
  started.value = false
  result.value = null
  stages.value = []
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

  let run_id
  try {
    const resp = await fetch('/api/v1/analysis/product', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query.value.trim(), cookie: cookieInput.value.trim() || undefined }),
    })
    if (!resp.ok) {
      const err = await resp.json()
      throw new Error(err.detail || '启动分析失败')
    }
    const data = await resp.json()
    run_id = data.run_id
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

  evtSource.addEventListener('result', (e) => {
    result.value = JSON.parse(e.data)
    _markAllDone()
  })

  evtSource.addEventListener('error', (e) => {
    try {
      const d = JSON.parse(e.data)
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
  return text
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

function parseSections(markdown) {
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
      else sections[0].body += line + '\n'
    }
  }
  if (cur) sections.push(cur)
  return sections
}

const EXCLUDE_PATTERN = /结论|总结|综述|概述|局限|说明|注意/

// 计算每个 ref 与某个 section 的匹配分数（命中词数）
function matchScore(ref, sec) {
  const haystack = (sec.title + ' ' + sec.body).toLowerCase()
  const words = (ref.topic || '').split(/[\s，。、！？,.!?]+/).filter(w => w.length >= 2)
  return words.filter(w => haystack.includes(w.toLowerCase())).length
}

const sectionRows = computed(() => {
  if (!result.value) return []
  const refs = result.value.references || []
  const sections = parseSections(result.value.final_answer || '')

  // 全局去重：每个 ref 只分配给分数最高的那个 section
  // key = source_note_url + topic
  const refKey = r => (r.source_note_url || '') + '||' + (r.topic || '')
  const refBestSection = new Map() // refKey -> { sectionIdx, score }

  sections.forEach((sec, si) => {
    if (EXCLUDE_PATTERN.test(sec.title)) return
    refs.forEach(r => {
      if (!r.source_note_url) return
      const score = matchScore(r, sec)
      if (score === 0) return
      const key = refKey(r)
      const prev = refBestSection.get(key)
      if (!prev || score > prev.score) {
        refBestSection.set(key, { sectionIdx: si, score })
      }
    })
  })

  // 按 section 收集各自分配到的 refs
  const sectionRefs = sections.map(() => [])
  refBestSection.forEach(({ sectionIdx }, key) => {
    const ref = refs.find(r => refKey(r) === key)
    if (ref) sectionRefs[sectionIdx].push(ref)
  })

  return sections.map((sec, si) => ({ ...sec, refs: sectionRefs[si] || [] }))
})

function showPopover(ref, event) {
  const rect = event.currentTarget.getBoundingClientRect()
  popover.data = ref
  popover.top = rect.top
  popover.left = rect.left - 270
  if (popover.left < 8) popover.left = rect.right + 8
  popover.visible = true
}

function hidePopover() {
  popover.visible = false
}

function truncateTitle(title) {
  if (!title || title === '无标题') return '查看原帖'
  return title.length > 12 ? title.slice(0, 12) + '…' : title
}

function sentimentTag(s) {
  return s === '正面' ? 'success' : s === '负面' ? 'danger' : 'info'
}

const pdfLoading = ref(false)

async function copyMarkdown() {
  await navigator.clipboard.writeText(result.value.final_answer || '')
  ElMessage.success('已复制 Markdown')
}

async function downloadPdf() {
  pdfLoading.value = true
  try {
    const { default: html2canvas } = await import('html2canvas')
    const { default: jsPDF } = await import('jspdf')
    const html = renderMd(result.value.final_answer || '')
    const div = document.createElement('div')
    div.style.cssText = 'position:absolute;left:-9999px;top:0;width:800px;font-family:sans-serif;font-size:14px;line-height:1.8;padding:40px;background:#fff;color:#222'
    div.innerHTML = html
    document.body.appendChild(div)
    const canvas = await html2canvas(div, { scale: 2, useCORS: true, logging: false, backgroundColor: '#fff' })
    document.body.removeChild(div)
    const imgData = canvas.toDataURL('image/png')
    const pdf = new jsPDF({ unit: 'px', format: 'a4', orientation: 'portrait' })
    const pageW = pdf.internal.pageSize.getWidth()
    const pageH = pdf.internal.pageSize.getHeight()
    const imgH = (canvas.height * pageW) / canvas.width
    let left = imgH
    let pos = 0
    pdf.addImage(imgData, 'PNG', 0, pos, pageW, imgH)
    left -= pageH
    while (left > 0) {
      pos = left - imgH
      pdf.addPage()
      pdf.addImage(imgData, 'PNG', 0, pos, pageW, imgH)
      left -= pageH
    }
    pdf.save('report.pdf')
  } catch (e) {
    ElMessage.error('PDF 生成失败')
  } finally {
    pdfLoading.value = false
  }
}

async function downloadWord() {
  try {
    const { Document, Packer, Paragraph, TextRun, HeadingLevel } = await import('docx')
    const lines = (result.value.final_answer || '').split('\n')
    const children = lines.map(line => {
      if (line.startsWith('### ')) return new Paragraph({ text: line.slice(4), heading: HeadingLevel.HEADING_3 })
      if (line.startsWith('## ')) return new Paragraph({ text: line.slice(3), heading: HeadingLevel.HEADING_2 })
      if (line.startsWith('# ')) return new Paragraph({ text: line.slice(2), heading: HeadingLevel.HEADING_1 })
      if (line.startsWith('- ')) return new Paragraph({ text: line.slice(2), bullet: { level: 0 } })
      const parts = line.split(/\*\*(.+?)\*\*/)
      if (parts.length === 1) return new Paragraph({ text: line })
      const textRuns = parts.map((p, i) => new TextRun({ text: p, bold: i % 2 === 1 }))
      return new Paragraph({ children: textRuns })
    })
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
.hero {
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: #f8f5f0;
  background-image: radial-gradient(circle, #d4c9be 1px, transparent 1px);
  background-size: 24px 24px;
  padding: 0 20px 80px;
}
.brand {
  font-size: 36px;
  font-weight: 700;
  color: #c0392b;
  margin-bottom: 12px;
  letter-spacing: 3px;
}
.hero-sub {
  color: #888;
  font-size: 15px;
  margin-bottom: 36px;
}
.hero-search {
  display: flex;
  gap: 12px;
  width: min(640px, 90vw);
}
.hero-input { flex: 1; }
.hero-btn { white-space: nowrap; }
.analysis-page {
  min-height: 100vh;
  background: #fafafa;
  display: flex;
  flex-direction: column;
}
.top-bar {
  background: #fff;
  border-bottom: 1px solid #eee;
  position: sticky;
  top: 0;
  z-index: 100;
}
.top-bar-inner {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
  max-width: 860px;
  margin: 0 auto;
  width: 100%;
}
.brand-mini {
  font-size: 18px;
  font-weight: 700;
  color: #c0392b;
  white-space: nowrap;
}
.progress-area {
  max-width: 860px;
  margin: 0 auto;
  padding: 16px 20px 0;
  width: 100%;
}
.progress-inner {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.stage-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: #555;
}
.stage-msg {
  flex: 1;
}
.result-wrap {
  max-width: 860px;
  margin: 24px auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 2px 16px rgba(0,0,0,0.07);
  padding: 20px 28px;
}
.result-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 8px;
}
.meta-tags { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.meta-actions { display: flex; gap: 6px; }
.report-sections {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.section-row {
  display: flex;
  gap: 14px;
  align-items: flex-start;
}
.section-main {
  flex: 1;
  min-width: 0;
  line-height: 1.75;
  font-size: 15px;
  color: #333;
}
.section-main :deep(h1) {
  font-size: 20px;
  margin: 8px 0 4px;
  color: #111;
  text-align: center;
  font-weight: 700;
}
.section-main :deep(h2) {
  font-size: 16px;
  margin: 4px 0 2px;
  color: #222;
  border-left: 3px solid #c0392b;
  background: #fff5f5;
  padding: 3px 8px;
  border-radius: 0 4px 4px 0;
  font-weight: 600;
}
.section-main :deep(h3) {
  margin: 2px 0 2px;
  font-size: 14px;
  color: #444;
  font-weight: 600;
  padding-left: 10px;
  border-left: 2px solid #ddd;
}
.section-main :deep(ul) {
  margin: 4px 0;
  padding-left: 20px;
}
.section-aside {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 200px;
  overflow-y: auto;
}
.aside-ref-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.aside-link {
  color: #409eff;
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.aside-link:hover {
  text-decoration: underline;
}
.popover-card {
  position: fixed;
  z-index: 9999;
  background: #fff;
  border: 1px solid #eee;
  border-radius: 10px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.12);
  padding: 14px 16px;
  width: 260px;
  pointer-events: none;
}
.popover-topic {
  font-size: 13px;
  font-weight: 600;
  color: #333;
  margin-bottom: 6px;
}
.popover-empty {
  font-size: 13px;
  color: #aaa;
}
.config-tip {
  margin-top: 12px;
  font-size: 13px;
  color: #666;
  line-height: 1.7;
}
.config-img {
  margin-top: 12px;
  width: 100%;
  border-radius: 6px;
  border: 1px solid #eee;
}
</style>