CLASSIFY_PROMPT = """
你是一名舆情分析专家，负责深入理解用户的查询意图。

用户查询: {query}

请分析此查询并以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "intent": "product_comparison | quality_issue | price_value | user_experience | general",
  "intent_confidence": 0.0至1.0的数字,
  "product_entities": ["实体名1", "实体名2"],
  "aliases": ["别名1", "别名2"],
  "entities_confidence": 0.0至1.0的数字,
  "key_aspects": [
    {{"aspect": "关注方面（中文，如：电池续航、相机拍照、价格性价比、游戏性能、发热散热等）", "priority": "high|medium|low", "user_sentiment": "positive|negative|neutral"}}
  ],
  "user_needs": ["用户需求1", "用户需求2", "用户需求3"],
  "rewritten_query": "更清晰、更适合搜索的查询语句（中文）",
  "search_context": {{
    "primary_entity": "主要实体名",
    "focus_aspects": ["方面1", "方面2", "方面3"],
    "search_hints": ["搜索提示1", "搜索提示2"],
    "time_relevance": "recent|evergreen"
  }}
}}

intent 说明:
- product_comparison: 用户在对比多个产品/选项
- quality_issue: 用户在询问产品质量问题、故障、缺陷
- price_value: 用户在询问性价比、价格是否值得
- user_experience: 用户在询问使用体验、评价反馈
- general: 其他通用查询

key_aspects 说明:
- aspect: 用户关注的具体方面，必须用中文描述（如：电池续航、相机拍照、价格性价比、游戏性能、发热散热、系统流畅度、外观设计等）
- priority: 优先级（high高优先级、medium中等、low低优先级）
- user_sentiment: 用户情感倾向（positive正面、negative负面、neutral中立）

search_context 说明:
- primary_entity: 主要的产品/实体名称
- focus_aspects: 基于key_aspects提取的关键词，用于指导后续检索
- search_hints: 给检索Agent的建议，如"关注真实体验"、"查找对比数据"等
- time_relevance: recent（需要最新内容）或evergreen（长期有效内容）
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

请从中选出最多 10 篇最少 8 篇与查询最相关、信息量最丰富的帖子。
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

话题/实体：{query}

以下是所有帖子的评论列表（共{comment_count}条）：
注意：nickname 为「[博主]」的条目来自帖子正文，代表博主本人的观点，权重较高。
{all_comments_json}

请完成以下分析：
1. 将评论按观点主题聚类，**必须输出 7 到 14 个不同的观点簇**
2. 对每个主题进行情感分析（正面/负面/中立）
3. 提取代表性引用（原文，最多 2 句）

聚类要求：
- 必须输出至少 7 个簇，最多 14 个簇
- 每个簇代表一个独立的观点/话题/维度
- 可以从以下角度细分：产品性能、使用体验、外观设计、价格评价、售后服务、竞品对比等
- 避免过于宽泛的分类（如"好评"和"差评"）

请以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "clusters": [
    {{
      "topic": "主题名称（具体且有区分度，如'续航表现'而非'产品评价'）",
      "sentiment": "正面 | 负面 | 中立",
      "count": 该主题的评论数量，
      "evidence_quotes": ["引用 1", "引用 2"]
    }}
    // 必须包含 7~14 个簇
  ]
}}
"""

VALIDATE_CLUSTERS_PROMPT = """
你是一名舆情分析师，负责验证观点簇与用户意图的相关性。

用户意图：{intent}

核心关注方面：
{key_aspects}

用户需求：{user_needs}

以下是聚类后的观点簇列表：
{clusters_json}

请对每个观点簇进行相关性评分（0.0~1.0），并删除相关性分数 < 0.4 的观点簇。

评分标准：
- 0.7~1.0：高度相关，直接涉及用户意图和关注方面
- 0.4~0.7：中等相关，部分涉及用户需求
- 0.0~0.4：低相关或无关，与用户意图不符

只返回 JSON（不要其他文字）：
{{
  "clusters": [
    {{
      "topic": "保留的观点主题",
      "sentiment": "正面|负面|中立",
      "count": 评论数量,
      "evidence_quotes": ["引用 1", "引用 2"],
      "relevance_score": 0.8,
      "relevance_reason": "相关性说明（20 字以内）"
    }}
    // 只包含相关性分数 >= 0.4 的观点簇
  ]
}}
"""

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Synthesis Agent 报告生成专用 Prompts
# ---------------------------------------------------------------------------

SYNTHESIS_PLAN_OUTLINE_PROMPT = """
你是舆情分析报告的总编审，负责制定报告结构大纲。

用户查询：{query}
帖子数：{post_count}，评论数：{comment_count}
情感分布：{sentiment_summary}

以下是带编号的观点聚类详情（编号从 0 开始）：
{numbered_clusters_json}

请基于以上数据制定详细的报告大纲。要求：
1. 根据数据特点决定报告基调（正面推荐/负面预警/平衡客观）
2. 报告必须包含三种特定结构的章节：
   - 首章：必须命名为整体印象，不需要子维度。
   - 中间章节：设计 2~3 个核心分析章节，需在 focus 中指明应使用 2~3 个子维度对观点进行下钻散开。
   - 末章：必须命名为总结或综合建议。
3. 中间章节必须通过 use_clusters 字段引用对应的簇编号（整数，如 [0, 1]），簇编号必须真实存在
4. 重要观点簇（count >= 2）原则上必须被引用

返回 JSON（只返回 JSON，不要其他文字）：
{{
  "report_strategy": {{
    "overall_tone": "正面推荐 | 负面预警 | 平衡客观",
    "structure": [
      {{
        "chapter": "章节标题（例如：整体印象，核心问题，产品亮点等）",
        "focus": "该章节的撰写重点。如果是中间主体章节，需明确指出供主笔者展开的 2~3 个具体子维度",
        "use_clusters": [0, 1]
      }}
    ]
  }}
}}
"""

SYNTHESIS_REPORT_PROMPT = """
你是专业的高级舆情分析师，请严格按照以下制定好的【报告大纲】撰写详实的分析报告。

用户原始查询: {query}
共分析帖子 {post_count} 篇，评论 {comment_count} 条。

【报告大纲（执行纲领，必须严格遵循）】
{report_outline}

【原始观点聚类数据（含用户原话，供撰写时参考引用）】
{clusters_json}

【报告输出格式严格要求】
- 直接返回纯 Markdown 文本，不要用 ``` 包裹，不要输出任何额外的问候语
- 用 `# ` 作为最顶部的报告主标题
- 严格按照大纲中的 chapter 顺序写作为二级标题 `## `
- 第一章必须是 `## 整体印象`：直接用一段连续的文字进行总结分析，**不要使用任何 `### ` 子标题**。
- 最后一章必须是 `## 总结` 或 `## 综合建议`：直接给出具体的执行意见，**不需要子标题**。
- **中间的主体章节**（非首尾章节）：根据大纲汇总的数据，每个 `## ` 章节下必须包含 **2 到 3 个的 `### ` 子标题**进行具象化观点独立展开分析。
- 在每个主体分析的 `### ` 子标题下，要求至少写 2~3 句话进行细节阐述。
- 务必并在阐述中穿插引用 1~2 句用户的真实原话（从 evidence_quotes 提取，不要带引号外的任何生硬标志，自然融入句意）。
- 篇幅限制：总字数在 1000~1600 字之间为宜。
"""

SYNTHESIS_MODIFY_OUTLINE_PROMPT = """
你是一名舆情分析报告的总编审，需要根据审查反馈修改大纲。

以下是上一版大纲：
{previous_outline_json}

以下是审查反馈：
{feedback}

以下是修改原则：
1. **保留正确章节**：标记为"保留章节"的章节，**保持原样不动**，不要修改其标题、focus 和 use_clusters
2. **修正问题章节**：只修改标记为"需修改章节"的部分，确保簇编号在有效范围内
3. **补充遗漏观点**：将遗漏的观点添加到合适章节，但不要删除已有的正确章节
4. **避免重复犯错**：确保修改后不会再次出现相同问题

请返回修改后的完整大纲 JSON（只返回 JSON，不要其他文字）：
{{
  "report_strategy": {{
    "overall_tone": "正面推荐 | 负面预警 | 平衡客观",
    "structure": [
      {{
        "chapter": "章节标题",
        "focus": "该章节的撰写重点",
        "use_clusters": [0, 1]
      }}
    ]
  }}
}}
"""

# ---------------------------------------------------------------------------
# Orchestrator 提示词 ReAct 
# ---------------------------------------------------------------------------

REACT_REASONING_PROMPT = """
你是一名舆情分析专家，正在执行第 {round} 轮推理。

用户查询: {query}

请分析此查询并返回 JSON（只返回 JSON，不要其他文字）：
{{
  "intent": "product_quality | price_value | comparison | event_hotspot | general",
  "product_entities": ["实体名1", "实体名2"],
  "aliases": ["别名1", "别名2"],
  "thought": "本轮推理过程：识别意图、实体，思考搜索策略"
}}

意图说明:
- product_quality: 询问产品质量、使用体验
- price_value: 询问性价比、价格是否值得
- comparison: 对比多个产品/选项
- event_hotspot: 询问热点事件、舆论焦点
- general: 其他通用查询
"""

REACT_ACTION_PROMPT = """
你是一名搜索词优化专家，基于以下推理生成搜索词。

用户原始查询: {query}
意图: {intent}
实体: {entities}
别名: {aliases}

要求:
1. 生成 3~5 个适合在小红书搜索的关键词
2. 关键词为中文，贴近小红书用户搜索习惯
3. 涵盖不同角度（品牌词、品类词、体验词、事件词、对比词）
4. 不要重复原始查询

只返回 JSON（不要其他文字）：
{{
  "query_plan": ["搜索词1", "搜索词2", "搜索词3", "搜索词4", "搜索词5"]
}}
"""

# ---------------------------------------------------------------------------
# Orchestrator Agent 意图识别专用 Prompts
# ---------------------------------------------------------------------------

INTENT_ACTION_PROMPT = """
你是一名意图识别专家，正在执行第 {round} 轮深度意图分析。

用户原始查询: {query}
上一轮分析结果:
- 意图: {intent}
- 实体: {entities}
- 关注方面: {aspects}
- 用户需求: {needs}

请基于上一轮分析，从不同角度重新审视查询，补充缺失的分析维度。
重点关注：
1. 是否遗漏了隐含的用户需求
2. 是否可以优化意图分类的颗粒度
3. 是否需要补充关键方面的识别

只返回 JSON（不要其他文字）：
{{
  "intent": "优化后的意图类型",
  "intent_confidence": 0.0至1.0的数字,
  "product_entities": ["补充或修正后的实体"],
  "aliases": ["补充的别名"],
  "entities_confidence": 0.0至1.0的数字,
  "key_aspects": [
    {{"aspect": "补充的关注方面（中文，如：游戏性能、发热散热、续航表现等）", "priority": "high|medium|low", "user_sentiment": "positive|negative|neutral"}}
  ],
  "user_needs": ["补充的用户需求1", "补充的用户需求2"],
  "improvement_summary": "本轮改进的要点说明"
}}
"""

INTENT_OBSERVATION_PROMPT = """
你是一名意图识别质量评估专家，评估当前意图分析的质量。

当前分析结果:
- 意图: {intent}
- 意图置信度: {intent_confidence}
- 实体: {entities}
- 实体置信度: {entities_confidence}
- 关键方面: {aspects}
- 用户需求: {needs}

请评估当前分析的质量，返回 JSON（只返回 JSON，不要其他文字）：
{{
  "quality_dimensions": {{
    "intent_classification": {{"score": 0.0至1.0, "reason": "意图分类质量的说明"}},
    "entity_recognition": {{"score": 0.0至1.0, "reason": "实体识别完整性的说明"}},
    "need_extraction": {{"score": 0.0至1.0, "reason": "需求提取深度的说明"}}
  }},
  "intent_analysis_score": 0.0至1.0的综合质量分数,
  "missing_dimensions": ["缺失的分析维度1", "缺失的分析维度2"],
  "should_continue": true或false,
  "continue_reason": "如果应该继续，说明需要改进的方向"
}}

评分标准:
- intent_classification >= 0.7: 意图分类明确，不是general
- entity_recognition >= 0.6: 识别到至少一个实体
- need_extraction >= 0.6: 提取到至少1个用户需求

综合质量分数 >= 0.8 时，should_continue 为 false。
"""

# ---------------------------------------------------------------------------
# Retrieve Agent 检索子图专用 Prompts
# ---------------------------------------------------------------------------

RETRIEVE_EXPAND_PROMPT = """
你是一名搜索词优化专家，当前搜索结果不足，需要扩展搜索词。

用户原始查询：{query}
识别到的意图：{intent}
搜索上下文：{search_context}
已使用的搜索词：{used_keywords}
当前已获取帖子数：{current_post_count}
目标帖子数：{target_count}

请生成 2~3 个新的搜索词，要求：
1. 不能与已使用的搜索词重复
2. 结合搜索上下文中的 focus_aspects 和 search_hints 从不同角度切入
3. 关键词为中文，贴近小红书用户搜索习惯

只返回 JSON（不要其他文字）：
{{
  "new_keywords": ["新搜索词 1", "新搜索词 2"]
}}
"""

# ---------------------------------------------------------------------------
# Retrieve Agent Function Calling 专用 Prompt
# ---------------------------------------------------------------------------

RETRIEVE_FC_SYSTEM_PROMPT = """你是一名小红书舆情检索专家。你的任务是通过调用搜索工具，为用户查询收集足够的帖子。

用户查询：{query}
意图：{intent}
实体：{entities}
别名：{aliases}
搜索上下文：{search_context}
已使用的搜索词：{used_keywords}
当前已获取帖子数：{current_count}
目标帖子数：{target_count}

工作方式：
1. 根据查询意图，生成合适的搜索关键词，调用 search_posts 工具
2. 观察搜索结果，判断是否需要继续搜索
3. 如果帖子数量已达到目标（{target_count} 篇），停止搜索，不要再调用工具
4. 每次搜索使用不同角度的关键词，避免重复

注意：
- 不要重复使用已使用的搜索词
- 每次调用 search_posts 只使用一个关键词
- 当帖子数量足够时，直接结束（不要再调用工具）
"""

# ---------------------------------------------------------------------------
# Analyze Agent Function Calling 专用 Prompt
# ---------------------------------------------------------------------------

ANALYZE_FC_SYSTEM_PROMPT = """你是一名小红书评论分析专家。你的任务是通过调用工具爬取帖子评论，为后续观点聚类收集足够的评论数据。

用户查询：{query}
当前已获取评论数：{current_comment_count}
目标评论数：{target_comment_count}
当前轮次：{round_num}/{max_rounds}
本轮最多爬取帖子数：{max_posts_this_round}

可供爬取的帖子列表（按评论数和相关性排序）：
{posts_json}

工作方式：
1. 从帖子列表中选择评论数多、相关性高的帖子
2. 调用 search_comments 工具爬取该帖子的评论（传入 note_url）
3. 本轮最多爬取 {max_posts_this_round} 篇帖子，达到上限后停止
4. 每次只调用一个 search_comments

注意：
- 优先选择 comment_count 高的帖子
- 达到本轮爬取上限后直接结束，不要再调用工具
"""

# ---------------------------------------------------------------------------
# Screen Agent 筛选子图专用 Prompts
# ---------------------------------------------------------------------------

SCREEN_AD_DETECT_PROMPT = """
你是一名内容审核专家，检测小红书帖子是否为广告或软广。

帖子标题：{title}
帖子预览：{desc_preview}
标签：{tags}
互动数据：点赞{like} 评论{comment} 收藏{collect}

请判断：
1. 是否为硬广（直接推销、含购买引导、联系方式）
2. 是否为软广（隐性推广、过度赞美、模板化文案）
3. 是否为真实用户分享

返回 JSON（不要其他文字）：
{{
  "is_hard_ad": true/false,
  "is_soft_ad": true/false,
  "is_genuine_share": true/false,
  "confidence": 0.0~1.0,
  "reason": "判断依据，50 字以内"
}}
"""

SCREEN_RELEVANCE_PROMPT = """
你是一名舆情分析师，评估帖子与用户查询的相关性。

用户查询：{query}
意图类型：{intent}
核心关注方面：{key_aspects}
用户需求：{user_needs}

帖子标题：{title}
帖子预览：{desc_preview}
标签：{tags}

请评分并返回 JSON（不要其他文字）：
{{
  "relevance_score": 0.0~1.0,
  "matched_aspects": ["匹配的关注方面 1", "匹配的关注方面 2"],
  "reason": "相关性说明，50 字以内"
}}
"""
