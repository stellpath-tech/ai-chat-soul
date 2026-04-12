import urllib.request
import json
import time
import urllib.error

BASE_URL = "http://localhost:9899"

def request(method, path, data=None, headers=None):
    if headers is None:
        headers = {}
    if data is not None:
        data = json.dumps(data).encode('utf-8')
        headers["Content-Type"] = "application/json"
    
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except Exception as e:
        return {"error": str(e)}

print("=== 边界测试开始 ===")

# 1. 创建一个已经过期的邀请码 (expireAt < now)
print("\n[测试 1] 创建已过期的邀请码 (expired_code)")
expired_code = "exp123"
request("POST", "/api/invite_code", {
    "inviteCode": expired_code,
    "expireAt": int(time.time() * 1000) - 10000  # 10秒前过期
})

# 2. 尝试使用过期的邀请码登录
print("\n[测试 2] 用户 A 尝试使用过期的邀请码注册/登录")
res = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613800138000",
    "inviteCode": expired_code
})
print("结果:", res)
assert not res.get("success"), "预期失败，但成功了！"
assert res.get("message") == "内测码已过期", f"预期提示'内测码已过期'，实际为 {res.get('message')}"

# 3. 创建一个有效的普通用户邀请码 (32位)
print("\n[测试 3] 创建有效的普通用户邀请码 (valid_code_1)")
valid_code_1 = "12345678901234561234567890123456"
request("POST", "/api/invite_code", {
    "inviteCode": valid_code_1,
    "expireAt": int(time.time() * 1000) + 3600000  # 1小时后过期
})

# 4. 用户 A 使用有效邀请码注册/登录
print("\n[测试 4] 用户 A 尝试使用有效的邀请码注册/登录")
res = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613800138000",
    "inviteCode": valid_code_1
})
print("结果:", res)
assert res.get("success"), "预期成功，但失败了！"

# 5. 用户 B 尝试使用已被用户 A 绑定的邀请码
print("\n[测试 5] 用户 B 尝试使用已被用户 A 绑定的邀请码 (valid_code_1)")
res = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613900139000",
    "inviteCode": valid_code_1
})
print("结果:", res)
assert not res.get("success"), "预期失败，但成功了！"
assert res.get("message") == "内测码已被其他手机号绑定", f"预期提示'内测码已被其他手机号绑定'，实际为 {res.get('message')}"

# 6. 用户 A 再次使用自己已绑定的邀请码登录（未过期情况）
print("\n[测试 6] 用户 A 再次使用自己已绑定的邀请码登录 (valid_code_1, 未过期)")
res = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613800138000",
    "inviteCode": valid_code_1
})
print("结果:", res)
assert res.get("success"), "预期成功，但失败了！"

# 7. 创建一个新的有效邀请码
print("\n[测试 7] 创建一个新的有效邀请码 (valid_code_2)")
valid_code_2 = "uvwxyz" # 内部用户 (6位), 全新邀请码避免和前一个测试冲突
request("POST", "/api/invite_code", {
    "inviteCode": valid_code_2,
    "expireAt": int(time.time() * 1000) + 3600000
})

# 8. 用户 A 使用新的有效邀请码登录 (更新内测码和分组)
print("\n[测试 8] 用户 A 使用新的有效邀请码登录 (valid_code_2)")
res = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613800138000",
    "inviteCode": valid_code_2
})
print("结果:", res)
assert res.get("success"), "预期成功，但失败了！"

# 9. 尝试使用不存在的邀请码
print("\n[测试 9] 尝试使用不存在的邀请码")
res = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613800138000",
    "inviteCode": "not_exist_code"
})
print("结果:", res)
assert not res.get("success"), "预期失败，但成功了！"
assert res.get("message") == "无效的内测码", f"预期提示'无效的内测码'，实际为 {res.get('message')}"

print("\n=== 所有边界测试通过 ===")

# 10. 用户 A 再次使用自己已绑定的邀请码登录（已过期情况）
print("\n[测试 10] 用户 C 绑定邀请码，然后邀请码过期，用户 C 再次登录")
# 先创建一个马上过期的码
fast_exp_code = "fast12"
request("POST", "/api/invite_code", {
    "inviteCode": fast_exp_code,
    "expireAt": int(time.time() * 1000) + 1000  # 1秒后过期
})
# 用户 C 绑定
res1 = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613800138999",
    "inviteCode": fast_exp_code
})
assert res1.get("success"), "预期成功，但失败了！"
print("用户 C 首次绑定成功，等待2秒让邀请码过期...")
import time
time.sleep(2)

# 用户 C 再次登录
res2 = request("POST", "/api/auth/register", {
    "phoneNumber": "+8613800138999",
    "inviteCode": fast_exp_code
})
print("结果:", res2)
assert not res2.get("success"), "预期失败，但成功了！"
assert res2.get("message") == "内测码已过期", f"预期提示'内测码已过期'，实际为 {res2.get('message')}"

print("\n=== 补充边界测试通过 ===")

print("\n[测试 11] 使用不存在的 auth_token 调用聊天接口")
res3 = request("POST", "/message", 
    data={"message": "hello", "stream": False},
    headers={"x-auth-token": "this_is_a_fake_token_123456"}
)
print("结果:", res3)
assert res3.get("code") == 401, f"预期401，实际返回: {res3}"
assert res3.get("message") == "unauthorized", "预期unauthorized"

print("\n=== 全部测试通过 ===")
