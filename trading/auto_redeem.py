"""
Auto-Redeem: On-chain redemption of resolved Polymarket positions.

When a prediction market resolves, your conditional tokens (position shares)
need to be redeemed back to USDC. Polymarket auto-settles eventually, but
it can take 10+ minutes — leaving your balance at $0 while profits are locked.

This module supports TWO redemption methods:

1. **Direct on-chain** (default): Signs a Gnosis Safe `execTransaction` and
   submits it directly to Polygon.  Works with proxy wallets (sig_type=2).
   Costs ~0.004 POL gas per redemption.  Requires POLY_PRIVATE_KEY only.

2. **Gasless builder relayer** (optional): Uses Polymarket's builder relayer
   for zero-gas redemption.  Requires POLY_BUILDER_* credentials.

The bot auto-detects which method is available and uses the best one.

Required:  POLY_PRIVATE_KEY (already set for trading)
Optional:  POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE

Dependencies: web3, eth-abi, eth-account, requests
"""

import asyncio
import logging
import os
import time
import traceback
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ─── Contract addresses on Polygon Mainnet ───
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Polygon RPC endpoints (fallback list)
DEFAULT_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]

# Gnosis Safe nonce() selector
SAFE_NONCE_SELECTOR = "0xaffed0e0"

# EIP-712 typehashes for Gnosis Safe v1.3.0
SAFE_TX_TYPEHASH = bytes.fromhex(
    "bb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8"
)
DOMAIN_SEPARATOR_TYPEHASH = bytes.fromhex(
    "47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
)


class AutoRedeemer:
    """Auto-redemption of resolved Polymarket positions via Gnosis Safe."""

    def __init__(self, clob_client, sig_type: int = 0):
        self.clob_client = clob_client
        self.sig_type = sig_type
        self._last_check = 0.0
        self._check_interval = 120.0   # Check every 2 minutes
        self._redeemed_conditions: Set[str] = set()
        self._init_errors: List[str] = []
        self._enabled = False
        self._method = "none"   # "direct" | "relayer" | "direct_eoa" | "none"
        self._total_redeemed = 0
        self._total_usd_recovered = 0.0
        self._private_key = ""
        self._proxy_wallet = ""
        self._signer_address = ""
        self._w3 = None
        self._relayer = None

    # ─── Initialization ───────────────────────────────────────────────

    def init(self) -> bool:
        """Initialize auto-redeemer.  Returns True if any method is available."""
        from config import Config

        pk = (Config.POLY_PRIVATE_KEY or "").strip()
        if not pk:
            self._init_errors.append("No POLY_PRIVATE_KEY")
            return False
        if not pk.startswith("0x"):
            pk = "0x" + pk
        self._private_key = pk

        self._proxy_wallet = (Config.POLY_PROXY_WALLET or "").strip()

        try:
            from eth_account import Account
            acct = Account.from_key(pk)
            self._signer_address = acct.address
            if not self._proxy_wallet:
                self._proxy_wallet = acct.address
        except Exception as e:
            self._init_errors.append(f"Key error: {e}")
            return False

        # ── Method 1: Gasless builder relayer (if credentials provided) ──
        bk = os.getenv("POLY_BUILDER_API_KEY", "").strip()
        bs = os.getenv("POLY_BUILDER_SECRET", "").strip()
        bp = os.getenv("POLY_BUILDER_PASSPHRASE", "").strip()
        if bk and bs and bp:
            try:
                from py_builder_relayer_client.client import RelayClient
                from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
                builder_config = BuilderConfig(
                    local_builder_creds=BuilderApiKeyCreds(key=bk, secret=bs, passphrase=bp)
                )
                self._relayer = RelayClient(
                    relayer_url="https://relayer-v2.polymarket.com",
                    chain_id=137, private_key=pk, builder_config=builder_config,
                )
                self._method = "relayer"
                self._enabled = True
                print("✅ Auto-redeem: gasless builder relayer", flush=True)
                return True
            except Exception as e:
                self._init_errors.append(f"Relayer init: {e}")

        # ── Method 2: Direct on-chain via Gnosis Safe (proxy wallets) ──
        if self.sig_type == 2 and self._proxy_wallet:
            ok = self._init_web3()
            if ok:
                self._method = "direct"
                self._enabled = True
                return True

        # ── Method 3: Direct on-chain for EOA wallets ──
        if self.sig_type != 2:
            ok = self._init_web3()
            if ok:
                self._method = "direct_eoa"
                self._enabled = True
                return True

        if not self._enabled:
            print(f"⚠️ Auto-redeem disabled: "
                  f"{'; '.join(self._init_errors) or 'no method available'}", flush=True)
        return self._enabled

    def _init_web3(self) -> bool:
        """Connect to a working Polygon RPC endpoint."""
        try:
            from web3 import Web3
            from config import Config

            rpc_env = os.getenv("POLYGON_RPC_URL", "").strip()
            rpcs = [rpc_env] + DEFAULT_RPCS if rpc_env else list(DEFAULT_RPCS)

            for rpc_url in rpcs:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 10}))
                    if w3.is_connected():
                        self._w3 = w3
                        print(f"✅ Auto-redeem: direct on-chain "
                              f"({'Safe' if self.sig_type == 2 else 'EOA'}) "
                              f"via {rpc_url[:50]}", flush=True)
                        return True
                except Exception:
                    continue

            self._init_errors.append("No working Polygon RPC")
            return False

        except ImportError as e:
            self._init_errors.append(f"web3 not installed: {e}")
            return False

    # Backward-compatible alias
    def init_relayer(self) -> bool:
        return self.init()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    # ─── Main check loop ──────────────────────────────────────────────

    async def check_and_redeem(self) -> Dict:
        """Check for unredeemed resolved positions and redeem them."""
        if not self._enabled:
            return {"redeemed": 0, "total_redeemed_usd": 0}

        now = time.time()
        if now - self._last_check < self._check_interval:
            return {"redeemed": 0, "total_redeemed_usd": 0}
        self._last_check = now

        try:
            positions = await self._get_conditional_positions()
            if not positions:
                return {"redeemed": 0, "total_redeemed_usd": 0}

            redeemed = 0
            failed = 0
            total_usd = 0.0

            for token_id, balance in positions.items():
                if balance <= 0:
                    continue

                market_info = await self._get_market_for_token(token_id)
                if not market_info:
                    continue

                condition_id = market_info.get("condition_id",
                                market_info.get("conditionId", ""))
                if not condition_id:
                    continue
                if condition_id in self._redeemed_conditions:
                    continue

                # Must be resolved / closed
                closed = market_info.get("closed", False)
                resolved = market_info.get("resolved", False)
                if not (closed or resolved):
                    continue

                neg_risk = market_info.get("neg_risk",
                            market_info.get("negRisk", False))
                title = market_info.get("question",
                         market_info.get("title", condition_id[:16]))

                print(f"💰 Auto-redeem: {title[:50]}... "
                      f"({balance:.2f} tokens, neg_risk={neg_risk})", flush=True)

                ok = await self._redeem(condition_id, neg_risk)
                if ok:
                    self._redeemed_conditions.add(condition_id)
                    redeemed += 1
                    self._total_redeemed += 1
                    total_usd += balance
                    self._total_usd_recovered += balance
                    print(f"✅ Redeemed! Session total: {self._total_redeemed} "
                          f"(~${self._total_usd_recovered:.2f})", flush=True)
                else:
                    failed += 1
                    print(f"⚠️ Redeem failed: {condition_id[:16]}...", flush=True)

                # Wait between redeems so nonce clears on-chain
                await asyncio.sleep(5)

            return {"redeemed": redeemed, "failed": failed,
                    "total_redeemed_usd": total_usd}

        except Exception as e:
            logger.error("Auto-redeem error: %s", e)
            print(f"⚠️ Auto-redeem error: {e}", flush=True)
            return {"redeemed": 0, "total_redeemed_usd": 0}

    # ─── Position discovery ───────────────────────────────────────────

    async def _get_conditional_positions(self) -> Dict[str, float]:
        """Find all conditional token positions on the wallet."""
        try:
            import requests

            address = self._proxy_wallet or self._signer_address
            if not address:
                return {}

            positions = {}

            # Gamma API
            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/positions",
                    params={"user": address.lower()},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for pos in data:
                            token_id = pos.get("asset", pos.get("token_id", ""))
                            size = float(pos.get("size", pos.get("balance", 0)))
                            if token_id and size > 0:
                                positions[token_id] = size
                    if positions:
                        return positions
            except Exception:
                pass

            # Fallback: data-api
            try:
                resp = requests.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": address.lower()},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for pos in data:
                            token_id = pos.get("asset", pos.get("token_id", ""))
                            size = float(pos.get("size", pos.get("balance", 0)))
                            if token_id and size > 0:
                                positions[token_id] = size
            except Exception:
                pass

            return positions

        except Exception as e:
            logger.error("Get positions failed: %s", e)
            return {}

    async def _get_market_for_token(self, token_id: str) -> Optional[Dict]:
        """Look up market info for a token ID via Gamma API."""
        try:
            import requests

            resp = requests.get(
                f"{GAMMA_API_URL}/markets",
                params={"clob_token_ids": token_id},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]

            # Fallback parameter name
            resp = requests.get(
                f"{GAMMA_API_URL}/markets",
                params={"token_id": token_id},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
        except Exception as e:
            logger.debug("Market lookup failed: %s", e)
        return None

    # ─── Redemption dispatch ──────────────────────────────────────────

    async def _redeem(self, condition_id: str, neg_risk: bool) -> bool:
        """Redeem using the best available method."""
        if self._method == "relayer":
            return await self._redeem_via_relayer(condition_id, neg_risk)
        elif self._method == "direct":
            return await self._redeem_via_safe(condition_id, neg_risk)
        elif self._method == "direct_eoa":
            return await self._redeem_via_eoa(condition_id, neg_risk)
        return False

    @staticmethod
    def _build_redeem_calldata(condition_id: str, neg_risk: bool) -> tuple:
        """Build redeemPositions calldata + target address.
        
        Returns: (calldata_bytes, target_address_str)
        """
        from web3 import Web3
        from eth_abi import encode

        cond_bytes = bytes.fromhex(
            condition_id[2:] if condition_id.startswith("0x") else condition_id
        )

        if neg_risk:
            selector = Web3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
            params = encode(["bytes32", "uint256[]"],
                            [cond_bytes, [2**256 - 1, 2**256 - 1]])
            return selector + params, NEG_RISK_ADAPTER
        else:
            selector = Web3.keccak(
                text="redeemPositions(address,bytes32,bytes32,uint256[])"
            )[:4]
            params = encode(
                ["address", "bytes32", "bytes32", "uint256[]"],
                [USDC_ADDRESS, b"\x00" * 32, cond_bytes, [1, 2]]
            )
            return selector + params, CTF_ADDRESS

    # ─── Method 1: Gasless builder relayer ────────────────────────────

    async def _redeem_via_relayer(self, condition_id: str, neg_risk: bool) -> bool:
        """Redeem via Polymarket builder relayer (gasless, needs POLY_BUILDER_*)."""
        if not self._relayer:
            return False
        try:
            from py_builder_relayer_client.models import SafeTransaction, OperationType

            calldata, target = self._build_redeem_calldata(condition_id, neg_risk)

            tx = SafeTransaction(
                to=target, operation=OperationType.Call,
                data="0x" + calldata.hex(), value="0",
            )

            resp = self._relayer.execute([tx], "Redeem positions")
            tx_id = getattr(resp, 'transaction_id', str(resp))
            print(f"  📨 Relayer submitted: {tx_id}", flush=True)

            for _ in range(20):
                await asyncio.sleep(3)
                try:
                    status = self._relayer.get_transaction(tx_id)
                    if isinstance(status, list):
                        status = status[0] if status else {}
                    state = (status.get("state", "") if isinstance(status, dict)
                             else str(status))
                    if "CONFIRMED" in state.upper():
                        return True
                    if "FAILED" in state.upper() or "INVALID" in state.upper():
                        return False
                except Exception:
                    continue

            print("  ⏰ Relayer timeout — may still confirm on-chain", flush=True)
            return False

        except Exception as e:
            print(f"  ❌ Relayer redeem error: {e}", flush=True)
            return False

    # ─── Generic Safe transaction execution ─────────────────────────

    async def _execute_via_safe(self, target: str, inner_data: bytes,
                                gas_limit: int = 300_000,
                                label: str = "Safe tx") -> bool:
        """Execute an arbitrary call through the Gnosis Safe.

        Signs an EIP-712 Safe transaction and submits on-chain.
        Costs ~0.004 POL gas.  Used for redeem, setApprovalForAll, etc.
        """
        if not self._w3:
            return False

        try:
            from web3 import Web3
            from eth_account import Account

            w3 = self._w3
            safe_addr = Web3.to_checksum_address(self._proxy_wallet)
            target = Web3.to_checksum_address(target)

            # ── Get Safe nonce ──
            nonce = await self._get_safe_nonce(safe_addr)
            if nonce is None:
                print(f"  ❌ Could not read Safe nonce", flush=True)
                return False

            # ── Compute EIP-712 Safe transaction hash ──
            zero_addr = "0x" + "00" * 20
            safe_tx_hash = self._compute_safe_tx_hash(
                safe_addr, target, 0, inner_data, 0,
                0, 0, 0, zero_addr, zero_addr, nonce,
            )

            # ── ECDSA sign ──
            acct = Account.from_key(self._private_key)
            signed = acct.unsafe_sign_hash(safe_tx_hash)
            sig_bytes = (signed.r.to_bytes(32, 'big') +
                         signed.s.to_bytes(32, 'big') +
                         bytes([signed.v]))

            # ── Build Safe.execTransaction call ──
            exec_abi = [{
                "name": "execTransaction", "type": "function",
                "inputs": [
                    {"name": "to",             "type": "address"},
                    {"name": "value",          "type": "uint256"},
                    {"name": "data",           "type": "bytes"},
                    {"name": "operation",      "type": "uint8"},
                    {"name": "safeTxGas",      "type": "uint256"},
                    {"name": "baseGas",        "type": "uint256"},
                    {"name": "gasPrice",       "type": "uint256"},
                    {"name": "gasToken",       "type": "address"},
                    {"name": "refundReceiver", "type": "address"},
                    {"name": "signatures",     "type": "bytes"},
                ],
                "outputs": [{"name": "", "type": "bool"}],
            }]

            safe = w3.eth.contract(address=safe_addr, abi=exec_abi)
            gas_price = w3.eth.gas_price
            max_fee = max(gas_price * 2, w3.to_wei(35, 'gwei'))
            priority_fee = min(w3.to_wei(30, 'gwei'), max_fee - 1)

            # Use 'pending' nonce to avoid "replacement transaction underpriced"
            # when a previous tx is still in the mempool
            eoa_nonce = w3.eth.get_transaction_count(acct.address, 'pending')

            tx_data = safe.functions.execTransaction(
                target, 0, inner_data, 0,
                0, 0, 0, zero_addr, zero_addr, sig_bytes,
            ).build_transaction({
                'from': acct.address,
                'nonce': eoa_nonce,
                'gas': gas_limit,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'chainId': 137,
            })

            signed_tx = w3.eth.account.sign_transaction(tx_data, self._private_key)
            try:
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            except Exception as send_err:
                err_msg = str(send_err).lower()
                if 'replacement' in err_msg or 'underpriced' in err_msg or 'nonce' in err_msg:
                    print(f"  ⏳ Nonce conflict, waiting for pending tx to clear...",
                          flush=True)
                    await asyncio.sleep(15)
                    # Retry with fresh nonce
                    new_nonce = w3.eth.get_transaction_count(acct.address, 'pending')
                    tx_data['nonce'] = new_nonce
                    signed_tx = w3.eth.account.sign_transaction(tx_data, self._private_key)
                    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                else:
                    raise

            print(f"  📨 {label}: {tx_hash.hex()}", flush=True)

            # ── Wait for receipt (try multiple RPCs if primary times out) ──
            receipt = await self._wait_for_receipt(tx_hash, eoa_nonce, acct.address)

            if receipt is None:
                print(f"  ⚠️ {label}: tx sent but receipt unconfirmed "
                      f"(may still land on-chain)", flush=True)
                return False

            if receipt['status'] == 1:
                print(f"  ✅ {label} confirmed (gas: {receipt['gasUsed']}, "
                      f"block {receipt['blockNumber']})", flush=True)
                return True
            else:
                print(f"  ❌ {label} reverted (block {receipt['blockNumber']})", flush=True)
                return False

        except Exception as e:
            print(f"  ❌ {label} error: {e}", flush=True)
            traceback.print_exc()
            return False

    async def _wait_for_receipt(self, tx_hash, eoa_nonce: int,
                                 eoa_address: str,
                                 timeout: int = 180) -> Optional[dict]:
        """Wait for tx receipt, trying multiple RPCs on timeout.

        If the primary RPC can't find the receipt, fall back to other RPCs.
        Also checks if the nonce has advanced (meaning the tx WAS mined even
        if we can't get the specific receipt).
        """
        from web3 import Web3

        loop = asyncio.get_event_loop()

        # ── Try primary RPC first (180s) ──
        try:
            receipt = await loop.run_in_executor(
                None,
                lambda: self._w3.eth.wait_for_transaction_receipt(
                    tx_hash, timeout=timeout
                )
            )
            return receipt
        except Exception as primary_err:
            print(f"  ⏳ Primary RPC timeout ({timeout}s), trying fallbacks...",
                  flush=True)

        # ── Try each fallback RPC ──
        for rpc_url in DEFAULT_RPCS:
            try:
                fallback_w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={
                    'timeout': 30
                }))
                receipt = fallback_w3.eth.get_transaction_receipt(tx_hash)
                if receipt:
                    print(f"  ✅ Receipt found via {rpc_url}", flush=True)
                    return receipt
            except Exception:
                continue

        # ── Check if nonce advanced (tx was mined but receipt unavailable) ──
        try:
            current_nonce = self._w3.eth.get_transaction_count(eoa_address)
            if current_nonce > eoa_nonce:
                print(f"  ⚠️ Nonce advanced ({eoa_nonce}→{current_nonce}), "
                      f"tx likely mined but receipt unavailable", flush=True)
                # Return a synthetic success — the tx DID execute
                return {'status': 1, 'gasUsed': 0, 'blockNumber': 0}
        except Exception:
            pass

        return None

    # ─── Method 2: Direct on-chain via Gnosis Safe execTransaction ───

    async def _redeem_via_safe(self, condition_id: str, neg_risk: bool) -> bool:
        """Redeem via Safe.execTransaction → CTF.redeemPositions."""
        inner_data, target_str = self._build_redeem_calldata(condition_id, neg_risk)
        return await self._execute_via_safe(
            target_str, inner_data, gas_limit=300_000, label="Redeem"
        )

    async def _get_safe_nonce(self, safe_addr: str) -> Optional[int]:
        """Read the current nonce from the Gnosis Safe contract."""
        try:
            result = self._w3.eth.call({
                'to': safe_addr,
                'data': SAFE_NONCE_SELECTOR,
            })
            return int.from_bytes(result, 'big')
        except Exception as e:
            print(f"  ⚠️ Safe nonce read error: {e}", flush=True)
            return None

    def _compute_safe_tx_hash(self, safe_addr: str, to: str, value: int,
                               data: bytes, operation: int,
                               safe_tx_gas: int, base_gas: int, gas_price: int,
                               gas_token: str, refund_receiver: str,
                               nonce: int) -> bytes:
        """Compute the EIP-712 Safe transaction hash for signing.

        Matches GnosisSafe.getTransactionHash() exactly.
        """
        from web3 import Web3

        # Step 1: Struct hash
        data_hash = Web3.keccak(data)
        encoded = (
            SAFE_TX_TYPEHASH +
            bytes.fromhex(to[2:].lower().zfill(64)) +
            value.to_bytes(32, 'big') +
            data_hash +
            operation.to_bytes(32, 'big') +
            safe_tx_gas.to_bytes(32, 'big') +
            base_gas.to_bytes(32, 'big') +
            gas_price.to_bytes(32, 'big') +
            bytes.fromhex(gas_token[2:].lower().zfill(64)) +
            bytes.fromhex(refund_receiver[2:].lower().zfill(64)) +
            nonce.to_bytes(32, 'big')
        )
        safe_tx_hash = Web3.keccak(encoded)

        # Step 2: Domain separator
        domain_data = (
            DOMAIN_SEPARATOR_TYPEHASH +
            (137).to_bytes(32, 'big') +     # chainId = Polygon
            bytes.fromhex(safe_addr[2:].lower().zfill(64))
        )
        domain_separator = Web3.keccak(domain_data)

        # Step 3: EIP-712 final hash = keccak256("\x19\x01" || domainSep || structHash)
        return Web3.keccak(b"\x19\x01" + domain_separator + safe_tx_hash)

    # ─── Method 3: Direct EOA (no Safe wrapper) ──────────────────────

    async def _redeem_via_eoa(self, condition_id: str, neg_risk: bool) -> bool:
        """Redeem directly on CTF contract (for EOA wallets, not proxy)."""
        if not self._w3:
            return False

        try:
            from web3 import Web3
            from eth_account import Account

            w3 = self._w3
            acct = Account.from_key(self._private_key)
            calldata, target_str = self._build_redeem_calldata(condition_id, neg_risk)
            target = Web3.to_checksum_address(target_str)

            gas_price = w3.eth.gas_price
            max_fee = max(gas_price * 2, w3.to_wei(35, 'gwei'))
            priority_fee = min(w3.to_wei(30, 'gwei'), max_fee - 1)

            tx = {
                'from': acct.address,
                'to': target,
                'data': '0x' + calldata.hex(),
                'nonce': w3.eth.get_transaction_count(acct.address),
                'gas': 200_000,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'chainId': 137,
                'value': 0,
            }

            signed_tx = w3.eth.account.sign_transaction(tx, self._private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"  📨 EOA tx sent: {tx_hash.hex()}", flush=True)

            loop = asyncio.get_event_loop()
            receipt = await loop.run_in_executor(
                None,
                lambda: w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
            )

            if receipt['status'] == 1:
                print(f"  ✅ EOA redeem confirmed (gas: {receipt['gasUsed']})", flush=True)
                return True
            else:
                print(f"  ❌ EOA redeem reverted", flush=True)
                return False

        except Exception as e:
            print(f"  ❌ EOA redeem error: {e}", flush=True)
            return False

    # ─── CTF Exchange Approval ─────────────────────────────────────────

    async def ensure_ctf_approval(self) -> bool:
        """Check and fix CTF exchange approval for selling.

        Sell orders require the CTF contract to have `setApprovalForAll`
        for both the Normal and NegRisk exchanges.  If not approved, this
        method sends the approval transactions on-chain via the Safe.

        Returns True if all approvals are OK (already set or newly set).
        """
        if not self._w3 or self._method not in ("direct", "direct_eoa"):
            return False

        from web3 import Web3
        from eth_abi import encode

        CTF = Web3.to_checksum_address(CTF_ADDRESS)
        owner = Web3.to_checksum_address(self._proxy_wallet)

        EXCHANGES = {
            'Normal':  '0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E',
            'NegRisk': '0xC5d563A36AE78145C45a50134d48A1215220f80a',
        }

        # isApprovedForAll(address,address) selector = 0xe985e9c5
        all_ok = True
        for label, exchange in EXCHANGES.items():
            try:
                owner_pad = owner.lower().replace('0x', '').zfill(64)
                op_pad = exchange.lower().replace('0x', '').zfill(64)
                result = self._w3.eth.call({
                    'to': CTF,
                    'data': f'0xe985e9c5{owner_pad}{op_pad}',
                })
                approved = int.from_bytes(result, 'big') == 1

                if approved:
                    print(f"✅ CTF approval ({label}): approved", flush=True)
                    continue

                # Not approved — send setApprovalForAll(operator, true)
                print(f"⚠️ CTF approval ({label}): NOT approved — fixing on-chain...",
                      flush=True)

                # setApprovalForAll(address,bool) selector = 0xa22cb465
                selector = bytes.fromhex('a22cb465')
                calldata = selector + encode(
                    ['address', 'bool'],
                    [Web3.to_checksum_address(exchange), True]
                )

                if self._method == "direct":
                    ok = await self._execute_via_safe(
                        CTF_ADDRESS, calldata, gas_limit=120_000,
                        label=f"Approve {label}"
                    )
                else:
                    ok = await self._execute_eoa_call(
                        CTF_ADDRESS, calldata, gas_limit=120_000,
                        label=f"Approve {label}"
                    )

                if ok:
                    print(f"✅ CTF approval ({label}): now approved!", flush=True)
                else:
                    print(f"❌ CTF approval ({label}): tx failed!", flush=True)
                    all_ok = False

            except Exception as e:
                print(f"⚠️ CTF approval check ({label}): {e}", flush=True)
                all_ok = False

        return all_ok

    async def _execute_eoa_call(self, target: str, calldata: bytes,
                                gas_limit: int = 200_000,
                                label: str = "EOA tx") -> bool:
        """Execute a direct EOA call (no Safe wrapper)."""
        if not self._w3:
            return False
        try:
            from web3 import Web3
            from eth_account import Account
            w3 = self._w3
            acct = Account.from_key(self._private_key)
            gas_price = w3.eth.gas_price
            max_fee = max(gas_price * 2, w3.to_wei(35, 'gwei'))
            priority_fee = min(w3.to_wei(30, 'gwei'), max_fee - 1)
            tx = {
                'from': acct.address,
                'to': Web3.to_checksum_address(target),
                'data': '0x' + calldata.hex(),
                'nonce': w3.eth.get_transaction_count(acct.address),
                'gas': gas_limit,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'chainId': 137,
                'value': 0,
            }
            signed_tx = w3.eth.account.sign_transaction(tx, self._private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"  📨 {label}: {tx_hash.hex()}", flush=True)
            loop = asyncio.get_event_loop()
            receipt = await loop.run_in_executor(
                None,
                lambda: w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
            )
            if receipt['status'] == 1:
                print(f"  ✅ {label} confirmed (gas: {receipt['gasUsed']})", flush=True)
                return True
            else:
                print(f"  ❌ {label} reverted", flush=True)
                return False
        except Exception as e:
            print(f"  ❌ {label} error: {e}", flush=True)
            return False

    # ─── Public helpers ───────────────────────────────────────────────

    async def force_check(self) -> Dict:
        """Force an immediate check (ignores cooldown). For /redeem command."""
        self._last_check = 0
        return await self.check_and_redeem()

    def get_status(self) -> Dict:
        """Get auto-redeem status for debug/status commands."""
        return {
            "enabled": self._enabled,
            "method": self._method,
            "total_redeemed": self._total_redeemed,
            "total_usd_recovered": self._total_usd_recovered,
            "tracked_redeemed": len(self._redeemed_conditions),
            "last_check": self._last_check,
            "check_interval": self._check_interval,
            "proxy_wallet": (self._proxy_wallet[:10] + "..."
                             if self._proxy_wallet else "none"),
            "signer": (self._signer_address[:10] + "..."
                       if self._signer_address else "none"),
            "errors": self._init_errors[-3:] if self._init_errors else [],
        }
