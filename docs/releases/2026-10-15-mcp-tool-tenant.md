# 单连接 MCP：工具级空间选择与自动发现指引

日期：2026-10-15。上游应用 WeKnora v0.8.0；MCP Server 1.1.1，运行依赖 MCP Python SDK 2.0.0。目标仓库 `mwe-support/weknora-mcp-dispatch`，版本分支 `weknora-v0.8.0`，`main` 同步。本批不修改主项目。

## 变更条目与动机

条目 `tool-tenant-routing`，实现提交 `36b910d6999b3380659932fb4a53baa733ddc38f`（`feat(mcp): select workspace per tool and guide source discovery`）。

前一批已支持平台 Key，但需要在每个 HTTP 请求中指定 `X-Tenant-ID`。Codex 的固定请求头配置使一条连接固定到一个空间，不能仅凭用户的业务问题自行跨空间检索。本批将目标空间提升为工具参数，并向客户端提供发现流程。

- 所有 29 个空间相关工具增加可选 `tenant_id`，覆盖读取、检索、文档、分块、问答、智能体和原有管理工具；全局 `create_tenant` 不增加此参数。
- `tenant_id` 严格接受正 uint64 整数；字符串、布尔、小数、零、负数和超范围值在执行前拒绝。
- 显式参数优先于有效的 HTTP 空间头；省略/传空值时保持原有请求头行为。不要求改配置或重连以选择另一空间。
- 每个工具调用创建独立请求上下文和后端客户端；同步工具、异步问答及工作线程均继承所选空间。调用结束或异常时关闭其客户端并恢复外层上下文，不修改其他工具调用或 HTTP 请求的 Session。
- 动态认证模式缺失身份时不回退静态 Key；原共享密钥/stdio 模式仍可使用部署 Key，并由后端校验空间范围。
- `list_tenants` 加入只读工具策略，允许只读 MCP 实例提供平台空间发现。
- 初始化 `instructions` 指导 Codex：用户未指定空间时，先 `list_tenants(all_spaces=true)`，再按返回 ID 枚举知识库、选择相关来源检索、标明来源空间/知识库；用户无需提供 ID。已选来源不宣称覆盖所有内容。
- README 和 `examples/codex-platform-key.toml` 同步单连接配置与调用流程。配置只启用读取/检索工具，不包含 Key 或固定空间 ID；属于同一发布条目。

未新增依赖。使用 MCP SDK 运行环境中原有的 Pydantic 验证参数，不改变 Key 能力、共享关系、成员授权、上游 API 或业务数据。空间 Key 仍不能通过参数跨空间。

## 生产部署与影响

镜像 `local/weknora-mcp-dispatch:v0.8.0-mwe-tool-tenant-20261015-r1`，镜像 ID：
`sha256:12a83a1f538081fd0d87b6dff954f0c4df2ce6b0e4e0bea8b95eb049f8dc380e`。

基于上一批已验证镜像 `v0.8.0-mwe-platform-key-20261015-r1`（ID `sha256:df1ff3ecde7ae7057149b866ec5a5b257d4e55a0690a2e9f79dcaf7e2455bcc3`），保留 Python/SDK/运行依赖，只覆盖两个源文件，并标记正确源码提交。

持久构建路径 `/public/knowledgebase/mcp-tool-tenant-20261015/source/Dockerfile.production`；生产 override 仅调整 MCP `image` 和 `build`。执行：

```sh
cd /public/knowledgebase/weknora/src
docker compose -p src --profile minio -f docker-compose.yml -f ../override.yml up -d --no-deps --no-build mcp
```

其他 30 个运行容器 ID 保持，候选容器已删除。未重建主应用、前端、clean、模型或解析器。构建与生产运行文件 SHA-256 逐一一致：

| 文件 | SHA-256 |
|---|---|
| `weknora_mcp_auth.py` | `26cade3e57e814ec1f7d16b47ff2788cbf17180dbd1a430a2bc91b7fd8167531` |
| `weknora_mcp_server.py` | `51eaa3a4ba896deac1eb199bd62cff8dae6e5c005c72539f37578ebe6a04f613` |

## 验证

新增回归在前批原始源码上运行失败，覆盖缺少参数/schema、参数未转发、异步/线程上下文及发现指引缺失；修复后，在相同 MCP SDK 2.0.0 运行基线上执行：

```sh
PYTHONPATH=/tmp/test-deps:/app:/app/tests python -m pytest -p no:cacheprovider tests test_file_path_security.py test_mcp_transports.py test_stdio_stdout.py
```

**48 passed，75 subtests passed。** 覆盖全部 29 工具参数 schema 和路由、显式参数优先级、旧请求头/省略参数、异常恢复与客户端关闭、同步/异步并发、空间 Key 拒绝跨空间、严格参数类型、动态模式丢失身份拒绝、静态模式兼容、只读发现策略，以及此前上传路径、stdio、HTTP/SSE 和认证缓存回归。

Codex TOML 通过 Python `tomllib` 校验，并通过本机 `codex -c '<临时示例配置>' mcp get weknora_single_config_example --json` 读取验证；没有改用户实际配置，输出没有凭据值。配置字段依据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

生产验收复用此前隐藏输入并匹配的同一个既有平台 Key。用户要求避免重复输入后，从服务器上同一条加密 Key 记录在进程内存解密，校验其 SHA-256 与存储哈希相符；不生成新 Key、不记录明文或哈希。连续两次完整复验均通过：每次真实 MCP SDK ClientSession 只初始化一次，全程没有固定 `X-Tenant-ID`，所有访问 ID 从 MCP 返回取得；数据库仅用于对照覆盖，不提供调用目标。

| 场景 | 结果 |
|---|---|
| 初始化指引、工具发现 | 30 工具，29 个可选 `tenant_id` schema；发现指引已返回 |
| `list_tenants(all_spaces=true)` | 22 个空间，与数据库目录一致 |
| 逐空间库列表和共享库列表 | 22/22 成功，库 ID 集合与数据库完全一致 |
| 知识库详情、文档列表 | 各 49/49 成功 |
| 非空库抽样文档、分块读取 | 各 46/46 成功，归属一致；3 库为空 |
| 同一 ClientSession 并发两空间，3 轮 | 6/6 返回正确目录，无串空间 |
| 非法/不存在的工具空间参数 | 7/7 拒绝；随后空间枚举仍正确 |
| 缺少目标空间的知识库读取 | 按预期返回工具错误 |
| 匿名/无效 Key | HTTP 401/403 |

安全回执在工作区 `reports/mcp-tool-tenant-20261015/`：`tests.log`、`build.json`、`deployment.json`、`mcp-single-result.json`、`mcp-single-success-1.json`。原始红灯日志和服务器回执位于同名服务器发布目录。Key、Key 哈希、业务正文和私有配置备份未提交仓库。

首次 SDK 检查在 15 空间/35 库后以 ExceptionGroup 中断，未保留其内层异常，未能完整归因；MCP 无重启或 OOM。代码未再变更，复用同 Key 连续两次完整复验均成功，均为单会话、22 空间/49 库。保留这一首次异常边界，不将成功复验解释为已定位其根因。

## 回滚与边界

回滚配置在服务器私有目录 `/public/knowledgebase/mcp-tool-tenant-20261015/private/override.before.yml`，目录 700、文件 600。前批镜像保留。回滚脚本只恢复 MCP image/build 字段，断言其他配置保持：

```sh
python3 /public/knowledgebase/mcp-tool-tenant-20261015/rollback_mcp.py --check-only
python3 /public/knowledgebase/mcp-tool-tenant-20261015/rollback_mcp.py
```

只读回滚预检已通过；未执行生产回滚演练。回退将失去工具 `tenant_id` 与自动发现指引，恢复前批固定 HTTP 空间头的用法。变更后客户端需重连 MCP 以刷新工具 schema 和初始化指引。

发布首轮验证权限、目录覆盖与读取，不执行业务写入、删除、模型问答或 GPU 检索；管理和问答工具的参数路由由隔离回归验证。发布首轮未运行真实 Codex 模型回合，后续验收如下。实际业务问题的知识库选择与答案质量仍由 Codex 决策和检索内容决定，发现指引不等于对每个自然语言问题的路由质量保证。

## 后续：本机 Codex 配置与真实无空间提示词验收

同日已将本机用户配置中的现有 `weknora` 条目切换为此前验证的同一平台 Key；沿用客户端支持的 Authorization 入口，不设置 `enabled_tools`、`disabled_tools` 或固定空间头，实际发现 30 个工具。配置文件访问限于当前 Windows 用户、SYSTEM 和管理员；旧 WeKnora 条目仅作 DPAPI 当前用户加密备份。其他连接、默认模型及全局审批设置保持。没有向仓库提交实际用户配置或凭据。

本次不改变生产镜像或源代码，仍为本文件记录的 `v0.8.0-mwe-tool-tenant-20261015-r1`。调用已安装的 Codex CLI 0.147.0，通过真实模型回合和实际 WeKnora 配置测试普通业务问题，提示词没有空间/知识库名称或 ID。

- 测试通过，约 83.8 秒，32 次 MCP 调用全部成功。
- 自动枚举 22 个空间，自行发现 3 个空间内的 11 个知识库。
- 查询文档目录并读取 4 份相关文档及分块，最后给出带来源说明的回答，没有要求用户指定空间。
- 全部调用目标 ID 均来自前序 MCP 枚举结果，没有预置 ID。
- 调用构成：空间枚举 1 次、知识库列表 3 次、文档列表 17 次、文档详情 4 次、分块读取 7 次。

测试条件：该 CLI 的 ChatGPT 入口拒绝本机默认 `gpt-6.1-sol`，因此根据其最新模型目录，仅在测试命令中临时使用 `gpt-5.6-sol`；全局模型未改。非交互式调用需要临时将已获用户授权的七个只读工具设为 `approval_mode="approve"`，该设置没有写回用户配置，写入/删除工具未自动批准。

生产 GPU 当时繁忙，本次通过目录与正文分块查阅完成检索，没有使用 `hybrid_search`、问答或 GPU 路径。其他 MCP、shell、网页搜索、多代理仅在测试进程关闭，配置本身仍开放全部 WeKnora 工具。该用例证明自动发现/选库/文档检索流程，不覆盖向量检索质量、其他业务问题或政策结论的人工逐条复核。

脱敏证据位于工作区 `reports/mcp-tool-tenant-20261015/codex-e2e/result.json`，配置和回滚说明见 `reports/2026-10-15-local-codex-weknora-acceptance.md`。记录不包含文档正文、完整业务回答、Key 或原始会话输出。旧本机配置可通过同工作区 `rollback_local_codex.py` 恢复 WeKnora 条目，已完成只读预检；无需回滚生产服务。现有桌面连接若缓存旧配置，需要重新连接。
