"""Pick the employer's own posting over third-party job sites, and label links for the dashboard."""
import re
from urllib.parse import quote_plus, urlparse

ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com", "workable.com",
             "recruitee.com", "bamboohr.com", "breezy.hr", "pinpointhq.com", "rippling.com", "icims.com",
             "oraclecloud.com", "taleo.net", "successfactors.com", "jobvite.com", "eightfold.ai", "dayforcehcm.com",
             "ultipro.com", "paylocity.com", "adp.com", "applytojob.com", "teamtailor.com", "personio.de")
AGGREGATORS = ("linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com", "monster.com", "simplyhired.com",
               "talent.com", "jooble.org", "adzuna.com", "lensa.com", "builtin.com", "dice.com", "careerbuilder.com",
               "snagajob.com", "jobrapido.com", "learn4good.com", "salary.com", "theladders.com", "wellfound.com",
               "jobleads.com", "joblist.com", "tallo.com", "recruit.net", "bebee.com", "teal.com", "himalayas.app",
               "remoterocketship.com", "workingnomads.com", "weworkremotely.com", "ycombinator.com", "jobgether.com",
               "careerjet.com", "getwork.com", "jobs.com", "whatjobs.com", "jobilize.com", "flexjobs.com", "google.com")


def host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _ends(h, names):
    return any(h == n or h.endswith("." + n) for n in names)


def is_aggregator(url: str) -> bool:
    return _ends(host(url), AGGREGATORS)


def kind(url: str, company: str = "", employer_site: str = "") -> str:
    """'company' (employer's site or its job system), 'jobsite' (third party) or 'other'."""
    h = host(url)
    if not h:
        return "other"
    if _ends(h, ATS_HOSTS):
        return "company"
    eh = host(employer_site)
    if eh and (h == eh or h.endswith("." + eh.removeprefix("www.")) or eh.endswith("." + h)):
        return "company"
    words = [w for w in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(w) > 2 and w not in
             ("inc", "llc", "corp", "the", "and", "group", "company", "technologies", "digital", "services")]
    if words and any(w in h for w in words) and not is_aggregator(url):
        return "company"
    return "jobsite" if is_aggregator(url) else "other"


def best(links, company: str = "", employer_site: str = "", direct=()) -> str:
    """The link to open: the employer's own posting first, a third-party job site last."""
    links = [l for l in links if l and l.startswith(("http://", "https://"))]
    if not links:
        return ""
    rank = {"company": 0, "other": 1, "jobsite": 2}
    return sorted(links, key=lambda l: (rank[kind(l, company, employer_site)], 0 if l in direct else 1,
                                        links.index(l)))[0]


def careers_search(company: str, title: str) -> str:
    """A web search that usually finds the posting on the company's own careers page."""
    return "https://www.google.com/search?q=" + quote_plus(f'"{company}" careers "{title}"')


_RECRUITER_NAME = re.compile(r"\b(jobs?|careers?|recruit\w*|staffing|talent\w*|hiring|headhunt\w*|placements?|"
                             r"resourcing|personnel|times|remote|search partners|workforce solutions)\b", re.I)
_RECRUITER_TEXT = re.compile(r"\b(our client|on behalf of (a|our) client|for a client of ours|our partner company|"
                             r"recruitment agency|staffing agency)\b", re.I)


def is_recruiter(company: str, description: str = "") -> bool:
    """A recruiter, staffing agency or job site posting for someone else (not the employer itself)."""
    return bool(_RECRUITER_NAME.search(company or "") or _RECRUITER_TEXT.search((description or "")[:6000]))


def open_url(url: str, company: str = "", title: str = "", employer_site: str = "") -> str:
    """Where 'Open posting' goes: the posting itself, or, for a job only seen on a job site,
    a search for it on the company's own careers site (never LinkedIn / Indeed)."""
    if kind(url, company, employer_site) == "jobsite":
        return careers_search(company, title)
    return url
