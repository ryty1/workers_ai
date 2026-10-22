#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 catalog.json 里的模型按「付费要求 + 计费方式」分类，输出 Markdown 和 CSV。

用法：
    python3 classify_models.py                     # 读 ~/.config/cfai/catalog.json
    python3 classify_models.py --catalog x.json --out-dir ./out
"""
import argparse
import csv
import json
import os

# Cloudflare 官方定价页明确列出：这些模型必须 Workers Paid 或 AI Gateway 预充值
PAID_ONLY = {
    "@cf/moonshotai/kimi-k2.6",
    "@cf/moonshotai/kimi-k2.7-code",
    "@cf/zai-org/glm-5.2",
    "@cf/zai-org/glm-5.3",
    "@cf/zai-org/glm-5.3-flash",
    "@cf/deepseek-ai/deepseek-v4-flash-0731",
    "@cf/deepseek-ai/deepseek-v4.1-flash-1017",
    "@cf/deepseek-ai/deepseek-v4-pro-0813",
}

# $1 / M token ≈ 91,000 神经元（CF: $0.011 / 1,000 神经元）
NEURONS_PER_USD_M = 91000
DAILY_NEURONS = 10000
TYPICAL_IN, TYPICAL_OUT = 500, 300


def classify(mid, e):
    pin, pout = e.get("price_in"), e.get("price_out")
    other = e.get("price_other") or []
    if mid in PAID_ONLY:
        return "A 需付费订阅"
    if pin is None and pout is None:
        if not other or all((o.get("price") or 0) == 0 for o in other):
            return "B 完全免费"
    if other and pin is None and pout is None:
        return "E 按次/分钟计费"
    if pin is not None and pout is None:
        return "C 只收输入"
    return "D 双向收费"


def entry_point(mid, task):
    t = (task or "").lower()
    if "clef" in mid:
        return "/v1/decide"
    if "text generation" in t:
        return "/v1/chat/completions"
    return "—（网关未暴露）"


def build_rows(cat):
    rows = []
    for mid, e in (cat.get("models") or {}).items():
        task = e.get("task") or ""
        pin, pout, pc = e.get("price_in"), e.get("price_out"), e.get("price_cached")
        other = e.get("price_other") or []
        neu_in = round(pin * NEURONS_PER_USD_M) if pin is not None else None
        neu_out = round(pout * NEURONS_PER_USD_M) if pout is not None else None

        if neu_in is not None:
            per_req = (TYPICAL_IN * pin + (TYPICAL_OUT * pout if pout else 0)) / 1e6 * NEURONS_PER_USD_M
            per_day = int(DAILY_NEURONS / per_req) if per_req > 0 else None
            per_req_s = f"{per_req:.1f}"
            per_day_s = str(per_day) if per_day is not None else "∞"
        else:
            per_req_s = "—"
            per_day_s = "—"

        rows.append({
            "分类": classify(mid, e),
            "模型": mid,
            "任务": task,
            "上下文": e.get("context") or "",
            "输入$/M": "" if pin is None else pin,
            "输出$/M": "" if pout is None else pout,
            "缓存$/M": "" if pc is None else pc,
            "其它计价": "; ".join(f"{o.get('unit')}=${o.get('price')}" for o in other),
            "神经元/M输入": "" if neu_in is None else neu_in,
            "神经元/M输出": "" if neu_out is None else neu_out,
            "典型请求神经元": per_req_s,
            "单账号每日次数": per_day_s,
            "cfai入口": entry_point(mid, task),
        })
    rows.sort(key=lambda r: (r["分类"], -(float(r["输出$/M"]) if r["输出$/M"] != "" else 0), r["模型"]))
    return rows


def write_md(rows, path, total):
    groups = {}
    for r in rows:
        groups.setdefault(r["分类"], []).append(r)

    out = []
    out.append("# Cloudflare Workers AI 模型分类（%d 个）\n" % total)
    out.append("> 数据来源：本机 `catalog.json`（Cloudflare 官方目录）+ "
               "[官方定价页](https://developers.cloudflare.com/workers-ai/platform/pricing/)\n")
    out.append("## 一句话规则\n")
    out.append("- 免费额度：**每个账号每天 10,000 神经元**，所有模型共用这一个池子，UTC 00:00 重置")
    out.append("- 超出后：免费账号直接报错，需要 **Workers Paid（$5/月）** 或 **AI Gateway 预充值**")
    out.append("- 换算：**神经元/M token ≈ 价格 × 91,000**（CF 定价 $0.011 / 1,000 神经元）")
    out.append("- 下文「每次」按典型请求 **输入 500 / 输出 300 token** 估算\n")

    counts = {k: len(v) for k, v in sorted(groups.items())}
    out.append("## 分类汇总\n")
    out.append("| 分类 | 数量 | 说明 |")
    out.append("|---|---|---|")
    desc = {
        "A 需付费订阅": "免费账号调不了，必须付费计划或预充值",
        "B 完全免费": "目录价 $0，基本不消耗神经元",
        "C 只收输入": "输出免费，回答越长越划算",
        "D 双向收费": "输入输出都计费，按输出价从低到高排",
        "E 按次/分钟计费": "非 token 计价，语音/图像类",
    }
    for k in sorted(groups):
        out.append(f"| {k} | {counts[k]} | {desc.get(k,'')} |")
    out.append("")

    for k in sorted(groups):
        out.append(f"## {k}（{counts[k]} 个）\n")
        if k.startswith("E"):
            out.append("| 模型 | 任务 | 计价 |")
            out.append("|---|---|---|")
            for r in groups[k]:
                out.append(f"| `{r['模型']}` | {r['任务']} | {r['其它计价']} |")
        elif k.startswith("B"):
            out.append("| 模型 | 任务 | 上下文 | 网关入口 |")
            out.append("|---|---|---|---|")
            for r in groups[k]:
                out.append(f"| `{r['模型']}` | {r['任务']} | {r['上下文'] or '-'} | {r['cfai入口']} |")
        else:
            out.append("| 模型 | 任务 | 上下文 | 输入$/M | 输出$/M | 神经元/M 出 | 每次 | 单账号/天 |")
            out.append("|---|---|---|---|---|---|---|---|")
            for r in groups[k]:
                out.append(
                    f"| `{r['模型']}` | {r['任务']} | {r['上下文'] or '-'} | "
                    f"{r['输入$/M'] or '-'} | {r['输出$/M'] or '免费'} | "
                    f"{r['神经元/M输出'] or '-'} | {r['典型请求神经元']} | {r['单账号每日次数']} |")
        out.append("")

    out.append("## 说明\n")
    out.append("- 「单账号每日次数」按 10,000 神经元算；你有 N 个账号就是 N 倍")
    out.append("- 「网关入口」指 cfai 当前暴露的接口；其余任务（图像、语音、向量）需要直连 CF API")
    out.append("- LoRA 模型（`*-lora`）需要在请求里指定 base model，实际基本已弃用")
    out.append("- 价格以 Cloudflare 官方为准，本表由 `classify_models.py` 从目录缓存生成\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=os.path.expanduser("~/.config/cfai/catalog.json"))
    ap.add_argument("--out-dir", default=".")
    a = ap.parse_args()

    cat = json.load(open(a.catalog, encoding="utf-8"))
    rows = build_rows(cat)
    os.makedirs(a.out_dir, exist_ok=True)

    md = os.path.join(a.out_dir, "models-pricing.md")
    csvp = os.path.join(a.out_dir, "models-pricing.csv")
    write_md(rows, md, len(rows))
    with open(csvp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"共 {len(rows)} 个模型")
    for k in sorted({r["分类"] for r in rows}):
        print(f"  {k}: {sum(1 for r in rows if r['分类']==k)}")
    print(f"\n已写出:\n  {md}\n  {csvp}")


if __name__ == "__main__":
    main()
