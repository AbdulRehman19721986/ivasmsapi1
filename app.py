"""
IVAS SMS Dashboard – Final Version
- Fixed decompression: only decompress if content starts with gzip magic bytes
- Proxy endpoint /api/getsms returns the exact HTML from IVAS
- Admin panel, Firebase, custom numbers, live SMS, etc.
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
IVAS_EMAIL    = os.environ.get('IVAS_EMAIL',    'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')
COOKIES_ENV   = os.environ.get('COOKIES_JSON',  '')

# ---------------------- Firebase Helpers ----------------------
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

    try:
        numbers_ref = db.child("custom_numbers")
        if numbers_ref.get() is None:
            numbers_ref.set([])
            logger.info("Custom numbers collection initialized.")
        else:
            logger.info("Custom numbers collection already exists.")
    except Exception as e:
        logger.error(f"Firebase custom_numbers init failed: {e}")

def get_announcement():
    try:
        ann = db.child("announcements").get().val()
        return ann.get("active", "") if ann else ""
    except Exception as e:
        logger.error(f"get_announcement failed: {e}")
        return ""

def update_announcement(new_msg):
    try:
        ann_ref = db.child("announcements")
        current = ann_ref.get().val() or {}
        history = current.get("history", [])
        if current.get("active"):
            history.append({"text": current["active"], "timestamp": datetime.utcnow().isoformat()})
        ann_ref.update({
            "active": new_msg,
            "history": history[-50:]
        })
        logger.info("Announcement updated.")
    except Exception as e:
        logger.error(f"update_announcement failed: {e}")

def change_admin_password(new_password):
    try:
        db.child("admin").update({"password": generate_password_hash(new_password)})
        logger.info("Admin password updated.")
    except Exception as e:
        logger.error(f"change_admin_password failed: {e}")

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

# ---------------------- Number Management ----------------------
def get_custom_numbers():
    try:
        numbers = db.child("custom_numbers").get().val()
        return numbers if numbers else []
    except Exception as e:
        logger.error(f"get_custom_numbers failed: {e}")
        return []

def add_custom_number(number_data):
    try:
        numbers_ref = db.child("custom_numbers")
        current = numbers_ref.get().val() or []
        if any(n.get('number') == number_data.get('number') for n in current):
            return False
        number_data['added_at'] = datetime.utcnow().isoformat()
        current.append(number_data)
        numbers_ref.set(current)
        logger.info(f"Added custom number: {number_data['number']}")
        return True
    except Exception as e:
        logger.error(f"add_custom_number failed: {e}")
        return False

def remove_custom_number(number):
    try:
        numbers_ref = db.child("custom_numbers")
        current = numbers_ref.get().val() or []
        filtered = [n for n in current if n.get('number') != number]
        if len(filtered) == len(current):
            return False
        numbers_ref.set(filtered)
        logger.info(f"Removed custom number: {number}")
        return True
    except Exception as e:
        logger.error(f"remove_custom_number failed: {e}")
        return False

# ---------------------- IVAS Client ----------------------
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
            'Accept-Encoding': 'gzip, deflate',  # no brotli
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })

    def _decompress(self, response):
        """Only decompress if content is actually gzipped (magic bytes)."""
        encoding = response.headers.get('Content-Encoding', '').lower()
        content = response.content
        try:
            if encoding == 'gzip' and content.startswith(b'\x1f\x8b'):
                content = gzip.decompress(content)
            # brotli not expected
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
        if not raw:
            return {}
        try:
            d = json.loads(raw)
            if isinstance(d, list):
                return {c['name']: c['value'] for c in d if 'name' in c}
            if isinstance(d, dict):
                return d
        except Exception as e:
            logger.error(f"Cookie parse: {e}")
        return {}

    def login(self):
        cookies = self._load_cookies()
        if cookies:
            for name, value in cookies.items():
                self.scraper.cookies.set(name, value, domain='www.ivasms.com')
            logger.info(f"Injected {len(cookies)} cookies")
            if self._verify():
                return True
            logger.warning("Cookies stale – trying credentials")

        logger.info("Attempting credential login...")
        try:
            r1 = self.scraper.get(f"{BASE_URL}/login", timeout=10)
            if r1.status_code != 200:
                return False
            html = self._decompress(r1)
            soup = BeautifulSoup(html, 'html.parser')
            token_input = soup.find('input', {'name': '_token'})
            if not token_input:
                logger.error("No CSRF token on login page")
                return False
            token = token_input['value']
            data = {
                '_token': token,
                'email': IVAS_EMAIL,
                'password': IVAS_PASSWORD,
                'remember': '1'
            }
            r2 = self.scraper.post(f"{BASE_URL}/login", data=data, allow_redirects=True, timeout=10)
            if r2.status_code == 200:
                return self._verify()
        except Exception as e:
            logger.error(f"Credential login error: {e}")
        return False

    def _verify(self):
        try:
            resp = self.scraper.get(f"{BASE_URL}/portal/sms/received", timeout=10)
            if resp.status_code == 200:
                html = self._decompress(resp)
                soup = BeautifulSoup(html, 'html.parser')
                token_input = soup.find('input', {'name': '_token'})
                if token_input:
                    self.csrf_token = token_input['value']
                    self.logged_in = True
                    logger.info("✅ Session OK")
                    return True
                logger.warning("No _token found")
        except Exception as e:
            logger.error(f"_verify error: {e}")
        return False

    def ensure_login(self):
        if self.logged_in and self.csrf_token:
            return True
        return self.login()

    # ---------- Original API methods (preserved) ----------
    def check_otps(self, from_date="", to_date=""):
        if not self.ensure_login():
            return None
        try:
            payload = {
                'from': from_date,
                'to': to_date,
                '_token': self.csrf_token
            }
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            resp = self.scraper.post(f"{self.base_url}/portal/sms/received/getsms",
                                     data=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None
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
                rev_el = (item.select_one('.col-3:nth-child(5) p span.currency_cdr') or
                          item.select_one('.col-3:last-child p span'))
                details.append({
                    'range': rng.get_text(strip=True),
                    'count': _p(cols[0]) if cols else '0',
                    'paid': _p(cols[1]) if len(cols)>1 else '0',
                    'unpaid': _p(cols[2]) if len(cols)>2 else '0',
                    'revenue': rev_el.get_text(strip=True) if rev_el else '0'
                })
            result = {
                'count_sms': _t('#CountSMS'),
                'paid_sms': _t('#PaidSMS'),
                'unpaid_sms': _t('#UnpaidSMS'),
                'revenue': _t('#RevenueSMS'),
                'sms_details': details,
                '_raw': html
            }
            return result
        except Exception as e:
            logger.error(f"check_otps: {e}")
            return None

    def get_sms_details(self, phone_range, from_date="", to_date=""):
        if not self.ensure_login():
            return []
        try:
            payload = {
                '_token': self.csrf_token,
                'start': from_date,
                'end': to_date,
                'range': phone_range
            }
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            resp = self.scraper.post(f"{self.base_url}/portal/sms/received/getsms/number",
                                     data=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                return []
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')
            out = []
            for item in soup.select('div.card.card-body'):
                ph = item.select_one('.col-sm-4')
                cols = item.select('.col-3')
                if not ph: continue
                onclick = ph.get('onclick', '')
                id_num = onclick.split("'")[3] if "'" in onclick and len(onclick.split("'"))>3 else ''
                def _p(el):
                    if not el: return '0'
                    p = el.select_one('p')
                    return p.get_text(strip=True) if p else '0'
                rev_el = (item.select_one('.col-3:nth-child(5) p span.currency_cdr') or
                          item.select_one('.col-3:last-child p span'))
                out.append({
                    'phone_number': ph.get_text(strip=True),
                    'count': _p(cols[0]) if cols else '0',
                    'paid': _p(cols[1]) if len(cols)>1 else '0',
                    'unpaid': _p(cols[2]) if len(cols)>2 else '0',
                    'revenue': rev_el.get_text(strip=True) if rev_el else '0',
                    'id_number': id_num
                })
            return out
        except Exception as e:
            logger.error(f"get_sms_details: {e}")
            return []

    def get_otp_message(self, phone_number, phone_range, from_date="", to_date=""):
        if not self.ensure_login():
            return None
        try:
            payload = {
                '_token': self.csrf_token,
                'start': from_date,
                'end': to_date,
                'Number': phone_number,
                'Range': phone_range
            }
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            resp = self.scraper.post(f"{self.base_url}/portal/sms/received/getsms/number/sms",
                                     data=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')
            for sel in ['.col-9.col-sm-6 p','.message-text','.sms-body','.col-9 p','p']:
                el = soup.select_one(sel)
                if el:
                    t = el.get_text(strip=True)
                    if t:
                        return t
            return None
        except Exception as e:
            logger.error(f"get_otp_message: {e}")
            return None

    def get_all_otp_messages(self, sms_details, from_date="", to_date="", limit=None):
        if not sms_details:
            return []
        all_otps = []
        for d in sms_details:
            rng = d['range']
            numbers = self.get_sms_details(rng, from_date, to_date)
            for nd in numbers:
                if limit and len(all_otps) >= limit:
                    break
                msg = self.get_otp_message(nd['phone_number'], rng, from_date, to_date)
                all_otps.append({
                    'range': rng,
                    'phone_number': nd['phone_number'],
                    'otp_message': msg or '',
                    'count': nd['count'],
                    'paid': nd['paid'],
                    'revenue': nd['revenue']
                })
            if limit and len(all_otps) >= limit:
                break
        return all_otps

    # ---------- Additional methods for dashboard ----------
    def fetch_numbers(self):
        if not self.ensure_login():
            return None
        try:
            resp = self.scraper.get(f"{BASE_URL}/portal/numbers", timeout=10)
            if resp.status_code != 200:
                return None
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')
            out = []
            for row in soup.select('table tbody tr'):
                cells = [c.get_text(strip=True) for c in row.find_all('td')]
                if cells and re.match(r'^\+?\d{7,}$', cells[0]):
                    out.append({
                        'number': cells[0],
                        'range_name': cells[1] if len(cells)>1 else '',
                        'rate': cells[2] if len(cells)>2 else '',
                        'limit': cells[3] if len(cells)>3 else ''
                    })
            if not out:
                seen = set()
                for m in re.finditer(r'\b(\d{10,})\b', html):
                    n = m.group(1)
                    if n not in seen:
                        seen.add(n)
                        out.append({'number': n, 'range_name': '', 'rate': '', 'limit': ''})
            return out
        except Exception as e:
            logger.error(f"fetch_numbers: {e}")
            return None

    def fetch_live_sms(self):
        if not self.ensure_login():
            return None
        try:
            resp = self.scraper.get(f"{BASE_URL}/portal/live/my_sms", timeout=10)
            if resp.status_code != 200:
                return None
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')
            def _t(sid):
                el = soup.find(id=sid)
                return el.get_text(strip=True).replace(' USD', '').replace(',', '') if el else '0'
            stats = {
                'total': _t('CountSMS'),
                'paid': _t('PaidSMS'),
                'unpaid': _t('UnpaidSMS'),
                'revenue': _t('RevenueSMS')
            }
            nums = set()
            for m in re.finditer(r'\b(\d{10,})\b', html):
                nums.add(m.group(1))
            sid_rows = []
            for table in soup.find_all('table'):
                for row in table.find_all('tr'):
                    cells = row.find_all('td')
                    if len(cells) >= 4:
                        sid_rows.append({
                            'sid': cells[0].get_text(strip=True),
                            'paid': cells[1].get_text(strip=True),
                            'limit': cells[2].get_text(strip=True),
                            'message': cells[3].get_text(strip=True)
                        })
            return {
                'stats': stats,
                'sms_today': stats['total'],
                'numbers': list(nums)[:200],
                'sid_rows': sid_rows
            }
        except Exception as e:
            logger.error(f"fetch_live_sms: {e}")
            return None

# ---------------------- Flask App ----------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
client = IVASClient()

init_firebase_data()

logger.info("Boot login…")
if client.login():
    logger.info("🚀 Logged in OK")
else:
    logger.error("⚠️  Login FAILED — check credentials and network")

# ---------------------- Original Arslan-MD API Endpoint ----------------------
@app.route('/sms')
def get_sms():
    date_str = request.args.get('date')
    limit = request.args.get('limit')
    to_date = request.args.get('to_date', '')

    if not date_str:
        return jsonify({'error': 'Date parameter is required in DD/MM/YYYY format'}), 400

    try:
        datetime.strptime(date_str, '%d/%m/%Y')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use DD/MM/YYYY'}), 400

    if limit:
        try:
            limit = int(limit)
            if limit <= 0:
                return jsonify({'error': 'Limit must be a positive integer'}), 400
        except ValueError:
            return jsonify({'error': 'Limit must be a valid integer'}), 400
    else:
        limit = None

    if not client.logged_in:
        return jsonify({'error': 'Client not authenticated'}), 401

    result = client.check_otps(from_date=date_str, to_date=to_date)
    if not result:
        return jsonify({'error': 'Failed to fetch OTP data'}), 500

    otp_messages = client.get_all_otp_messages(result.get('sms_details', []),
                                               from_date=date_str,
                                               to_date=to_date,
                                               limit=limit)

    return jsonify({
        'status': 'success',
        'from_date': date_str,
        'to_date': to_date or 'Not specified',
        'limit': limit if limit is not None else 'Not specified',
        'sms_stats': {
            'count_sms': result['count_sms'],
            'paid_sms': result['paid_sms'],
            'unpaid_sms': result['unpaid_sms'],
            'revenue': result['revenue']
        },
        'otp_messages': otp_messages
    })

# ---------------------- Proxy endpoint for IVAS getsms ----------------------
@app.route('/api/getsms', methods=['POST'])
def proxy_getsms():
    """Forward the POST request to IVAS and return the raw HTML response."""
    if not client.ensure_login():
        return jsonify({'error': 'Not authenticated'}), 401

    from_date = request.form.get('from')
    to_date = request.form.get('to')

    payload = {
        'from': from_date,
        'to': to_date,
        '_token': client.csrf_token
    }

    headers = {
        'Accept': 'text/html, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': BASE_URL,
        'Referer': f"{BASE_URL}/portal/sms/received"
    }

    try:
        resp = client.scraper.post(
            f"{BASE_URL}/portal/sms/received/getsms",
            data=payload,
            headers=headers,
            timeout=10
        )
        if resp.status_code != 200:
            return Response(f"Error from IVAS: {resp.status_code}", status=500)
        html = client._decompress(resp)
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
    return jsonify({'logged_in': client.logged_in, 'ts': datetime.utcnow().isoformat()})

@app.route('/api/numbers')
def api_numbers():
    ivas_numbers = client.fetch_numbers() or []
    custom_numbers = get_custom_numbers()
    existing = {n['number'] for n in custom_numbers}
    merged = custom_numbers + [n for n in ivas_numbers if n['number'] not in existing]
    return jsonify({'numbers': merged, 'count': len(merged)})

@app.route('/api/received')
def api_received():
    d = client.check_otps(request.args.get('from',''), request.args.get('to',''))
    if d is None:
        return jsonify({'error': 'fetch failed'}), 500
    d.pop('_raw', None)
    return jsonify(d)

@app.route('/api/otps')
def api_otps():
    from_date = request.args.get('from', '')
    to_date = request.args.get('to', '')
    limit = int(request.args.get('limit', 50))
    stats = client.check_otps(from_date, to_date)
    if not stats:
        return jsonify({'error': 'fetch failed'}), 500
    otps = client.get_all_otp_messages(stats.get('sms_details', []), from_date, to_date, limit)
    stats.pop('_raw', None)
    return jsonify({'stats': stats, 'otps': otps, 'count': len(otps)})

@app.route('/api/live')
def api_live():
    d = client.fetch_live_sms()
    if d is None:
        return jsonify({'error': 'fetch failed'}), 500
    return jsonify(d)

@app.route('/api/all')
def api_all():
    today = datetime.now().strftime('%Y-%m-%d')
    numbers = client.fetch_numbers()
    received = client.check_otps(today, today)
    live = client.fetch_live_sms()
    errors = [k for k, v in [('numbers', numbers), ('received', received), ('live', live)] if v is None]
    if errors:
        return jsonify({'error': f"Failed: {', '.join(errors)}"}), 500
    if received:
        received.pop('_raw', None)
    return jsonify({'numbers': numbers, 'received': received, 'live': live, 'ts': datetime.utcnow().isoformat()})

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    client.logged_in = False
    client.csrf_token = None
    return jsonify({'success': client.login()})

@app.route('/api/announcements')
def api_announcements():
    return jsonify({'message': get_announcement()})

# Admin APIs
@app.route('/admin/login', methods=['POST'])
def admin_login():
    data = request.json
    if verify_admin(data.get('username'), data.get('password')):
        session['admin_logged_in'] = True
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_logged_in', None)
    return jsonify({'success': True})

@app.route('/admin/change-password', methods=['POST'])
def admin_change_password():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    new_pass = request.json.get('new_password')
    if not new_pass:
        return jsonify({'error': 'Missing password'}), 400
    change_admin_password(new_pass)
    return jsonify({'success': True})

@app.route('/admin/update-announcement', methods=['POST'])
def admin_update_announcement():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    new_msg = request.json.get('message')
    if new_msg is None:
        return jsonify({'error': 'Missing message'}), 400
    update_announcement(new_msg)
    return jsonify({'success': True})

@app.route('/admin/numbers', methods=['GET'])
def admin_list_numbers():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'numbers': get_custom_numbers()})

@app.route('/admin/numbers', methods=['POST'])
def admin_add_number():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    number = data.get('number')
    if not number:
        return jsonify({'error': 'Number is required'}), 400
    number = str(number).strip()
    if not re.match(r'^\+?\d{7,}$', number):
        return jsonify({'error': 'Invalid number format'}), 400
    number_data = {
        'number': number,
        'range_name': data.get('range_name', 'Custom'),
        'rate': data.get('rate', ''),
        'limit': data.get('limit', '')
    }
    if add_custom_number(number_data):
        return jsonify({'success': True})
    return jsonify({'error': 'Number already exists'}), 400

@app.route('/admin/numbers/<number>', methods=['DELETE'])
def admin_remove_number(number):
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if remove_custom_number(number):
        return jsonify({'success': True})
    return jsonify({'error': 'Number not found'}), 404

# Debug endpoints
@app.route('/debug/raw/<path:p>')
def debug_raw(p):
    if not client.ensure_login():
        return "not logged in", 401
    r = client.scraper.get(f"{BASE_URL}/{p}")
    return client._decompress(r) if r else "no response", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
