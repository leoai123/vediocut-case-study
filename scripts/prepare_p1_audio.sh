#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}

# 多專案：第一個參數是專案 id（必填），經 app/project_paths.py 解析出 src/analysis
# 路徑（單一真相來源，不再自己拼 analysis/<proj> 或讀 VEDIOCUT_PROJ/VEDIOCUT_SRC）。
if [[ $# -lt 1 ]]; then
  print -u2 -- "用法: $0 <project-id>　例：$0 p1"
  exit 1
fi
PROJ="$1"

if ! resolved=$(python3 "$ROOT/app/project_paths.py" "$PROJ"); then
  print -u2 -- "解析專案 '$PROJ' 路徑失敗（見上方錯誤）"
  exit 1
fi

# 【穩定契約】下面解析的是 app/project_paths.py `__main__` 印出的 key=value 逐行
# stdout（見該檔 __main__ 段落註解）。這是 zsh 腳本讀取專案路徑的唯一介面；
# 要改「key=value 逐行」格式或砍掉某個 key 之前，必須先改完所有消費端
# （grep 'project_paths.py' scripts/*.sh 找出全部消費端）。
SRC=""; ANALYSIS=""
while IFS='=' read -r key val; do
  case "$key" in
    src) SRC="$val" ;;
    analysis) ANALYSIS="$val" ;;
  esac
done <<< "$resolved"

missing=()
[[ -z "$SRC" ]] && missing+=(src)
[[ -z "$ANALYSIS" ]] && missing+=(analysis)
if (( ${#missing[@]} > 0 )); then
  print -u2 -- "無法解析專案 '$PROJ' 的路徑：預期 key（${(j:, :)missing}）沒出現在 project_paths.py 輸出中（契約可能被改壞）"
  exit 1
fi

OUT="$ANALYSIS/audio"
mkdir -p "$OUT"

        # 單一壞檔（錄影中斷、iCloud「最佳化儲存空間」清掉本機資料的 SF_DATALESS
# 佔位檔…）不該讓整批中止——set -e 下 ffmpeg 一失敗就結束，前面做完的都還在但
# 後面的完全沒跑，且結尾統計看起來只是「數量不足」，不知道是誰壞了。改為跳過並
# 在最後大聲列出，讓缺漏是明講的而不是默默少檔。
typeset -a FAILED
for source in "$SRC"/*.MOV; do
  name=${source:t:r}
  target="$OUT/$name.m4a"
  if [[ ! -s "$target" ]]; then
    if ! ffmpeg -y -hide_banner -loglevel warning \
      -i "$source" -map '0:a:0?' -vn \
      -ac 1 -ar 16000 -c:a aac -b:a 48k \
      -movflags +faststart "$target"; then
      print -u2 -- "  ! 抽音訊失敗，跳過：$name（來源讀不到或已損毀）"
      rm -f "$target"
      FAILED+=("$name")
    fi
  fi
done
if (( ${#FAILED} )); then
  print -u2 -- ""
  print -u2 -- "  ⚠ ${#FAILED} 支素材無法抽音訊，已跳過：${FAILED[*]}"
  print -u2 -- "    這些素材不會進入逐字稿與剪輯。若是 SF_DATALESS 佔位檔（實佔 0 block），"
  print -u2 -- "    需先在「照片」下載原始檔或清出磁碟空間，再重跑本步驟。"
fi

for audio in "$OUT"/*.m4a; do
  ffprobe -v error \
    -show_entries format=filename,duration,size \
    -of csv=p=0 "$audio"
done > "$ANALYSIS/audio-manifest.csv"
