# game-voc-radar

抓取公开的游戏玩家评测，分析**玩家情绪与槽点**，输出一个纯静态的可视化站点。

- 纵览页：各游戏的情绪分横向对比 + 槽点 Top3
- 单游戏详情页：日声量趋势、槽点分布、每类槽点原声、最受认可的差评/好评

详细施工方案与实测结论见 [`docs/SOP.md`](docs/SOP.md)。

**当前状态**：最小可用闭环已跑通（Steam 数据源 5 款游戏）。Reddit 数据源待配置凭据。

---

## 快速开始

```bash
pip install -r requirements.txt

# 1) 校验配置
python src/pipeline.py --check-config

# 2) 抓取 -> 清洗打标 -> 聚合输出（需要能访问 store.steampowered.com）
python src/pipeline.py --run

# 3) 已有 data/raw 时，只重跑分析与聚合（不联网）
python src/pipeline.py --analyze-only

# 4) 本地预览（必须用 http 服务打开，直接双击 html 会被浏览器跨域策略拦住）
python -m http.server 8765 --bind 127.0.0.1
# 然后访问 http://127.0.0.1:8765/site/index.html
```

---

## 数据源现状

| 源 | 覆盖 | 语言 | 凭据 | 状态 |
|---|---|---|---|---|
| **Steam** | 终末地、鸣潮、绝区零、三角洲行动、恶意不息 | 中文 | 无需 | ✅ 已跑通 |
| Reddit | 原神、崩坏：星穹铁道、明日方舟 | 英文 | 见下 | ⬜ 采集器未实现 |
| B 站 | — | 中文 | 无需 | ⚠️ 仅评论接口可用 |
| NGA | — | 中文 | 需登录态 | ❌ 已排除 |

### 为什么选 Steam 作为主力源

1. 官方公开接口，无需凭据，**不受出口 IP 风控影响**（受限出口 IP 环境下也能跑）
2. `language=schinese` 支持按语言筛选，能直接拿到**中文玩家评测**
3. `voted_up` 字段是 Steam 官方标注的好评/差评，**情绪极性不需要做情感分析**
4. 附带 `playtime_at_review`（游玩时长）、`votes_up`（认可度）等高质量辅助字段

### 为什么 B 站和 NGA 不可用（实测结论）

- **B 站**：`search` / `popular` / `ranking` 接口被浏览器指纹闸门拦截（搜索接口返回 `code=0` 但 `data` 只有 `v_voucher` 的风控空响应）。解开它需要伪造浏览器指纹，属于绕过反爬，本项目不做。评论接口本身可用，但无法自动发现视频。
- **NGA**：`read.php` 对游客一律 403「访客不能直接访问」，两个域名均如此。`app_api` 可达但需要真实账号登录态 + 逆向 App 签名。内容按设计锁在登录之后，且版规禁止爬虫。

### 启用 Reddit 源

1. 打开 `https://old.reddit.com/prefs/apps`，创建应用，类型选 **script**
2. 记下 `client_id`（应用名正下方）与 `secret`
3. 设置环境变量（`secret` 等同密码，**不要提交到仓库**）：

```powershell
$env:REDDIT_CLIENT_ID = "你的client_id"
$env:REDDIT_CLIENT_SECRET = "你的secret"
$env:REDDIT_USER_AGENT = "windows:game-voc-radar:0.1 (by /u/你的Reddit用户名)"
```

> **为什么不推荐 `setx`**：它会把密钥永久写进当前用户的注册表（`HKCU\Environment`），明文存储，任何能读该账户注册表的程序或管理员都能取到，且不随会话结束失效。共享或受管电脑上尤其不要这样存密钥。

4. 把 `config/games.yaml` 里对应条目的 `enabled` 改成 `true`

---

## 配置

改配置不需要动代码：

| 文件 | 作用 |
|---|---|
| `config/games.yaml` | 游戏名单、数据源、appid / subreddit、启用开关 |
| `config/taxonomy.yaml` | 6 类槽点定义 + 关键词规则表 + 情绪词典 |
| `config/settings.yaml` | 时间窗、样本量、限速、聚合参数、LLM 开关 |

### 新增一款 Steam 游戏

在 Steam 商店搜索该游戏，URL 里的数字就是 appid：

```
https://store.steampowered.com/app/2507950/Delta_Force/
                              ^^^^^^^ appid
```

然后在 `config/games.yaml` 追加：

```yaml
  - slug: your-game
    name: 游戏名
    source: steam
    steam_appid: 1234567
    enabled: true
```

---

## 产物结构

```
data/raw/{slug}.json          原始评测（作者已哈希假名化，gitignore）
data/public/overview.json     纵览页数据
data/public/games/{slug}.json 单游戏详情数据
```

前端只读 `data/public/`，与抓取层完全解耦——可以在没有网络的环境里重跑分析、改页面。

前端依赖已本地化：`site/assets/vendor/echarts.min.js`。运行时不请求任何 CDN，内网与离线环境可直接打开。

### 作者身份假名化（`AUTHOR_SALT`）

`data/raw` 里不落盘 Steam 原始 steamid，只写 `author_hash = sha256(salt + ":" + steamid)[:12]`。

- 默认盐是写死在代码里的公开常量，属于**假名化而非匿名化**：steamid 是可枚举空间，对已知 ID 可以反查；同一作者的哈希稳定，跨游戏、跨快照可关联。
- 处理**非公开内容**时，必须注入每次部署随机生成、不入库的盐：

```powershell
$env:AUTHOR_SALT = "<每次部署随机生成的长随机串>"
```

- 换盐后同一作者的哈希会变，历史数据与新数据的作者关联会断开——这是预期行为，不是 bug。盐一旦丢失，历史 `author_hash` 无法复原。

---

## 已知限制

- **接口的 `day_range` 参数实测不生效**，时间窗一律由本地按 `timestamp_created` 过滤
- 命中 `max_comments_per_game` 上限时，时间窗早期日期的 0 条是**没有抓到**而非当天无评测。产物中的 `fetch_stop_reason` 与 `capped` 字段会标记这种情况，页面也会提示
- 槽点命中率（`tagged_ratio`）偏低是正常的：大量短评不含规则关键词。后续接入 LLM 精修可提升
- Steam 的中文评测以 PC 玩家为主，与手游/主机玩家群体存在偏差
- 时间窗内样本很少时（如新游戏），z-score 拐点检测不可靠

---

## 合规声明

**使用前请完整阅读本节。**

1. 本项目**仅采集平台官方公开接口的内容**，不涉及任何登录态、私有数据或付费数据；
2. 所有数据仅供**学习与研究**使用，不得用于商业用途或二次分发原始数据；
3. 请求带明确的 User-Agent 与项目地址，全局串行限速，不对目标站点造成压力；
4. **不存储用户账号原始标识**：Steam 评测作者 ID 在写入原始数据前即做哈希假名化，原始 steamid 不落盘。注意这是哈希假名化而非匿名化：默认盐写在代码里，对已知 ID 可反查；处理非公开内容时应通过 `AUTHOR_SALT` 注入随机盐，详见 docs/SOP.md 第 7.8 节与 README「作者身份假名化」；
5. 图表与页面中不展示可定位到具体个人的信息；
6. 本项目**不做任何绕过反爬、伪造指纹或绕过访问控制的行为**，遇到风控一律中止并提示；
7. 请使用者自行确认所在地区法律与目标平台服务条款，因使用本项目产生的一切后果由使用者承担；
8. 项目不包含、也不依赖任何企业内部数据或非公开接口。

若你是平台方并认为本项目不妥，请提 Issue，会立即调整或下线。

---

## License

代码采用 MIT。

**授权范围注意**：MIT 只覆盖本项目代码，**不覆盖 `data/public/` 下的示例产物**。其中的评测文本摘录来自平台公开内容，版权属于原发布者，随仓库提供仅为演示用途，不构成任何授权，请勿二次分发或用作训练语料。
