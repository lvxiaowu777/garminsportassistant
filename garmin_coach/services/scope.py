"""话题边界：与运动/健康/训练无关的请求，直接给固定回复，不进模型。

和注入拦截的区别：
- 注入拦截：想改写规则 → 400
- 话题边界：请求本身合法，但不是这个产品的业务 → 200 + 固定拒答
"""
from __future__ import annotations

import re

#: 明确允许的问候 / 元问题（不触发拒答）
ALLOW = re.compile(
    r"^\s*(你好|您好|hi|hello|在吗|谢谢|多谢|你是谁|你能做什么|你会什么|怎么用|帮助|help)",
    re.IGNORECASE,
)

#: 明显不属于业务的请求
OFFTOPIC = [
    (r"(什么是|解释一下|介绍一下|讲讲|科普).{0,10}(redis|python|java|docker|linux|mysql|tcp|http|算法|数据结构|操作系统|计算机网络|k8s|git)", "技术概念"),
    (r"(写|帮我写|生成|来一段).{0,20}(代码|程序|脚本|爬虫|网页|作文|文案|简历|邮件)", "写作/编程"),
    (r"(翻译|译成|用英文说|英文怎么说)", "翻译"),
    (r"(讲个|来个|说个).{0,6}(笑话|段子|故事)", "闲聊"),
    (r"(股票|基金|币圈|彩票|新闻|电影|电视剧|游戏攻略|菜谱|做饭)", "无关领域"),
    (r"(数学题|解方程|微积分|高数|物理题|化学题)", "解题"),
    (r"(帮我|替我).{0,6}(写|做|订|查).{0,6}(作业|论文|PPT|机票|酒店)", "代办杂事"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in OFFTOPIC]


def find_offtopic(text: str) -> str | None:
    """命中返回类别，否则 None。"""
    raw = (text or "").strip()
    if not raw or ALLOW.match(raw):
        return None
    for pattern, label in _COMPILED:
        if pattern.search(raw):
            return label
    return None


def offtopic_reply() -> str:
    return (
        "这个问题不在我的范围内。我只负责你的训练、恢复、睡眠和身体数据——"
        "想聊今天跑不跑、最近练得怎么样、或者记一次训练，随时说。"
    )
