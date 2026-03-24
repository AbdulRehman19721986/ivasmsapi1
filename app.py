import os
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

scraper = cloudscraper.create_scraper()
scraper.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Origin': BASE_URL,
})

def login():
    """Perform login and return the final page HTML if successful, else None."""
    try:
        # 1. Get login page
        resp = scraper.get(f"{BASE_URL}/login", timeout=15)
        if resp.status_code != 200:
            logger.error(f"Failed to get login page: {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        csrf_token = soup.find('input', {'name': '_token'})
        if not csrf_token:
            logger.error("No CSRF token found")
            return None

        # 2. Post login
        payload = {
            '_token': csrf_token['value'],
            'email': IVAS_EMAIL,
            'password': IVAS_PASSWORD,
            'remember': '1'
        }
        login_resp = scraper.post(f"{BASE_URL}/login", data=payload, allow_redirects=True, timeout=15)
        logger.info(f"Login POST final status: {login_resp.status_code}")

        # 3. Check if we are logged in
        if login_resp.status_code == 200:
            soup = BeautifulSoup(login_resp.text, 'html.parser')
            # Look for logged-in indicators
            if soup.find('a', href=lambda x: x and 'logout' in x.lower()):
                logger.info("Login successful (found logout link).")
                return login_resp.text
            if soup.find(text=lambda x: x and 'Account Code' in x):
                logger.info("Login successful (found Account Code).")
                return login_resp.text
            if soup.find('a', href=lambda x: x and '/portal/profile' in x):
                logger.info("Login successful (found profile link).")
                return login_resp.text

            logger.warning("Logged-in indicators not found.")
            # Save snippet for debugging
            with open('login_debug.html', 'w') as f:
                f.write(login_resp.text)
            return None
        else:
            logger.error(f"Login failed with status {login_resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Login exception: {e}")
        return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/debug/login')
def debug_login():
    html = login()
    if html:
        return html
    else:
        return "Login failed", 401

@app.route('/api/all')
def api_all():
    html = login()
    if not html:
        return jsonify({'error': 'Login failed'}), 401

    # Here you would parse numbers, received, live from the HTML
    # For now, return a placeholder
    return jsonify({
        'numbers': [],
        'received': {'stats': {}, 'messages': []},
        'live': {'stats': {}, 'messages': []}
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
