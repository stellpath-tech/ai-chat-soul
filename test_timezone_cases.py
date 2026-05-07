
import requests
import json
import time
import sys

def test_timezone(name, tz_data):
    print(f"\n--- Testing Case: {name} ---")
    url = "http://localhost:9899/message"
    payload = {
        "message": "现在几点？请直接告诉我日期、时间和时区名称，不要说多余的话。",
        "stream": True,
        "timezone": tz_data
    }
    
    try:
        # 1. Send message
        resp = requests.post(url, json=payload)
        resp_data = resp.json()
        if resp_data.get("status") != "success":
            print(f"Error sending message: {resp_data}")
            return
        
        request_id = resp_data.get("request_id")
        print(f"Request ID: {request_id}")
        
        # 2. Get stream response
        stream_url = f"http://localhost:9899/stream?request_id={request_id}"
        print("Waiting for response...")
        
        full_content = ""
        with requests.get(stream_url, stream=True, timeout=30) as r:
            for line in r.iter_lines():
                if line:
                    line_str = line.decode('utf-8')
                    if line_str.startswith("data: "):
                        data = json.loads(line_str[6:])
                        if data.get("type") == "delta":
                            content = data.get("content", "")
                            full_content += content
                            # print(content, end="", flush=True)
                        elif data.get("type") == "done":
                            break
        
        print(f"\nAI Response: {full_content.strip()}")
        
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    # Test cases
    cases = [
        ("Shanghai (UTC+8)", {
            "tz_isDST": False,
            "tz_iana": "Asia/Shanghai",
            "tz_offset_min": 480
        }),
        ("New York (UTC-4/EDT)", {
            "tz_isDST": True,
            "tz_iana": "America/New_York",
            "tz_offset_min": -240
        }),
        ("Tokyo (UTC+9)", {
            "tz_isDST": False,
            "tz_iana": "Asia/Tokyo",
            "tz_offset_min": 540
        }),
        ("No Timezone (Fallback)", None)
    ]
    
    for name, tz in cases:
        test_timezone(name, tz)
        time.sleep(2) # Wait a bit between requests
