"""Rule-based sponsorship classifier (first-pass, cheap filter).

Returns (verdict, evidence) where verdict is one of:
  "does_not_sponsor" - posting rules out sponsorship, or requires citizenship / a clearance
  "sponsors"         - posting explicitly offers sponsorship
  "unclear"          - no signal either way (hand off to the H-1B filing history check)

Order matters, and a "no" always beats a "yes":
  1. explicit "no" phrases (citizenship, clearance, "without sponsorship"...)
  2. any mention of sponsorship / H-1B that is negated in its own sentence
     ("do not currently offer H-1B sponsorship", "cannot, at this time, offer sponsorship",
      "sponsorship is not available"). A negation that is only about green cards is ignored,
      because many employers sponsor H-1B but not permanent residency.
  3. explicit "yes" phrases
"""
import bisect
import re

# Plain "we do not sponsor ..." / "sponsorship is not available" wording is handled by the
# context rule below, not here, so that "we don't sponsor green cards" can be told apart.
NEGATIVE = [
    r"no h-?1b",
    r"h-?1b (is |are )?not (available|supported|sponsored|offered)",
    r"h-?1b (candidates|applicants|holders|workers|visas?) (will not|won't|cannot|can't|are not|aren't|do not|don't)\b",
    r"must be (a )?(u\.?s\.?|united states) citizen",
    r"u\.?s\.? citizens? only",
    r"only (u\.?s\.?|united states) citizens",
    r"(u\.?s\.?|united states) citizenship (is )?(required|needed|mandatory)",
    r"requires? (u\.?s\.?|united states) citizenship",
    r"(requires?|must (be|hold)|must have) (a )?(u\.?s\.?|united states) (citizen|citizenship|person)",
    r"(green card|permanent resident)s? (holders? )?(only|required)",
    r"(active )?(secret|top secret|ts/sci) (security )?clearance",
    r"must be authorized to work (in the (u\.?s\.?|united states) )?without",
    r"authorized to work in the (u\.?s\.?|united states) (on a permanent basis|without)",
    r"\bitar\b",   # whole word: a bare "itar" also matches inside "military"
]

POSITIVE = [
    r"(visa|h-?1b|employment|work) sponsorship (is |are )?(available|offered|provided|supported)",
    r"visa sponsorship\b[^.;]{0,40}\b(available|offered|provided)",
    r"\bsponsorship (is |are )?(available|offered|provided)",
    r"\b(offer|offers|offering|provide|provides|providing|support|supports|supporting|extend|extends)\b "
    r"(h-?1b |visa |employment |immigration |work )*sponsorship",
    r"sponsorship (for|of) (h-?1b|work|employment)( visas?)?",
    r"\b(will|can|may|able to|willing to|happy to|open to)\b( also)? (provide |offer )?sponsor(ship)?( visas?)?",
    r"\bwe (do |also )?sponsor",
    r"visa (support|assistance)",
    r"h-?1b (transfer|sponsorship)",
    r"relocation and visa",
]

_neg = [re.compile(p, re.I) for p in NEGATIVE]
_pos = [re.compile(p, re.I) for p in POSITIVE]

_SENT_END = re.compile(r"[.;!?\n]")
_TOPIC = re.compile(r"\bsponsorship\b|\bsponsor(?:s|ing)?\b|\bh-?1-?b\b", re.I)   # not "sponsored"
_CONTRAST = re.compile(r"\b(but|however|although|though|yet|while)\b", re.I)
_NEG_BEFORE = re.compile(
    r"\b(no|not|never|cannot|can't|can not|won't|don't|doesn't|didn't|isn't|aren't|unable|without|"
    r"neither|nor|unavailable|ineligible)\b|n't\b", re.I)
_NEG_AFTER = re.compile(
    r"^\s*(?:(?:is|are|was|will|would|can|may|does|do|being)\s+)?(?:be\s+)?(?:not|never|unavailable|no longer)\b", re.I)
_GREEN = re.compile(r"green card|permanent resid|lawful permanent|immigrant visa|\bperm\b", re.I)
_H1B_OR_VISA = re.compile(r"h-?1-?b|\bvisas?\b|work permit|employment authori", re.I)


def _snippet(text, start, end, pad=80):
    s, e = max(0, start - pad), min(len(text), end + pad)
    return " ".join(text[s:e].split())


def _negated_mention(text: str):
    """First sponsorship / H-1B mention that is negated within its own sentence, else None."""
    ends = [0] + [m.end() for m in _SENT_END.finditer(text)] + [len(text)]
    for m in _TOPIC.finditer(text):
        i = bisect.bisect_right(ends, m.start())
        s_start, s_end = ends[i - 1], ends[min(i, len(ends) - 1)]
        before = text[max(s_start, m.start() - 60): m.start()]
        for c in _CONTRAST.finditer(before):      # "no relocation, BUT visa sponsorship is available"
            before = before[c.end():]
        after = text[m.end(): min(s_end, m.end() + 25)]
        if not (_NEG_BEFORE.search(before) or _NEG_AFTER.match(after)):
            continue
        # "We do not sponsor green cards": about permanent residency, not about H-1B
        obj = re.split(r"[,;]|\b(?:but|however|although|though)\b", text[m.end(): min(s_end, m.end() + 40)])[0]
        scope = before + " " + obj
        if _GREEN.search(scope) and not _H1B_OR_VISA.search(scope + " " + m.group(0)):
            continue
        return _snippet(text, m.start(), m.end())
    return None


def classify(text: str):
    text = text or ""
    for rx in _neg:
        m = rx.search(text)
        if m:
            return "does_not_sponsor", _snippet(text, m.start(), m.end())
    hit = _negated_mention(text)
    if hit:
        return "does_not_sponsor", hit
    for rx in _pos:
        m = rx.search(text)
        if m:
            return "sponsors", _snippet(text, m.start(), m.end())
    return "unclear", ""


_VISA_WORDS = re.compile(r"visa|sponsor|work authori[sz]ation|authori[sz]ed to work|immigration|h-?1b|eligib\w+ to work|right to work", re.I)


def context_sentence(description: str):
    """First sentence of the posting that touches visas / work authorization, or None."""
    for s in re.split(r"(?<=[.!?])\s+|\n+", description or ""):
        if _VISA_WORDS.search(s) and 15 < len(s.strip()) < 400:
            return s.strip()
    return None
