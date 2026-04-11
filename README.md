# 小红书多agent舆情分析系统

基于多Agent协作的小红书舆情分析系统。通过MCP协议封装外部数据源，利用大模型Function Calling实现自动化任务编排。实现输入热点事件、商品质量等查询自然语言，自动抓取相关帖子与评论，生成结构化舆情分析报告。

---

## 项目简介

本项目用于分析小红书平台上的产品口碑与用户观点，支持：

- 产品关键词搜索
- 小红书帖子抓取与筛选
- 评论抓取与情绪分析
- 生成结构化分析报告
- 前端展示分析过程与最终报告
- 导出 Markdown / Word / PDF 报告

### 效果图

![image](https://github.com/Leonardo-Jay/xiaohongshu-agent-analyzer/blob/master/example.jpg)

### 数据抓取

小红书数据采集基于 [Spider_XHS](https://github.com/cv-cat/Spider_XHS) 项目实现，感谢大佬[@cv-cat](https://github.com/cv-cat) 的开源贡献。

---

## 项目结构

```text
my-vue3-vite-project/
├─ src/                    # 前端源码
├─ public/                 # 前端静态资源
├─ backend/                # FastAPI 后端
│  ├─ app/
│  │  ├─ agents/          # Agent 模块（Orchestrator/Retrieve/Screen/Analyze/Synthesis）
│  │  ├─ memory/          # 记忆机制（Karpathy Wiki）
│  │  ├─ utils/           # 工具模块（标签生成、记忆检索等）
│  │  ├─ graph/           # 工作流图编排
│  │  └─ api/             # FastAPI 路由
│  ├─ mcp_server/         # MCP 服务器（小红书数据抓取）
│  ├─ data/               # 记忆数据存储（自动生成）
│  ├─ .env                # 环境变量配置
│  └─ requirements.txt
├─ Spider_XHS-master/      # 小红书抓取依赖项目
├─ package.json            # 前端依赖
├─ index.html              # Vite 前端入口 HTML
└─ README.md
```

---

## 记忆机制：基于 Karpathy Wiki 理念

本项目实现了基于 **Karpathy Wiki 理念**的知识预编译记忆系统，具有以下核心特性：

### 知识预编译（Knowledge Compilation）

- **分析阶段预处理**：LLM 在分析时生成丰富的三层标签（主标签、子标签、同义标签）
- **纯结构化存储**：不依赖向量数据库或 embedding，只存储结构化标签
- **快速检索**：检索时只需简单的字符串匹配，无需重新计算

### 三层标签体系

```python
# 观点簇示例
{
  "topic": "电池续航与充电速度",
  "primary_aspects": ["电池续航"],           # 主标签：核心关注点
  "sub_aspects": ["充电速度", "电池耐用性"],  # 子标签：具体细节
  "synonym_aspects": ["续航能力", "充电效率"] # 同义标签：用户其他说法
}
```

### 证据可追溯

- 每个观点簇关联 2-5 条真实用户评论作为证据
- 基于内容哈希去重，避免重复存储
- 支持从证据反查观点簇

### 智能复用策略

| 策略 | 覆盖率 | 行为 |
|------|--------|------|
| 完全复用（full） | ≥ 80% | 直接使用历史分析结果 |
| 增量更新（incremental） | 40-80% | 合并新旧观点簇 |
| 全新分析（none） | < 40% | 重新抓取分析 |

---

## 环境要求

- Node.js 18+
- Python 3.10+
- npm 9+

---

## 安装与运行

### 1）安装前端依赖（已包含 Spider_XHS 的签名依赖）

```bash
npm install
```

### 2）安装后端依赖（已包含 Spider_XHS 的 Python 依赖）

```bash
pip install -r backend/requirements.txt
```

### 3）配置环境变量

在 `backend/` 目录下创建 `.env` 文件：

```env
# ===== 小红书 Cookie =====
XHS_COOKIES=你的小红书 Cookie

# ===== LLM 提供商配置 =====
# 可选值: qianfan (默认) | longcat | modelscope
LLM_PROVIDER=qianfan

# ===== 千帆配置 (LLM_PROVIDER=qianfan 时使用) =====
QIANFAN_BEARER_TOKEN=你的千帆 Token
QIANFAN_BASE_URL=https://qianfan.baidubce.com/v2/chat/completions
QIANFAN_MODEL=ernie-4.5-21b-a3b

# ===== Longcat 配置 (LLM_PROVIDER=longcat 时使用) =====
# LONGCAT_BASE_URL=https://api.longcat.chat/openai/v1/chat/completions
# LONGCAT_MODEL=LongCat-Flash-Chat
# LONGCAT_API_KEY=你的 Longcat API Key

# ===== ModelScope 配置 (LLM_PROVIDER=modelscope 时使用) =====
# MODELSCOPE_BASE_URL=https://api-inference.modelscope.cn/v1
# MODELSCOPE_MODEL=MiniMax/MiniMax-M2.5
# MODELSCOPE_API_KEY=你的 ModelScope API Key

# ===== 其他配置 =====
MCP_POOL_SIZE=2  # MCP 连接池大小
ENABLE_MEMORY=false  # 是否默认开启记忆功能
```

### 4）启动后端

```bash
cd backend
uvicorn app.main:app --reload
```

### 5）启动前端

```bash
npm run dev
```

默认前端开发地址：`http://localhost:8001`

分析页面示例：`http://localhost:8001/analysis`

---

## 注意事项

本项目仅供学习交流使用，任何涉及数据注入的操作都是不被允许的，如有违反，后果自负。

---

<details>
<summary>English</summary>

# Xiaohongshu Multi-agent Sentiment Analysis

A Xiaohongshu public opinion analysis system based on multi-agent collaboration. It encapsulates external data sources through the MCP protocol and utilizes the large model Function Calling to implement automated task orchestration. It enables the input of natural language queries such as hot events and product quality, automatically retrieves relevant posts and comments, and generates structured public opinion analysis reports.

---

## Project Overview

This project is used to analyze product reputation and user opinions on the Xiaohongshu platform, supporting:

- Product keyword search
- Xiaohongshu post scraping and filtering
- Comment scraping and sentiment analysis
- Structured analysis report generation
- Frontend display of analysis process and final report
- Export to Markdown / Word / PDF reports

### Data Scraping

Xiaohongshu data collection is implemented based on the [Spider_XHS](https://github.com/cv-cat/Spider_XHS) project. Thanks to [@cv-cat](https://github.com/cv-cat) for the open source contribution.

---

## Memory Mechanism: Based on Karpathy Wiki Philosophy

This project implements a knowledge pre-compilation memory system based on **Karpathy Wiki philosophy**, with the following core features:

### Knowledge Compilation

- **Pre-processing during analysis**: LLM generates rich three-layer tags (primary, sub, synonym aspects)
- **Pure structured storage**: No dependency on vector databases or embeddings
- **Fast retrieval**: Simple string matching during retrieval

### Three-layer Tag System

```python
# Opinion cluster example
{
  "topic": "Battery Life and Charging Speed",
  "primary_aspects": ["Battery Life"],        # Primary: Core focus points
  "sub_aspects": ["Charging Speed", "Durability"],  # Sub: Specific details
  "synonym_aspects": ["Battery Duration", "Charging Efficiency"]  # Synonym: User variations
}
```

### Evidence Traceability

- Each opinion cluster is associated with 2-5 real user comments as evidence
- Content hash-based deduplication
- Support reverse lookup from evidence to opinion cluster

### Smart Reuse Strategy

| Strategy | Coverage | Behavior |
|----------|----------|----------|
| Full Reuse | ≥ 80% | Directly use historical analysis results |
| Incremental Update | 40-80% | Merge old and new opinion clusters |
| Fresh Analysis | < 40% | Re-scrape and analyze |

---

## Requirements

- Node.js 18+
- Python 3.10+
- npm 9+

---

## Installation & Running

### 1) Install Frontend Dependencies (includes Spider_XHS signing dependencies)

```bash
npm install
```

### 2) Install Backend Dependencies (includes Spider_XHS Python dependencies)

```bash
pip install -r backend/requirements.txt
```

### 3) Configure Environment Variables

Create a `.env` file in the `backend/` directory:

```env
# ===== Xiaohongshu Cookie =====
XHS_COOKIES=Your Xiaohongshu Cookie

# ===== LLM Provider Configuration =====
# Options: qianfan (default) | longcat | modelscope
LLM_PROVIDER=qianfan

# ===== Qianfan Config (when LLM_PROVIDER=qianfan) =====
QIANFAN_BEARER_TOKEN=Your Qianfan Token
QIANFAN_BASE_URL=https://qianfan.baidubce.com/v2/chat/completions
QIANFAN_MODEL=ernie-4.5-21b-a3b

# ===== Longcat Config (when LLM_PROVIDER=longcat) =====
# LONGCAT_BASE_URL=https://api.longcat.chat/openai/v1/chat/completions
# LONGCAT_MODEL=LongCat-Flash-Chat
# LONGCAT_API_KEY=Your Longcat API Key

# ===== ModelScope Config (when LLM_PROVIDER=modelscope) =====
# MODELSCOPE_BASE_URL=https://api-inference.modelscope.cn/v1
# MODELSCOPE_MODEL=MiniMax/MiniMax-M2.5
# MODELSCOPE_API_KEY=Your ModelScope API Key

# ===== Other Config =====
MCP_POOL_SIZE=2  # MCP connection pool size
ENABLE_MEMORY=false  # Enable memory feature by default
```

### 4) Start Backend

```bash
cd backend
uvicorn app.main:app --reload
```

### 5) Start Frontend

```bash
npm run dev
```

Default frontend dev address: `http://localhost:8001`

Analysis page: `http://localhost:8001/analysis`

---

## Disclaimer

This project is for learning and communication purposes only. Any operations involving data injection are not allowed. Users are responsible for any violations.

</details>
