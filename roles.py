"""Which postings count as YOUR roles, at YOUR level. Rules live in config.yaml.

Role A, AI/ML software engineer: the title needs an engineering word (engineer, developer...)
AND an AI/ML word (AI, ML, machine learning, LLM...). Research, scientist and data-analytics
titles do not count, and neither do other disciplines (hardware, sales, QA...).
Role B, Forward Deployed Engineer: "forward deployed" / "FDE" in the title.

Level: titles with Staff / Principal / Director / Intern / Junior etc. are dropped, then the
years of experience the posting asks for must fall inside the band set in config.yaml
(`experience`). A posting that states no years is judged on its title alone.
"""
import re

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
YEARS_RX = re.compile(
    _NUM + r"\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*" + _NUM + r"\s*\+?)?\s*(?:years?|yrs?)\b", re.I)
SOFT_WORDS = ("prefer", "nice to have", "bonus", "ideally", "a plus")
LEVEL_ONE_RX = re.compile(r"\b(engineer|developer)\s+(i|1)\b")   # "Software Engineer I"


def has_any(words, text: str) -> bool:
    """Whole-word match, so 'ai' does not fire inside 'retail' and 'ml' not inside 'html'."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", text) for w in words)


def title_ok(title: str, cfg: dict) -> bool:
    t = title.lower()
    r = cfg["roles"]
    if has_any(r["exclude_title"], t) or LEVEL_ONE_RX.search(t):
        return False
    if has_any(r["forward_deployed"], t):
        return has_any(r.get("fde_engineer_words") or ["engineer"], t)
    if r.get("only") == "forward_deployed":
        return False
    return has_any(r["engineer_words"], t) and has_any(r["ai_words"], t)


def _num(s: str) -> int:
    return NUMBER_WORDS.get(s.lower()) if not s.isdigit() else int(s)


def required_years(text: str):
    """Years of experience the posting asks for: the largest minimum it states, or None.

    '3-5 years' counts as 3, '5+ years' as 5. Only mentions near the word 'experience' count,
    and ones marked preferred / nice-to-have / bonus are ignored.
    """
    best = None
    for m in YEARS_RX.finditer(text or ""):
        before = text[max(0, m.start() - 80): m.start()].lower()
        after = text[m.end(): m.end() + 80].lower()
        if "experience" not in before + after:
            continue
        if any(w in re.split(r"[.;\n]", before)[-1] for w in SOFT_WORDS):   # same sentence only
            continue
        n = _num(m.group(1))
        if n is not None and (best is None or n > best):
            best = n
    return best


def level_check(description: str, cfg: dict):
    """(fits, years_asked_or_None)."""
    exp = cfg.get("experience", {})
    lo, hi = exp.get("min_required_ok", 3), exp.get("max_required_ok", 6)
    n = required_years(description)
    if n is None:
        return True, None
    return lo <= n <= hi, n
