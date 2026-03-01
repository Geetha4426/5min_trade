"""
Auto-Redeem: Gasless redemption of resolved Polymarket positions.

When a prediction market resolves, your conditional tokens (position shares)
need to be redeemed back to USDC. Polymarket auto-settles eventually, but it
can be slow — leaving your balance at $0 while profits are locked.

This module uses Polymarket's Builder Relayer for gasless redemption:
- Scans for conditional token balances on your wallet
- Checks if the corresponding markets are resolved
- Submits gasless redeem transactions via the builder relayer
- Polls for confirmation

Required env vars (optional — without them auto-redeem is disabled):
  POLY_BUILDER_API_KEY
  POLY_BUILDER_SECRET
  POLY_BUILDER_PASSPHRASE

Also requires: POLY_PRIVATE_KEY (already set for trading)

Dependencies: py-builder-relayer-client, web3, eth-abi
"""

import asyncio
import logging
import os
import time
import traceback
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Contract addresses on Polygon ───
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
GAMMA_API_URL = "https://gamma-api.polymarket.com"


class AutoRedeemer:
    """Gasless auto-redemption of resolved Polymarket positions."""

    def __init__(self, clob_client, sig_type: int = 0):
        """
        Args:
            clob_client: Initialized py_clob_client.ClobClient instance
            sig_type: Signature type (0=EOA, 2=Proxy)
        """
        self.clob_client = clob_client
        self.sig_type = sig_type
        self.relayer = None
        self._last_check = 0.0
        self._check_interval = 60.0  # Check every 60 seconds
        self._redeemed_conditions: set = set()  # Track already-redeemed condition IDs
        self._init_errors: List[str] = []
        self._enabled = False
        self._total_redeemed = 0

    def init_relayer(self) -> bool:
        """Initialize the builder relayer client. Returns True if successful."""
        bk = os.getenv("POLY_BUILDER_API_KEY", "").strip()
        bs = os.getenv("POLY_BUILDER_SECRET", "").strip()
        bp = os.getenv("POLY_BUILDER_PASSPHRASE", "").strip()
        pk = os.getenv("POLY_PRIVATE_KEY", "").strip()

        if not (bk and bs and bp):
            msg = "Auto-redeem DISABLED: no builder relayer credentials"
            logger.info(msg)
            print(f"ℹ️ {msg}", flush=True)
            print("  To enable: set POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE", flush=True)
            return False

        if not pk:
            msg = "Auto-redeem DISABLED: no POLY_PRIVATE_KEY"
            logger.warning(msg)
            return False

        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

            if not pk.startswith("0x"):
                pk = "0x" + pk

            builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=bk, secret=bs, passphrase=bp
                )
            )
            self.relayer = RelayClient(
                relayer_url="https://relayer-v2.polymarket.com",
                chain_id=137,
                private_key=pk,
                builder_config=builder_config,
            )
            self._enabled = True
            print("✅ Auto-redeem enabled (gasless builder relayer)", flush=True)
            return True

        except ImportError as e:
            msg = f"Auto-redeem DISABLED: missing dependency — {e}"
            logger.warning(msg)
            print(f"⚠️ {msg}", flush=True)
            print("  Install: pip install py-builder-relayer-client web3 eth-abi", flush=True)
            self._init_errors.append(str(e))
            return False

        except Exception as e:
            msg = f"Auto-redeem init failed: {e}"
            logger.error(msg)
            print(f"❌ {msg}", flush=True)
            self._init_errors.append(str(e))
            return False

    @property
    def is_enabled(self) -> bool:
        return self._enabled and self.relayer is not None

    async def check_and_redeem(self) -> Dict:
        """
        Check for unredeemed resolved positions and redeem them.
        
        Returns: dict with 'redeemed' count and 'total_redeemed_usd' estimate.
        
        Should be called periodically (e.g. every scan loop iteration).
        Self-throttles to check_interval (default 60s).
        """
        if not self.is_enabled:
            return {"redeemed": 0, "total_redeemed_usd": 0}

        now = time.time()
        if now - self._last_check < self._check_interval:
            return {"redeemed": 0, "total_redeemed_usd": 0}

        self._last_check = now

        try:
            # Step 1: Find all conditional token positions on our wallet
            positions = await self._get_conditional_positions()
            if not positions:
                return {"redeemed": 0, "total_redeemed_usd": 0}

            # Step 2: For each position, check if market is resolved
            redeemed = 0
            total_usd = 0.0
            for token_id, balance in positions.items():
                if balance <= 0:
                    continue

                # Look up market info for this token
                market_info = await self._get_market_for_token(token_id)
                if not market_info:
                    continue

                condition_id = market_info.get("conditionId", "")
                if not condition_id:
                    continue

                # Skip if already redeemed
                if condition_id in self._redeemed_conditions:
                    continue

                # Check if market is resolved (closed=true)
                if not market_info.get("closed", False):
                    continue

                # Market is resolved and we have tokens — REDEEM!
                neg_risk = market_info.get("negRisk", False)
                title = market_info.get("question", condition_id[:16])
                
                print(f"💰 Auto-redeem: {title[:50]} (balance={balance:.1f} shares)", flush=True)
                
                ok = await self._redeem_market(condition_id, neg_risk)
                if ok:
                    self._redeemed_conditions.add(condition_id)
                    redeemed += 1
                    self._total_redeemed += 1
                    # Estimate USDC value (shares * ~$1 each for winning bets)
                    total_usd += balance
                    print(f"✅ Redeemed! Total redeemed this session: {self._total_redeemed}", flush=True)
                else:
                    print(f"⚠️ Redeem failed for {condition_id[:16]}...", flush=True)

            return {"redeemed": redeemed, "total_redeemed_usd": total_usd}

        except Exception as e:
            logger.error("Auto-redeem check failed: %s", e)
            print(f"⚠️ Auto-redeem error: {e}", flush=True)
            return {"redeemed": 0, "total_redeemed_usd": 0}

    async def _get_conditional_positions(self) -> Dict[str, float]:
        """
        Get all conditional token positions from the wallet.
        
        Uses the Polymarket Data API to find positions.
        Returns: {token_id: balance_shares}
        """
        try:
            import requests
            from eth_account import Account
            from config import Config

            pk = Config.POLY_PRIVATE_KEY.strip()
            if not pk.startswith("0x"):
                pk = "0x" + pk
            wallet = Account.from_key(pk)
            
            # Use the proxy wallet address if configured (that's where funds live)
            address = Config.POLY_PROXY_WALLET.strip() if Config.POLY_PROXY_WALLET else wallet.address
            if not address:
                address = wallet.address

            # Method 1: Use Polymarket's positions API
            positions = {}
            
            # Try the gamma positions endpoint
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
                        logger.debug("Found %d positions via gamma API", len(positions))
                        return positions
            except Exception as e:
                logger.debug("Gamma positions API failed: %s", e)

            # Method 2: Use CLOB get_balance_allowance for known tokens
            # (This is a fallback — we check tokens from recent trades stored in DB)
            # For now, try the data API
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
                    if positions:
                        logger.debug("Found %d positions via data API", len(positions))
                        return positions
            except Exception as e:
                logger.debug("Data API positions failed: %s", e)

            # Method 3: Check tokens from positions tracked by live_trader
            # (This fallback uses CLOB get_balance_allowance per token)
            return positions

        except Exception as e:
            logger.error("Failed to get conditional positions: %s", e)
            return {}

    async def _get_market_for_token(self, token_id: str) -> Optional[Dict]:
        """Look up market info for a conditional token ID via Gamma API."""
        try:
            import requests
            
            # Gamma API: look up market by clob token ID
            resp = requests.get(
                f"{GAMMA_API_URL}/markets",
                params={"clob_token_ids": token_id},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]

            # Fallback: search by token ID in the market's clobTokenIds field
            # Some endpoints use different parameter names
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
            logger.debug("Market lookup failed for token %s: %s", token_id[:12], e)

        return None

    async def _redeem_market(self, condition_id: str, neg_risk: bool) -> bool:
        """
        Redeem resolved positions via gasless builder relayer.
        
        Uses SafeTransaction to call the appropriate contract:
        - neg_risk=True: NegRiskAdapter.redeemPositions(bytes32, uint256[])
        - neg_risk=False: CTF.redeemPositions(address, bytes32, bytes32, uint256[])
        
        Returns True on success.
        """
        if not self.relayer:
            return False

        try:
            from web3 import Web3
            from eth_abi import encode
            from py_builder_relayer_client.models import SafeTransaction, OperationType

            cond_bytes = bytes.fromhex(
                condition_id[2:] if condition_id.startswith("0x") else condition_id
            )

            if neg_risk:
                # NegRiskAdapter.redeemPositions(bytes32, uint256[])
                max_uint = 2**256 - 1
                selector = Web3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
                params = encode(
                    ["bytes32", "uint256[]"],
                    [cond_bytes, [max_uint, max_uint]],
                )
                target = NEG_RISK_ADAPTER
            else:
                # CTF.redeemPositions(address, bytes32, bytes32, uint256[])
                selector = Web3.keccak(
                    text="redeemPositions(address,bytes32,bytes32,uint256[])"
                )[:4]
                params = encode(
                    ["address", "bytes32", "bytes32", "uint256[]"],
                    [USDC_ADDRESS, b"\x00" * 32, cond_bytes, [1, 2]],
                )
                target = CTF_ADDRESS

            calldata = "0x" + (selector + params).hex()
            tx = SafeTransaction(
                to=target,
                operation=OperationType.Call,
                data=calldata,
                value="0",
            )

            # Submit the redeem transaction
            logger.info("Submitting redeem for condition %s (neg_risk=%s)", condition_id[:16], neg_risk)
            resp = self.relayer.execute([tx], "Redeem positions")
            tx_id = getattr(resp, 'transaction_id', str(resp))
            print(f"  📨 Redeem submitted: {tx_id}", flush=True)

            # Poll for confirmation (max 60s)
            for _ in range(20):
                await asyncio.sleep(3)
                try:
                    status = self.relayer.get_transaction(tx_id)
                    if isinstance(status, list):
                        status = status[0] if status else {}
                    
                    state = status.get("state", "") if isinstance(status, dict) else str(status)
                    
                    if "CONFIRMED" in state.upper():
                        tx_hash = status.get("transactionHash", "") if isinstance(status, dict) else ""
                        print(f"  ✅ Redeem CONFIRMED: {tx_hash[:20]}...", flush=True)
                        return True
                    
                    if "FAILED" in state.upper() or "INVALID" in state.upper():
                        error = status.get("errorMsg", "") if isinstance(status, dict) else ""
                        print(f"  ❌ Redeem FAILED: {error[:80]}", flush=True)
                        return False
                except Exception as poll_err:
                    logger.debug("Redeem poll error: %s", poll_err)
                    continue

            print("  ⏰ Redeem timeout — may still confirm on-chain", flush=True)
            return False

        except ImportError as e:
            print(f"  ❌ Redeem failed: missing dependency — {e}", flush=True)
            print("  Install: pip install py-builder-relayer-client web3 eth-abi", flush=True)
            return False

        except Exception as e:
            logger.error("Redeem error for %s: %s", condition_id[:16], e)
            print(f"  ❌ Redeem error: {e}", flush=True)
            traceback.print_exc()
            return False

    async def force_check(self) -> Dict:
        """Force an immediate check (ignores cooldown). For manual /redeem command."""
        self._last_check = 0
        return await self.check_and_redeem()

    def get_status(self) -> Dict:
        """Get auto-redeem status for debug/status commands."""
        return {
            "enabled": self._enabled,
            "total_redeemed": self._total_redeemed,
            "tracked_redeemed": len(self._redeemed_conditions),
            "last_check": self._last_check,
            "check_interval": self._check_interval,
            "errors": self._init_errors[-3:] if self._init_errors else [],
        }
