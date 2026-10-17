#!/usr/bin/env bash
# 環境體檢：跑剪輯流程前先確認工具、套件、模型都「實際可用」，而不只是「存在」。
#
# 這支腳本的每一項檢查都對應一次真實事故——例如缺 numpy 不會報錯，而是讓切點精修
# 靜默跳過；模型被雲端同步清成佔位檔時，檔案大小照舊、內容卻讀不出來。
#
# 用法：scripts/doctor.sh        結束碼 0 = 可以開跑；1 = 有必須先處理的項目
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
fails=0; warns=0
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; warns=$((warns+1)); }
bad()  { echo "  ✗ $*"; fails=$((fails+1)); }

echo "== 系統工具 =="
if command -v zsh >/dev/null 2>&1; then ok "zsh（流程腳本以 zsh 撰寫）"
else bad "缺 zsh：流程腳本需要它（Linux：apt install zsh）"; fi

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  ok "ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')"
  # 先存進變數再比對，不要 `ffmpeg ... | grep -q`：grep -q 一命中就關管線，ffmpeg 還在寫
  # 輸出時收到 SIGPIPE 回傳非零，配上 pipefail 會把「找到了」誤判成「沒找到」。
  filters="$(ffmpeg -hide_banner -filters 2>/dev/null)"
  encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null)"
  grep -qE '^[[:space:]]*[^[:space:]]+[[:space:]]+subtitles[[:space:]]' <<<"$filters" \
    && ok "ffmpeg 支援 subtitles 濾鏡（燒字幕用，需 libass）" \
    || bad "ffmpeg 缺 subtitles 濾鏡：字幕燒不上去（需編入 libass）。Homebrew 的一般 ffmpeg 9.x 沒有 libass；macOS：brew install ffmpeg-full，再把 $(brew --prefix ffmpeg-full)/bin 加到 PATH 最前面——它是 keg-only，不會自動進 PATH"
  grep -q libx264 <<<"$encoders" \
    && ok "ffmpeg 支援 libx264" || bad "ffmpeg 缺 libx264 編碼器"
else
  bad "缺 ffmpeg / ffprobe（macOS：brew install ffmpeg-full，再把 $(brew --prefix ffmpeg-full)/bin 加到 PATH 最前面——它是 keg-only，不會自動進 PATH）"
fi

if command -v whisper-cli >/dev/null 2>&1; then
  help="$(whisper-cli --help 2>&1)"
  ok "whisper-cli：$(command -v whisper-cli)"
  grep -q -- '--dtw' <<<"$help" && ok "whisper-cli 支援 --dtw（token 級時間碼）" \
    || bad "whisper-cli 不支援 --dtw：版本太舊，切點會不準"
  grep -q -- '--vad' <<<"$help" && ok "whisper-cli 支援 --vad" \
    || warn "whisper-cli 不支援 --vad：會轉錄到更多環境雜音"
else
  bad "缺 whisper-cli（macOS：brew install whisper-cpp）"
fi

echo "== Python 環境 =="
if [[ -x "$PY" ]]; then
  ver="$("$PY" -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  case "$ver" in
    3.10|3.11|3.12|3.13) ok ".venv Python $ver" ;;
    *) warn ".venv Python $ver：torch 未必有這個版本的 wheel，建議 3.10–3.13" ;;
  esac
  for mod in numpy librosa torch transformers PIL; do
    if "$PY" -c "import $mod" 2>/dev/null; then ok "可 import $mod"
    else
      case "$mod" in
        numpy|librosa) bad "無法 import $mod：切點能量精修會靜默降級（字尾被切掉）" ;;
        *)             bad "無法 import $mod：視覺索引無法建立" ;;
      esac
    fi
  done
else
  bad "找不到 .venv。建立：python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

echo "== 模型 =="
readable_ggml() {
  local head
  head="$(dd if="$1" bs=4 count=1 2>/dev/null | od -An -c | tr -d ' \n')"
  [[ "$head" == "lmgg" ]]
}
check_model() {  # $1=檔名 $2=缺少時的嚴重度 bad|warn $3=說明
  local f="$ROOT/models/$1"
  if [[ ! -e "$f" ]]; then "$2" "缺 models/$1（$3）→ scripts/setup_models.sh"
  elif ! readable_ggml "$f"; then bad "models/$1 存在但讀不出內容——多半是雲端同步的佔位檔，見下方「資料夾位置」"
  else ok "models/$1 可讀（$3）"; fi
}
check_model ggml-large-v3-turbo-q8_0.bin bad  "主要 ASR 模型"
check_model ggml-silero-v6.2.0.bin       warn "VAD，濾掉無人聲片段"
[[ -e "$ROOT/models/ggml-medium.bin" ]] && check_model ggml-medium.bin warn "退回用 ASR 模型"

echo "== 資料夾位置與磁碟 =="
case "$ROOT" in
  "$HOME/Desktop"*|"$HOME/Documents"*)
    synced=0
    if [[ "$(uname)" == "Darwin" ]]; then
      for k in FXICloudDriveDesktop FXICloudDriveDocuments; do
        [[ "$(defaults read com.apple.finder "$k" 2>/dev/null)" == "1" ]] && synced=1
      done
    fi
    if (( synced )); then
      bad "專案在 $ROOT，而 iCloud「桌面與文件」同步已開啟：磁碟吃緊時素材與模型會被清成佔位檔。搬到 ~/Developer 或外接硬碟"
    else
      warn "專案在 ~/Desktop 或 ~/Documents 底下；若日後開啟 iCloud 同步，檔案可能被清成佔位檔"
    fi ;;
  *) ok "專案不在 ~/Desktop、~/Documents 底下" ;;
esac

# 找出「大小 > 0 但實際佔用 0 block」的佔位檔（素材與模型）
PYX="$PY"; [[ -x "$PYX" ]] || PYX="$(command -v python3)"
placeholders="$("$PYX" - "$ROOT" <<'PYEOF'
import sys, pathlib
root = pathlib.Path(sys.argv[1])
for sub in ("models", "footage"):
    d = root / sub
    if not d.exists():
        continue
    for p in d.rglob("*"):
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file() and st.st_size > 0 and st.st_blocks == 0:
            print(p.relative_to(root))
PYEOF
)"
if [[ -n "$placeholders" ]]; then
  bad "發現 $(wc -l <<<"$placeholders" | tr -d ' ') 個佔位檔（有大小、無資料）："
  sed 's/^/      /' <<<"$placeholders" | head -10
  echo "      補救：先清出磁碟空間，再實際讀一次檔案即會回補（例：cat 檔案 > /dev/null）"
else
  ok "models/ 與 footage/ 無佔位檔"
fi

use="$(df -P "$ROOT" | awk 'NR==2{gsub("%","",$5); print $5}')"
avail="$(df -h "$ROOT" | awk 'NR==2{print $4}')"
if (( use >= 90 )); then
  warn "磁碟已用 ${use}%（剩 $avail）：>90% 會讓視覺索引與渲染極慢、甚至逾時。實測索引 79 分鐘只用 1.27 秒 CPU"
else
  ok "磁碟已用 ${use}%（剩 $avail）"
fi

echo "== 校正 pass 的 LLM =="
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then ok "ANTHROPIC_API_KEY 已設定：校正可直接呼叫 API"
elif [[ -n "${OPENAI_API_KEY:-}" ]]; then ok "OPENAI_API_KEY 已設定：校正可直接呼叫 API"
else echo "  · 未設定 API key：校正 pass 走 Claude Code 代理路線（見 skill 第 6 步），不影響使用"; fi

echo
if (( fails )); then echo "✗ $fails 項必須先處理、$warns 項警告"; exit 1
elif (( warns )); then echo "⚠ 可以開跑，但有 $warns 項警告值得看一下"; exit 0
else echo "✓ 全部通過，可以開跑"; exit 0; fi
