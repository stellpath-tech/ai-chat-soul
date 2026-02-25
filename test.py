import os
import openai

api_key = os.getenv("ARK_API_KEY")
if not api_key:
    raise ValueError("请先设置环境变量 ARK_API_KEY")

openai.api_key = api_key
openai.api_base = "https://ark.cn-beijing.volces.com/api/v3"

resp = openai.ChatCompletion.create(
    model="doubao-seed-2-0-pro-260215",
    messages=[
        {"role": "user", "content": "你好，做个自我介绍"}
    ],
    temperature=0.7,
)

print(resp["choices"][0]["message"]["content"])
