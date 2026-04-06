"""此文件仅供测试使用！！
    XHS_COOKIES = -1时可不进行爬虫，仅测试功能改动或llm生成效果"""

from __future__ import annotations
import random
import uuid

def _generate_fake_id() -> str:
    return uuid.uuid4().hex[:24]

def generate_mock_posts(query: str, require_num: int = 25) -> list[dict]:
    """生成用来“喂饱”RetrieveAgent的伪造帖子列表"""
    posts = []
    # 模拟真实搜索返回的不同类型标题格式
    title_templates = [
        "千万别买{query}，一万点吐槽！",
        "哇塞！{query}这么惊艳，属实没有想到啊",
        "{query}一周真实体验，后悔了吗？",
        "姐妹不开玩笑，真的很不建议你买{query}",
        "一图看懂：{query}到底升级了什么",
        "{query}值得买吗？测完我沉默了",
        "爆赞！{query}可以说是目前最强，没有之一",
        "纠结了很久，最终还是入手了{query}",
        "{query}避坑指南，必看！",
        "还在纠结{query}吗？看这一篇就够了"
    ]

    desc_templates = [
        "这几天深度体验了{query}，我想说它的性能其实还可以，但确实有发热的情况，推荐给不玩游戏的人。",
        "大家千万不要盲目跟风买{query}，我用了一周感觉续航完全达不到宣传的标准，有点小失望。",
        "哇真是挖到宝啦！用了{query}之后感觉其他的都不香了！拍照效果YYDS，绝绝子！",
        "作为一个数码爱好者，客观评价一下{query}，外观满分，系统流畅度90分，但价格略高。",
        "纯纯的拔草贴，谁买谁后悔，品控太差了，我这台边框还有缝隙！"
    ]

    for i in range(require_num):
        note_id = _generate_fake_id()
        title = random.choice(title_templates).format(query=query)
        desc = random.choice(desc_templates).format(query=query)

        posts.append({
            "note_id": note_id,
            "note_url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "title": title,
            "desc": desc,
            "like_count": random.randint(10, 10000),
            "comment_count": random.randint(5, 500),
            "display_title": title[:10] + "...",
            "collected_count": random.randint(1, 1000),
            "tags": [{"name": query}, {"name": "真实测评"}],
            "user": {
                "nickname": f"路人测试员_{random.randint(100, 999)}",
                "level": "非品牌",
                "is_brand": False,
                "avatar": "fake"
            }
        })
    return posts

def generate_mock_detail(note_url: str) -> dict:
    """生成单篇帖子大段图文内容的详细数据（为了过Screen质量检测）"""
    note_id = note_url.split("/")[-1] if "/" in note_url else note_url
    query = "产品"

    return {
        "note_id": note_id,
        "note_url": note_url,
        "title": f"关于{query}的深度干货分享！",
        "desc": f"这次真的要好好夸一夸（或者骂一骂）这件事。关于 {query}，很多人可能还停留在以前的印象中。\n而最近我深度使用了将近一个月的时间。无论是产品本身的续航、性能释放还是外观做工，其实都有一些值得被大家看到（或者吐槽）的地方。\n\n优点：\n1. 外观设计不错，属于第一眼就能抓住人的类型。\n2. 质感在线，拿在手里不会觉得廉价。\n\n缺点：\n1. 重量有点重，长时间单手握持会很累。\n2. 价格稍微有些虚高了，如果在双十一或者有大促的时候入手可能更划算点。\n\n总结：如果你是一个重度颜控用户，可以考虑；但如果是追求极致性价比的学生党，建议再等等市场降价或者选择其他竞品。\n(此文纯人工手打，不含任何合作广告)",
        "like_count": random.randint(100, 5000),
        "comment_count": random.randint(50, 300),
        "display_title": "深度测评细节",
        "collected_count": random.randint(10, 500),
        "tags": [{"name": "干货测评"}, {"name": "购买建议"}, {"name": "避坑指南"}],
        "user": {
            "nickname": f"资深买家_{random.randint(1000, 9999)}",
            "level": "普通",
            "is_brand": False,
            "avatar": ""
        }
    }

def generate_mock_comments(note_url: str, num_comments: int = 35) -> list[dict]:
    """生成“喂饱”AnalyzeAgent的评论争吵列表"""
    comments = []

    comment_templates = [
        # 正面
        "太棒了吧，看得我也想去买一台了！",
        "确实，我的也是昨天刚到，拍照绝绝子，爱了爱了❤️",
        "这颜值也太高了，本来想买别的，看了你的直接下单！",
        "我觉得挺好用的啊，你们咋那么多问题，估计运气不好吧。",
        "这个配色真的很难让人拒绝",
        # 负面
        "这博主肯定收钱了吧，这工业垃圾也吹？",
        "完全是智商税，续航拉胯得一批，别买别买！",
        "刚用两天就卡成PPT，这就退货去，气死我了。",
        "品控太差了，收到屏幕还有坏点，无语...",
        "价格定这么高谁给的勇气？隔壁一半钱配置都比这好。",
        # 中立或提问
        "请问玩原神的话发热严重吗？",
        "纠结要不要等下一代，毕竟首发感觉都在当小白鼠",
        "博主这个壳子是在哪里买的呀，求链接🥺",
        "想知道电池寿命大概多久，掉电快不快？",
        "重量如何？女孩子拿久了手酸不酸？"
    ]

    for i in range(max(num_comments, 35)):
        content = random.choice(comment_templates)
        comments.append({
            "comment_id": _generate_fake_id(),
            "content": content,
            "like_count": random.randint(0, 500),
            "nickname": f"网友_{random.randint(10000, 99999)}"
        })
    return comments
