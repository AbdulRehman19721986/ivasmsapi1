import os
import re
import logging
import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

IVAS_EMAIL = os.environ.get('IVAS_EMAIL', 'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')
BASE_URL = "https://www.ivasms.com"

class IVASClient:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.logged_in = False
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': BASE_URL,
        })

    def login(self):
        try:
            # Get login page to extract CSRF token
            resp = self.scraper.get(f"{BASE_URL}/login", timeout=15)
            if resp.status_code != 200:
                logger.error(f"Failed to get login page: {resp.status_code}")
                return False
            soup = BeautifulSoup(resp.text, 'html.parser')
            csrf = soup.find('input', {'name': '_token'})
            if not csrf:
                logger.error("No CSRF token")
                return False

            # Post login
            payload = {
                '_token': csrf['value'],
                'email': IVAS_EMAIL,
                'password': IVAS_PASSWORD,
                'remember': '1'
            }
            login_resp = self.scraper.post(f"{BASE_URL}/login", data=payload, allow_redirects=True, timeout=15)
            if login_resp.status_code != 200:
                logger.error(f"Login POST returned {login_resp.status_code}")
                return False

            # Verify by checking if we can access a protected page (e.g., numbers)
            numbers_resp = self.scraper.get(f"{BASE_URL}/portal/numbers")
            if numbers_resp.status_code != 200:
                logger.error("Cannot access numbers page after login")
                return False

            # Quick check: does numbers page contain a table with numbers?
            soup = BeautifulSoup(numbers_resp.text, 'html.parser')
            if soup.find('table') or re.search(r'\+\d{10,}', numbers_resp.text):
                self.logged_in = True
                logger.info("Login successful (numbers page accessible).")
                return True
            else:
                logger.warning("Numbers page loaded but no numbers found")
                return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def ensure_login(self):
        if self.logged_in:
            # Quick check: try to fetch numbers page
            try:
                resp = self.scraper.get(f"{BASE_URL}/portal/numbers", timeout=10)
                if resp.status_code == 200:
                    return True
            except:
                pass
            self.logged_in = False
        return self.login()

    def fetch_numbers(self):
        if not self.ensure_login():
            return None
        resp = self.scraper.get(f"{BASE_URL}/portal/numbers")
        if resp.status_code != 200:
            logger.error(f"Numbers fetch failed: {resp.status_code}")
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
        numbers = []
        # Try to extract numbers from table
        rows = soup.select("table tbody tr")
        if not rows:
            rows = soup.select("table tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 1:
                number = cells[0].get_text(strip=True)
                if number and re.match(r'^\+?\d{10,}$', number):
                    numbers.append({
                        'number': number,
                        'details': [c.get_text(strip=True) for c in cells[1:]]
                    })
        if not numbers:
            # Fallback: find any phone number pattern
            for text in soup.find_all(text=True):
                match = re.search(r'(\+\d{10,})', text)
                if match:
                    numbers.append({'number': match.group(1), 'details': []})
        logger.info(f"Found {len(numbers)} numbers")
        return numbers

    def fetch_live_sms(self):
        if not self.ensure_login():
            return None
        resp = self.scraper.get(f"{BASE_URL}/portal/live/my_sms")
        if resp.status_code != 200:
            logger.error(f"Live SMS fetch failed: {resp.status_code}")
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
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
        logger.info(f"Fetched {len(messages)} live messages")
        return messages

    def fetch_received_sms(self, from_date=None, to_date=None):
        if not self.ensure_login():
            return None
        # Build payload
        payload = {}
        if from_date:
            payload['from'] = from_date
        if to_date:
            payload['to'] = to_date
        # Need CSRF token – extract from page
        resp = self.scraper.get(f"{BASE_URL}/portal/sms/received")
        if resp.status_code != 200:
            logger.error("Cannot access received SMS page")
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
        csrf = soup.find('input', {'name': '_token'})
        if csrf:
            payload['_token'] = csrf['value']
        headers = {
            'Accept': 'text/html, */*; q=0.01',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': BASE_URL,
            'Referer': f"{BASE_URL}/portal/sms/received"
        }
        data_resp = self.scraper.post(f"{BASE_URL}/portal/sms/received/getsms", data=payload, headers=headers)
        if data_resp.status_code != 200:
            logger.error(f"Received SMS fetch failed: {data_resp.status_code}")
            return None
        soup = BeautifulSoup(data_resp.text, 'html.parser')
        stats = {
            'count': soup.select_one("#CountSMS").text if soup.select_one("#CountSMS") else '0',
            'paid': soup.select_one("#PaidSMS").text if soup.select_one("#PaidSMS") else '0',
            'unpaid': soup.select_one("#UnpaidSMS").text if soup.select_one("#UnpaidSMS") else '0',
            'revenue': soup.select_one("#RevenueSMS").text.replace(' USD', '') if soup.select_one("#RevenueSMS") else '0'
        }
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
        return {'stats': stats, 'messages': messages}

client = IVASClient()

# Initial login attempt
if not client.login():
    logger.error("Initial login failed")
else:
    logger.info("Initial login successful")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/numbers')
def api_numbers():
    numbers = client.fetch_numbers()
    if numbers is None:
        return jsonify({'error': 'Could not fetch numbers'}), 500
    return jsonify({'numbers': numbers})

@app.route('/api/live')
def api_live():
    messages = client.fetch_live_sms()
    if messages is None:
        return jsonify({'error': 'Could not fetch live SMS'}), 500
    return jsonify({'messages': messages})

@app.route('/api/received')
def api_received():
    from_date = request.args.get('from')
    to_date = request.args.get('to')
    data = client.fetch_received_sms(from_date, to_date)
    if data is None:
        return jsonify({'error': 'Could not fetch received SMS'}), 500
    return jsonify(data)

@app.route('/api/all')
def api_all():
    numbers = client.fetch_numbers()
    live = client.fetch_live_sms()
    received = client.fetch_received_sms()
    if numbers is None or live is None or received is None:
        return jsonify({'error': 'Failed to fetch data'}), 500
    return jsonify({
        'numbers': numbers,
        'live': live,
        'received': received
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
