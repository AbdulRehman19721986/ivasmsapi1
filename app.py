from flask import Flask, request, jsonify, render_template
from datetime import datetime
import cloudscraper
import json
from bs4 import BeautifulSoup
import logging
import os
import gzip
import brotli

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class IVASSMSClient:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.base_url = "https://www.ivasms.com"
        self.logged_in = False
        self.csrf_token = None
        
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })

    def decompress_response(self, response):
        encoding = response.headers.get('Content-Encoding', '').lower()
        content = response.content
        try:
            if encoding == 'gzip':
                content = gzip.decompress(content)
            elif encoding == 'br':
                content = brotli.decompress(content)
            return content.decode('utf-8', errors='replace')
        except Exception as e:
            logger.error(f"Error decompressing response: {e}")
            return response.text

    def load_cookies(self, file_path="cookies.json"):
        try:
            if os.getenv("COOKIES_JSON"):
                cookies_raw = json.loads(os.getenv("COOKIES_JSON"))
            else:
                with open(file_path, 'r') as file:
                    cookies_raw = json.load(file)
            if isinstance(cookies_raw, dict):
                return cookies_raw
            elif isinstance(cookies_raw, list):
                cookies = {}
                for cookie in cookies_raw:
                    if 'name' in cookie and 'value' in cookie:
                        cookies[cookie['name']] = cookie['value']
                return cookies
            else:
                raise ValueError("Unsupported cookies format.")
        except Exception as e:
            logger.error(f"Error loading cookies: {e}")
            return None

    def login_with_cookies(self, cookies_file="cookies.json"):
        logger.debug("Logging in with cookies")
        cookies = self.load_cookies(cookies_file)
        if not cookies:
            return False
        for name, value in cookies.items():
            self.scraper.cookies.set(name, value, domain="www.ivasms.com")
        try:
            response = self.scraper.get(f"{self.base_url}/portal/sms/received", timeout=10)
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                csrf_input = soup.find('input', {'name': '_token'})
                if csrf_input:
                    self.csrf_token = csrf_input.get('value')
                    self.logged_in = True
                    return True
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def check_otps(self, from_date="", to_date=""):
        if not self.logged_in or not self.csrf_token:
            return None
        try:
            payload = {'from': from_date, 'to': to_date, '_token': self.csrf_token}
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            response = self.scraper.post(f"{self.base_url}/portal/sms/received/getsms", data=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                count_sms = soup.select_one("#CountSMS").text if soup.select_one("#CountSMS") else '0'
                paid_sms = soup.select_one("#PaidSMS").text if soup.select_one("#PaidSMS") else '0'
                unpaid_sms = soup.select_one("#UnpaidSMS").text if soup.select_one("#UnpaidSMS") else '0'
                revenue_sms = soup.select_one("#RevenueSMS").text.replace(' USD', '') if soup.select_one("#RevenueSMS") else '0'
                sms_details = []
                items = soup.select("div.item")
                for item in items:
                    country_number = item.select_one(".col-sm-4").text.strip()
                    count = item.select_one(".col-3:nth-child(2) p").text.strip()
                    paid = item.select_one(".col-3:nth-child(3) p").text.strip()
                    unpaid = item.select_one(".col-3:nth-child(4) p").text.strip()
                    revenue = item.select_one(".col-3:nth-child(5) p span.currency_cdr").text.strip()
                    sms_details.append({
                        'country_number': country_number,
                        'count': count,
                        'paid': paid,
                        'unpaid': unpaid,
                        'revenue': revenue
                    })
                return {
                    'count_sms': count_sms,
                    'paid_sms': paid_sms,
                    'unpaid_sms': unpaid_sms,
                    'revenue': revenue_sms,
                    'sms_details': sms_details
                }
            return None
        except Exception as e:
            logger.error(f"Error checking OTPs: {e}")
            return None

    def get_sms_details(self, phone_range, from_date="", to_date=""):
        if not self.logged_in or not self.csrf_token:
            return None
        try:
            payload = {'_token': self.csrf_token, 'start': from_date, 'end': to_date, 'range': phone_range}
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            response = self.scraper.post(f"{self.base_url}/portal/sms/received/getsms/number", data=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                number_details = []
                items = soup.select("div.card.card-body")
                for item in items:
                    phone_number = item.select_one(".col-sm-4").text.strip()
                    count = item.select_one(".col-3:nth-child(2) p").text.strip()
                    paid = item.select_one(".col-3:nth-child(3) p").text.strip()
                    unpaid = item.select_one(".col-3:nth-child(4) p").text.strip()
                    revenue = item.select_one(".col-3:nth-child(5) p span.currency_cdr").text.strip()
                    onclick = item.select_one(".col-sm-4").get('onclick', '')
                    id_number = onclick.split("'")[3] if onclick else ''
                    number_details.append({
                        'phone_number': phone_number,
                        'count': count,
                        'paid': paid,
                        'unpaid': unpaid,
                        'revenue': revenue,
                        'id_number': id_number
                    })
                return number_details
            return None
        except Exception as e:
            logger.error(f"Error getting SMS details: {e}")
            return None

    def get_otp_message(self, phone_number, phone_range, from_date="", to_date=""):
        if not self.logged_in or not self.csrf_token:
            return None
        try:
            payload = {'_token': self.csrf_token, 'start': from_date, 'end': to_date, 'Number': phone_number, 'Range': phone_range}
            headers = {
                'Accept': 'text/html, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.base_url,
                'Referer': f"{self.base_url}/portal/sms/received"
            }
            response = self.scraper.post(f"{self.base_url}/portal/sms/received/getsms/number/sms", data=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                html_content = self.decompress_response(response)
                soup = BeautifulSoup(html_content, 'html.parser')
                message = soup.select_one(".col-9.col-sm-6 p").text.strip() if soup.select_one(".col-9.col-sm-6 p") else None
                return message
            return None
        except Exception as e:
            logger.error(f"Error getting OTP message: {e}")
            return None

    def get_all_otp_messages(self, sms_details, from_date="", to_date="", limit=None):
        all_otp_messages = []
        for detail in sms_details:
            phone_range = detail['country_number']
            number_details = self.get_sms_details(phone_range, from_date, to_date)
            if number_details:
                for number_detail in number_details:
                    if limit is not None and len(all_otp_messages) >= limit:
                        return all_otp_messages
                    phone_number = number_detail['phone_number']
                    otp_message = self.get_otp_message(phone_number, phone_range, from_date, to_date)
                    if otp_message:
                        all_otp_messages.append({
                            'range': phone_range,
                            'phone_number': phone_number,
                            'otp_message': otp_message
                        })
        return all_otp_messages

app = Flask(__name__)
client = IVASSMSClient()

with app.app_context():
    if not client.login_with_cookies():
        logger.error("Failed to login with cookies")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/sms')
def api_sms():
    date_str = request.args.get('date')
    if not date_str:
        date_str = datetime.now().strftime('%d/%m/%Y')
    try:
        datetime.strptime(date_str, '%d/%m/%Y')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use DD/MM/YYYY'}), 400

    to_date = request.args.get('to_date', '')
    if to_date:
        try:
            datetime.strptime(to_date, '%d/%m/%Y')
        except ValueError:
            return jsonify({'error': 'Invalid to_date format'}), 400

    limit = request.args.get('limit')
    if limit:
        try:
            limit = int(limit)
            if limit <= 0:
                return jsonify({'error': 'Limit must be positive'}), 400
        except ValueError:
            return jsonify({'error': 'Limit must be integer'}), 400

    if not client.logged_in:
        return jsonify({'error': 'Not authenticated'}), 401

    result = client.check_otps(from_date=date_str, to_date=to_date)
    if not result:
        return jsonify({'error': 'Failed to fetch OTP data'}), 500

    otp_messages = client.get_all_otp_messages(result['sms_details'], from_date=date_str, to_date=to_date, limit=limit)

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
