"""
快速测试：验证 get_full_system_prompt 每轮是否正确注入时间+季节
"""
import sys
import os
sys.stdout.reconfigure(encoding='utf-8')

# 把项目根目录加入 path
sys.path.insert(0, os.path.dirname(__file__))

import datetime
import time as _time


def make_runtime_info():
    def get_current_time():
        now = datetime.datetime.now()
        try:
            offset = -_time.timezone if not _time.daylight else -_time.altzone
            hours = offset // 3600
            minutes = (offset % 3600) // 60
            tz = f"UTC{hours:+03d}:{minutes:02d}" if minutes else f"UTC{hours:+03d}"
        except Exception:
            tz = "UTC"
        weekday_map = {
            'Monday': '星期一', 'Tuesday': '星期二', 'Wednesday': '星期三',
            'Thursday': '星期四', 'Friday': '星期五', 'Saturday': '星期六', 'Sunday': '星期日'
        }
        return {
            'time': now.strftime("%Y-%m-%d %H:%M:%S"),
            'weekday': weekday_map.get(now.strftime("%A"), now.strftime("%A")),
            'timezone': tz,
        }
    return {"_get_current_time": get_current_time}


def test_time_injection():
    from agent.protocol.agent import Agent
    from agent.protocol.models import LLMModel

    runtime_info = make_runtime_info()

    # 创建一个最小 Agent（无需真实 model/tools）
    agent = Agent(
        system_prompt="你是满仓，一只来自缝隙世界的破损小熊饼干。",
        runtime_info=runtime_info,
    )
    agent.rule_path = None  # 不依赖文件系统

    prompt = agent.get_full_system_prompt()

    print("=" * 60)
    print("生成的 system prompt：")
    print("=" * 60)
    print(prompt)
    print("=" * 60)

    # 验证时间和季节都出现了
    now = datetime.datetime.now()
    month = now.month
    expected_season = {
        12: "冬天", 1: "冬天", 2: "冬天",
        3: "春天", 4: "春天", 5: "春天",
        6: "夏天", 7: "夏天", 8: "夏天",
        9: "秋天", 10: "秋天", 11: "秋天",
    }[month]

    assert "当前时间" in prompt, "❌ 没有注入时间"
    assert expected_season in prompt, f"❌ 没有注入季节（当前应为{expected_season}）"
    print(f"\n✅ 时间注入正常，检测到季节：{expected_season}")


if __name__ == "__main__":
    test_time_injection()
