---
name: vediocut-flow
description: vediocut 剪輯全流程 runbook——從一個素材資料夾開案到成片輸出的標準步驟、每步驗收條件與已知坑。使用者說「用 vediocut 剪這個資料夾」「幫我把這些素材剪成一支 vlog」「重新剪輯」「出第二版」時使用。
---

# vediocut 剪輯全流程 runbook

逐字稿優先順剪 pipeline，12 步從素材到成片。每步含指令、驗收條件、已知坑。
**校正 pass（第 6 步）不可跳過**——p5 v1 事故：0/321 段校正，字幕帶著「物超壽質」等
錯字出片，才有這份 skill。每步跑完先看驗收條件，過了才進下一步。

> 文中的 p1–p9 是開發期間的內部測試專案（素材未公開），用來標明每條規則是從哪次
> 事故長出來的。照做就好，不需要知道那些專案的內容。

**開始前先跑 `scripts/doctor.sh`**——它檢查 ffmpeg、whisper-cli、Python 套件、模型是否
**實際可讀**、磁碟空間、以及專案是否放在雲端同步資料夾裡。任一項不過就先處理，不要硬跑。

## 三條貫穿全流程的原則（2026-07-25 定案）

1. **不要建詞表，讓模型用世界知識**（第 2、6 步）。人工詞表不只沒效率，還會把協調者
   的猜測固化成下游的權威依據——實測 Haiku 照錯詞表把「燒臘店」改成「燒肉店」，
   而零詞表的 Sonnet 從上下文推對了。glossary 只留私有詞（寵物名之類）。
2. **不要在 prompt 裡寫死地區或列可疑詞**（第 6 步）。寫死地區跟寫死詞表是同一類錯誤；
   讓模型從逐字稿自己推斷地區/領域，同一套流程才能跨地區通用（裸考已驗證可行）。
3. **「我看不懂」不等於「該剪掉」**（第 8 步）。可疑詞先試還原 → 抽幀/搜尋驗證 →
   語意可推斷就照播 → 只有真正會誤導觀眾才剪。p6 v1 因違反這條誤剪四段有價值內容。

## 1. 素材偵察

```bash
for f in <srcDir>/*.MOV <srcDir>/*.mov; do
  ffprobe -v error -show_entries format=duration:stream=codec_name,width,height \
    -of default=nk=1:nw=1 "$f" 2>/dev/null
done
ffmpeg -i <代表性檔案>.MOV -vf "select='eq(n\,0)+eq(n\,300)+eq(n\,600)'" -vsync vfr \
  /tmp/frame_%d.jpg   # 抽 3-4 張判斷內容類型（食物/開箱/講解…）與可能模板
ls <srcDir>/*.mp3      # 檢查配樂——pipeline 設計是專案自帶音樂，無 mp3 要先補進 srcDir
ls <srcDir> | grep -vi '\.mov$\|\.mp3$'   # 非 MOV 檔（螢幕錄影等）
```
**驗收**：拿到時長分布、編碼、內容類型判斷、mp3 是否齊、**檔名與方向已過檢**。
**已知坑**：`list_assets()`（app/project_paths.py）只認 `.MOV`/`.mov`（大小寫皆可），
其他容器（如螢幕錄影 .mp4）會被靜默忽略、不進 pipeline——要嘛移出 srcDir 要嘛轉檔成 .MOV。

### 1a. 檔名體檢（p6 事故：8 支檔名含空格）
```bash
ls <srcDir> | grep " "     # macOS Finder 複製會產生「IMG_1234 2.MOV」
cd <srcDir> && for f in *" "*.MOV; do mv -n "$f" "${f// /_}"; done
```
`render_p1.py` / `edit_server.py` 的 `ASSET_ID_RE = ^[A-Za-z0-9_-]+$` 會拒絕含空格的
assetId——**不改就渲染必失敗**，且錯誤發生在流程最末端，代價最高。

### 1b. 方向體檢（p6 事故：5 支旋轉錯誤，若沒抓到成片會有 3 分鐘躺著的畫面）
儲存尺寸 ≠ 顯示方向：rotation metadata 會翻轉。而 metadata 本身可能是錯的。
```bash
# 逐支列 storage / rotation，並用 contact sheet 目視內容方向
ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
  -show_entries stream_side_data=rotation -of default=nw=1 "$f"
# 一次看完 24 支：每支抽一幀縮圖拼網格
i=0; for f in <srcDir>/*.MOV; do i=$((i+1)); ffmpeg -y -v error -ss 3 -i "$f" -frames:v 1 \
  -vf "scale=240:240:force_original_aspect_ratio=decrease,pad=240:240:(ow-iw)/2:(oh-ih)/2:color=gray,\
drawtext=text='$i':fontsize=28:fontcolor=yellow:box=1:boxcolor=black@0.7:x=4:y=4" /tmp/g/$(printf '%02d' $i).jpg; done
ffmpeg -y -pattern_type glob -i "/tmp/g/*.jpg" -filter_complex "tile=6x4" /tmp/g/GRID.jpg
```
判定與修法（**先用 `-noautorotate` 看原始儲存方向再決定**）：
- 內容正立、只是 rotation 多餘 → `ffmpeg -display_rotation 0 -i in -c copy -map 0 out`（無損）
- 內容真的躺著 → `ffmpeg -noautorotate -i in -vf transpose=1 -c:v libx264 -crf 16 -c:a copy out`
  （transpose=1 順時針 90°；轉錯就是顛倒，先抽幀比對 tp1/tp2 再決定）
- **重編碼後要再查一次 rotation**：原檔的 rotation 會被複製到輸出，與 transpose 疊加變顛倒
  （p6 有一支就是這樣被我自己弄顛倒，抽幀才發現）→ 再跑一次 `-display_rotation 0 -c copy`
- 原檔備份到 `<srcDir>/_orig_rotation/`（子目錄，`list_assets()` 用 `iterdir()` 不會掃到）

## 2. 開案

```bash
scripts/new_project.sh <id> <srcDir> [template] [--lang <code>]
```
`<srcDir>` 是相對專案根目錄的路徑，建議放在 `footage/<名稱>/`（已在 `.gitignore`）。
選模板前對照 `templates/*/story_brief.md` 的適用情境（食物/開箱/講解/房產…），不確定
就先不帶 template 走通用結構。輸出規格在 `analysis/<id>/project.json` 的 `target`，預設
`1920x1080`／30fps；迭代 EDL 時用渲染的 `--preview`（720p 快速模式）即可，不必改 manifest。

### glossary：**預設留空**（2026-07-25 策略調整）
**不要花時間建詞表。** 專名還原已改由第 6 步的校正 pass 用世界知識處理，`glossary`
降級為「例外補丁」——只放**模型與搜尋都查不出、而只有使用者知道的私有詞**
（例：寵物或家人的名字）。典型專案填 0–3 詞，多數專案留空。

**人工詞表不只沒效率，它會主動誤導**（p6 鐵證）：協調者依「三燒飯」推測填入
「燒**肉**店」，Haiku 照詞表改成燒肉店；同一批素材給零詞表的 Sonnet，反而從上下文
推對「燒**臘**店」（三燒飯＝燒臘三寶飯、自選蔥醬）。**詞表把協調者的猜測固化成
下游的權威依據，讓模型不再用自己的知識。** 另有 p5 案例：憑印象填了一個店名，
抽幀看到招牌才發現是完全不同的名字。

若真要填，鐵律不變：**每個詞都要有畫面或事實佐證，不確定就不填**。
**驗收**：`analysis/<id>/{project.json,shot_index.json,config.json}` 三檔存在；
`project.json` 的 `target.resolution` 符合需求；`glossary` 留空或只有私有詞。
**已知坑**：srcDir 未涵蓋進 `.gitignore` 會被 `new_project.sh` 自動 append，不用手動處理；
但若曾經手動 `git add` 過大型素材，`.gitignore` 救不了已加入索引的檔案。

## 3. 抽音訊

```bash
scripts/prepare_p1_audio.sh <id>
```
**驗收**：`analysis/<id>/audio/*.m4a` 數量 == srcDir 內 .MOV 數量。

## 4. 轉錄

```bash
scripts/transcribe_p1_perfile.sh <id> &   # 長跑，背景執行
```
**驗收**：`analysis/<id>/transcripts/raw/*.json` 數量與音檔數量到齊才算完成。
**已知坑**：轉錄凍住時別空等——`ps -o time= -p <pid>` 看 CPU 時間是否還在漲，
沒漲就 `sample <pid>` 看堆疊確認卡在哪。

## 5. 合併

```bash
python3 scripts/merge_transcript.py --project <id>
```
記下印出的 segment 數與總口白秒數，後面規劃/驗證要用來對照。
**驗收**：`analysis/<id>/transcript.json` 存在，segments 非空。

## 6. 校正 pass（必跑，禁止跳過——p5 v1 事故）

有 `ANTHROPIC_API_KEY`：
```bash
python3 scripts/correct_transcript.py --project <id> --dry   # 先看 diff
python3 scripts/correct_transcript.py --project <id>          # 確認後寫回
```
無 key 走 Claude Code 離線三步：
```bash
python3 scripts/correct_transcript.py --project <id> --emit-batches /tmp/<id>_batches.json
# 派 Sonnet 代理（不是 Haiku）讀批次檔，依 system prompt 產出 corrections JSON:
#   [{"index":int, "corrected":str, "confidence":"high|medium|low", "note":str}]
python3 scripts/correct_transcript.py --project <id> --apply /tmp/<id>_corrections.json --dry
python3 scripts/correct_transcript.py --project <id> --apply /tmp/<id>_corrections.json
```

### 這一步的核心是「世界知識」，不是查表（2026-07-25 策略調整）
prompt 已改為兩段式：**先讓模型從逐字稿推斷地區/領域/詞彙圈，再用該脈絡的知識還原
可疑詞**。所以：

- **派 Sonnet 級代理，不要派 Haiku**——關鍵能力是在地專名知識，這是 Haiku 的弱項。
  實測同一批素材：Haiku＋人工詞表 8 筆，Sonnet＋零詞表 **17 筆**，且 Haiku 那 8 筆
  裡還有一筆是照錯詞表改錯的。`llm_providers.py` 的 API 路徑預設也已改 `claude-sonnet-5`。
- **不要在給代理的 prompt 裡指定地區或列出可疑詞清單**。prompt 本身刻意不預設地區
  （這樣才能跨地區通用），而列清單＝洩題，會讓你無法分辨模型是真的懂還是照抄。
  裸考實測：代理在不知拍攝地點下自行推斷出正確地區與領域，並還原出協調者原本
  判定「語意不明」的詞。
- **不要告訴代理影片拍什麼**，只給檔案路徑與「system 欄位是你的完整規則」。

`--apply` 的 guardrails：批次成員檢查、長度差 >40%（`MAX_LEN_RATIO`）直接丟棄、
長度差 >2%（`REWRITE_WARN_RATIO`）印「⚠ 需人工覆核」、
段數/assetId/sourceStart/sourceEnd/textRaw 任一被動到就 assert 失敗中止。

**驗收**：
1. stdout 的「改動 N 段（guardrail 丟棄 M 筆）」確認 N > 0
2. **看「⚠ 需人工覆核」警告**（長度變化 >2%）——純同音字替換的長度變化必為 0%，
   非 0% 就要判斷是良性（如英文拼字 `Olet→Outlet` 會變長）還是越權改寫。
   p6 v1 事故：Haiku 把「但如果要找過季的話 我推薦…」改成「所以我推薦…」刪掉整個
   子句，長度只差 18% 沒觸發 40% 門檻——**guardrail 抓不到刪子句**，這道警告是補丁。
3. **看「⚠ 低信心（medium/low）」警告**並記進 story.json：這些會寫進 transcript 的
   `correctionConfidence`／`correctionNote` 欄位。medium/low 不是「不可信」而是
   「需要在第 8 步用畫面或搜尋驗證」——別當成沒發生。
4. 抽查幾個修正詞在 `transcript.json` 的 `text` 欄位（非 `textRaw`）確實已生效

**協調者也要覆核，不要全交給代理**：p6 有一筆「平價正面」兩個代理都放棄，協調者
發現「平價/評價」完全同音、上下文在講店家口碑 → 補上「評價正面」。人工覆核補的是
代理漏的，不是重做代理的工作。

**已知坑**：`textRaw` 永遠是原始 ASR 輸出，`text` 才是校正後——下游（字幕、規劃）
一律該讀 `text`；跳過這步等於全片沿用未校正的 `textRaw`，這正是 p5 v1 事故的成因。

## 7. 視覺索引

```bash
PYTHONUNBUFFERED=1 .venv/bin/python app/visual_index.py build --project <id> --model v2 &
```
**驗收**：`analysis/<id>/visual_index_v2.json` 存在且 non-empty，log 末行「N 幀 / M assets」
的 assets 數與 MOV 數一致（0 資產會直接報錯不會靜默）。
**已知坑**：必須用 `.venv/bin/python`——系統 `python3` 缺 torch/numpy 會直接報錯或
給出退化結果；`PYTHONUNBUFFERED=1` 讓長跑進度即時可見，不然背景執行時看不到進度。
**索引「看起來死掉」其實多半是磁碟太慢（p6 定案）**：把 log 導到
`analysis/<id>/visual_index.log`（不要只靠 harness 的 output 檔），才能看出進度到哪。
p6 三次「失敗」的真相是**它其實會成功、只是要 60 分鐘**——實測限 3 支素材時
**CPU 只用 17 秒、牆鐘卻 18 分 16 秒、CPU 使用率 1%**，99% 時間在等磁碟 I/O。
根因是磁碟 97% 滿 → page cache 容不下 torch 的數千個套件檔案 → 每次 import 都重讀磁碟。
程式碼本身沒問題（已有 16 張/批的分批與 `torch.no_grad()`）。
**所以**：先看磁碟使用率（`df -h .`），>90% 就先清空間再跑；判斷是否還活著用
`ps -o time`（CPU 時間有沒有在漲）而不是等牆鐘；給它 60 分鐘再判死。
**不要卡在這一步**：試 2 次不成就跳到第 10 步走人工抽幀配 B-roll（p4/p6 都這樣出片），
把狀況記進 story.json。B-roll 從來不是成片的必要條件。

## 8. 故事規劃

讀 `analysis/<id>/project.json` 的 `template` 欄位 → 對應 `templates/<t>/story_brief.md`
當敘事簡報。**規劃前逐項過 checklist**（本 skill 核心）：

- **重複句掃描**：相鄰或同段的重講、false start、換句重說（同一意思講兩次，
  只留較完整的那一次）
- **連貫性 beat 檢查**：章節交界要有橋接句（例：吃飯中 →「全部吃完了」→ 出店總評）；
  埋的伏筆要有回收；跳章前確認語意銜接
- **環境音污染標記**：餐廳/市場等多人場所素材，逐段檢查後半是否鄰桌對話被轉錄成
  旁白（受污染區間禁選，記入 `story.json` 的 `asrQuality` 欄位，含 `issue`/`affected`/
  `handling`/`productNote`）
- **whisper 長段標籤**：一句話被標成極長時長（如 90 秒）＝該段後面是無語音長尾，
  選段要手動收窗，不能整段照抄時間碼。**且字幕會連帶消失**——build_srt 的中點規則
  會因 segment 中點遠超切點而排除該句（p5 a13c 實例），此時要把 transcript.json 該
  segment 的 sourceEnd 修正為實際語音結束點再重渲染
- **可疑詞的四階處置（2026-07-25 修正——「語意不明」不再等於剪掉）**：
  使用者回報 p6 v1「剪得太嚴謹」，根因是舊規則只有「語意不明→避開」一條。改成：
  1. **先試還原**：這是不是某個專名的誤聽？用世界知識比對。第 6 步的 confidence
     標記就是入口——`correctionConfidence` 為 medium/low 的段落優先查
  2. **抽幀或搜尋驗證**：招牌、菜單、商品標籤常常就在畫面裡；店名可搜尋驗證
  3. **語意可推斷就照播**：口語不完美但觀眾聽得懂的（例：講醬料時說「超之多」），
     保留原文不剪。**不要為了一個錯字剪掉整句有價值的推薦**
  4. **只有真正無法還原且會誤導觀眾**（多人重疊、環境音污染）才剪
  p6 v1 誤剪四段（一個品項推薦、一段口味評價、一句醬料形容、一句口碑描述），
  全是第 1–3 階就該救回的
- **順剪鐵律**：時間順序不可重排，唯一例外是 hook 抽句。
  **「同素材內重排」不是例外**——渲染器的相鄰同來源重疊守衛會把回頭段的 eff_in 鉗到
  前段之後，eff_out 卻還在原位 → 負時長、ffmpeg exit 222。p7 我把一段總評從 90s 挪到
  同素材 153s 之後當收束就踩到了。真要重排請先問自己是不是講者本來就結論先行——
  多半照他的順序更自然

產出 `analysis/<id>/story.json`（含 `templateFit`：`{"applied","verdict":"full"|"partial",
"note"}` 判定與理由）＋ `analysis/<id>/edit_decision.json`。
**驗收**：story.json 五個 checklist 項目都能在 `asrQuality`/`notes`/chapters 描述中找到
對應處置，不是空手放過。

## 9. 驗證

```bash
python3 scripts/validate_edl.py --project <id>
```
**驗收**：exit code 0、錯誤數 0；警告逐條讀懂原因再決定是否處理，不要無腦忽略。

## 10. B-roll

```bash
.venv/bin/python app/broll_engine.py --project <id> --v2 --dry
```
**逐一抽幀人工覆核**每個候選 B-roll 指派（引擎會語意錯配，非充分可信）。錯配的手動
改 `edit_decision.json` 對應項並標 `"locked": true`（重跑引擎會原樣保留 locked 條目、
不重新指派）。黃金鏡頭（依模板 brief，如上菜揭曉瞬間、第一口反應、抵達第一眼）絕不
可被 B-roll 蓋掉，必須是乾淨 A-roll。

### 判斷自動候選該不該用（兩個退化訊號）
1. **同一素材被重複選中多次**：p6 有一支店門口自拍被選 5 次，派去蓋四個完全無關的
   段落（規劃說明、另外兩家店的介紹、收尾道別）——這是「引擎找不到差異」的
   訊號，不是「這素材很萬用」
2. **cos 值落在 0.42–0.55 區間**：這是雜訊區，代表「畫面近似」而非「語意對應」。
   語意真的對上時 cos 通常更高，且 reason 會指得出具體對應物

**同質素材上不要期待自動檢索（三專案定論）**：p2 擋 12 段、p4 全擋、p6 部分穿透但
語意錯配。共同點是「整支都是同一場景的自拍講話」——CLIP 只能判斷「這些都是街景」。
**這不是 sepMargin 門檻高低的問題，調門檻救不了**；要救得換路線（例如先用 LLM 讀
逐字稿產生「該配什麼畫面」的語意描述，再拿描述去檢索，而不是拿整句台詞硬比）。
此類素材直接走人工抽幀，密度低於模板預設是**誠實反映素材性質**，不是妥協。

**驗收**：抽幀覆核每個 B-roll 窗口語意對得上口白；黃金鏡頭窗口內無 B-roll；
拒絕的自動候選要把理由記進 story.json（`brollDecision`）。

## 11. 渲染

```bash
.venv/bin/python scripts/render_p1.py --project <id>
```
**必用 `.venv/bin/python`**——系統 python 跑會讓切點能量精修靜默降級（啟動時有警告，
看到就停下換 python 重跑）。改 EDL 迭代時先 `--preview`（720p 快速預覽）確認再出正式版；
重算切點前先清快取：
```bash
rm -rf .p<id>-render
```
**驗收**：`output/<id>/final.mp4` + `final.srt` 產出，時長與 story.json 估算落在合理區間。

## 12. 法醫驗證

1. 字幕時序：讀 `output/<id>/final.srt` 逐條檢查無時間重疊、無超過片長、無相鄰重複
   字幕（全片重複句除 hook 重播外應為零）
2. 影格抽查：B-roll 合成點畫面正確、黃金鏡頭確實露臉、字幕字級/斷行正常
3. `ffprobe` 確認輸出是 1080p、時長與預期一致
4. （選用）把 story.json 的處置紀錄與成片規格整理給使用者——`analysis/`、`output/` 預設
   不進版控，素材與逐字稿是使用者的私人資料

**驗收**：前三項全過才算成片可交付；任一項不過退回對應步驟，不要在渲染後小改字幕檔了事。

---

## 已知坑速查

- **venv vs 系統 python**：系統 python3 缺 numpy/torch，`visual_index.py`/`render_p1.py`
  一律用 `.venv/bin/python`
- **Python stdout 緩衝**：背景長跑腳本加 `PYTHONUNBUFFERED=1` 或 `-u`，不然看不到進度
- **長工凍住診斷**：`ps -o time= -p <pid>` 看 CPU 時間是否還在漲，沒漲用 `sample <pid>`
  看堆疊，別空等
- **ffprobe csv 尾逗號**：抽單一數值用 `-of default=nk=1:nw=1`，不要用 `csv=p=0`
- **ffmpeg 高負載 -60 逾時**：`render_p1.py` 預設走多 pass 拆解 filtergraph，已規避
- **whisper 標籤系統性早 0.3s**：`SUB_DELAY` 已補償，不用再手調
- **large-v3 隨機輸出簡體**：繁體偏置 prompt 已寫進 `transcribe_p1_perfile.sh`
- **模板 `broll.strategy` 必須 `"custom"`**：填 preset 名（balanced/minimal/rich）會被
  `resolve_broll_cfg()` 強制覆蓋密度值，模板數值形同虛設
- **相機 MOV 大小寫**：`list_assets()` 對 `.MOV`/`.mov` 皆可，但 `render_p1.py` 等下游
  硬拼 `f"{assetId}.MOV"`，在區分大小寫的檔案系統上可能對不上
- **新 srcDir 的 gitignore**：`new_project.sh` 會自動偵測未涵蓋並 append，不用手動處理
- **磁碟 >90% 滿會拖垮整條流程**：page cache 容不下 torch 套件 → import 極慢（實測
  CPU 17s／牆鐘 18 分）；寫入的成片也會碎片化（p5 成片讀取掉到 47 KB/s，同期其他
  成片是 2 GB/s）。跑索引或渲染前先 `df -h .`，>90% 先清 `.p*-render`（可重建）
- **專案別放在 iCloud 同步的資料夾**（macOS「桌面與文件」同步會涵蓋 `~/Desktop`、
  `~/Documents`）：磁碟吃緊時系統會把素材、甚至 ASR 模型清成**佔位檔**——檔案大小照舊、
  實際佔用 0 block、讀取回 0 bytes **且不報錯**。p8 因此 43 支轉錄全滅（模型被清掉）、
  渲染卡在讀不到的素材上。放 `~/Developer` 或外接硬碟。`doctor.sh` 會檢查這件事；
  救回已被清掉的檔案：先清出磁碟空間，再實際讀一次檔案即會自動回補
- **`git commit -F -` 配背景執行會卡死**：heredoc 傳不進去，git 永遠等 stdin，還會留下
  `.git/index.lock` 讓後續所有 git 寫入失敗。改用 `-F <訊息檔>`。清 lock 前必先
  `pgrep git` 確認無活動程序（診斷法同長工：`ps -o time` 看 CPU 時間有沒有在漲）
