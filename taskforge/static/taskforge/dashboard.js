"use strict";
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("taskforge-token");
let runPage = 1;
let selectedRun = null;
let refreshing = false;
let noticeTimer;

function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 7000);
}
async function api(path, method = "GET", body, headers = {}) {
  const response = await fetch(path, {method, headers: {"Content-Type": "application/json", ...(token ? {Authorization: `Bearer ${token}`} : {}), ...headers}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    if (response.status === 401 && token) signOut();
    throw new Error(data.detail + (data.errors ? ` ${JSON.stringify(data.errors)}` : ""));
  }
  return data;
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function action(text, handler) {
  const button = node("button", text, "quiet");
  button.addEventListener("click", () => safely(handler, button));
  return button;
}
async function safely(handler, button) {
  if (button) button.disabled = true;
  try { await handler(); } catch (error) { notify(error.message, true); }
  finally { if (button) button.disabled = false; }
}
function signOut() {
  token = null; selectedRun = null;
  sessionStorage.removeItem("taskforge-token");
  $("dashboard").hidden = true; $("auth").hidden = false; $("logout").hidden = true;
  $("identity").textContent = "Your task control room";
}
async function signIn(register = false) {
  const form = $("login-form");
  if (!form.reportValidity()) return;
  const values = Object.fromEntries(new FormData(form));
  if (register) await api("/api/auth/register", "POST", values);
  const result = await api("/api/auth/login", "POST", values);
  token = result.access_token; sessionStorage.setItem("taskforge-token", token);
  form.reset(); await enter();
}
async function enter() {
  const me = await api("/api/auth/me");
  $("identity").textContent = me.email;
  $("auth").hidden = true; $("dashboard").hidden = false; $("logout").hidden = false;
  await refresh();
}
async function allPages(path) {
  let rows = [], page = 1;
  while (true) {
    const data = await api(`${path}?page=${page}`);
    rows.push(...data.results);
    if (!data.next) return rows;
    page++;
  }
}
function emptyRow(body, columns, text) {
  const row = node("tr"), cell = node("td", text, "empty"); cell.colSpan = columns; row.append(cell); body.append(row);
}
async function refresh() {
  if (refreshing || !token) return;
  refreshing = true;
  try {
    const [projects, jobs, runs, queued, running, retrying, succeeded] = await Promise.all([
      allPages("/api/projects"), allPages("/api/jobs"), api(`/api/runs?page=${runPage}&state=${encodeURIComponent($("run-filter").value)}`),
      ...["QUEUED", "RUNNING", "RETRYING", "SUCCEEDED"].map(state => api(`/api/runs?state=${state}`))
    ]);
    $("project-count").textContent = projects.length; $("job-count").textContent = jobs.length;
    $("active-count").textContent = queued.count + running.count + retrying.count; $("success-count").textContent = succeeded.count;
    const previousProject = $("project-select").value;
    $("project-list").replaceChildren(); $("project-select").replaceChildren();
    projects.forEach(project => {
      const card = node("div", undefined, "project-card"); card.append(node("span", "▤"), document.createTextNode(project.name), node("small", `Created ${new Date(project.created_at).toLocaleDateString()}`));
      $("project-list").append(card);
      const option = node("option", project.name); option.value = project.id; $("project-select").append(option);
    });
    if (projects.some(p => p.id === previousProject)) $("project-select").value = previousProject;
    if (!projects.length) $("project-list").append(node("p", "Create your first project to get started.", "muted"));
    $("job-list").replaceChildren();
    const names = new Map(jobs.map(job => [job.id, job.name]));
    jobs.forEach(job => {
      const row = node("tr"); row.append(node("td", job.name), node("td", job.task_type), node("td", `${job.timeout_seconds}s`));
      const cell = node("td"); cell.append(action("Run job ↗", async () => {
        const run = await api(`/api/jobs/${job.id}/submit`, "POST", {}, {"Idempotency-Key": crypto.randomUUID()});
        notify(`Run ${run.id.slice(0, 8)} accepted`); await refresh();
      })); row.append(cell); $("job-list").append(row);
    });
    if (!jobs.length) emptyRow($("job-list"), 4, "No jobs yet. Define your first task above.");
    $("run-list").replaceChildren();
    runs.results.forEach(run => {
      const row = node("tr"), name = node("td", names.get(run.job) || "Job"); name.append(node("small", run.id.slice(0, 8)));
      const state = node("td"); state.append(node("span", run.state, `badge ${run.state}`));
      row.append(name, state, node("td", run.attempt_count), node("td", new Date(run.created_at).toLocaleString()));
      const cell = node("td"); cell.append(action("View logs", async () => { selectedRun = run.id; $("log-panel").hidden = false; $("log-run").textContent = run.id; await refreshLogs(); $("log-panel").scrollIntoView({behavior: "smooth"}); }));
      if (run.state === "DEAD") cell.append(action("Requeue", async () => { await api(`/api/runs/${run.id}/requeue`, "POST", {}); notify("Run requeued"); await refresh(); }));
      row.append(cell); $("run-list").append(row);
    });
    if (!runs.results.length) emptyRow($("run-list"), 5, "No executions here yet. Put a job to work.");
    $("previous").disabled = !runs.previous; $("next").disabled = !runs.next; $("page-label").textContent = `Page ${runPage}`;
    if (selectedRun) await refreshLogs();
  } finally { refreshing = false; }
}
async function refreshLogs() {
  if (!selectedRun) return;
  let cursor = 0, lines = [];
  while (true) {
    const logs = await api(`/api/runs/${selectedRun}/logs?after=${cursor}&limit=500`);
    lines.push(...logs.results.map(log => `${log.timestamp}  ${log.event}  ${log.message}`));
    cursor = logs.next_cursor; if (!logs.has_more) break;
  }
  $("log-output").textContent = lines.join("\n") || "Waiting for output…";
}
$("login-form").addEventListener("submit", event => { event.preventDefault(); safely(() => signIn(), event.submitter); });
$("register").addEventListener("click", () => safely(() => signIn(true), $("register")));
$("logout").addEventListener("click", signOut);
$("refresh").addEventListener("click", () => safely(refresh));
$("close-logs").addEventListener("click", () => { selectedRun = null; $("log-panel").hidden = true; });
$("run-filter").addEventListener("change", () => { runPage = 1; safely(refresh); });
$("previous").addEventListener("click", () => { runPage--; safely(refresh); });
$("next").addEventListener("click", () => { runPage++; safely(refresh); });
$("project-form").addEventListener("submit", event => { event.preventDefault(); safely(async () => { await api("/api/projects", "POST", Object.fromEntries(new FormData(event.target))); event.target.reset(); await refresh(); }, event.submitter); });
$("job-form").addEventListener("submit", event => { event.preventDefault(); safely(async () => { const data = Object.fromEntries(new FormData(event.target)); await api(`/api/projects/${data.project}/jobs`, "POST", {name: data.name, task_type: data.task_type, payload_template: JSON.parse(data.payload)}); notify("Job created"); await refresh(); }, event.submitter); });
$("job-form").elements.task_type.addEventListener("change", event => {
  const examples = {python_demo: {value: "Hello, TaskForge"}, sleep: {seconds: 2}, http_call: {url: "https://httpbin.org/get"}};
  $("job-form").elements.payload.value = JSON.stringify(examples[event.target.value]);
});
fetch("/healthz").then(r => r.json()).then(data => { $("health").textContent = data.status === "ok" ? "Systems operational" : "Dependency unavailable"; }).catch(() => { $("health").textContent = "Service unavailable"; });
if (token) safely(enter);
setInterval(() => { if (token && !document.hidden) safely(refresh); }, 4000);
