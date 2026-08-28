/* finesse · register=product · sub=ai-workbench · motion=feedback-only */
// 铸形 FormForge Studio — frontend logic.
// Submits the config form, streams SSE progress, loads the GLB into <model-viewer>.

const STAGES = [
  { prefix: "SPEC_PLANNER", label: "1. 需求解析" },
  { prefix: "ARCHITECT",    label: "2. 几何方案设计" },
  { prefix: "CODER",        label: "3. Python CAD 编程" },
  { prefix: "REPAIR",       label: "4. Aider 自动修复（备用）" },
];

const STAGE_NAMES = {
  planner: "需求解析",
  architect: "几何方案设计",
  coder: "CAD 代码生成",
  autonomous_skill_loop: "自动质检与修复",
};

const RUN_STAGE_ORDER = ["planner", "architect", "coder", "autonomous_skill_loop"];

const ERROR_NAMES = {
  none: "质量检查通过",
  dimension: "尺寸检查未通过",
  topology: "拓扑检查未通过",
  fatal: "无法自动修复",
};

const STATE_LABELS = {
  ready: "待命",
  working: "运行中",
  complete: "已完成",
  stopped: "已停止",
  error: "需处理",
};

const API_KEY_STORAGE_KEY = "formforge.api_key.v1";
const LEGACY_API_KEY_STORAGE_KEYS = ["zihan_cad.api_key.v1", "mac.api_key.v1"];

let providers = {};
let currentJobId = null;
let activeStage = null;
let runStartedAt = null;
let elapsedTimer = null;

function readSavedApiKey() {
  try {
    const current = window.localStorage.getItem(API_KEY_STORAGE_KEY);
    if (current) return current;
    for (const key of LEGACY_API_KEY_STORAGE_KEYS) {
      const legacy = window.localStorage.getItem(key);
      if (legacy) return legacy;
    }
    return "";
  } catch {
    return "";
  }
}

function setRunStatus(message, tone = "ready") {
  const status = document.getElementById("status");
  const dot = document.createElement("span");
  const text = document.createElement("span");
  dot.className = "status-pulse";
  dot.setAttribute("aria-hidden", "true");
  text.textContent = message;
  status.dataset.tone = tone;
  status.replaceChildren(dot, text);

  const chip = document.getElementById("run-state-chip");
  chip.dataset.state = tone;
  chip.textContent = STATE_LABELS[tone] || message;
}

function getRunStep(stage) {
  return document.querySelector(`#run-steps [data-stage="${stage}"]`);
}

function resetRunSteps() {
  activeStage = null;
  for (const item of document.querySelectorAll("#run-steps li")) {
    item.dataset.state = "queued";
    item.removeAttribute("aria-current");
    item.querySelector("time").textContent = "待命";
  }
}

function activateRunStage(stage, iter = 1) {
  const targetIndex = RUN_STAGE_ORDER.indexOf(stage);
  if (targetIndex < 0) return;

  activeStage = stage;
  RUN_STAGE_ORDER.forEach((stageName, index) => {
    const item = getRunStep(stageName);
    if (!item) return;
    item.removeAttribute("aria-current");
    if (index < targetIndex) {
      item.dataset.state = "complete";
      item.querySelector("time").textContent = "完成";
    } else if (index === targetIndex) {
      item.dataset.state = "active";
      item.setAttribute("aria-current", "step");
      item.querySelector("time").textContent = `第 ${iter || 1} 轮`;
    } else {
      item.dataset.state = "queued";
      item.querySelector("time").textContent = "待命";
    }
  });
}

function markActiveRunStage(state, label) {
  const stage = activeStage;
  const item = stage ? getRunStep(stage) : null;
  if (!item) return;
  item.dataset.state = state;
  item.removeAttribute("aria-current");
  item.querySelector("time").textContent = label;
}

function finishRunSteps(msg) {
  if (msg.cancelled) {
    markActiveRunStage("stopped", "已停止");
    return;
  }
  if (msg.error_type && msg.error_type !== "none") {
    if (!activeStage) activeStage = RUN_STAGE_ORDER.at(-1);
    markActiveRunStage("error", "未通过");
    return;
  }
  for (const stage of RUN_STAGE_ORDER) {
    const item = getRunStep(stage);
    if (!item) continue;
    item.dataset.state = "complete";
    item.removeAttribute("aria-current");
    item.querySelector("time").textContent = "完成";
  }
}

function formatElapsed(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function updateElapsed() {
  if (runStartedAt == null) return;
  document.getElementById("elapsed-time").textContent = formatElapsed(Date.now() - runStartedAt);
}

function stopRunMetrics() {
  if (elapsedTimer != null) window.clearInterval(elapsedTimer);
  elapsedTimer = null;
  updateElapsed();
}

function startRunMetrics() {
  stopRunMetrics();
  runStartedAt = Date.now();
  document.getElementById("elapsed-time").textContent = "0:00";
  document.getElementById("token-receipt").textContent = "—";
  document.getElementById("api-receipt").textContent = "—";
  elapsedTimer = window.setInterval(updateElapsed, 1000);
}

function updateRunMetrics(tokens, apiCalls) {
  if (tokens != null) {
    document.getElementById("token-receipt").textContent = Number(tokens).toLocaleString("zh-CN");
  }
  if (apiCalls != null) document.getElementById("api-receipt").textContent = `${apiCalls} 次`;
}

function resetArtifacts() {
  document.getElementById("artifact-count").textContent = "0";
  document.getElementById("downloads").innerHTML = `
    <div class="empty-state">
      <span class="empty-state-icon" aria-hidden="true">↳</span>
      <p><strong>还没有工程文件</strong><span>完成建模后可在这里下载。</span></p>
    </div>`;
  document.getElementById("stats").textContent = "";
}

function clearModelPreview() {
  const mv = document.getElementById("mv");
  mv.removeAttribute("src");
  document.getElementById("viewer-wrap").classList.remove("has-model");
}

function showModelPreview(url) {
  const mv = document.getElementById("mv");
  mv.setAttribute("src", url);
  document.getElementById("viewer-wrap").classList.add("has-model");
}

function updateApiKeyStatus(saved, message = "") {
  const status = document.getElementById("api-key-status");
  status.classList.toggle("saved", saved);
  status.classList.toggle("missing", !saved);
  status.textContent = message || (saved ? "✓ 已保存" : "未保存");
}

function saveApiKey(apiKey, { collapse = true } = {}) {
  const normalized = apiKey.trim();
  if (!normalized) {
    updateApiKeyStatus(false, "请先输入密钥");
    const details = document.getElementById("api-settings");
    details.open = true;
    document.getElementById("api_key").focus();
    return false;
  }

  try {
    window.localStorage.setItem(API_KEY_STORAGE_KEY, normalized);
  } catch {
    updateApiKeyStatus(false, "浏览器禁止保存");
    return false;
  }

  document.getElementById("api_key").value = normalized;
  updateApiKeyStatus(true);
  if (collapse) document.getElementById("api-settings").open = false;
  return true;
}

function restoreApiKey() {
  const saved = readSavedApiKey();
  if (saved) {
    try {
      if (!window.localStorage.getItem(API_KEY_STORAGE_KEY)) {
        window.localStorage.setItem(API_KEY_STORAGE_KEY, saved);
      }
    } catch {
      // The already-restored key still works for this page load.
    }
    document.getElementById("api_key").value = saved;
    updateApiKeyStatus(true);
  } else {
    updateApiKeyStatus(false);
  }
  document.getElementById("api-settings").open = false;
}

async function loadSchema() {
  const r = await fetch("/api/config/schema");
  if (!r.ok) throw new Error(`配置请求失败（${r.status}）`);
  const d = await r.json();
  const cfg = d.config;
  providers = d.providers;

  document.getElementById("DS_BASE_URL").value = cfg.DS_BASE_URL;
  document.getElementById("prompt").value = cfg.USER_REQUEST;
  document.getElementById("workflow").value = cfg.WORKFLOW_ID || "original";
  document.getElementById("MAX_RETRIES").value = cfg.MAX_RETRIES;
  document.getElementById("MAX_EXEC_RETRIES").value = cfg.MAX_EXEC_RETRIES;
  document.getElementById("provider").value = "qwen";

  const tbody = document.querySelector("#stage-table tbody");
  tbody.innerHTML = "";
  for (const s of STAGES) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.label}</td>
      <td><input type="text" id="${s.prefix}_MODEL" value="${cfg[s.prefix + "_MODEL"] ?? ""}" /></td>
      <td><input type="number" step="0.1" id="${s.prefix}_TEMPERATURE" value="${cfg[s.prefix + "_TEMPERATURE"] ?? 0}" /></td>
      <td><input type="number" id="${s.prefix}_MAX_TOKENS" value="${cfg[s.prefix + "_MAX_TOKENS"] ?? 0}" /></td>`;
    tbody.appendChild(tr);
  }

  // Mark model inputs as customized once the user edits them (so provider
  // preset auto-fill doesn't clobber their choice).
  for (const s of STAGES) {
    const inp = document.getElementById(s.prefix + "_MODEL");
    inp.addEventListener("input", () => { inp.dataset.customized = "1"; });
  }

  restoreApiKey();
}

document.getElementById("prompt").addEventListener("input", () => {
  const prompt = document.getElementById("prompt");
  if (prompt.value.trim()) {
    prompt.removeAttribute("aria-invalid");
    document.getElementById("prompt-error").textContent = "";
  }
});

document.getElementById("save-api-key").addEventListener("click", () => {
  saveApiKey(document.getElementById("api_key").value);
});

document.getElementById("clear-api-key").addEventListener("click", () => {
  try {
    window.localStorage.removeItem(API_KEY_STORAGE_KEY);
    for (const key of LEGACY_API_KEY_STORAGE_KEYS) {
      window.localStorage.removeItem(key);
    }
  } catch {
    // The field can still be cleared even when browser storage is unavailable.
  }
  document.getElementById("api_key").value = "";
  updateApiKeyStatus(false, "已清除");
  document.getElementById("api_key").focus();
});

document.getElementById("api_key").addEventListener("input", () => {
  const current = document.getElementById("api_key").value.trim();
  const saved = readSavedApiKey();
  if (!current) {
    updateApiKeyStatus(false, saved ? "修改中" : "未保存");
  } else if (current === saved) {
    updateApiKeyStatus(true);
  } else {
    updateApiKeyStatus(false, "修改未保存");
  }
});

document.getElementById("provider").addEventListener("change", (e) => {
  const p = providers[e.target.value];
  if (!p) return;
  document.getElementById("DS_BASE_URL").value = p.ds_base_url;
  for (const s of STAGES) {
    const inp = document.getElementById(s.prefix + "_MODEL");
    if (inp && !inp.dataset.customized) inp.value = p.model_hint;
  }
});

document.getElementById("run-btn").addEventListener("click", async () => {
  const prompt = document.getElementById("prompt");
  if (!prompt.value.trim()) {
    prompt.setAttribute("aria-invalid", "true");
    document.getElementById("prompt-error").textContent = "请先填写零件描述，然后开始生成。";
    prompt.focus();
    return;
  }

  const apiKey = document.getElementById("api_key").value.trim() || readSavedApiKey();
  if (!apiKey) {
    updateApiKeyStatus(false, "请先输入密钥");
    document.getElementById("api-settings").open = true;
    document.getElementById("api_key").focus();
    return;
  }

  saveApiKey(apiKey);

  const config = {
    DS_BASE_URL: document.getElementById("DS_BASE_URL").value,
    MAX_RETRIES: parseInt(document.getElementById("MAX_RETRIES").value, 10),
    MAX_EXEC_RETRIES: parseInt(document.getElementById("MAX_EXEC_RETRIES").value, 10),
  };
  for (const s of STAGES) {
    config[s.prefix + "_MODEL"] = document.getElementById(s.prefix + "_MODEL").value;
    config[s.prefix + "_TEMPERATURE"] = parseFloat(document.getElementById(s.prefix + "_TEMPERATURE").value);
    config[s.prefix + "_MAX_TOKENS"] = parseInt(document.getElementById(s.prefix + "_MAX_TOKENS").value, 10);
  }

  const body = {
    config,
    prompt: prompt.value.trim(),
    api_key: apiKey,
    workflow: document.getElementById("workflow").value,
    dest_path: document.getElementById("dest_path").value,
  };

  const log = document.getElementById("log");
  const runBtn = document.getElementById("run-btn");
  const stopBtn = document.getElementById("stop-btn");
  log.textContent = "// 正在创建建模任务\n";
  resetArtifacts();
  resetRunSteps();
  startRunMetrics();
  clearModelPreview();
  setRunStatus("正在提交工程任务", "working");

  runBtn.disabled = true;
  runBtn.dataset.running = "true";
  runBtn.querySelector(".run-label").textContent = "生成中";
  stopBtn.disabled = true;
  stopBtn.textContent = "■ 停止";

  try {
    const r = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const message = await r.text();
      throw new Error(message || `HTTP ${r.status}`);
    }
    const { job_id: jobId } = await r.json();
    currentJobId = jobId;
    stopBtn.disabled = false;
    streamEvents(jobId);
  } catch (error) {
    stopRunMetrics();
    setRunStatus(`提交失败：${error.message || error}`, "error");
    markActiveRunStage("error", "提交失败");
    runBtn.disabled = false;
    runBtn.removeAttribute("data-running");
    runBtn.querySelector(".run-label").textContent = "开始生成";
  }
});

document.getElementById("stop-btn").addEventListener("click", async () => {
  const stopBtn = document.getElementById("stop-btn");
  if (!currentJobId) return;
  stopBtn.disabled = true;
  stopBtn.textContent = "正在停止…";
  setRunStatus("停止请求已发送，正在保留已有结果", "stopped");
  markActiveRunStage("stopped", "停止中");
  try {
    const response = await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (e) {
    setRunStatus(`停止任务失败：${e.message || e}`, "error");
    markActiveRunStage("error", "停止失败");
    stopBtn.disabled = false;
    stopBtn.textContent = "■ 停止";
  }
  // SSE will deliver the synthetic 'done' next; the onmessage handler
  // re-enables the run button and shows partial downloads.
});

function streamEvents(jobId) {
  currentJobId = jobId;
  const log = document.getElementById("log");
  const mv = document.getElementById("mv");
  const runBtn = document.getElementById("run-btn");
  const stopBtn = document.getElementById("stop-btn");
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  let finished = false;

  function finishStream() {
    if (finished) return;
    finished = true;
    stopRunMetrics();
    runBtn.disabled = false;
    runBtn.removeAttribute("data-running");
    runBtn.querySelector(".run-label").textContent = "开始生成";
    stopBtn.disabled = true;
    stopBtn.textContent = "■ 停止";
    currentJobId = null;
  }

  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.log) {
      log.textContent += msg.log + "\n";
      log.scrollTop = log.scrollHeight;
    }
    if (msg.stage) {
      const stageName = STAGE_NAMES[msg.stage] || msg.stage;
      activateRunStage(msg.stage, msg.iter);
      setRunStatus(`当前阶段：${stageName} · 第 ${msg.iter || 1} 轮`, "working");
    }
    if (msg.warn) {
      log.textContent += "⚠ " + msg.warn + "\n";
    }
    if (msg.intermediate) {
      // <model-viewer> doesn't reliably reload when only the query string
      // changes (server appends ?t=<ts> for cache-busting, but the element
      // may keep the old scene around and the new model renders on top of it,
      // looking like "another model" instead of replacing it). Force a clean
      // reload by clearing src, flushing layout, then setting the new URL.
      mv.removeAttribute("src");
      void mv.offsetWidth; // force reflow so model-viewer tears down the scene
      showModelPreview(msg.glb);
      setRunStatus("三维预览已更新，任务仍在继续", "working");
    }
    if (msg.done) {
      updateRunMetrics(msg.tokens, msg.api_calls);
      finishRunSteps(msg);
      if (msg.cancelled) {
        setRunStatus("任务已停止，已有结果仍可下载", "stopped");
      } else if (msg.error_type && msg.error_type !== "none") {
        const errorName = ERROR_NAMES[msg.error_type] || msg.error_type;
        setRunStatus(`生成结束 · ${errorName}`, "error");
      } else {
        setRunStatus("生成完成 · 质量检查通过", "complete");
      }
      es.close();
      finishStream();
      showResult(jobId, msg);
    }
    if (msg.error) {
      setRunStatus("运行失败：" + msg.error, "error");
      markActiveRunStage("error", "失败");
      log.textContent += "✗ " + msg.error + "\n";
      es.close();
      finishStream();
    }
  };

  es.onerror = () => {
    if (finished) return;
    setRunStatus("与任务服务的连接已断开", "error");
    markActiveRunStage("error", "连接中断");
    es.close();
    finishStream();
  };
}

function showResult(jobId, msg) {
  const mv = document.getElementById("mv");
  const dl = document.getElementById("downloads");
  dl.innerHTML = "";

  if (msg.glb) {
    showModelPreview(`/api/jobs/${jobId}/files/model.glb`);
  } else {
    mv.setAttribute("alt", "没有可预览的 GLB 文件，请尝试下载 STEP 或 STL");
  }

  // Only show download buttons for artifacts that actually exist on disk.
  const files = [
    ["GLB 三维预览", "model.glb", msg.glb],
    ["STEP 工程模型", "model.step", msg.step],
    ["STL 网格模型", "model.stl", msg.stl],
    ["Python 源码", "source.py", msg.py],
    ["尺寸测量结果", "measurements.json", msg.measurements],
    ["运行诊断结果", "missed.json", msg.missed],
  ];
  let artifactCount = 0;
  for (const [label, fname, path] of files) {
    if (!path) continue;
    const a = document.createElement("a");
    a.href = `/api/jobs/${jobId}/files/${fname}`;
    a.textContent = `↓ ${label}`;
    a.className = "dl-btn";
    dl.appendChild(a);
    artifactCount += 1;
  }

  document.getElementById("artifact-count").textContent = String(artifactCount);
  if (artifactCount === 0) resetArtifacts();

  const statsStr = [];
  if (msg.tokens != null) statsStr.push(`Token ${Number(msg.tokens).toLocaleString("zh-CN")}`);
  if (msg.api_calls != null) statsStr.push(`API 调用 ${msg.api_calls} 次`);
  document.getElementById("stats").textContent = statsStr.join(" · ");
}

resetRunSteps();
resetArtifacts();
loadSchema().catch((error) => {
  console.error("铸形初始化失败", error);
  setRunStatus("初始化失败，请确认本地服务已正常启动。", "error");
});
