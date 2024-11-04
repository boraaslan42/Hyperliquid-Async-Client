from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex
import msgpack
from decimal import Decimal
import json
import logging
import aiohttp
from json import JSONDecodeError
import eth_account
import time
import asyncio

def get_timestamp_ms():
    return int(time.time() * 1000)

def float_to_wire(x: float) -> str:
    rounded = "{:.8f}".format(x)
    if abs(float(rounded) - x) >= 1e-12:
        raise ValueError("float_to_wire causes rounding", x)
    if rounded == "-0":
        rounded = "0"
    normalized = Decimal(rounded).normalize()
    return f"{normalized:f}"

class Error(Exception):
    pass

class ClientError(Error):
    def __init__(self, status_code, error_code, error_message, header, error_data=None):
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        self.header = header
        self.error_data = error_data

class ServerError(Error):
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message

class HyperLiquidClient:
    def __init__(self, config_path):
        """
        Initialize the HyperLiquid client
        
        Args:
            config_path (str): Path to config.json file
        """
        self.base_url = "https://api.hyperliquid.xyz"
        self._logger = logging.getLogger(__name__)
        self.session = None
        
        with open(config_path) as f:
            config = json.load(f)
            
        self.wallet = eth_account.Account.from_key(config["secret_key"])
        self.address = config["account_address"]
        self.vault_address = None
        self.account_address = self.address
        #self.asset_info=await self.get_universe()[0]["universe"]

        print("Running with account address:", self.address)
        if self.address != self.wallet.address:
            print("Running with agent address:", self.wallet.address)

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers={"Content-Type": "application/json"})
        meta_data = await self.meta()
        #coin is string asset is int
        #coin BTC asset 0
        self.coin_to_asset = {}
        self.coin_info = {}
        tasks = []
        for idx, asset_info in enumerate(meta_data["universe"]):
            name = asset_info["name"]
            self.coin_to_asset[name] = idx
            self.coin_info[name] = asset_info
            tasks.append(self.update_leverage(name,asset_info["maxLeverage"]))
        asyncio.gather(*tasks)            
            
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def post(self, url_path: str, payload=None):
        """Post request to API endpoint"""
        if self.session is None:
            self.session = aiohttp.ClientSession(headers={"Content-Type": "application/json"})
            
        payload = payload or {}
        url = self.base_url + url_path
        
        async with self.session.post(url, json=payload) as response:
            await self._handle_exception(response)
            try:
                return await response.json()
            except ValueError:
                return {"error": f"Could not parse JSON: {await response.text()}"}

    async def _handle_exception(self, response):
        """Handle API exceptions"""
        status_code = response.status
        if status_code < 400:
            return
        if 400 <= status_code < 500:
            try:
                err = await response.json()
            except JSONDecodeError:
                raise ClientError(status_code, None, await response.text(), None, response.headers)
            if err is None:
                raise ClientError(status_code, None, await response.text(), None, response.headers)
            error_data = err.get("data")
            raise ClientError(status_code, err["code"], err["msg"], response.headers, error_data)
        raise ServerError(status_code, await response.text())

    def sign_l1_action(self, wallet, action, vault_address, nonce, is_mainnet):
        data = msgpack.packb(action)
        data += nonce.to_bytes(8, "big")
        if vault_address is None:
            data += b"\x00"
        else:
            data += b"\x01"
            data += bytes.fromhex(vault_address[2:] if vault_address.startswith("0x") else vault_address)
        hash = keccak(data)
        
        phantom_agent = {"source": "a" if is_mainnet else "b", "connectionId": hash}
        data = {
            "domain": {
                "chainId": 1337,
                "name": "Exchange",
                "verifyingContract": "0x0000000000000000000000000000000000000000",
                "version": "1",
            },
            "types": {
                "Agent": [
                    {"name": "source", "type": "string"},
                    {"name": "connectionId", "type": "bytes32"},
                ],
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
            },
            "primaryType": "Agent",
            "message": phantom_agent,
        }
        
        structured_data = encode_structured_data(data)
        
        signed = wallet.sign_message(structured_data)
        return {"r": to_hex(signed["r"]), "s": to_hex(signed["s"]), "v": signed["v"]}

    async def _post_action(self, action, nonce):
        """Internal method to post actions to the exchange"""
        signature = self.sign_l1_action(
            self.wallet,
            action,
            self.vault_address,
            nonce,
            self.base_url == "https://api.hyperliquid.xyz",
        )
        
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": self.vault_address if action["type"] != "usdClassTransfer" else None,
        }
        logging.debug(payload)
        return await self.post("/exchange", payload)

    async def place_order(
        self,
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type,
        reduce_only: bool = False,
        ):
        """Place a single order"""
        order_wire = {
            "a":self.coin_to_asset[name],
            "b": is_buy,
            "p": float_to_wire(limit_px),
            "s": float_to_wire(sz),
            "r": reduce_only,
            "t": order_type,
        }
        timestamp = get_timestamp_ms()
        
        order_action = {
            "type": "order",
            "orders": [order_wire],
            "grouping": "na",
        }
        
        return await self._post_action(
            order_action,
            timestamp,
        )

    async def place_orders(self, order_requests): 
        """Place multiple orders at once"""
        order_wires = []
        for order in order_requests:
            order_wire = {
                
                "a": self.coin_to_asset[order["coin"]],
                "b": order["is_buy"],
                "p": float_to_wire(order["limit_px"]),
                "s": float_to_wire(order["sz"]),
                "r": order["reduce_only"],
                "t": order["order_type"],
            }
            order_wires.append(order_wire)

        timestamp = get_timestamp_ms()
        
        order_action = {
            "type": "order",
            "orders": order_wires,
            "grouping": "na",
        }
        
        return await self._post_action(
            order_action,
            timestamp,
        )

    async def cancel_order(self, name: str, oid: int):
        """Cancel a single order"""
        timestamp = get_timestamp_ms()
        cancel_action = {
            "type": "cancel",
            "cancels": [
                {
                    "a": self.coin_to_asset[name],
                    "o": oid,
                }
            ],
        }
        
        return await self._post_action(
            cancel_action,
            timestamp,
        )

    async def cancel_orders(self, cancel_requests):
        """Cancel multiple orders at once"""
        timestamp = get_timestamp_ms()
        cancel_action = {
            "type": "cancel",
            "cancels": [
                {
                    "a": self.coin_to_asset[cancel["coin"]],
                    "o": cancel["oid"],
                }
                for cancel in cancel_requests
            ],
        }
        
        return await self._post_action(
            cancel_action,
            timestamp,
        )

    async def query_order_by_oid(self, oid: int):
        """Query order status by order ID"""
        return await self.post("/info", {"type": "orderStatus", "user": self.address, "oid": oid})

    async def user_state(self):
        """Retrieve trading details about the user"""
        return await self.post("/info", {"type": "clearinghouseState", "user": self.address})
    async def get_universe(self):
        """Retrieve trading details about the user"""
        return await self.post("/info", {"type": "metaAndAssetCtxs", "user": self.address})

    async def open_orders(self):
        """Retrieve user's open orders"""
        return await self.post("/info", {"type": "openOrders", "user": self.address})

    async def meta(self):
        """Retrieve exchange perp metadata"""
        return await self.post("/info", {"type": "meta"})

    async def update_leverage(self, asset_name: str, leverage: int, is_cross: bool = True):
        """
        Update leverage for an asset
        
        Args:
            asset_name (str): Asset name (e.g. 'ETH')
            leverage (int): Desired leverage (must be <= max_leverage)
            is_cross (bool): True for cross margin, False for isolated
        """
        # Get asset info and validate leverage
        asset_info = self.coin_info[asset_name]
        max_leverage = asset_info['maxLeverage']
        
        if leverage > max_leverage:
            raise ValueError(f"Leverage {leverage} exceeds maximum allowed leverage {max_leverage} for {asset_name}")
        
        action = {
            "type": "updateLeverage",
            "asset": self.coin_to_asset[asset_name],
            "isCross": is_cross,
            "leverage": leverage
        }
        
        timestamp = get_timestamp_ms()
        return await self._post_action(action, timestamp)


    async def get_positions(self):
        """Get current positions for the account"""
        user_state = await self.user_state()
        positions = []
        for position in user_state["assetPositions"]:
            positions.append(position["position"])
        return positions

    async def get_balance(self):
        """Get account balance and state"""
        return await self.user_state()

    async def get_order_status(self, order_result):
        """Get status of an order by its order result"""
        if order_result["status"] == "ok":
            status = order_result["response"]["data"]["statuses"][0]
            if "resting" in status:
                return await self.query_order_by_oid(status["resting"]["oid"])
        return None

async def main():
    async with HyperLiquidClient("config.json") as client:
        # Get and display positions
        positions = await client.get_positions()
        if positions:
            print("positions:")
            for position in positions:
                print(json.dumps(position, indent=2))
        else:
            print("no open positions")
        
        # Place a limit order
        start_time = time.time()
        order_result = await client.place_order("ETH", True, 0.1, 1100, {"limit": {"tif": "Gtc"}})
        print("order took", time.time() - start_time, "seconds")
        print(order_result)
        # Get order status
        order_status = await client.get_order_status(order_result)
        if order_status:
            print("Order status:", order_status)
        # Cancel the order
        if order_result["status"] == "ok":
            start_time = time.time()
            cancel_result = await client.cancel_order(
                "ETH", 
                order_result["response"]["data"]["statuses"][0]["resting"]["oid"]
            )
            print("cancel took", time.time() - start_time, "seconds")
            if cancel_result:
                print("Cancel result:", cancel_result)

if __name__ == "__main__":
    asyncio.run(main())