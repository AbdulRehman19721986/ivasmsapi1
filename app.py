"""
IVAS SMS Dashboard - Professional Edition
Author: Abdul Rehman Rajpoot
Fixed: Cookie-based auth + credential login fallback + full retry logic
"""

import os
import re
import json
import time
import logging
import cloudscraper
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template
from requests.exceptions import ConnectionError, Timeout, RequestException

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
IVAS_EMAIL    = os.environ.get('IVAS_EMAIL', 'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')
BASE_URL      = "https://www.ivasms.com"

# Optional: paste fresh cookies as a JSON string in this env var
# e.g. IVAS_COOKIES='[{"name":"ivas_sms_session","value":"...","domain":"www.ivasms.com"},...]'
IVAS_COOKIES_ENV = os.environ.get('IVAS_COOKIES', '')

MAX_RETRIES   = 3
RETRY_DELAY   = 2
REQUEST_TO    = 20   # seconds

# ─── IVAS Client ─────────────────────────────────────────────────────────────
class IVASClient:
    def __init__(self):
        self.scraper   = self._make_scraper()
        self.logged_in = False
        self.csrf      = None
        self._inject_cookies_from_env()

    # ── Scraper factory ──────────────────────────────────────────────────────
    def _make_scraper(self):
        s = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        s.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/122.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
        })
        return s

    # ── Cookie helpers ────────────────────────────────────────────────────────
    def _inject_cookies_from_env(self):
        """Load cookies from IVAS_COOKIES env variable (JSON list)."""
        raw = IVAS_COOKIES_ENV.strip()
        if not raw:
            # Try loading from cookies.json file in the same directory
            cookie_path = os.path.join(os.path.dirname(__file__), 'cookies.json')
            if os.path.exists(cookie_path):
                try:
                    with open(cookie_path) as f:
                        raw = f.read().strip()
                except Exception:
                    pass
        if not raw:
            return
        try:
            cookies = json.loads(raw)
            for c in cookies:
                self.scraper.cookies.set(
                    c['name'], c['value'],
                    domain=c.get('domain', 'www.ivasms.com'),
                    path=c.get('path', '/')
                )
            logger.info(f"Injected {len(cookies)} cookies from storage.")
        except Exception as e:
            logger.warning(f"Failed to parse cookies: {e}")

    # ── HTTP helper ───────────────────────────────────────────────────────────
    def _req(self, method, url, **kwargs):
        kwargs.setdefault('timeout', REQUEST_TO)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.scraper.request(method, url, **kwargs)
                logger.debug(f"{method.upper()} {url} → {resp.status_code}")
                return resp
            except (ConnectionError, Timeout) as e:
                logger.warning(f"[{attempt}/{MAX_RETRIES}] {method} {url} failed: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    raise
            except RequestException as e:
                logger.error(f"Request exception: {e}")
                raise
        return None

    # ── Login check ───────────────────────────────────────────────────────────
    def _is_portal_page(self, html: str) -> bool:
        """Return True if the HTML looks like an authenticated portal page."""
        markers = [
            r'/logout',
            r'Account Code',
            r'Dashboard',
            r'portal/numbers',
            r'portal/live',
            r'IVAS SMS',
        ]
        for m in markers:
            if re.search(m, html, re.IGNORECASE):
                return True
        return False

    def _extract_csrf(self, html: str) -> str | None:
        soup = BeautifulSoup(html, 'html.parser')
        el = soup.find('input', {'name': '_token'})
        if el:
            return el.get('value')
        # Also look in meta tags
        meta = soup.find('meta', {'name': 'csrf-token'})
        if meta:
            return meta.get('content')
        return None

    # ── Cookie-based session validation ──────────────────────────────────────
    def _try_cookie_auth(self) -> bool:
        """Try to access portal using existing cookies."""
        try:
            resp = self._req('GET', f"{BASE_URL}/portal", allow_redirects=True)
            if resp and resp.status_code == 200 and self._is_portal_page(resp.text):
                self.csrf = self._extract_csrf(resp.text)
                self.logged_in = True
                logger.info("✅ Cookie auth succeeded.")
                return True
        except Exception as e:
            logger.warning(f"Cookie auth failed: {e}")
        return False

    # ── Credential login ──────────────────────────────────────────────────────
    def _credential_login(self) -> bool:
        """Full form-based login."""
        logger.info("🔑 Attempting credential login...")
        try:
            # 1. GET login page → extract CSRF
            resp = self._req('GET', f"{BASE_URL}/login")
            if not resp or resp.status_code != 200:
                logger.error(f"Login page unreachable: {resp.status_code if resp else 'no response'}")
                return False

            csrf = self._extract_csrf(resp.text)
            if not csrf:
                logger.error("No CSRF token on login page.")
                logger.info(f"Login page snippet: {resp.text[:300]}")
                return False

            logger.info(f"Got CSRF token: {csrf[:20]}...")

            # 2. POST credentials
            payload = {
                '_token':   csrf,
                'email':    IVAS_EMAIL,
                'password': IVAS_PASSWORD,
                'remember': '1',
            }
            post_resp = self._req(
                'POST', f"{BASE_URL}/login",
                data=payload,
                allow_redirects=False,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': BASE_URL,
                    'Referer': f"{BASE_URL}/login",
                }
            )

            if not post_resp:
                logger.error("No response from login POST")
                return False

            logger.info(f"Login POST → {post_resp.status_code}, Location: {post_resp.headers.get('Location', 'none')}")

            # 3. Follow redirect(s)
            target_url = None
            if post_resp.status_code in (301, 302):
                loc = post_resp.headers.get('Location', '')
                target_url = loc if loc.startswith('http') else BASE_URL + loc
            elif post_resp.status_code == 200:
                # Some setups return 200 with dashboard HTML directly
                if self._is_portal_page(post_resp.text):
                    self.csrf = self._extract_csrf(post_resp.text)
                    self.logged_in = True
                    logger.info("✅ Credential login successful (200 with portal).")
                    return True
                # May have redirected internally; try portal directly
                target_url = f"{BASE_URL}/portal"
            else:
                logger.error(f"Unexpected login POST status: {post_resp.status_code}")
                logger.info(f"Response snippet: {post_resp.text[:400]}")
                return False

            if target_url:
                follow = self._req('GET', target_url, allow_redirects=True)
                if follow and follow.status_code == 200 and self._is_portal_page(follow.text):
                    self.csrf = self._extract_csrf(follow.text)
                    self.logged_in = True
                    logger.info(f"✅ Credential login successful (redirect to {target_url}).")
                    return True
                else:
                    status = follow.status_code if follow else 'no response'
                    logger.error(f"Redirect page not portal. Status: {status}")
                    if follow:
                        logger.info(f"Page snippet: {follow.text[:400]}")

        except Exception as e:
            logger.error(f"Credential login exception: {e}", exc_info=True)

        return False

    # ── Public login ──────────────────────────────────────────────────────────
    def login(self) -> bool:
        """Try cookie auth first, then credential login."""
        if self._try_cookie_auth():
            return True
        return self._credential_login()

    def ensure_login(self) -> bool:
        """Ensure we have an active session."""
        if self.logged_in:
            # Lightweight session check
            try:
                r = self._req('GET', f"{BASE_URL}/portal", allow_redirects=True)
                if r and r.status_code == 200 and self._is_portal_page(r.text):
                    self.csrf = self._extract_csrf(r.text)
                    return True
            except Exception:
                pass
            logger.warning("Session appears expired. Re-logging in...")
            self.logged_in = False
        return self.login()

    # ── Fetch Numbers ─────────────────────────────────────────────────────────
    def fetch_numbers(self):
        if not self.ensure_login():
            return None
        try:
            resp = self._req('GET', f"{BASE_URL}/portal/numbers")
            if not resp or resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            numbers = []

            # Strategy 1: table rows
            for row in soup.select("table tbody tr"):
                cells = row.find_all("td")
                if cells:
                    num = cells[0].get_text(strip=True)
                    if re.match(r'^\+?\d{7,}$', num):
                        numbers.append({
                            'number': num,
                            'details': [c.get_text(strip=True) for c in cells[1:]]
                        })

            # Strategy 2: any phone-like text
            if not numbers:
                for m in re.finditer(r'(\+\d{7,})', resp.text):
                    numbers.append({'number': m.group(1), 'details': []})

            # Deduplicate
            seen = set()
            unique = []
            for n in numbers:
                if n['number'] not in seen:
                    seen.add(n['number'])
                    unique.append(n)

            logger.info(f"Numbers found: {len(unique)}")
            return unique
        except Exception as e:
            logger.error(f"fetch_numbers error: {e}")
            return None

    # ── Fetch Received SMS ────────────────────────────────────────────────────
    def fetch_received_sms(self, from_date=None, to_date=None):
        if not self.ensure_login():
            return None
        try:
            # First GET the received page to obtain fresh CSRF
            page = self._req('GET', f"{BASE_URL}/portal/sms/received")
            if not page or page.status_code != 200:
                return None

            csrf = self._extract_csrf(page.text) or self.csrf or ''
            payload = {'_token': csrf}
            if from_date:
                payload['from'] = from_date
            if to_date:
                payload['to'] = to_date

            resp = self._req(
                'POST', f"{BASE_URL}/portal/sms/received/getsms",
                data=payload,
                headers={
                    'Accept': 'text/html, */*; q=0.01',
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': f"{BASE_URL}/portal/sms/received",
                    'Origin': BASE_URL,
                }
            )
            if not resp or resp.status_code != 200:
                # Fallback: parse GET page
                resp = page

            soup = BeautifulSoup(resp.text, 'html.parser')

            def _txt(sel):
                el = soup.select_one(sel)
                return el.get_text(strip=True).replace(' USD', '') if el else '0'

            stats = {
                'count':   _txt('#CountSMS'),
                'paid':    _txt('#PaidSMS'),
                'unpaid':  _txt('#UnpaidSMS'),
                'revenue': _txt('#RevenueSMS'),
            }

            messages = self._parse_sms_table(soup)
            logger.info(f"Received SMS: {len(messages)}")
            return {'stats': stats, 'messages': messages}
        except Exception as e:
            logger.error(f"fetch_received_sms error: {e}")
            return None

    # ── Fetch Live SMS ────────────────────────────────────────────────────────
    def fetch_live_sms(self):
        if not self.ensure_login():
            return None
        try:
            resp = self._req('GET', f"{BASE_URL}/portal/live/my_sms")
            if not resp or resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')

            def _txt(sid):
                el = soup.find(id=sid)
                return el.get_text(strip=True).replace(' USD', '').replace(',', '') if el else '0'

            stats = {
                'total':   _txt('CountSMS'),
                'paid':    _txt('PaidSMS'),
                'unpaid':  _txt('UnpaidSMS'),
                'revenue': _txt('RevenueSMS'),
            }
            messages = self._parse_sms_table(soup)
            logger.info(f"Live SMS: {len(messages)}")
            return {'stats': stats, 'messages': messages}
        except Exception as e:
            logger.error(f"fetch_live_sms error: {e}")
            return None

    # ── Fetch Dashboard Stats ─────────────────────────────────────────────────
    def fetch_dashboard_stats(self):
        if not self.ensure_login():
            return None
        try:
            resp = self._req('GET', f"{BASE_URL}/portal")
            if not resp or resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')

            # Extract top apps
            apps = []
            for card in soup.select('.app-card, .service-card, [class*="app"]')[:10]:
                name = card.get_text(strip=True)
                if name and len(name) < 40:
                    apps.append(name)

            # Extract ranges / numbers summary
            ranges = []
            for r in soup.select('[class*="range"], [class*="number"]')[:10]:
                txt = r.get_text(strip=True)
                if re.search(r'\d{4,}', txt):
                    ranges.append(txt[:50])

            return {'apps': apps, 'ranges': ranges}
        except Exception as e:
            logger.error(f"fetch_dashboard_stats error: {e}")
            return None

    # ── SMS Table Parser ──────────────────────────────────────────────────────
    @staticmethod
    def _parse_sms_table(soup) -> list:
        messages = []
        rows = soup.select("table tbody tr") or soup.select("table tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 3:
                texts = [c.get_text(strip=True) for c in cells]
                # Skip header-like rows
                if any(t.lower() in ('sender', 'from', 'number', 'message', 'sms') for t in texts[:2]):
                    continue
                msg = {
                    'sender':  texts[0] if len(texts) > 0 else '',
                    'message': texts[1] if len(texts) > 1 else '',
                    'time':    texts[2] if len(texts) > 2 else '',
                    'revenue': texts[3] if len(texts) > 3 else '0',
                }
                if msg['message']:
                    messages.append(msg)
        return messages

    # ── Debug raw page ────────────────────────────────────────────────────────
    def debug_page(self, path: str) -> str:
        if not self.ensure_login():
            return "NOT LOGGED IN"
        try:
            resp = self._req('GET', f"{BASE_URL}{path}")
            return resp.text if resp else "No response"
        except Exception as e:
            return str(e)


# ─── App Setup ────────────────────────────────────────────────────────────────
app    = Flask(__name__)
client = IVASClient()

# Boot-time login
if client.login():
    logger.info("🚀 App started – logged in successfully.")
else:
    logger.error("⚠️  App started – initial login FAILED. Will retry per request.")


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    return jsonify({
        'logged_in': client.logged_in,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'email': IVAS_EMAIL,
    })


@app.route('/api/numbers')
def api_numbers():
    data = client.fetch_numbers()
    if data is None:
        return jsonify({'error': 'Could not fetch numbers – check logs'}), 500
    return jsonify({'numbers': data, 'count': len(data)})


@app.route('/api/received')
def api_received():
    data = client.fetch_received_sms(
        request.args.get('from'),
        request.args.get('to')
    )
    if data is None:
        return jsonify({'error': 'Could not fetch received SMS'}), 500
    return jsonify(data)


@app.route('/api/live')
def api_live():
    data = client.fetch_live_sms()
    if data is None:
        return jsonify({'error': 'Could not fetch live SMS'}), 500
    return jsonify(data)


@app.route('/api/all')
def api_all():
    numbers  = client.fetch_numbers()
    received = client.fetch_received_sms()
    live     = client.fetch_live_sms()

    errors = []
    if numbers  is None: errors.append('numbers')
    if received is None: errors.append('received')
    if live     is None: errors.append('live')

    if errors:
        return jsonify({'error': f"Failed to fetch: {', '.join(errors)}"}), 500

    return jsonify({
        'numbers':  numbers,
        'received': received,
        'live':     live,
        'fetched_at': datetime.utcnow().isoformat() + 'Z',
    })


@app.route('/api/refresh-login', methods=['POST'])
def api_refresh_login():
    """Force a fresh login attempt."""
    client.logged_in = False
    success = client.login()
    return jsonify({'success': success, 'logged_in': client.logged_in})


# ─── Debug Routes (remove in production if desired) ──────────────────────────
@app.route('/debug/<path:page>')
def debug_raw(page):
    html = client.debug_page(f"/{page}")
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/debug/login-test')
def debug_login_test():
    """Run a fresh login and return result."""
    client.logged_in = False
    ok = client.login()
    return jsonify({
        'success': ok,
        'logged_in': client.logged_in,
        'email': IVAS_EMAIL,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
