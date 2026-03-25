"""
IVAS SMS Dashboard – Final Version with Auto-Login
- Fixed decompression: only decompress if gzip magic bytes present
- Reliable login using cookies or credentials
- Proxy endpoint /api/getsms returns IVAS HTML
"""

import os, re, json, time, gzip, logging
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, session, Response
import cloudscraper
from requests.exceptions import ConnectionError, Timeout
from werkzeug.security import generate_password_hash, check_password_hash
import pyrebase

# ---------------------- Logging ----------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------------------- Constants ----------------------
BASE_URL      = "https://www.ivasms.com"
IVAS_EMAIL    = os.environ.get('IVAS_EMAIL',    'your_email@example.com') # CHANGE THIS or use environment variables
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'your_password') # CHANGE THIS or use environment variables
COOKIES_ENV   = os.environ.get('COOKIES_JSON',  '')

# ---------------------- Firebase Helpers (Optional) ----------------------
# Ensure firebase_config.py is present or comment out this section if not used
try:
    from firebase_config import firebase, db

    def init_firebase_data():
        try:
            admin_ref = db.child("admin")
            if admin_ref.get() is None:
                admin_ref.set({
                    "username": "redx",
                    "password": generate_password_hash("redx")
                })
                logger.info("Admin user created in Firebase.")
            else:
                logger.info("Admin user already exists in Firebase.")
        except Exception as e:
            logger.error(f"Firebase admin init failed: {e}")

        try:
            ann_ref = db.child("announcements")
            if ann_ref.get() is None:
                ann_ref.set({
                    "active": "Welcome to the IVAS OTP Dashboard!",
                    "history": []
                })
                logger.info("Announcement created.")
            else:
                logger.info("Announcement already exists.")
        except Exception as e:
            logger.error(f"Firebase announcement init failed: {e}")

    def get_announcement():
        try:
            ann = db.child("announcements").get().val()
            return ann.get("active", "") if ann else ""
        except Exception as e:
            logger.error(f"get_announcement failed: {e}")
            return ""

    def verify_admin(username, password):
        try:
            data = db.child("admin").get().val()
            if data and data.get("username") == username:
                stored_hash = data.get("password", "")
                if check_password_hash(stored_hash, password):
                    logger.info("Admin verified via Firebase.")
                    return True
        except Exception as e:
            logger.error(f"Firebase verify_admin failed: {e}")

        if username == "redx" and password == "redx":
            logger.warning("Using hardcoded admin credentials (fallback).")
            return True
        return False
except ImportError:
    logger.warning("firebase_config.py not found. Firebase features will be disabled.")
    db = None
    def init_firebase_data(): pass
    def get_announcement(): return "Firebase is not configured."
    def verify_admin(username, password):
        return username == "redx" and password == "redx"

# ---------------------- IVAS Client with robust login ----------------------
class IVASClient:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(browser={'browser':'chrome','platform':'windows','mobile':False})
        self.base_url = BASE_URL
        self.logged_in = False
        self.csrf_token = None
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })

    def _decompress(self, response):
        content = response.content
        try:
            if content.startswith(b'\x1f\x8b'):
                content = gzip.decompress(content)
            return content.decode('utf-8', errors='replace')
        except Exception as e:
            logger.warning(f"Decompression failed: {e}")
            return response.text

    def _load_cookies(self):
        raw = COOKIES_ENV.strip()
        if not raw:
            p = os.path.join(os.path.dirname(__file__), 'cookies.json')
            if os.path.exists(p):
                with open(p) as f:
                    raw = f.read().strip()
        if not raw: return {}
        try:
            d = json.loads(raw)
            if isinstance(d, list): return {c['name']: c['value'] for c in d if 'name' in c}
            if isinstance(d, dict): return d
        except Exception as e: logger.error(f"Cookie parse: {e}")
        return {}

    def login(self):
        """Auto login using cookies, fallback to credentials."""
        cookies = self._load_cookies()
        if cookies:
            for name, value in cookies.items():
                self.scraper.cookies.set(name, value, domain='www.ivasms.com')
            logger.info(f"Injected {len(cookies)} cookies. Verifying session...")
            if self._verify():
                return True
            logger.warning("Cookies are stale or invalid. Falling back to credentials.")

        logger.info("Attempting credential login...")
        try:
            login_page_resp = self.scraper.get(f"{BASE_URL}/login", timeout=15)
            if login_page_resp.status_code != 200:
                logger.error(f"Failed to fetch login page. Status: {login_page_resp.status_code}")
                return False

            soup = BeautifulSoup(self._decompress(login_page_resp), 'html.parser')
            token_input = soup.find('input', {'name': '_token'})
            if not token_input:
                logger.error("Could not find CSRF token on login page.")
                return False
            
            login_data = {
                '_token': token_input['value'],
                'email': IVAS_EMAIL,
                'password': IVAS_PASSWORD,
                'remember': '1'
            }
            
            login_post_resp = self.scraper.post(f"{BASE_URL}/login", data=login_data, allow_redirects=True, timeout=15)
            if login_post_resp.status_code == 200:
                return self._verify()
            else:
                logger.error(f"Login POST request failed. Status: {login_post_resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"An exception occurred during credential login: {e}")
            return False

    def _verify(self):
        """Check if session is valid by accessing a protected page."""
        try:
            resp = self.scraper.get(f"{BASE_URL}/portal/sms/received", timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Verification failed: Received status {resp.status_code}.")
                return False
            
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')
            
            if "Account Login" in html or "Forgot Password" in html:
                logger.warning("Verification failed: Redirected to login page.")
                return False
            
            token_input = soup.find('input', {'name': '_token'})
            if token_input:
                self.csrf_token = token_input['value']
                self.logged_in = True
                logger.info("✅ Session verified successfully. CSRF token captured.")
                return True
            else:
                logger.warning("Verification failed: No CSRF token found on protected page.")
                return False
        except Exception as e:
            logger.error(f"An exception occurred during verification: {e}")
            return False

    def ensure_login(self):
        if self.logged_in and self.csrf_token:
            return True
        return self.login()

    def check_otps(self, from_date="", to_date=""):
        if not self.ensure_login(): return None
        # ... (rest of the methods are the same as your provided file)
        try:
            payload = { 'from': from_date, 'to': to_date, '_token': self.csrf_token }
            headers = { 'Accept': 'text/html, */*; q=0.01', 'X-Requested-With': 'XMLHttpRequest', 'Origin': self.base_url, 'Referer': f"{self.base_url}/portal/sms/received" }
            resp = self.scraper.post(f"{self.base_url}/portal/sms/received/getsms", data=payload, headers=headers, timeout=10)
            if resp.status_code != 200: return None
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')
            def _t(sel):
                el = soup.select_one(sel)
                return el.get_text(strip=True).replace(' USD', '') if el else '0'
            details = []
            for item in soup.select('div.item'):
                rng = item.select_one('.col-sm-4')
                cols = item.select('.col-3')
                if not rng: continue
                def _p(el):
                    if not el: return '0'
                    p = el.select_one('p')
                    return p.get_text(strip=True) if p else el.get_text(strip=True)
                rev_el = (item.select_one('.col-3:nth-child(5) p span.currency_cdr') or item.select_one('.col-3:last-child p span'))
                details.append({
                    'range': rng.get_text(strip=True),
                    'count': _p(cols[0]) if cols else '0',
                    'paid': _p(cols[1]) if len(cols)>1 else '0',
                    'unpaid': _p(cols[2]) if len(cols)>2 else '0',
                    'revenue': rev_el.get_text(strip=True) if rev_el else '0'
                })
            return { 'count_sms': _t('#CountSMS'), 'paid_sms': _t('#PaidSMS'), 'unpaid_sms': _t('#UnpaidSMS'), 'revenue': _t('#RevenueSMS'), 'sms_details': details, '_raw': html }
        except Exception as e:
            logger.error(f"check_otps: {e}")
            return None
            
    def fetch_live_sms(self):
        if not self.ensure_login(): return None
        try:
            resp = self.scraper.get(f"{self.base_url}/portal/live/my_sms", timeout=10)
            if resp.status_code != 200: return None
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')
            def _t(sid):
                el = soup.find(id=sid)
                return el.get_text(strip=True).replace(' USD', '').replace(',', '') if el else '0'
            stats = { 'total': _t('CountSMS'), 'paid': _t('PaidSMS'), 'unpaid': _t('UnpaidSMS'), 'revenue': _t('RevenueSMS') }
            sid_rows = []
            for table in soup.find_all('table'):
                for row in table.find_all('tr'):
                    cells = row.find_all('td')
                    if len(cells) >= 4:
                        sid_rows.append({ 'sid': cells[0].get_text(strip=True), 'paid': cells[1].get_text(strip=True), 'limit': cells[2].get_text(strip=True), 'message': cells[3].get_text(strip=True) })
            return { 'stats': stats, 'sms_today': stats['total'], 'sid_rows': sid_rows }
        except Exception as e:
            logger.error(f"fetch_live_sms: {e}")
            return None


# ---------------------- Flask App ----------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
client = IVASClient()

@app.before_first_request
def initial_login():
    logger.info("Attempting initial login on app startup...")
    if client.login():
        logger.info("🚀 Initial login successful.")
    else:
        logger.error("⚠️ Initial login FAILED. The app may not function correctly. Please check cookies or credentials.")

# ---------------------- Proxy endpoint for IVAS getsms ----------------------
@app.route('/api/getsms', methods=['POST'])
def proxy_getsms():
    if not client.ensure_login():
        return Response('<div class="sms-empty"><p style="color:red;">Authentication with IVAS failed. Please update cookies.json or credentials.</p></div>', status=401)

    from_date = request.form.get('from')
    to_date = request.form.get('to')
    payload = {'from': from_date, 'to': to_date, '_token': client.csrf_token}
    headers = {'Accept': 'text/html, */*; q=0.01', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest', 'Origin': BASE_URL, 'Referer': f"{BASE_URL}/portal/sms/received"}

    try:
        resp = client.scraper.post(f"{BASE_URL}/portal/sms/received/getsms", data=payload, headers=headers, timeout=15)
        if resp.status_code != 200:
            return Response(f"Error from IVAS: {resp.status_code}", status=500)
        
        html = client._decompress(resp)
        if "Account Login" in html:
            logger.warning("Proxy received login page, session is likely invalid.")
            client.logged_in = False # Mark as logged out
            return Response('<div class="sms-empty"><p style="color:red;">Your session expired. The app is trying to re-login. Please try again in a moment.</p></div>', status=401)
            
        return Response(html, mimetype='text/html')
    except Exception as e:
        logger.error(f"Proxy getsms error: {e}")
        return Response(f"Proxy error: {e}", status=500)

# ---------------------- Dashboard Routes ----------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def api_status():
    # Actively check login status before reporting
    is_logged_in = client.logged_in
    if not is_logged_in:
        is_logged_in = client.login() # Attempt to re-login if needed
    return jsonify({'logged_in': is_logged_in})

@app.route('/api/live')
def api_live():
    data = client.fetch_live_sms()
    if data is None:
        return jsonify({'error': 'Failed to fetch live SMS data. Check authentication.'}), 500
    return jsonify(data)

@app.route('/api/all')
def api_all():
    today = datetime.now().strftime('%Y-%m-%d')
    # Use the same date format as the datepicker
    received = client.check_otps(today, today) 
    live = client.fetch_live_sms()
    
    if received is None or live is None:
        return jsonify({'error': 'Failed to fetch data, login may be required.'}), 500
        
    received.pop('_raw', None)
    return jsonify({'received': received, 'live': live, 'ts': datetime.utcnow().isoformat()})

# ... (Add other admin routes from your file if you use them) ...

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
