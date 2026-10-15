# WeKnora MCP Dispatch

当前维护与GitHub默认分支为`weknora-v0.8.0`，`main`同步同版内容；[本批整理与复审](docs/releases/2026-09-22-v080-github-publication.md)。历史v0.7.2不再接收当前版本定制。

这是当前面向 WeKnora v0.8.0 的多用户 MCP 网关，基于官方 MCP Server 1.1.1
（MCP Python SDK 2.x 高层 API）改造。一个 Streamable HTTP 服务实例可以接收
多个客户端请求，并以每个客户端提交的 WeKnora API Key 决定其可访问的空间和知识库。

v0.8.0 的真实协议、读写、检索、对话及权限验证结果见
[clean MCP 兼容验收](docs/releases/2026-09-18-v080-clean-mcp-acceptance.md)，包含未覆盖范围和回滚方式。

## 上游优先的模块结构

- `weknora_mcp_server.py` 保留官方 v0.7.2 的 MCPServer、30 个工具和
  `WeKnoraClient` 实现，只包含少量模块接线代码。
- `weknora_mcp_auth.py` 独立实现请求级 Bearer 鉴权、Key 验证缓存、
  `ContextVar` 身份隔离、客户端分派和只读工具策略。
- `tests/test_auth_module.py` 从模块公开接口验证鉴权行为；
  `tests/test_request_scoped_auth.py` 只验证模块与 MCP SDK 2.x 的集成。

后续同步官方版本时，应先更新官方 MCP 文件，再单独适配鉴权模块接线，避免把
鉴权实现重新混入官方工具业务代码。

## 定制能力

- `MCP_AUTH_MODE=weknora_api_key`：将 Bearer Token 作为 WeKnora API Key 验证。
- API Key 仅绑定到当前 MCP 请求上下文，异步任务和同步工具工作线程均保持租户隔离。
- 动态 Key 模式不会回退到服务端静态 `WEKNORA_API_KEY`，上下文丢失时失败关闭。
- 验证缓存只保存 SHA-256 指纹；缓存容量、TTL 和验证并发均有上限。
- 生产环境设置 `MCP_READ_ONLY=false`，暴露官方 30 个工具；每次调用仍由请求中的
  WeKnora API Key 在后端执行真实权限校验。需要纯查询端点时可改回 `true`。
- `create_knowledge_from_file` 只允许读取 `MCP_ALLOWED_UPLOAD_DIRS` 下的服务端文件；
  生产 Compose 将专用宿主机目录只读挂载到 `/uploads`，不允许扫描容器工作目录。
- 保留上游 `shared_secret` 和 stdio 模式，便于兼容原有部署。

### 平台 API Key 与目标空间

平台 Key 使用同样的 `Authorization: Bearer <Key>`。初始化和工具目录发现无需指定空间；
调用 `list_tenants(all_spaces=true)` 可枚举所有空间，Key 需包含 `system_tenants_read`
或 `system_tenants_manage`。默认 `list_tenants()` 保留原来的当前空间列表行为。

单连接跨空间使用：先调用 `list_tenants(all_spaces=true)`，然后将返回的空间 ID 作为
工具可选参数 `tenant_id`，例如 `list_knowledge_bases(tenant_id=10000)`、
`hybrid_search(kb_id="<库ID>", query="<问题>", tenant_id=10000)`。
29 个空间相关工具均提供此参数，包括文档/分块读取、问答、智能体及已有管理工具；
只有全局 `create_tenant` 不需要它。参数严格接受正 uint64 整数，并由后端逐次核验 Key 权限。

MCP 初始化会提供发现指引：用户未指定空间时，Codex 自行枚举空间与候选知识库，
选择相关来源检索并标注来源；用户无需提供空间或知识库 ID。工具能力仍由 Key 决定。

Codex 单连接配置见 [`examples/codex-platform-key.toml`](examples/codex-platform-key.toml)。
此配置不设置固定空间头，只启用枚举和知识库读取/检索工具，Key 从环境变量读取。

保留固定空间连接的用法：在 MCP HTTP 请求中增加 `X-Tenant-ID: <空间ID>`，例如：

```text
Authorization: Bearer <平台 API Key>
X-Tenant-ID: 10000
```

省略 `tenant_id` 时，工具使用该请求头；显式 `tenant_id` 优先于请求头，且只影响当前工具调用。
同一连接连续或并发调用不同空间时，无需改配置、重连或变更 API Key。
Key 仍需具有相应能力（读取为 `retrieve`），知识库范围仍由 WeKnora 后端校验。
空间 Key 不能借此切换到其他空间。目标空间在请求、工具调用及同步工具线程中隔离，
验证缓存按 Key 和空间的组合分开保存，重复、非法或超出 uint64 范围的空间头会被拒绝。

请求链路：

```text
MCP client
  ├─ 可选：外层网关鉴权请求头
  └─ Authorization: Bearer <WeKnora API Key>
          ↓
WeKnora MCP Dispatch
  ├─ GET /knowledge-bases 验证 Key，同时携带可选 X-Tenant-ID
  ├─ 绑定当前请求身份与目标空间
  └─ X-API-Key / X-Tenant-ID 转发至 WeKnora API
```

## 生产 Docker 配置

使用本仓库构建镜像，并参考
[`production.request-scoped.override.yml`](./production.request-scoped.override.yml)
覆盖 WeKnora 官方 Compose 的 `mcp` 服务。请求级模式下应保持
`WEKNORA_API_KEY` 和 `MCP_SERVER_AUTH_TOKEN` 为空。

```bash
docker build -t local/weknora-mcp-dispatch:v0.7.2 .
```

关键环境变量：

| 变量 | 推荐值 | 说明 |
| --- | --- | --- |
| `WEKNORA_BASE_URL` | `http://WeKnora-app:8080/api/v1` | WeKnora API 地址 |
| `MCP_AUTH_MODE` | `weknora_api_key` | 启用请求级空间 Key |
| `MCP_READ_ONLY` | `false` | 暴露全部 30 个工具，实际权限由请求 API Key 决定 |
| `MCP_HTTP_PATH` | `/mcp` | Streamable HTTP 路径 |
| `MCP_ALLOWED_UPLOAD_DIRS` | `/uploads` | 文件导入工具唯一可读取的服务端目录 |
| `MCP_UPLOAD_HOST_DIR` | `/public/knowledgebase/mcp-uploads` | 只读挂载到 `/uploads` 的宿主机目录 |
| `MCP_API_KEY_VALIDATION_TTL_SECONDS` | `60` | 验证缓存 TTL |
| `MCP_API_KEY_VALIDATION_CACHE_MAX_ENTRIES` | `2048` | 最大缓存指纹数 |
| `MCP_API_KEY_VALIDATION_MAX_CONCURRENCY` | `16` | 最大并发验证数 |

## WorkBuddy 配置示例

写入用户级 `~/.workbuddy/mcp.json` 或项目级 `.workbuddy/mcp.json`：

```json
{
  "mcpServers": {
    "weknora": {
      "type": "http",
      "url": "https://kb.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${WEKNORA_SPACE_API_KEY}",
        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}"
      }
    }
  }
}
```

如果入口没有 Cloudflare Access，删除两个 `CF-Access-*` 请求头即可。

## Codex 配置示例

在 `~/.codex/config.toml` 中加入：

```toml
[mcp_servers.weknora]
url = "https://kb.example.com/mcp"
bearer_token_env_var = "WEKNORA_SPACE_API_KEY"
env_http_headers = { "CF-Access-Client-Id" = "CF_ACCESS_CLIENT_ID", "CF-Access-Client-Secret" = "CF_ACCESS_CLIENT_SECRET" }
```

如果入口没有 Cloudflare Access，删除 `env_http_headers`。不要把 WeKnora API Key
或外层网关 Secret 明文提交到配置仓库。

## 快速开始

> 推荐直接参考 [MCP配置说明](./MCP_CONFIG.md)，无需进行以下操作。
> 本节沿用上游 stdio/静态 Key 用法；多用户 HTTP 生产部署请使用上面的请求级配置。

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 配置环境变量
```bash
# Linux/macOS
export WEKNORA_BASE_URL="http://localhost:8080/api/v1"
export WEKNORA_API_KEY="your_api_key_here"

# Windows PowerShell
$env:WEKNORA_BASE_URL="http://localhost:8080/api/v1"
$env:WEKNORA_API_KEY="your_api_key_here"

# Windows CMD
set WEKNORA_BASE_URL=http://localhost:8080/api/v1
set WEKNORA_API_KEY=your_api_key_here
```

### 3. 运行服务器

**推荐方式 - 使用主入口点：**
```bash
python main.py
```

**其他运行方式：**
```bash
# 使用原始启动脚本
python run_server.py

# 使用便捷脚本
python run.py

# 直接运行服务器模块
python weknora_mcp_server.py

# 作为 Python 模块运行
python -m weknora_mcp_server
```

### 4. 命令行选项
```bash
python main.py --help                 # 显示帮助信息
python main.py --check-only           # 仅检查环境配置
python main.py --verbose              # 启用详细日志
python main.py --version              # 显示版本信息
```

## 安装为 Python 包

### 从 PyPI 安装

```bash
pip install tencent-weknora-mcp
# 或使用 uvx 直接运行（无需预安装）
uvx --from tencent-weknora-mcp weknora-mcp-server
```

> 官方 PyPI 包名为 **`tencent-weknora-mcp`**（Tencent/WeKnora 维护，Trusted Publishing 发布）。
> 旧社区包 `weknora-mcp` 请不要再使用。
> 安装后命令行入口仍为 `weknora-mcp-server` / `weknora-server`。
> 官方 PyPI 包不包含本仓库的请求级多用户派发功能；生产部署该功能时应构建本仓库。

### 开发模式安装
```bash
pip install -e .
```

安装后可以使用命令行工具：
```bash
weknora-mcp-server
# 或
weknora-server
```

### 生产模式安装
```bash
pip install .
```

### 构建分发包
```bash
# 使用 setuptools
python setup.py sdist bdist_wheel

# 使用现代构建工具
pip install build
python -m build
```

## 测试模组

运行测试脚本验证模组是否正常工作：
```bash
python test_module.py
pytest test_*.py tests
```

PowerShell 不展开 `test_*.py` 通配符，可使用：

```powershell
$rootTests = Get-ChildItem -File -Filter 'test_*.py' | ForEach-Object FullName
python -m pytest @rootTests tests
```

## 功能特性

该 MCP 服务器提供以下工具：

### 空间管理
- `create_tenant` - 创建新空间
- `list_tenants` - 列出所有空间

### 知识库管理
- `create_knowledge_base` - 创建知识库
- `list_knowledge_bases` - 列出知识库
- `list_shared_knowledge_bases` - 列出共享给当前空间的知识库
- `get_knowledge_base` - 获取知识库详情
- `delete_knowledge_base` - 删除知识库
- `hybrid_search` - 混合搜索

### 知识管理
- `create_knowledge_from_file` - 从本地文件创建知识
- `create_knowledge_from_url` - 从 URL 创建知识
- `create_knowledge_from_text` - 从文本创建知识
- `list_knowledge` - 列出知识
- `get_knowledge` - 获取知识详情
- `delete_knowledge` - 删除知识

### 模型管理
- `create_model` - 创建模型
- `list_models` - 列出模型
- `get_model` - 获取模型详情

### 会话管理
- `create_session` - 创建聊天会话
- `get_session` - 获取会话详情
- `list_sessions` - 列出会话
- `delete_session` - 删除会话

### 聊天功能
- `chat` - 发送聊天消息
- `agent_chat` - 使用指定 Agent 发送聊天消息

### Agent 管理
- `list_agents` - 列出当前空间可用的 Agent
- `get_agent` - 获取 Agent 详情

### 块管理
- `list_chunks` - 列出知识块
- `delete_chunk` - 删除知识块

### Wiki 查询
- `wiki_search` - 搜索 Wiki 页面
- `wiki_read_page` - 读取 Wiki 页面
- `wiki_index_view` - 查看 Wiki 索引

## 故障排除

如果遇到导入错误，请确保：
1. 已安装所有必需的依赖包
2. Python 版本兼容（推荐 3.10+）
3. 没有文件名冲突（避免使用 `mcp.py` 作为文件名）

## 调用效果

<img width="950" height="2063" alt="118d078426f42f3d4983c13386085d7f" src="https://github.com/user-attachments/assets/09111ec8-0489-415c-969d-aa3835778e14" />
