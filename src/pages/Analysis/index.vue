<template>
  <div>
    <!-- 固定右上角配置按钮 -->
    <div style="position:fixed;top:16px;right:20px;z-index:200">
      <el-button @click="configVisible=true">配置 Cookie</el-button>
    </div>

    <div v-if="!started" class="hero">
      <div class="brand">小红书舆情分析</div>
      <p class="hero-sub">基于小红书真实用户数据，快速生成产品口碑、热点舆情、事件舆论等报告</p>
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
            style="max-width:480px;flex:1"
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
              <el-icon v-else class="is-loading" color="#1E3A8A"><Loading /></el-icon>
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
        <div class="evidence-sidebar" v-if="result && result.references && result.references.length > 0">
          <div class="sidebar-header">参考原文与评论</div>
          <div class="evidence-list">
            <a
              v-for="(ref, ri) in result.references"
              :key="ri"
              class="evidence-card"
              :href="ref.source_note_url" target="_blank" rel="noopener"
              :id="'ref-card-' + ri"
            >
              <div class="evidence-card-header">
                <span class="card-badge">{{ ri + 1 }}</span>
                <div class="evidence-topic">{{ ref.topic }}</div>
              </div>
              <div class="evidence-meta">
                <el-tag :type="sentimentTag(ref.sentiment)" size="small">{{ ref.sentiment }}</el-tag>
                <span class="evidence-source">{{ truncateTitle(ref.source_title) }}</span>
              </div>
              <div class="evidence-quotes" v-if="ref.quotes?.length || ref.evidence_quotes?.length">
                <div v-for="(q, qi) in (ref.quotes || ref.evidence_quotes || []).slice(0, 2)" :key="qi" class="evidence-quote-item">
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
import { ref, computed, reactive, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheckFilled, CircleCloseFilled, Loading } from '@element-plus/icons-vue'

const query = ref('')
const loading = ref(false)
const started = ref(false)
const stages = ref([])
const result = ref(null)
const reportBuffer = ref('')
let hasScrolledToReport = false

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
      body: JSON.stringify({ query: query.value.trim(), cookie: cookieInput.value.trim() || undefined }),
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
      else {
        sections[0].body += line + '\n'
        sections[0].raw += line + '\n'
      }
    }
  }
  if (cur) sections.push(cur)

  // 挂载引用序号角标
  const refs = result.value?.references || []
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
          const quotes = [...(ref.quotes || []), ...(ref.evidence_quotes || [])]
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
  return parseSections(result.value.final_answer || '')
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
  // 优先复制已完成的完整报告，如果报告还在生成中，复制当前的 buffer
  const content = reportBuffer.value || result.value?.final_answer || '';
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
/* ── Keyframes ── */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(20px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes slideDown {
  from { opacity: 0; transform: translateY(-16px); }
  to   { opacity: 1; transform: translateY(0); }
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
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #DBEAFE 0%, #F3F4F6 60%, #D1FAE5 100%);
  background-image: linear-gradient(135deg, #DBEAFE 0%, #F3F4F6 60%, #D1FAE5 100%),
    radial-gradient(circle, rgba(30,58,138,0.06) 1px, transparent 1px);
  background-size: auto, 28px 28px;
  padding: 0 20px 80px;
  animation: fadeInUp 0.6s ease both;
}
.brand {
  font-size: 38px;
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
</style>