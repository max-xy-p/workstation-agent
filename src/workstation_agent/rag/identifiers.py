"""生成文档、章节和片段的可重复标识，不访问存储。"""

from hashlib import sha256


def stable_id(prefix: str, *parts: str) -> str:
    """用分隔后的内容计算稳定标识，避免重复摄取产生新块。"""
    return prefix + "_" + sha256(chr(0).join(parts).encode("utf-8")).hexdigest()[:24]
