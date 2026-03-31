# JobAlerts - Project Overview

An automated job search and notification system designed for New Grad Computer Science roles in the Baltimore, MD area. The script scrapes multiple sources, aggregates the results, and sends a formatted HTML email digest.

## Core Technologies
- **Python 3.13+**
- **Selenium**: Used for guest-scraping LinkedIn job postings.
- **BeautifulSoup4**: For parsing HTML content.
- **Requests**: Interfacing with Adzuna and USAJobs APIs.
- **webdriver-manager**: Automatic management of Chrome drivers.
- **python-dotenv**: Environment variable management.

## Project Structure
- `job_alert.py`: The main script containing the scraping logic, API clients, and email dispatch system.
- `last_results.json`: Stores the results of the last successful run to prevent duplicate notifications (if implemented) or for logging purposes.
- `.env`: (Not committed) Contains sensitive API keys and email credentials.

## Setup & Installation

### 1. Prerequisites
Ensure you have Python installed. It is recommended to use the provided virtual environment in `.venv`.

### 2. Install Dependencies
```powershell
pip install requests beautifulsoup4 python-dotenv selenium webdriver-manager
```

### 3. Configuration
Create a `.env` file in the root directory with the following variables:
```env
EMAIL_SENDER=your_gmail@gmail.com
EMAIL_PASSWORD=your_gmail_app_password
EMAIL_RECIPIENT=your_email@gmail.com
ADZUNA_APP_ID=xxxxxxxx
ADZUNA_APP_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
USAJOBS_API_KEY=your_usajobs_key
USAJOBS_USER_AGENT=your_email@gmail.com
HANDSHAKE_EMAIL=your_handshake_email
HANDSHAKE_PASSWORD=your_handshake_password
```

## Usage

### Running Manually
```powershell
python job_alert.py
```

### Automation
The script is designed to be scheduled (e.g., every 3 hours).
- **Windows**: Use Task Scheduler to run `python.exe job_alert.py`.
- **Linux/Mac**: Add a cron job: `0 */3 * * * /path/to/python /path/to/job_alert.py`.

## Development Conventions
- **Scraping**: LinkedIn scraping is performed as a guest to avoid account flagging; however, it is sensitive to rate limits.
- **API Integration**: Prefers official APIs (Adzuna, USAJobs) where available.
- **Logging**: Uses the standard `logging` module to track progress and errors.
- **Email**: Sends a single MIMEMultipart HTML email containing all new job listings.
