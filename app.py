"""
IVAS SMS Dashboard - Professional v2
Uses the correct 3-step API:
  1. POST /portal/sms/received/getsms          → get ranges + stats
  2. POST /portal/sms/received/getsms/number   → get numbers in a range
  3. POST /portal/sms/received/getsms/number/sms → get OTP message for a number

Also scrapes:
  - /portal/numbers      → My Numbers (full table)
  - /portal/live/my_sms  → Live SMS list
"""

import os, re, json, time, gzip, logging
import brotli
import cloudscraper
from io import BytesIO
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template
from requests.exceptions import ConnectionError, Timeout

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
BASE_URL      = "https://www.ivasms.com"
IVAS_EMAIL    = os.environ.get('IVAS_EMAIL',    'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')
COOKIES_ENV   = os.environ.get('COOKIES_JSON',  '')   # optional JSON cookie string

# ─── Client ───────────────────────────────────────────────────────────────────
class IVASClient:
    def __init__(self):
        self.scraper    = self._make_scraper()
        self.logged_in  = False
        self.csrf_token = None

    # ── scraper ──────────────────────────────────────────────────────────────
    def _make_scraper(self):
        s = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        s.headers.update({
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection':      'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest':  'document',
            'Sec-Fetch-Mode':  'navigate',
            'Sec-Fetch-Site':  'none',
            'Sec-Fetch-User':  '?1',
            'Cache-Control':   'max-age=0',
        })
        return s

    # ── decompress ────────────────────────────────────────────────────────────
    def _decompress(self, resp) -> str:
        enc     = resp.headers.get('Content-Encoding', '').lower()
        content = resp.content
        try:
            if enc == 'gzip':
                content = gzip.decompress(content)
            elif enc == 'br':
                content = brotli.decompress(content)
        except Exception as e:
            logger.warning(f"Decompress warning: {e}")
        try:
            return content.decode('utf-8', errors='replace')
        except Exception:
            return resp.text

    # ── http helper ───────────────────────────────────────────────────────────
    def _req(self, method, url, retries=3, **kwargs):
        kwargs.setdefault('timeout', 20)
        for attempt in range(1, retries + 1):
            try:
                resp = self.scraper.request(method, url, **kwargs)
                logger.info(f"[{attempt}] {method.upper()} {url} → {resp.status_code}")
                return resp
            except (ConnectionError, Timeout) as e:
                logger.warning(f"Request fail [{attempt}/{retries}]: {e}")
                if attempt < retries:
                    time.sleep(2 * attempt)
                else:
                    raise
        return None

    # ── cookie loading ────────────────────────────────────────────────────────
    def _load_cookies(self):
        raw = COOKIES_ENV.strip()
        if not raw:
            path = os.path.join(os.path.dirname(__file__), 'cookies.json')
            if os.path.exists(path):
                with open(path) as f:
                    raw = f.read().strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return {c['name']: c['value'] for c in data if 'name' in c}
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.error(f"Cookie parse error: {e}")
        return {}

    # ── login ─────────────────────────────────────────────────────────────────
    def login(self) -> bool:
        """Try cookie auth → then credential auth."""
        # 1. Cookie auth
        cookies = self._load_cookies()
        if cookies:
            for name, value in cookies.items():
                self.scraper.cookies.set(name, value, domain="www.ivasms.com")
            logger.info(f"Injected {len(cookies)} cookies.")
            if self._verify_session():
                return True
            logger.warning("Cookies loaded but session invalid – trying credentials.")

        # 2. Credential login
        return self._credential_login()

    def _verify_session(self) -> bool:
        """Check if current session is valid by hitting the received SMS page."""
        try:
            resp  = self._req('GET', f"{BASE_URL}/portal/sms/received")
            if resp and resp.status_code == 200:
                html = self._decompress(resp)
                soup = BeautifulSoup(html, 'html.parser')
                csrf = soup.find('input', {'name': '_token'})
                if csrf:
                    self.csrf_token = csrf['value']
                    self.logged_in  = True
                    logger.info("✅ Session valid – CSRF obtained.")
                    return True
                logger.warning("Session check: no CSRF on page.")
                logger.debug(f"Page snippet: {html[:500]}")
        except Exception as e:
            logger.error(f"Session verify error: {e}")
        return False

    def _credential_login(self) -> bool:
        logger.info("🔑 Credential login…")
        try:
            resp = self._req('GET', f"{BASE_URL}/login")
            if not resp or resp.status_code != 200:
                return False
            soup = BeautifulSoup(self._decompress(resp), 'html.parser')
            csrf_el = soup.find('input', {'name': '_token'})
            if not csrf_el:
                logger.error("No CSRF on login page.")
                return False
            csrf = csrf_el['value']
            post = self._req('POST', f"{BASE_URL}/login",
                             data={'_token': csrf, 'email': IVAS_EMAIL,
                                   'password': IVAS_PASSWORD, 'remember': '1'},
                             allow_redirects=True,
                             headers={'Content-Type': 'application/x-www-form-urlencoded',
                                      'Origin': BASE_URL, 'Referer': f"{BASE_URL}/login"})
            if post and post.status_code == 200:
                return self._verify_session()
        except Exception as e:
            logger.error(f"Credential login error: {e}")
        return False

    def ensure_login(self) -> bool:
        if self.logged_in and self.csrf_token:
            return True
        return self.login()

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API METHODS
    # ─────────────────────────────────────────────────────────────────────────

    # ── 1. My Numbers ─────────────────────────────────────────────────────────
    def fetch_numbers(self):
        """Scrape /portal/numbers – returns list of {number, range, rate, limit}"""
        if not self.ensure_login():
            return None
        try:
            resp = self._req('GET', f"{BASE_URL}/portal/numbers")
            if not resp or resp.status_code != 200:
                return None
            soup  = BeautifulSoup(self._decompress(resp), 'html.parser')
            rows  = soup.select("table tbody tr")
            numbers = []
            for row in rows:
                cells = [c.get_text(strip=True) for c in row.find_all('td')]
                if len(cells) >= 3 and re.match(r'^\+?\d{7,}$', cells[0]):
                    numbers.append({
                        'number':     cells[0],
                        'range_name': cells[1] if len(cells) > 1 else '',
                        'rate':       cells[2] if len(cells) > 2 else '',
                        'limit':      cells[3] if len(cells) > 3 else '',
                    })
            if not numbers:
                # Fallback: regex scan
                for m in re.finditer(r'(\d{10,})', self._decompress(resp)):
                    numbers.append({'number': m.group(1), 'range_name': '', 'rate': '', 'limit': ''})
            seen = set(); unique = []
            for n in numbers:
                if n['number'] not in seen:
                    seen.add(n['number']); unique.append(n)
            logger.info(f"Numbers: {len(unique)}")
            return unique
        except Exception as e:
            logger.error(f"fetch_numbers: {e}")
            return None

    # ── 2. Received SMS (stats + per-range breakdown) ─────────────────────────
    def fetch_received_stats(self, from_date='', to_date=''):
        """
        POST /portal/sms/received/getsms
        Returns {count_sms, paid_sms, unpaid_sms, revenue, sms_details:[{range,count,paid,unpaid,revenue}]}
        """
        if not self.ensure_login():
            return None
        try:
            payload = {'from': from_date, 'to': to_date, '_token': self.csrf_token}
            headers = {
                'Accept':           'text/html, */*; q=0.01',
                'Content-Type':     'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin':           BASE_URL,
                'Referer':          f"{BASE_URL}/portal/sms/received",
            }
            resp = self._req('POST', f"{BASE_URL}/portal/sms/received/getsms",
                             data=payload, headers=headers)
            if not resp or resp.status_code != 200:
                return None
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')

            def _t(sel):
                el = soup.select_one(sel)
                return el.get_text(strip=True).replace(' USD','') if el else '0'

            details = []
            for item in soup.select("div.item"):
                rng  = item.select_one(".col-sm-4")
                cols = item.select(".col-3")
                if rng and len(cols) >= 4:
                    def _p(el): return el.select_one('p').get_text(strip=True) if el and el.select_one('p') else '0'
                    rev_el = item.select_one(".col-3:nth-child(5) p span.currency_cdr") or \
                             item.select_one(".col-3:last-child p span")
                    details.append({
                        'range':   rng.get_text(strip=True),
                        'count':   _p(cols[0]),
                        'paid':    _p(cols[1]),
                        'unpaid':  _p(cols[2]),
                        'revenue': rev_el.get_text(strip=True) if rev_el else _p(cols[3]),
                    })

            result = {
                'count_sms': _t('#CountSMS'),
                'paid_sms':  _t('#PaidSMS'),
                'unpaid_sms':_t('#UnpaidSMS'),
                'revenue':   _t('#RevenueSMS'),
                'sms_details': details,
                '_raw': html,
            }
            logger.info(f"Received stats: {result['count_sms']} SMS, {len(details)} ranges")
            return result
        except Exception as e:
            logger.error(f"fetch_received_stats: {e}")
            return None

    # ── 3. Numbers in a range ─────────────────────────────────────────────────
    def fetch_numbers_in_range(self, phone_range, from_date='', to_date=''):
        """
        POST /portal/sms/received/getsms/number
        Returns [{phone_number, count, paid, unpaid, revenue, id_number}]
        """
        if not self.ensure_login():
            return []
        try:
            payload = {'_token': self.csrf_token, 'start': from_date,
                       'end': to_date, 'range': phone_range}
            headers = {
                'Accept':           'text/html, */*; q=0.01',
                'Content-Type':     'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin':           BASE_URL,
                'Referer':          f"{BASE_URL}/portal/sms/received",
            }
            resp = self._req('POST', f"{BASE_URL}/portal/sms/received/getsms/number",
                             data=payload, headers=headers)
            if not resp or resp.status_code != 200:
                return []
            soup = BeautifulSoup(self._decompress(resp), 'html.parser')
            results = []
            for item in soup.select("div.card.card-body"):
                ph   = item.select_one(".col-sm-4")
                cols = item.select(".col-3")
                if ph:
                    onclick   = ph.get('onclick', '')
                    id_number = onclick.split("'")[3] if "'" in onclick and len(onclick.split("'")) > 3 else ''
                    def _p(el): return el.select_one('p').get_text(strip=True) if el and el.select_one('p') else '0'
                    rev_el = item.select_one(".col-3:nth-child(5) p span.currency_cdr") or \
                             item.select_one(".col-3:last-child p span")
                    results.append({
                        'phone_number': ph.get_text(strip=True),
                        'count':   _p(cols[0]) if cols else '0',
                        'paid':    _p(cols[1]) if len(cols) > 1 else '0',
                        'unpaid':  _p(cols[2]) if len(cols) > 2 else '0',
                        'revenue': rev_el.get_text(strip=True) if rev_el else (_p(cols[3]) if len(cols) > 3 else '0'),
                        'id_number': id_number,
                    })
            logger.info(f"Numbers in {phone_range}: {len(results)}")
            return results
        except Exception as e:
            logger.error(f"fetch_numbers_in_range({phone_range}): {e}")
            return []

    # ── 4. OTP message for a number ───────────────────────────────────────────
    def fetch_otp_for_number(self, phone_number, phone_range, from_date='', to_date=''):
        """
        POST /portal/sms/received/getsms/number/sms
        Returns the OTP/message string or None
        """
        if not self.ensure_login():
            return None
        try:
            payload = {
                '_token': self.csrf_token,
                'start':  from_date,
                'end':    to_date,
                'Number': phone_number,
                'Range':  phone_range,
            }
            headers = {
                'Accept':           'text/html, */*; q=0.01',
                'Content-Type':     'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin':           BASE_URL,
                'Referer':          f"{BASE_URL}/portal/sms/received",
            }
            resp = self._req('POST', f"{BASE_URL}/portal/sms/received/getsms/number/sms",
                             data=payload, headers=headers)
            if not resp or resp.status_code != 200:
                return None
            soup = BeautifulSoup(self._decompress(resp), 'html.parser')
            # Try various selectors
            for sel in [".col-9.col-sm-6 p", ".message-text", ".sms-body", ".col-9 p", "p"]:
                el = soup.select_one(sel)
                if el and el.get_text(strip=True):
                    return el.get_text(strip=True)
            return None
        except Exception as e:
            logger.error(f"fetch_otp_for_number({phone_number}): {e}")
            return None

    # ── 5. Full OTP fetch (all ranges → numbers → messages) ───────────────────
    def fetch_all_otps(self, from_date='', to_date='', limit=50):
        """
        Orchestrate the full 3-step chain.
        Returns list of {range, phone_number, otp_message, count, paid, revenue}
        """
        stats = self.fetch_received_stats(from_date, to_date)
        if not stats:
            return None, None

        all_otps = []
        for detail in stats.get('sms_details', []):
            rng          = detail['range']
            num_details  = self.fetch_numbers_in_range(rng, from_date, to_date)
            for nd in num_details:
                if limit and len(all_otps) >= limit:
                    break
                msg = self.fetch_otp_for_number(nd['phone_number'], rng, from_date, to_date)
                all_otps.append({
                    'range':        rng,
                    'phone_number': nd['phone_number'],
                    'otp_message':  msg or '',
                    'count':        nd['count'],
                    'paid':         nd['paid'],
                    'revenue':      nd['revenue'],
                })
            if limit and len(all_otps) >= limit:
                break

        logger.info(f"Total OTPs fetched: {len(all_otps)}")
        return stats, all_otps

    # ── 6. Live SMS ───────────────────────────────────────────────────────────
    def fetch_live_sms(self):
        """Scrape /portal/live/my_sms"""
        if not self.ensure_login():
            return None
        try:
            resp = self._req('GET', f"{BASE_URL}/portal/live/my_sms")
            if not resp or resp.status_code != 200:
                return None
            html = self._decompress(resp)
            soup = BeautifulSoup(html, 'html.parser')

            def _t(sid):
                el = soup.find(id=sid)
                return el.get_text(strip=True).replace(' USD','').replace(',','') if el else '0'

            stats = {
                'total':   _t('CountSMS'),
                'paid':    _t('PaidSMS'),
                'unpaid':  _t('UnpaidSMS'),
                'revenue': _t('RevenueSMS'),
            }

            # Parse the numbers list on the right side panel
            numbers_list = []
            for item in soup.select(".list-group-item, .number-item, li"):
                txt = item.get_text(strip=True)
                if re.match(r'^\d{10,}$', txt):
                    numbers_list.append(txt)

            # Parse SID table (left pane)
            rows = []
            for row in soup.select("table tbody tr"):
                cells = [c.get_text(strip=True) for c in row.find_all('td')]
                if cells and len(cells) >= 3:
                    rows.append({
                        'sid':     cells[0],
                        'paid':    cells[1] if len(cells) > 1 else '',
                        'limit':   cells[2] if len(cells) > 2 else '',
                        'message': cells[3] if len(cells) > 3 else '',
                    })

            sms_today = _t('sms_today') or '0'
            # Also try a simpler selector
            today_el = soup.select_one(".sms-today, #sms_today, .badge-today")
            if today_el:
                sms_today = today_el.get_text(strip=True)

            logger.info(f"Live: {stats['total']} total, {len(numbers_list)} numbers, {len(rows)} SID rows")
            return {
                'stats':       stats,
                'sms_today':   sms_today,
                'numbers':     numbers_list,
                'sid_rows':    rows,
            }
        except Exception as e:
            logger.error(f"fetch_live_sms: {e}")
            return None


# ─── Flask App ────────────────────────────────────────────────────────────────
app    = Flask(__name__)
client = IVASClient()

logger.info("Attempting boot-time login…")
if client.login():
    logger.info("🚀 Boot login SUCCESS")
else:
    logger.error("⚠️  Boot login FAILED – will retry per request")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    return jsonify({'logged_in': client.logged_in, 'ts': datetime.utcnow().isoformat()})


@app.route('/api/numbers')
def api_numbers():
    data = client.fetch_numbers()
    if data is None:
        return jsonify({'error': 'fetch failed'}), 500
    return jsonify({'numbers': data, 'count': len(data)})


@app.route('/api/received')
def api_received():
    fd = request.args.get('from', '')
    td = request.args.get('to',   '')
    stats = client.fetch_received_stats(fd, td)
    if stats is None:
        return jsonify({'error': 'fetch failed'}), 500
    stats.pop('_raw', None)
    return jsonify(stats)


@app.route('/api/otps')
def api_otps():
    """Full 3-step OTP chain."""
    fd    = request.args.get('from',  '')
    td    = request.args.get('to',    '')
    limit = int(request.args.get('limit', 50))
    stats, otps = client.fetch_all_otps(fd, td, limit)
    if stats is None:
        return jsonify({'error': 'fetch failed'}), 500
    stats.pop('_raw', None)
    return jsonify({'stats': stats, 'otps': otps, 'count': len(otps)})


@app.route('/api/live')
def api_live():
    data = client.fetch_live_sms()
    if data is None:
        return jsonify({'error': 'fetch failed'}), 500
    return jsonify(data)


@app.route('/api/all')
def api_all():
    today = datetime.now().strftime('%Y-%m-%d')
    numbers  = client.fetch_numbers()
    received = client.fetch_received_stats(today, today)
    live     = client.fetch_live_sms()
    errors   = [k for k, v in [('numbers', numbers), ('received', received), ('live', live)] if v is None]
    if errors:
        return jsonify({'error': f"Failed: {', '.join(errors)}"}), 500
    received_clean = {k: v for k, v in received.items() if k != '_raw'}
    return jsonify({'numbers': numbers, 'received': received_clean, 'live': live,
                    'ts': datetime.utcnow().isoformat()})


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    client.logged_in = False; client.csrf_token = None
    ok = client.login()
    return jsonify({'success': ok})


# Debug
@app.route('/debug/<path:p>')
def debug(p):
    if not client.ensure_login(): return "not logged in", 401
    r = client._req('GET', f"{BASE_URL}/{p}")
    return (r.text if r else "no response"), 200, {'Content-Type': 'text/html; charset=utf-8'}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
