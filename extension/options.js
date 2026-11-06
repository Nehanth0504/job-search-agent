const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["url", "pw"], (s) => {
  $("url").value = s.url || "https://jobagent.your-tailnet.ts.net";
  $("pw").value = s.pw || "";
});
$("save").onclick = async () => {
  const url = $("url").value.trim().replace(/\/+$/, ""), pw = $("pw").value;
  await chrome.storage.local.set({ url, pw });
  const msg = $("msg");
  msg.textContent = "Testing…"; msg.className = "";
  try {
    const r = await fetch(url + "/api/jobs/recent", { headers: { "X-JobAgent": "1", Authorization: "Basic " + btoa("me:" + pw) } });
    if (r.status === 401) throw new Error("wrong password");
    if (!r.ok) throw new Error("dashboard answered " + r.status);
    const d = await r.json();
    msg.textContent = `Connected. ${d.jobs.length} recent jobs found.`; msg.className = "ok";
  } catch (e) {
    msg.textContent = "Could not connect: " + e.message + ". Is Tailscale on, and is the address right?"; msg.className = "bad";
  }
};
