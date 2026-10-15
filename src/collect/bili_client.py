"""B 站公开 web 接口客户端。

设计约束（对应 docs/SOP.md 第 7 节合规声明）：
- 只访问无需登录态的公开接口，不携带任何账号 Cookie 或凭证
- 全局串行请求 + 随机间隔限速，不并发
- 携带可识别身份的 User-Agent，便于平台方追溯
- 命中风控错误码立即中止，不做任何绕过尝试
"""

import hashlib
import random
import time
import urllib.parse
from pathlib import Path

import requests

# 公开常量：WBI mixin key 的 64 位重排表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

HOME_URL = "https://www.bilibili.com/"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
# 公开接口，用于领取匿名设备指纹。缺 buvid3 时搜索会被风控返回空响应
FINGER_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

# WBI 签名要求剔除这些字符
_FILTER_CHARS = "!'()*"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BiliApiError(RuntimeError):
    """B 站接口返回非 0 code。"""

    def __init__(self, code, message, url):
        super().__init__(f"接口返回 code={code} message={message} url={url}")
        self.code = code
        self.message = message
        self.url = url


class RateLimitedError(BiliApiError):
    """命中风控。需要人工降低频率，不自动继续。"""


def _extract_wbi_key(url):
    """从 https://i0.hdslb.com/bfs/wbi/<key>.png 里取出 <key>。"""
    return Path(urllib.parse.urlparse(url).path).stem


def _mixin_key(img_key, sub_key):
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


class BiliClient:
    def __init__(self, settings):
        collect = settings["collect"]
        self.timeout = collect["timeout"]
        self.delay_min = collect["delay_min"]
        self.delay_max = collect["delay_max"]
        self.max_retries = collect["max_retries"]
        self.backoff_base = collect["backoff_base"]
        self.abort_on_codes = set(collect["abort_on_codes"])
        self.contact = collect["user_agent_contact"]

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"{_BROWSER_UA} game-voc-radar/0.1 (+{self.contact})",
            "Referer": HOME_URL,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.request_count = 0
        self.img_key = None
        self.sub_key = None
        self.mixin_key = None

    # ---------- 限速 ----------

    def _throttle(self):
        gap = random.uniform(self.delay_min, self.delay_max)
        if self.request_count:
            time.sleep(gap)
        self.request_count += 1

    # ---------- 底层请求 ----------

    def get(self, url, params=None):
        """发起一次 GET，返回已解包的 data 字段。

        非 0 code 抛 BiliApiError；命中风控抛 RateLimitedError。
        """
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code >= 500:
                    last_error = BiliApiError(response.status_code, "服务端错误", url)
                else:
                    payload = response.json()
                    code = payload.get("code")
                    if code == 0:
                        return payload.get("data")
                    error = BiliApiError(code, payload.get("message"), url)
                    if code in self.abort_on_codes:
                        raise RateLimitedError(
                            code, payload.get("message"), url
                        ) from None
                    last_error = error

            if attempt < self.max_retries:
                time.sleep(self.backoff_base * (2 ** (attempt - 1)))

        raise last_error

    # ---------- 会话与 WBI ----------

    def prime(self):
        """领取匿名设备指纹 Cookie（buvid3 / buvid4 / b_nut）。

        这一步不是可选项：缺少 buvid3 时，搜索类接口会返回 code=0
        但 data 里只有 v_voucher 的风控空响应。
        """
        self._throttle()
        self.session.get(HOME_URL, timeout=self.timeout)

        self._throttle()
        response = self.session.get(FINGER_SPI_URL, timeout=self.timeout)
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}

        buvid3, buvid4 = data.get("b_3"), data.get("b_4")
        if not buvid3:
            raise BiliApiError(response.json().get("code"), "指纹接口未返回 b_3", FINGER_SPI_URL)

        self.session.cookies.set("buvid3", buvid3, domain=".bilibili.com")
        if buvid4:
            self.session.cookies.set("buvid4", buvid4, domain=".bilibili.com")
        self.session.cookies.set("b_nut", str(int(time.time())), domain=".bilibili.com")
        return sorted(self.session.cookies.keys())

    def load_wbi_key(self, force=False):
        """从 nav 接口取 img_key / sub_key 并算出 mixin_key。

        未登录时 nav 会返回非 0 code，但 data.wbi_img 依然可用，
        因此这里不走 get() 的错误分支。
        """
        if self.mixin_key and not force:
            return self.mixin_key

        self._throttle()
        response = self.session.get(NAV_URL, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        wbi_img = data.get("wbi_img") or {}
        if not wbi_img.get("img_url") or not wbi_img.get("sub_url"):
            raise BiliApiError(payload.get("code"), "nav 接口未返回 wbi_img", NAV_URL)

        self.img_key = _extract_wbi_key(wbi_img["img_url"])
        self.sub_key = _extract_wbi_key(wbi_img["sub_url"])
        self.mixin_key = _mixin_key(self.img_key, self.sub_key)
        return self.mixin_key

    def sign(self, params):
        """按 WBI 规则补齐 wts 与 w_rid。"""
        mixin = self.load_wbi_key()
        signed = dict(params)
        signed["wts"] = int(time.time())
        signed = dict(sorted(signed.items()))
        signed = {
            key: "".join(ch for ch in str(value) if ch not in _FILTER_CHARS)
            for key, value in signed.items()
        }
        query = urllib.parse.urlencode(signed)
        signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return signed

    def signed_get(self, url, params=None):
        return self.get(url, self.sign(params or {}))

    # ---------- 业务接口 ----------

    def search_videos(self, keyword, page=1, page_size=20, order="click"):
        """按关键词搜视频。order=click 表示按播放量排序。"""
        data = self.signed_get(SEARCH_URL, {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "order": order,
        }) or {}

        # 风控会以 code=0 + v_voucher 的形式返回空结果，必须显式报错，
        # 否则会被当成「搜不到视频」从而掩盖真实问题
        if not data.get("result") and data.get("v_voucher"):
            raise RateLimitedError(
                0, f"风控空响应 v_voucher={data['v_voucher']}，需检查 buvid3 是否生效", SEARCH_URL
            )
        return data
