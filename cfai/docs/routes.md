# 路由总表

cfai 把 Cloudflare 的 10 类模型任务映射到 7 个接口。**73 个模型全部有入口**，没有"能拉下来但调不了"的。

---

## 1. 任务 → 接口

| CF 任务 | 模型数 | 接口 | 说明 |
|---|---|---|---|
| Text Generation | 31 | `POST /v1/chat/completions` | 标准 OpenAI 对话（stream / tools / response_format） |
| Text Generation（clef） | 4 | `POST /v1/decide` | 决策模型，入参 `{state, questions}` |
| Text-to-Image | 11 | `POST /v1/images/generations` | 文生图，返回 `b64_json` |
| Text Embeddings | 7 | `POST /v1/embeddings` | 标准 OpenAI 向量接口 |
| Automatic Speech Recognition | 6 | `POST /v1/audio/transcriptions` | multipart 上传音频 → 文本 |
| Text-to-Speech | 5 | `POST /v1/audio/speech` | 返回二进制音频流 |
| Image-to-Text | 2 | `POST /v1/chat/completions` | 带 `image_url` 的视觉请求 |
| Text Classification | 2 | `POST /v1/run` | 重排 / 分类，透传 |
| Translation | 2 | `POST /v1/run` | 机器翻译，透传 |
| Image Classification | 1 | `POST /v1/run` | 图像分类，透传 |
| Dumb Pipe | 1 | `POST /v1/run` | 语音轮次判断，透传 |

控制台「模型」页的**路由**列会直接标出每个模型走哪个接口。

---

## 2. 各接口速查

### `/v1/chat/completions`

```json
{"model":"llama","messages":[{"role":"user","content":"你好"}]}
```

带图（视觉模型）：

```json
{"model":"@cf/llava-hf/llava-1.5-7b-hf",
 "messages":[{"role":"user","content":[
   {"type":"text","text":"这是什么"},
   {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]}]}
```

> 网关把图片解成字节数组发给 CF。`@cf/meta/llama-3.2-11b-vision-instruct` 需要先在 CF 后台同意 Llama 社区许可，否则 403。

### `/v1/decide`

```json
{"model":"clef-flash","state":"...","questions":{"urgent":{"type":"noul","instructions":"紧急吗？"}}}
```

### `/v1/images/generations`

```json
{"model":"flux-2-dev","prompt":"a red apple","n":1,"response_format":"b64_json"}
```

### `/v1/embeddings`

```json
{"model":"@cf/baai/bge-m3","input":["第一段","第二段"]}
```

```json
{"object":"list","data":[{"object":"embedding","index":0,"embedding":[...]}],"usage":{...}}
```

### `/v1/audio/transcriptions`

```bash
curl -X POST .../v1/audio/transcriptions \
  -F "file=@a.mp3" --form-string "model=@cf/openai/whisper"
```

```json
{"text": "转录结果"}
```

> 注意 `-F "model=..."` 里 `@` 会被 curl 当成文件读取，要用 `--form-string`。

### `/v1/audio/speech`

```json
{"model":"@cf/deepgram/aura-1","input":"要朗读的文本","voice":"angus"}
```

返回 `Content-Type: audio/mpeg` 的二进制。

可选 `encoding`（linear16 / flac / mulaw / alaw / mp3 / opus / aac）、`container`（none / wav / ogg）。

### `/v1/run`（通用透传）

给没有 OpenAI 对应形态的模型用：

```json
{"model":"@cf/baai/bge-reranker-base","query":"cloudflare workers ai",
 "contexts":[{"text":"..."},{"text":"..."}]}
```

响应是 CF 的原始 `result`：

```json
{"result":{"response":[{"id":0,"score":0.79},{"id":1,"score":0.00003}]},"success":true}
```

---

## 3. 上游请求形态（容易踩的坑）

| 情况 | 上游要求 |
|---|---|
| `flux-2-dev`、`flux-2-klein-*` | **multipart/form-data**，JSON 会被拒 |
| `flux-1-schnell`、SDXL、dreamshaper | JSON `{"prompt": ...}` |
| whisper / nova-3 转写 | multipart，字段名 `audio` |
| aura / melotts 合成 | JSON，返回二进制 |
| llava / llama-3.2-vision | JSON `{"prompt", "image": [字节数组]}` |
| moondream | 图片要 `array` 或 `binary`，形态不同 |
| 向量 / 重排 / 分类 | JSON |

网关内部已经按模型自动选，客户端不用关心。

---

## 4. 错误码

| HTTP | 含义 |
|---|---|
| 400 | 参数缺失，或上游拒绝（模型 id 不存在、格式不对） |
| 401 | Key 无效 |
| 403 | 模型未启用，或需要先同意模型许可 |
| 429 | Key / 账号额度到顶 |
| 502 | 所有账号都失败 |
| 503 | 没有可用账号（冷却 / 超配额 / 鉴权失败） |
