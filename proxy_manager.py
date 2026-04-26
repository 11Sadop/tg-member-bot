import asyncio
import aiohttp
import logging
import random
import time
import os
import json
from python_socks.async_.asyncio import Proxy

logger = logging.getLogger("proxy_manager")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
VERIFIED_PROXIES_FILE = os.path.join(DATA_DIR, "verified_proxies.json")

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"
]

class ProxyManager:
    def __init__(self):
        self.working_proxies = self._load_proxies()
        self.is_fetching = False
        
    def _load_proxies(self):
        if os.path.exists(VERIFIED_PROXIES_FILE):
            try:
                with open(VERIFIED_PROXIES_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_proxies(self, proxies):
        with open(VERIFIED_PROXIES_FILE, "w") as f:
            json.dump(proxies, f)
        self.working_proxies = proxies

    def get_working_proxies_count(self):
        return len(self.working_proxies)

    def get_random_proxy(self):
        if not self.working_proxies:
            return None
        proxy_str = random.choice(self.working_proxies)
        parts = proxy_str.split(":")
        return {
            'proxy_type': 'socks5',
            'addr': parts[0],
            'port': int(parts[1])
        }

    async def fetch_proxies(self):
        raw_proxies = set()
        logger.info("Fetching free proxies from sources...")
        async with aiohttp.ClientSession() as session:
            for url in PROXY_SOURCES:
                try:
                    async with session.get(url, timeout=10) as response:
                        if response.status == 200:
                            text = await response.text()
                            for line in text.splitlines():
                                line = line.strip()
                                if line and ":" in line:
                                    raw_proxies.add(line)
                except Exception as e:
                    logger.warning(f"Failed to fetch from {url}: {e}")
        
        logger.info(f"Fetched {len(raw_proxies)} raw proxies. Starting verification...")
        return list(raw_proxies)

    async def verify_proxy(self, proxy_str):
        """Test proxy by attempting to connect to Telegram DC."""
        parts = proxy_str.split(":")
        if len(parts) != 2:
            return None
            
        try:
            # Telegram's DC4 IP
            target_host = '149.154.167.220'
            target_port = 443
            
            proxy = Proxy.from_url(f"socks5://{proxy_str}")
            
            # Try to establish connection within 4 seconds (strict to ensure fast proxies)
            async with asyncio.timeout(4.0):
                sock = await proxy.connect(dest_host=target_host, dest_port=target_port)
                sock.close()
                return proxy_str
        except Exception:
            return None

    async def verify_proxies_batch(self, proxies, batch_size=200):
        working = []
        for i in range(0, len(proxies), batch_size):
            batch = proxies[i:i+batch_size]
            tasks = [self.verify_proxy(p) for p in batch]
            results = await asyncio.gather(*tasks)
            working.extend([r for r in results if r])
            logger.info(f"Verified batch {i//batch_size + 1}, found {len([r for r in results if r])} working.")
            # Break early if we have enough proxies to save time
            if len(working) >= 30:
                logger.info("Found enough working proxies. Stopping early.")
                break
        return working

    async def run_update_cycle(self):
        if self.is_fetching:
            return False
            
        self.is_fetching = True
        try:
            raw_proxies = await self.fetch_proxies()
            random.shuffle(raw_proxies)
            
            # Verify only up to 500 to save resources on Render
            test_batch = raw_proxies[:500] 
            
            working = await self.verify_proxies_batch(test_batch)
            if working:
                self._save_proxies(working)
                logger.info(f"Saved {len(working)} verified proxies.")
                return True
            else:
                logger.warning("No working proxies found in this batch.")
                return False
        finally:
            self.is_fetching = False

proxy_manager = ProxyManager()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(proxy_manager.run_update_cycle())
