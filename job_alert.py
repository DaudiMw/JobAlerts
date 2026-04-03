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
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode
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


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── Filters ──────────────────────────────────────────────────────────────────

_EXP_BLOCKLIST = [
    r"\b(?:[2-9]|\d{2,})\+?\s*(?:-?\d+)?\s*(?:years?|yrs?)\b",
    r"\b(?:minimum|min|at least)\s+(?:of\s+)?(?:[2-9]|\d{2,})\s*(?:years?|yrs?)\b",
    r"\bsenior\b", r"\bsr\.\b", r"\bmid[- ]?level\b", r"\bintermediate\b",
    r"\blead\b", r"\bprincipal\b", r"\bstaff\b", r"\barchitect\b",
    r"\bmanager\b", r"\bdirector\b", r"\bhead of\b",
    # Generalized Level indicators (Level II, Grade 3, etc.)
    r"\b(?:level|grade|tier|sde|swe|ds)\s*[2-9]\b",
    r"\b\w+\s+[II|III|IV|V|VI]+\b",
]
_EXP_RE = re.compile("|".join(_EXP_BLOCKLIST), re.IGNORECASE)

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

def is_related(job: dict) -> bool:
    """Return True if the job title matches any of the user-provided keywords."""
    title = job.get("title", "").lower()
    for kw in KEYWORDS:
        # Check if the keyword exists as a phrase in the title
        if kw.lower() in title:
            return True
    return False

# ── Scrapers ──────────────────────────────────────────────────────────────────

def fetch_linkedin(keyword: str) -> list[dict]:
    jobs = []
    params = {
        "keywords": f"{keyword} entry level OR junior OR associate OR new grad",
        "location": LOCATION,
        "distance": RADIUS_MILES,
        "f_TPR": "r10800",
        "f_E": "1,2",
        "start": 0,
    }
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{urlencode(params)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.find_all("li"):
            title_tag   = card.find("h3", class_="base-search-card__title")
            company_tag = card.find("h4", class_="base-search-card__subtitle")
            loc_tag     = card.find("span", class_="job-search-card__location")
            link_tag    = card.find("a", class_="base-card__full-link")
            if title_tag and link_tag:
                jobs.append({
                    "title":    title_tag.text.strip(),
                    "company":  company_tag.text.strip() if company_tag else "N/A",
                    "location": loc_tag.text.strip() if loc_tag else LOCATION,
                    "url":      link_tag["href"],
                    "source":   "LinkedIn",
                    "posted":   "Last 3h",
                })
    except Exception as e:
        log.warning(f"LinkedIn fetch failed for '{keyword}': {e}")
    return jobs

def fetch_adzuna(keyword: str) -> list[dict]:
    jobs = []
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return jobs
    params = {
        "app_id":           ADZUNA_APP_ID,
        "app_key":          ADZUNA_APP_KEY,
        "results_per_page": "25",
        "what":             f"{keyword} entry level OR junior OR associate OR new grad",
        "where":            LOCATION,
        "distance":         str(RADIUS_MILES),
        "max_days_old":     "1",
        "sort_by":          "date",
        "full_time":        "1",
    }
    url = "https://api.adzuna.com/v1/api/jobs/us/search/1?" + urlencode(params)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        for job in data.get("results", []):
            jobs.append({
                "title":    job.get("title", "N/A"),
                "company":  job.get("company", {}).get("display_name", "N/A"),
                "location": job.get("location", {}).get("display_name", LOCATION),
                "url":      job.get("redirect_url", "#"),
                "source":   "Adzuna",
                "posted":   job.get("created", "Today")[:10],
            })
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
    
    # Construct broad query
    for query in KEYWORDS:
        
        # # API format requires specific headers and parameters
        params = {
            "Keyword": query,
            "SecurityClearanceRequired": 0,
            "LocationName": LOCATION,
            "Radius": RADIUS_MILES,
            "DatePosted": 1,
            "ResultsPerPage": 50,
        }
        
        # # Add JobCategoryCode for IT/Computer Science if any keyword relates to it
        # if any(kw in query.lower() for kw in ["software", "developer", "engineer", "data", "computer", "it"]):
        #     params["JobCategoryCode"] = "2210"

        headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": USAJOBS_USER_AGENT or EMAIL_SENDER or "job_alert_script",
            "Authorization-Key": USAJOBS_API_KEY.strip(),
        }
        
        try:
            url = "https://data.usajobs.gov/api/search"
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            
            data = r.json()
            # breakpoint()
            search_result = data.get("SearchResult", {})

            log.info(f"USAJobs found {search_result.get("SearchResultCount", 0)} jobs.")

            items = search_result.get("SearchResultItems", [])
            
            for item in items:
                j = item.get("MatchedObjectDescriptor", {})
                jobs.append({
                    "title":    j.get("PositionTitle", "N/A"),
                    "company":  j.get("OrganizationName", "N/A"),
                    "location": j.get("PositionLocationDisplay", LOCATION),
                    "url":      j.get("PositionURI", "#"),
                    "source":   "USAJobs",
                    "posted":   j.get("PublicationStartDate", "")[:10],
                })
            log.info(f"USAJobs returned {len(jobs)} jobs.")
        except Exception as e:
            log.warning(f"USAJobs fetch failed: {e}")

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

        for job in job_links_data[:10]:
            try:
                driver.get(job["url"])
                time.sleep(2)
                desc = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-hook='job-description'], [class*='description']"))).text
                candidate = {**job, "location": LOCATION, "source": "Handshake", "posted": "Recent", "description": desc[:3000]}
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
        driver.get("https://simplify.jobs/l/new-grad-software")
        time.sleep(5)
        cards = driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']")
        for card in cards[:15]:
            try:
                text = card.text.split("\n")
                if len(text) >= 2:
                    job = {"title": text[0], "company": text[1], "location": LOCATION, "url": card.get_attribute("href"), "source": "Simplify", "posted": "Recent"}
                    if is_related(job):
                        jobs.append(job)
            except: continue
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
        params = {"q": f"{keyword} entry level", "l": LOCATION, "radius": RADIUS_MILES, "fromage": "1"}
        driver.get(f"https://www.indeed.com/jobs?{urlencode(params)}")
        time.sleep(4)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for div in soup.find_all("div", class_="job_seen_beacon"):
            title_tag = div.find("h2", class_="jobTitle")
            comp_tag  = div.find("span", {"data-testid": "company-name"})
            loc_tag   = div.find("div", {"data-testid": "text-location"})
            link_tag  = div.find("a", class_="jcs-JobTitle")
            if title_tag and link_tag:
                jobs.append({
                    "title":    title_tag.text.strip(),
                    "company":  comp_tag.text.strip() if comp_tag else "N/A",
                    "location": loc_tag.text.strip() if loc_tag else LOCATION,
                    "url":      "https://www.indeed.com" + link_tag["href"],
                    "source":   "Indeed",
                    "posted":   "Today",
                })
    except Exception as e:
        log.warning(f"Indeed fetch failed for '{keyword}': {e}")
    finally:
        if driver: driver.quit()
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
        key = (job["title"].lower().strip(), job["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique

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
                last_urls = {j["url"] for j in prev_data if "url" in j}
        except: pass

    all_jobs = []
    # Broad scrapers (Dynamic Queries)
    all_jobs.extend(fetch_usajobs())
    all_jobs.extend(fetch_handshake())
    all_jobs.extend(fetch_simplify())

    # Keyword scrapers
    for kw in KEYWORDS:
        log.info(f"Searching: '{kw}'")
        all_jobs.extend(fetch_linkedin(kw))
        all_jobs.extend(fetch_adzuna(kw))
        all_jobs.extend(fetch_indeed(kw))
        # all_jobs.extend(fetch_mwejobs(kw))
        time.sleep(2)

    unique_jobs = deduplicate(all_jobs)
    # Apply generalized filters
    filtered_jobs = [j for j in unique_jobs if is_entry_level(j) and is_related(j)]
    
    new_jobs = [j for j in filtered_jobs if j["url"] not in last_urls]
    log.info(f"Summary: {len(all_jobs)} found -> {len(filtered_jobs)} relevant/entry -> {len(new_jobs)} new.")

    # Always send email, even if new_jobs is empty
    send_email(new_jobs)

    with open(out_path, "w") as f:
        json.dump(filtered_jobs, f, indent=2)

if __name__ == "__main__":
    run()
