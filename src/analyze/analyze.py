"""清洗、规则打标与聚合。

最小 demo 版本：把 SOP 的 Step 4-6 合并在一个模块里，内部按
clean / tag / aggregate 三层函数划分。要拆成独立文件时按这三条缝切。
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

import yaml

# 少于这个字数的评论直接丢弃，通常是「good」「666」这类无信息量内容
MIN_TEXT_LENGTH = 4
MAX_EXAMPLES_PER_CATEGORY = 3
MAX_SAMPLE_REVIEWS = 5

# 噪音模式。第一条是实测遇到的游戏内反作弊提示，被玩家直接贴进评测；
# 其余是加群、代充一类广告（实测抓到的第一条 B 站评论就是加群广告）
_NOISE_PATTERNS = [
    r"为有效防范与检测外挂行为",
    r"虚拟化技术\s*[（(]?\s*VT-?X",
    r"[Qq]{2}群\s*[:：]?\s*\d{5,}",
    r"加群\s*\d{5,}",
    r"微信\s*[:：]?\s*[A-Za-z0-9_-]{5,}",
    r"https?://\S+",
    r"代充|代练|低价出售|充值优惠|优惠券",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.I)


def load_categories(taxonomy_path):
    """读取槽点分类体系，预编译成正则。"""
    with taxonomy_path.open(encoding="utf-8") as handle:
        taxonomy = yaml.safe_load(handle)

    categories = []
    for raw in taxonomy["categories"]:
        # 关键词里混有纯数字（如「648」），YAML 会解析成 int，统一转成字符串
        keywords = [str(k) for k in (raw.get("keywords") or [])]
        negatives = [str(n) for n in (raw.get("negatives") or [])]
        categories.append({
            "id": raw["id"],
            "name": raw["name"],
            "weight": raw.get("weight", 1.0),
            "keyword_re": re.compile("|".join(re.escape(k) for k in keywords), re.I) if keywords else None,
            "negative_re": re.compile("|".join(re.escape(n) for n in negatives), re.I) if negatives else None,
        })
    return categories


def clean(reviews):
    """去重并剔除噪音。返回 (保留的评论, 丢弃原因计数)。"""
    seen = set()
    kept = []
    dropped = Counter()

    for review in reviews:
        text = (review.get("text") or "").strip()
        if len(text) < MIN_TEXT_LENGTH:
            dropped["过短"] += 1
            continue
        if _NOISE_RE.search(text):
            dropped["广告或噪音"] += 1
            continue

        key = review.get("review_id") or text
        if key in seen:
            dropped["重复"] += 1
            continue
        seen.add(key)

        review["text"] = re.sub(r"\s+", " ", text)
        kept.append(review)

    return kept, dict(dropped)


def tag(reviews, categories):
    """给每条评论打上命中的槽点类别，一条可命中多个类别。

    情绪极性直接取 voted_up（Steam 官方标注），不做情感分析。
    """
    for review in reviews:
        text = review["text"]
        hits = []
        for category in categories:
            if category["keyword_re"] is None:
                continue
            if not category["keyword_re"].search(text):
                continue
            if category["negative_re"] is not None and category["negative_re"].search(text):
                continue
            hits.append(category["id"])
        review["categories"] = hits
        review["sentiment"] = "pos" if review.get("voted_up") else "neg"
    return reviews


def _daily_series(reviews, tz, days):
    """按日聚合声量并补齐空缺日期，前端可直接画折线。"""
    buckets = Counter()
    for review in reviews:
        day = datetime.fromtimestamp(review["created_at"], tz).date()
        buckets[day] += 1

    today = datetime.now(tz).date()
    return [
        {"date": (today - timedelta(days=offset)).isoformat(),
         "count": buckets.get(today - timedelta(days=offset), 0)}
        for offset in range(days - 1, -1, -1)
    ]


def _anomaly_days(series, threshold):
    """日声量 z-score 超阈值即标记为拐点。样本太少时返回空。"""
    counts = [item["count"] for item in series]
    if len(counts) < 5:
        return []
    deviation = pstdev(counts)
    if deviation == 0:
        return []
    average = mean(counts)
    return [
        item["date"]
        for item, count in zip(series, counts)
        if (count - average) / deviation > threshold
    ]


def _excerpt(review, length):
    text = review["text"]
    return text if len(text) <= length else text[: length - 1] + "…"


def _by_votes(reviews, count):
    return sorted(reviews, key=lambda item: item.get("votes_up") or 0, reverse=True)[:count]


def _as_sample(review, excerpt_length):
    return {
        "text": _excerpt(review, excerpt_length),
        "voted_up": review.get("voted_up"),
        "votes_up": review.get("votes_up"),
        "created_at": review.get("created_at"),
        "playtime_hours": review.get("playtime_hours"),
        "categories": review["categories"],
    }


def _category_rows(reviews, categories, excerpt_length):
    """按命中数 x 权重排序，并为每类挑出投票最高的代表评论。"""
    counts = Counter()
    grouped = defaultdict(list)
    for review in reviews:
        for category_id in review["categories"]:
            counts[category_id] += 1
            grouped[category_id].append(review)

    tagged_total = sum(counts.values())
    rows = []
    for category in categories:
        count = counts.get(category["id"], 0)
        if not count:
            continue
        rows.append({
            "id": category["id"],
            "name": category["name"],
            "count": count,
            "ratio": count / tagged_total if tagged_total else 0.0,
            "score": count * category["weight"],
            "examples": [
                _as_sample(item, excerpt_length)
                for item in _by_votes(grouped[category["id"]], MAX_EXAMPLES_PER_CATEGORY)
            ],
        })
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows, tagged_total


def aggregate_game(game, reviews, categories, settings):
    """聚合单款游戏，产出前端直接可用的结构。"""
    tz = ZoneInfo(settings["output"]["timezone"])
    digits = settings["output"]["round_digits"]
    window_days = settings["collect"]["window_days"]
    excerpt_length = settings["analyze"]["excerpt_length"]

    total = len(reviews)
    positive = sum(1 for review in reviews if review["sentiment"] == "pos")
    positive_ratio = positive / total if total else 0.0
    tagged_reviews = sum(1 for review in reviews if review["categories"])

    rows, _ = _category_rows(reviews, categories, excerpt_length)
    for row in rows:
        row["ratio"] = round(row["ratio"], digits)
        row["score"] = round(row["score"], 1)

    daily = _daily_series(reviews, tz, window_days)
    negatives = [r for r in reviews if r["sentiment"] == "neg"]
    positives = [r for r in reviews if r["sentiment"] == "pos"]

    return {
        "slug": game["slug"],
        "name": game["name"],
        "source": game["source"],
        "steam_appid": game.get("steam_appid"),
        "window_days": window_days,
        "comment_count": total,
        "positive_count": positive,
        "negative_count": total - positive,
        "positive_ratio": round(positive_ratio, digits),
        # 值域 [-1, 1]，0 为中性
        "sentiment_score": round(positive_ratio * 2 - 1, digits),
        "tagged_ratio": round(tagged_reviews / total, digits) if total else 0.0,
        "daily": daily,
        "anomaly_days": _anomaly_days(daily, settings["analyze"]["anomaly_zscore"]),
        "categories": rows,
        "top_negative": [_as_sample(r, excerpt_length * 3)
                         for r in _by_votes(negatives, MAX_SAMPLE_REVIEWS)],
        "top_positive": [_as_sample(r, excerpt_length * 3)
                         for r in _by_votes(positives, MAX_SAMPLE_REVIEWS)],
    }
