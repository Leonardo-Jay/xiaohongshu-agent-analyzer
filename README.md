# 小红书产品舆情分析系统 / Xiaohongshu Product Sentiment Analysis

一个基于 **Vue 3 + Vite + FastAPI** 的小红书产品舆情分析系统。输入产品关键词后，系统会抓取小红书相关帖子与评论，生成结构化口碑分析报告。

A **Vue 3 + Vite + FastAPI** based sentiment analysis system for Xiaohongshu product discussions. Enter a product keyword, and the system retrieves related posts/comments and generates a structured analysis report.

---

## 中文说明

### 项目简介

本项目用于分析小红书平台上的产品口碑与用户观点，支持：

- 产品关键词搜索
- 小红书帖子抓取与筛选
- 评论抓取与情绪分析
- 生成结构化分析报告
- 前端展示分析过程与最终报告
- 导出 Markdown / Word / PDF 报告

### 技术栈

#### 前端
- Vue 3
- Vite
- Element Plus
- Vue Router
- Pinia

#### 后端
- FastAPI
- SSE（流式返回分析进度）
- MCP
- Python dotenv
- httpx / requests

#### 数据抓取
- `Spider_XHS-master`（小红书抓取相关能力）

---

### 项目结构

```text
my-vue3-vite-project/
├─ src/                    # 前端源码
├─ public/                 # 前端静态资源
├─ backend/                # FastAPI 后端
│  ├─ app/
│  ├─ mcp_server/
│  ├─ .env
│  └─ requirements.txt
├─ Spider_XHS-master/      # 小红书抓取依赖项目
├─ package.json            # 前端依赖
├─ index.html              # Vite 前端入口 HTML
└─ README.md
```

---

### 环境要求

- Node.js 18+
- Python 3.10+
- npm 9+

---

### 安装与运行

#### 1）安装前端依赖

```bash
npm install
```

#### 2）安装后端依赖

```bash
pip install -r backend/requirements.txt
```

#### 3）安装 Spider_XHS 依赖

```bash
cd Spider_XHS-master
npm install
```

#### 4）配置环境变量

直接在 `backend/.env` 中填写配置：

```env
XHS_COOKIES=你的小红书 Cookie
QIANFAN_BEARER_TOKEN=你的千帆 Token
QIANFAN_BASE_URL=https://qianfan.baidubce.com/v2/chat/completions
QIANFAN_MODEL=qwen3-14b
MCP_POOL_SIZE=2
```

> 说明：你后续会在仓库中提供一个示例版 `backend/.env`，其他开发者按示例填写即可。

#### 5）启动后端

```bash
cd backend
uvicorn app.main:app --reload
```

#### 6）启动前端

```bash
npm run dev
```

默认前端开发地址：

```text
http://localhost:8001
```

分析页面示例：

```text
http://localhost:8001/analysis
```

---

### 开源前注意事项

以下文件不要上传真实敏感内容：

- `backend/.env` 中的真实 Cookie / Token
- `node_modules/`
- `dist/`
- `.claude/`
- 本地虚拟环境目录（如 `.venv/`, `venv/`）

如果你要把 `backend/.env` 当示例文件上传到仓库，请先把其中的真实 Cookie 和 Token 全部替换成占位符。

---

## English

### Overview

This project is a Xiaohongshu product sentiment analysis system built with **Vue 3 + Vite + FastAPI**.
It searches Xiaohongshu posts by product keyword, retrieves comments, analyzes public opinion, and generates a structured report.

### Features

- Product keyword search
- Xiaohongshu post retrieval and filtering
- Comment fetching and sentiment analysis
- Structured report generation
- Frontend progress display with streamed updates
- Export as Markdown / Word / PDF

### Tech Stack

#### Frontend
- Vue 3
- Vite
- Element Plus
- Vue Router
- Pinia

#### Backend
- FastAPI
- SSE for streaming progress
- MCP
- Python dotenv
- httpx / requests

#### Crawling Layer
- `Spider_XHS-master`

---

### Project Structure

```text
my-vue3-vite-project/
├─ src/                    # frontend source code
├─ public/                 # static assets
├─ backend/                # FastAPI backend
│  ├─ app/
│  ├─ mcp_server/
│  ├─ .env
│  └─ requirements.txt
├─ Spider_XHS-master/      # Xiaohongshu crawling dependency
├─ package.json            # frontend dependencies
├─ index.html              # Vite frontend entry HTML
└─ README.md
```

---

### Requirements

- Node.js 18+
- Python 3.10+
- npm 9+

---

### Setup

#### 1) Install frontend dependencies

```bash
npm install
```

#### 2) Install backend dependencies

```bash
pip install -r backend/requirements.txt
```

#### 3) Install Spider_XHS dependencies

```bash
cd Spider_XHS-master
npm install
```

#### 4) Configure environment variables

Fill the configuration directly in `backend/.env`:

```env
XHS_COOKIES=your Xiaohongshu cookie
QIANFAN_BEARER_TOKEN=your Qianfan token
QIANFAN_BASE_URL=https://qianfan.baidubce.com/v2/chat/completions
QIANFAN_MODEL=qwen3-14b
MCP_POOL_SIZE=2
```

> Note: you plan to provide a sample `backend/.env` in the repository. Other developers can follow that template.

#### 5) Start backend

```bash
cd backend
uvicorn app.main:app --reload
```

#### 6) Start frontend

```bash
npm run dev
```

Default frontend development URL:

```text
http://localhost:8001
```

Analysis page example:

```text
http://localhost:8001/analysis
```

---

### Before Open Sourcing

Do not upload real sensitive values, especially:

- real cookies or tokens inside `backend/.env`
- `node_modules/`
- `dist/`
- `.claude/`
- local virtual environments such as `.venv/` or `venv/`

If you want to upload `backend/.env` as a sample file, replace all real cookies and tokens with placeholders first.
