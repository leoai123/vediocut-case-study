#!/usr/bin/env python3
"""資產口白量剖析：統計每個素材（assetId）在逐字稿裡的總說話秒數。

動機
----
broll_engine 的 config.broll.fusion.faceDenyAssets（視覺檢索須排除的「講話 A-roll
大檔」清單）過去是每個專案手工核對 contact sheet 產生的固定清單，換專案就要重editor
一次，忘了填就會被 visual_index 檢索出「講者自己的臉」當 B-roll 候選。

觀察：口白秒數高的素材，幾乎都是對鏡頭持續講話的 A-roll（B-roll/空景素材幾乎沒有
被轉錄出逐字稿文字，因為根本沒人對鏡頭講話）。用「口白秒數 >= 門檻」當保守預設，
比完全不篩（可能把講者的臉挑成 B-roll）安全，也比每次手動列表不容易漏。

這只是保守猜測，不是精準判定——精準判定仍以 shot_index.json 的 hasFace 標記為準。
本模組給 broll_engine 在 config 沒明列 faceDenyAssets 時當降級預設，也可在開新專案
時人工核對用。

用法：
  python3 app/asset_profile.py --project p2                # 印口白秒數表 + 建議名單
  python3 app/asset_profile.py --project p2 --threshold 15

程式介面：
  spoken_seconds(project) -> dict[assetId, float]
  suggest_face_deny(project, threshold=20.0) -> list[str]
"""
import argparse
import json
from collections import defaultdict

try:
    import project_paths                      # python3 app/X.py 直接執行（app/ 在 sys.path）
except ModuleNotFoundError:                   # 被以 app.X 匯入時（如 render_p1 匯入 music_beats）
    from app import project_paths

DEFAULT_THRESHOLD = 20.0


def spoken_seconds(project) -> dict:
    """回傳 {assetId: 總口白秒數}，資料來自 Project.transcript 的 segments[]。"""
    data = json.loads(project.transcript.read_text(encoding="utf-8"))
    out = defaultdict(float)
    for seg in data.get("segments", []):
        aid = seg.get("assetId")
        if not aid:
            continue
        dur = seg.get("sourceEnd", 0) - seg.get("sourceStart", 0)
        out[aid] += max(0.0, dur)
    return dict(out)


def suggest_face_deny(project, threshold: float = DEFAULT_THRESHOLD) -> list:
    """回傳口白秒數 >= threshold 的資產清單（排序），供 fusion.faceDenyAssets 自動預設。"""
    secs = spoken_seconds(project)
    return sorted(a for a, s in secs.items() if s >= threshold)


def main():
    ap = argparse.ArgumentParser(
        description="資產口白量剖析（供 broll_engine 的 face-deny 自動推定使用）")
    project_paths.add_project_arg(ap)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"重口白判定門檻（秒），預設 {DEFAULT_THRESHOLD}")
    args = ap.parse_args()
    with project_paths.cli_guard():
        project = project_paths.load(args.project)

    secs = spoken_seconds(project)
    deny = set(suggest_face_deny(project, args.threshold))

    print(f"專案 {project.id}：{len(secs)} 個資產有口白紀錄（門檻 ≥{args.threshold}s）\n")
    print(f"{'assetId':<12}{'口白秒數':>10}")
    for a, s in sorted(secs.items(), key=lambda kv: -kv[1]):
        mark = "  ← 建議 deny" if a in deny else ""
        print(f"{a:<12}{s:>10.1f}{mark}")

    print(f"\n建議 fusion.faceDenyAssets（共 {len(deny)} 支）：")
    print("  " + "、".join(sorted(deny)) if deny else "  （無）")


if __name__ == "__main__":
    main()
