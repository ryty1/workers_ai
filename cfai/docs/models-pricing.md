# Cloudflare Workers AI 模型分类（73 个）

> 数据来源：本机 `catalog.json`（Cloudflare 官方目录）+ [官方定价页](https://developers.cloudflare.com/workers-ai/platform/pricing/)

## 一句话规则

- 免费额度：**每个账号每天 10,000 神经元**，所有模型共用这一个池子，UTC 00:00 重置
- 超出后：免费账号直接报错，需要 **Workers Paid（$5/月）** 或 **AI Gateway 预充值**
- 换算：**神经元/M token ≈ 价格 × 91,000**（CF 定价 $0.011 / 1,000 神经元）
- 下文「每次」按典型请求 **输入 500 / 输出 300 token** 估算

## 分类汇总

| 分类 | 数量 | 说明 |
|---|---|---|
| A 需付费订阅 | 8 | 免费账号调不了，必须付费计划或预充值 |
| B 完全免费 | 19 | 目录价 $0，基本不消耗神经元 |
| C 只收输入 | 12 | 输出免费，回答越长越划算 |
| D 双向收费 | 23 | 输入输出都计费，按输出价从低到高排 |
| E 按次/分钟计费 | 11 | 非 token 计价，语音/图像类 |

## A 需付费订阅（8 个）

| 模型 | 任务 | 上下文 | 输入$/M | 输出$/M | 神经元/M 出 | 每次 | 单账号/天 |
|---|---|---|---|---|---|---|---|
| `@cf/zai-org/glm-5.2` | Text Generation | 262144 | 1.4 | 4.4 | 400400 | 183.8 | 54 |
| `@cf/zai-org/glm-5.3` | Text Generation | 1048576 | 1.4 | 4.4 | 400400 | 183.8 | 54 |
| `@cf/moonshotai/kimi-k2.6` | Text Generation | 262144 | 0.95 | 4 | 364000 | 152.4 | 65 |
| `@cf/moonshotai/kimi-k2.7-code` | Text Generation | 262144 | 0.95 | 4 | 364000 | 152.4 | 65 |
| `@cf/deepseek-ai/deepseek-v4-pro-0813` | Text Generation | 1048576 | 1.32 | 3.96 | 360360 | 168.2 | 59 |
| `@cf/deepseek-ai/deepseek-v4-flash-0731` | Text Generation | 1048576 | 0.44 | 1.32 | 120120 | 56.1 | 178 |
| `@cf/deepseek-ai/deepseek-v4.1-flash-1017` | Text Generation | 1048576 | 0.3 | 1.2 | 109200 | 46.4 | 215 |
| `@cf/zai-org/glm-5.3-flash` | Text Generation | 1048576 | 0.15 | 0.5 | 45500 | 20.5 | 488 |

## B 完全免费（19 个）

| 模型 | 任务 | 上下文 | 网关入口 |
|---|---|---|---|
| `@cf/black-forest-labs/flux-1-schnell` | Text-to-Image | - | —（网关未暴露） |
| `@cf/black-forest-labs/flux-2-dev` | Text-to-Image | - | —（网关未暴露） |
| `@cf/black-forest-labs/flux-2-klein-4b` | Text-to-Image | - | —（网关未暴露） |
| `@cf/black-forest-labs/flux-2-klein-9b` | Text-to-Image | - | —（网关未暴露） |
| `@cf/bytedance/stable-diffusion-xl-lightning` | Text-to-Image | - | —（网关未暴露） |
| `@cf/daily-co/phonellm-mini-alpha-2` | Text Generation | 262140 | /v1/chat/completions |
| `@cf/google/embeddinggemma-300m` | Text Embeddings | - | —（网关未暴露） |
| `@cf/google/gemma-2b-it-lora` | Text Generation | 8192 | /v1/chat/completions |
| `@cf/google/gemma-7b-it-lora` | Text Generation | 3500 | /v1/chat/completions |
| `@cf/llava-hf/llava-1.5-7b-hf` | Image-to-Text | - | —（网关未暴露） |
| `@cf/lykon/dreamshaper-8-lcm` | Text-to-Image | - | —（网关未暴露） |
| `@cf/meta-llama/llama-2-7b-chat-hf-lora` | Text Generation | 8192 | /v1/chat/completions |
| `@cf/mistral/mistral-7b-instruct-v0.2-lora` | Text Generation | 15000 | /v1/chat/completions |
| `@cf/openai/whisper` | Automatic Speech Recognition | - | —（网关未暴露） |
| `@cf/openai/whisper-tiny-en` | Automatic Speech Recognition | - | —（网关未暴露） |
| `@cf/runwayml/stable-diffusion-v1-5-inpainting` | Text-to-Image | - | —（网关未暴露） |
| `@cf/stabilityai/stable-diffusion-xl-base-1.0` | Text-to-Image | - | —（网关未暴露） |
| `@cf/swiss-ai/apertus-v1.5-8b` | Text Generation | 262144 | /v1/chat/completions |
| `@cf/utter-project/eurollm-9b-it` | Text Generation | 32000 | /v1/chat/completions |

## C 只收输入（12 个）

| 模型 | 任务 | 上下文 | 输入$/M | 输出$/M | 神经元/M 出 | 每次 | 单账号/天 |
|---|---|---|---|---|---|---|---|
| `@cf/baai/bge-base-en-v1.5` | Text Embeddings | 153600 | 0.0666 | 免费 | - | 3.0 | 3300 |
| `@cf/baai/bge-large-en-v1.5` | Text Embeddings | - | 0.204 | 免费 | - | 9.3 | 1077 |
| `@cf/baai/bge-m3` | Text Embeddings | 60000 | 0.0118 | 免费 | - | 0.5 | 18625 |
| `@cf/baai/bge-reranker-base` | Text Classification | - | 0.00311 | 免费 | - | 0.1 | 70668 |
| `@cf/baai/bge-small-en-v1.5` | Text Embeddings | - | 0.0202 | 免费 | - | 0.9 | 10880 |
| `@cf/cloudflare/clairic` | Text Generation | 65536 | 0.1 | 免费 | - | 4.5 | 2197 |
| `@cf/cloudflare/clef` | Text Generation | 65536 | 0.24 | 免费 | - | 10.9 | 915 |
| `@cf/cloudflare/clef-flash` | Text Generation | 24576 | 0.038 | 免费 | - | 1.7 | 5783 |
| `@cf/cloudflare/clef-omni` | Text Generation | 65536 | 0.15 | 免费 | - | 6.8 | 1465 |
| `@cf/huggingface/distilbert-sst-2-int8` | Text Classification | - | 0.0263 | 免费 | - | 1.2 | 8356 |
| `@cf/pfnet/plamo-embedding-1b` | Text Embeddings | - | 0.0186 | 免费 | - | 0.8 | 11816 |
| `@cf/qwen/qwen3-embedding-0.6b` | Text Embeddings | 8192 | 0.0118 | 免费 | - | 0.5 | 18625 |

## D 双向收费（23 个）

| 模型 | 任务 | 上下文 | 输入$/M | 输出$/M | 神经元/M 出 | 每次 | 单账号/天 |
|---|---|---|---|---|---|---|---|
| `@cf/deepseek-ai/deepseek-r1-distill-qwen-32b` | Text Generation | 80000 | 0.497 | 4.881 | 444171 | 155.9 | 64 |
| `@cf/qwen/qwen3.8-27b` | Text Generation | 262144 | 0.45 | 3.2 | 291200 | 107.8 | 92 |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | Text Generation | 24000 | 0.293 | 2.253 | 205023 | 74.8 | 133 |
| `@cf/nvidia/nemotron-3-120b-a12b` | Text Generation | 256000 | 0.5 | 1.5 | 136500 | 63.7 | 156 |
| `@cf/moondream/moondream3.1-9B-A2B` | Image-to-Text | - | 0.3 | 1 | 91000 | 40.9 | 244 |
| `@cf/qwen/qwen2.5-coder-32b-instruct` | Text Generation | 32768 | 0.66 | 1 | 91000 | 57.3 | 174 |
| `@cf/qwen/qwq-32b` | Text Generation | 24000 | 0.66 | 1 | 91000 | 57.3 | 174 |
| `@cf/meta/llama-4-scout-17b-16e-instruct` | Text Generation | 131000 | 0.27 | 0.85 | 77350 | 35.5 | 281 |
| `@cf/openai/gpt-oss-120b` | Text Generation | 128000 | 0.35 | 0.75 | 68250 | 36.4 | 274 |
| `@cf/meta/llama-3.2-11b-vision-instruct` | Text Generation | 128000 | 0.0485 | 0.676 | 61516 | 20.7 | 483 |
| `@cf/aisingapore/gemma-sea-lion-v4-27b-it` | Text Generation | 128000 | 0.351 | 0.555 | 50505 | 31.1 | 321 |
| `@cf/mistralai/mistral-small-3.1-24b-instruct` | Text Generation | 128000 | 0.351 | 0.555 | 50505 | 31.1 | 321 |
| `@cf/zai-org/glm-4.7-flash` | Text Generation | 131072 | 0.0605 | 0.4 | 36400 | 13.7 | 731 |
| `@cf/ai4bharat/indictrans2-en-indic-1B` | Translation | - | 0.342 | 0.342 | 31122 | 24.9 | 401 |
| `@cf/meta/m2m100-1.2b` | Translation | - | 0.342 | 0.342 | 31122 | 24.9 | 401 |
| `@cf/meta/llama-3.2-3b-instruct` | Text Generation | 80000 | 0.0509 | 0.335 | 30485 | 11.5 | 872 |
| `@cf/qwen/qwen3-30b-a3b-fp8` | Text Generation | 32768 | 0.0509 | 0.335 | 30485 | 11.5 | 872 |
| `@cf/google/gemma-4-26b-a4b-it` | Text Generation | 256000 | 0.1 | 0.3 | 27300 | 12.7 | 784 |
| `@cf/openai/gpt-oss-20b` | Text Generation | 128000 | 0.2 | 0.3 | 27300 | 17.3 | 578 |
| `@cf/meta/llama-3.1-8b-instruct-fp8` | Text Generation | 32000 | 0.152 | 0.287 | 26117 | 14.8 | 677 |
| `@cf/meta/llama-3.2-1b-instruct` | Text Generation | 60000 | 0.027 | 0.201 | 18291 | 6.7 | 1489 |
| `@cf/ibm-granite/granite-4.0-h-micro` | Text Generation | 131000 | 0.017 | 0.112 | 10192 | 3.8 | 2610 |
| `@cf/meta/llama-guard-3-8b` | Text Generation | 131072 | 0.484 | 0.03 | 2730 | 22.8 | 437 |

## E 按次/分钟计费（11 个）

| 模型 | 任务 | 计价 |
|---|---|---|
| `@cf/deepgram/aura-1` | Text-to-Speech | per 1k characters=$0.015 |
| `@cf/deepgram/aura-2-en` | Text-to-Speech | per 1k characters=$0.03 |
| `@cf/deepgram/aura-2-es` | Text-to-Speech | per 1k characters=$0.03 |
| `@cf/deepgram/flux` | Automatic Speech Recognition | per audio minute (websocket)=$0.0077 |
| `@cf/deepgram/nova-3` | Automatic Speech Recognition | per audio minute=$0.0052; per audio minute (websocket)=$0.0092 |
| `@cf/leonardo/lucid-origin` | Text-to-Image | per 512 by 512 tile=$0.007; per step=$0.000132 |
| `@cf/leonardo/phoenix-1.0` | Text-to-Image | per 512 by 512 tile=$0.00583; per step=$0.00011 |
| `@cf/microsoft/resnet-50` | Image Classification | per inference request=$2.51e-06 |
| `@cf/myshell-ai/melotts` | Text-to-Speech | per audio minute=$0.000205 |
| `@cf/openai/whisper-large-v3-turbo` | Automatic Speech Recognition | per audio minute=$0.000513 |
| `@cf/pipecat-ai/smart-turn-v2` | Dumb Pipe | per audio minute=$0.000338 |

## 说明

- 「单账号每日次数」按 10,000 神经元算；你有 N 个账号就是 N 倍
- 「网关入口」指 cfai 当前暴露的接口；其余任务（图像、语音、向量）需要直连 CF API
- LoRA 模型（`*-lora`）需要在请求里指定 base model，实际基本已弃用
- 价格以 Cloudflare 官方为准，本表由 `classify_models.py` 从目录缓存生成
