import http from "k6/http";
import sse from "k6/x/sse";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";
import { textSummary } from "https://jslib.k6.io/k6-summary/0.1.0/index.js";

// ─── 自定义指标 ───────────────────────────────────────────────
const messageSendDuration = new Trend("message_send_duration", true);
const streamTTFB          = new Trend("stream_ttfb", true);
const streamTotalDuration = new Trend("stream_total_duration", true);
const streamEventCount    = new Trend("stream_event_count");
const messageErrors       = new Rate("message_errors");
const streamErrors        = new Rate("stream_errors");
const completedChats      = new Counter("completed_chats");

// ─── 配置 ─────────────────────────────────────────────────────
const BASE_URL    = __ENV.BASE_URL || "http://localhost:9899";
const SSE_TIMEOUT = parseInt(__ENV.SSE_TIMEOUT || "120");

// ─── stages 从环境变量读入 ────────────────────────────────────
const stagesJson = __ENV.STAGES_JSON || '[]';
let stages;
try {
  stages = JSON.parse(stagesJson);
} catch (e) {
  stages = [{ duration: "60s", target: 10 }];
}

export const options = {
  stages: stages,
  // 阶梯加压不设 thresholds，目的是观察劣化而非 pass/fail
};

// ─── 随机消息生成 ─────────────────────────────────────────────
const TOPICS = [
  "今天天气怎么样", "帮我写一首诗", "讲个笑话", "你好呀",
  "推荐一部电影", "解释一下量子力学", "写一段代码",
  "帮我翻译这句话", "你觉得AI会取代人类吗", "讲一个故事",
  "帮我做个计划", "你有什么技能", "北京有什么好玩的",
  "如何学编程", "给我一个建议", "解释一下相对论",
  "推荐一本书", "写一封邮件", "帮我算一道数学题",
  "你最喜欢什么颜色",
];

const SUFFIXES = [
  "简短回答即可", "详细说说", "用一句话概括",
  "举个例子", "用比喻解释", "",
];

function randomMessage(vuId, iterNum) {
  const topic  = TOPICS[Math.floor(Math.random() * TOPICS.length)];
  const suffix = SUFFIXES[Math.floor(Math.random() * SUFFIXES.length)];
  const noise  = `[vu${vuId}_i${iterNum}_${Date.now()}]`;
  return `${topic} ${noise} ${suffix}`.trim();
}

// ─── 主测试逻辑 ───────────────────────────────────────────────
export default function () {
  const vuId      = __VU;
  const iterNum   = __ITER;
  const sessionId = `stress_vu${vuId}`;
  const msg       = randomMessage(vuId, iterNum);

  // ── Step 1: POST /message ──────────────────────────────────
  const payload = JSON.stringify({
    message:    msg,
    session_id: sessionId,
    stream:     true,
  });

  const postStart = Date.now();
  const res = http.post(`${BASE_URL}/message`, payload, {
    headers: { "Content-Type": "application/json" },
    tags:    { name: "POST_message" },
    timeout: "10s",
  });
  const postDuration = Date.now() - postStart;

  messageSendDuration.add(postDuration);

  const postOk = check(res, {
    "POST /message status 200": (r) => r.status === 200,
    "POST /message has request_id": (r) => {
      try {
        return JSON.parse(r.body).request_id !== undefined;
      } catch (e) {
        return false;
      }
    },
  });

  if (!postOk || res.status !== 200) {
    messageErrors.add(1);
    streamErrors.add(1);
    sleep(1);
    return;
  }
  messageErrors.add(0);

  let body;
  try {
    body = JSON.parse(res.body);
  } catch (e) {
    messageErrors.add(1);
    streamErrors.add(1);
    sleep(1);
    return;
  }

  const requestId = body.request_id;

  // ── Step 2: GET /stream (SSE) ──────────────────────────────
  const sseUrl = `${BASE_URL}/stream?request_id=${requestId}`;
  const sseStart = Date.now();
  let firstDeltaTime = 0;
  let eventCount = 0;
  let gotDone = false;

  sse.open(sseUrl, {
    headers: { "Accept": "text/event-stream" },
    tags:    { name: "GET_stream" },
  }, function (client) {
    client.on("event", function (event) {
      eventCount++;

      if ((Date.now() - sseStart) > SSE_TIMEOUT * 1000) {
        client.close();
        return;
      }

      let parsed;
      try {
        parsed = JSON.parse(event.data);
      } catch (e) {
        return;
      }

      if (parsed.type === "delta" && firstDeltaTime === 0) {
        firstDeltaTime = Date.now();
      }

      if (parsed.type === "done") {
        gotDone = true;
        client.close();
      }
    });

    client.on("error", function (e) {
      console.error(`SSE error [vu${vuId}]: ${e.error()}`);
      client.close();
    });
  });

  const sseDuration = Date.now() - sseStart;

  // ── Step 3: 记录指标 ──────────────────────────────────────
  if (gotDone) {
    streamErrors.add(0);
    completedChats.add(1);
    streamTotalDuration.add(sseDuration);
    streamEventCount.add(eventCount);

    if (firstDeltaTime > 0) {
      streamTTFB.add(firstDeltaTime - sseStart);
    }
  } else {
    streamErrors.add(1);
  }

  sleep(0.5 + Math.random() * 1.5);
}

// ─── 测试结束汇总 ─────────────────────────────────────────────
export function handleSummary(data) {
  const result = {
    stdout: textSummary(data, { indent: "  ", enableColors: true }),
  };

  if (__ENV.SUMMARY_EXPORT) {
    result[__ENV.SUMMARY_EXPORT] = JSON.stringify(data, null, 2);
  }

  return result;
}
