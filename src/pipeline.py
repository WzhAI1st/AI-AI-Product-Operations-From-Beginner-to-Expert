"""pipeline 总入口。

    python src/pipeline.py --check-config   校验三份配置
    python src/pipeline.py --check-conn     验证 B 站接口连通性与 WBI 签名
    python src/pipeline.py --run            抓取 -> 清洗打标 -> 聚合输出
    python src/pipeline.py --analyze-only   只用 data/raw 已有数据重跑聚合
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from analyze.analyze import aggregate_game, clean, load_categories, tag
from collect.bili_client import BiliApiError, BiliClient, RateLimitedError
from collect.steam_client import SteamClient

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

MAX_OVERVIEW_CATEGORIES = 3

STOP_REASONS = {
    "window_edge": "已翻到时间窗边界",
    "cap": "已达单游戏条数上限",
    "exhausted": "该语言评测已全部取完",
}


def load_yaml(name):
    with (CONFIG_DIR / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_games():
    return load_yaml("games.yaml")["games"]


def enabled_games():
    return [game for game in load_games() if game.get("enabled", True)]


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 配置自检


def check_config():
    games = load_games()
    taxonomy = load_yaml("taxonomy.yaml")
    settings = load_yaml("settings.yaml")

    # user_agent_contact 是合规声明要求的项目标识，仍为占位符时直接提示
    contact = settings["collect"]["user_agent_contact"]
    if "your-name" in contact:
        print(f"[提示] collect.user_agent_contact 仍是占位符 {contact}，"
              "发布前请改成真实仓库地址，否则合规声明里请求头带项目地址这条没有落地。")

    slugs = [game["slug"] for game in games]
    if len(slugs) != len(set(slugs)):
        print("配置错误：games.yaml 中存在重复的 slug", file=sys.stderr)
        return 1

    active = enabled_games()
    if not active:
        print("配置错误：没有任何 enabled 的游戏", file=sys.stderr)
        return 1

    for game in games:
        if game["source"] == "steam" and not game.get("steam_appid"):
            print(f"配置错误：{game['slug']} 是 steam 源但缺少 steam_appid", file=sys.stderr)
            return 1
        if game["source"] == "reddit" and not game.get("subreddit"):
            print(f"配置错误：{game['slug']} 是 reddit 源但缺少 subreddit", file=sys.stderr)
            return 1

    print(f"游戏名单：{len(games)} 款，其中启用 {len(active)} 款")
    for game in games:
        flag = "启用" if game.get("enabled", True) else "跳过"
        detail = game.get("steam_appid") or game.get("subreddit") or "-"
        print(f"  [{flag}] {game['name']} ({game['slug']}) 源={game['source']} {detail}")

    print(f"\n槽点分类：{len(taxonomy['categories'])} 类")
    for category in taxonomy["categories"]:
        print(f"  - {category['name']} ({category['id']}) "
              f"关键词={len(category['keywords'])} 权重={category['weight']}")

    collect = settings["collect"]
    print("\n采集参数：")
    print(f"  时间窗      {collect['window_days']} 天（本地按时间戳过滤）")
    print(f"  单游戏上限  {collect['steam']['max_comments_per_game']} 条")
    print(f"  语言筛选    {collect['steam']['language']}")
    print(f"  请求间隔    {collect['delay_min']} - {collect['delay_max']} 秒")

    llm = settings["analyze"]["llm"]
    print(f"\nLLM 精修：{'已开启' if llm['enabled'] else '已关闭（规则层独立可跑）'}")
    print("\n配置自检通过。")
    return 0


# ---------------------------------------------------------------- B 站连通性


def check_conn():
    """连通性验证：推导 WBI 密钥，并用签名请求打一次最小查询。"""
    client = BiliClient(load_yaml("settings.yaml"))

    try:
        print("[1/3] 访问首页获取匿名 Cookie")
        cookies = client.prime()
        print(f"      获得 {len(cookies)} 项: {', '.join(cookies) or '（无）'}")

        print("[2/3] 从 nav 接口推导 WBI 密钥")
        client.load_wbi_key()
        print(f"      img_key   = {client.img_key}")
        print(f"      sub_key   = {client.sub_key}")
        print(f"      mixin_key = {client.mixin_key}")

        print("[3/3] 签名请求探测（page_size=1，仅验证签名是否被接受）")
        data = client.search_videos(keyword="原神", page=1, page_size=1)
        results = data.get("result") or []
        print(f"      numResults = {data.get('numResults')}")
        if results:
            print(f"      首条标题   = {results[0].get('title')}")
    except RateLimitedError as exc:
        print(f"\n命中风控（code={exc.code}）：{exc.message}", file=sys.stderr)
        print("搜索接口的反爬闸门需绕过浏览器指纹才能解开，本项目不做，仅评论接口可用。",
              file=sys.stderr)
        return 2
    except BiliApiError as exc:
        print(f"\n接口调用失败：{exc}", file=sys.stderr)
        return 1

    print(f"\n连通性检查通过，累计请求 {client.request_count} 次。")
    return 0


# ---------------------------------------------------------------- 采集


def collect(settings):
    raw_dir = ROOT / settings["output"]["raw_dir"]
    raw_dir.mkdir(parents=True, exist_ok=True)

    steam = SteamClient(settings)
    collected = {}

    for game in enabled_games():
        if game["source"] != "steam":
            print(f"跳过 {game['name']}：数据源 {game['source']} 尚未实现")
            continue

        print(f"抓取 {game['name']}（appid={game['steam_appid']}）…")
        reviews, stop_reason = steam.fetch_reviews(game["steam_appid"], game["slug"])
        print(f"  {len(reviews)} 条（{STOP_REASONS[stop_reason]}），累计请求 {steam.request_count} 次")

        # 一并记录停止原因：命中条数上限时，时间窗早期日期的「0 条」是没抓到，不是真没有
        write_json(raw_dir / f"{game['slug']}.json",
                   {"stop_reason": stop_reason, "reviews": reviews})
        collected[game["slug"]] = {"stop_reason": stop_reason, "reviews": reviews}

    return collected


def load_raw(settings):
    raw_dir = ROOT / settings["output"]["raw_dir"]
    collected = {}
    for game in enabled_games():
        path = raw_dir / f"{game['slug']}.json"
        if not path.exists():
            print(f"缺少原始数据，跳过 {game['name']}：{path}")
            continue
        with path.open(encoding="utf-8") as handle:
            collected[game["slug"]] = json.load(handle)
    return collected


# ---------------------------------------------------------------- 分析


def analyze(settings, collected):
    categories = load_categories(CONFIG_DIR / "taxonomy.yaml")
    public_dir = ROOT / settings["output"]["public_dir"]
    tz = ZoneInfo(settings["output"]["timezone"])

    overview_games = []
    total_reviews = 0
    total_dropped = {}

    for game in enabled_games():
        reviews = collected.get(game["slug"])
        if not reviews:
            continue

        cleaned, dropped = clean(reviews["reviews"])
        tag(cleaned, categories)
        row = aggregate_game(game, cleaned, categories, settings)
        row["fetch_stop_reason"] = reviews.get("stop_reason")

        write_json(public_dir / "games" / f"{game['slug']}.json", row)

        total_reviews += row["comment_count"]
        for reason, count in dropped.items():
            total_dropped[reason] = total_dropped.get(reason, 0) + count

        print(f"{game['name']}: 保留 {row['comment_count']}/{len(reviews['reviews'])} 条，"
              f"好评率 {row['positive_ratio']:.0%}，槽点命中 "
              f"{row['tagged_ratio']:.0%}，丢弃 {dropped or '无'}")

        overview_games.append({
            "slug": row["slug"],
            "name": row["name"],
            "source": row["source"],
            "steam_appid": row["steam_appid"],
            "comment_count": row["comment_count"],
            "positive_ratio": row["positive_ratio"],
            "sentiment_score": row["sentiment_score"],
            "tagged_ratio": row["tagged_ratio"],
            "anomaly_days": row["anomaly_days"],
            # 命中条数上限时，时间窗早期日期不完整，前端需明确提示
            "capped": row["fetch_stop_reason"] == "cap",
            "top_categories": [
                {"id": item["id"], "name": item["name"], "ratio": item["ratio"]}
                for item in row["categories"][:MAX_OVERVIEW_CATEGORIES]
            ],
        })

    overview_games.sort(key=lambda item: item["sentiment_score"])

    overview = {
        "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
        "window_days": settings["collect"]["window_days"],
        "llm_refined": settings["analyze"]["llm"]["enabled"],
        "review_total": total_reviews,
        "dropped": total_dropped,
        "games": overview_games,
    }
    write_json(public_dir / "overview.json", overview)
    return overview


# ---------------------------------------------------------------- 入口


def run(analyze_only=False):
    settings = load_yaml("settings.yaml")
    collected = load_raw(settings) if analyze_only else collect(settings)
    overview = analyze(settings, collected)
    print(f"\n完成：{len(overview['games'])} 款游戏，共 {overview['review_total']} 条评论。")
    print(f"产物目录：{settings['output']['public_dir']}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="游戏舆情与槽点分析 pipeline")
    parser.add_argument("--check-config", action="store_true", help="校验 config/ 下三份配置")
    parser.add_argument("--check-conn", action="store_true", help="验证 B 站接口连通性与 WBI 签名")
    parser.add_argument("--run", action="store_true", help="抓取 -> 清洗打标 -> 聚合输出")
    parser.add_argument("--analyze-only", action="store_true", help="只用 data/raw 已有数据重跑聚合")
    args = parser.parse_args()

    if args.check_config:
        return check_config()
    if args.check_conn:
        return check_conn()
    if args.run:
        return run()
    if args.analyze_only:
        return run(analyze_only=True)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
