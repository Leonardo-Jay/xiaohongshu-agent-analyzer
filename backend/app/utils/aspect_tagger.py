"""
观点簇的 Aspect 标签生成器（三层标签体系）

基于 Karpathy Wiki 理念：知识预编译
- 在分析阶段让 LLM 生成丰富的多层标签（主标签、子标签、同义标签）
- 检索阶段只做简单的字符串匹配
- 用 LLM 的语言理解能力替代 embedding 的语义搜索
"""
import json
import re
from typing import Any

from loguru import logger

from app.tools.llm import create_llm


def extract_json(text: str) -> dict:
    """
    从LLM响应中提取JSON对象（容错版本）

    处理以下情况：
    1. 纯JSON
    2. JSON被markdown代码块包裹（```json...```）
    3. JSON之后有额外文字（Extra data问题）
    4. 嵌套JSON对象

    Args:
        text: LLM返回的原始文本

    Returns:
        解析后的字典对象

    Raises:
        ValueError: 无法提取有效JSON对象
    """
    # 步骤1：尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 步骤2：尝试去除markdown代码块后解析
    cleaned_text = text.strip()
    if cleaned_text.startswith("```"):
        # 去除开头的 ```json 或 ```
        cleaned_text = cleaned_text.split("\n", 1)[1] if "\n" in cleaned_text else cleaned_text[3:]
    if cleaned_text.endswith("```"):
        cleaned_text = cleaned_text.rsplit("\n", 1)[0]

    try:
        return json.loads(cleaned_text)
    except json.JSONDecodeError:
        pass

    # 步骤3：尝试提取第一个完整的JSON对象（处理Extra data问题）
    # 使用花括号计数法找到最外层的完整JSON对象
    brace_count = 0
    start_idx = None
    for i, char in enumerate(cleaned_text):
        if char == '{':
            if start_idx is None:
                start_idx = i
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0 and start_idx is not None:
                json_str = cleaned_text[start_idx:i+1]
                try:
                    result = json.loads(json_str)
                    logger.debug(f"[AspectTagger] 成功提取JSON对象（长度: {len(json_str)} 字符）")
                    return result
                except json.JSONDecodeError as e:
                    logger.debug(f"[AspectTagger] 提取的JSON片段解析失败: {e}")
                    break

    # 步骤4：所有方法都失败，抛出异常
    raise ValueError(f"无法从响应中提取有效的JSON对象。响应前100字符: {text[:100]}")


class AspectTagger:
    """Aspect 标签生成器（三层标签体系）"""

    def __init__(self):
        self._llm = None

    async def generate_tags(
        self,
        clusters: list[dict[str, Any]],
        domain: str = "product"
    ) -> list[dict[str, Any]]:
        """
        为观点簇批量生成三层标签

        Args:
            clusters: 观点簇列表 [{"topic": "...", "sentiment": "...", "evidence_quotes": [...], ...}, ...]
            domain: 领域类型（product/event/general）

        Returns:
            带三层标签的观点簇列表
        """
        if not clusters:
            return []

        # 构造批量标注 Prompt
        prompt = self._build_tagging_prompt(clusters, domain)

        try:
            # 调用 LLM
            if self._llm is None:
                self._llm = create_llm(temperature=0.1)

            response = await self._llm.ainvoke(prompt)
            result_text = response.content.strip()

            # 调试：打印 LLM 原始响应
            logger.debug(f"[AspectTagger] LLM 原始响应: {result_text[:500]}")

            # 解析 JSON 响应（使用容错版本）
            result = extract_json(result_text)

            # 调试：打印解析后的结果
            logger.debug(f"[AspectTagger] 解析后的 JSON: {result}")

            # 合并标签到观点簇
            tagged_clusters = []

            # 支持两种 JSON 格式：
            # 1. 字典格式：{"0": {...}, "1": {...}}
            # 2. 列表格式：[{...}, {...}]
            if isinstance(result, list):
                # 列表格式
                logger.debug("[AspectTagger] 检测到列表格式")
                for i, cluster in enumerate(clusters):
                    cluster_with_tags = cluster.copy()
                    tags = result[i] if i < len(result) else {}
                    cluster_with_tags["primary_aspects"] = tags.get("primary_aspects", [])
                    cluster_with_tags["sub_aspects"] = tags.get("sub_aspects", [])
                    cluster_with_tags["synonym_aspects"] = tags.get("synonym_aspects", [])
                    tagged_clusters.append(cluster_with_tags)
                    logger.debug(f"[AspectTagger] 观点簇 {i}: topic={cluster['topic']}, tags={tags}")
            elif isinstance(result, dict):
                # 字典格式
                logger.debug("[AspectTagger] 检测到字典格式")
                for i, cluster in enumerate(clusters):
                    cluster_with_tags = cluster.copy()
                    tags = result.get(str(i), {})
                    cluster_with_tags["primary_aspects"] = tags.get("primary_aspects", [])
                    cluster_with_tags["sub_aspects"] = tags.get("sub_aspects", [])
                    cluster_with_tags["synonym_aspects"] = tags.get("synonym_aspects", [])
                    tagged_clusters.append(cluster_with_tags)
                    logger.debug(f"[AspectTagger] 观点簇 {i}: topic={cluster['topic']}, tags={tags}")
            else:
                logger.warning(f"[AspectTagger] 未知的 JSON 格式: {type(result)}")
                # 降级：返回空标签
                for cluster in clusters:
                    tagged_clusters.append({
                        **cluster,
                        "primary_aspects": [],
                        "sub_aspects": [],
                        "synonym_aspects": []
                    })

            logger.info(f"[AspectTagger] 为 {len(clusters)} 个观点簇生成三层标签")
            return tagged_clusters

        except Exception as e:
            logger.warning(f"[AspectTagger] 标签生成失败，使用空标签: {e}")
            # 降级：返回空标签
            return [
                {
                    **cluster,
                    "primary_aspects": [],
                    "sub_aspects": [],
                    "synonym_aspects": []
                }
                for cluster in clusters
            ]

    def _build_tagging_prompt(
        self,
        clusters: list[dict[str, Any]],
        domain: str
    ) -> str:
        """构造标注 Prompt（三层标签）"""
        # 格式化观点簇列表
        clusters_text = []
        for i, cluster in enumerate(clusters):
            topic = cluster['topic']
            sentiment = cluster['sentiment']
            # 提取证据摘要（前3条）
            evidence_quotes = cluster.get('evidence_quotes', [])
            evidence_summary = "\n  ".join(evidence_quotes[:3]) if evidence_quotes else "无"

            clusters_text.append(
                f"{i}. 观点：{topic}\n"
                f"   情感：{sentiment}\n"
                f"   证据摘要：\n  {evidence_summary}"
            )

        clusters_formatted = "\n\n".join(clusters_text)

        prompt = f"""
你是一个专业的观点标签生成器。为观点簇生成多层标签，帮助用户快速检索。

## 观点簇列表
{clusters_formatted}

## 任务
为每个观点簇生成三层标签：

### 1. 主标签（1-2个）
- 核心关注点，用户最可能搜索的词
- 示例：游戏性能、续航、拍照、外观设计、价格性价比

### 2. 子标签（2-4个）
- 具体细节，帮助精确匹配
- 示例：流畅度、帧率、原神、王者荣耀、充电速度、快充

### 3. 同义标签（1-2个）
- 用户可能用的其他说法
- 示例：性能表现、游戏体验、电池续航、性价比

## 标注原则
1. 标签必须是中文
2. 标签要具体，避免过于宽泛（如"性能"太宽泛，应该是"游戏性能"）
3. 考虑用户的搜索习惯（如"打游戏"→"游戏性能"）
4. 同义标签要真正是同义词，不是相关词
5. 根据证据内容生成标签，不要凭空想象

## 输出格式（JSON）
返回 JSON 对象，key 是观点簇的索引（字符串），value 是三层标签：

```json
{{
  "0": {{
    "primary_aspects": ["游戏性能"],
    "sub_aspects": ["流畅度", "帧率", "原神"],
    "synonym_aspects": ["性能表现", "游戏体验"]
  }},
  "1": {{
    "primary_aspects": ["续航"],
    "sub_aspects": ["电池", "待机时间"],
    "synonym_aspects": ["电池续航"]
  }},
  ...
}}
```

只返回 JSON，不要其他解释。
"""
        return prompt

    async def generate_tags_for_single_cluster(
        self,
        topic: str,
        sentiment: str,
        evidence_quotes: list[str] | None = None,
        domain: str = "product"
    ) -> dict[str, list[str]]:
        """
        为单个观点簇生成三层标签（用于增量更新）

        Args:
            topic: 观点主题
            sentiment: 情感倾向
            evidence_quotes: 证据引用列表
            domain: 领域类型

        Returns:
            三层标签字典 {"primary_aspects": [...], "sub_aspects": [...], "synonym_aspects": [...]}
        """
        cluster = {
            "topic": topic,
            "sentiment": sentiment,
            "evidence_quotes": evidence_quotes or []
        }
        result = await self.generate_tags([cluster], domain)
        if result:
            return {
                "primary_aspects": result[0].get("primary_aspects", []),
                "sub_aspects": result[0].get("sub_aspects", []),
                "synonym_aspects": result[0].get("synonym_aspects", [])
            }
        return {"primary_aspects": [], "sub_aspects": [], "synonym_aspects": []}


# 全局实例
_aspect_tagger: AspectTagger | None = None


def get_aspect_tagger() -> AspectTagger:
    """获取 AspectTagger 单例"""
    global _aspect_tagger
    if _aspect_tagger is None:
        _aspect_tagger = AspectTagger()
    return _aspect_tagger
