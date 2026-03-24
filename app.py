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
            # 1. Get login page to extract CSRF token
            resp = self.scraper.get(f"{self.base_url}/login")
            if resp.status_code != 200:
                logger.error(f"Failed to reach login page: {resp.status_code}")
                return False

            soup = BeautifulSoup(resp.text, 'html.parser')
            csrf_token = soup.find('input', {'name': '_token'})
            if not csrf_token:
                logger.error("No CSRF token found on login page.")
                return False
            csrf_token = csrf_token['value']

            # 2. Post login data
            payload = {
                '_token': csrf_token,
                'email': self.email,
                'password': self.password,
                'remember': '1'
            }
            login_resp = self.scraper.post(f"{self.base_url}/login", data=payload, allow_redirects=True)
            # After login, we should be redirected to dashboard or portal
            if login_resp.status_code != 200:
                logger.error(f"Login POST returned {login_resp.status_code}")
                return False

            # 3. Verify we are logged in by accessing a protected page
            dashboard_resp = self.scraper.get(f"{self.base_url}/dashboard")
            if dashboard_resp.status_code != 200:
                logger.error("Failed to access dashboard after login.")
                return False

            # 4. Extract CSRF token from dashboard for later use
            soup = BeautifulSoup(dashboard_resp.text, 'html.parser')
            token_input = soup.find('input', {'name': '_token'})
            if token_input:
                self.csrf_token = token_input['value']
                logger.info("CSRF token extracted.")
            else:
                logger.warning("No CSRF token found on dashboard; proceeding anyway.")

            self.logged_in = True
            logger.info("Login successful.")
            return True

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
        # Try table rows first
        rows = soup.select("table tbody tr")
        if not rows:
            rows = soup.select("table tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 2:
                number = cells[0].get_text(strip=True)
                # Filter out empty or header rows
                if number and not number.lower().startswith('number'):
                    numbers.append({
                        'number': number,
                        'details': [c.get_text(strip=True) for c in cells[1:]]
                    })
        # If no table found, try looking for divs with numbers (fallback)
        if not numbers:
            # Look for any element that looks like a phone number (contains '+')
            potential_numbers = soup.find_all(text=re.compile(r'\+?\d{10,}'))
            for txt in potential_numbers:
                txt = txt.strip()
                if re.match(r'^\+?\d{10,}$', txt):
                    numbers.append({'number': txt, 'details': []})
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

        # Extract statistics – try common IDs
        stats = {
            'total': '0',
            'paid': '0',
            'unpaid': '0',
            'revenue': '0'
        }
        for sid, key in [('CountSMS', 'total'), ('PaidSMS', 'paid'), ('UnpaidSMS', 'unpaid'), ('RevenueSMS', 'revenue')]:
            elem = soup.find(id=sid)
            if elem:
                val = elem.get_text(strip=True).replace(' USD', '').replace(',', '')
                stats[key] = val
        # If not found, try to extract from text
        if stats['total'] == '0':
            text = soup.get_text()
            match = re.search(r'Total SMS[\s:]*(\d+)', text, re.I)
            if match:
                stats['total'] = match.group(1)

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
        # Fallback: look for divs with message content (some IVAS versions use divs)
        if not messages:
            message_divs = soup.select(".message-item, .sms-item, .otp-item")
            for div in message_divs:
                sender = div.select_one(".sender, .from") or div.select_one(".col-3")
                message = div.select_one(".message, .text") or div.select_one(".col-9")
                time = div.select_one(".time, .date") or div.select_one(".col-2")
                revenue = div.select_one(".revenue, .price") or div.select_one(".col-2")
                if sender and message:
                    messages.append({
                        'sender': sender.get_text(strip=True),
                        'message': message.get_text(strip=True),
                        'time': time.get_text(strip=True) if time else '',
                        'revenue': revenue.get_text(strip=True) if revenue else ''
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

# Debug endpoint: return raw HTML of any page (for troubleshooting)
@app.route('/debug/html')
def debug_html():
    page = request.args.get('page', 'dashboard')
    if not client.ensure_login():
        return "Not logged in", 401
    if page == 'numbers':
        resp = client.scraper.get(f"{client.base_url}/portal/numbers")
    elif page == 'live':
        resp = client.scraper.get(f"{client.base_url}/portal/live/my_sms")
    else:
        resp = client.scraper.get(f"{client.base_url}/dashboard")
    return resp.text

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
