"""
IVAS SMS Dashboard v3 — KEY FIX: Accept-Encoding = 'gzip, deflate' (no brotli)
"""
import os, re, json, time, gzip, logging
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template
import cloudscraper
from requests.exceptions import ConnectionError, Timeout

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_URL      = "https://www.ivasms.com"
IVAS_EMAIL    = os.environ.get('IVAS_EMAIL',    'usa19721986@gmail.com')
IVAS_PASSWORD = os.environ.get('IVAS_PASSWORD', 'Amin@1972')
COOKIES_ENV   = os.environ.get('COOKIES_JSON',  '')

class IVASClient:
    def __init__(self):
        self.scraper    = self._make_scraper()
        self.logged_in  = False
        self.csrf_token = None

    def _make_scraper(self):
        s = cloudscraper.create_scraper(browser={'browser':'chrome','platform':'windows','mobile':False})
        s.headers.update({
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection':      'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest':  'document',
            'Sec-Fetch-Mode':  'navigate',
            'Sec-Fetch-Site':  'none',
            'Sec-Fetch-User':  '?1',
            'Cache-Control':   'max-age=0',
        })
        return s

    def _text(self, resp) -> str:
        enc = resp.headers.get('Content-Encoding', '').lower()
        raw = resp.content
        if enc == 'gzip':
            try: raw = gzip.decompress(raw)
            except Exception as e: logger.warning(f"gzip decompress: {e}"); return resp.text
        elif enc == 'br':
            logger.warning("Got brotli — using resp.text fallback")
            return resp.text
        return raw.decode('utf-8', errors='replace')

    def _req(self, method, url, retries=3, extra_headers=None, **kwargs):
        kwargs.setdefault('timeout', 25)
        hdrs = {'Accept-Encoding': 'gzip, deflate'}
        if extra_headers: hdrs.update(extra_headers)
        if 'headers' in kwargs: hdrs.update(kwargs.pop('headers'))
        for attempt in range(1, retries + 1):
            try:
                resp = self.scraper.request(method, url, headers=hdrs, **kwargs)
                enc  = resp.headers.get('Content-Encoding','none')
                logger.info(f"[{attempt}] {method.upper()} {url} → {resp.status_code} enc={enc}")
                return resp
            except (ConnectionError, Timeout) as e:
                logger.warning(f"[{attempt}/{retries}] {e}")
                if attempt < retries: time.sleep(2*attempt)
                else: raise
        return None

    def _ajax(self, referer):
        return {'Accept':'text/html, */*; q=0.01',
                'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With':'XMLHttpRequest',
                'Origin':BASE_URL, 'Referer':referer,
                'Accept-Encoding':'gzip, deflate'}

    def _load_cookies(self):
        raw = COOKIES_ENV.strip()
        if not raw:
            p = os.path.join(os.path.dirname(__file__), 'cookies.json')
            if os.path.exists(p):
                with open(p) as f: raw = f.read().strip()
        if not raw: return {}
        try:
            d = json.loads(raw)
            if isinstance(d, list): return {c['name']:c['value'] for c in d if 'name' in c}
            if isinstance(d, dict): return d
        except Exception as e: logger.error(f"Cookie parse: {e}")
        return {}

    def login(self) -> bool:
        cookies = self._load_cookies()
        if cookies:
            for n,v in cookies.items(): self.scraper.cookies.set(n, v, domain='www.ivasms.com')
            logger.info(f"Injected {len(cookies)} cookies")
            if self._verify(): return True
            logger.warning("Cookies stale — trying credentials")
        return self._cred_login()

    def _verify(self) -> bool:
        try:
            resp = self._req('GET', f"{BASE_URL}/portal/sms/received")
            if resp and resp.status_code == 200:
                html = self._text(resp)
                soup = BeautifulSoup(html, 'html.parser')
                el   = soup.find('input', {'name': '_token'})
                if el:
                    self.csrf_token = el['value']
                    self.logged_in  = True
                    logger.info(f"✅ Session OK. CSRF={self.csrf_token[:16]}…")
                    return True
                logger.warning(f"No _token on page. Snippet: {html[:400]}")
        except Exception as e: logger.error(f"_verify: {e}")
        return False

    def _cred_login(self) -> bool:
        logger.info("🔑 Credential login…")
        try:
            r1 = self._req('GET', f"{BASE_URL}/login")
            if not r1 or r1.status_code != 200: return False
            el = BeautifulSoup(self._text(r1), 'html.parser').find('input', {'name': '_token'})
            if not el: logger.error("No CSRF on login page"); return False
            r2 = self._req('POST', f"{BASE_URL}/login",
                           data={'_token':el['value'],'email':IVAS_EMAIL,'password':IVAS_PASSWORD,'remember':'1'},
                           allow_redirects=True,
                           extra_headers={'Content-Type':'application/x-www-form-urlencoded','Origin':BASE_URL,'Referer':f"{BASE_URL}/login"})
            if r2 and r2.status_code == 200: return self._verify()
        except Exception as e: logger.error(f"_cred_login: {e}")
        return False

    def ensure_login(self) -> bool:
        if self.logged_in and self.csrf_token: return True
        return self.login()

    # Numbers
    def fetch_numbers(self):
        if not self.ensure_login(): return None
        try:
            resp = self._req('GET', f"{BASE_URL}/portal/numbers")
            if not resp or resp.status_code != 200: return None
            html = self._text(resp)
            soup = BeautifulSoup(html, 'html.parser')
            out  = []
            for row in soup.select('table tbody tr'):
                cells = [c.get_text(strip=True) for c in row.find_all('td')]
                if cells and re.match(r'^\+?\d{7,}$', cells[0]):
                    out.append({'number':cells[0],'range_name':cells[1] if len(cells)>1 else '',
                                'rate':cells[2] if len(cells)>2 else '','limit':cells[3] if len(cells)>3 else ''})
            if not out:
                seen=set()
                for m in re.finditer(r'\b(\d{10,})\b', html):
                    n=m.group(1)
                    if n not in seen: seen.add(n); out.append({'number':n,'range_name':'','rate':'','limit':''})
            logger.info(f"Numbers: {len(out)}")
            return out
        except Exception as e: logger.error(f"fetch_numbers: {e}"); return None

    # Step 1
    def fetch_received_stats(self, from_date='', to_date=''):
        if not self.ensure_login(): return None
        try:
            resp = self._req('POST', f"{BASE_URL}/portal/sms/received/getsms",
                             data={'from':from_date,'to':to_date,'_token':self.csrf_token},
                             extra_headers=self._ajax(f"{BASE_URL}/portal/sms/received"))
            if not resp or resp.status_code != 200: return None
            html = self._text(resp)
            soup = BeautifulSoup(html, 'html.parser')
            def _t(sel):
                el=soup.select_one(sel); return el.get_text(strip=True).replace(' USD','') if el else '0'
            details=[]
            for item in soup.select('div.item'):
                rng=item.select_one('.col-sm-4'); cols=item.select('.col-3')
                if not rng: continue
                def _p(el):
                    if not el: return '0'
                    p=el.select_one('p'); return p.get_text(strip=True) if p else el.get_text(strip=True)
                rev_el=(item.select_one('.col-3:nth-child(5) p span.currency_cdr') or
                        item.select_one('.col-3:last-child p span'))
                details.append({'range':rng.get_text(strip=True),
                                 'count':_p(cols[0]) if cols else '0',
                                 'paid':_p(cols[1]) if len(cols)>1 else '0',
                                 'unpaid':_p(cols[2]) if len(cols)>2 else '0',
                                 'revenue':rev_el.get_text(strip=True) if rev_el else '0'})
            result={'count_sms':_t('#CountSMS'),'paid_sms':_t('#PaidSMS'),
                    'unpaid_sms':_t('#UnpaidSMS'),'revenue':_t('#RevenueSMS'),
                    'sms_details':details,'_raw':html}
            logger.info(f"Received: {result['count_sms']} SMS, {len(details)} ranges")
            return result
        except Exception as e: logger.error(f"fetch_received_stats: {e}"); return None

    # Step 2
    def fetch_numbers_in_range(self, phone_range, from_date='', to_date=''):
        if not self.ensure_login(): return []
        try:
            resp = self._req('POST', f"{BASE_URL}/portal/sms/received/getsms/number",
                             data={'_token':self.csrf_token,'start':from_date,'end':to_date,'range':phone_range},
                             extra_headers=self._ajax(f"{BASE_URL}/portal/sms/received"))
            if not resp or resp.status_code != 200: return []
            html = self._text(resp)
            soup = BeautifulSoup(html, 'html.parser')
            out  = []
            for item in soup.select('div.card.card-body'):
                ph=item.select_one('.col-sm-4'); cols=item.select('.col-3')
                if not ph: continue
                onclick=ph.get('onclick','')
                id_num=onclick.split("'")[3] if "'" in onclick and len(onclick.split("'"))>3 else ''
                def _p(el):
                    if not el: return '0'
                    p=el.select_one('p'); return p.get_text(strip=True) if p else '0'
                rev_el=(item.select_one('.col-3:nth-child(5) p span.currency_cdr') or
                        item.select_one('.col-3:last-child p span'))
                out.append({'phone_number':ph.get_text(strip=True),
                            'count':_p(cols[0]) if cols else '0',
                            'paid':_p(cols[1]) if len(cols)>1 else '0',
                            'unpaid':_p(cols[2]) if len(cols)>2 else '0',
                            'revenue':rev_el.get_text(strip=True) if rev_el else '0',
                            'id_number':id_num})
            logger.info(f"  Range '{phone_range}': {len(out)} numbers")
            return out
        except Exception as e: logger.error(f"fetch_numbers_in_range: {e}"); return []

    # Step 3
    def fetch_otp_for_number(self, phone_number, phone_range, from_date='', to_date=''):
        if not self.ensure_login(): return None
        try:
            resp = self._req('POST', f"{BASE_URL}/portal/sms/received/getsms/number/sms",
                             data={'_token':self.csrf_token,'start':from_date,'end':to_date,
                                   'Number':phone_number,'Range':phone_range},
                             extra_headers=self._ajax(f"{BASE_URL}/portal/sms/received"))
            if not resp or resp.status_code != 200: return None
            html = self._text(resp)
            soup = BeautifulSoup(html, 'html.parser')
            for sel in ['.col-9.col-sm-6 p','.message-text','.sms-body','.col-9 p','p']:
                el=soup.select_one(sel)
                if el:
                    t=el.get_text(strip=True)
                    if t: logger.info(f"    OTP {phone_number}: {t[:80]}"); return t
            return None
        except Exception as e: logger.error(f"fetch_otp({phone_number}): {e}"); return None

    def fetch_all_otps(self, from_date='', to_date='', limit=50):
        stats = self.fetch_received_stats(from_date, to_date)
        if not stats: return None, None
        all_otps=[]
        for d in stats.get('sms_details',[]):
            rng=d['range']
            for nd in self.fetch_numbers_in_range(rng, from_date, to_date):
                if limit and len(all_otps)>=limit: break
                msg=self.fetch_otp_for_number(nd['phone_number'], rng, from_date, to_date)
                all_otps.append({'range':rng,'phone_number':nd['phone_number'],
                                 'otp_message':msg or '','count':nd['count'],
                                 'paid':nd['paid'],'revenue':nd['revenue']})
            if limit and len(all_otps)>=limit: break
        logger.info(f"Total OTPs: {len(all_otps)}")
        return stats, all_otps

    def fetch_live_sms(self):
        if not self.ensure_login(): return None
        try:
            resp = self._req('GET', f"{BASE_URL}/portal/live/my_sms")
            if not resp or resp.status_code != 200: return None
            html = self._text(resp)
            soup = BeautifulSoup(html, 'html.parser')
            def _t(sid):
                el=soup.find(id=sid); return el.get_text(strip=True).replace(' USD','').replace(',','') if el else '0'
            stats={'total':_t('CountSMS'),'paid':_t('PaidSMS'),'unpaid':_t('UnpaidSMS'),'revenue':_t('RevenueSMS')}
            nums_list=[]; seen=set()
            for m in re.finditer(r'\b(\d{10,})\b', html):
                n=m.group(1)
                if n not in seen: seen.add(n); nums_list.append(n)
            sid_rows=[]
            for row in soup.select('table tbody tr'):
                cells=[c.get_text(strip=True) for c in row.find_all('td')]
                if len(cells)>=2:
                    sid_rows.append({'sid':cells[0],'paid':cells[1] if len(cells)>1 else '',
                                     'limit':cells[2] if len(cells)>2 else '','message':cells[3] if len(cells)>3 else ''})
            logger.info(f"Live: {stats}, {len(nums_list)} nums, {len(sid_rows)} rows")
            return {'stats':stats,'sms_today':stats['total'],'numbers':nums_list[:200],'sid_rows':sid_rows}
        except Exception as e: logger.error(f"fetch_live_sms: {e}"); return None


app    = Flask(__name__)
client = IVASClient()
logger.info("Boot login…")
if client.login(): logger.info("🚀 Logged in OK")
else:              logger.error("⚠️  Login FAILED")

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def api_status(): return jsonify({'logged_in':client.logged_in,'ts':datetime.utcnow().isoformat()})

@app.route('/api/numbers')
def api_numbers():
    d=client.fetch_numbers()
    if d is None: return jsonify({'error':'fetch failed'}),500
    return jsonify({'numbers':d,'count':len(d)})

@app.route('/api/received')
def api_received():
    d=client.fetch_received_stats(request.args.get('from',''),request.args.get('to',''))
    if d is None: return jsonify({'error':'fetch failed'}),500
    d.pop('_raw',None); return jsonify(d)

@app.route('/api/otps')
def api_otps():
    stats,otps=client.fetch_all_otps(
        request.args.get('from',''),request.args.get('to',''),
        int(request.args.get('limit',50)))
    if stats is None: return jsonify({'error':'fetch failed'}),500
    stats.pop('_raw',None)
    return jsonify({'stats':stats,'otps':otps,'count':len(otps)})

@app.route('/api/live')
def api_live():
    d=client.fetch_live_sms()
    if d is None: return jsonify({'error':'fetch failed'}),500
    return jsonify(d)

@app.route('/api/all')
def api_all():
    today=datetime.now().strftime('%Y-%m-%d')
    numbers=client.fetch_numbers(); received=client.fetch_received_stats(today,today); live=client.fetch_live_sms()
    errors=[k for k,v in [('numbers',numbers),('received',received),('live',live)] if v is None]
    if errors: return jsonify({'error':f"Failed: {', '.join(errors)}"}),500
    received.pop('_raw',None)
    return jsonify({'numbers':numbers,'received':received,'live':live,'ts':datetime.utcnow().isoformat()})

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    client.logged_in=False; client.csrf_token=None
    return jsonify({'success':client.login()})

@app.route('/debug/raw/<path:p>')
def debug_raw(p):
    if not client.ensure_login(): return "not logged in",401
    r=client._req('GET',f"{BASE_URL}/{p}")
    return (client._text(r) if r else "no response"),200,{'Content-Type':'text/plain; charset=utf-8'}

@app.route('/debug/<path:p>')
def debug_html(p):
    if not client.ensure_login(): return "not logged in",401
    r=client._req('GET',f"{BASE_URL}/{p}")
    return (r.text if r else "no response"),200,{'Content-Type':'text/html; charset=utf-8'}

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
