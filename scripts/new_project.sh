#!/bin/zsh
# 建立新專案：analysis/<id>/ manifest + 空 shot_index + config.json 模板，
# 並從既有專案複製同名配樂的 beats/*.json（比對 slug）、確保來源資料夾已被
# .gitignore 涵蓋（教訓：曾有 3.5GB 測試素材誤 git add 卡死）、更新
# analysis/current_project 指標指到新專案。
# 選填第三參數指定情境模板（templates/<template>/template.json），
# 以白名單淺合併套用其 hook/broll 鍵到 config.json；模板不得動
# music/target/fusion（契約見 templates/README.md）。
# 選填 --lang <code> 旗標指定素材語言（寫入 manifest 的 "language" 欄位，
# 驅動轉錄 prompt、字幕字型、校正策略等下游行為，見 app/project_paths.py 的
# Project.language 契約）；不給則預設 "zh"，manifest 仍會明確寫入該值。
#
# 用法: scripts/new_project.sh <id> <srcDir名> [template] [--lang <code>]
#   <id>       新專案 id（例：p3）。analysis/<id>/ 已存在即報錯，不覆蓋。
#   <srcDir名> ROOT 下的來源資料夾名稱（內含 .MOV 素材，可選 .mp3 配樂）。
#   [template] 選填，情境模板 id（見 templates/ 目錄）。不給則行為與無模板時完全相同。
#   --lang     選填，素材語言碼（例：zh、en）。不給則預設 zh。可與 [template] 並用、
#              不限順序（例：$0 p3 srcDir --lang en food　或　$0 p3 srcDir food --lang en）。
set -euo pipefail
setopt null_glob

ROOT=${0:A:h:h}

LANG_CODE="zh"
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lang)
      if [[ $# -lt 2 ]]; then
        print -u2 -- "--lang 需要接一個語言碼參數，例：--lang en"
        exit 1
      fi
      LANG_CODE="$2"
      shift 2
      ;;
    --lang=*)
      LANG_CODE="${1#--lang=}"
      shift
      ;;
    *)
      positional+=("$1")
      shift
      ;;
  esac
done
set -- "${positional[@]}"

if [[ $# -lt 2 ]]; then
  print -u2 -- "用法: $0 <id> <srcDir名> [template] [--lang <code>]　例：$0 myvlog footage/myvlog food --lang zh"
  exit 1
fi
ID="$1"
SRCDIR="$2"
TEMPLATE="${3:-}"
ANALYSIS_DIR="$ROOT/analysis/$ID"
SRC_PATH="$ROOT/$SRCDIR"
POINTER="$ROOT/analysis/current_project"
GITIGNORE="$ROOT/.gitignore"
TEMPLATES_DIR="$ROOT/templates"

if [[ -e "$ANALYSIS_DIR" ]]; then
  print -u2 -- "專案 '$ID' 已存在：$ANALYSIS_DIR"
  exit 1
fi

if [[ ! -d "$SRC_PATH" ]]; then
  print -u2 -- "來源資料夾不存在：$SRC_PATH"
  exit 1
fi

# --- 選填模板驗證：不存在就報錯並列出 templates/ 下所有可用模板 id + label ---
if [[ -n "$TEMPLATE" ]]; then
  TEMPLATE_JSON="$TEMPLATES_DIR/$TEMPLATE/template.json"
  if [[ ! -f "$TEMPLATE_JSON" ]]; then
    print -u2 -- "模板 '$TEMPLATE' 不存在：$TEMPLATE_JSON"
    print -u2 -- "可用模板："
    ROOT="$ROOT" python3 - <<'PYEOF' >&2
import json
import os
from pathlib import Path

tdir = Path(os.environ["ROOT"]) / "templates"
found = []
if tdir.is_dir():
    for p in sorted(tdir.iterdir()):
        tf = p / "template.json"
        if p.is_dir() and tf.exists():
            try:
                data = json.loads(tf.read_text(encoding="utf-8"))
                found.append(f"  {data.get('id', p.name)}\t{data.get('label', '(無 label)')}")
            except Exception as e:
                found.append(f"  {p.name}\t(解析失敗：{e})")
print("\n".join(found) if found else "  （無可用模板）")
PYEOF
    exit 1
  fi
fi

mkdir -p "$ANALYSIS_DIR"

# --- .gitignore 涵蓋檢查：未涵蓋就 append，防大型素材誤入版控 ---
if git -C "$ROOT" check-ignore -q -- "$SRCDIR" 2>/dev/null; then
  gitignore_status="已被既有規則涵蓋"
else
  {
    print ""
    print "# 自動新增（scripts/new_project.sh $ID，$(date +%Y-%m-%d)）：srcDir 尚未被既有規則涵蓋"
    print "/$SRCDIR"
  } >> "$GITIGNORE"
  gitignore_status="未涵蓋 → 已 append 至 .gitignore"
fi

# --- manifest / shot_index / config.json / beats 複製：交給 python 處理 JSON 與 slug 比對 ---
ID="$ID" SRCDIR="$SRCDIR" ROOT="$ROOT" TEMPLATE="$TEMPLATE" LANG_CODE="$LANG_CODE" python3 - <<'PYEOF'
import json
import os
import sys
from datetime import date
from pathlib import Path

root = Path(os.environ["ROOT"])
pid = os.environ["ID"]
srcdir = os.environ["SRCDIR"]
sys.path.insert(0, str(root))
from app.music_beats import slugify  # noqa: E402  沿用既有 slug 規則，不重複實作

src_path = root / srcdir
analysis_dir = root / "analysis" / pid

mp3s = sorted(p.name for p in src_path.iterdir()
              if p.is_file() and p.suffix.lower() == ".mp3")

template_id = os.environ.get("TEMPLATE", "")  # 空字串＝未指定模板，行為與改動前完全相同
lang_code = os.environ.get("LANG_CODE", "zh") or "zh"  # 空字串防呆，仍落回預設 zh

# 1) manifest
manifest = {
    "id": pid,
    "srcDir": srcdir,
    "createdAt": date.today().isoformat(),
    "music": mp3s,
    "hookCount": 3,
    "target": {"aspect": "16:9", "fps": 30, "resolution": "1920x1080"},
    "language": lang_code,
}
if template_id:
    manifest["template"] = template_id
(analysis_dir / "project.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# 2) 空 shot_index（引擎相容的空索引，MVP 無人工標註）
shot_index = {"schemaVersion": 2, "shots": {}}
(analysis_dir / "shot_index.json").write_text(
    json.dumps(shot_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# 3) config.json 模板（broll.strategy=balanced、protectChapters=["hook"]）
config = {
    "schemaVersion": 1,
    "target": {"durationSec": 180, "aspect": "16:9"},
    "music": {"primary": mp3s[0] if mp3s else "", "cues": []},
    "hook": {"enabled": True, "mode": "teaser", "count": 3},
    "broll": {
        "strategy": "balanced",
        "densityPerMin": 3.0,
        "minLen": 2.0,
        "maxLen": 3.5,
        "protectChapters": ["hook"],
        "protectClips": [],
        "allowReuse": False,
        "maxPerClip": 1,
        "scoreThreshold": 1.0,
        "deny": [],
        "fusion": {
            "enabled": True,
            "wKeyword": 1.0,
            "wVisual": 0.7,
            "kwCeil": 2.5,
            "visFloor": 0.25,
            "visCeil": 0.32,
            "fusedThreshold": 0.34,
            "minVisCos": 0.28,
            "topkPerQuery": 8,
            "faceDenyAssets": [],
        },
    },
}

# 3.5) 情境模板白名單淺合併（僅 hook/broll；music/target/fusion 等鐵律鍵一律不動，
#      白名單外的鍵印警告後忽略——防止模板越權，契約見 templates/README.md）
HOOK_WHITELIST = {"enabled", "mode", "count", "style"}
BROLL_WHITELIST = {
    "strategy", "densityPerMin", "minLen", "maxLen",
    "allowReuse", "maxPerClip", "protectChapters", "deny",
}
TEMPLATE_TOP_WHITELIST = {"id", "label", "description", "hook", "broll"}

template_label = None
template_overridden: list[str] = []
template_rejected: list[str] = []

if template_id:
    template_json_path = root / "templates" / template_id / "template.json"
    tpl = json.loads(template_json_path.read_text(encoding="utf-8"))
    template_label = tpl.get("label", template_id)

    for top_key in tpl:
        if top_key not in TEMPLATE_TOP_WHITELIST:
            template_rejected.append(top_key)

    tpl_hook = tpl.get("hook")
    if isinstance(tpl_hook, dict):
        for k, v in tpl_hook.items():
            if k in HOOK_WHITELIST:
                config["hook"][k] = v
                template_overridden.append(f"hook.{k}")
            else:
                template_rejected.append(f"hook.{k}")

    tpl_broll = tpl.get("broll")
    if isinstance(tpl_broll, dict):
        for k, v in tpl_broll.items():
            if k in BROLL_WHITELIST:
                config["broll"][k] = v
                template_overridden.append(f"broll.{k}")
            else:
                template_rejected.append(f"broll.{k}")

(analysis_dir / "config.json").write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# 4) 從既有專案複製同名音樂的 beats/*.json（比對 slug，取第一個找到的既有專案）
beats_dir = analysis_dir / "beats"
beats_dir.mkdir(parents=True, exist_ok=True)
analysis_root = root / "analysis"
copied, missing = [], []
for mp3 in mp3s:
    slug = slugify(mp3)
    found = None
    for proj_dir in sorted(analysis_root.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name == pid:
            continue
        cand = proj_dir / "beats" / f"{slug}.json"
        if cand.exists():
            found = cand
            break
    if found:
        (beats_dir / f"{slug}.json").write_bytes(found.read_bytes())
        copied.append(f"{slug} (取自 {found.parent.parent.name})")
    else:
        missing.append(slug)

print(f"[new_project] manifest → {analysis_dir / 'project.json'}（language={lang_code}）")
print(f"[new_project] shot_index → {analysis_dir / 'shot_index.json'}（空索引）")
print(f"[new_project] config → {analysis_dir / 'config.json'}（模板）")
print(f"[new_project] 掃到配樂 {len(mp3s)} 首：{mp3s if mp3s else '（無）'}")
print(f"[new_project] beats 複製：{copied if copied else '（無相符）'}")
if missing:
    print(f"[new_project] 配樂節拍尚未分析：{missing}（選用；不分析也能渲染，只是 B-roll 切點不會對齊拍點）")
    print(f"              要分析的話：.venv/bin/python app/music_beats.py --project {pid}")
if template_id:
    print(f"[new_project] 套用模板 {template_id}（{template_label}）："
          f"覆蓋了 {template_overridden if template_overridden else '（無鍵）'}")
    if template_rejected:
        print(f"[new_project] 警告：模板含白名單外／禁用鍵，已忽略（模板不得動 "
              f"music/target/fusion）：{template_rejected}")
PYEOF

# --- 更新 analysis/current_project 指標指到新專案（沿用既有檔案慣例：末尾帶換行）---
print -r -- "$ID" > "$POINTER"

print -r -- "[new_project] .gitignore：$gitignore_status"
print -r -- "[new_project] current_project 指標 → $ID"
print -r -- "[new_project] 完成：專案 '$ID'（srcDir=$SRCDIR）"
