import os
import re
import time
import logging
import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template
from requests.exceptions import ConnectionError, Timeout

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
        self.max_retries = 3
        self.retry_delay = 2

        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': self.base_url,
        })

    def _request_with_retry(self, method, url, **kwargs):
        """Make a request with retry logic."""
        for attempt in range(self.max_retries):
            try:
                response = self.scraper.request(method, url, timeout=15, **kwargs)
                return response
            except (ConnectionError, Timeout) as e:
                logger.warning(f"Request failed (attempt {attempt+1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                else:
                    raise
        return None

    def login(self):
        """Perform login using email and password."""
        logger.info("Attempting login...")
        try:
            # 1. Get login page to extract CSRF token
            resp = self._request_with_retry('GET', f"{self.base_url}/login")
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
            login_resp = self._request_with_retry('POST', f"{self.base_url}/login", data=payload, allow_redirects=True)
            if login_resp.status_code != 200:
                logger.error(f"Login POST returned {login_resp.status_code}")
                return False

            # 3. Verify login by accessing a page that requires authentication
            # Try /portal first (which redirects to /portal/sms/received)
            portal_resp = self._request_with_retry('GET', f"{self.base_url}/portal")
            if portal_resp.status_code != 200:
                logger.error(f"Failed to access portal after login: {portal_resp.status_code}")
                return False

            # 4. Check if we are actually logged in (look for user-specific element)
            soup = BeautifulSoup(portal_resp.text, 'html.parser')
            # Try to find something that indicates logged‑in state, e.g., a logout link or username
            logout_link = soup.find('a', href=re.compile(r'/logout'))
            if not logout_link:
                logger.warning("No logout link found – login may have failed.")
                # Could also check for presence of "Account Code" or similar
                account_code = soup.find(text=re.compile(r'Account Code'))
                if not account_code:
                    logger.error("No account code found – login likely failed.")
                    return False

            # Extract CSRF token from portal page (if present)
            token_input = soup.find('input', {'name': '_token'})
            if token_input:
                self.csrf_token = token_input['value']
                logger.info("CSRF token extracted from portal.")
            else:
                logger.warning("No CSRF token found; proceeding anyway.")

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
        # Quick check by hitting a protected page
        try:
            test = self._request_with_retry('GET', f"{self.base_url}/portal")
            if test.status_code == 200:
                # Refresh CSRF token
                soup = BeautifulSoup(test.text, 'html.parser')
                token_input = soup.find('input', {'name': '_token'})
                if token_input:
                    self.csrf_token = token_input['value']
                return True
        except Exception:
            pass
        logger.warning("Session expired, re‑logging in.")
        self.logged_in = False
        return self.login()

    def fetch_numbers(self):
        """Scrape the numbers page and return list of numbers."""
        if not self.ensure_login():
            return None
        resp = self._request_with_retry('GET', f"{self.base_url}/portal/numbers")
        if resp.status_code != 200:
            logger.error(f"Failed to fetch numbers: {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        numbers = []
        rows = soup.select("table tbody tr")
        if not rows:
            rows = soup.select("table tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 1:
                number = cells[0].get_text(strip=True)
                if number and not number.lower().startswith('number') and re.match(r'^\+?\d{10,}$', number):
                    numbers.append({
                        'number': number,
                        'details': [c.get_text(strip=True) for c in cells[1:]]
                    })
        if not numbers:
            # Fallback: find any phone number in text
            potential = soup.find_all(text=re.compile(r'\+?\d{10,}'))
            for txt in potential:
                txt = txt.strip()
                if re.match(r'^\+?\d{10,}$', txt):
                    numbers.append({'number': txt, 'details': []})
        logger.info(f"Found {len(numbers)} numbers.")
        return numbers

    def fetch_received_sms(self, from_date=None, to_date=None):
        """Fetch SMS from /portal/sms/received (with optional date range)."""
        if not self.ensure_login():
            return None
        # Build payload
        payload = {}
        if from_date:
            payload['from'] = from_date
        if to_date:
            payload['to'] = to_date
        if self.csrf_token:
            payload['_token'] = self.csrf_token

        headers = {
            'Accept': 'text/html, */*; q=0.01',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': self.base_url,
            'Referer': f"{self.base_url}/portal/sms/received"
        }
        resp = self._request_with_retry('POST', f"{self.base_url}/portal/sms/received/getsms", data=payload, headers=headers)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch received SMS: {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        # Extract statistics (same as before)
        stats = {
            'count': soup.select_one("#CountSMS").text if soup.select_one("#CountSMS") else '0',
            'paid': soup.select_one("#PaidSMS").text if soup.select_one("#PaidSMS") else '0',
            'unpaid': soup.select_one("#UnpaidSMS").text if soup.select_one("#UnpaidSMS") else '0',
            'revenue': soup.select_one("#RevenueSMS").text.replace(' USD', '') if soup.select_one("#RevenueSMS") else '0'
        }
        # Parse messages (if any)
        messages = []
        items = soup.select("div.item")
        for item in items:
            # This old structure might not be present; fallback to table
            pass
        # Fallback to table
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
                if message:
                    messages.append({
                        'sender': sender,
                        'message': message,
                        'time': time_str,
                        'revenue': revenue
                    })
        logger.info(f"Fetched {len(messages)} received SMS messages.")
        return {'stats': stats, 'messages': messages}

    def fetch_live_sms(self):
        """Scrape the live SMS page and return messages + stats."""
        if not self.ensure_login():
            return None
        resp = self._request_with_retry('GET', f"{self.base_url}/portal/live/my_sms")
        if resp.status_code != 200:
            logger.error(f"Failed to fetch live SMS: {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Extract statistics (may be similar IDs)
        stats = {'total': '0', 'paid': '0', 'unpaid': '0', 'revenue': '0'}
        for sid, key in [('CountSMS', 'total'), ('PaidSMS', 'paid'), ('UnpaidSMS', 'unpaid'), ('RevenueSMS', 'revenue')]:
            elem = soup.find(id=sid)
            if elem:
                val = elem.get_text(strip=True).replace(' USD', '').replace(',', '')
                stats[key] = val

        # Parse messages from table
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
                if message:
                    messages.append({
                        'sender': sender,
                        'message': message,
                        'time': time_str,
                        'revenue': revenue
                    })
        # Fallback: look for divs
        if not messages:
            divs = soup.select(".message-item, .sms-item, .otp-item")
            for div in divs:
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
        return {'stats': stats, 'messages': messages}

# -------------------------------------------------------------------
# Flask App
# -------------------------------------------------------------------
app = Flask(__name__)

# Read credentials from environment variables (set in Vercel)
IVAS_EMAIL = os.environ.get('IVAS_EMAIL', 'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')

client = IVASAutoClient(IVAS_EMAIL, IVAS_PASSWORD)

# Attempt initial login on startup
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

@app.route('/api/received')
def api_received():
    from_date = request.args.get('from')
    to_date = request.args.get('to')
    data = client.fetch_received_sms(from_date, to_date)
    if data is None:
        return jsonify({'error': 'Could not fetch received SMS'}), 500
    return jsonify(data)

@app.route('/api/live')
def api_live():
    data = client.fetch_live_sms()
    if data is None:
        return jsonify({'error': 'Could not fetch live SMS'}), 500
    return jsonify(data)

# Combined endpoint for dashboard
@app.route('/api/all')
def api_all():
    numbers = client.fetch_numbers()
    received = client.fetch_received_sms()
    live = client.fetch_live_sms()
    if numbers is None or received is None or live is None:
        return jsonify({'error': 'Failed to fetch one or more data sources'}), 500
    return jsonify({
        'numbers': numbers,
        'received': received,
        'live': live
    })

# Debug endpoints
@app.route('/debug/<page>')
def debug_page(page):
    if not client.ensure_login():
        return "Not logged in", 401
    if page == 'portal':
        resp = client._request_with_retry('GET', f"{client.base_url}/portal")
    elif page == 'numbers':
        resp = client._request_with_retry('GET', f"{client.base_url}/portal/numbers")
    elif page == 'live':
        resp = client._request_with_retry('GET', f"{client.base_url}/portal/live/my_sms")
    elif page == 'received':
        resp = client._request_with_retry('GET', f"{client.base_url}/portal/sms/received")
    else:
        resp = client._request_with_retry('GET', f"{client.base_url}/{page}")
    return resp.text

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
