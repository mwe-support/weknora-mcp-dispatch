# 平台 API Key 鉴权与按请求空间路由

日期：2026-10-15。上游应用基线 WeKnora v0.8.0；MCP Server 基线 1.1.1 / MCP Python SDK 2.0.0。仓库 `mwe-support/weknora-mcp-dispatch`，目标分支 `weknora-v0.8.0`，`main` 同步该版本分支。本批只修改 MCP，不修改主项目。

## 问题与修复条目

条目 `platform-key-routing`，实现提交 `881429f81561b1a8e86f79642978d2242057bc98`（`fix(mcp): authenticate platform keys and forward request workspace`）。

同一有效平台 Key 在 REST 读取成功，但在 MCP 初始化和读取时返回 403。根因是 MCP 用不带目标空间的 `/knowledge-bases` 验证平台 Key，将后端已认证后的 `409 / TENANT_REQUIRED` 当成无效凭据；工具请求也没有转发目标空间。

- 验证请求转发可选 `X-Tenant-ID`；验证缓存使用 Key 与空间组合的 SHA-256 指纹，避免一个空间的允许/拒绝污染另一个空间。
- 未指定空间时，仅接受后端明确的 `409` 且 JSON `code=TENANT_REQUIRED` 作为平台身份已认证信号，允许协议初始化和工具发现。其他冲突不放行。具体工具能力、空间归属、Key 撤销/过期和知识库范围仍由每次后端请求校验。
- 请求上下文绑定目标空间，客户端独立保留该空间，转发到同步工具线程创建的所有 HTTP Session；不共享或改写其他请求的 Session。
- 拒绝重复、空、非 ASCII 十进制、零、负数或超出 uint64 的空间头；合法值规范化后进入缓存及请求上下文。
- `list_tenants(all_spaces=true)` 显式调用 `/tenants/all`；需 `system_tenants_read` 或 `system_tenants_manage`。默认调用仍使用 `/tenants`，保持旧客户端行为。
- README 同步平台 Key 配置、逐请求空间选择和固定请求头客户端的使用方式；本项文档变更属于同一发布条目。

## 用户与部署影响

平台 Key 可以初始化 MCP、发现工具并枚举其能力允许的空间。读取知识库时，每个 MCP HTTP 请求增加 `X-Tenant-ID: <知识库所属空间ID>`，并具有 `retrieve` 能力。固定请求头的客户端可按空间配置多个连接并复用同一个 Key。未指定目标空间的知识库读取仍拒绝；空间 Key 仍不能切换到其他空间。

未新增工具或更改现有工具名；`list_tenants` 增加可选参数。没有修改 Key、空间成员、共享关系、主应用、前端、模型、解析器或 clean 部署。

生产镜像：`local/weknora-mcp-dispatch:v0.8.0-mwe-platform-key-20261015-r1`。

- 镜像 ID：`sha256:df1ff3ecde7ae7057149b866ec5a5b257d4e55a0690a2e9f79dcaf7e2455bcc3`。
- 保留运行基线：`local/weknora-mcp-dispatch:v0.7.2-mwe-1b16874`，镜像 ID `sha256:baf882f167f0df5fb463699536370cd587b2ed5e69af6b1e23265023bad1bf84`；保留原有 Python/SDK/依赖，只覆盖两个 MCP 源文件，不重新解析浮动依赖。
- 持久构建目录：`/public/knowledgebase/mcp-platform-key-20261015/source`；`Dockerfile.production` 以原运行镜像为 FROM，仅 COPY `weknora_mcp_auth.py`、`weknora_mcp_server.py` 并标记正确源码提交。
- 生产 override 的 MCP `image` 和 `build` 指向该镜像与构建目录，其他配置保持。
- 仅通过以下命令重建 MCP；`--profile minio` 用于满足现有 Compose 的 app/minio 配置校验，`--no-deps` 保证不重建依赖：

```sh
cd /public/knowledgebase/weknora/src
docker compose -p src --profile minio -f docker-compose.yml -f ../override.yml up -d --no-deps --no-build mcp
```

运行文件哈希已与构建源文件逐一匹配：

| 文件 | SHA-256 |
|---|---|
| `weknora_mcp_auth.py` | `18585550a1556cb9c91056940c624adbf7c81acdf6c4c458258a2d8c055d317d` |
| `weknora_mcp_server.py` | `ddd9aae94dd0ac1f5063bf2933f99d1a1cbd2e10ee50cd28ae4faedb13259759` |

生产切换前后其他 30 个运行容器 ID 保持；候选容器已清除。MCP 匿名请求返回 401，运行正常。

## 验证命令与结果

回归测试先在旧代码上运行新增测试，复现平台初始化拒绝、转发丢失、缓存空间混用和并发空间丢失；修复后，在与生产相同 SDK 2.0.0 运行环境的隔离容器中执行：

```sh
PYTHONPATH=/tmp/test-deps:/app:/app/tests python -m pytest -p no:cacheprovider tests test_file_path_security.py test_mcp_transports.py test_stdio_stdout.py
```

结果：**41 passed，11 subtests passed**。测试用 pytest/pytest-asyncio 仅安装在一次性容器的 `/tmp/test-deps`，不改变生产依赖。覆盖原空间 Key、共享密钥、工具路径限制、stdio/HTTP/SSE 兼容、平台无空间发现、精确 409 认证、缓存隔离、非法/重复头和同 Key 跨空间任务/工作线程隔离。

生产公网使用用户通过本地隐藏输入提供的既有平台 Key，指纹仅在进程内存校验；真实 MCP ClientSession SDK 1.26.0，协商协议 `2025-11-25`，自动执行初始化及 initialized 通知。只调用读取工具，业务正文不落盘。

| 验证 | 结果 |
|---|---|
| 不指定空间初始化、发现工具 | 成功，30 工具；`list_tenants` 新参数在 schema 中存在 |
| `list_tenants(all_spaces=true)` | 22 个空间，与数据库未删除空间 ID 集合一致 |
| 逐空间 `list_knowledge_bases` | 22/22 与数据库知识库 ID 集合一致 |
| `get_knowledge_base` / `list_knowledge` | 各 49/49 成功，详情 ID/所属空间回验一致 |
| 非空库抽样 `get_knowledge` / `list_chunks` | 各 46/46 成功，文档归属和分块归属回验一致；3 库为空 |
| 同 Key、两空间并发列表，重复 3 轮 | 6/6 返回各自空间准确目录，无串库 |
| 未指定空间调用知识库读取 | 工具返回错误，符合预期 |
| 匿名 / 无效 Key / 非法空间 / 不存在空间 | HTTP 401 / 403 / 400 / 403 |

安全回执在工作区 `reports/platform-api-key-live-20261015/`：`tests.log`、`build.json`、`deployment.json`、`mcp-after-result.json`；生产侧构建/部署回执在同名日期发布目录。没有向仓库提交 Key、Key 哈希、令牌、业务正文或私有配置备份。

## 回滚与残余风险

原镜像保留；生产私有备份位于 `/public/knowledgebase/mcp-platform-key-20261015/private/override.before.yml`（目录 700、文件 600）。回滚脚本只将当前 override 的 MCP `image`/`build` 恢复到备份值，断言其他字段保持，不覆盖后续其他服务修改：

```sh
python3 /public/knowledgebase/mcp-platform-key-20261015/rollback_mcp.py
```

脚本执行同样带 `--profile minio`、`--no-deps`、`--no-build` 的 MCP 重建，并检查匿名请求 401。回退会恢复原空间 Key 功能，同时重新失去平台 Key 的 MCP 兼容性。本轮未执行生产回滚演练；脚本语法及配置差异断言已核验。

验证缓存的既有有效 TTL 仍为 60 秒；撤销后的协议连接判定可能在 TTL 内保留，但每次实际工具调用仍由后端重新鉴权，不因缓存得到业务权限。真实撤销/到期测试未修改用户 Key，相关拒绝由后端既有规则承担。

本次验证访问权限和目录覆盖，不证明检索质量、模型回答正确性、全库逐篇内容完整性或写入/删除行为。未执行业务写入、删除、模型调用或 GPU 任务。
