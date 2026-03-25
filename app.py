"""
IVAS SMS Dashboard – Final Deployable Version
- Robust Login: Tries cookies first, then falls back to credentials.
- Perfect OTPs Tab: Uses a backend proxy to mirror the IVAS interface.
- Optional Firebase: App runs even if firebase_config.py is missing.
- Designed for Vercel & Kataboom.
"""
import os
import re
import json
import gzip
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, session, Response
import cloudscraper
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------- Logging Setup ----------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------------------- Configuration ----------------------
BASE_URL = "https://www.ivasms.com"
# IMPORTANT: Replace with your credentials or set as environment variables
IVAS_EMAIL = os.environ.get('IVAS_EMAIL', 'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')
COOKIES_ENV = os.environ.get('COOKIES_JSON', '')

# ---------------------- Optional Firebase Integration ----------------------
try:
    import pyrebase
    from firebase_config import firebase, db
    FIREBASE_ENABLED = True
    logger.info("Firebase module loaded successfully.")
except (ImportError, ModuleNotFoundError):
    FIREBASE_ENABLED = False
    db = None
    logger.warning("Firebase not configured (firebase_config.py missing or pyrebase not installed). Admin features will be limited.")

def verify_admin(username, password):
    if FIREBASE_ENABLED:
        try:
            admin_data = db.child("admin").get().val()
            if admin_data and admin_data.get("username") == username and check_password_hash(admin_data.get("password"), password):
                return True
        except Exception as e:
            logger.error(f"Firebase admin verification failed: {e}")
    # Fallback for both Firebase failure and when Firebase is disabled
    if username == "redx" and password == "redx":
        logger.warning("Using hardcoded fallback credentials for admin login.")
        return True
    return False

# ---------------------- IVAS Client Class ----------------------
class IVASClient:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(browser={'browser':'chrome','platform':'windows','mobile':False})
        self.logged_in = False
        self.csrf_token = None
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
        })

    def _decompress(self, response):
        content = response.content
        if content.startswith(b'\x1f\x8b'): # Gzip magic bytes
            try:
                return gzip.decompress(content).decode('utf-8', errors='replace')
            except Exception as e:
                logger.warning(f"Gzip decompression failed: {e}")
        return content.decode('utf-8', errors='replace')

    def _load_cookies_from_file(self):
        raw_json = COOKIES_ENV.strip()
        if not raw_json:
            path = os.path.join(os.path.dirname(__file__), 'cookies.json')
            if os.path.exists(path):
                with open(path) as f:
                    raw_json = f.read().strip()
        if not raw_json: return None
        try:
            data = json.loads(raw_json)
            return {c['name']: c['value'] for c in data if 'name' in c and 'value' in c}
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.error(f"Failed to parse cookies.json: {e}")
            return None

    def _verify_session(self):
        try:
            resp = self.scraper.get(f"{BASE_URL}/portal/sms/received", timeout=15, allow_redirects=True)
            if resp.status_code != 200 or "Account Login" in resp.text:
                logger.warning("Session verification failed. Response indicates user is not logged in.")
                return False
            
            soup = BeautifulSoup(self._decompress(resp), 'html.parser')
            token = soup.find('input', {'name': '_token'})
            if token and token.get('value'):
                self.csrf_token = token['value']
                self.logged_in = True
                logger.info("✅ Session verified successfully.")
                return True
            logger.warning("Session verification failed: Could not find CSRF token on a protected page.")
            return False
        except Exception as e:
            logger.error(f"Exception during session verification: {e}")
            return False

    def login(self):
        logger.info("Starting new login attempt...")
        # Step 1: Try with cookies
        cookies = self._load_cookies_from_file()
        if cookies:
            logger.info("Attempting login with cookies from cookies.json...")
            self.scraper.cookies.clear()
            for name, value in cookies.items():
                self.scraper.cookies.set(name, value, domain="www.ivasms.com")
            if self._verify_session():
                return True
            logger.warning("Cookies are invalid or expired.")
        else:
            logger.info("No cookies found. Proceeding with credentials.")

        # Step 2: Fallback to credentials
        logger.info("Attempting login with email/password credentials...")
        try:
            self.scraper.cookies.clear()
            login_page = self.scraper.get(f"{BASE_URL}/login", timeout=15)
            soup = BeautifulSoup(self._decompress(login_page), 'html.parser')
            token = soup.find('input', {'name': '_token'})
            if not token:
                logger.error("Credential login failed: Could not find CSRF token on login page.")
                return False
            
            payload = {'_token': token['value'], 'email': IVAS_EMAIL, 'password': IVAS_PASSWORD}
            self.scraper.post(f"{BASE_URL}/login", data=payload, timeout=15)

            if self._verify_session():
                return True
            logger.error("Credential login failed. Please check your IVAS_EMAIL and IVAS_PASSWORD.")
            return False
        except Exception as e:
            logger.error(f"Exception during credential login: {e}")
            return False

    def ensure_login(self):
        return self.logged_in and self.csrf_token or self.login()

    def fetch_all_data(self):
        if not self.ensure_login(): return None
        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
            # Fetch Received SMS for today
            received_resp = self.scraper.post(
                f"{BASE_URL}/portal/sms/received/getsms",
                data={'from': today_str, 'to': today_str, '_token': self.csrf_token},
                headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': f"{BASE_URL}/portal/sms/received"}
            )
            soup_received = BeautifulSoup(self._decompress(received_resp), 'html.parser')
            received_stats = {
                'count_sms': soup_received.select_one("#CountSMS").text.strip() if soup_received.select_one("#CountSMS") else '0',
                'paid_sms': soup_received.select_one("#PaidSMS").text.strip() if soup_received.select_one("#PaidSMS") else '0',
                'unpaid_sms': soup_received.select_one("#UnpaidSMS").text.strip() if soup_received.select_one("#UnpaidSMS") else '0',
                'revenue': soup_received.select_one("#RevenueSMS").text.strip().replace(' USD', '') if soup_received.select_one("#RevenueSMS") else '0.00'
            }
            # Fetch Live SMS
            live_resp = self.scraper.get(f"{BASE_URL}/portal/live/my_sms", timeout=15)
            soup_live = BeautifulSoup(self._decompress(live_resp), 'html.parser')
            live_stats = {
                'sms_today': soup_live.select_one("#CountSMS").text.strip() if soup_live.select_one("#CountSMS") else '0',
                'revenue': soup_live.select_one("#RevenueSMS").text.strip().replace(' USD', '') if soup_live.select_one("#RevenueSMS") else '0.00'
            }
            return {'received': received_stats, 'live': live_stats}
        except Exception as e:
            logger.error(f"Failed to fetch all data: {e}")
            self.logged_in = False # Assume session is bad
            return None

# ---------------------- Flask Application ----------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a-very-secret-key-for-dev')
client = IVASClient()

@app.before_first_request
def initial_login():
    client.login()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def api_status():
    return jsonify({'logged_in': client.logged_in})

@app.route('/api/all')
def api_all():
    data = client.fetch_all_data()
    if data:
        return jsonify(data)
    return jsonify({'error': 'Authentication with IVAS failed. Check logs.'}), 502

@app.route('/api/getsms', methods=['POST'])
def proxy_getsms():
    if not client.ensure_login():
        return Response('<p style="color:red;text-align:center;padding:2rem;">Error: Not logged into IVAS. Session may have expired. Please refresh the main dashboard.</p>', status=401)
    try:
        payload = {
            'from': request.form.get('from'),
            'to': request.form.get('to'),
            '_token': client.csrf_token
        }
        headers = {'X-Requested-With': 'XMLHttpRequest', 'Referer': f"{BASE_URL}/portal/sms/received"}
        resp = client.scraper.post(f"{BASE_URL}/portal/sms/received/getsms", data=payload, headers=headers, timeout=20)
        
        if resp.status_code != 200:
             return Response(f"Error from IVAS server: Status {resp.status_code}", status=502)

        html_content = client._decompress(resp)
        if "Account Login" in html_content:
            client.logged_in = False # Mark session as invalid
            return Response('<p style="color:red;text-align:center;padding:2rem;">Session expired. The application is attempting to reconnect. Please try again in a few moments.</p>', status=401)
        
        return Response(html_content, mimetype='text/html')
    except Exception as e:
        logger.error(f"Proxy request failed: {e}")
        return Response(f"An internal error occurred: {e}", status=500)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
