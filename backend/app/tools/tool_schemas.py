"""MCP 工具的 OpenAI Function Calling 格式 Schema 定义。"""

SEARCH_POSTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_posts",
        "description": "在小红书搜索帖子。根据关键词检索相关帖子列表，返回帖子基本信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，如：'iPhone16 评测'、'小米14 续航体验'",
                },
                "require_num": {
                    "type": "integer",
                    "description": "需要获取的帖子数量，建议 4~5",
                    "default": 5,
                },
                "sort_type": {
                    "type": "integer",
                    "enum": [0, 1, 2, 3, 4],
                    "description": "排序方式：0综合，1最新，2最多点赞，3最多评论，4最多收藏",
                    "default": 0,
                },
            },
            "required": ["keyword"],
        },
    },
}

FETCH_POST_DETAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_post_detail",
        "description": "拉取单篇帖子的详细内容（正文、标签、互动数据）。",
        "parameters": {
            "type": "object",
            "properties": {
                "note_url": {"type": "string", "description": "帖子 URL"},
            },
            "required": ["note_url"],
        },
    },
}

SEARCH_COMMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_comments",
        "description": "爬取指定帖子的用户评论列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "note_url": {"type": "string", "description": "帖子 URL"},
            },
            "required": ["note_url"],
        },
    },
}

# Retrieve Agent 使用的工具集（搜索帖子 + 拉取详情）
RETRIEVE_TOOLS = [SEARCH_POSTS_SCHEMA, FETCH_POST_DETAIL_SCHEMA]

# Analyze Agent 使用的工具集（爬取评论）
ANALYZE_TOOLS = [SEARCH_COMMENTS_SCHEMA]

# ---------------------------------------------------------------------------
# Orchestrator Agent 工具集（意图分析）
# ---------------------------------------------------------------------------

INTENT_ANALYSIS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "analyze_intent",
        "description": "分析用户查询意图，识别产品实体、用户需求等关键信息",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["product_comparison", "quality_issue", "price_value", "user_experience", "event_hotspot", "general"],
                    "description": "意图类型"
                },
                "product_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "主实体列表（canonical entities），用于长期记忆归档和实体区分。"
                        "只填写人物、品牌、产品、机构等稳定主体，不要填写搜索词、事件短语、活动名、热搜标题。"
                        "例如用户问'吴克群助农卖菜怎么样'，这里应填 ['吴克群']，"
                        "不要填 ['吴克群助农', '吴克群助农卖菜']；这些话题词应放入 key_aspects 或 search_hints。"
                    )
                },
                "key_aspects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关注方面，如：['续航', '拍照', '价格']"
                },
                "user_needs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户需求和痛点"
                },
                "intent_confidence": {
                    "type": "number",
                    "description": "意图置信度 0-1，默认 0.5",
                    "default": 0.5
                },
                "aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "主实体的别名或简称，不要把事件短语、活动名、热搜标题当作别名"
                },
                "rewritten_query": {
                    "type": "string",
                    "description": "优化后的查询语句"
                },
                "primary_entity": {
                    "type": "string",
                    "description": (
                        "唯一主实体名，必须从 product_entities 中选择一个稳定主体，"
                        "用于长期记忆和检索归档。不要填写事件短语或搜索关键词。"
                    )
                },
                "search_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "搜索提示词，可填写事件短语、活动名、热搜标题、同义搜索词"
                },
                "time_relevance": {
                    "type": "string",
                    "enum": ["recent", "evergreen"],
                    "description": "时间相关性"
                },
                "temporal_context": {
                    "type": "object",
                    "description": "轻量时间上下文，用于指导后续检索排序与内容时间分析",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["evergreen", "recent", "specific_range", "historical", "change_check"],
                            "description": "时间需求类型"
                        },
                        "window": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["none", "relative", "absolute"]},
                                "start_date": {"type": "string", "description": "YYYY-MM-DD，可为空"},
                                "end_date": {"type": "string", "description": "YYYY-MM-DD，可为空"},
                                "label": {"type": "string", "description": "用户可读时间范围"}
                            }
                        },
                        "retrieval_policy": {
                            "type": "string",
                            "enum": ["balanced", "latest_first", "comment_hot", "like_hot"],
                            "description": "检索排序策略"
                        },
                        "content_time_analysis": {
                            "type": "string",
                            "enum": ["auto", "required", "skip"],
                            "description": "是否生成内容时间分析"
                        },
                        "reason": {"type": "string", "description": "时间判断理由"}
                    }
                }
            },
            "required": ["intent", "product_entities", "key_aspects", "user_needs"]
        }
    }
}

INTENT_TOOLS = [INTENT_ANALYSIS_SCHEMA]

# ---------------------------------------------------------------------------
# Orchestrator Agent Action 节点工具集（热搜 + 更新决策）
# ---------------------------------------------------------------------------

HOT_TOPIC_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_hot_topics",
        "description": "查询各平台热搜榜单，获取与关键词相关的当前公众讨论热点。提供你无法从查询本身获知的当下舆论背景：包括相关的热搜话题、公众讨论方向、事件时效性。即使是明确的查询，热搜也可能揭示当下与该话题相关的最新讨论焦点。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，如实体名、事件关键词",
                },
                "platform": {
                    "type": "string",
                    "enum": ["douyin", "weibo", "toutiao", "baidu", "all"],
                    "description": "搜索的平台，默认 all 搜索全部平台",
                    "default": "all",
                },
            },
            "required": ["keyword"],
        },
    },
}

TRENDING_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_trending_list",
        "description": "获取指定平台的实时热搜榜单。当你需要了解当前公众正在讨论的热门话题以辅助意图分析时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": ["douyin", "weibo", "toutiao", "baidu"],
                    "description": "平台名称",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 20",
                    "default": 20,
                },
            },
            "required": ["platform"],
        },
    },
}

CURRENT_TIME_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "获取当前北京时间。用于解析用户查询中的今天、昨天、最近、近半年、发布初期等时间表达。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

UPDATE_DECISION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_intent_analysis",
        "description": "基于热搜工具的发现，决定是否更新意图分析的各个字段。每个更新都需要说明理由和来源。",
        "parameters": {
            "type": "object",
            "properties": {
                "should_update_intent": {
                    "type": "boolean",
                    "description": "是否需要修改意图分类",
                },
                "new_intent": {
                    "type": "string",
                    "enum": [
                        "product_comparison",
                        "quality_issue",
                        "price_value",
                        "user_experience",
                        "event_hotspot",
                        "general",
                    ],
                    "description": "如果需要修改，新的意图类型",
                },
                "intent_update_reason": {
                    "type": "string",
                    "description": "修改意图的理由（基于哪条热搜，为什么要改）",
                },
                "new_entities_to_add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "需要补充的主实体（来自热搜发现，不重复已有实体）。"
                        "只允许补充人物、品牌、产品、机构等稳定主体；"
                        "不要补充热搜标题、事件短语、活动名、搜索关键词。"
                        "例如已有实体为'吴克群'时，不要补充'吴克群助农卖菜'、'吴克群惠农专线'。"
                    ),
                },
                "new_aspects_to_add": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "aspect": {"type": "string"},
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "user_sentiment": {
                                "type": "string",
                                "enum": ["positive", "negative", "neutral"],
                            },
                            "source_hot_topic": {
                                "type": "string",
                                "description": "来源热搜标题",
                            },
                        },
                        "required": [
                            "aspect",
                            "priority",
                            "user_sentiment",
                            "source_hot_topic",
                        ],
                    },
                    "description": "需要补充的关注方面（来自热搜发现）",
                },
                "time_relevance": {
                    "type": "string",
                    "enum": ["recent", "evergreen", "no_change"],
                    "description": "时效性判断",
                },
                "temporal_context": {
                    "type": "object",
                    "description": "需要更新的轻量时间上下文，字段同 analyze_intent.temporal_context；无变化可省略",
                },
                "search_hints_to_add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "基于热搜发现补充的检索建议。热搜标题、事件短语、活动名应放在这里，而不是 new_entities_to_add",
                },
                "confidence_adjustment": {
                    "type": "number",
                    "description": "置信度调整值（-0.2到+0.3）",
                },
            },
            "required": [
                "should_update_intent",
                "time_relevance",
                "confidence_adjustment",
            ],
        },
    },
}

BAIDU_RELATED_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_baidu_related",
        "description": "查询百度相关搜索，获取与关键词相关的公众搜索方向。揭示大众在搜索某个实体时还关心什么维度，帮你发现查询实体的常见关联话题和隐含关注点。例如查询'iPhone 15'时，相关搜索可能显示'iPhone 15价格''iPhone 15续航'等，揭示用户可能未明说但公众普遍关注的方面。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，如实体名、产品名",
                },
            },
            "required": ["keyword"],
        },
    },
}

BAIDU_BAIKE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_baidu_baike",
        "description": "查询百度百科摘要，获取实体的权威定义和概述。帮你确认查询中实体的真实含义（消歧义），补充实体的上下文信息。例如'苹果'可能指公司也可能指水果，百科摘要可以帮你确认具体指代。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "查询关键词，如实体名",
                },
            },
            "required": ["keyword"],
        },
    },
}

# Action 节点可用工具：热搜 + 百度相关搜索 + 百度百科 + 更新决策
ORCHESTRATOR_TOOLS = [
    HOT_TOPIC_SEARCH_SCHEMA, TRENDING_LIST_SCHEMA, CURRENT_TIME_SCHEMA,
    BAIDU_RELATED_SEARCH_SCHEMA, BAIDU_BAIKE_SCHEMA,
    UPDATE_DECISION_SCHEMA,
]
