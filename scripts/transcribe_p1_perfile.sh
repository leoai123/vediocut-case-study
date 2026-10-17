#!/bin/zsh
# 逐字稿優先：逐檔轉錄（取代舊的 concat-WAV 做法）
# 每個來源檔各自轉錄，segment 時間碼即等於該來源檔的 sourceStart/End，
# 免去跨檔幻覺與時間碼回推。
#
# 用法: transcribe_p1_perfile.sh <project-id> [grep-pattern]
#   第一個參數：專案 id（必填），經 app/project_paths.py 解析出 analysis 路徑
#              （單一真相來源，不再自己拼 analysis/<proj> 或讀 VEDIOCUT_PROJ）。
#   第二個參數：可選，只跑檔名符合此 pattern 的音檔（預設 IMG_）。
set -euo pipefail
setopt null_glob

ROOT=${0:A:h:h}

if [[ $# -lt 1 ]]; then
  print -u2 -- "用法: $0 <project-id> [grep-pattern]　例：$0 p1"
  exit 1
fi
PROJ="$1"
PATTERN="${2:-IMG_}"

if ! resolved=$(python3 "$ROOT/app/project_paths.py" "$PROJ"); then
  print -u2 -- "解析專案 '$PROJ' 路徑失敗（見上方錯誤）"
  exit 1
fi

# 【穩定契約】下面解析的是 app/project_paths.py `__main__` 印出的 key=value 逐行
# stdout（見該檔 __main__ 段落註解）。這是 zsh 腳本讀取專案路徑的唯一介面；
# 要改「key=value 逐行」格式或砍掉某個 key 之前，必須先改完所有消費端
# （grep 'project_paths.py' scripts/*.sh 找出全部消費端）。
ANALYSIS=""
LANGUAGE=""
while IFS='=' read -r key val; do
  case "$key" in
    analysis) ANALYSIS="$val" ;;
    language) LANGUAGE="$val" ;;
  esac
done <<< "$resolved"

if [[ -z "$ANALYSIS" ]]; then
  print -u2 -- "無法解析專案 '$PROJ' 的路徑：預期 key（analysis）沒出現在 project_paths.py 輸出中（契約可能被改壞）"
  exit 1
fi
LANGUAGE="${LANGUAGE:-zh}"  # 契約缺 key 時的防呆預設，正常情況下 project_paths.py 一律會印 language

# 素材語言決定 whisper-cli 的 -l 與 --prompt：zh 沿用既有繁體偏置 prompt；
# en 用 -l en 且不帶 prompt（繁體偏置 prompt 對英文素材是雜訊）；
# 其他語言碼尚無專用設定，退回 -l auto、不帶 prompt，並提醒使用者。
lang_args=()
case "$LANGUAGE" in
  zh)
    # --prompt 繁體偏置：large-v3 家族對 zh 會隨機輸出簡體（p1 實測），
    # 此 prompt 鎖繁體字形；同音錯字（生鮮區/生死去類）交給 scripts/correct_transcript.py
    # 的 LLM 後校正 pass（glossary 驅動），兩者分工。
    lang_args=(-l auto --prompt "以下是繁體中文的逐字稿：")
    ;;
  en)
    lang_args=(-l en)
    ;;
  *)
    print -u2 -- "[warn] language=$LANGUAGE 尚無專用轉錄設定，使用 auto"
    lang_args=(-l auto)
    ;;
esac

AUDIO="$ANALYSIS/audio"
WAV="$ANALYSIS/audio/wav"
OUT="$ANALYSIS/transcripts/raw"
# ASR 模型：優先 large-v3-turbo（q8_0 量化，混語準度優於 medium），缺檔退回 medium。
# --dtw：token 級時間碼（whisper.cpp 內建 DTW 對齊），提升切點/字幕時序的標籤精度。
#   注意 DTW 與 flash attention 互斥，必須搭 -nfa（實測不加會被靜默停用 t_dtw=-1）；
#   關 flash attn 轉錄較慢（~20-30%），屬背景批次可接受。
# 注意：既有專案的 EDL 切點綁定當時轉錄的時間碼，換模型只影響「之後新轉錄的專案」，
# 不要對已完成剪輯的專案重轉錄。
          # 可讀性檢查而非 -s：iCloud「最佳化儲存空間」清掉本機資料後留下的 SF_DATALESS
# 佔位檔，st_size 仍是原始大小（-s 判定為存在），實際讀取卻回 0 bytes。p8 踩到這個坑——
# large-v3-turbo 被清成佔位檔，-s 判定通過、退回機制沒觸發，43 支全部以
# 「invalid model data (bad magic)」失敗。這裡實際讀一段來判定模型是否真的可用。
model_readable() {
  [[ -s "$1" ]] || return 1
  local n
  n=$(dd if="$1" bs=1k count=1 2>/dev/null | wc -c | tr -d ' ')
  [[ "${n:-0}" -gt 0 ]]
}
MODEL="$ROOT/models/ggml-large-v3-turbo-q8_0.bin"
DTW_NAME="large.v3.turbo"
if ! model_readable "$MODEL"; then
  if [[ -s "$MODEL" ]]; then
    print -u2 -- "[warn] $MODEL 存在但讀不到內容（多半是 iCloud 清除本機資料的佔位檔，"
    print -u2 -- "       實佔 0 block）→ 退回 medium。要恢復請下載原始檔或清出磁碟空間。"
  else
    print -u2 -- "[warn] 找不到 large-v3-turbo-q8_0，退回 medium：$MODEL"
  fi
  MODEL="$ROOT/models/ggml-medium.bin"
  DTW_NAME="medium"
fi
model_readable "$MODEL" || { print -u2 "ASR 模型無法讀取：$MODEL"; exit 1; }
VAD_MODEL="$ROOT/models/ggml-silero-v6.2.0.bin"

mkdir -p "$OUT" "$WAV"

[[ -s "$MODEL" ]] || { print -u2 "Missing ASR model: $MODEL"; exit 1; }
print -r -- "[asr] 使用模型：${MODEL:t}"

vad_args=()
if [[ -s "$VAD_MODEL" ]]; then
  vad_args=(--vad -vm "$VAD_MODEL" -vt 0.5 -vsd 200 -vp 40)
fi

for audio in "$AUDIO"/${PATTERN}*.m4a; do
  [[ -e "$audio" ]] || continue
  name=${audio:t:r}
  out="$OUT/$name"
  if [[ -s "$out.json" ]]; then
    print -r -- "skip (exists): $name"
    continue
  fi
  # whisper.cpp 只吃 WAV：先轉 16k 單聲道 PCM
  wav="$WAV/$name.wav"
  if [[ ! -s "$wav" ]]; then
    ffmpeg -y -hide_banner -loglevel error -i "$audio" \
      -vn -ac 1 -ar 16000 -c:a pcm_s16le "$wav"
  fi
  print -r -- ">>> transcribing $name (language=$LANGUAGE)"
  whisper-cli \
    -m "$MODEL" -f "$wav" \
    ${lang_args} -t 6 -bs 1 -bo 1 -sow -ml 42 -sns \
    --dtw "$DTW_NAME" -nfa \
    ${vad_args} \
    -ojf -osrt -otxt \
    -of "$out" 2> "$out.log" || {
      print -u2 "FAILED: $name (see $out.log)"; continue; }
done

print -r -- "done. json files:"
n=("$OUT"/*.json); print -r -- "${#n}"
