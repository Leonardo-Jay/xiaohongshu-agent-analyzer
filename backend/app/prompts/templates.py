CLASSIFY_PROMPT = """
你是一名舆情分析专家，负责理解用户的查询意图。

用户查询: {query}

请分析此查询并以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "intent": "product_quality | price_value | comparison | event_hotspot | general",
  "product_entities": ["实体名1", "实体名2"],
  "aliases": ["别名1", "别名2"],
  "rewritten_query": "更清晰、更适合搜索的查询语句（中文）"
}}

intent 说明:
- product_quality: 用户在询问产品质量、使用体验、效果
- price_value: 用户在询问性价比、价格是否值得
- comparison: 用户在对比多个产品/选项
- event_hotspot: 用户在询问热点事件、社会话题、舆论焦点
- general: 其他通用查询
"""

REWRITE_PROMPT = """
你是一名搜索词优化专家，基于以下信息生成 3~5 个适合在小红书搜索的关键词。

用户原始查询: {query}
识别到的意图: {intent}
识别到的实体: {entities}
别名: {aliases}

要求:
1. 关键词为中文，贴近小红书用户的搜索习惯
2. 涵盖不同角度（如: 品牌词、品类词、体验词、事件词、对比词）
3. 不要重复原始查询

请以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "query_plan": ["搜索词1", "搜索词2", "搜索词3"]
}}
"""

EXPAND_PROMPT = """
你是一名搜索词优化专家。当前搜索结果不足，需要扩展搜索词。

用户原始查询: {query}
已使用的搜索词: {used_queries}
当前已获取帖子数: {post_count}

请生成 2~3 个新的搜索词，避免与已使用的重复，尽量从不同角度切入。

请以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "new_queries": ["新搜索词1", "新搜索词2"]
}}
"""

SCREEN_PROMPT = """
你是一名舆情分析师，负责筛选与用户查询最相关的小红书帖子。

用户查询: {query}

以下是搜索到的帖子列表（JSON）:
{posts_json}

请从中选出最多 8 篇与查询最相关、信息量最丰富的帖子。
判断标准：
1. 帖子内容直接涉及用户查询的话题/实体
2. 有实质性内容（评价、讨论、体验分享，非广告、非无效内容）
3. 互动数据较高（点赞/评论多）

请以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "selected_ids": ["note_id_1", "note_id_2", ...]
}}
"""

OPINION_PROMPT = """
你是一名舆情分析师，负责分析用户评论中的观点。

话题/实体: {query}
帖子标题: {title}
帖子描述: {desc}

以下是该帖子的评论列表（JSON）:
注意：其中 nickname 为「[博主]」的条目来自帖子正文，代表博主本人的观点，权重较高。
{comments_json}

请完成以下分析：
1. 将评论按观点主题聚类（最多 6 个主题）
2. 对每个主题进行情感分析（正面/负面/中立）
3. 提取代表性引用（原文，最多 2 句）

请以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "clusters": [
    {{
      "topic": "主题名称",
      "sentiment": "正面|负面|中立",
      "count": 评论数量,
      "evidence_quotes": ["引用1", "引用2"]
    }}
  ]
}}
"""

SYNTHESIS_META_PROMPT = """
你是舆情分析师，基于以下数据给本次分析打分。

用户查询: {query}
帖子数: {post_count}，评论数: {comment_count}
观点聚类: {clusters_json}

打分规则（不要过度保守）：
- 有帖子且有十条以上评论：confidence_score ≥ 0.6
- 只要有帖子/评论：confidence_score >0.4


只返回 JSON，不要其他文字：
{{"confidence_score": 0.0至1.0的数字, "limitations": "5个字说明"}}
"""

SYNTHESIS_REPORT_PROMPT = """
你是舆情分析师，请基于以下小红书用户数据撰写详实的分析报告。

用户查询: {query}
共分析帖子 {post_count} 篇，评论 {comment_count} 条。

观点聚类汇总（含用户原话）:
{clusters_json}

【报告格式要求】
- 直接返回 Markdown 文本，不要包在 JSON 里，不要加代码围栏
- 用 # 作为报告总标题
- 用 ## 划分主要章节：整体印象、正面反馈、负面反馈、争议点、总结
- 每个 ## 章节下用不多于3个的 ### 子标题对每个观点单独展开分析，每个观点至少 4~6 句话
- 篇幅限制：总字数在800~1400字之间
- 引用 1~2 句用户原话（来自 evidence_quotes），不要带证据引用字符
- 综合建议章节给出 3~4 条可操作的具体建议
"""
