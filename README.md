# 小红书产品舆情分析系统 / Xiaohongshu Product Sentiment Analysis

基于多Agent协作的小红书产品舆情分析系统。通过MCP协议封装外部数据源，利用大模型Function Calling实现自动化任务编排。输入产品关键词，自动抓取相关帖子与评论，生成结构化口碑分析报告。

A multi-agent sentiment analysis system for Xiaohongshu product discussions. Uses MCP protocol for external data sources and LLM function calling for automated task orchestration. Enter a product keyword to retrieve posts/comments and generate structured analysis reports.

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


### 🎨效果图
![image](https://github.com/Leonardo-Jay/xiaohongshu-agent-analyzer/blob/master/example.jpg))

### 数据抓取
- 小红书数据采集基于 [Spider_XHS](https://github.com/cv-cat/Spider_XHS) 项目实现，感谢大佬[@cv-cat](https://github.com/cv-cat) 的开源贡献。

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
### 注意⚠️本项目仅供学习交流使用，任何涉及数据注入的操作都是不被允许的，如有违反，后果自负。

