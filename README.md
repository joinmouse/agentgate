# AgentGate

面向 agent 工作负载的生产级推理网关：统一模型接入、成本感知路由、配额与限流、全量链路追踪与**失败归因**。

> 一个 agent 任务动辄几十上百次模型调用（Kimi K2.6 单任务可发起 4000+ 次工具调用）。
> 没有网关，这些调用是一堆孤立的 HTTP 请求；有了网关，它们才是可调度、可计量、
> 可归因、可优化的系统。

## 核心能力

| 能力 | 实现 | 代码位置 |
|---------|------|---------|
| **模型网关** | OpenAI 兼容 `POST /v1/chat/completions`，多 provider 协议适配（mock + OpenAI 兼容适配器），API key 鉴权 | `agentgate/main.py`, `providers.py` |
| **多模型路由** | alias（`auto`/`cheap`/`smart`）→ 候选链；按成本升序选择，熔断器自动摘除故障 provider，失败自动 fallback | `agentgate/routing.py` |
| **成本控制** | 全量 token 计量 + 单价表成本核算；每 key 每日 token 配额（429 quota）；滑动窗口限流（429 ratelimit） | `agentgate/metering.py`, `storage.py` |
| **链路管理** | 每请求一条 trace（receive → route.decide → provider.call → respond 四个 span），可查询、可回溯 | `agentgate/tracing.py` |
| **差异化：失败归因** | 每次失败自动标注阶段（auth/quota/ratelimit/route/provider/timeout），`/admin/attribution` 跨 trace 聚合——现有可观测平台普遍缺的层 | `tracing.py`, `main.py` |

## 快速开始

零外部依赖（Python ≥3.11 + FastAPI + SQLite + mock provider）：

```bash
pip install -r requirements.txt
uvicorn agentgate.main:app --port 8090

# 发起调用（mock provider 直接响应，无需任何真实 API key）
curl -X POST localhost:8090/v1/chat/completions \
  -H "Authorization: Bearer sk-demo-alice" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'

# 控制台
open http://localhost:8090/   # Admin Key: admin-dev-key
```

接入真实模型：编辑 `config.example.toml`，把 provider 改为 `type = "openai"` 并填
`base_url`（Kimi: `https://api.moonshot.cn/v1`）与 `api_key` 即可——调用方代码零改动。

`config.demo.toml` 提供了带故障注入的演示配置（100% 失败的 provider、小配额 key、
低速限流 key），可现场演示 fallback、配额 429、限流 429 与失败归因聚合。

## 运行测试

```bash
python -m pytest tests/ -v   # 19 个用例：路由 / 计量 / 熔断 / 端到端 API / 归因
```

## 架构

```
client ──► AgentGate (FastAPI)
             ├─ auth          API key 鉴权
             ├─ quota         每日 token 配额（SQLite 实时累计）
             ├─ ratelimit     滑动窗口 RPM
             ├─ router        alias → 候选链；成本升序 + 熔断器 + fallback
             ├─ providers     mock | OpenAI 兼容适配器（Kimi/OpenAI/DeepSeek/...）
             ├─ metering      usage → 成本核算 → usage_events 表
             └─ tracing       trace/span 全量记录 + 失败阶段归因
                    │
                    ▼
              dashboard (/)   traces / usage / attribution 可视化
```

## 关键设计决策

1. **成本优先的路由，而非静态优先级**：候选链按 prompt 单价升序重排——简单任务不该
   用最贵的模型。熔断器（连续失败 3 次，冷却 60s）保证故障 provider 自动沉底，
   恢复后自动回到候选池。
2. **归因即数据**：tracing 回答“发生了什么”，归因回答“为什么坏了”。每次失败在发生的
   阶段被打标（auth/quota/ratelimit/route/provider/timeout），跨 trace 聚合后就是
   运维看板：哪类故障在涨，一眼可见。
3. **mock provider 一等公民**：确定性、可模拟延迟与失败率，使整个网关（含测试）零外部
   依赖可运行——这也是 CI 与面试现场演示的前提。
4. **SQLite 起步，接口面向 Postgres**：单写锁 + 参数化 SQL；`storage.py` 是唯一数据访问
   层，替换引擎不改业务代码。

## 后续路线

- 语义路由：按任务类型/上下文长度/历史成功率选模型，而不只是静态价格
- 跨 trace 质量聚合：“某时段某模型错误率漂移”告警（现有平台的空白）
- OTel exporter：span 导出到 Jaeger/Tempo，接入既有可观测栈
- 优先级队列与降级策略：核心业务在配额紧张时优先保障
- Go 重写数据面（控制面留 Python）：对标微秒级代理开销

## License

MIT
