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
                    "enum": ["product_comparison", "quality_issue", "price_value", "user_experience", "general"],
                    "description": "意图类型"
                },
                "intent_confidence": {
                    "type": "number",
                    "description": "意图分类的置信度，范围 0.0-1.0",
                    "minimum": 0.0,
                    "maximum": 1.0
                },
                "product_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "产品实体列表，如 ['iPhone 15', '华为Mate 60']"
                },
                "aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "产品别名或简称"
                },
                "entities_confidence": {
                    "type": "number",
                    "description": "实体识别的置信度，范围 0.0-1.0",
                    "minimum": 0.0,
                    "maximum": 1.0
                },
                "key_aspects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "aspect": {
                                "type": "string",
                                "description": "关注方面（中文，如：电池续航、相机拍照、价格性价比等）"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "重要程度"
                            },
                            "user_sentiment": {
                                "type": "string",
                                "enum": ["positive", "negative", "neutral"],
                                "description": "用户情感倾向"
                            }
                        },
                        "required": ["aspect", "priority", "user_sentiment"]
                    },
                    "description": "用户关注的核心方面"
                },
                "user_needs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户需求和痛点"
                },
                "rewritten_query": {
                    "type": "string",
                    "description": "优化后的查询语句（中文）"
                },
                "search_context": {
                    "type": "object",
                    "properties": {
                        "primary_entity": {"type": "string", "description": "主要实体名"},
                        "focus_aspects": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关注方面列表"
                        },
                        "search_hints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "搜索提示"
                        },
                        "time_relevance": {
                            "type": "string",
                            "enum": ["recent", "evergreen"],
                            "description": "时间相关性"
                        }
                    },
                    "required": ["primary_entity", "focus_aspects", "search_hints", "time_relevance"]
                }
            },
            "required": [
                "intent",
                "intent_confidence",
                "product_entities",
                "aliases",
                "entities_confidence",
                "key_aspects",
                "user_needs",
                "rewritten_query",
                "search_context"
            ]
        }
    }
}

INTENT_TOOLS = [INTENT_ANALYSIS_SCHEMA]
