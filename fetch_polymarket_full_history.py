import requests
import json
from datetime import datetime

# --- CONFIG ---
USER_ADDRESS = "0xd8f8c13644ea84d62e1ec88c5d1215e436eb0f11"  # Change as needed
CLOB_API = "https://clob.polymarket.com/api/trades"  # Official CLOB endpoint
OUTPUT_JSON = "polymarket_full_history.json"

# --- 1. Fetch all trades for the user from CLOB API ---
def fetch_all_trades(address):
    trades = []
    page = 1
    while True:
        url = f"{CLOB_API}?user={address}&page={page}&limit=100"
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            break
        data = resp.json()
        if not data or not isinstance(data, list):
            break
        trades.extend(data)
        if len(data) < 100:
            break
        page += 1
    return trades

# --- 2. Reconstruct positions and trade history ---
def reconstruct_positions(trades):
    agg = {}
    trade_history = []
    for trade in trades:
        token_id = trade.get('asset_id', trade.get('tokenID', ''))
        if not token_id:
            continue
        side = trade.get('side', 'BUY').upper()
        size = float(trade.get('size', 0))
        price = float(trade.get('price', 0))
        ts = int(trade.get('timestamp', trade.get('created_at', 0)))
        dt = datetime.utcfromtimestamp(ts).isoformat() if ts else ''
        trade_history.append({
            'token_id': token_id,
            'condition_id': trade.get('market', trade.get('conditionId', '')),
            'side': side,
            'size': size,
            'price': price,
            'datetime': dt,
            'raw': trade
        })
        if token_id not in agg:
            agg[token_id] = {'size': 0, 'cost': 0, 'trades': []}
        if side == 'BUY':
            agg[token_id]['cost'] += size * price
            agg[token_id]['size'] += size
        else:
            agg[token_id]['size'] -= size
            agg[token_id]['cost'] -= size * price
        agg[token_id]['trades'].append({'side': side, 'size': size, 'price': price, 'datetime': dt})
    # Build positions
    positions = []
    for token_id, info in agg.items():
        avg_price = info['cost'] / info['size'] if info['size'] > 0 else 0
        positions.append({
            'token_id': token_id,
            'net_size': info['size'],
            'avg_price': avg_price,
            'trade_count': len(info['trades']),
            'trades': info['trades']
        })
    return positions, trade_history

if __name__ == "__main__":
    print(f"Fetching all trades for {USER_ADDRESS} from CLOB API...")
    trades = fetch_all_trades(USER_ADDRESS)
    print(f"Fetched {len(trades)} trades.")
    positions, trade_history = reconstruct_positions(trades)
    print(f"Reconstructed {len(positions)} positions.")
    # Write to JSON
    output = {
        'address': USER_ADDRESS,
        'positions': positions,
        'trade_history': trade_history
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"All trade history and reconstructed positions written to {OUTPUT_JSON}")
