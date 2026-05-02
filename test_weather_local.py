import urllib.request
import json
import threading
import time
import sys

# mock config before anything else
import types
sys.modules['config'] = types.ModuleType('config')
class MockConfig:
    def get(self, key, default=None):
        return default
sys.modules['config'].conf = lambda: MockConfig()
sys.modules['common.log'] = types.ModuleType('common.log')
sys.modules['common.log'].logger = type('Logger', (), {'info': print, 'error': print, 'debug': print, 'warning': print})()
sys.modules['common.singleton'] = types.ModuleType('common.singleton')
sys.modules['common.singleton'].singleton = lambda x: x

from channel.web.web_channel import WeatherHandler
import web

urls = ('/api/weather', 'WeatherHandler')
app = web.application(urls, globals())

def start_server():
    web.httpserver.runsimple(app.wsgifunc(), ("127.0.0.1", 9999))

server_thread = threading.Thread(target=start_server, daemon=True)
server_thread.start()

time.sleep(2) # wait for server to start

try:
    print("Testing /api/weather without parameters...")
    req = urllib.request.Request("http://127.0.0.1:9999/api/weather", method="GET")
    with urllib.request.urlopen(req) as response:
        res = json.loads(response.read())
        print("Response:", res)
        assert res.get("success") == False
        assert "Missing" in res.get("message")
        print("Test passed: Missing parameters handled correctly.")
except Exception as e:
    print("Error:", e)

try:
    print("\nTesting /api/weather with lat/lon parameters...")
    req = urllib.request.Request("http://127.0.0.1:9999/api/weather?lat=31.23&lon=121.47", method="GET")
    with urllib.request.urlopen(req) as response:
        res = json.loads(response.read())
        print("Response Success:", res.get("success"))
        assert res.get("success") == True
        assert res.get("data") is not None
        data = res.get("data")
        assert "now" in data
        assert "temp" in data["now"]
        print("Test passed: Valid parameters returned valid data.")
        
    print("\nTesting /api/weather cache hit...")
    req2 = urllib.request.Request("http://127.0.0.1:9999/api/weather?lat=31.23&lon=121.47", method="GET")
    with urllib.request.urlopen(req2) as response:
        res2 = json.loads(response.read())
        print("Response Success:", res2.get("success"))
        assert res2.get("success") == True
        assert "cached" in res2.get("message").lower()
        print("Test passed: Cache hit successful.")
except Exception as e:
    print("Error:", e)

print("\nIntegration tests completed.")
