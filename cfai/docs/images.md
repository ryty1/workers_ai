# `/v1/images/generations` 接口文档

OpenAI 图像生成接口 → Cloudflare 原生 `/ai/run/<model>`。

```
POST https://your-domain.com/v1/images/generations
Authorization: Bearer sk-xxxxxxxx
Content-Type: application/json
```

---

## 1. 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `prompt` | string | **是** | 画面描述 |
| `model` | string | 否 | 默认 `@cf/black-forest-labs/flux-2-dev`。也接受别名 |
| `n` | int | 否 | 生成几张，1–4，默认 1 |
| `response_format` | string | 否 | `b64_json`（默认）或 `url`（返回 `data:` URL） |
| `negative_prompt` | string | 否 | 不想要的内容（SDXL / flux 支持） |
| `width` / `height` | int | 否 | 像素尺寸，部分模型支持 |
| `steps` / `num_steps` | int | 否 | 扩散步数，越多越慢越细 |
| `seed` | int | 否 | 固定随机种子，便于复现 |
| `guidance` | number | 否 | 提示词遵循强度 |
| `aspect_ratio` | string | 否 | 如 `16:9`，部分模型支持 |

```json
{"model": "flux-2-dev", "prompt": "a red apple on a wooden table, product photo", "n": 1}
```

`model` 只写短名时会自动补成 `@cf/black-forest-labs/<名字>`。

---

## 2. 响应

```json
{
  "created": 1792666750,
  "data": [
    {"b64_json": "/9j/4AAQSkZJRgABAQAAAQABAAD...", "revised_prompt": "a red apple ..."}
  ]
}
```

`response_format: "url"` 时：

```json
{"created": 1792666750, "data": [{"url": "data:image/jpeg;base64,/9j/4AAQ...", "revised_prompt": "..."}]}
```

MIME 是从图片字节头嗅出来的（CF 的模型有的返回 JPEG，有的返回 PNG）。

---

## 3. 上游请求方式（实现细节）

CF 的文生图模型分两种入参，网关会自动选：

| 模型 | 入参 |
|---|---|
| `flux-2-dev`、`flux-2-klein-4b`、`flux-2-klein-9b` | **multipart/form-data**（CF 的 schema 只接受这个） |
| `flux-1-schnell`、`stable-diffusion-xl-*`、`dreamshaper` 等 | JSON `{"prompt": ...}` |

判断规则：模型 id 里含 `flux-2` 或 `klein` 就走 multipart，其余走 JSON。

两种方式都打到：

```
https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/<model>
```

CF 返回 `{"result": {"image": "<base64>"}, "success": true}`，网关把它包成 OpenAI 的 `data[]`。

---

## 4. 错误码

| HTTP | 含义 |
|---|---|
| 400 | 缺 `prompt`，或上游拒绝参数（比如模型 id 不存在） |
| 401 | Key 无效 |
| 403 | 模型没在「模型目录」里启用 |
| 429 | Key 或账号今日额度到顶 |
| 502 | 池子里所有账号都失败 |
| 503 | 没有可用账号（全在冷却/超配额/鉴权失败） |

---

## 5. 示例

```bash
curl https://your-domain.com/v1/images/generations \
  -H "Authorization: Bearer $CFAI_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"flux-2-dev","prompt":"a red apple on a wooden table"}' \
  | jq -r '.data[0].b64_json' | base64 -d > apple.jpg
```

Python（OpenAI SDK 直接把 base_url 指过来也能用）：

```python
from openai import OpenAI
c = OpenAI(base_url="https://your-domain.com/v1", api_key="sk-xxx")
r = c.images.generate(model="flux-2-dev", prompt="a red apple", n=1, response_format="b64_json")
import base64; open("apple.jpg","wb").write(base64.b64decode(r.data[0].b64_json))
```

---

## 6. 额度

图像模型按**次**计费，不按 token：

| 模型 | 价格 |
|---|---|
| `flux-1-schnell` | 免费 |
| `flux-2-dev` / `flux-2-klein-*` | 免费 |
| `stable-diffusion-xl-base-1.0` | 免费 |
| `leonardo/lucid-origin` | $0.007 / 512×512 图块 + $0.000132 / 步 |
| `leonardo/phoenix-1.0` | $0.00583 / 图块 + $0.00011 / 步 |

免费额度是每个账号每天 10,000 神经元；免费模型基本不消耗。
