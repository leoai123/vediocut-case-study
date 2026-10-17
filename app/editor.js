"use strict";
let edl = null;
let dirty = false;
let polling = null;
let currentProject = null;   // 目前選取的專案 id（由 /api/projects 的 default 或使用者切換決定）
let projectsMeta = {};       // id -> {id, notes, createdAt, final, radio, vertical}

const $ = s => document.querySelector(s);
const fmt = n => (Math.round(n * 100) / 100);

// ---------- i18n ----------
const I18N = {
  zh: {
    page_title: "vediocut 剪輯介面",
    app_title: "vediocut 剪輯介面",
    app_subtitle: "逐字稿優先順剪 · 微調後重新渲染",
    project_label: "專案",
    reload_btn: "↻ 重新載入",
    save_btn: "儲存 EDL",
    render_h2: "渲染",
    opt_radio: "只出 A-roll radio（--radio，最快）",
    opt_nobroll: "不套 B-roll（--no-broll）",
    opt_clear: "清除切片快取（A-roll 微調/排序後必勾）",
    render_btn: "▶ 重新渲染",
    preview_btn: "預覽成片",
    check_edl_btn: "✓ 檢查 EDL",
    status_initial: "尚未查詢",
    render_hint: "radio 只重編 A-roll、最快，適合先確認順序與切點；完整渲染會套 B-roll、音樂、字幕，較久。",
    load_warn: "⚠ 系統忙碌，渲染可能失敗或很慢",
    vertical_h2: "9:16 直式輸出",
    vmode_label: "取景模式",
    vmode_crop: "crop（裁切填滿）",
    vmode_blur: "blur（模糊背景襯底）",
    render_vertical_btn: "▶ 輸出 9:16 直式",
    preview_vertical_btn: "預覽直式成片",
    vertical_hint_generic: "輸入來源：output/<project>/final.mp4（須先完成 16:9 渲染）。輸出：output/<project>/vertical.mp4。",
    vertical_hint: "輸入來源：output/{0}/final.mp4（須先完成 16:9 渲染）。輸出：output/{0}/vertical.mp4。",
    meta_h2: "片頭與音樂",
    hookcount_label: "精華片頭段數 hookCount（0＝關閉）",
    music_label: "音樂主檔（唯讀）",
    targetdur_label: "目標時長（秒，選填）",
    targetdur_placeholder: "留空＝不限",
    meta_status_label: "狀態 / 方法",
    meta_hint: "目標時長留空＝不限制成片長度、以故事完整性為主；填數字則為軟性目標（檢查 EDL 時 ±15% 提醒，不強制裁切）。",
    aroll_h2: "A-roll 主線",
    broll_h2: "B-roll 覆蓋",

    dirty_mark: "● 有未儲存變更",
    no_notes: "（無備註）",
    no_project: "（無專案）",
    meta_target_with_dur: "目標 {0}s",
    meta_target_unlimited: "不限（故事完整優先）",
    cues_th_file: "檔案",
    cues_th_chapter: "章節範圍",
    cues_th_note: "備註",
    segment_count: "共 {0} 段",
    locked_badge: "🔒 已鎖定",
    dur_label: "時長",
    move_up: "上移",
    move_down: "下移",
    lock_toggle_title: "鎖定/解鎖",
    unlock_btn: "解鎖",
    lock_btn: "🔒鎖定",
    delete_btn: "刪除",
    confirm_delete_aroll: "刪除 A-roll「{0} {1}」？",
    over_aroll_label: "覆蓋於 overAroll",
    not_exist_suffix: "（已不存在）",
    broll_dangling_warn: "⚠ 指向的 A-roll 已不存在，渲染時會被略過",
    confirm_delete_broll: "刪除 B-roll（{0} over {1}）？",
    no_broll_hint: "（沒有 B-roll）",

    save_success: "已儲存 {0}（A-roll {1}、B-roll {2}），已備份 edit_decision.bak.json",
    save_fail: "儲存失敗：{0}",

    confirm_render_dirty: "尚有未儲存變更，仍要用已存檔的 EDL 渲染？（建議先儲存）",
    render_started: "已開始渲染 {0}，請看下方狀態",
    render_start_fail: "無法開始渲染：{0}",
    vertical_render_started: "已開始 9:16 直式渲染 {0}，請看下方狀態",
    vertical_render_start_fail: "無法開始直式渲染：{0}",

    status_idle: "閒置",
    status_running: "渲染中…",
    status_done: "完成",
    status_failed: "失敗",
    status_vertical_suffix: "（9:16 直式）",
    status_other_project_suffix: "（非目前專案）",
    flag_clear_cache: "清快取",
    status_elapsed: " · 耗時 {0}s",
    render_done_toast: "渲染完成，可按「預覽」",
    vertical_render_done_toast: "9:16 直式渲染完成，可按「預覽」",
    render_failed_toast: "渲染失敗，見下方 log",
    load_label: "系統負載 {0}",
    fail_duration_guard: "⚠ 渲染失敗（時長異常守衛觸發）：\n{0}",
    fail_generic: "⚠ 渲染失敗：\n{0}",

    checking: "檢查中…",
    no_output: "（無輸出）",
    check_pass_toast: "EDL 檢查通過（0 錯誤）",
    check_fail_toast: "EDL 檢查發現錯誤，見下方輸出",
    check_fail_msg: "檢查失敗：{0}",

    confirm_switch_project: "切換專案會捨棄未儲存變更，繼續？",
    switched_project_toast: "已切換到專案 {0}",
    load_fail_toast: "載入失敗：{0}",
    confirm_reload: "重新載入會捨棄未儲存變更，繼續？",
    reloaded_toast: "已重新載入",
    init_load_fail_toast: "初始載入失敗：{0}",
  },
  en: {
    page_title: "vediocut Editor",
    app_title: "vediocut Editor",
    app_subtitle: "Transcript-first rough cut · fine-tune, then re-render",
    project_label: "Project",
    reload_btn: "↻ Reload",
    save_btn: "Save EDL",
    render_h2: "Render",
    opt_radio: "A-roll radio only (--radio, fastest)",
    opt_nobroll: "Skip B-roll (--no-broll)",
    opt_clear: "Clear clip cache (required after editing/reordering A-roll)",
    render_btn: "▶ Re-render",
    preview_btn: "Preview",
    check_edl_btn: "✓ Check EDL",
    status_initial: "Not checked yet",
    render_hint: "Radio mode only re-encodes the A-roll and is fastest — good for confirming order and cut points first. A full render adds B-roll, music, and captions, and takes longer.",
    load_warn: "⚠ System is busy — render may fail or be slow",
    vertical_h2: "9:16 Vertical Output",
    vmode_label: "Framing mode",
    vmode_crop: "crop (fill by cropping)",
    vmode_blur: "blur (blurred background fill)",
    render_vertical_btn: "▶ Render 9:16 vertical",
    preview_vertical_btn: "Preview vertical",
    vertical_hint_generic: "Source: output/<project>/final.mp4 (16:9 render must finish first). Output: output/<project>/vertical.mp4.",
    vertical_hint: "Source: output/{0}/final.mp4 (16:9 render must finish first). Output: output/{0}/vertical.mp4.",
    meta_h2: "Intro & Music",
    hookcount_label: "Highlight intro segment count — hookCount (0 = off)",
    music_label: "Music track (read-only)",
    targetdur_label: "Target duration (seconds, optional)",
    targetdur_placeholder: "Blank = no limit",
    meta_status_label: "Status / Method",
    meta_hint: "Leave target duration blank to prioritize story completeness with no length limit. Enter a number for a soft target — Check EDL flags it at ±15%, but won't force a cut.",
    aroll_h2: "A-roll Main Line",
    broll_h2: "B-roll Overlays",

    dirty_mark: "● Unsaved changes",
    no_notes: "(no notes)",
    no_project: "(no project)",
    meta_target_with_dur: "Target {0}s",
    meta_target_unlimited: "No limit (story completeness first)",
    cues_th_file: "File",
    cues_th_chapter: "Chapter range",
    cues_th_note: "Note",
    segment_count: "{0} segments",
    locked_badge: "🔒 Locked",
    dur_label: "Duration",
    move_up: "Move up",
    move_down: "Move down",
    lock_toggle_title: "Lock/unlock",
    unlock_btn: "Unlock",
    lock_btn: "🔒 Lock",
    delete_btn: "Delete",
    confirm_delete_aroll: 'Delete A-roll "{0} {1}"?',
    over_aroll_label: "Overlay on overAroll",
    not_exist_suffix: " (no longer exists)",
    broll_dangling_warn: "⚠ The A-roll it points to no longer exists — it will be skipped when rendering",
    confirm_delete_broll: "Delete B-roll ({0} over {1})?",
    no_broll_hint: "(No B-roll)",

    save_success: "Saved {0} (A-roll {1}, B-roll {2}) — backed up to edit_decision.bak.json",
    save_fail: "Save failed: {0}",

    confirm_render_dirty: "You have unsaved changes. Render using the saved EDL anyway? (Saving first is recommended)",
    render_started: "Started rendering {0} — check the status below",
    render_start_fail: "Couldn't start render: {0}",
    vertical_render_started: "Started 9:16 vertical render for {0} — check the status below",
    vertical_render_start_fail: "Couldn't start vertical render: {0}",

    status_idle: "Idle",
    status_running: "Rendering…",
    status_done: "Done",
    status_failed: "Failed",
    status_vertical_suffix: " (9:16 vertical)",
    status_other_project_suffix: " (not current project)",
    flag_clear_cache: "clear cache",
    status_elapsed: " · took {0}s",
    render_done_toast: "Render complete — click Preview",
    vertical_render_done_toast: "9:16 vertical render complete — click Preview",
    render_failed_toast: "Render failed — see the log below",
    load_label: "System load {0}",
    fail_duration_guard: "⚠ Render failed (duration-anomaly guard triggered):\n{0}",
    fail_generic: "⚠ Render failed:\n{0}",

    checking: "Checking…",
    no_output: "(no output)",
    check_pass_toast: "EDL check passed (0 errors)",
    check_fail_toast: "EDL check found errors — see the output below",
    check_fail_msg: "Check failed: {0}",

    confirm_switch_project: "Switching projects will discard unsaved changes. Continue?",
    switched_project_toast: "Switched to project {0}",
    load_fail_toast: "Load failed: {0}",
    confirm_reload: "Reloading will discard unsaved changes. Continue?",
    reloaded_toast: "Reloaded",
    init_load_fail_toast: "Initial load failed: {0}",
  },
};

let currentLang = localStorage.getItem("vediocut_lang") || "zh";

function t(key, ...args){
  const dict = I18N[currentLang] || I18N.zh;
  let s = dict[key];
  if(s == null) s = I18N.zh[key] ?? key;
  args.forEach((a, i) => { s = s.split("{" + i + "}").join(a); });
  return s;
}

function applyI18n(){
  document.querySelectorAll("[data-i18n]").forEach(el => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
    el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
  });
  document.title = t("page_title");
  const langBtn = $("#langToggle");
  if(langBtn) langBtn.textContent = currentLang === "zh" ? "EN" : "中";
  // verticalHint 已由 renderProjectInfo() 填入實際專案名稱時，切換語言不應退回泛用佔位文字
  const hint = $("#verticalHint");
  if(hint && currentProject) hint.textContent = t("vertical_hint", currentProject);
}

function setLang(lang){
  currentLang = lang;
  localStorage.setItem("vediocut_lang", lang);
  applyI18n();
}

applyI18n();

function toast(msg, isErr){
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  setTimeout(() => t.className = "", 2600);
}
function markDirty(){ dirty = true; $("#dirty").textContent = t("dirty_mark"); }
function clearDirty(){ dirty = false; $("#dirty").textContent = ""; }

async function api(path, opts){
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if(!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
  return data;
}

// ---------- 專案 ----------
async function loadProjects(){
  const r = await api("/api/projects");
  projectsMeta = {};
  (r.projects || []).forEach(p => { projectsMeta[p.id] = p; });
  const sel = $("#projectSel");
  sel.innerHTML = (r.projects || [])
    .map(p => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join("");
  // 保留目前選取（若仍存在）；否則用伺服器預設，最後退回清單第一個
  const ids = Object.keys(projectsMeta);
  if(!(currentProject && projectsMeta[currentProject])){
    currentProject = (r.default && projectsMeta[r.default]) ? r.default : ids[0];
  }
  if(currentProject) sel.value = currentProject;
  renderProjectInfo();
}

function renderProjectInfo(){
  const p = projectsMeta[currentProject];
  $("#projectInfo").textContent = p ? `#${p.id} · ${p.notes || t("no_notes")}` : t("no_project");
  const hint = $("#verticalHint");
  if(hint && currentProject){
    hint.textContent = t("vertical_hint", currentProject);
  }
}

// ---------- 載入 ----------
async function load(){
  edl = await api("/api/edl?project=" + encodeURIComponent(currentProject));
  edl.aroll = edl.aroll || [];
  edl.broll = edl.broll || [];
  renderMeta(); renderAroll(); renderBroll();
  clearDirty();
}

function renderMeta(){
  $("#hookCount").value = edl.hookCount ?? 0;
  $("#music").value = (edl.music && edl.music.file) || "";
  const tgt = edl.target || {};
  $("#targetDur").value = (tgt.durationSec == null) ? "" : tgt.durationSec;
  $("#metaInfo").textContent =
    (tgt.durationSec != null
      ? t("meta_target_with_dur", tgt.durationSec) + (tgt.tolerance ? " ("+tgt.tolerance[0]+"–"+tgt.tolerance[1]+"s)" : "")
      : t("meta_target_unlimited")) +
    " · " + (edl.method || "");
  renderCues();
}

// ---------- 音樂 cues（唯讀）----------
function renderCues(){
  const wrap = $("#cuesWrap");
  const cues = (edl.music && Array.isArray(edl.music.cues)) ? edl.music.cues : [];
  if(!cues.length){ wrap.innerHTML = ""; return; }
  const rows = cues.map(c => `
    <tr>
      <td class="asset">${esc(c.file || "")}</td>
      <td>${esc(c.fromChapter || "")} → ${esc(c.toChapter || "")}</td>
      <td>${esc(c.note || "")}</td>
    </tr>`).join("");
  wrap.innerHTML = `
    <table class="cues">
      <thead><tr><th>${t("cues_th_file")}</th><th>${t("cues_th_chapter")}</th><th>${t("cues_th_note")}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------- A-roll ----------
function arollIds(){ return edl.aroll.map(a => a.id); }

function renderAroll(){
  $("#arollCount").textContent = t("segment_count", edl.aroll.length);
  const box = $("#aroll"); box.innerHTML = "";
  edl.aroll.forEach((a, i) => {
    const locked = !!a.locked;
    const dur = fmt((a.sourceOut ?? 0) - (a.sourceIn ?? 0));
    const c = document.createElement("div");
    c.className = "card" + (locked ? " locked" : "");
    c.innerHTML = `
      <div class="row1">
        <span class="badge chap">${esc(a.chapter || "")}</span>
        <span class="idtag">${esc(a.id || "")}</span>
        <span class="asset">${esc(a.assetId || "")}</span>
        <span class="txt">${esc(a.text || "")}</span>
        ${locked ? `<span class="lockbadge">${t("locked_badge")}</span>` : ''}
      </div>
      <div class="fields">
        <label class="f">sourceIn<input type="number" step="0.01" class="in" value="${a.sourceIn ?? 0}" ${locked?"disabled":""}></label>
        <label class="f">sourceOut<input type="number" step="0.01" class="out" value="${a.sourceOut ?? 0}" ${locked?"disabled":""}></label>
        <span class="dur">${t("dur_label")} <b class="durv">${dur}</b> s</span>
        <span class="spacer"></span>
        <button class="icon up"    ${(i===0||locked)?"disabled":""} title="${t("move_up")}">▲</button>
        <button class="icon down"  ${(i===edl.aroll.length-1||locked)?"disabled":""} title="${t("move_down")}">▼</button>
        <button class="icon lock" title="${t("lock_toggle_title")}">${locked?t("unlock_btn"):t("lock_btn")}</button>
        <button class="icon danger del" ${locked?"disabled":""} title="${t("delete_btn")}">${t("delete_btn")}</button>
      </div>`;
    const inEl = c.querySelector(".in"), outEl = c.querySelector(".out"), durEl = c.querySelector(".durv");
    const recalc = () => { durEl.textContent = fmt((parseFloat(outEl.value)||0) - (parseFloat(inEl.value)||0)); };
    inEl.addEventListener("input", () => { a.sourceIn = parseFloat(inEl.value)||0; recalc(); markDirty(); });
    outEl.addEventListener("input", () => { a.sourceOut = parseFloat(outEl.value)||0; recalc(); markDirty(); });
    c.querySelector(".up").onclick   = () => { swap(i, i-1); };
    c.querySelector(".down").onclick = () => { swap(i, i+1); };
    c.querySelector(".lock").onclick = () => { a.locked = !a.locked; markDirty(); renderAroll(); };
    c.querySelector(".del").onclick  = () => {
      if(!confirm(t("confirm_delete_aroll", a.id, a.text||""))) return;
      edl.aroll.splice(i,1); markDirty(); renderAroll(); renderBroll();
    };
    box.appendChild(c);
  });
}
function swap(i, j){
  if(j<0 || j>=edl.aroll.length) return;
  const a = edl.aroll;
  [a[i], a[j]] = [a[j], a[i]];
  markDirty(); renderAroll();
}

// ---------- B-roll ----------
function renderBroll(){
  $("#brollCount").textContent = t("segment_count", edl.broll.length);
  const box = $("#broll"); box.innerHTML = "";
  const ids = arollIds();
  edl.broll.forEach((b, i) => {
    const dangling = !ids.includes(b.overAroll);
    const c = document.createElement("div");
    c.className = "card";
    const opts = ids.map(id => `<option value="${esc(id)}" ${id===b.overAroll?"selected":""}>${esc(id)}</option>`).join("");
    const extra = dangling ? `<option value="${esc(b.overAroll)}" selected>${esc(b.overAroll)}${t("not_exist_suffix")}</option>` : "";
    c.innerHTML = `
      <div class="row1">
        <span class="asset">${esc(b.assetId || "")}</span>
        <span class="txt">${esc(b.reason || "")}</span>
        <button class="icon danger del" title="${t("delete_btn")}">${t("delete_btn")}</button>
      </div>
      <div class="fields broll-grid">
        <label class="f" style="width:130px">${t("over_aroll_label")}<select class="over">${extra}${opts}</select></label>
        <label class="f">duration<input type="number" step="0.1" class="d" value="${b.duration ?? 0}" style="width:92px"></label>
        <label class="f">startInClip<input type="number" step="0.1" class="s" value="${b.startInClip ?? 0}" style="width:92px"></label>
        <label class="f">sourceIn<input type="number" step="0.1" class="si" value="${b.sourceIn ?? 0}" style="width:92px"></label>
      </div>
      ${dangling ? `<div class="warnrow">${t("broll_dangling_warn")}</div>` : ''}`;
    c.querySelector(".over").onchange = e => { b.overAroll = e.target.value; markDirty(); renderBroll(); };
    c.querySelector(".d").addEventListener("input", e => { b.duration = parseFloat(e.target.value)||0; markDirty(); });
    c.querySelector(".s").addEventListener("input", e => { b.startInClip = parseFloat(e.target.value)||0; markDirty(); });
    c.querySelector(".si").addEventListener("input", e => { b.sourceIn = parseFloat(e.target.value)||0; markDirty(); });
    c.querySelector(".del").onclick = () => {
      if(!confirm(t("confirm_delete_broll", b.assetId, b.overAroll))) return;
      edl.broll.splice(i,1); markDirty(); renderBroll();
    };
    box.appendChild(c);
  });
  if(edl.broll.length===0) box.innerHTML = `<p class="hint">${t("no_broll_hint")}</p>`;
}

// ---------- 儲存 ----------
async function save(){
  edl.hookCount = parseInt($("#hookCount").value, 10) || 0;
  // 目標時長（選填）：留空＝不限（以故事完整為主）；填值＝軟性目標，容忍區間自動 ±15%
  const tv = $("#targetDur").value.trim();
  edl.target = edl.target || {};
  if(tv === ""){
    delete edl.target.durationSec;
    delete edl.target.tolerance;
  }else{
    const n = parseFloat(tv);
    if(Number.isFinite(n) && n > 0){
      edl.target.durationSec = n;
      edl.target.tolerance = [Math.round(n * 0.85), Math.round(n * 1.15)];
    }
  }
  try{
    const payload = {...edl, project: currentProject};
    const r = await api("/api/edl", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)});
    clearDirty();
    toast(t("save_success", r.project, r.aroll, r.broll));
  }catch(e){ toast(t("save_fail", e.message), true); }
}

// ---------- 渲染 ----------
const LOAD_BUSY_THRESHOLD = 30;   // load1 超過此值視為系統忙碌

async function doRender(){
  if(dirty && !confirm(t("confirm_render_dirty"))) return;
  const body = {
    radio: $("#opt_radio").checked,
    no_broll: $("#opt_nobroll").checked,
    clear_cache: $("#opt_clear").checked,
    project: currentProject,
  };
  try{
    await api("/api/render", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)});
    toast(t("render_started", currentProject));
    startPolling();
  }catch(e){ toast(t("render_start_fail", e.message), true); }
}

async function doRenderVertical(){
  const body = { mode: $("#vMode").value, project: currentProject };
  try{
    await api("/api/render/vertical", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)});
    toast(t("vertical_render_started", currentProject));
    startPolling();
  }catch(e){ toast(t("vertical_render_start_fail", e.message), true); }
}

function paintStatus(s){
  const dot = $("#dot"), txt = $("#statusText");
  dot.className = "dot " + (s.status||"");
  const map = {idle:t("status_idle"), running:t("status_running"), done:t("status_done"), failed:t("status_failed")};
  let line = map[s.status] || s.status;
  if(s.kind==="vertical") line += t("status_vertical_suffix");
  if(s.project) line += " [" + s.project + "]" + (s.project !== currentProject ? t("status_other_project_suffix") : "");
  if(s.args){
    const flags = [];
    if(s.args.radio) flags.push("--radio");
    if(s.args.no_broll) flags.push("--no-broll");
    if(s.args.clear_cache) flags.push(t("flag_clear_cache")+(s.cleared!=null?("×"+s.cleared):""));
    if(s.args.mode) flags.push("mode="+s.args.mode);
    if(flags.length) line += "（" + flags.join(" ") + "）";
  }
  if(s.status==="done" && s.started && s.ended)
    line += t("status_elapsed", Math.round(s.ended - s.started));
  if(s.status==="failed" && s.returncode!=null) line += " · exit " + s.returncode;
  txt.textContent = line;
  const log = $("#log");
  if(s.log){ log.style.display="block"; log.textContent = s.log; log.scrollTop = log.scrollHeight; }

  // 系統負載
  const loadTag = $("#loadTag"), loadWarn = $("#loadWarn");
  if(Array.isArray(s.loadavg) && s.loadavg.length){
    const load1 = s.loadavg[0];
    const busy = load1 > LOAD_BUSY_THRESHOLD;
    loadTag.textContent = t("load_label", s.loadavg.map(n => n.toFixed(1)).join(" / "));
    loadTag.className = "loadtag" + (busy ? " busy" : "");
    loadWarn.className = "loadwarn" + (busy ? " show" : "");
  } else {
    loadTag.textContent = "";
    loadWarn.className = "loadwarn";
  }

  // 渲染失敗時醒目顯示錯誤（尤其「時長異常」守衛訊息）
  const fail = $("#failCallout");
  if(s.status==="failed" && s.log){
    const lines = s.log.split("\n").filter(l => l.trim());
    const durLines = lines.filter(l => l.includes("時長異常"));
    const tail = lines.slice(-6).join("\n");
    fail.textContent = durLines.length
      ? t("fail_duration_guard", durLines.join("\n"))
      : t("fail_generic", tail);
    fail.className = "fail-callout show";
  } else {
    fail.className = "fail-callout";
  }
}

async function pollOnce(){
  if(!currentProject) return;
  try{
    const s = await api("/api/render/status?project=" + encodeURIComponent(currentProject));
    paintStatus(s);
    if(s.status!=="running"){ stopPolling();
      if(s.status==="done") toast(s.kind==="vertical" ? t("vertical_render_done_toast") : t("render_done_toast"));
      if(s.status==="failed") toast(t("render_failed_toast"), true);
    }
  }catch(e){ /* 忽略暫時性錯誤 */ }
}
function startPolling(){ stopPolling(); pollOnce(); polling = setInterval(pollOnce, 1500); }
function stopPolling(){ if(polling){ clearInterval(polling); polling=null; } }

// ---------- 預覽 ----------
function doPreview(){
  const v = $("#video");
  v.style.display = "block";
  // project 帶上避免預覽跑錯專案；t 破快取，抓最新成片
  v.src = "/api/preview?file=final&project=" + encodeURIComponent(currentProject) + "&t=" + Date.now();
  v.load();
  v.scrollIntoView({behavior:"smooth", block:"nearest"});
}
function doPreviewVertical(){
  const v = $("#videoVertical");
  v.style.display = "block";
  v.src = "/api/preview?file=vertical&project=" + encodeURIComponent(currentProject) + "&t=" + Date.now();
  v.load();
  v.scrollIntoView({behavior:"smooth", block:"nearest"});
}

// ---------- 檢查 EDL ----------
async function doCheckEdl(){
  const out = $("#validateOut");
  out.style.display = "block";
  out.className = "validate";
  out.textContent = t("checking");
  try{
    const r = await api("/api/validate", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({project: currentProject})});
    const text = [r.stdout, r.stderr].filter(Boolean).join("\n").trim() || t("no_output");
    out.textContent = text;
    out.className = "validate " + (r.ok ? "ok" : "err");
    toast(r.ok ? t("check_pass_toast") : t("check_fail_toast"), !r.ok);
  }catch(e){
    out.textContent = t("check_fail_msg", e.message);
    out.className = "validate err";
    toast(t("check_fail_msg", e.message), true);
  }
}

// ---------- 事件 ----------
$("#projectSel").onchange = async (e) => {
  const next = e.target.value;
  if(next === currentProject) return;
  if(dirty && !confirm(t("confirm_switch_project"))){
    e.target.value = currentProject;   // 使用者取消 → 還原下拉選單
    return;
  }
  currentProject = next;
  renderProjectInfo();
  stopPolling();
  $("#video").style.display = "none";
  $("#videoVertical").style.display = "none";
  $("#log").style.display = "none";
  $("#validateOut").style.display = "none";
  $("#failCallout").className = "fail-callout";
  try{ await load(); toast(t("switched_project_toast", currentProject)); }
  catch(err){ toast(t("load_fail_toast", err.message), true); }
  pollOnce();
};
$("#reload").onclick = async () => {
  if(dirty && !confirm(t("confirm_reload"))) return;
  try{ await load(); toast(t("reloaded_toast")); }catch(e){ toast(t("load_fail_toast", e.message), true); }
};
$("#save").onclick = save;
$("#render").onclick = doRender;
$("#preview").onclick = doPreview;
$("#checkEdl").onclick = doCheckEdl;
$("#renderVertical").onclick = doRenderVertical;
$("#previewVertical").onclick = doPreviewVertical;
$("#hookCount").addEventListener("input", markDirty);
$("#targetDur").addEventListener("input", markDirty);
$("#langToggle").onclick = () => setLang(currentLang === "zh" ? "en" : "zh");
window.addEventListener("beforeunload", e => { if(dirty){ e.preventDefault(); e.returnValue=""; } });

function esc(s){ return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

// ---------- 啟動 ----------
(async function start(){
  try{
    await loadProjects();
    await load();
  }catch(e){ toast(t("init_load_fail_toast", e.message), true); }
  pollOnce();
})();
