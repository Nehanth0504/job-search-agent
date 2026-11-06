"""The browser extension's form filler, run in a real (headless) Chromium on a sample application form."""
import base64
import json
import os

import pytest

FORM = """<html><body><form id="f">
 <div><label for="fn">First Name *</label><input id="fn" required></div>
 <div><label for="ln">Last Name *</label><input id="ln" required></div>
 <div><label for="em">Email *</label><input id="em" type="email" required></div>
 <div><label for="ph">Phone</label><input id="ph"></div>
 <div><label for="cv">Resume/CV *</label><input id="cv" type="file" required></div>
 <div><label for="q1">Are you legally authorized to work in the United States? *</label>
   <select id="q1" required><option value="">--</option><option value="1">Yes</option><option value="0">No</option></select></div>
 <div><label for="q2">Will you require visa sponsorship? *</label>
   <select id="q2" required><option value="">--</option><option value="y">Yes</option><option value="n">No</option></select></div>
 <fieldset><legend>Are you open to relocation?</legend>
   <label><input type="radio" name="r" value="y">Yes</label><label><input type="radio" name="r" value="n">No</label></fieldset>
 <div><label for="g">Gender</label><select id="g"><option value="">--</option><option>Female</option></select></div>
 <div><label for="c">Something custom *</label><textarea id="c" required></textarea></div>
 <button type="submit">Submit</button>
</form><script>window.sent=false;document.getElementById('f').onsubmit=e=>{e.preventDefault();window.sent=true}</script>
</body></html>"""


def test_filler_fills_known_fields_and_never_submits(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    data = dict(
        profile=dict(first_name="Alex", last_name="Rivera", full_name="Alex Rivera",
                     email="me@example.com", phone="555-123-4567", location="Dallas, TX", city="Dallas", state="TX"),
        standard=[["Are you legally authorized to work in the United States?", "Yes"],
                  ["Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)?", "Yes"],
                  ["Are you willing to relocate?", "Yes, anywhere in the US."]],
        job_answers=[], cover_letter_text="",
        resume=dict(name="r.docx", b64=base64.b64encode(b"PK fake docx").decode()), cover=None)
    page_file = tmp_path / "form.html"
    page_file.write_text(FORM)
    filler = open(os.path.join(os.path.dirname(__file__), "extension", "filler.js")).read()
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.goto(page_file.as_uri())
        page.add_script_tag(content=filler)
        res = page.evaluate("d => window.__jobAgentFill(d)", data)
        v = page.evaluate("""() => ({fn: fn.value, ln: ln.value, em: em.value, q1: q1.value, q2: q2.value,
            r: (document.querySelector('input[name=r]:checked') || {}).value, g: g.value,
            cv: cv.files.length ? cv.files[0].name : '', sent: window.sent})""")
        b.close()
    assert v == dict(fn="Alex", ln="Rivera", em="me@example.com", q1="1", q2="y", r="y", g="", cv="r.docx",
                     sent=False)
    assert res["files"] == 1 and "Something custom *" in res["attention"]
