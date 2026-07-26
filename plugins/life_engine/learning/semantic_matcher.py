"""语义匹配工具：改进的洞察强化匹配算法。

从简单字符重叠升级为语义级别的匹配，更准确地识别"同一模式在不同情境的复现"。
"""

from __future__ import annotations

import logging
import re
from typing import Any

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False

logger = logging.getLogger("life_engine.learning.semantic_matcher")

# 停用词：过滤掉这些词，提高匹配精度
STOP_WORDS = {
    # 中文停用词
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没",
    "看", "好", "自己", "这", "那", "什么", "可以", "这个", "现在", "知道",
    # 英文停用词
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "should", "could", "may", "might", "must", "can", "i", "you",
    "he", "she", "it", "we", "they", "my", "your", "his", "her", "its",
}


def tokenize_semantic(text: str) -> list[str]:
    """语义分词：支持中英混合，提取有意义的词。
    
    Args:
        text: 待分词文本
        
    Returns:
        词列表，已过滤停用词和单字
    """
    if not text or not text.strip():
        return []
    
    # 规范化：移除标点、统一小写
    text = re.sub(r'[^\w\s]', ' ', text)
    text = text.lower().strip()
    
    if not text:
        return []
    
    tokens: list[str] = []
    
    if JIEBA_AVAILABLE:
        # 使用 jieba 分词（中文友好）
        for word in jieba.cut(text):
            word = word.strip()
            if len(word) < 2:  # 过滤单字/单字母
                continue
            if word in STOP_WORDS:
                continue
            tokens.append(word)
    else:
        # 降级方案：按空格分词 + 中文按字
        for segment in text.split():
            segment = segment.strip()
            if not segment:
                continue
            # 判断是否为中文
            if any('一' <= c <= '鿿' for c in segment):
                # 中文：按双字提取
                for i in range(len(segment) - 1):
                    bigram = segment[i:i+2]
                    if bigram not in STOP_WORDS:
                        tokens.append(bigram)
            else:
                # 英文
                if len(segment) >= 2 and segment not in STOP_WORDS:
                    tokens.append(segment)
    
    return tokens


def semantic_overlap(text1: str, text2: str) -> float:
    """计算两段文本的语义重叠度。

    使用改进的 Jaccard 相似度：
    - 基于分词后的词集合
    - 加权：较长的共同词权重更高

    Args:
        text1: 文本1
        text2: 文本2

    Returns:
        重叠度 [0.0, 1.0]
    """
    tokens1 = set(tokenize_semantic(text1))
    tokens2 = set(tokenize_semantic(text2))

    if not tokens1 or not tokens2:
        return 0.0

    intersection = tokens1 & tokens2

    # 基础 Jaccard 相似度
    union = tokens1 | tokens2
    jaccard = len(intersection) / len(union) if union else 0.0

    # 加权：考虑共同词的长度（更长的词更有意义）
    if intersection:
        avg_common_len = sum(len(w) for w in intersection) / len(intersection)
        weight_bonus = min(0.2, (avg_common_len - 2) * 0.05)  # 每多一个字符加5%，最多20%
    else:
        weight_bonus = 0.0

    return min(1.0, jaccard + weight_bonus)


def extract_keywords(text: str, top_n: int = 5) -> list[str]:
    """提取文本关键词（按出现频率）。
    
    Args:
        text: 待提取文本
        top_n: 返回前 N 个关键词
        
    Returns:
        关键词列表
    """
    tokens = tokenize_semantic(text)
    if not tokens:
        return []
    
    # 统计词频
    freq: dict[str, int] = {}
    for token in tokens:
        freq[token] = freq.get(token, 0) + 1
    
    # 按频率排序
    sorted_tokens = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [token for token, _ in sorted_tokens[:top_n]]


def match_insight_pattern(
    claim1: str,
    claim2: str,
    *,
    topic1: str = "",
    topic2: str = "",
    same_topic_threshold: float = 0.5,
    diff_topic_threshold: float = 0.7,
) -> float:
    """匹配两条洞察是否为同一模式。
    
    针对洞察强化场景优化：
    - 同主题时用较低阈值（捕捉改述）
    - 不同主题时用较高阈值（避免误合并）
    
    Args:
        claim1: 洞察陈述1
        claim2: 洞察陈述2
        topic1: 洞察1的主题
        topic2: 洞察2的主题
        same_topic_threshold: 同主题时的阈值
        diff_topic_threshold: 不同主题时的阈值
        
    Returns:
        匹配度 [0.0, 1.0]，超过阈值则认为是同一模式
    """
    overlap = semantic_overlap(claim1, claim2)
    
    # 判断是否同主题
    both_have_topic = bool(topic1) and bool(topic2)
    if both_have_topic:
        if topic1 != topic2:
            # 明确不同主题：使用更严格阈值
            return overlap if overlap >= diff_topic_threshold else 0.0
        else:
            # 同主题：使用宽松阈值
            return overlap if overlap >= same_topic_threshold else 0.0
    else:
        # 至少一方无主题：使用中等阈值
        mid_threshold = (same_topic_threshold + diff_topic_threshold) / 2
        return overlap if overlap >= mid_threshold else 0.0


def format_insight_summary(claim: str, max_length: int = 60) -> str:
    """格式化洞察摘要（用于日志）。
    
    Args:
        claim: 洞察陈述
        max_length: 最大长度
        
    Returns:
        截断后的摘要
    """
    if len(claim) <= max_length:
        return claim
    return claim[:max_length-1] + "…"
