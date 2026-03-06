import requests
from web3 import Web3
from datetime import datetime
import json

# --- CONFIG ---
PROXY_ADDRESS = "0xd8f8c13644ea84d62e1ec88c5d1215e436eb0f11"  # Weather prediction user (automatedAItradingbot)
OUTPUT_JSON = "polymarket_profile_data.json"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"  # Free public RPC

# --- 1. Fetch open positions from Polymarket Data API ---
def fetch_open_positions(address):
    url = f"https://data-api.polymarket.com/positions?user={address}"
    resp = requests.get(url, timeout=15)
    if resp.status_code == 200:
        return resp.json()
    return []

# --- 2. Fetch all transactions for the address from Polygonscan API (free, limited) ---
def fetch_transactions(address, api_key=None):
    url = f"https://api.polygonscan.com/api?module=account&action=txlist&address={address}&sort=asc"
    if api_key:
        url += f"&apikey={api_key}"
    resp = requests.get(url, timeout=15)
    if resp.status_code == 200:
        return resp.json().get('result', [])
    return []

# --- 3. Parse ConditionalTokens events (Transfer, Redeem) ---
# (For brevity, only fetches logs for ConditionalTokens contract)
CONDITIONAL_TOKENS = "0xCeAfDD6bc0bEF976fdCd1112955828E00543c0Ce"
TRANSFER_EVENT_SIG = Web3.keccak(text="Transfer(address,address,uint256)").hex()
REDEEM_EVENT_SIG = Web3.keccak(text="Redemption(address,uint256)").hex()

def fetch_conditional_token_events(address, from_block=0, to_block='latest'):
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    logs = w3.eth.get_logs({
        'fromBlock': from_block,
        'toBlock': to_block,
        'address': Web3.to_checksum_address(CONDITIONAL_TOKENS),
        'topics': [TRANSFER_EVENT_SIG, None, Web3.to_hex(Web3.to_bytes(hexstr=address))]
    })
    return logs

# --- MAIN ---
if __name__ == "__main__":
    print(f"Analyzing Polymarket profile: {PROXY_ADDRESS}")
    print("\n--- Open Positions (Data API) ---")
    positions = fetch_open_positions(PROXY_ADDRESS)

    # Validate and collect open positions
    open_positions = []
    if not positions:
        print("No open positions found.")
    else:
        for pos in positions:
            # Validate required fields
            if not all(k in pos for k in ("title", "outcome", "size", "avgPrice")):
                print(f"Warning: Skipping incomplete position: {pos}")
                continue
            open_positions.append({
                "market": pos.get("title", "")[:120],
                "outcome": pos.get("outcome", ""),
                "shares": pos.get("size", 0),
                "avg_price": pos.get("avgPrice", 0),
                "pnl": pos.get("pnl", 0),
                "token_id": pos.get("asset", pos.get("tokenId", pos.get("token", ""))),
                "condition_id": pos.get("conditionId", pos.get("condition_id", "")),
            })
            print(f"Market: {pos.get('title','')[:60]} | Outcome: {pos.get('outcome','')} | Shares: {pos.get('size')} | Avg Price: {pos.get('avgPrice')} | PnL: {pos.get('pnl',0):.2f}")
        print(f"\nTotal open positions: {len(open_positions)}")


    print("\n--- On-chain Transactions (Polygonscan) ---")
    txs = fetch_transactions(PROXY_ADDRESS)
    valid_txs = []
    if not isinstance(txs, list):
        print("Error: Unexpected response from Polygonscan API.")
    elif not txs or (len(txs) == 1 and 'isError' in txs[0]):
        print("No transactions found or Polygonscan API error.")
    else:
        print(f"Total transactions: {len(txs)}")
        count = 0
        for tx in reversed(txs):
            if 'timeStamp' in tx and 'hash' in tx:
                try:
                    tx_data = {
                        "timestamp": int(tx['timeStamp']),
                        "datetime": datetime.utcfromtimestamp(int(tx['timeStamp'])).isoformat(),
                        "hash": tx['hash'],
                        "from": tx.get('from', ''),
                        "to": tx.get('to', ''),
                        "value": tx.get('value', ''),
                        "input": tx.get('input', ''),
                        "isError": tx.get('isError', '0'),
                        "txType": "buy" if tx.get('to', '').lower() == PROXY_ADDRESS.lower() else "sell"
                    }
                    valid_txs.append(tx_data)
                    if count < 5:
                        print(f"{tx_data['datetime']} | {tx_data['hash']} | {tx_data['value']} wei | {tx_data['input'][:10]}... | {tx_data['txType']}")
                    count += 1
                except Exception:
                    continue

    # Optional: fetch ConditionalTokens events (advanced, may hit RPC limits)
    # logs = fetch_conditional_token_events(PROXY_ADDRESS)
    # print(f"\nConditionalTokens events found: {len(logs)}")

    # --- Write all data to JSON file ---
    output = {
        "address": PROXY_ADDRESS,
        "open_positions": open_positions,
        "transactions": valid_txs
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nAll data written to {OUTPUT_JSON}")
    print("\nDone.")
