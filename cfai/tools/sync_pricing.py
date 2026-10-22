#!/usr/bin/env python3
"""从 Cloudflare 官方定价页抓取每模型单价 + 神经元费率，生成 pricing.json。

用法:
  python3 tools/sync_pricing.py            # 抓取并写入 ~/.config/cfai/pricing.json
  python3 tools/sync_pricing.py --diff     # 抓取后和本地 catalog 对比，只打印差异

官方页：https://developers.cloudflare.com/workers-ai/platform/pricing/
（页面自带 "Last updated" 日期，会一起存下来）
"""
import json
import os
import re
import sys
import time
import urllib.request

SRC = "https://developers.cloudflare.com/workers-ai/platform/pricing/index.md"

# 单位归一化：官方写法 -> 我们的键
UNIT_KEYS = {
    "m input tokens": "in",
    "m output tokens": "out",
    "m cached input tokens": "cached",
}


def home_dir() -> str:
    return os.environ.get("CFAI_HOME") or os.path.join(os.path.expanduser("~"), ".config", "cfai")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "cfai-pricing/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def unit_key(u: str) -> str:
    u = u.replace("<br>", " ").replace("/", " ")
    u = re.sub(r"\s+", " ", u.strip().lower())
    u = u.strip(" ,.;")
    return UNIT_KEYS.get(u, u)


def parse_cell_money(cell: str):
    """'$0.027 per M input tokens $0.201 per M output tokens' -> {in:0.027, out:0.201}"""
    out = {}
    for m in re.finditer(r"\$([\d.]+)\s*per\s+(.+?)(?=\s*\$|\s*$)", cell):
        val, unit = float(m.group(1)), unit_key(m.group(2))
        if unit in out:                       # 同单位多段（极少数），保留较小值
            continue
        out[unit] = val
    return out


def parse_cell_neurons(cell: str):
    """'2457 neurons per M input tokens 18252 neurons per M output tokens'"""
    out = {}
    for m in re.finditer(r"([\d.]+)\s*neurons?\s+per\s+(.+?)(?=\s*[\d.]+\s*neurons?\s+per|\s*$)", cell):
        val, unit = float(m.group(1)), unit_key(m.group(2))
        if unit in out:
            continue
        out[unit] = val
    return out


def parse(md: str):
    updated = ""
    m = re.search(r"Last updated ([A-Za-z]{3} \d{1,2}, \d{4})", md)
    if m:
        updated = m.group(1)

    models, section = {}, ""
    for line in md.splitlines():
        line = line.strip()
        if line.startswith("## ") and "pricing" in line.lower():
            section = line[3:].replace(" model pricing", "").strip()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        mid = cells[0]
        if not mid.startswith("@cf/") and not mid.startswith("@hf/"):
            continue
        price = parse_cell_money(cells[1])
        neurons = parse_cell_neurons(cells[2])
        models[mid] = {
            "section": section,
            "price": {k: price.get(k) for k in ("in", "out", "cached") if price.get(k) is not None},
            "price_unit": {k: v for k, v in price.items() if k not in ("in", "out", "cached")},
            "neurons": {k: neurons.get(k) for k in ("in", "out", "cached") if neurons.get(k) is not None},
            "neurons_unit": {k: v for k, v in neurons.items() if k not in ("in", "out", "cached")},
        }
    return updated, models


def main():
    want_diff = "--diff" in sys.argv
    md = fetch(SRC)
    updated, models = parse(md)
    if not models:
        print("解析失败：页面上没找到定价表", file=sys.stderr)
        return 1

    data = {
        "source": SRC.replace("/index.md", "/"),
        "doc_updated": updated,
        "fetched_at": int(time.time()),
        "models": models,
    }
    out = os.path.join(home_dir(), "pricing.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"官方页最后更新: {updated}")
    print(f"解析模型数: {len(models)}")
    by_sec = {}
    for v in models.values():
        by_sec[v["section"]] = by_sec.get(v["section"], 0) + 1
    print("分类:", ", ".join(f"{k}={v}" for k, v in sorted(by_sec.items())))
    print(f"已写入 {out}")

    cat_path = os.path.join(home_dir(), "catalog.json")
    if want_diff and os.path.exists(cat_path):
        cat = (json.load(open(cat_path)).get("models") or {})
        print("\n--- 与本地 catalog 对比 ---")
        changed = []
        for mid, v in models.items():
            c = cat.get(mid)
            if not c:
                print(f"  新增  {mid}")
                continue
            cur = (c.get("price_in"), c.get("price_out"))
            new = (v["price"].get("in"), v["price"].get("out"))
            if cur != new:
                changed.append((mid, cur, new))
        for mid, cur, new in changed:
            print(f"  改价  {mid}: {cur} -> {new}")
        print(f"  合计: {len(models)} 个官方模型，{len(changed)} 个价格变化，"
              f"{sum(1 for m in models if m not in cat)} 个本地没有")
    return 0


if __name__ == "__main__":
    sys.exit(main())
