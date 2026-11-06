// Job Agent form filler. Injected into the application page (every frame) when you click "Fill".
// It fills what it can recognise, outlines filled fields in green and fields that need you in yellow,
// and NEVER clicks Submit.
(() => {
  if (window.__jobAgentFill) return;

  const GREEN = "2px solid #22a06b", YELLOW = "2px solid #e0a100";
  const SKIP = /gender|sex\b|pronoun|race|ethnic|hispanic|latino|veteran|disabilit|sexual orientation|transgender|date of birth|\bage\b|social security|ssn|captcha/i;

  function textOf(el) {
    return (el && (el.innerText || el.textContent) || "").replace(/\s+/g, " ").trim();
  }

  function labelOf(el) {
    const parts = [];
    const lab = (id) => id && document.getElementById(id);
    if (el.getAttribute("aria-label")) parts.push(el.getAttribute("aria-label"));
    (el.getAttribute("aria-labelledby") || "").split(/\s+/).forEach((id) => { const n = lab(id); if (n) parts.push(textOf(n)); });
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) parts.push(textOf(l));
    }
    const wrap = el.closest("label");
    if (wrap) parts.push(textOf(wrap));
    const fs = el.closest("fieldset");
    if (fs && fs.querySelector("legend")) parts.push(textOf(fs.querySelector("legend")));
    if (!parts.length) {
      // nearest container that has a little text: typical "<div><div>Question</div><input></div>"
      let p = el.parentElement;
      for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
        const t = textOf(p);
        if (t && t.length < 300) { parts.push(t); break; }
      }
    }
    parts.push(el.getAttribute("placeholder") || "", el.getAttribute("name") || "", el.id || "",
               el.getAttribute("autocomplete") || "", el.getAttribute("data-automation-id") || "");
    return parts.join(" | ").replace(/\s+/g, " ").slice(0, 400);
  }

  function isRequired(el, label) {
    return el.required || el.getAttribute("aria-required") === "true" || /\*/.test(label.split("|")[0] || "");
  }

  function visible(el) {
    if (el.type === "file") return true;                 // file inputs are often hidden behind a button
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  }

  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    el.focus();
    setter.call(el, value);                               // works with React / Vue controlled inputs
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
  }

  function yesNo(answer) {
    const a = (answer || "").trim().toLowerCase();
    if (a.startsWith("yes")) return "yes";
    if (a.startsWith("no")) return "no";
    return null;
  }

  // Build the rules from the dashboard data. Each: [regex on the field's label, value or function]
  function rules(d) {
    const p = d.profile || {};
    const std = Object.fromEntries(d.standard || []);
    const find = (re) => { const k = Object.keys(std).find((q) => re.test(q)); return k ? std[k] : ""; };
    const ja = d.job_answers || [];
    const years = (find(/years of/i).match(/\d+/) || [""])[0];
    return [
      [/first\s*name|given\s*name|forename|preferred\s*name/i, p.first_name],
      [/last\s*name|surname|family\s*name/i, p.last_name],
      [/^(\s*\|\s*)*(full\s*|legal\s*|your\s*)?name\b(?!.*(company|employer|school|reference|manager))/i, p.full_name],
      [/e-?mail/i, p.email],
      [/phone|mobile|cell/i, p.phone],
      [/linkedin/i, p.linkedin],
      [/github/i, p.github || p.website],
      [/portfolio|personal\s*(web)?site|website|blog/i, p.website || p.github || p.linkedin],
      [/\bcity\b/i, p.city],
      [/\b(state|province|region)\b/i, p.state],
      [/\bcountry\b/i, p.country],
      [/\blocation\b|where are you (located|based)|\baddress\b/i, p.location],
      [/current\s*(company|employer)|company\s*name|most recent (company|employer)|employer/i, p.current_company],
      [/current\s*(job\s*)?title|position\s*title|current\s*role/i, p.current_title],
      [/cover\s*letter/i, d.cover_letter_text],
      [/years? of (professional |relevant |work )?experience|how many years/i, years],
      [/authori[sz]ed to work|legally (eligible|authori[sz]ed|able)|right to work|eligible to work/i, find(/authori[sz]ed to work/i)],
      [/sponsor|visa|immigration/i, find(/sponsorship/i)],
      [/relocat/i, find(/relocat/i)],
      [/travel/i, find(/travel/i)],
      [/start date|available to start|availability|notice period|earliest/i, find(/start date/i)],
      [/salary|compensation|pay (range|expectation)|desired pay/i, find(/salary/i)],
      [/hear about|how did you (find|learn|hear)|source of (application|referral)|where did you (find|see)/i, find(/hear about/i)],
      [/why .*(work|join|interested|apply)|why (us|this company)|what (excites|interests|draws) you|motivat/i, (ja[0] || [])[1]],
      [/why .*(fit|qualif|good candidate)|what makes you|why should we/i, (ja[1] || [])[1]],
      [/project|accomplishment|achievement|proud of|tell us about (a|something)/i, (ja[2] || [])[1]],
    ];
  }

  function valueFor(label, list) {
    for (const [re, v] of list) if (re.test(label)) return v || null;
    return null;
  }

  function fileFrom(b64, name) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
  }

  window.__jobAgentFill = (d) => {
    const list = rules(d);
    let filled = 0, files = 0;
    const attention = [];
    const mark = (el, ok) => { try { el.style.outline = ok ? GREEN : YELLOW; el.style.outlineOffset = "2px"; } catch (e) { /* */ } };

    // 1. files: resume and cover letter
    const fileInputs = [...document.querySelectorAll("input[type=file]")];
    fileInputs.forEach((el, i) => {
      if (el.files && el.files.length) return;
      const label = labelOf(el).toLowerCase();
      let f = null;
      if (/cover/.test(label)) f = d.cover;
      else if (/resume|cv|curriculum/.test(label) || fileInputs.length === 1 || i === 0) f = d.resume;
      if (!f || !f.b64) { attention.push(labelOf(el).split("|")[0].trim() || "file upload"); return; }
      try {
        const dt = new DataTransfer();
        dt.items.add(fileFrom(f.b64, f.name));
        el.files = dt.files;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        files++;
        mark(el.closest("div") || el, true);
      } catch (e) {
        attention.push("upload " + (/cover/.test(label) ? "cover letter" : "resume"));
      }
    });

    // 2. text fields and text areas
    const inputs = [...document.querySelectorAll("input, textarea")].filter((el) =>
      !["hidden", "file", "submit", "button", "checkbox", "radio", "image", "reset", "password"].includes(el.type) &&
      !el.disabled && !el.readOnly && visible(el));
    for (const el of inputs) {
      const label = labelOf(el);
      if (SKIP.test(label) || el.getAttribute("role") === "combobox") {
        if (isRequired(el, label)) { attention.push(label.split("|")[0].trim()); mark(el, false); }
        continue;
      }
      if (el.value && el.value.trim()) continue;          // never overwrite what is already there
      const v = valueFor(label, list);
      if (v) { setValue(el, String(v)); filled++; mark(el, true); }
      else if (isRequired(el, label)) { attention.push(label.split("|")[0].trim() || el.name); mark(el, false); }
    }

    // 3. drop-down lists
    for (const el of [...document.querySelectorAll("select")].filter((e) => !e.disabled && visible(e))) {
      const label = labelOf(el);
      if (SKIP.test(label)) continue;
      if (el.selectedIndex > 0 && el.value) continue;
      const v = valueFor(label, list);
      const opts = [...el.options];
      let opt = null;
      if (v) {
        const yn = yesNo(v), lv = String(v).toLowerCase();
        opt = opts.find((o) => o.value && yn && o.text.trim().toLowerCase().startsWith(yn)) ||
              opts.find((o) => o.value && o.text.trim().toLowerCase() === lv) ||
              opts.find((o) => o.value && lv.length > 2 && o.text.toLowerCase().includes(lv)) ||
              opts.find((o) => o.value && lv.length > 2 && lv.includes(o.text.trim().toLowerCase()) && o.text.trim().length > 2);
      }
      if (opt) {
        el.value = opt.value;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        filled++; mark(el, true);
      } else if (isRequired(el, label)) { attention.push(label.split("|")[0].trim()); mark(el, false); }
    }

    // 4. yes / no radio buttons (grouped by name)
    const groups = {};
    for (const r of document.querySelectorAll("input[type=radio]")) (groups[r.name || r.id] = groups[r.name || r.id] || []).push(r);
    for (const radios of Object.values(groups)) {
      if (radios.some((r) => r.checked)) continue;
      const first = radios[0];
      const q = (first.closest("fieldset") && textOf(first.closest("fieldset").querySelector("legend"))) ||
                (first.getAttribute("aria-labelledby") && textOf(document.getElementById(first.getAttribute("aria-labelledby")))) ||
                textOf(first.closest("[role=radiogroup]") || first.parentElement.parentElement).slice(0, 300);
      if (SKIP.test(q)) continue;
      const yn = yesNo(valueFor(q, list));
      const target = yn && radios.find((r) => {
        const t = (textOf(r.closest("label")) || labelOf(r)).toLowerCase();
        return t.startsWith(yn) || new RegExp("\\b" + yn + "\\b").test(t);
      });
      if (target) { target.click(); filled++; mark(target.closest("label") || target, true); }
      else if (radios.some((r) => r.required)) { attention.push(q.slice(0, 80)); mark(first.closest("fieldset") || first, false); }
    }

    return { filled, files, attention: attention.filter(Boolean) };
  };
})();
