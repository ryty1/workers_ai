# `/v1/decide` 接口文档

决策接口。调用 Cloudflare 的 **clef 系列**（System One API）模型：给一段 `state`，问它若干个问题，
它返回每个问题的答案和概率。**它不生成文本**——这是它和 `/v1/chat/completions` 的根本区别。

```
POST https://your-domain.com/v1/decide
Authorization: Bearer sk-xxxxxxxx
Content-Type: application/json
```

---

## 1. 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `model` | string | 否 | `clef-flash` / `clef-omni`，默认 `clef-flash`。也接受原始 id `@cf/cloudflare/clef-flash`（但设了别名时会被拒，见 §6） |
| `state` | string \| object \| array | **是** | 要判断的内容。字符串，或结构化数据（工单对象、聊天记录、应用状态）。超长会按模型上限截断 |
| `questions` | object | **是** | 问题表：`问题id → 问题定义`。1–64 个；id 可用字母、数字、`_`、`.`、`-`，最长 100 字符。答案按同样的 id 返回 |
| `images` | array | 否 | 最多 4 张 PNG/JPEG/WebP，放在 `state` 之前一起理解。见 §5 |

`state` 用对象比用拼接字符串效果好——字段名本身就是提示。

```json
{
  "model": "clef-flash",
  "state": {"ticket_id": 4821, "plan": "Enterprise", "issue": "checkout 500", "loss_per_hour": 4000},
  "questions": { "...": { "...": "..." } }
}
```

---

## 2. 三种问题类型

### 2.1 `noul` — 是 / 否

返回**「是」的概率**（0–1）。

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | ✅ | `"noul"` |
| `instructions` | ✅ | 是非问题。字符串，或包含问题与参考数据的对象/数组 |
| `criteria` | 否 | `{"true": "是代表什么", "false": "否代表什么"}`，能显著提高准确率 |

```json
"urgent": {
  "type": "noul",
  "instructions": "Is this urgent?",
  "criteria": {"true": "needs action today", "false": "can wait a day or more"}
}
```

返回：

```json
"urgent": {"type": "noul", "noul": 0.9333}
```

### 2.2 `choice` — 多选一

从你给定的选项里挑一个，返回选中项、每项概率、置信度。

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | ✅ | `"choice"` |
| `instructions` | ✅ | 要决定什么 |
| `criteria` | ✅ | `选项id → 描述`，**2–255 个**。描述可以是字符串/对象/数组，不需要描述时给 `null` |

```json
"team": {
  "type": "choice",
  "instructions": "Which team should handle this ticket?",
  "criteria": {
    "billing": "money, refunds, invoices",
    "technical": "bugs, errors, outages",
    "sales": "pricing, new purchases"
  }
}
```

返回：

```json
"team": {
  "type": "choice",
  "choice": "technical",
  "probabilities": {"billing": 0.0169, "technical": 0.9702, "sales": 0.0129},
  "confidence": 0.9127
}
```

### 2.3 `score` — 有序量表打分

按你给的有序等级打分，返回加权分数 + 每级概率 + 置信度。

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | ✅ | `"score"` |
| `instructions` | ✅ | 要评估什么 |
| `criteria` | ✅ | **有序等级数组，从低到高**，2–10 个。元素可以是字符串/对象/数组 |

```json
"severity": {
  "type": "score",
  "instructions": "How severe is the impact?",
  "criteria": ["No impact", "Minor", "Major", "Critical"]
}
```

返回（`score` 是**下标**加权值，`legend` 给出下标到文案的映射）：

```json
"severity": {
  "type": "score",
  "score": 2.9152,
  "legend": {"0": "No impact", "1": "Minor", "2": "Major", "3": "Critical"},
  "probabilities": {"0": 0.0048, "1": 0.0041, "2": 0.0622, "3": 0.9289},
  "confidence": 0.8223
}
```

`score = Σ(下标 × 概率)`，2.9152 约等于「Major 偏 Critical」。取整：`Math.round(score)` → `2` = Major。

---

## 3. 响应

```json
{
  "result": {
    "model": "clef-flash",
    "answers": {
      "urgent": {"type": "noul", "noul": 0.9333},
      "team": {"type": "choice", "choice": "technical", "...": "..."},
      "severity": {"type": "score", "score": 2.9152, "...": "..."}
    },
    "usage": {"input_tokens": 358, "output_tokens": 0}
  },
  "success": true,
  "errors": [],
  "messages": []
}
```

响应头 `X-Cfai-Account` 是本次实际落到的账号名，排查用。

**计费**：只算输入。clef-flash $0.038/M 输入 token、clef-omni $0.15/M，输出免费。
上面这次 358 token ≈ $0.0000136。

---

## 4. 错误码

| HTTP | 含义 | 处理 |
|---|---|---|
| 400 | `state` / `questions` 缺失，或 `type` 不是三种之一 | 改请求体 |
| 401 | Key 无效或已停用 | 换 Key |
| 403 | 模型没在「模型目录」里启用，或该模型设了别名却用原始 id 调用 | 控制台启用 / 改用别名 |
| 429 | 该 Key 今日请求数或神经元到顶 | 等 UTC 00:00 重置 |
| 502 | 池子里所有账号都失败（错误详情在 `error.message`） | 看账号页错误 |
| 503 | 没有可用账号（全在冷却 / 超配额 / 鉴权失败） | 等冷却或加账号 |

上游 4xx（参数错）**不计入账号失败**，不会把健康账号打成故障。

---

## 5. 图片输入

最多 4 张，单张 4 MiB / 16 兆像素，总解码 8 MiB，整个请求体 ≤ 13 MiB。**不接受远程 URL**。

两种写法，可混用：

```json
"images": [
  "data:image/png;base64,iVBORw0KGgo...",
  {"content_type": "image/jpeg", "base64": "/9j/4AAQSkZJR..."}
]
```

---

## 6. 路由、别名、限额

- **调度**：和 chat 共用同一个账号池，轮询 + 失败转移 + 冷却退避，单次调用最多重试 `router.max_retries` 个账号
- **别名**：`router.alias_only = true`（默认）时，给 `@cf/cloudflare/clef-flash` 设了别名就必须用别名调，用原始 id 会 403
- **白名单**：模型必须在「模型目录」里勾选启用，否则 403
- **Key 限额**：`/v1/decide` 的调用计入该 Key 的每日请求数和神经元，超限 429
- **模型能力**：`clef-flash` 上下文 24576，`clef-omni` 65536（可带图）

---

## 7. 调用示例

### curl

```bash
curl https://your-domain.com/v1/decide \
  -H "Authorization: Bearer $CFAI_KEY" \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
{
  "model": "clef-flash",
  "state": "Customer ticket #4821: checkout returns 500 since 09:10 UTC, ~$4000/hour lost.",
  "questions": {
    "urgent":   {"type": "noul", "instructions": "Is this urgent?"},
    "team":     {"type": "choice", "instructions": "Which team?",
                 "criteria": {"billing": "money", "technical": "bugs", "sales": "pricing"}},
    "severity": {"type": "score", "instructions": "How severe?",
                 "criteria": ["No impact", "Minor", "Major", "Critical"]}
  }
}
JSON
```

### Python

```python
import requests

r = requests.post(
    "https://your-domain.com/v1/decide",
    headers={"Authorization": "Bearer sk-xxx"},
    json={
        "model": "clef-flash",
        "state": {"issue": "checkout 500", "loss_per_hour": 4000, "plan": "Enterprise"},
        "questions": {
            "urgent": {"type": "noul", "instructions": "Is this urgent?"},
            "severity": {"type": "score", "instructions": "How severe?",
                         "criteria": ["No impact", "Minor", "Major", "Critical"]},
        },
    },
    timeout=60,
)
r.raise_for_status()
ans = r.json()["result"]["answers"]
print(ans["urgent"]["noul"])            # 0.9333
print(round(ans["severity"]["score"]))  # 3 -> Critical
```

### Node

```js
const r = await fetch("https://your-domain.com/v1/decide", {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.CFAI_KEY}`, "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "clef-flash",
    state: { issue: "checkout 500", loss_per_hour: 4000 },
    questions: {
      urgent: { type: "noul", instructions: "Is this urgent?" },
      team: { type: "choice", instructions: "Which team?",
              criteria: { billing: "money", technical: "bugs", sales: "pricing" } },
    },
  }),
});
const { result } = await r.json();
console.log(result.answers.team.choice);   // "technical"
```

---

## 8. CLI 等价用法

```bash
cfai decide -m clef-flash \
  --state-file examples/state.txt \
  --questions-file examples/questions.json

cfai decide -m clef-omni --state "..." --questions '{"ok":{"type":"noul","instructions":"Is this a test?"}}'
cfai decide -m clef-flash --state-file s.txt --questions-file q.json --json   # 原始 JSON
cfai decide -m clef-omni --state-file s.txt --questions-file q.json --image shot.png
```

---

## 9. 和 `/v1/chat/completions` 的区别

| | `/v1/chat/completions` | `/v1/decide` |
|---|---|---|
| 模型 | llama / glm / qwen 等 | 只有 clef 系列 |
| 输入 | `messages` 对话 | `state` + `questions` |
| 输出 | 生成文本 | 概率 / 选项 / 分数 |
| 用途 | 对话、写作、代码 | 分类、路由、打分、是否判断 |
| 延迟 | 首字 0.3–0.5s | 单次 0.3–0.9s（一次问多个问题也这么快） |

把 clef 的请求发到 `/v1/chat/completions` 会直接 400，反之亦然——两个入口各管各的 schema。
