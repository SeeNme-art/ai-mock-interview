"use strict";

const byId = id => document.getElementById(id);
const {page, session, source} = document.body.dataset;
const base = "/sessions/" + encodeURIComponent(session);
const keys = new Map();
const fileKeys = new WeakMap();
const directions = {agent: "Agent 工程", rag: "RAG 检索", backend: "后端开发"};
const types = {knowledge: "知识题", project: "项目题"};
const outcomes = {skipped: "已跳过", generation_failed: "题目不可用", unanswered: "未回答", answered: "已回答"};
const dimensions = {correctness: "正确性", depth: "深度", structure: "结构", relevance: "相关性",
  technical_reasoning: "技术推理", implementation_ownership: "个人贡献", validation_evidence: "验证证据", communication: "表达"};

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function notice(text, error = false) {
  const view = byId("notice");
  view.hidden = !text;
  view.textContent = text;
  view.classList.toggle("error", error);
}

async function api(path, method = "GET", payload) {
  const options = {method};
  if (payload instanceof File) {
    if (!fileKeys.has(payload)) fileKeys.set(payload, crypto.randomUUID());
    options.headers = {"Content-Type": "application/octet-stream", "Idempotency-Key": fileKeys.get(payload)};
    options.body = payload;
  } else if (method !== "GET") {
    const body = JSON.stringify(payload ?? {});
    const identity = method + path + body;
    if (!keys.has(identity)) keys.set(identity, crypto.randomUUID());
    options.headers = {"Content-Type": "application/json", "Idempotency-Key": keys.get(identity)};
    options.body = body;
  }
  const response = await fetch("/api/v1" + path, options);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok || !data?.ok) {
    const error = new Error(data?.error?.message ?? "请求暂时失败，请刷新后查看已保存进度。");
    error.code = data?.error?.code;
    throw error;
  }
  return data.data;
}

function showError(error) {
  notice(error.code === "STALE_REVISION" ? "该页面进度已过期，请刷新后再继续。" : error.message, true);
}

function handle(control, handler) {
  return async event => {
    event?.preventDefault();
    if (control.disabled) return;
    control.disabled = true;
    notice("");
    try { await handler(event); } catch (error) { showError(error); }
    finally { control.disabled = false; }
  };
}

function link(url, text, className = "button secondary") {
  const result = node("a", text, className);
  result.href = url;
  return result;
}

function view(name, id = session) {
  return "/" + name + "?s=" + encodeURIComponent(id);
}

async function newPage() {
  const form = byId("preview-form");
  const confirmation = byId("confirm-form");
  let draft;
  let inputVersion = 0;
  const showSource = () => {
    const mode = form.elements.source.value;
    if (confirmation.elements.cloud_assistance) {
      confirmation.elements.cloud_assistance.checked = false;
      byId("cloud-assistance").hidden = mode === "example";
    }
    byId("paste-label").hidden = mode !== "text";
    byId("paste-help").hidden = byId("paste-label").hidden;
    byId("file-fields").hidden = mode !== "file";
  };
  form.addEventListener("input", () => {
    inputVersion += 1;
    draft = undefined;
    showSource();
    confirmation.hidden = true;
    byId("document-text").value = "";
  });
  showSource();
  form.addEventListener("submit", handle(form.querySelector("button"), async () => {
    const mode = form.elements.source.value;
    const version = inputVersion;
    confirmation.hidden = true;
    draft = undefined;
    let preview;
    if (mode === "file") {
      const file = form.elements.file.files[0];
      const format = file?.name.split(".").pop().toLowerCase();
      if (!file || !["pdf", "docx"].includes(format)) throw new Error("请选择 PDF 或 DOCX 文件。");
      if (file.size > 5 * 1024 * 1024) throw new Error("文件最大 5 MiB，请缩小文件或粘贴文本。");
      preview = await api("/drafts/document?format=" + format, "POST", file);
    } else {
      preview = await api("/drafts/preview", "POST", {source: mode, text: mode === "text" ? form.elements.text.value : ""});
    }
    if (version !== inputVersion) return;
    draft = preview;
    const material = draft.material;
    for (const key of ["jd_text", "name", "action", "result"]) {
      confirmation.elements[key].value = material[key] ?? material.projects[0]?.[key] ?? "";
    }
    confirmation.elements.skills.value = material.skills.join("、");
    confirmation.elements.mode.value = material.projects.length ? "mixed" : "knowledge_only";
    const documentPreview = byId("document-preview");
    documentPreview.hidden = !draft.source_text;
    documentPreview.open = Boolean(draft.source_text);
    byId("document-text").value = draft.source_text ?? "";
    const missing = [];
    if (!material.jd_text) missing.push("岗位描述");
    if (!material.skills.length) missing.push("技能");
    if (!material.projects.length) missing.push("项目做法");
    else if (!material.projects[0].result) missing.push("项目结果");
    const missingFields = byId("missing-fields");
    missingFields.hidden = !missing.length;
    missingFields.textContent = "可按需补充：" + missing.join("、") + "。";
    confirmation.hidden = false;
    confirmation.elements.jd_text.focus();
  }));
  confirmation.addEventListener("submit", handle(confirmation.querySelector("button"), async () => {
    if (!draft) return;
    const field = name => confirmation.elements[name].value.trim();
    const project = field("action") ? [{action: field("action"), name: field("name") || null,
      result: field("result") || null}] : [];
    const material = {jd_text: field("jd_text") || null, skills: field("skills").split(/[,，、]/).map(item => item.trim()).filter(Boolean),
      projects: project};
    const result = await api("/sessions", "POST", {draft_id: draft.draft_id, material, mode: field("mode"),
      cloud_assistance: confirmation.elements.cloud_assistance?.checked ?? false});
    location.assign(view("planning", result.session_id));
  }));
}

async function planningPage() {
  const form = byId("plan-form");
  const start = byId("start");
  let state = await api(base + "/state");
  form.elements.difficulty.value = state.settings.difficulty;
  form.elements.question_count.value = state.settings.question_count;
  form.querySelectorAll("[name=direction]").forEach(input => { input.checked = state.settings.directions.includes(input.value); });
  function render(plan) {
    state = {...state, revision: plan.revision, plan_version: plan.plan_version};
    byId("plan-empty").hidden = true;
    byId("plan-items").replaceChildren(...plan.items.map(item =>
      node("li", [types[item.task_type], directions[item.dimension], item.difficulty === "basic" ? "基础" : "应用"].join(" · "))));
    const hints = [...plan.hints];
    if (plan.retrieval) hints.push(plan.retrieval.method === "faiss" ? "已使用本地向量检索排序。" : "向量检索不可用，已改用审核题库顺序。");
    byId("plan-hints").replaceChildren(...hints.map(text => node("p", text, "muted")));
    start.disabled = false;
  }
  if (state.plan_version) {
    render(await api(base + "/plan"));
  }
  form.addEventListener("change", () => { start.disabled = true; });
  form.addEventListener("submit", handle(form.querySelector("button"), async () => {
    const fields = new FormData(form);
    start.disabled = true;
    const choices = {directions: fields.getAll("direction"), difficulty: fields.get("difficulty"),
      question_count: Number(fields.get("question_count"))};
    render(await api(base + "/config", "PATCH", {choices, vector: fields.has("vector"), expected_revision: state.revision}));
  }));
  start.addEventListener("click", handle(start, async () => {
    await api(base + "/start", "POST", {expected_revision: state.revision});
    location.assign(view("interview"));
  }));
}

async function interviewPage() {
  const form = byId("answer-form");
  let state;
  function render(result) {
    if (state && result.revision < state.revision) return;
    state = result;
    if (state.phase === "FINISHED") { location.replace(view("report")); return; }
    const turn = state.current;
    const working = state.resume;
    const waiting = turn?.state === "awaiting_answer";
    byId("question-meta").textContent = turn ? `第 ${turn.seq} 题 · ${types[turn.task_type]}${turn.round_no ? " · 追问 " + turn.round_no : ""}` : "";
    byId("question").textContent = turn?.state === "processing" ? "正在处理已保存的回答" :
      turn?.question ?? (working ? "正在准备题目" : "当前题已处理");
    byId("progress").max = state.progress.planned || 5;
    byId("progress").value = state.progress.closed;
    byId("progress-text").textContent = `已处理 ${state.progress.closed} / ${state.progress.planned} 题`;
    form.hidden = !waiting;
    const saved = byId("saved-answer");
    saved.hidden = !turn?.answer;
    saved.textContent = turn?.answer ? "已保存的回答\n" + turn.answer : "";
    byId("continue").hidden = !working;
    byId("next").hidden = Boolean(turn || working);
    byId("skip").hidden = !turn;
    byId("abort").disabled = false;
  }
  async function refresh() { render(await api(base + "/state")); }
  async function settle() {
    await refresh();
    for (let count = 0; count < 60 && state.phase === "ACTIVE" && state.resume; count++) {
      await new Promise(resolve => setTimeout(resolve, 500));
      await refresh();
    }
  }
  form.addEventListener("submit", handle(form.querySelector("button[type=submit]"), async () => {
    const turn = state.current;
    await api(`/turns/${turn.turn_id}/answer`, "POST", {exchange_id: turn.exchange_id, expected_revision: state.revision,
      answer: form.elements.answer.value});
    form.elements.answer.value = "";
    notice("回答已保存，正在处理。");
    await settle();
  }));
  byId("skip").addEventListener("click", handle(byId("skip"), async () => {
    if (!confirm("跳过整道题？已提交的回答会保留。")) return;
    await api(`/turns/${state.current.turn_id}/skip`, "POST", {expected_revision: state.revision});
    form.elements.answer.value = "";
    await settle();
  }));
  byId("continue").addEventListener("click", handle(byId("continue"), async () => {
    await api(base + "/resume", "POST", {operation_id: state.operation.operation_id, expected_revision: state.revision});
    await settle();
  }));
  byId("next").addEventListener("click", handle(byId("next"), async () => {
    await api(base + "/advance", "POST", {expected_revision: state.revision});
    await settle();
  }));
  byId("abort").addEventListener("click", handle(byId("abort"), async () => {
    if (!confirm("结束本场面试？已提交的回答会保留。")) return;
    await api(base + "/abort", "POST", {expected_revision: state.revision, reason: "user_cancel"});
    await settle();
  }));
  await settle();
}

async function reportPage() {
  const generate = byId("generate-report");
  async function render() {
    const report = await api(base + "/report");
    const pending = report.status === "pending";
    generate.hidden = !pending;
    const intro = pending ? "报告待生成。可继续完成收尾。" :
      `${report.source === "example" ? "示例固定评分。 " : report.cloud_assistance ? "实验性 DeepSeek 点评。 " : ""}已答 ${report.counts.answered} 题，` +
      `有效评分 ${report.counts.scored} 题${report.counts.skipped ? "，跳过 " + report.counts.skipped + " 题" : ""}。`;
    byId("report-intro").textContent = intro;
    if (pending) return true;
    const exportLink = byId("export");
    exportLink.href = "/api/v1/reports/" + encodeURIComponent(report.id) + "/export";
    exportLink.hidden = false;
    byId("report-summary").replaceChildren(...[
      ["知识题", report.knowledge_score, report.category_counts.knowledge],
      ["项目题", report.project_score, report.category_counts.project],
    ].map(([title, score, count]) => {
      const card = node("article", undefined, "card stack");
      card.append(node("h2", title), node("p", score === null ? "暂无有效评分" : Number(score).toFixed(1), "score"),
        node("p", "有效评分 " + count + " 题", "muted"));
      return card;
    }));
    byId("report-items").replaceChildren(...report.items.map(item => {
      const section = node("details", undefined, "card");
      section.append(node("summary", `第 ${item.seq} 题 · ${types[item.task_type]} · ${outcomes[item.outcome] ?? "待回顾"}`));
      const body = node("div", undefined, "stack");
      for (const exchange of item.exchanges) {
        body.append(node("h3", exchange.question), node("p", exchange.answer ?? "未提交回答", "answer-body"));
      }
      const evaluation = item.evaluation;
      if (evaluation?.status === "failed") {
        const failure = evaluation.model_context?.validation_failure;
        const detail = failure ? `（${failure.field}，${failure.message}）` : "";
        body.append(node("p", `本题未产生有效评分${detail}。`, "muted"));
      }
      for (const [name, result] of Object.entries(evaluation?.assessment?.dimensions ?? {})) {
        if (!result.applicable) continue;
        body.append(node("h3", `${dimensions[name] ?? name} · ${result.value}/4`), node("p", result.reason));
        for (const evidence of result.evidence) body.append(node("p", "原文依据：" + evidence.quote, "answer-body"));
      }
      for (const source of item.sources ?? []) body.append(node("p", "审核出处：" + (source.title ?? source.id), "muted"));
      for (const tip of report.improvements.filter(value => value.seq === item.seq)) {
        body.append(node("p", "练习建议：" + tip.action), node("p", "自查：" + tip.self_check, "muted"));
      }
      section.append(body);
      return section;
    }));
    return false;
  }
  generate.addEventListener("click", handle(generate, async () => {
    const state = await api(base + "/state");
    if (state.resume) {
      await api(base + "/resume", "POST", {expected_revision: state.revision, operation_id: state.resume.operation_id});
      await new Promise(resolve => setTimeout(resolve, 500));
    } else {
      await api(base + "/report/generate", "POST");
    }
    await render();
  }));
  for (let count = 0; count < 10 && await render(); count++) {
    await new Promise(resolve => setTimeout(resolve, 250));
  }
}

async function waitDeletion(jobId) {
  if (!jobId) return;
  for (let count = 0; count < 12; count++) {
    const job = await api(`/deletions/${jobId}`);
    if (job.status === "completed") {
      notice("已删除本机应用保存的会话。");
      return;
    }
    if (job.status === "failed") {
      notice("清理暂未完成，可以从下方重试。", true);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  notice("会话正在清理，可稍后查看任务状态。");
}

async function historyPage(cursor = "") {
  const rows = await api("/sessions" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
  const list = byId("history-list");
  if (!cursor && !rows.sessions.length && !rows.deletions.length) {
    const empty = node("article", undefined, "card stack");
    empty.append(node("h2", "还没有练习记录"), node("p", "可以先用内置示例走一遍完整面试。"));
    list.replaceChildren(empty);
    return;
  }
  if (!cursor) list.replaceChildren();
  if (!cursor) for (const job of rows.deletions) {
    const card = node("article", undefined, "card history-row");
    const button = node("button", "继续清理", "secondary");
    button.addEventListener("click", handle(button, async () => {
      const result = await api(`/deletions/${job.id}/retry`, "POST");
      await waitDeletion(result?.job_id);
      await historyPage();
    }));
    card.append(node("p", job.status === "failed" ? "有一场会话的清理待重试。" : "有一场会话正在清理。"), button);
    list.append(card);
  }
  list.append(...rows.sessions.map(row => {
    const card = node("article", undefined, "card history-row");
    const title = node("div", undefined, "stack");
    const stage = {PLANNING: "待开始", ACTIVE: "进行中", FINISHED: "已结束"}[row.phase];
    const mode = row.mode === "mixed" ? "混合面试" : "知识专项";
    const date = new Date(row.created_at).toLocaleString("zh-CN");
    title.append(node("h2", mode), node("p", `${stage} · ${date} · ${row.progress.closed}/${row.question_count} 题`, "muted"));
    const scores = [row.scores?.knowledge_score == null ? null : "知识 " + row.scores.knowledge_score,
      row.scores?.project_score == null ? null : "项目 " + row.scores.project_score].filter(Boolean);
    if (scores.length) title.append(node("p", scores.join(" · "), "muted"));
    const actions = node("div", undefined, "actions");
    const route = {PLANNING: "planning", ACTIVE: "interview", FINISHED: "report"}[row.phase];
    actions.append(link(view(route, row.id), row.phase === "FINISHED" ? "查看报告" : "继续练习"));
    if (row.phase === "FINISHED") {
      const clone = node("button", "再练一次", "secondary");
      clone.addEventListener("click", handle(clone, async () => {
        const result = await api(`/sessions/${row.id}/clone`, "POST");
        location.assign(view("planning", result.session_id));
      }));
      const remove = node("button", "删除", "secondary");
      remove.addEventListener("click", handle(remove, async () => {
        if (!confirm("删除本机已保存的材料、回答、报告和可能包含它的应用备份？")) return;
        const result = await api(`/sessions/${row.id}`, "DELETE", {expected_revision: row.revision});
        await waitDeletion(result?.job_id);
        await historyPage();
      }));
      actions.append(clone, remove);
    }
    card.append(title, actions);
    return card;
  }));
  if (rows.next_cursor) {
    const more = node("button", "加载更早的记录", "secondary");
    more.addEventListener("click", handle(more, async () => {
      await historyPage(rows.next_cursor);
      more.remove();
    }));
    list.append(more);
  }
}

const pages = {history: historyPage, new: newPage, planning: planningPage, interview: interviewPage, report: reportPage};
pages[page]().catch(showError);
