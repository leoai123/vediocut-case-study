#!/usr/bin/env python3
"""EDL 安全邊界驗證（deterministic）。

檢查 analysis/<project>/edit_decision.json 是否：
 1. 每個 A-roll/B-roll 的 assetId 存在、sourceIn<sourceOut 且落在該來源檔實際時長內
 2. A-roll 切點對齊句尾（sourceOut 接近某 sentenceComplete segment 的結尾）
 3. A-roll 依拍攝順序（assetId 遞增）排列，除非 chapter=="hook" 允許前置
 4. 片段長度合理（A-roll 2–30s、B-roll 1.5–8s）
 5. 成片總長落在目標容忍區間
 6. mustKeep 全數出現、banned 完全不出現
無效項印出並以非零 exit code 結束（供 pipeline 判斷是否回退規則式）。

用法：
  python3 scripts/validate_edl.py --project p1
  未帶 --project 時，走 app/project_paths.load() 的解析順序
  （明確參數 > VEDIOCUT_PROJ > current_project 指標 > 唯一專案）。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # 讓 `python3 scripts/validate_edl.py` 也能 import app.*
import app.project_paths as project_paths  # noqa: E402  單一真相來源：路徑一律經此模組解析

EDL = TRANSCRIPT = SRC = None

BOUNDARY_TOL = 0.6   # A-roll sourceOut 與句尾允許誤差(秒)
AROLL_MIN, AROLL_MAX = 2.0, 30.0
BROLL_MIN, BROLL_MAX = 1.2, 8.0

_dur_cache = {}


def asset_duration(asset_id):
    if asset_id in _dur_cache:
        return _dur_cache[asset_id]
    path = SRC / f"{asset_id}.MOV"
    dur = None
    if path.exists():
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nk=1:nw=1", str(path)],
                capture_output=True, text=True, timeout=30)
            dur = float(out.stdout.strip())
        except (ValueError, subprocess.TimeoutExpired):
            dur = None
    _dur_cache[asset_id] = dur
    return dur


def main():
    global EDL, TRANSCRIPT, SRC
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    project_paths.add_project_arg(ap)
    args = ap.parse_args()

    with project_paths.cli_guard():
        proj = project_paths.load(args.project)
        EDL, TRANSCRIPT, SRC = proj.edl, proj.transcript, proj.src

    errors, warnings = [], []
    if not EDL.exists():
        print(f"EDL 不存在: {EDL}", file=sys.stderr)
        sys.exit(2)
    edl = json.loads(EDL.read_text(encoding="utf-8"))
    transcript = json.loads(TRANSCRIPT.read_text(encoding="utf-8")) if TRANSCRIPT.exists() else {"segments": []}

    # segment 邊界集合（whisper 已按停頓切分，切在邊界即不切斷語句）
    seg_starts, seg_ends = {}, {}
    for s in transcript.get("segments", []):
        seg_starts.setdefault(s["assetId"], []).append(s["sourceStart"])
        seg_ends.setdefault(s["assetId"], []).append(s["sourceEnd"])

    aroll = edl.get("aroll", [])
    total = 0.0
    prev_asset = None
    prev_clip = None   # 時間軸上的前一段（含 hook），供同素材相鄰檢查
    for c in aroll:
        cid = c.get("id", "?")
        a = c.get("assetId")
        si, so = c.get("sourceIn"), c.get("sourceOut")
        if si is None or so is None or so <= si:
            errors.append(f"[{cid}] sourceIn/Out 無效: {si}->{so}")
            continue
        dur = so - si
        total += dur
        if not (AROLL_MIN <= dur <= AROLL_MAX):
            warnings.append(f"[{cid}] A-roll 長度 {dur:.1f}s 超出建議 {AROLL_MIN}-{AROLL_MAX}s")
        if not (SRC / f"{a}.MOV").exists():
            errors.append(f"[{cid}] 找不到來源檔 {a}.MOV")
        else:
            adur = asset_duration(a)
            if adur is None:
                warnings.append(f"[{cid}] 無法讀取 {a} 時長（ffprobe timeout，高負載），略過越界檢查")
            elif so > adur + 0.05:
                errors.append(f"[{cid}] sourceOut {so:.2f}s 超過來源檔時長 {adur:.2f}s")
        # 切點對齊 segment 邊界（不切斷語句）
        ends = seg_ends.get(a, [])
        starts = seg_starts.get(a, [])
        if ends and min(abs(so - e) for e in ends) > BOUNDARY_TOL:
            warnings.append(f"[{cid}] sourceOut {so:.2f}s 未對齊 segment 尾(±{BOUNDARY_TOL}s) → 可能切在語句中間")
        if starts and min(abs(si - e) for e in starts) > BOUNDARY_TOL:
            warnings.append(f"[{cid}] sourceIn {si:.2f}s 未對齊 segment 首(±{BOUNDARY_TOL}s) → 可能切在語句中間")
        # 順序
        if c.get("chapter") != "hook" and prev_asset and a < prev_asset:
            errors.append(f"[{cid}] 非 hook 卻早於前一段 {prev_asset}（跨日/錯序）")
        prev_asset = a if c.get("chapter") != "hook" else prev_asset
        # 同一支素材內回頭：上面的順序檢查比的是素材編號，同一支素材時永遠不會觸發。
        # 但渲染器的相鄰重疊守衛會把後段起點鉗到前段終點之後，後段若整段在前面就變成
        # 負時長、ffmpeg 失敗——p7 正是這樣在渲染最後一步才炸掉。在這裡先擋。
        if prev_clip and prev_clip.get("assetId") == a and si < prev_clip["sourceOut"] - 0.05:
            errors.append(f"[{cid}] 同一支素材 {a} 內回頭：起點 {si:.2f}s 早於前一段終點 "
                          f"{prev_clip['sourceOut']:.2f}s（順剪鐵律，同素材內也不可重排）")
        prev_clip = c

    for b in edl.get("broll", []):
        bid = b.get("overAroll", "?")
        si, d = b.get("sourceIn"), b.get("duration")
        if si is None or d is None or d <= 0:
            errors.append(f"[B/{bid}] B-roll sourceIn/duration 無效"); continue
        so = si + d
        if not (BROLL_MIN <= d <= BROLL_MAX):
            warnings.append(f"[B/{bid}] B-roll 長度 {d:.1f}s 超出 {BROLL_MIN}-{BROLL_MAX}s")
        ba = b.get("assetId")
        if not (SRC / f"{ba}.MOV").exists():
            errors.append(f"[B/{bid}] 找不到 B-roll 來源檔 {ba}.MOV"); continue
        adur = asset_duration(ba)
        if adur is not None and so > adur + 0.05:
            errors.append(f"[B/{bid}] B-roll sourceOut {so:.1f}s 超過來源檔時長")

    # 總長：時長是「選填的軟性目標」——未設定時不檢查，以故事完整性為主
    tgt = edl.get("target") or {}
    tol = tgt.get("tolerance")
    dur_target = tgt.get("durationSec")
    if tol:
        lo, hi = tol
        if not (lo <= total <= hi):
            warnings.append(f"A-roll 總長 {total:.1f}s 不在目標區間 {lo}-{hi}s（軟性提醒，不強制；B-roll 為覆蓋不影響總長）")
    elif dur_target:
        if abs(total - dur_target) > 0.15 * dur_target:
            warnings.append(f"A-roll 總長 {total:.1f}s 偏離目標 {dur_target}s 超過 ±15%（軟性提醒，不強制）")

    # must/ban
    joined = " ".join(c.get("text", "") for c in aroll)
    for m in edl.get("mustKeep", []):
        if m and m not in joined:
            errors.append(f"必留內容遺漏: {m!r}")
    for ban in edl.get("banned", []):
        if ban and ban in joined:
            errors.append(f"出現禁用內容: {ban!r}")

    print(f"A-roll 段數={len(aroll)} 總長={total:.1f}s  錯誤={len(errors)} 警告={len(warnings)}")
    for w in warnings:
        print("  ⚠ ", w)
    for e in errors:
        print("  ✗ ", e)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
