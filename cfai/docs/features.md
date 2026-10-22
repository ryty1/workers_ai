# 功能详解（调度 · 定价 · 别名 · 限额）

> 安装和上手请看 [README.md](../README.md)，本文件是深入说明。

把 N 个 CF 账号（或 N 个自建 Worker 反代）合成**一个入口**：轮询分摊、429/5xx 自动冷却+故障转移、每日配额、用量统计，并提供 **OpenAI 兼容网关**，任何客户端（Cherry Studio / LobeChat / Cursor / OpenAI SDK）都能直接用。

纯 Python 标准库，无第三方依赖。

## 两种账号可混用

| 类型 | 用途 | 需要 |
|---|---|---|
| `cloudflare` | 直连 CF 官方 OpenAI 兼容端点 | `account_id` + `api_token`（Workers AI 权限） |
| `openai` | 你自己部署的 Worker / 反代 / 任何 OpenAI 兼容服务 | `base_url` + `api_key`（可空） |

一个池子里可以同时有 `cf-a`、`cf-b`、`worker-1`，调度逻辑完全一致。

## 安装

```bash
chmod +x cfai.py
ln -sf $PWD/cfai.py /usr/local/bin/cfai
cfai --version
```

配置放在 `~/.config/cfai/`（可用 `CFAI_HOME` 改），配置文件权限自动 600。

## 加账号

直连 CF 账号：

```bash
cfai account add cf-a --type cloudflare \
  --account-id 0123456789abcdef0123456789abcdef \
  --token "你的CF_API_TOKEN" \
  --daily-requests 300 --test
```

已有 Worker 反代（WorkBuddy2API 那种）：

```bash
cfai account add worker-1 --type openai \
  --base-url https://xxx.workers.dev/v1 \
  --api-key "worker里的key" \
  --model-map '{"llama":"@cf/meta/llama-3.1-8b-instruct"}'
```

其它命令：

```bash
cfai account list          # 状态：就绪 / 冷却N秒 / 鉴权失败 / 今日请求数
cfai account test          # 全部测一遍，失败的自动标鉴权错误
cfai account reset rate    # 手动清冷却
cfai account rm cf-a
```

## 用 CLI

```bash
cfai alias set llama @cf/meta/llama-3.1-8b-instruct   # 起个短名
cfai chat "写个快排" -m llama                          # 一次性
cfai chat "写个快排" -m llama --stream                 # 流式
cfai chat -m llama -s "你是简洁的中文助手"             # 交互模式（多轮）
cfai chat "hi" -m llama --json                        # 原始 JSON，方便脚本
cfai stats                                            # 各账号用量/失败/最近错误
cfai models --remote                                  # 从各账号实时拉模型列表
```

## 当网关用（推荐）

```bash
cfai serve --port 8787 --api-key sk-local
```

然后任意客户端填：

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: `sk-local`（不传 `--api-key` 则不校验）
- Model: `llama`（别名）或 `@cf/meta/...` 全名

支持 `/v1/chat/completions`（含 `stream: true`）、`/v1/models`、`/stats`、`/health`。
响应头 `X-Cfai-Account` 告诉你这次是哪个账号干的活。

常驻：

```bash
# systemd
sudo tee /etc/systemd/system/cfai.service >/dev/null <<'EOF'
[Unit]
Description=cfai gateway
After=network-online.target

[Service]
ExecStart=/usr/local/bin/cfai serve --port 8787 --api-key sk-local
Restart=always
User=root

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now cfai
```

## 决策模型（clef 系列）

`@cf/cloudflare/clef`、`clef-flash`、`clef-omni` 是 Cloudflare 的**决策模型**（System One API），不是聊天模型：不接受 `messages`，输入是 `{model, state, questions}`，输出是每个选项的概率。走原生 `/ai/run/<model>` 端点。

```bash
cfai decide -m clef-flash \
  --state-file examples/state.txt \
  --questions-file examples/questions.json

cfai decide -m clef-omni --state "用户说：付了钱但没收到货" \
  --questions '{"refund":{"type":"noul","instructions":"用户是在要求退款吗？"}}'
```

输出：

```
model: clef-flash  account: cf-02
  urgent        noul    YES  p=0.958
  team          choice  technical  (conf 0.77)
                      technical=0.918  billing=0.068  sales=0.014
  severity      score   2.89  top=Critical  (conf 0.80)
                      0=0.01  1=0.01  2=0.06  3=0.92
```

三种题型：

- `noul` — 是/否，返回概率 `noul`（≥0.5 判为 YES）
- `choice` — 多选一，`criteria` 是 `{选项: 描述}`，返回 `choice` + 每项概率 + confidence
- `score` — 有序打分，`criteria` 是低到高的列表，返回加权分 `score` + 每档概率

`--image a.png` 可附最多 4 张图（clef-omni 还支持音视频，走 JSON 的 `audio`/`video` 字段）。
调度规则和 chat 完全一样：轮询、冷却、故障转移都在。注意 clef 的 `usage` 只有 `input_tokens`，没有 neurons。

`cfai chat -m clef-flash` 会被拦下并提示改用 `cfai decide`；网关收到 clef 模型也直接返回 400 说明原因。

### HTTP 接口 `POST /v1/decide`

除了 CLI，网关也提供决策接口，客户端可以直接调：

```bash
curl https://your-domain.com/v1/decide \
  -H "Authorization: Bearer $CFAI_KEY" -H "Content-Type: application/json" \
  -d '{"model":"clef-flash","state":"checkout 500 since 09:10",
       "questions":{"urgent":{"type":"noul","instructions":"Is this urgent?"}}}'
```

返回 `{"result":{"answers":{"urgent":{"type":"noul","noul":0.93}},"usage":{...}},"success":true}`，
响应头 `X-Cfai-Account` 是实际落到的账号。

**完整文档（字段、三种题型 schema、错误码、Python/Node 示例）：[`docs/decide.md`](docs/decide.md)**

## Web 控制台

网关自带管理界面，浏览器打开 `http://<host>:8787/admin`，用 API Key 登录（存在本机 localStorage）。

- **概览**：今日请求 / 成功率 / 神经元用量 / 剩余额度四张卡，最近 24 小时柱状图（按小时累计），每个账号的卡片（状态、请求、失败、额度进度条、最近错误）
- **账号**：表格 + 添加/编辑/测试/重置/删除；密钥只显示后四位，改 key 时留空即保持不变
- **模型**：搜索、拉取（从各账号实时刷新目录）、显示每个模型有几个账号可用
- **设置**：调度策略、重试次数、RPM、冷却、超时、默认模型、模型别名

后端接口（都要求 `Authorization: Bearer <key>`）：

```
GET    /admin/api/snapshot
GET    /admin/api/models?q=clef
POST   /admin/api/accounts                {name,type,base_url,api_key,...}
PATCH  /admin/api/accounts/<name>
DELETE /admin/api/accounts/<name>
POST   /admin/api/accounts/<name>/test
POST   /admin/api/accounts/<name>/reset
POST   /admin/api/catalog/refresh
POST   /admin/api/router                  {strategy,rpm,max_retries,...}
POST   /admin/api/aliases  ·  DELETE /admin/api/aliases/<name>
```

页面是单文件 `admin.html`（无构建、无 CDN 依赖），改样式直接编辑它，刷新即生效，不用重启网关。

## 额度自动计算

CF 免费额度是**每账号每天 10,000 Neurons**，网关默认就按这个算：

- 账号没单独设 `daily_neurons` 时，自动套用全局默认额度（`router.default_daily_neurons`，默认 10000）
- 控制台「剩余额度」= 总额度 − 已用，每张账号卡上有进度条；设成 0 表示不限（付费账号用）
- 额度用满的账号自动跳过，等 UTC 零点重置

**计数按 UTC 零点重置**（CF 就是这个时间），不是本地时间——否则时区一差就会和真实额度错几个小时。

神经元用量取自响应头 `cf-ai-neurons`（CF 每个响应都带），比 body 里的 `usage.neurons` 更可靠，**流式请求也能算进去**。

## 出站 User-Agent

默认发 `cfai/1.0`，可以在控制台「设置」里改全局值，或在账号编辑里单独覆盖（留空=继承全局）。

注意这只是标识客户端身份，不改变风控判定——请求头不是 CF 判断账号行为的主要依据。

## 模型定价

模型目录接口自带价格，网关解析后存进 `catalog.json`，按 token 计费的三类分开存：

- `price_in` — 每百万输入 token 美元
- `price_out` — 每百万输出 token 美元
- `price_cached` — 每百万缓存输入 token 美元

**没有 `price_out` 的模型就是"输出免费"**——CF 目录里这类有 12 个（clef / clef-flash / clef-omni / clairic 这些决策模型，以及全部 embedding 模型）。它们只按输入计费，输出不计费。

```bash
cfai models --remote            # 表格里带「入$」「出$」两列
cfai models --task "text generation"
```

控制台「模型」页有同样的两列，输出免费的显示绿色「免费」徽标；下面还有**费用试算**：选模型、填输入/输出 token 数，直接算出这次调用的美元成本。

非 token 计费的模型（图像 per step / per tile、语音 per minute、TTS per 1k characters）价格存在 `price_other` 里，界面暂不展示。

## 模型白名单（只暴露你要的模型）

默认 `allowed_models` 为空 = 全部放行。填了就进入白名单模式：

- `GET /v1/models` 只返回列表里的模型（别名和真实 id 都会列出），客户端下拉框里看不到其余 60 多个
- 调用未启用的模型直接 `403`，不会打到上游浪费额度
- `cfai chat` / `cfai decide` 同样受限
- 改完立即生效，**不用重启**（网关按配置文件 mtime 自动重载）

控制台「模型」页每行有勾选框，勾上即启用；右上角「只看已启用」过滤，标题旁显示 `已启用 N`。

命令行/接口：

```bash
curl -X POST http://127.0.0.1:8787/admin/api/models/allow \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"ids":["glm","llama","@cf/cloudflare/clef-flash"]}'
```

## 后台运行（systemd）

已配置为系统服务，开机自启、崩溃 3 秒后自动拉起：

```bash
systemctl status cfai      # 看状态
systemctl restart cfai     # 重启
journalctl -u cfai -f      # 看日志
```

单元文件 `/etc/systemd/system/cfai.service`，API Key 存在 `/root/.config/cfai/env`（600），不写在单元里，`systemctl cat` 看不到密钥。

## 控制台登录（密码）

- 首次没有密码时，用网关 API Key 登录；登录后在「设置 → 控制台密码」设置密码（≥6 位）
- 设了密码之后，控制台只认密码，API Key 不能再进控制台（但仍然可以调 /v1）
- 密码以 `sha256("cfai:v1:"+密码)` 存在 config 里，不存明文

### 会话（12 小时过期）

登录成功后服务端发一个随机 session token（不是密码哈希），有效期 **12 小时**：

- 页面右上角显示 `会话 11:59` 倒计时，到点自动退回登录页
- 关掉浏览器再打开，12 小时内不用重登；超过就必须重新输密码
- 「退出」会调 `/admin/api/logout` 让这个 token 立刻失效
- **改密码 → 所有会话全部失效**，所有设备都得重新登录
- 服务重启也会清空所有会话（token 只存在内存里）

改有效期：`CFAI_SESSION_TTL=3600 cfai serve ...`（秒），或 systemd 的 `Environment=`。

脚本仍可用密码哈希直接当 Bearer 调 `/admin/api/*`，不受会话影响：

```bash
HASH=$(cfai passwd --show 2>/dev/null || python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.config/cfai/config.json')))['admin_password'])")
curl -H "Authorization: Bearer $HASH" http://127.0.0.1:8787/admin/api/snapshot
```

## 客户端 Key 管理

控制台「KEY」页：列表 / 显示完整 Key / 新建 / 编辑 / 停用 / 删除，每个 Key 独立限额和用量。

命令行同样能管（无浏览器环境用）：

```bash
cfai key list                                  # 列表 + 今日用量
cfai key add 私人 --daily-requests 500         # 新建（留空自动生成 sk-+40hex）
cfai key disable 私人                          # 停用（下一个请求就 401）
cfai key enable 私人
cfai key rm 私人
```

- Key 由 `sk-` + 40 位随机 hex 生成，可自己填
- 每个 Key 可设**每日请求上限**和**每日神经元上限**，UTC 零点重置
- 超限返回 `429`，提示具体是哪个上限
- 停用/删除后立刻失效（下一个请求就 401）
- 用量按 Key 分别统计：请求数、成功/失败、神经元、tokens

接口：

```
POST   /admin/api/keys              {name,key?,daily_requests?,daily_neurons?,note?}
PATCH  /admin/api/keys/<name>       {enabled?,key?,daily_requests?,daily_neurons?}
DELETE /admin/api/keys/<name>
POST   /admin/api/login             {password}  → {token}
POST   /admin/api/password          {password}
POST   /admin/api/accounts/<name>/calibrate  {neurons}
```

## 额度对不齐时怎么校准

网关只统计**经过本网关**的请求。如果你还在 CF 控制台 Playground、别的工具、或者直接 curl 账号，那些消耗不会进这里的数字，所以会和 CF 后台对不上。

控制台「账号」页每行有「校准」按钮，把 CF 后台当天看到的神经元数填进去，本地计数就对齐了。

## 别名优先（alias_only）

默认 `router.alias_only = true`：**只要某个模型设了别名，对外就只露别名**。

- `GET /v1/models` 不再列出原始 id（`@cf/cloudflare/clef-flash`），只列 `clef-flash`
- 用原始 id 调用会返回 `403 该模型已设置别名，请使用 clef-flash 调用（原始 id 不对外）`
- `cfai decide -m @cf/cloudflare/clef-flash` 同样会被拦下并提示改用别名

想让原始 id 也可见可调，把配置里的 `router.alias_only` 改成 `false`。

## 账号状态是「真实可用」吗

状态不再是猜的，分五种：

| 状态 | 含义 |
|---|---|
| 未验证 | 从没做过真实探测（新加的账号、或关掉了自动体检） |
| 就绪 | 最近一次真实探测通过 |
| 异常 | 最近一次探测失败（错误在卡片上） |
| 鉴权失败 | 401/403，已踢出池子 |
| 冷却 | 429/5xx 退避中 |

- **添加账号时默认勾选「保存后立即真实探测一次」**，探测失败会直接把账号标成异常/鉴权失败，不会假装成功
- **后台每 60 分钟自动体检**（`router.health_check_minutes`，0=关闭），逐个账号按模型类型发一次最小请求
- 探测按模型自适应：聊天模型发 `messages`，clef 发 `state+questions`，embedding 发 `text`

体检成本：每个账号每小时一次 `max_tokens=1` 的请求，8 个账号约 400 neurons/天，占 8 万总额度的 0.5%。

## 重置 vs 校准

- **重置**：只清冷却、鉴权失败标记和错误信息，**不动今天的用量**
- **校准**：把「已用神经元」改成你指定的数字（对账用）

## 调度规则

- **策略**：`round_robin`（默认）/ `least_used` / `random`，改 `~/.config/cfai/config.json` 的 `router.strategy`
- **故障转移**：单次请求最多试 `max_retries` 个账号
- **冷却**：429 按 `Retry-After`（或 60s），5xx 指数退避（60s→120s→…→上限 900s），404 只跳过不冷却
- **401/403**：直接把账号标为鉴权失败并踢出池子，直到 `cfai account test` 或 `account reset` 通过
- **配额**：`daily_requests` / `daily_neurons` 是每账号每日软上限，到点自动跳过；跨天自动清零
- **神经元记账**：CF 响应里的 `usage.neurons` 是真实计费单位，`cfai stats` 按账号累计（流式响应没有 usage，只计请求数）
- **模型白名单**：`--models "@cf/meta/llama-3.1-8b-instruct,@cf/qwen/*"` 支持通配符，不填=该账号接所有模型

CF 免费额度是**每账号每天 1 万 Neurons**，所以 N 个账号 ≈ N 倍额度。给每个账号加 `--daily-neurons 10000` 就能在到点前自动切换，不会打到超额报错。

模型列表会变：`cfai models --remote --task "text generation"` 从各账号实时拉（CF 的 `/ai/v1/models` 是 405，工具走的是 `/ai/models/search` 目录接口）。

## 自检 / 测试

```bash
cfai doctor        # 配置目录、权限、账号数、api.cloudflare.com 连通性
python3 tests/test_e2e.py   # 端到端：轮询、429 冷却、故障转移、流式、网关、配额
```

测试用本地 mock 上游，不消耗真实额度。
