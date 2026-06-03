CLASSIFY_PROMPT = """
你是一名舆情分析专家，负责深入理解用户的查询意图。

用户查询: {query}

请分析此查询并以 JSON 格式返回，只返回 JSON，不要其他文字：
{{
  "intent": "product_comparison | quality_issue | price_value | user_experience | general",
  "intent_confidence": 0.0至1.0的数字,
  "product_entities": ["稳定主实体1", "稳定主实体2"],
  "aliases": ["别名1", "别名2"],
  "entities_confidence": 0.0至1.0的数字,
  "key_aspects": [
    {{"aspect": "关注方面（中文，如：电池续航、相机拍照、价格性价比、游戏性能、发热散热等）", "priority": "high|medium|low", "user_sentiment": "positive|negative|neutral"}}
  ],
  "user_needs": ["用户需求1", "用户需求2", "用户需求3"],
  "rewritten_query": "更清晰、更适合搜索的查询语句（中文）",
  "search_context": {{
    "primary_entity": "主要稳定主实体名",
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

product_entities 说明:
- 只填写稳定主实体（人物、品牌、产品、机构），用于长期记忆归档
- 不要填写事件短语、活动名、热搜标题、搜索关键词；这些应放入 search_hints 或 key_aspects
- 示例：“吴克群助农卖菜怎么样” → product_entities=["吴克群"]，search_hints=["吴克群助农", "吴克群助农卖菜"]

search_context 说明:
- primary_entity: 主要稳定主实体名，必须来自 product_entities
- focus_aspects: 基于key_aspects提取的关键词，用于指导后续检索
- search_hints: 给检索Agent的建议，可包含事件短语、活动名、热搜标题、搜索词
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

以下是所有帖子正文/评论证据列表（共{comment_count}条）：
注意：source_type=post_body 或 nickname 为「[博主]」的条目来自帖子正文，代表博主本人的观点，权重较高。
{all_comments_json}

请完成以下分析：
1. 将评论按观点主题聚类，**必须输出 7 到 14 个不同的观点簇**
2. 对每个主题进行情感分析（正面/负面/中立）
3. 提取代表性引用（原文，最多 2 句）
4. 每个观点簇必须返回 evidence_ids，且只能使用输入列表里的 evidence_id，不能编造

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
      "evidence_ids": ["ev_001", "ev_002"],
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
      "evidence_ids": ["ev_001", "ev_002"],
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
   - 中间章节：必须设计 2~3 个核心分析章节，每个章节必须在 focus 中指明应使用 2~3 个子维度对观点进行下钻散开。
   - 末章：必须命名为总结或综合建议。
3. 中间章节必须通过 use_clusters 字段引用对应的簇编号（整数，如 [0, 1]），簇编号必须真实存在
4. 重要观点簇（count >= 2）原则上必须被引用
5. 中间章节标题必须是“洞察型判断句”，不能只是维度标签。标题应说明矛盾、原因、影响或购买决策含义。
   - 差标题：续航表现、品控问题、发热问题、使用体验、购买建议、核心问题分析
   - 好标题：续航争议本质是宣传预期与真实体验之间的落差
   - 好标题：品控瑕疵会把普通体验不满升级为整机可靠性怀疑
   - 好标题：发热问题削弱了性能优势在真实场景中的说服力
6. focus 不能只列关键词，必须写清每个主体章节下 2~3 个可展开的小论点；小论点也应是洞察型判断，不要写成“续航表现、品控问题”这类标签。

返回 JSON（只返回 JSON，不要其他文字）：
{{
  "report_strategy": {{
    "overall_tone": "正面推荐 | 负面预警 | 平衡客观",
    "structure": [
      {{
        "chapter": "章节标题。中间主体章节必须是洞察型判断句，例如：续航争议本质是宣传预期与真实体验之间的落差",
        "focus": "该章节的撰写重点。中间主体章节需明确 2~3 个洞察型小论点，每个小论点要说明现象、原因/影响和可引用的证据方向",
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

SYNTHESIS_REPORT_IR_PROMPT = """
你是专业的高级舆情分析师。请基于给定的紧凑数据生成 Report IR v1 JSON。

用户查询：{query}

【报告大纲】
{report_outline}

【紧凑报告上下文】
{report_context_json}

【硬性要求】
1. 只返回 JSON，不要 Markdown，不要解释文字，不要代码围栏。
2. 不要输出 metadata 和 citations 字段；这两个字段由后端确定性补齐。
3. 只能使用 context 中出现过的 cluster id 和 citation id，不能编造 id。
4. sections 至少包含：整体印象、2 个主体分析章节、总结或综合建议。
5. 每个 analysis 类型章节至少包含 1 个 citation_ids。
6. section.type 只能使用 overview、analysis、recommendation、risk、appendix 五种；总结/结论/建议章节统一使用 recommendation，绝对不要输出 conclusion 或 summary。
7. blocks 只使用 paragraph、subheading、list 三种 type。
8. chart.type 只使用 bar、pie、table 三种。
9. 段落文字要完整自然，不要出现“根据数据可知”这类空泛套话。
10. 所有 analysis 章节里的 subheading 必须是洞察型小标题，不能只写“续航表现、品控问题、发热问题、购买建议、使用体验”这种标签。小标题要说明矛盾、原因、影响或决策含义。
11. 普通 analysis 章节至少包含 2 个 subheading；每个 subheading 后必须至少跟 1 个 paragraph，且该 paragraph 不少于 120 个中文字符。“内容事件演化”章节可按真实事件数量输出，事件较少时允许 1 个 subheading，段落不少于 80 个中文字符。
12. 每个 analysis paragraph 必须完成“现象 → 用户原话/证据 → 影响/原因解释 → 对购买决策或品牌信任的含义”这条分析链路，不能只复述结论。
13. 每个 analysis paragraph 必须自然嵌入至少一句 context.citations 中的用户原话片段；不要只在段末堆 citation_ids。
14. 如果样本量很小，可以说明“当前样本中”，但仍然要展开影响机制，不要用一句话结束小节。
15. 总正文控制在 1400~2200 中文字，优先保证分析密度，不要为了缩短而压成一句话。
16. summary_cards 必须输出 4 条，并覆盖：整体倾向、核心风险、购买决策、样本限制/统计口径。每条 value 至少写成 1 句高密度判断，说明“现象 + 原因/证据 + 影响/决策含义”，绝对不要只写“负面偏多、正面偏多、分化明显”。
17. charts 最多输出 2 个；必须优先输出一个 table 类型的“舆情指标概览”，每行使用 label/value/insight 三列。数据概览不是情绪数量清单，必须解释统计口径、样本规模、核心议题、证据密度或正负面张力。
18. 如果情绪统计口径来自观点簇或模型归类，不要让数量看起来等同于帖子数/评论数；需要在 insight 中说明“作为倾向参考”。
19. 如果 context.content_time_analysis.available=true 且 events 非空，sections 必须在“整体印象”之后加入一个 analysis 类型章节，标题固定为“内容事件演化”。该章节只能基于 content_time_analysis.events 写作，事件小标题优先使用 event.title，段落必须引用 event.citation_ids 中至少一个 citation id。
20. 禁止在报告正文中使用：较早样本、中后段样本、爆发期、扩散期、沉淀期、复燃期、传播生命周期。内容时间分析只能写“先出现 / 随后扩展 / 后续转向 / 反向补充”或具体日期，不要自行判断传播生命周期。

【输出 JSON 结构】
{{
  "version": "1.0",
  "title": "报告标题",
  "summary_cards": [
    {{
      "label": "整体倾向",
      "value": "当前样本中负面反馈集中在续航和品控，说明用户不满不是单点抱怨，而是会影响购买信任的连续体验问题。",
      "supporting_cluster_ids": ["cl_0", "cl_1"]
    }},
    {{
      "label": "核心风险",
      "value": "续航落差与制造瑕疵会把普通体验不满放大成可靠性怀疑，需要优先判断这些问题是否覆盖自己的高频使用场景。",
      "supporting_cluster_ids": ["cl_0"]
    }},
    {{
      "label": "购买决策",
      "value": "若用户重视稳定续航和低发热体验，当前口碑更偏向谨慎观望；若只看外观或基础流畅度，则仍需结合价格权衡。",
      "supporting_cluster_ids": ["cl_2"]
    }},
    {{
      "label": "样本限制",
      "value": "本报告基于当前抓取样本与观点簇统计，适合判断舆论方向和主要风险，不宜直接等同于全网用户比例。",
      "supporting_cluster_ids": ["cl_0"]
    }}
  ],
  "sections": [
    {{
      "id": "sec_overview",
      "title": "整体印象",
      "type": "overview",
      "cluster_ids": ["cl_0", "cl_1"],
      "blocks": [
        {{
          "type": "paragraph",
          "text": "该章节正文",
          "citation_ids": ["cit_0"]
        }}
      ]
    }}
  ],
  "charts": [
    {{
      "id": "chart_overview",
      "type": "table",
      "title": "舆情指标概览",
      "data": [
        {{"label": "样本规模", "value": "6 篇帖子 / 6 条评论", "insight": "样本量偏小，适合识别风险方向，不适合推断全网占比。"}},
        {{"label": "主要负面议题", "value": "续航、品控、发热", "insight": "痛点集中在基础体验和可靠性，会直接影响购买信心。"}},
        {{"label": "情绪口径", "value": "负面占优", "insight": "基于当前观点簇与文本归类的倾向判断，数量只作为方向参考。"}}
      ]
    }}
  ]
}}
"""

CONTENT_TIME_ANALYSIS_PROMPT = """
你是一名内容演化分析师。请基于当前样本的时间顺序和观点簇，分析“内容表达如何变化”。

用户查询：{query}

时间上下文：
{temporal_context_json}

观点簇：
{clusters_json}

按时间/顺序压缩后的证据桶：
{evidence_buckets_json}

严格要求：
1. 只分析内容变化，不要判断传播生命周期。
2. 禁止使用：较早样本、中后段样本、爆发期、扩散期、沉淀期、复燃期、传播生命周期。
3. 事件数量 0 到 4 个；如果材料不足，available=false，events=[]。
4. 每个事件必须说明“先是什么表达，随后/后续变成什么表达”之一，不要只罗列观点。
5. 每个事件必须引用输入中真实存在的 cluster_ids 和 evidence_ids。
6. title 使用洞察型判断句，sequence_label 只能使用：先出现、随后扩展、后续转向、反向补充。

返回 JSON（只返回 JSON，不要解释文字）：
{{
  "available": true,
  "ordering_basis": "parsed_time | mixed_time_and_order | retrieval_order",
  "dominant_pattern": "多问题合流 | 情绪表达加重 | 购买劝退转向 | 正负观点分化 | 无明显演化",
  "events": [
    {{
      "id": "cte_1",
      "order": 1,
      "event_type": "topic_shift | sentiment_intensify | decision_shift | issue_convergence | counter_signal",
      "title": "续航落差先成为负面讨论入口",
      "sequence_label": "先出现",
      "summary": "用户最先集中表达的是续航达不到宣传预期，随后相关讨论开始影响购买信任。",
      "cluster_ids": ["cl_0"],
      "evidence_ids": ["ev_001", "ev_004"],
      "confidence": 0.78
    }}
  ],
  "limitations": []
}}
"""

SYNTHESIS_REPORT_IR_REPAIR_PROMPT = """
你需要修复一份 Report IR JSON，使其符合校验要求。

【校验问题】
{issues}

【允许使用的 cluster id】
{allowed_cluster_ids}

【允许使用的 citation id】
{allowed_citation_ids}

【枚举约束】
- section.type 只能是 overview、analysis、recommendation、risk、appendix。
- 总结/结论/建议章节统一使用 recommendation，不能使用 conclusion 或 summary。
- block.type 只能是 paragraph、subheading、list。
- chart.type 只能是 bar、pie、table。

【内容质量约束】
- analysis 章节标题和 subheading 必须是洞察型判断句，不能是“续航表现、品控问题、发热问题、购买建议、使用体验”等标签式标题。
- 普通 analysis subheading 后至少有 1 个不少于 120 个中文字符的 paragraph；“内容事件演化”章节按真实事件数量展开，事件较少时允许 1 个 subheading，段落不少于 80 个中文字符。
- analysis paragraph 必须自然嵌入至少一句用户原话，并解释这条证据为什么重要。
- 不要只输出“结论 + citation_ids”，要写出原因、影响、风险或购买决策含义。
- summary_cards 必须有 4 条，覆盖整体倾向、核心风险、购买决策、样本限制/统计口径；每条 value 至少 1 句完整判断，不能只写“负面偏多/正面偏多/分化明显”。
- charts 必须优先给出 table 类型的“舆情指标概览”，每行至少包含 label、value、insight；不要只列正面/负面/中立数量。
- 数据概览必须解释统计口径和样本限制，尤其在样本量小或情绪数量口径不等于帖子/评论总数时。
- 如果原报告缺少“内容事件演化”但上下文提供了可用的 content_time_analysis.events，必须补上该 analysis 章节；只能引用事件里的 citation_ids，不要自行脑补时间线。
- 禁止使用：较早样本、中后段样本、爆发期、扩散期、沉淀期、复燃期、传播生命周期。

【原始 JSON】
{report_ir_json}

请只返回修复后的 JSON，不要 Markdown，不要解释文字，不要代码围栏。
"""

SYNTHESIS_REPORT_IR_RAW_REPAIR_PROMPT = """
你需要把一次失败的 Report IR 原始输出转换成合法的 Report IR v1 JSON。

这不是重写 Markdown 报告，也不是解释失败原因；你的任务是尽量从原始输出中恢复结构化 JSON。如果原始输出里没有完整 JSON，也要基于给定上下文重新组织一份合法 JSON。

【解析错误】
{parse_error}

【允许使用的 cluster id】
{allowed_cluster_ids}

【允许使用的 citation id】
{allowed_citation_ids}

【报告上下文】
{report_context_json}

【原始输出统计】
raw_chars={raw_chars}

【原始输出前段】
{raw_prefix}

【原始输出后段】
{raw_suffix}

【硬性要求】
1. 只返回 JSON，不要 Markdown，不要解释文字，不要代码围栏。
2. 不要输出 metadata 和 citations 字段；后端会确定性补齐。
3. 只能使用允许列表中的 cluster id 和 citation id，不能编造 id。
4. section.type 只能是 overview、analysis、recommendation、risk、appendix。
5. block.type 只能是 paragraph、subheading、list；chart.type 只能是 bar、pie、table。
6. 普通 analysis 章节至少 2 个 subheading；“内容事件演化”章节按真实事件数量展开，事件较少时允许 1 个 subheading。
7. summary_cards 必须有 4 条，覆盖整体倾向、核心风险、购买决策、样本限制/统计口径。
8. charts 必须优先给出 table 类型的“舆情指标概览”，每行至少包含 label、value、insight。

请只返回修复后的 JSON。
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
  "product_entities": ["稳定主实体1", "稳定主实体2"],
  "aliases": ["别名1", "别名2"],
  "thought": "本轮推理过程：识别意图、实体，思考搜索策略"
}}

意图说明:
- product_quality: 询问产品质量、使用体验
- price_value: 询问性价比、价格是否值得
- comparison: 对比多个产品/选项
- event_hotspot: 询问热点事件、舆论焦点
- general: 其他通用查询

实体边界：
- product_entities 只放稳定主实体（人物、品牌、产品、机构）
- 事件短语、活动名、热搜标题、搜索词不要放入 product_entities
- 示例：“吴克群助农卖菜” → product_entities=["吴克群"]
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
  "product_entities": ["补充或修正后的稳定主实体"],
  "aliases": ["补充的别名"],
  "entities_confidence": 0.0至1.0的数字,
  "key_aspects": [
    {{"aspect": "补充的关注方面（中文，如：游戏性能、发热散热、续航表现等）", "priority": "high|medium|low", "user_sentiment": "positive|negative|neutral"}}
  ],
  "user_needs": ["补充的用户需求1", "补充的用户需求2"],
  "improvement_summary": "本轮改进的要点说明"
}}

注意：
- product_entities 用于长期记忆归档，只能是稳定主实体
- 不要把事件短语、活动名、热搜标题、搜索关键词当成实体
- 示例：“吴克群助农卖菜” 的实体是“吴克群”，不是“吴克群助农”
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
# Orchestrator Action 节点 Function Calling 专用 Prompt
# ---------------------------------------------------------------------------

ORCHESTRATOR_FC_PROMPT = """你是意图识别专家。你需要深入理解用户的查询意图，为后续检索和分析提供精准的方向。

用户查询：{query}
初步分析：实体={entities}，关注方面={aspects}，用户需求={needs}

字段边界：
- 实体字段只表示稳定主实体，用于长期记忆归档和跨轮区分。
- 人物/品牌/产品/机构才是实体；事件短语、活动名、热搜标题、搜索关键词不是实体。
- 如果用户查询是“人物 + 事件/活动”，实体应是人物名，事件/活动应进入关注方面或搜索提示。
- 示例：“吴克群助农卖菜怎么样” → 实体=吴克群；关注方面=助农活动、社会反响、实际效果；搜索提示=吴克群助农、吴克群助农卖菜、吴克群惠农专线。
- 热搜工具发现的新话题，优先补充到 search_hints_to_add 或 new_aspects_to_add，不要把完整热搜标题补进 new_entities_to_add。

你拥有以下工具，可以获取你作为语言模型无法获知的当下信息：

1. 热搜查询工具（search_hot_topics / get_trending_list）
   热搜数据来自抖音、微博、今日头条、百度等平台的实时榜单。
   它提供了当下公众正在讨论什么的关键信息。
   价值：
   - 发现与查询相关的当前讨论焦点（如查询"iPhone 15续航"时，热搜可能显示"iPhone 15续航翻车"）
   - 确认查询中实体在当前舆论中的含义和上下文（如"苹果"当前热搜是公司还是水果）
   - 判断查询话题的时效性
   - 发现用户未明说但与查询相关的热点方向

2. 百度相关搜索（search_baidu_related）
   获取与关键词相关的百度搜索推荐词。
   价值：
   - 揭示公众在搜索某实体时还关心什么维度（如搜"iPhone 15"会发现大众还关注"价格""续航""信号"）
   - 发现查询实体的常见关联话题和隐含关注点
   - 为检索方向提供更多关键词线索

3. 百度百科摘要（search_baidu_baike）
   获取实体的百科定义和概述。
   价值：
   - 消歧义：确认查询中实体的真实含义（如"苹果"是公司还是水果）
   - 补充实体的上下文信息（如了解"deepseek"是AI公司，帮助确认实体性质）

4. 当前时间工具（get_current_time）
   当用户查询包含“今天、昨天、最近、近期、近半年、发布初期、以前、现在还、变化”等时间表达时调用。
   价值：
   - 把相对时间转换为稳定日期窗口
   - 为 temporal_context.window 提供 start_date/end_date
   - 避免后续 Agent 依赖模型记忆中的当前日期

请根据你的判断决定是否调用工具、调用哪个工具、传入什么参数。
你可以在任何时候调用工具，只要你觉得这些信息有助于更准确地理解用户意图。"""

# ---------------------------------------------------------------------------
# Reasoning 节点专用 Prompt（含 intent 解释 + 字段引导）
# ---------------------------------------------------------------------------

REASONING_FC_PROMPT = """分析用户查询意图：{query}

意图类型说明：
- product_comparison: 产品对比、选购决策
- quality_issue: 质量问题、缺陷投诉
- price_value: 价格、性价比、优惠
- user_experience: 使用体验、功能评价
- event_hotspot: 社会事件、行业热点、融资并购等新闻事件
- general: 日常闲聊、无明确产品/事件导向

分析要求：
1. 准确判断意图类型
2. 提取稳定主实体和别名（至少1个别名）。实体用于长期记忆归档，不是搜索关键词列表
3. 识别所有关注方面（至少2-3个）
4. 深度提取用户需求（至少2-3条）
5. 生成优化后的检索查询
6. 生成轻量 temporal_context，用于后续检索排序与内容时间分析。
   - 日常口碑/泛体验：mode=evergreen, retrieval_policy=balanced
   - 最近/近期/今天/昨天/现在还：mode=recent 或 change_check, retrieval_policy=latest_first
   - 指定年份/月/日期：mode=specific_range, window.kind=absolute
   - 过去/以前/发布初期/上市初期：mode=historical, retrieval_policy=comment_hot
   - 默认 content_time_analysis=auto

实体边界规则：
- product_entities 只能放稳定主体：人物、品牌、产品、机构。
- 不要把事件短语、活动名、热搜标题、搜索词放入 product_entities。
- primary_entity 必须是 product_entities 中最核心的稳定主体。
- aliases 只放主实体的简称/别称，不放事件短语。
- search_hints 可以放事件短语、活动名、热搜标题和具体检索词。
- temporal_context 只保留 mode、window、retrieval_policy、content_time_analysis、reason 五类信息，不要输出额外字段。

示例：
- 查询“吴克群助农卖菜怎么样”
  product_entities=["吴克群"]
  primary_entity="吴克群"
  key_aspects=["助农活动", "社会反响", "实际效果", "公众反应"]
  search_hints=["吴克群助农", "吴克群助农卖菜", "吴克群惠农专线"]
- 查询“iPhone17 续航翻车吗”
  product_entities=["iPhone 17"]
  primary_entity="iPhone 17"
  key_aspects=["续航", "耗电", "用户反馈"]
  search_hints=["iPhone17续航", "iPhone17掉电", "iPhone17续航翻车"]
  temporal_context={{"mode":"recent","window":{{"kind":"none","start_date":"","end_date":"","label":"近期"}},"retrieval_policy":"latest_first","content_time_analysis":"auto","reason":"用户询问翻车，需要优先检索近期反馈"}}

请调用 analyze_intent 工具返回结构化的意图分析结果。"""

# ---------------------------------------------------------------------------
# Retrieve Agent 检索子图专用 Prompts
# ---------------------------------------------------------------------------

RETRIEVE_EXPAND_PROMPT = """
你是一名搜索词优化专家，当前搜索结果不足，需要扩展搜索词。

用户原始查询：{query}
识别到的意图：{intent}
搜索上下文：{search_context}
时间上下文：{temporal_context}
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
2. 根据时间上下文选择 sort_type：balanced 优先 0，可补充 3/1；latest_first 优先 1；comment_hot 优先 3；like_hot 优先 2
3. 观察搜索结果，判断是否需要继续搜索
4. 如果帖子数量已达到目标（{target_count} 篇），停止搜索，不要再调用工具
5. 每次搜索使用不同角度的关键词，避免重复

注意：
- 不要重复使用已使用的搜索词
- 每次调用 search_posts 只使用一个关键词
- search_posts 的 sort_type 必须符合时间上下文，不要所有调用都使用同一种排序
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
