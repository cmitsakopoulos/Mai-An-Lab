import asyncio
import re
import aiohttp
from collections import OrderedDict

async def main():
    print("Fetching login page...")
    connector = aiohttp.TCPConnector(ssl=False)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        async with session.get("https://play.qobuz.com/login", timeout=30) as req:
            login_page = await req.text()
            
        print("Login page length:", len(login_page))
        bundle_url_match = re.search(
            r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>',
            login_page,
        )
        if bundle_url_match is None:
            print("FAILED to find bundle.js link in login page HTML!")
            print("First 1000 chars of HTML:")
            print(login_page[:1000])
            return
            
        bundle_url = bundle_url_match.group(1)
        print("Found bundle URL:", bundle_url)
        
        print("Fetching bundle...")
        async with session.get("https://play.qobuz.com" + bundle_url, timeout=60) as req:
            bundle = await req.text()
            
        print("Bundle length:", len(bundle))
        
        # Test App ID regex
        app_id_regex = r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"(\w{32})'
        match = re.search(app_id_regex, bundle)
        if match is None:
            print("FAILED to match appId regex in bundle!")
            # Try a broader search for appId
            broad_match = re.findall(r'appId:"(\d+)"', bundle)
            print("Broad matches for appId:", broad_match)
            broad_secret = re.findall(r'appSecret:"(\w+)"', bundle)
            print("Broad matches for appSecret:", broad_secret)
            return
            
        app_id = match.group("app_id")
        print("Matched App ID:", app_id)
        
        # Test secrets parsing
        seed_timezone_regex = r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)'
        seed_matches = list(re.finditer(seed_timezone_regex, bundle))
        print(f"Found {len(seed_matches)} seed matches")
        for m in seed_matches[:3]:
            print("Seed match:", m.group("seed"), m.group("timezone"))

asyncio.run(main())
