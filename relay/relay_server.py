"""
Simple Python CLOB API Relay for Polymarket

Run this on any VPS in an allowed region (Singapore, UK, Germany, Japan).
Forwards signed order requests to clob.polymarket.com.

Best free VPS options:
  - Oracle Cloud Free Tier (FOREVER free, Singapore/Tokyo available)
  - Google Cloud Free f1-micro (but only us-central1)

Usage:
  pip install flask requests gunicorn
  gunicorn relay_server:app -b 0.0.0.0:8080

Then set CLOB_RELAY_URL=http://your-vps-ip:8080 in your bot's .env
"""

import os
from flask import Flask, request, Response
import requests as req

app = Flask(__name__)

TARGET = 'https://clob.polymarket.com'
AUTH_TOKEN = os.getenv('RELAY_AUTH_TOKEN', '')  # Optional security


@app.before_request
def check_auth():
    """Optional: verify relay auth token."""
    if AUTH_TOKEN:
        token = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
        if token != AUTH_TOKEN:
            return Response('{"error": "Unauthorized"}', status=401, mimetype='application/json')


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
def proxy(path):
    """Forward all requests to Polymarket CLOB API."""
    
    # CORS preflight
    if request.method == 'OPTIONS':
        return Response('', status=204, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': '*',
        })
    
    # Build target URL
    url = f"{TARGET}/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
    
    # Forward headers (skip hop-by-hop and relay auth)
    skip_headers = {
        'host', 'authorization', 'x-forwarded-for', 
        'x-real-ip', 'cf-connecting-ip', 'cf-ipcountry',
        'transfer-encoding',
    }
    headers = {
        k: v for k, v in request.headers 
        if k.lower() not in skip_headers
    }
    headers['Host'] = 'clob.polymarket.com'
    
    try:
        resp = req.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            timeout=30,
            allow_redirects=False,
        )
        
        # Forward response
        excluded_headers = {'transfer-encoding', 'content-encoding', 'connection'}
        response_headers = {
            k: v for k, v in resp.headers.items() 
            if k.lower() not in excluded_headers
        }
        response_headers['X-Relay'] = '5min-relay'
        
        return Response(
            resp.content,
            status=resp.status_code,
            headers=response_headers,
        )
    except Exception as e:
        return Response(
            f'{{"error": "Relay error", "detail": "{str(e)}"}}',
            status=502,
            mimetype='application/json',
        )


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    print(f"🔄 Polymarket CLOB relay starting on port {port}")
    app.run(host='0.0.0.0', port=port)
