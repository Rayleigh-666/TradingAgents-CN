# TradingAgents-CN 个性化底座 · 定制切入点指南

> 基于 TradingAgents-CN v3.0.0（commit `51060a7`）实际代码勘察整理
> 目标：用**最低侵入**的方式做个性化定制，保持与 `upstream/v3.0` 的可同步性

---

## 0. 总原则

**能配置 → 能新增 → 才改核心。**
必须改核心时，把改动收敛到「一处、几行」，并统一打标记，方便 rebase 上游时定位。

核心成本不在 git，在**冲突**。所以定制的形态比定制的功能更重要。

---

## 1. 侵入性分级总览

| 档位 | 手段 | 改源码？ | 上游同步成本 |
|---|---|---|---|
| **A** | DB / UI 配置、`.env` | 否 | 零 |
| **B** | 新增文件（技能包、Agent、Provider 子类） | 否 | 零，新文件不冲突 |
| **C** | 改核心 1~2 处硬编码 | 是 | 低，冲突点已知且集中 |
| **D** | 大范围改 `core/` | 是 | 高，**尽量避免** |

---

## 2. A 档：零改码

### A1 · 提示词（Prompt）— 性价比最高
- **机制**：MongoDB 集合 `prompt_templates` + `user_template_configs`
- **证据**：`tradingagents/utils/template_client.py:43-67`
- **生效优先级（高 → 低）**：
  1. 调试模板
  2. 节点专属模板（`workflow_id` + `node_id`）
  3. 流程专属模板（`workflow_id`）
  4. 用户配置模板
  5. 用户偏好系统模板
  6. 默认系统模板
- **默认 Prompt 从哪来**：`app/services/system_prompt_seed_service.py`，启动时把代码里的默认值播种进 DB
- **怎么做**：走 UI / 直接写 DB。**不要改代码里的硬编码 fallback**

### A2 · 大模型（LLM）厂商
- **机制**：未知 provider 自动降级为 OpenAI 兼容模式
- **证据**：`tradingagents/graph/trading_graph.py:379-416`
- 任何 **OpenAI 兼容**厂商（DeepSeek / Qwen / GLM / Kimi / 本地 vLLM…）**零改码**：
  在 UI/DB 里配置 provider 的 `base_url` + API Key 即可。
  Key 支持 `<PROVIDER>_API_KEY`、`CUSTOM_OPENAI_API_KEY` 等环境变量形式
- 若要走 v2 适配器通道：基类在 `core/llm/providers/base.py`

### A3 · 数据源开关与优先级
- **证据**：`.env` 开关 `ENABLE_*`（`app/core/unified_config.py:24-48`）；
  优先级从 DB `system_configs` 读取（`tradingagents/dataflows/data_source_manager.py:110-176`）
- **怎么做**：只调整**已有**数据源的启用与排序，完全不碰代码

### A4 · Agent 运行参数
- **证据**：DB `agent_configs`（`core/config/agent_config_manager.py`）；
  代码侧默认值 `core/agents/config.py:242` 的 `BUILTIN_AGENTS`
- **优先级**：DB `agent_configs` > 代码里的 `BUILTIN_AGENTS` 默认值
- **怎么做**：通过 DB/UI 覆盖某个 Agent 的模型、温度、工具绑定等

---

## 3. B 档：只新增文件（最推荐的个性化载体）

### B1 · 技能包（Skill）★ 首选
- **目录约定**：`skills/<name>/SKILL.md`
- **解析器**：`core/skills/parser.py`（读 YAML frontmatter）
- **执行协议**：`python` / `http` / `mcp` 三种
- **现成示例**：`skills/a-share-runtime-data-access/`、`skills/ifind-repilot-finance-data-search/`、
  `skills/ifind-repilot-news-search/`、`skills/nanobot-agent-design/` 等
- **怎么做**：新建一个目录 + 一个 `SKILL.md`，声明协议与入口即可，**零核心改动**
- **为什么首选**：把你的「独有能力」封装成技能包，是侵入性最低、复用性最高的方式

### B2 · 自定义 Agent
- **注册装饰器**：`core/agents/registry.py:163` 的 `@register_agent`
- **基类**：`core/agents/base.py`，以及 `analyst.py` / `researcher.py` / `manager.py` / `trader.py`
- **免写代码路线**：`core/agents/universal.py` 的 `UniversalAgent`，纯配置驱动
- **怎么做**：新增一个 `.py`，继承基类 + 加装饰器；或直接用 `UniversalAgent` + DB 配置

### B3 · 自定义数据源 Provider
- 官方在文件头写明了步骤 —— `tradingagents/constants/data_sources.py`：
  1. 在 `DataSourceCode` 枚举加编码
  2. 在 `DATA_SOURCE_REGISTRY` 注册
  3. 实现 provider 接口（基类 `tradingagents/dataflows/providers/base_provider.py:11`）
  4. 更新前端的数据源选项（可选）
- ⚠️ 注册完还需在适配器列表里挂一次 → 见 **C2**

### B4 · 结果后处理器
- **目录**：`core/agents/post_processors/`
- **已有**：`report_saver` / `email_notifier` / `system_notifier` / `report_generator_v2` / `data_preparer_v2`
- **配置示例**：`config/post_processors_example.yaml`、`config/workflow_with_post_processing_example.yaml`
- **怎么做**：照现有处理器写一个新的，挂到工作流上

---

## 4. C 档：必须改核心（目前只有两处硬编码边界）

### C1 · 非 OpenAI 兼容的 LLM 协议
- **位置**：`tradingagents/graph/trading_graph.py` 的 `create_llm_by_provider()` if/elif 链
- **改法**：加一个 `elif provider == "xxx":` 分支，几行代码
- 只有**自研/私有协议**才需要动这里

### C2 · 全新数据源协议
- **位置**：`app/services/data_sources/manager.py` 中硬编码的适配器类列表
- **改法**：把新适配器类名挂进列表
- 实现完 Provider 后，**必须**在这里登记一次

> 这两处是目前扩展性的**硬边界**。
> 建议在改动处统一加注释标记，例如 `# [PERSONAL] 自定义 provider`，rebase 上游时可快速定位。

---

## 5. 前端定制

| 项 | 可配置？ | 位置 |
|---|---|---|
| API 地址 / 端口 | ✅ | `frontend/.env.local`（已被 gitignore） |
| 主题色 | ✅ 改一个变量 | `frontend/src/styles/variables.scss` 的 `$primary-color` |
| Logo / favicon | ✅ 替换资源 | `frontend/public/` |
| 侧边菜单（Pro 位） | ✅ | `frontend/src/config/navigation.pro.ts`（社区版为空，是留给扩展的钩子） |
| 应用标题 | ⚠️ 硬编码 | `frontend/src/stores/app.ts`、`frontend/src/router/index.ts` |

品牌名硬编码在两处，改动很小但属于「改源码」，建议最后再做。

---

## 6. 别碰 / 慎碰清单

- **`config/*.json`**（`models.json` / `pricing.json` / `settings.json` / `usage.json`）
  → 运行时**自动生成**，`.gitignore:76-77` 已忽略，`config/README.md` 明确写「请勿提交到公共仓库」。
  **改配置请走 `.env` 或 DB，不要改这些文件。**
- **`core/` 核心目录**：尽量不碰，尤其 `core/workflow/`、`core/state/`、`core/gateway/`
- **`config/searxng/searxng-local-modifications.patch`**：那是给第三方 SearXNG 打的本地补丁，与应用配置无关，别误改
- 已 gitignore 的本地目录：`data/`、`logs/`、`.workbuddy/`、`frontend/.env*`、`config/backup/`

---

## 7. 推荐落地顺序

1. 先用 **A 档** 把模型、Prompt、数据源调成你要的样子（零风险，先跑通）
2. 再把独有的能力做成 **B1 技能包**（最干净的个性化载体）
3. 需要新 Agent 走 **B2**；需要新数据源走 **B3 + C2**
4. 前端品牌最后做

---

## 8. 日常同步上游

```bash
git fetch upstream
git rebase upstream/v3.0     # 把你的提交重放到最新上游之上
```

改动越集中在「新增文件」和「那两处 C 档边界」，这一步越省事。

- `origin` → 你的仓库（push 目标）
- `upstream` → `hsliuping/TradingAgents-CN`（官方更新来源）
- `main` → 你的定制开发线
- `v3.0` → 纯上游镜像线
