#!/usr/bin/env bash
# 下載 ASR 模型到 models/，並以 Hugging Face 公布的 sha256 驗證完整性。
#
# 用法：
#   scripts/setup_models.sh                 # 必要模型：large-v3-turbo-q8_0（874 MB）＋ Silero VAD（0.9 MB）
#   scripts/setup_models.sh --with-fallback # 另載 medium（1.5 GB），large 不可用時的退回模型
#
# 已存在且 checksum 正確的檔案會跳過；下載中斷可重跑（curl 續傳）。
# Chinese-CLIP（視覺索引）不在這裡——第一次跑 app/visual_index.py 時由 transformers 自動下載。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/models"
mkdir -p "$DEST"

# 檔名 | Hugging Face repo | sha256（取自 HF LFS 中繼資料）
REQUIRED=(
  "ggml-large-v3-turbo-q8_0.bin|ggerganov/whisper.cpp|317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1"
  "ggml-silero-v6.2.0.bin|ggml-org/whisper-vad|2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"
)
FALLBACK=(
  "ggml-medium.bin|ggerganov/whisper.cpp|6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208"
)

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1; fi
}

# 「檔案存在」不等於「讀得到」：雲端同步資料夾的佔位檔大小欄位照舊、內容卻讀不出來。
# 這裡實際讀前 4 bytes，確認是 ggml 的 magic（'lmgg'）。
readable_ggml() {
  local head
  head="$(dd if="$1" bs=4 count=1 2>/dev/null | od -An -c | tr -d ' \n')"
  [[ "$head" == "lmgg" ]]
}

fetch() {
  local name="${1%%|*}" rest="${1#*|}"
  local repo="${rest%%|*}" want="${rest##*|}"
  local out="$DEST/$name"
  local url="https://huggingface.co/$repo/resolve/main/$name"

  if [[ -f "$out" ]] && [[ "$(sha256_of "$out")" == "$want" ]]; then
    echo "  ✓ $name 已存在且 checksum 正確，跳過"
    return 0
  fi
  echo "  ↓ $name  ←  $url"
  curl -L --fail --retry 3 --continue-at - -o "$out" "$url"

  local got; got="$(sha256_of "$out")"
  if [[ "$got" != "$want" ]]; then
    echo "  ✗ $name checksum 不符" >&2
    echo "      預期 $want" >&2
    echo "      實際 $got" >&2
    echo "    刪除後重跑：rm \"$out\" && $0" >&2
    return 1
  fi
  if ! readable_ggml "$out"; then
    echo "  ✗ $name checksum 正確但讀不出 ggml 檔頭——檔案可能被雲端同步清成佔位檔" >&2
    return 1
  fi
  echo "  ✓ $name 下載完成並驗證"
}

echo "[setup_models] 目的地：$DEST"
fail=0
for m in "${REQUIRED[@]}"; do fetch "$m" || fail=1; done
if [[ "${1:-}" == "--with-fallback" ]]; then
  for m in "${FALLBACK[@]}"; do fetch "$m" || fail=1; done
fi

if (( fail )); then
  echo "[setup_models] 有模型未就緒，見上方訊息" >&2
  exit 1
fi
echo "[setup_models] 完成。下一步：scripts/doctor.sh"
