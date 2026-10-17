# EDL 格式：`analysis/<id>/edit_decision.json`

剪輯決策表（EDL）描述「從哪支素材的幾秒到幾秒，依什麼順序接起來」。它是非破壞式的，
不會修改任何素材。這是故事規劃（第 8 步）的產出、渲染（第 11 步）的輸入。

用 Claude Code 跑 skill 時，這份檔案由它產生。不用 Claude Code 的話，你（或任何 LLM）
讀 `analysis/<id>/transcript.json` 後照這個格式寫出來即可。

## 最小可用範例

```json
{
  "schemaVersion": 1,
  "aroll": [
    {"id": "a01", "chapter": "intro", "assetId": "IMG_0001", "sourceIn": 1.2, "sourceOut": 9.8,
     "text": "今天要帶大家去吃這條街", "reason": "開場定位"},
    {"id": "a02", "chapter": "main",  "assetId": "IMG_0002", "sourceIn": 0.5, "sourceOut": 14.0,
     "text": "這家店我吃過很多次", "reason": "第一家店的推薦"}
  ],
  "broll": []
}
```

`assetId` 是素材檔名去掉 `.MOV`；`sourceIn` / `sourceOut` 是該素材內的秒數，
從 `transcript.json` 對應 segment 的 `sourceStart` / `sourceEnd` 取。

## 欄位

### 頂層

| 欄位 | 必填 | 說明 |
|---|---|---|
| `aroll` | ✓ | A-roll 片段陣列，**陣列順序就是成片順序** |
| `broll` | | B-roll 覆蓋陣列，可為空 |
| `hookCount` | | 片頭精華預告的段數（對應 `chapter: "hook"` 的片段數） |
| `music` | | 配樂設定，見下方；省略則無配樂 |
| `mustKeep` | | 必須出現在成片文字中的關鍵詞，驗證器會檢查 |
| `banned` | | 不可出現的詞，驗證器會檢查 |
| `target` | | 目標片長（選用、軟性）：`{"durationSec": 180}` 偏離 ±15% 時警告，或 `{"tolerance": [160, 200]}` 指定區間 |

### `aroll[]`

| 欄位 | 必填 | 說明 |
|---|---|---|
| `id` | ✓ | 唯一識別，B-roll 用它指定覆蓋哪一段 |
| `assetId` | ✓ | 素材 ID（檔名去掉 `.MOV`，只允許英數字、`_`、`-`） |
| `sourceIn` / `sourceOut` | ✓ | 素材內的起訖秒數 |
| `chapter` | ✓ | 章節名稱。`"hook"` 有特殊意義：片頭預告，不受順序規則限制 |
| `text` | | 這段說了什麼（驗證 `mustKeep` 用，也方便人讀） |
| `reason` | | 為什麼選這段——**強烈建議寫**，之後回頭覆核才知道當初的判斷 |

### `broll[]`

| 欄位 | 必填 | 說明 |
|---|---|---|
| `overAroll` | ✓ | 覆蓋哪一段 A-roll（填它的 `id`） |
| `startInClip` | ✓ | 從那段 A-roll 的第幾秒開始蓋 |
| `duration` | ✓ | 蓋多久（秒） |
| `assetId` / `sourceIn` | ✓ | B-roll 畫面來源 |
| `locked` | | `true` 時重跑 B-roll 引擎不會改動這一條 |
| `reason` / `confidence` | | 選擇理由與信心 |

B-roll 只換畫面，**聲音仍是 A-roll 的**。

### `music`

```json
"music": {
  "file": "bgm.mp3",
  "ducking": true,
  "cues": [
    {"file": "bgm.mp3",   "fromChapter": "hook", "toChapter": "main"},
    {"file": "calm.mp3",  "fromChapter": "outro", "toChapter": "outro"}
  ]
}
```

`file` 是素材資料夾內的 mp3 檔名。`ducking: true` 會在人聲出現時自動壓低音樂。
`cues` 選用：依章節切換不同配樂。

## 驗證器會擋下的錯誤

跑 `python3 scripts/validate_edl.py --project <id>`，以下任一項都會讓它回報錯誤：

- `sourceIn` / `sourceOut` 缺漏，或 `sourceOut <= sourceIn`
- 找不到來源檔，或 `sourceOut` 超過素材長度（A-roll 與 B-roll 都檢查）
- **時間順序倒退**：非 hook 的片段，`assetId` 必須遞增（手機素材的檔名編號即拍攝順序）
- **同一支素材內回頭**：時間軸上相鄰的兩段來自同一支素材時，後一段的起點不能早於前一段的終點
- B-roll 的 `sourceIn` / `duration` 缺漏或 `duration <= 0`
- `mustKeep` 的詞沒出現在任何 A-roll 的 `text`、`banned` 的詞出現了

警告（不擋渲染，但值得逐條看）：

- **切點沒對齊 segment 邊界**（±容許值）——可能切在一句話中間，最值得注意
- A-roll 短於 2 秒或長於 30 秒；B-roll 長度超出建議範圍
- 總長偏離 `target`

驗證器**不檢查** B-roll 是否落在它所覆蓋的 A-roll 範圍內，`startInClip + duration`
請自行確保不超過該段 A-roll 的長度。

## 規則背後的理由

- **順序不可重排**：把畫面關掉也要聽得懂，時間順序是最可靠的敘事骨架。唯一例外是片頭預告
- **只在句尾切**：`sourceIn` / `sourceOut` 對齊 transcript segment 的邊界，渲染器會再往後找
  語音結束後的安靜處做精修，避免字尾被切掉
- 完整方法論見 [`02-editorial-method.md`](02-editorial-method.md)
