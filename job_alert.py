"""
General Job Alert Script
Sources: LinkedIn (guest scrape), Adzuna API, USAJobs API, Handshake (link), Simplify, Indeed
Location: 50 miles of Baltimore, MD | Entry-level
Sends an HTML email digest with all results.
"""

import os
import smtplib
import json
import time
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode, urlparse, urlunparse
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
EMAIL_SENDER       = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD     = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENT    = os.getenv("EMAIL_RECIPIENT")
ADZUNA_APP_ID      = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY     = os.getenv("ADZUNA_APP_KEY")
USAJOBS_API_KEY    = os.getenv("USAJOBS_API_KEY")
USAJOBS_USER_AGENT = os.getenv("USAJOBS_USER_AGENT", "")
HANDSHAKE_EMAIL    = os.getenv("HANDSHAKE_EMAIL")
HANDSHAKE_PASSWORD = os.getenv("HANDSHAKE_PASSWORD")

LOCATION     = "Baltimore, MD"
RADIUS_MILES = 50

keywords_env = os.getenv("KEYWORDS", '["software engineer"]') 

try:
    KEYWORDS = json.loads(keywords_env)
except json.JSONDecodeError:
    log.error("Failed to parse KEYWORDS from environment.")
    KEYWORDS = ["software engineer"] # Fallback

log.info(f"Loaded keywords: {KEYWORDS}")


def _load_json_list(env_name: str, fallback: list[str]) -> list[str]:
    raw = os.getenv(env_name)
    if not raw:
        return fallback
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Failed to parse %s; using fallback.", env_name)
        return fallback
    if not isinstance(data, list):
        log.warning("%s must be a JSON list; using fallback.", env_name)
        return fallback
    return [str(item).strip() for item in data if str(item).strip()]


SEARCH_LOCATIONS = _load_json_list(
    "SEARCH_LOCATIONS",
    ["Baltimore, MD", "Columbia, MD", "Washington, DC", "Arlington, VA"],
)
GREENHOUSE_BOARDS = _load_json_list("GREENHOUSE_BOARDS", [])
LEVER_SITES = _load_json_list("LEVER_SITES", [])
INCLUDE_REMOTE = os.getenv("INCLUDE_REMOTE", "1").lower() not in {"0", "false", "no"}
MAX_SOURCE_PAGES = max(1, int(os.getenv("MAX_SOURCE_PAGES", "3")))
MAX_SOURCE_RESULTS = max(25, int(os.getenv("MAX_SOURCE_RESULTS", "75")))
REQUEST_RETRIES = max(1, int(os.getenv("REQUEST_RETRIES", "3")))
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "20")))


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ── Filters ──────────────────────────────────────────────────────────────────

_EXP_BLOCKLIST = [
    r"\b(?:[2-9]|\d{2,})\+?\s*(?:-?\d+)?\s*(?:years?|yrs?)\b",
    r"\b(?:minimum|min|at least)\s+(?:of\s+)?(?:[2-9]|\d{2,})\s*(?:years?|yrs?)\b",
    r"\b(?:[2-9]|\d{2,}|two|three|four|five|six|seven|eight|nine|ten)\s*(?:\+|plus)?\s*(?:or more\s+)?(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|professional\s+|related\s+)?experience\b",
    r"\b(?:requires?|required|seeking)\s+(?:a\s+minimum\s+of\s+)?(?:[2-9]|\d{2,}|two|three|four|five|six|seven|eight|nine|ten)\s*(?:\+|plus)?\s*(?:years?|yrs?)\b",
    r"\bexperience\s*[:\-]?\s*(?:[2-9]|\d{2,}|two|three|four|five|six|seven|eight|nine|ten)\s*(?:\+|plus)?\s*(?:years?|yrs?)\b",
    r"\bsenior\b", r"\bsr\.\b", r"\bmid[- ]?level\b", r"\bintermediate\b",
    r"\blead\b", r"\bprincipal\b", r"\bstaff\b", r"\barchitect\b",
    r"\bmanager\b", r"\bdirector\b", r"\bhead of\b",
    # Generalized Level indicators (Level II, Grade 3, etc.)
    r"\b(?:level|grade|tier|sde|swe|ds)\s*[2-9]\b",
    r"\b\w+\s+[II|III|IV|V|VI]+\b",
]
_EXP_RE = re.compile("|".join(_EXP_BLOCKLIST), re.IGNORECASE)
_ENTRY_PATTERNS = [
    r"\bentry[\s-]?level\b", r"\bjunior\b", r"\bjr\.?\b",
    r"\bnew[\s-]?grad\b", r"\brecent grad(?:uate)?\b", r"\bgraduate\b",
    r"\bearly career\b", r"\bapprentice\b", r"\b0[\s-]*[–-]?[\s-]*2 years\b",
    r"\b1[\s-]*[–-]?[\s-]*2 years\b", r"\b0-2 years\b", r"\b1-2 years\b",
]
_ENTRY_RE = re.compile("|".join(_ENTRY_PATTERNS), re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(remote|work from home|hybrid|distributed|anywhere)\b", re.IGNORECASE)
_LINKEDIN_TITLE_BLOCKLIST = re.compile(
    r"\b("
    r"sales associate|retail associate|campus retail|cashier|barista|"
    r"waiter|waitress|hostess?|customer service (?:representative|associate)|"
    r"field service technician|installer|mechanic|warehouse associate|"
    r"merchandis(?:er|ing)|brand ambassador|store manager|seasonal sales|"
    r"business development representative|recruiter"
    r")\b",
    re.IGNORECASE,
)
_CLEARANCE_LEVEL_RE = re.compile(
    r"\b("
    r"top secret|secret|ts/sci|ts sci|sci|public trust|"
    r"security clearance|clearance with poly|polygraph|full scope poly"
    r")\b",
    re.IGNORECASE,
)
_CURRENT_CLEARANCE_RE = re.compile(
    r"\b("
    r"active|current|existing|already hold|must hold|hold an active|"
    r"possess|must possess|required at time of hire|day one|"
    r"prior to start|eligible for access on day one"
    r")\b",
    re.IGNORECASE,
)
_OBTAINABLE_CLEARANCE_RE = re.compile(
    r"\b("
    r"able to obtain|ability to obtain|eligible to obtain|"
    r"can obtain|must obtain|obtain and maintain|"
    r"obtain a|obtain an|become eligible for|"
    r"willing to undergo"
    r")\b",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def tokenize(value: str) -> set[str]:
    return {token for token in normalize_text(value).split() if len(token) > 1}


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    clean = parsed._replace(query="", fragment="")
    return urlunparse(clean).rstrip("/")


def request_with_retry(url: str, *, params: dict | None = None, headers: dict | None = None) -> requests.Response:
    last_error = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = SESSION.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt == REQUEST_RETRIES:
                break
            time.sleep(1.5 * attempt)
    raise last_error


def html_to_text(value: str) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def build_job(
    *,
    title: str,
    company: str,
    location: str,
    url: str,
    source: str,
    posted: str,
    description: str = "",
) -> dict:
    return {
        "title": str(title or "").strip() or "N/A",
        "company": str(company or "").strip() or "N/A",
        "location": str(location or "").strip() or LOCATION,
        "url": str(url or "").strip(),
        "source": source,
        "posted": str(posted).strip() if posted else "Recent",
        "description": str(description or "").strip(),
        "canonical_url": canonicalize_url(url),
    }


def location_matches(job: dict) -> bool:
    location_text = " ".join(
        part for part in [job.get("location", ""), job.get("description", ""), job.get("title", "")] if part
    )
    normalized = normalize_text(location_text)
    if not normalized:
        return True
    if INCLUDE_REMOTE and _REMOTE_RE.search(location_text):
        return True
    for candidate in SEARCH_LOCATIONS:
        candidate_tokens = tokenize(candidate)
        if candidate_tokens and candidate_tokens.issubset(set(normalized.split())):
            return True
        if normalize_text(candidate) in normalized:
            return True
    return False

def is_entry_level(job: dict) -> bool:
    """Return False if the job title or description signals high experience."""
    title = job.get("title", "")
    description = job.get("description", "")
    text = f"{title} {description}"
    if _EXP_RE.search(text):
        return False
    lower_title = title.lower()
    if any(word in lower_title for word in ["senior", "lead", "staff", "principal", "sr."]):
        return False
    return True


def requires_current_clearance(job: dict) -> bool:
    text = " ".join(
        part for part in [job.get("title", ""), job.get("description", ""), job.get("location", "")] if part
    )
    normalized = normalize_text(text)
    if not normalized or not _CLEARANCE_LEVEL_RE.search(text):
        return False
    if _OBTAINABLE_CLEARANCE_RE.search(text):
        return False
    if _CURRENT_CLEARANCE_RE.search(text):
        return True
    return bool(re.search(r"\bmust have\b.*\b(clearance|ts|sci|secret|polygraph)\b", normalized))

def score_job(job: dict) -> int:
    title = normalize_text(job.get("title", ""))
    description = normalize_text(job.get("description", ""))
    combined = f"{title} {description}".strip()
    title_tokens = set(title.split())
    combined_tokens = set(combined.split())

    score = 0
    for kw in KEYWORDS:
        kw_norm = normalize_text(kw)
        kw_tokens = tokenize(kw)
        if kw_norm and kw_norm in title:
            score += 5
            continue
        if kw_norm and kw_norm in combined:
            score += 3
        overlap_title = len(kw_tokens & title_tokens)
        overlap_combined = len(kw_tokens & combined_tokens)
        if kw_tokens and overlap_title >= max(1, len(kw_tokens) - 1):
            score += 3
        elif overlap_combined:
            score += min(2, overlap_combined)

    title_text = job.get("title", "")
    desc_text = job.get("description", "")
    if _ENTRY_RE.search(title_text):
        score += 3
    elif _ENTRY_RE.search(desc_text):
        score += 1
    if location_matches(job):
        score += 1
    return score


def is_related(job: dict) -> bool:
    return (
        is_entry_level(job)
        and not requires_current_clearance(job)
        and score_job(job) >= 4
    )


def is_relevant_linkedin_job(job: dict) -> bool:
    title = job.get("title", "")
    description = job.get("description", "")
    if _LINKEDIN_TITLE_BLOCKLIST.search(f"{title} {description}"):
        return False
    return is_related(job)


def is_recent_graduate_usajobs_item(item: dict) -> bool:
    descriptor = item.get("MatchedObjectDescriptor", {})
    details = descriptor.get("UserArea", {}).get("Details", {})
    hiring_paths = details.get("HiringPath", [])

    for path in hiring_paths if isinstance(hiring_paths, list) else []:
        name = ""
        code = ""
        if isinstance(path, dict):
            name = str(path.get("Name", ""))
            code = str(path.get("Code", ""))
        else:
            name = str(path)
        normalized = normalize_text(f"{name} {code}")
        if "recent graduate" in normalized or "graduates" in normalized:
            return True

    text_fields = [
        descriptor.get("QualificationSummary", ""),
        details.get("JobSummary", ""),
        details.get("WhoMayApply", ""),
    ]
    return any("recent graduate" in normalize_text(value) for value in text_fields if value)

# ── Scrapers ──────────────────────────────────────────────────────────────────

def fetch_linkedin(keyword: str) -> list[dict]:
    jobs = []
    seen_urls = set()
    try:
        for start in range(0, MAX_SOURCE_RESULTS, 25):
            params = {
                "keywords": f"{keyword} entry level OR junior OR associate OR new grad",
                "location": LOCATION,
                "distance": RADIUS_MILES,
                "f_TPR": "r86400",
                "f_E": "1,2",
                "start": start,
            }
            response = request_with_retry(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params=params,
            )
            soup = BeautifulSoup(response.text, "html.parser")
            cards = soup.find_all("li")
            if not cards:
                break
            page_added = 0
            for card in cards:
                title_tag = card.find("h3", class_="base-search-card__title")
                company_tag = card.find("h4", class_="base-search-card__subtitle")
                loc_tag = card.find("span", class_="job-search-card__location")
                link_tag = card.find("a", class_="base-card__full-link")
                if title_tag and link_tag:
                    candidate = build_job(
                        title=title_tag.text,
                        company=company_tag.text if company_tag else "N/A",
                        location=loc_tag.text if loc_tag else LOCATION,
                        url=link_tag["href"],
                        source="LinkedIn",
                        posted="Last 24h",
                    )
                    url = candidate["url"]
                    if url in seen_urls:
                        continue
                    if not is_relevant_linkedin_job(candidate):
                        continue
                    seen_urls.add(url)
                    jobs.append(candidate)
                    page_added += 1
            if page_added == 0:
                break
    except Exception as e:
        log.warning(f"LinkedIn fetch failed for '{keyword}': {e}")
    return jobs

def fetch_adzuna(keyword: str) -> list[dict]:
    jobs = []
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return jobs
    try:
        for page in range(1, MAX_SOURCE_PAGES + 1):
            params = {
                "app_id": ADZUNA_APP_ID,
                "app_key": ADZUNA_APP_KEY,
                "results_per_page": "50",
                "what": f"{keyword} entry level OR junior OR associate OR new grad",
                "where": LOCATION,
                "distance": str(RADIUS_MILES),
                "max_days_old": "2",
                "sort_by": "date",
                "full_time": "1",
            }
            response = request_with_retry(f"https://api.adzuna.com/v1/api/jobs/us/search/{page}", params=params)
            data = response.json()
            results = data.get("results", [])
            if not results:
                break
            for job in results:
                jobs.append(build_job(
                    title=job.get("title", "N/A"),
                    company=job.get("company", {}).get("display_name", "N/A"),
                    location=job.get("location", {}).get("display_name", LOCATION),
                    url=job.get("redirect_url", "#"),
                    source="Adzuna",
                    posted=job.get("created", "Today")[:10],
                    description=job.get("description", ""),
                ))
    except Exception as e:
        log.warning(f"Adzuna fetch failed for '{keyword}': {e}")
    return jobs

def fetch_usajobs():
    """USAJobs runs once with a broad query constructed from KEYWORDS."""

    # def get_title(jobitem: dict) -> str:
    #     return jobitem.get("MatchedObjectDescriptor", {}).get("PositionTitle")

    # def get_company(jobitem: dict) -> str:
    #     return jobitem.get("MatchedObjectDescriptor", {}).get("OrganizationName")
    
    # def get_location(jobitem: dict) -> str:
    #     return jobitem.get("MatchedObjectDescriptor", {}).get("PositionLocation", {}).get("LocationName")

    # def get_url(jobitem: dict) -> str:
    #     return jobitem.get("MatchedObjectDescriptor", {}).get("ApplyURI")[0]
    
    jobs = []
    if not USAJOBS_API_KEY:
        log.warning("USAJobs: Missing API Key.")
        return jobs
    
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": USAJOBS_USER_AGENT or EMAIL_SENDER or "job_alert_script",
        "Authorization-Key": USAJOBS_API_KEY.strip(),
    }

    for query in KEYWORDS:
        try:
            for page in range(1, MAX_SOURCE_PAGES + 1):
                params = {
                    "Keyword": query,
                    "HiringPath": "graduates",
                    "SecurityClearanceRequired": 0,
                    "LocationName": LOCATION,
                    "Radius": RADIUS_MILES,
                    "DatePosted": 2,
                    "Fields": "Full",
                    "ResultsPerPage": 100,
                    "Page": page,
                }
                response = request_with_retry("https://data.usajobs.gov/api/search", params=params, headers=headers)
                data = response.json()
                search_result = data.get("SearchResult", {})
                items = search_result.get("SearchResultItems", [])
                if page == 1:
                    log.info("USAJobs found %s jobs for '%s'.", search_result.get("SearchResultCount", 0), query)
                if not items:
                    break
                for item in items:
                    if not is_recent_graduate_usajobs_item(item):
                        continue
                    j = item.get("MatchedObjectDescriptor", {})
                    jobs.append(build_job(
                        title=j.get("PositionTitle", "N/A"),
                        company=j.get("OrganizationName", "N/A"),
                        location=j.get("PositionLocationDisplay", LOCATION),
                        url=j.get("PositionURI", "#"),
                        source="USAJobs",
                        posted=j.get("PublicationStartDate", "")[:10],
                        description=j.get("UserArea", {}).get("Details", {}).get("JobSummary", ""),
                    ))
        except Exception as e:
            log.warning("USAJobs fetch failed for '%s': %s", query, e)

    return jobs

def fetch_handshake() -> list[dict]:
    """Handshake runs once with a query derived from KEYWORDS."""
    jobs = []
    driver = None
    try:
        log.info("Handshake: initializing Chrome driver...")
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,800")
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        profile_path = os.path.join(script_dir, "chrome_profile_handshake")
        options.add_argument(f"--user-data-dir={profile_path}")
        options.add_argument("--profile-directory=Default")
        
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)

        driver.get("https://app.joinhandshake.com/dashboard")
        time.sleep(3)
        
        if "login" in driver.current_url or "auth" in driver.current_url:
            log.info("Handshake: initiating login...")
            driver.get("https://app.joinhandshake.com/login")
            time.sleep(3)
            
            page_source = driver.page_source
            if "myUMBC" in page_source or "saml" in driver.current_url:
                driver.get("https://app.joinhandshake.com/auth/saml/959/session/new?redirect_to_idp=true&ref=app-domain")
                time.sleep(5)
            elif "input" in page_source and "email" in page_source.lower():
                email_input = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']")))
                email_input.clear()
                email_input.send_keys(HANDSHAKE_EMAIL)
                driver.find_element(By.XPATH, "//button[contains(text(), 'Next')]").click()
                time.sleep(4)
                driver.get("https://app.joinhandshake.com/auth/saml/959/session/new?redirect_to_idp=true&ref=app-domain")
                time.sleep(5)

            if "joinhandshake.com" not in driver.current_url or "saml" in driver.current_url:
                log.info("Handshake: waiting for manual Duo approval (120s)...")
                WebDriverWait(driver, 120).until(lambda d: "joinhandshake.com" in d.current_url and "auth" not in d.current_url)
        else:
            log.info("Handshake: session reused.")

        # Construct broad query for Handshake (limited words)
        query = " ".join(KEYWORDS[:3])
        location_filter = json.dumps({"label": "Baltimore, MD", "point": "39.2896,-76.6123", "type": "place", "id": "171821836", "distance": "50mi"})
        search_params = {"query": query, "per_page": "25", "sort": "posted_date_desc", "locationFilter": location_filter}
        driver.get(f"https://app.joinhandshake.com/job-search/10856988?{urlencode(search_params)}")
        time.sleep(6)

        links = driver.find_elements(By.TAG_NAME, "a")
        job_links_data = []
        for a in links:
            href = a.get_attribute("href") or ""
            if "/jobs/" in href and "searchId" not in href:
                lines = [l.strip() for l in a.text.split("\n") if l.strip()]
                if len(lines) >= 2:
                    job_links_data.append({"title": lines[2] if len(lines) >= 3 else lines[1], "company": lines[0], "url": href})

        for job in job_links_data[: min(MAX_SOURCE_RESULTS, len(job_links_data))]:
            try:
                driver.get(job["url"])
                time.sleep(2)
                desc = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-hook='job-description'], [class*='description']"))).text
                candidate = build_job(
                    title=job["title"],
                    company=job["company"],
                    location=LOCATION,
                    url=job["url"],
                    source="Handshake",
                    posted="Recent",
                    description=desc[:3000],
                )
                if is_entry_level(candidate) and is_related(candidate):
                    jobs.append(candidate)
            except: continue
        log.info(f"Handshake: found {len(jobs)} related entry-level jobs.")
    except Exception as e:
        log.warning(f"Handshake failed: {e}")
    finally:
        if driver: driver.quit()
    return jobs

def fetch_simplify() -> list[dict]:
    """Simplify is CS-focused by default in this URL, but we'll still check relevance."""
    jobs = []
    driver = None
    try:
        log.info("Simplify: initializing driver...")
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        targets = [
            "https://simplify.jobs/l/new-grad-software",
            "https://simplify.jobs/l/entry-level-software-engineer",
        ]
        seen_urls = set()
        for target in targets:
            driver.get(target)
            time.sleep(5)
            cards = driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']")
            for card in cards[:MAX_SOURCE_RESULTS]:
                try:
                    url = card.get_attribute("href")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    text = [line.strip() for line in card.text.split("\n") if line.strip()]
                    if len(text) >= 2:
                        title = text[0]
                        company = text[1]
                        location = next((line for line in text[2:] if "," in line or "Remote" in line), LOCATION)
                        job = build_job(
                            title=title,
                            company=company,
                            location=location,
                            url=url,
                            source="Simplify",
                            posted="Recent",
                        )
                        if is_related(job):
                            jobs.append(job)
                except:
                    continue
        log.info(f"Simplify returned {len(jobs)} jobs.")
    except Exception as e:
        log.warning(f"Simplify failed: {e}")
    finally:
        if driver: driver.quit()
    return jobs

def fetch_indeed(keyword: str) -> list[dict]:
    jobs = []
    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument(f"user-agent={HEADERS['User-Agent']}")
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        seen_urls = set()
        for start in range(0, MAX_SOURCE_RESULTS, 10):
            params = {"q": f"{keyword} entry level", "l": LOCATION, "radius": RADIUS_MILES, "fromage": "2", "start": start}
            driver.get(f"https://www.indeed.com/jobs?{urlencode(params)}")
            time.sleep(4)
            soup = BeautifulSoup(driver.page_source, "html.parser")
            cards = soup.find_all("div", class_="job_seen_beacon")
            if not cards:
                break
            page_added = 0
            for div in cards:
                title_tag = div.find("h2", class_="jobTitle")
                comp_tag = div.find("span", {"data-testid": "company-name"})
                loc_tag = div.find("div", {"data-testid": "text-location"})
                link_tag = div.find("a", class_="jcs-JobTitle")
                if title_tag and link_tag:
                    url = "https://www.indeed.com" + link_tag["href"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    jobs.append(build_job(
                        title=title_tag.text,
                        company=comp_tag.text if comp_tag else "N/A",
                        location=loc_tag.text if loc_tag else LOCATION,
                        url=url,
                        source="Indeed",
                        posted="Today",
                    ))
                    page_added += 1
            if page_added == 0:
                break
    except Exception as e:
        log.warning(f"Indeed fetch failed for '{keyword}': {e}")
    finally:
        if driver: driver.quit()
    return jobs


def fetch_greenhouse() -> list[dict]:
    jobs = []
    if not GREENHOUSE_BOARDS:
        return jobs
    for board in GREENHOUSE_BOARDS:
        board_count = 0
        try:
            response = request_with_retry(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
            data = response.json()
            for job in data.get("jobs", []):
                jobs.append(build_job(
                    title=job.get("title", "N/A"),
                    company=board,
                    location=job.get("location", {}).get("name", LOCATION),
                    url=job.get("absolute_url", "#"),
                    source="Greenhouse",
                    posted=job.get("updated_at", "")[:10] or "Recent",
                    description=html_to_text(job.get("content", "")),
                ))
                board_count += 1
            log.info("Greenhouse board '%s' fetched %s jobs.", board, board_count)
        except Exception as e:
            log.warning("Greenhouse fetch failed for '%s': %s", board, e)
    return jobs


def fetch_lever() -> list[dict]:
    jobs = []
    if not LEVER_SITES:
        return jobs
    for site in LEVER_SITES:
        site_count = 0
        try:
            for skip in range(0, MAX_SOURCE_RESULTS, 100):
                response = request_with_retry(
                    f"https://api.lever.co/v0/postings/{site}",
                    params={"mode": "json", "limit": 100, "skip": skip},
                )
                listings = response.json()
                if not listings:
                    break
                for job in listings:
                    categories = job.get("categories", {})
                    jobs.append(build_job(
                        title=job.get("text", "N/A"),
                        company=site,
                        location=categories.get("location") or " / ".join(categories.get("allLocations", [])) or LOCATION,
                        url=job.get("hostedUrl") or job.get("applyUrl") or "#",
                        source="Lever",
                        posted="Recent",
                        description=html_to_text(job.get("descriptionPlain", "")) or html_to_text(job.get("description", "")),
                    ))
                    site_count += 1
                if len(listings) < 100:
                    break
            log.info("Lever site '%s' fetched %s jobs.", site, site_count)
        except Exception as e:
            log.warning("Lever fetch failed for '%s': %s", site, e)
    return jobs

def fetch_mwejobs(keyword: str) -> list[dict]:



    
    jobs = []
    driver = None
    try:
        log.info(f"MWEJobs: searching for '{keyword}'...")
        options = webdriver.ChromeOptions()
        # options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"user-agent={HEADERS['User-Agent']}")
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        
        # Using Default.aspx?guest=1 as it matches the provided HTML structure
        driver.get("https://mwejobs.maryland.gov/vosnet/Default.aspx?plang=E&guest=1")
        time.sleep(5)
        
        wait = WebDriverWait(driver, 20)
        
        # Try specific IDs provided by the user first
        kw_input = None
        for cid in ["univsearchtxtkeyword", "txtKeyword", "Keywords", "Keyword"]:
            try:
                kw_input = driver.find_element(By.ID, cid)
                if kw_input.is_displayed(): break
            except: continue
        
        if not kw_input:
            kw_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[id*='Keyword'], input[name*='Keyword']")))

        kw_input.clear()
        kw_input.send_keys(keyword)
        
        # Location input
        loc_input = None
        for cid in ["univsearchlocation", "txtLocation", "Location"]:
            try:
                loc_input = driver.find_element(By.ID, cid)
                if loc_input.is_displayed(): break
            except: continue
        
        if not loc_input:
            loc_input = driver.find_element(By.CSS_SELECTOR, "input[id*='Location'], input[name*='Location']")
        
        loc_input.clear()
        loc_input.send_keys(LOCATION)
        
        # Search button
        search_btn = None
        for cid in ["univsearchbtn", "btnSearch", "ButtonSearch", "SearchButton"]:
            try:
                search_btn = driver.find_element(By.ID, cid)
                if search_btn.is_displayed(): break
            except: continue
        
        if not search_btn:
            # Fallback for search button, including <a> tags as seen in HTML
            search_btn = driver.find_element(By.CSS_SELECTOR, "a[id*='searchbtn'], button[id*='Search'], input[type='submit'][value*='Search']")
            
        search_btn.click()
        
        # Wait for results page - looking for job title links
        wait.until(EC.presence_of_element_located((By.XPATH, "//a[contains(@id, 'lnkJobTitle')]")))
        time.sleep(3) 
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for a in soup.find_all("a", id=re.compile(r"lnkJobTitle")):
            tr = a.find_parent("tr")
            if tr:
                tds = tr.find_all("td")
                if len(tds) >= 3:
                    jobs.append({
                        "title":    a.text.strip(),
                        "company":  tds[2].text.strip() if len(tds) > 2 else "N/A",
                        "location": tds[3].text.strip() if len(tds) > 3 else LOCATION,
                        "url":      "https://mwejobs.maryland.gov/vosnet/" + a["href"],
                        "source":   "MWEJobs",
                        "posted":   "Recent",
                    })
        log.info(f"MWEJobs: found {len(jobs)} jobs for '{keyword}'.")
    except Exception as e:
        log.warning(f"MWEJobs fetch failed for '{keyword}': {e}")
    finally:
        if driver: driver.quit()
    return jobs

# ── Logic ─────────────────────────────────────────────────────────────────────

def deduplicate(jobs: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for job in jobs:
        key = (
            normalize_text(job.get("title", "")),
            normalize_text(job.get("company", "")),
            normalize_text(job.get("location", "")),
            job.get("canonical_url") or canonicalize_url(job.get("url", "")),
        )
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


def summarize_sources(jobs: list[dict]) -> Counter:
    return Counter(job.get("source", "Unknown") for job in jobs)

def build_email_html(jobs: list[dict]) -> str:
    by_source: dict[str, list] = {}
    for job in jobs: by_source.setdefault(job["source"], []).append(job)
    colors = {
        "LinkedIn": "#0077b5", 
        "Adzuna": "#e8593a", 
        "USAJobs": "#1a3a6e", 
        "Handshake": "#e8734a", 
        "Simplify": "#6366f1", 
        "Indeed": "#2164f3",
        "MWEJobs": "#00472f"
    }
    rows = ""
    
    if not jobs:
        rows = "<tr><td colspan='3' style='padding:30px;text-align:center;color:#666;font-size:16px;'>No new job listings found today.</td></tr>"
        summary_text = "No new listings found near Baltimore"
    else:
        summary_text = f"{len(jobs)} relevant listings found near Baltimore"
        for source, src_jobs in by_source.items():
            color = colors.get(source, "#555")
            rows += f"<tr><td colspan='3' style='background:{color};color:#fff;padding:8px 14px;font-weight:bold;'>{source} · {len(src_jobs)} listings</td></tr>"
            for j in src_jobs:
                rows += f"<tr><td style='padding:8px 14px;border-bottom:1px solid #eee;'><a href='{j['url']}' style='color:#1a73e8;text-decoration:none;font-weight:600;'>{j['title']}</a></td><td style='padding:8px 14px;border-bottom:1px solid #eee;'>{j['company']}</td><td style='padding:8px 14px;border-bottom:1px solid #eee;font-size:12px;'>{j['location']}<br>{j['posted']}</td></tr>"
    
    return f"<html><body style='font-family:Arial;max-width:900px;margin:auto;background:#f4f4f4;padding:20px;'><div style='background:#1a3a6e;padding:20px;border-radius:8px 8px 0 0;color:#fff;'><h1>🎓 New Job Alerts</h1><p>{summary_text}</p></div><div style='background:#fff;border:1px solid #ddd;border-radius:0 0 8px 8px;'><table width='100%' cellpadding='0' cellspacing='0'><tbody>{rows}</tbody></table></div></body></html>"

def send_email(jobs: list[dict]) -> None:
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]): return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 {len(jobs)} New Job Alerts - {datetime.now().strftime('%b %d %I:%M %p')}"
    msg["From"], msg["To"] = EMAIL_SENDER, EMAIL_RECIPIENT
    
    plain_text = "\n".join([f"[{j['source']}] {j['title']} @ {j['company']}" for j in jobs]) if jobs else "No new jobs found today."
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(build_email_html(jobs), "html"))
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
    log.info("Email sent.")

def run():
    log.info("Starting generalized job search...")
    
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_results.json")
    last_urls = set()
    if os.path.exists(out_path):
        try:
            with open(out_path, "r") as f:
                prev_data = json.load(f)
                last_urls = {canonicalize_url(j["url"]) for j in prev_data if "url" in j}
        except: pass

    source_stats = defaultdict(lambda: {"fetched": 0, "relevant": 0, "new": 0})
    all_jobs = []

    def collect(source_jobs: list[dict]) -> None:
        all_jobs.extend(source_jobs)
        for source, count in summarize_sources(source_jobs).items():
            source_stats[source]["fetched"] += count

    collect(fetch_usajobs())
    collect(fetch_handshake())
    collect(fetch_simplify())
    collect(fetch_greenhouse())
    collect(fetch_lever())

    for kw in KEYWORDS:
        log.info("Searching: '%s'", kw)
        collect(fetch_linkedin(kw))
        collect(fetch_adzuna(kw))
        collect(fetch_indeed(kw))
        # collect(fetch_mwejobs(kw))
        time.sleep(2)

    unique_jobs = deduplicate(all_jobs)
    filtered_jobs = [j for j in unique_jobs if is_related(j)]
    
    new_jobs = [j for j in filtered_jobs if canonicalize_url(j["url"]) not in last_urls]
    log.info(f"Summary: {len(all_jobs)} found -> {len(filtered_jobs)} relevant/entry -> {len(new_jobs)} new.")

    for source, count in summarize_sources(unique_jobs).items():
        source_stats[source]["deduped"] = count
    for source, count in summarize_sources(filtered_jobs).items():
        source_stats[source]["relevant"] += count
    for source, count in summarize_sources(new_jobs).items():
        source_stats[source]["new"] += count
    for source in sorted(source_stats):
        stats = source_stats[source]
        deduped = stats.get("deduped", stats["fetched"])
        filtered_out = max(0, deduped - stats["relevant"])
        log.info(
            "Source %-10s fetched=%-4s deduped=%-4s filtered_out=%-4s relevant=%-4s new=%-4s",
            source,
            stats["fetched"],
            deduped,
            filtered_out,
            stats["relevant"],
            stats["new"],
        )

    # Always send email, even if new_jobs is empty
    send_email(new_jobs)

    with open(out_path, "w") as f:
        json.dump(filtered_jobs, f, indent=2)

if __name__ == "__main__":
    run()
