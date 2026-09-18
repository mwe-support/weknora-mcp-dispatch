# WeKnora v0.8.0 clean MCP 兼容验收

日期：2026-09-18。结论：**现有 MWE MCP 可与 clean 的 WeKnora v0.8.0 r4 配合使用，已完成真实 MCP 协议、核心读写、检索、RAG 对话和知识库范围权限验收，无需修改 MCP 业务代码。**

## 版本与部署范围

- 专属仓库：`mwe-support/weknora-mcp-dispatch`；目标分支：`weknora-v0.8.0`。没有向官方仓库或主应用仓库混入 MCP 修改。
- 验收源码基线：`1b1687401671c501d4d2431e6aab8cf5ad3d8586`。运行容器的 `weknora_mcp_server.py`、`weknora_mcp_auth.py`、`upload_paths.py` SHA-256 与该提交一致。
- 官方 WeKnora v0.7.2→v0.8.0 的 `mcp-server` 目录无差异；本次 MCP `serverInfo.version` 仍为 **1.1.1**，运行 MCP Python SDK **2.0.0**。应用版本 v0.8.0 与 MCP 自身版本是两个概念。
- clean 后端：`marvel/weknora-app:v0.8.0-mwe-port-20260918-r4`。
- 复用已部署的 MCP 镜像并固定 ID：`sha256:baf882f167f0df5fb463699536370cd587b2ed5e69af6b1e23265023bad1bf84`。没有更换依赖或重新拉取浮动版本。
- 新增容器 `WeKnora-clean-mcp`，仅加入 clean 网络，后端固定 `http://WeKnora-clean-app:8080/api/v1`；请求级 `weknora_api_key` 鉴权，静态 `WEKNORA_API_KEY` 为空，`MCP_READ_ONLY=false`，实际操作权限仍由每请求 Key 的后端授权决定。
- 验收入口：**服务器本机** `http://127.0.0.1:28182/mcp`，不是电脑无需隧道即可访问的地址。没有开放新的公网/LAN 监听端口；生产 MCP 容器镜像、启动时间和现有客户端配置保持不变。

本机客户端如需访问该 clean 入口，可通过已有 SSH 连接建立隧道：

```powershell
ssh -N -L 127.0.0.1:28182:127.0.0.1:28182 marvel-kb
```

随后客户端使用 `http://127.0.0.1:28182/mcp`，通过环境变量提供 clean 空间 API Key。不要把 Key 写入仓库或聊天。当前生产 MCP 的客户端配置未切换到此入口。

## 实际验收

全部 **28 项运行时断言通过**；工具目录完整列出 **30 个工具**，不代表 30 个工具的所有参数和管理操作均已执行。

| 场景 | 结果 |
|---|---|
| `initialize`、通知、分页 `tools/list`、`tools/call` | 成功，Streamable HTTP 可用 |
| 缺少 Key / 无效 Key | 分别返回 401 / 403 |
| 真实知识库列表与详情、文档列表与详情、分块读取 | 成功，实际调用 clean v0.8.0 |
| 真实扫描 PDF 的 OCR 正文检索 | 通过知识库名称解析到正确 UUID，并命中预期文档 |
| 共享知识库、模型、智能体、会话列表 | 调用成功 |
| 创建临时知识库、Markdown 入库 | 成功，解析完成且可检索 |
| 从 `/uploads` 导入合成 TXT | 成功，解析完成 |
| 请求读取上传目录之外的 `/etc/passwd` | 被拒绝，未导入 |
| 仅允许一个 KB 的 `retrieve` Key | 列表仅返回该 KB，访问其他 KB 被拒绝，内容写入被拒绝 |
| 12 次并发、两种 Key 交替读取 | 当前请求的 KB 范围正确，没有相互串用 |
| 创建/读取会话、RAG `chat` | 返回 168 字回答，回答本身包含合成文档规定的 47 天；返回 4 条引用。关闭外部网页搜索 |
| 删除合成分块并重新列出 | 分块已消失 |
| 撤销已缓存验证的 Key 后再读数据 | 被拒绝；鉴权缓存没有绕过后端撤销检查 |
| 删除测试会话、文本和测试知识库 | 成功，包含上传文档的临时库已清理 |

真实业务资料只用于读取和检索；写入、分块删除和会话操作均使用明确标识的合成夹具。临时 Key 通过 v0.8.0 正式接口签发，设置一小时有效期，并在测试后撤销。

## 回归命令与清理证据

```text
python -m pytest -q
  21 passed
python -m unittest -q test_mcp_transports test_stdio_stdout test_create_knowledge_from_text test_file_path_security
  Ran 15 tests ... OK
python3 acceptance.py
  28 runtime checks passed
python3 final-check.py
  temporary keys / fixture KBs / fixture workspaces active count = 0
```

15 项补充测试覆盖 HTTP 无状态传输、旧 SSE 路径、线程会话隔离、stdio 输出纯净、文本发布及上传路径约束。21 项测试覆盖请求级鉴权模块、SDK 集成和名称解析。

最终检查：clean 的两处共享盘业务文档仍为 **2,043** 份；生产 MCP 未变；临时 Key、测试知识库、测试空间活动计数均为 **0**。合成上传源文件保留在只读测试目录，可用于重现实验；它不是业务文档。

结构化回执：[运行时验收](v080-clean/acceptance-results.json)、[部署信息](v080-clean/deployment.json)、[最终检查与源码哈希](v080-clean/final-check.json)。脚本位于工作区 `tmp/v080-mcp-20260918/` 和服务器 `/public/knowledgebase/weknora-clean-20260913/build/v080-mcp-20260918/`。回执没有 API Key、JWT、密码或业务正文。

## 未覆盖项与回滚

- 当前账号切换到非成员业务空间被拒绝（403），创建另一个临时空间被拒绝（429）。未修改角色、配额或绕过限制。因此，真实后端的“双工作空间、两个有效 Key”并发场景未执行；多租户上下文有自动化测试，同空间不同 KB 范围有上述真实并发测试，不能混称为完整跨空间实测。
- 未执行 `agent_chat`、Wiki 内容流程、外部 URL 抓取和模型/空间管理写入；智能体列表读取与 RAG `chat` 已执行。
- 当前 clean MCP 作为本机验收入口保留。若需撤销，只停止并移除 `WeKnora-clean-mcp`，移除 clean Compose 中本次新增的 `mcp` 服务条目；不要对整个 clean 项目执行 `down -v`。修改前 Compose 保存为构建目录中的 `compose.before-mcp.json`（私有配置，不得输出）。
- 本批次仓库修改仅为兼容性说明和证据，没有新的业务代码、数据库迁移或生产发布镜像。
