# cfai

把**多个 Cloudflare Workers AI 账号**聚合成一个统一调度池，对外提供 OpenAI 兼容接口。

单文件 Python，零第三方依赖。轮询、失败转移、冷却退避、额度控制、可视化管理台，全都自带。

```
你的应用  ──►  cfai (OpenAI 兼容)  ──┬──►  CF 账号 1
                                      ├──►  CF 账号 2
                                      └──►  CF 账号 N
```

- **一个接口调所有账号**：`/v1/chat/completions` 说完就完，不用自己写重试
- **失败自动转移**：429/5xx/超时自动换下一个账号，出问题的账号进冷却
- **额度叠加**：CF 免费额度 10000 神经元/天/账号，N 个账号就是 N 倍
- **决策模型也能用**：`clef-flash` / `clef-flash` / `clef-omni` 走 `/v1/decide`（Cloudflare 独有的 System One API）
- **网页控制台**：填表加账号、管 Key、看用量图表、改模型白名单，手机也能用
- **12 小时会话**：控制台登录过期自动退出，不会长期挂在浏览器里

---

## 1. 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Linux / macOS（Windows 用 WSL） |
| Python | **3.8+**，无第三方包 |
| 网络 | 能访问 `api.cloudflare.com` |
| 磁盘 | < 1 MB |

```bash
python3 --version     # 3.8 以上即可
```

---

## 2. 安装

```bash
tar xzf cfai-src-v1.2.0.tar.gz
cd cfai
chmod +x cfai.py
```

想装成全局命令（可选）：

```bash
ln -sf "$PWD/cfai.py" /usr/local/bin/cfai
```

包内容：

```
cfai.py            主程序（单文件，纯标准库）
admin.html         网页控制台
README.md          本文件（安装 + 快速上手）
docs/decide.md     /v1/decide 接口文档
docs/features.md   功能详解（调度、定价、别名、Key 限额…）
examples/          decide 的示例输入
tests/             端到端测试（可离线跑）
```

---

## 3. 初始化

```bash
./cfai.py init
```

生成 `~/.config/cfai/`：

```
config.json    账号、别名、白名单、路由设置（权限 600）
state.json     用量统计
catalog.json   模型目录 + 价格缓存
```

换位置：`export CFAI_HOME=/opt/cfai-data`。想重来：`./cfai.py init --force`（会丢账号）。

---

## 4. 加账号

两种类型可以混着用，调度器一视同仁。

### 4.1 cloudflare 类型（直连，推荐）

需要 `account_id` 和 API Token：

1. Cloudflare 控制台右侧栏 → **Account ID**（32 位十六进制）
2. **My Profile → API Tokens → Create Token** → 模板选 **Workers AI**
   （或自定义权限 `Account → Workers AI → Read`）
3. 复制 token（`cfut_...`，只显示一次）

```bash
./cfai.py account add cf-01 \
  --type cloudflare \
  --account-id 你的32位account_id \
  --token cfut_你的token
```

多个账号重复加，名字随便起。

### 4.2 openai 类型（Worker / 反代 / 已有地址）

```bash
./cfai.py account add worker-1 \
  --type openai \
  --base-url https://xxx.workers.dev/v1 \
  --api-key sk-xxxx
```

`base_url` 填到 `/v1` 为止，程序自己拼 `/chat/completions`。

### 4.3 验证

```bash
./cfai.py account list      # 看状态
./cfai.py account test      # 逐个真实探测，报告延迟
./cfai.py doctor            # 环境自检
```

`account test` 会按模型类型自动选探测方式（聊天 / 决策 / embedding），失败直接告诉你 HTTP 码和原因。

---

## 5. 选模型

```bash
./cfai.py models --remote      # 拉目录，列出模型和价格
./cfai.py models               # 看缓存（24 小时有效）
```

只暴露你要的模型（**强烈建议**，否则客户端会看到一堆用不了的）：

```bash
./cfai.py models --allow "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/zai-org/glm-4.7-flash"
```

短名（别名）：

```bash
./cfai.py alias set llama @cf/meta/llama-3.3-70b-instruct-fp8-fast
```

设了别名后，`alias_only`（默认开）会让原始 id 不可调用，客户端只能用 `llama`。

---

## 6. 客户端 Key

```bash
./cfai.py key add 私人 --daily-requests 500
# 已创建「私人」：sk-xxxxxxxx
```

- 留空自动生成 `sk-` + 40 位随机十六进制
- 可设每日请求数 / 每日神经元上限，超了返回 429，UTC 00:00 重置
- `cfai key list / disable / enable / rm`

---

## 7. 启动网关

```bash
./cfai.py serve --port 8787 --api-key sk-你的master-key
```

```
cfai 网关已启动: http://0.0.0.0:8787/v1
账号 3 个 · 策略 round_robin · 需要 API Key
```

也可以把 Key 放环境变量，避免出现在命令行历史：

```bash
CFAI_KEY=sk-你的master-key ./cfai.py serve --port 8787
```

### 验证

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models -H "Authorization: Bearer sk-..."

curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"llama","messages":[{"role":"user","content":"你好"}]}'
```

---

## 8. 开机自启（systemd）

```bash
sudo mkdir -p /opt/cfai
sudo cp cfai.py admin.html /opt/cfai/

sudo tee /etc/systemd/system/cfai.service >/dev/null <<'EOF'
[Unit]
Description=cfai - 多账号 Cloudflare Workers AI 网关
After=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/cfai.env
ExecStart=/usr/bin/python3 /opt/cfai/cfai.py serve --port 8787
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/cfai.env >/dev/null <<'EOF'
CFAI_KEY=sk-你的master-key
CFAI_SESSION_TTL=43200
EOF
sudo chmod 600 /etc/cfai.env        # 别让人看见密钥

sudo systemctl daemon-reload
sudo systemctl enable --now cfai
sudo systemctl status cfai
```

---

## 9. 网页控制台

浏览器打开 `http://服务器IP:8787/admin`，第一次进去设一个控制台密码（≥6 位）。

| 页面 | 能做什么 |
|---|---|
| **概览** | 今日请求 / 成功率 / 神经元 / 剩余额度、24 小时柱状图（可横向滚动、悬停看明细）、API 接入卡片（地址+Key+模型，一键复制） |
| **账号** | 填表加账号、改 KEY、测试、重置、校准、删除；状态分「就绪 / 未验证 / 异常 / 鉴权失败 / 冷却」 |
| **KEY** | 新建、显示、复制、改限额、停用、删除 |
| **模型** | 勾选白名单、看单价、费用试算 |
| **别名** | 短名映射 |
| **设置** | 轮询策略、超时、出站 UA、自动体检间隔 |

**登录会话 12 小时过期**，右上角有倒计时；改密码会让所有设备下线。忘记密码：`cfai passwd`。

手机端已适配：表格自动变卡片、按钮一行三个、弹窗变底部抽屉。

后台每 60 分钟自动体检一次账号（设置里可改，0=关）。

---

## 10. 接口

| 接口 | 用途 |
|---|---|
| `POST /v1/chat/completions` | 聊天模型，标准 OpenAI 协议（支持 stream、tools、response_format） |
| `POST /v1/decide` | 决策模型（clef 系列），输入 `{state, questions}`，输出概率/选项/分数 |
| `GET /v1/models` | 当前对外暴露的模型（白名单 + 别名） |
| `GET /health` | 存活检查 |
| `GET /stats` | 用量统计 |

`/v1/decide` 的完整字段、三种题型 schema、错误码、Python/Node 示例见 **[docs/decide.md](docs/decide.md)**。

---

## 11. 挂域名（可选）

**Cloudflare 代理**：SSL/TLS 模式必须和源站匹配。源站是纯 HTTP 就选 **Flexible**，否则会 `525 SSL handshake failed`。更安全的是源站也配证书，然后选 **Full (strict)**。

注意 8787 不在 CF 支持的边缘端口列表里，走 CF 代理时要换成 80/8080/8443 等。

**Nginx 反代**（SSE 必须关缓冲）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

---

## 12. 接客户端

任何支持 OpenAI 协议的客户端（Cherry Studio、ChatBox、LobeChat、OpenWebUI、各种 SDK）：

```
Base URL   http://你的地址:8787/v1
API Key    sk-...（第 6 步建的）
Model      llama（或你设的别名）
```

控制台首页「API 接入」卡片上有现成的地址和 Key，点复制即可。

---

## 13. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `403 模型未启用` | 模型不在白名单 | 控制台「模型」页勾选，或 `models --allow` |
| `400 required properties at '/' are 'model,state,questions'` | 拿聊天接口调了 clef 决策模型 | 改用 `/v1/decide` |
| `401 invalid api key` | Key 错/停用 | 控制台「KEY」页确认 |
| `429` | Key 或账号的日限额到顶 | 等 UTC 00:00，或调高限额 |
| `503 没有可用账号` | 全部在冷却/超配额/鉴权失败 | 看账号页的错误信息 |
| `525 SSL handshake failed` | CF 用 HTTPS 握源站的 HTTP | SSL/TLS 改 Flexible，或给源站配证书 |
| 流式没反应 | 反代缓冲了 SSE | Nginx `proxy_buffering off` |
| 端口连不上 | 8787 不在 CF 边缘端口里 | 用 80/8080/8443，或关掉 CF 代理直连 |
| 控制台一刷新就要登录 | 服务重启过（会话只存内存） | 正常行为，重登即可 |
| 额度对不上 | 之前有请求没记上 | 账号页「校准」填实际值 |

`./cfai.py doctor` 能一次查出大部分环境问题。

---

## 14. 升级 / 备份 / 卸载

```bash
# 备份（配置里有 token，注意权限）
tar czf cfai-backup-$(date +%F).tar.gz ~/.config/cfai/

# 升级：只覆盖 cfai.py 和 admin.html，配置不动
tar xzf cfai-src-vX.Y.Z.tar.gz
cp cfai/cfai.py cfai/admin.html /opt/cfai/
sudo systemctl restart cfai

# 卸载
sudo systemctl disable --now cfai
sudo rm /etc/systemd/system/cfai.service /etc/cfai.env
sudo rm -rf /opt/cfai ~/.config/cfai
```

---

## 15. 自检

不联网也能验证程序本身是否正常：

```bash
CFAI_HOME=/tmp/cfai-test python3 tests/test_e2e.py
# ALL TESTS PASSED
```

26 条断言覆盖轮询、429 冷却、故障转移、流式转发、RPM 限速、`/v1/decide`、出站 UA。

---

## 更多

- 调度策略、模型定价、别名规则、Key 限额细节 → [docs/features.md](docs/features.md)
- `/v1/decide` 接口 → [docs/decide.md](docs/decide.md)
