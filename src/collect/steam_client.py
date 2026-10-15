"""Steam 官方公开接口客户端。

优势（对应 docs/SOP.md）：
- 官方公开接口，无需任何凭据，不受出口 IP 风控影响
- 支持按语言筛选，language=schinese 可直接拿到中文玩家评测
- voted_up 字段是官方标注的好评/差评，情绪极性不需要再做情感分析

已知行为（实测）：
- 接口的 day_range 参数不生效，时间窗必须由本地按 timestamp_created 过滤
- filter=recent 按时间倒序返回，可据此在跨越时间窗边界时提前停止翻页
"""

import hashlib
import os
import random
import time

import requests

STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"

# 假名化用的盐。
#
# 注意这属于「假名化」而非「匿名化」：默认值是写死在代码里的公开常量，而 steamid
# 是可枚举的空间，所以对已知 ID 可以反查；同一作者的哈希又稳定，跨游戏可关联。
# 处理非公开内容时，请通过 AUTHOR_SALT 环境变量注入每次部署随机生成、不入库的盐。
_AUTHOR_SALT = os.environ.get("AUTHOR_SALT", "game-voc-radar")


def hash_author(steamid):
    if not steamid:
        return None
    return hashlib.sha256(f"{_AUTHOR_SALT}:{steamid}".encode()).hexdigest()[:12]


class SteamClient:
    def __init__(self, settings):
        collect = settings["collect"]
        self.timeout = collect["timeout"]
        self.delay_min = collect["delay_min"]
        self.delay_max = collect["delay_max"]
        self.max_retries = collect["max_retries"]
        self.backoff_base = collect["backoff_base"]
        self.window_days = collect["window_days"]
        self.contact = collect["user_agent_contact"]

        steam = collect["steam"]
        self.language = steam["language"]
        self.max_comments = steam["max_comments_per_game"]
        self.per_page = steam["reviews_per_page"]

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"game-voc-radar/0.1 (+{self.contact})",
            "Accept": "application/json",
        })
        self.request_count = 0

    # ---------- 底层 ----------

    def _throttle(self):
        if self.request_count:
            time.sleep(random.uniform(self.delay_min, self.delay_max))
        self.request_count += 1

    def _get(self, url, params):
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                else:
                    response.raise_for_status()
                    return response.json()
            except requests.RequestException as exc:
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(self.backoff_base * (2 ** (attempt - 1)))
        raise RuntimeError(f"请求 {url} 连续失败 {self.max_retries} 次: {last_error}")

    # ---------- 接口 ----------

    def find_appid(self, term, limit=5):
        """按名称检索 appid，用于维护 config/games.yaml。"""
        payload = self._get(STORESEARCH_URL, {"term": term, "cc": "cn", "l": "schinese"})
        return [
            {"appid": item["id"], "name": item["name"]}
            for item in (payload.get("items") or [])[:limit]
        ]

    def review_summary(self, appid):
        """总量与好评率。注意：这里返回的是全量统计，不是时间窗内的。"""
        payload = self._get(
            APPREVIEWS_URL.format(appid=appid),
            {"json": 1, "language": self.language, "num_per_page": 0, "purchase_type": "all"},
        )
        return payload.get("query_summary") or {}

    def fetch_reviews(self, appid, slug):
        """按时间倒序翻页抓取，直到跨越时间窗边界或达到条数上限。

        返回 (评论列表, 停止原因)。停止原因取值：window_edge / cap / exhausted。
        """
        cutoff = int(time.time()) - self.window_days * 86400
        cursor = "*"
        collected = []
        stop_reason = "exhausted"

        while len(collected) < self.max_comments:
            payload = self._get(
                APPREVIEWS_URL.format(appid=appid),
                {
                    "json": 1,
                    "language": self.language,
                    "filter": "recent",
                    "num_per_page": self.per_page,
                    "purchase_type": "all",
                    "cursor": cursor,
                },
            )
            if payload.get("success") != 1:
                break

            reviews = payload.get("reviews") or []
            if not reviews:
                break

            hit_edge = False
            for review in reviews:
                if (review.get("timestamp_created") or 0) < cutoff:
                    hit_edge = True
                    break
                collected.append(self._normalize(review, slug))

            if hit_edge:
                stop_reason = "window_edge"
                break
            if len(collected) >= self.max_comments:
                collected = collected[: self.max_comments]
                stop_reason = "cap"
                break

            next_cursor = payload.get("cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        return collected, stop_reason

    @staticmethod
    def _normalize(review, slug):
        author = review.get("author") or {}
        return {
            "source": "steam",
            "game_slug": slug,
            "review_id": str(review.get("recommendationid")),
            "created_at": review.get("timestamp_created"),
            "voted_up": bool(review.get("voted_up")),
            "votes_up": review.get("votes_up") or 0,
            "playtime_hours": round((author.get("playtime_at_review") or 0) / 60, 1),
            # 作者身份在此处即做哈希假名化，原始 steamid 不落盘
            "author_hash": hash_author(author.get("steamid")),
            "text": (review.get("review") or "").strip(),
        }
