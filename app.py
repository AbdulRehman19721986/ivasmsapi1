import os
import re
import logging
import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template
from datetime import datetime
import time
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class IVASAutoClient:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.base_url = "https://www.ivasms.com"
        self.scraper = cloudscraper.create_scraper()
        self.logged_in = False
        self.csrf_token = None

        # Standard headers
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': self.base_url,
        })

    def login(self):
        """Perform login using email and password, retrieve session cookies and CSRF token."""
        logger.info("Attempting login with provided credentials...")
        try:
            # First get the login page to capture any hidden tokens
            resp = self.scraper.get(f"{self.base_url}/login")
            if resp.status_code != 200:
                logger.error(f"Failed to reach login page: {resp.status_code}")
                return False

            soup = BeautifulSoup(resp.text, 'html.parser')
            csrf_token = soup.find('input', {'name': '_token'})
            csrf_token = csrf_token['value'] if csrf_token else ''

            # Prepare login payload
            payload = {
                '_token': csrf_token,
                'email': self.email,
                'password': self.password,
                'remember': '1'
            }

            # Post login
            login_resp = self.scraper.post(f"{self.base_url}/login", data=payload, allow_redirects=False)
            if login_resp.status_code in [302, 200]:
                # Follow redirect to dashboard
                dashboard_resp = self.scraper.get(f"{self.base_url}/dashboard")
                if dashboard_resp.status_code == 200:
                    # Extract CSRF token from dashboard for later use
                    soup = BeautifulSoup(dashboard_resp.text, 'html.parser')
                    token_input = soup.find('input', {'name': '_token'})
                    if token_input:
                        self.csrf_token = token_input['value']
                    self.logged_in = True
                    logger.info("Login successful.")
                    return True
            logger.error("Login failed – wrong credentials or login endpoint changed.")
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def ensure_login(self):
        """Check if session is still alive; if not, re‑login."""
        if not self.logged_in:
            return self.login()
        # Quick check by hitting a protected endpoint
        test = self.scraper.get(f"{self.base_url}/dashboard")
        if test.status_code == 200:
            return True
        logger.warning("Session expired, re‑logging in.")
        self.logged_in = False
        return self.login()

    def fetch_numbers(self):
        """Scrape the numbers page and return list of numbers with details."""
        if not self.ensure_login():
            return None
        resp = self.scraper.get(f"{self.base_url}/portal/numbers")
        if resp.status_code != 200:
            logger.error(f"Failed to fetch numbers: {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        numbers = []
        # The numbers are usually in a table with rows like <tr><td>+1234567890</td><td>...</td></tr>
        rows = soup.select("table tbody tr")
        if not rows:
            rows = soup.select("table tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 2:
                number = cells[0].get_text(strip=True)
                # Additional details may be in other cells
                numbers.append({
                    'number': number,
                    'details': [c.get_text(strip=True) for c in cells[1:]]
                })
        logger.info(f"Found {len(numbers)} numbers.")
        return numbers

    def fetch_live_sms(self):
        """Scrape the live SMS page and return messages + stats."""
        if not self.ensure_login():
            return None
        resp = self.scraper.get(f"{self.base_url}/portal/live/my_sms")
        if resp.status_code != 200:
            logger.error(f"Failed to fetch live SMS: {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Extract statistics (may appear in a stats bar)
        stats = {}
        # Try to find the totals – adjust selectors based on actual page
        total_span = soup.find(id="CountSMS") or soup.find(string=re.compile(r"Total SMS"))
        paid_span = soup.find(id="PaidSMS")
        unpaid_span = soup.find(id="UnpaidSMS")
        revenue_span = soup.find(id="RevenueSMS")

        stats['total'] = total_span.get_text(strip=True) if total_span else '0'
        stats['paid'] = paid_span.get_text(strip=True) if paid_span else '0'
        stats['unpaid'] = unpaid_span.get_text(strip=True) if unpaid_span else '0'
        stats['revenue'] = revenue_span.get_text(strip=True).replace(' USD', '') if revenue_span else '0'

        # Parse message table
        messages = []
        rows = soup.select("table tbody tr")
        if not rows:
            rows = soup.select("table tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 4:
                sender = cells[0].get_text(strip=True)
                message = cells[1].get_text(strip=True)
                time_str = cells[2].get_text(strip=True)
                revenue = cells[3].get_text(strip=True)
                if message:  # only include rows with actual message
                    messages.append({
                        'sender': sender,
                        'message': message,
                        'time': time_str,
                        'revenue': revenue
                    })
        logger.info(f"Fetched {len(messages)} live SMS messages.")
        return {
            'stats': stats,
            'messages': messages
        }

# -------------------------------------------------------------------
# Flask App
# -------------------------------------------------------------------
app = Flask(__name__)

# Read credentials from environment variables (set in Vercel dashboard)
IVAS_EMAIL = os.environ.get('IVAS_EMAIL', 'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')

client = IVASAutoClient(IVAS_EMAIL, IVAS_PASSWORD)

# Attempt initial login at startup
with app.app_context():
    if not client.login():
        logger.error("Initial login failed – check credentials or IVAS availability.")
    else:
        logger.info("Initial login successful.")

@app.route('/')
def dashboard():
    return render_template('index.html')

@app.route('/api/numbers')
def api_numbers():
    numbers = client.fetch_numbers()
    if numbers is None:
        return jsonify({'error': 'Could not fetch numbers'}), 500
    return jsonify({'numbers': numbers})

@app.route('/api/live')
def api_live():
    live_data = client.fetch_live_sms()
    if live_data is None:
        return jsonify({'error': 'Could not fetch live SMS'}), 500
    return jsonify(live_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
