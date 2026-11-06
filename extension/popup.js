const $ = (id) => document.getElementById(id);
let cfg = {}, tab = null, data = null;

const api = (path, opts = {}) =>
  fetch(cfg.url + path, Object.assign({}, opts, {
    headers: Object.assign({ "X-JobAgent": "1", Authorization: "Basic " + btoa("me:" + (cfg.pw || "")) }, opts.headers || {}),
  })).then((r) => { if (!r.ok) throw new Error("dashboard answered " + r.status); return r.json(); });

function showJob(j) {
  $("jobbox").innerHTML = "";
  if (!j) return;
  const d = document.createElement("div");
  d.className = "job";
  d.innerHTML = "<b></b><span class='muted'></span>";
  d.querySelector("b").textContent = j.title;
  d.querySelector("span").textContent = j.company + (j.status === "applied" ? " · already marked applied" : "");
  $("jobbox").appendChild(d);
}

async function loadById(id) {
  $("status").textContent = "Getting your resume and answers…";
  data = await api("/api/autofill?id=" + encodeURIComponent(id));
  showJob(data.job);
  $("status").textContent = "Ready. Open the application form on this page, then fill it.";
  $("fill").disabled = false; $("done").disabled = false;
}

async function init() {
  cfg = await chrome.storage.local.get(["url", "pw"]);
  $("opts").onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
  if (!cfg.url || !cfg.pw) {
    $("status").innerHTML = "Set up the extension first: <a href='#' id='go'>open settings</a>.";
    $("go").onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
    return;
  }
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    data = await api("/api/autofill?url=" + encodeURIComponent(tab.url));
  } catch (e) {
    $("status").innerHTML = "<span class='bad'>Could not reach your dashboard (" + e.message + "). Is Tailscale on?</span>";
    return;
  }
  if (data.job) {
    showJob(data.job);
    $("status").textContent = "This page matches one of your jobs.";
    $("fill").disabled = false; $("done").disabled = false;
  } else {
    $("status").textContent = "Which job is this application for?";
  }
  const recent = await api("/api/jobs/recent");
  const pick = $("pick");
  pick.innerHTML = "<option value=''>Choose a job…</option>" + recent.jobs.map((j) =>
    `<option value="${j.id}">${(j.company + " – " + j.title).replace(/[<>&"]/g, "")}${j.status === "applied" ? " (applied)" : ""}</option>`).join("");
  if (data.job) pick.value = String(data.job.id);
  pick.style.display = "block";
  pick.onchange = () => pick.value && loadById(pick.value).catch((e) => ($("out").textContent = e.message));
}

$("fill").onclick = async () => {
  $("fill").disabled = true;
  $("out").textContent = "Filling…";
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["filler.js"] });
    const res = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: (d) => (window.__jobAgentFill ? window.__jobAgentFill(d) : null),
      args: [data],
    });
    let filled = 0, files = 0; const todo = [];
    for (const r of res) if (r && r.result) { filled += r.result.filled; files += r.result.files; todo.push(...r.result.attention); }
    const out = $("out");
    out.innerHTML = "";
    const p = document.createElement("div");
    p.className = filled || files ? "ok" : "warn";
    p.textContent = filled || files
      ? `Filled ${filled} field${filled === 1 ? "" : "s"} and attached ${files} file${files === 1 ? "" : "s"} (green).`
      : "No form fields found. Open the application form (click Apply on the page) and try again.";
    out.appendChild(p);
    if (todo.length) {
      const w = document.createElement("div");
      w.className = "warn";
      w.textContent = "Needs you (yellow): " + [...new Set(todo)].slice(0, 12).join(" · ");
      out.appendChild(w);
    }
  } catch (e) {
    $("out").innerHTML = "<span class='bad'></span>";
    $("out").firstChild.textContent = "Could not fill this page: " + e.message;
  }
  $("fill").disabled = false;
};

$("done").onclick = async () => {
  try {
    await api(`/api/jobs/${data.job.id}/applied`, { method: "POST" });
    $("out").innerHTML = "<span class='ok'>Marked as applied on your dashboard.</span>";
    $("done").disabled = true;
  } catch (e) {
    $("out").textContent = "Could not update the dashboard: " + e.message;
  }
};

init();
