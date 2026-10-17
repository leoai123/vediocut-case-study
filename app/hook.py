#!/usr/bin/env python3
"""精華片頭「搶先看」：從逐字稿挑高光句，組成片頭 teaser。

評分訊號（hook-worthiness）：
  - 反應詞：好飽/天啊/哇/欸/太/好吃…
  - 數字/價值：塊/元/划算/便宜/折…
  - 強觀點：不是/最/居然/竟然/其實…
挑 config.hook.count 句，短(2–5s)、分散在不同來源，依時間順序組成 teaser。
回傳 hook 片段清單（chapter="hook"），供 orchestrator 前置到 aroll。
"""
import re

REACTION = ["好飽", "天啊", "哇", "欸", "太", "好吃", "誇張", "驚"]
VALUE = ["塊", "元", "划算", "便宜", "折", "特價", "優惠", "免費", "點"]
CLAIM = ["不是", "最", "居然", "竟然", "其實", "沒想到", "強制", "從來沒"]
NUM = re.compile(r"\d")


def score(text):
    s = 0
    s += 3 * sum(w in text for w in REACTION)
    s += 2 * sum(w in text for w in VALUE)
    s += 2 * sum(w in text for w in CLAIM)
    if NUM.search(text):
        s += 1
    return s


def pick_hook(transcript, count=3, min_len=1.5, max_len=4.5):
    segs = [s for s in transcript.get("segments", [])
            if min_len <= (s["sourceEnd"] - s["sourceStart"]) <= max_len
            and len(s["text"]) >= 4]
    scored = sorted(segs, key=lambda s: -score(s["text"]))
    picked, used_assets = [], set()
    for s in scored:
        if score(s["text"]) <= 1:
            break
        if s["assetId"] in used_assets:          # 分散來源，避免同一段連拍
            continue
        picked.append(s)
        used_assets.add(s["assetId"])
        if len(picked) >= count:
            break
    # 依拍攝時間順序排列 teaser（assetId 遞增）
    picked.sort(key=lambda s: (s["assetId"], s["sourceStart"]))
    hook_clips = []
    for i, s in enumerate(picked):
        hook_clips.append({
            "id": f"h{i+1:02d}", "chapter": "hook", "assetId": s["assetId"],
            "sourceIn": round(s["sourceStart"], 2), "sourceOut": round(s["sourceEnd"], 2),
            "text": s["text"], "reason": f"高光句(score={score(s['text'])})作精華片頭",
        })
    return hook_clips


if __name__ == "__main__":
    import argparse
    import json

    try:
        import project_paths
    except ModuleNotFoundError:
        from app import project_paths

    ap = argparse.ArgumentParser(description="精華片頭候選預覽（只讀 transcript+config，不寫檔）")
    project_paths.add_project_arg(ap)
    args = ap.parse_args()
    with project_paths.cli_guard():
        project = project_paths.load(args.project)

    t = json.loads(project.transcript.read_text(encoding="utf-8"))
    cfg = json.loads(project.config.read_text(encoding="utf-8"))
    clips = pick_hook(t, cfg.get("hook", {}).get("count", 3))
    for c in clips:
        print(f"  {c['id']} {c['assetId']} {c['sourceIn']}-{c['sourceOut']}s  {c['text']}")
