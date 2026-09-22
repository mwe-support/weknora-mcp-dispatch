# v0.8.0版本分支发布整理

日期：2026-09-22。仓库`mwe-support/weknora-mcp-dispatch`；目标`weknora-v0.8.0`，同步main并设v0.8.0为默认分支。旧v0.7.2不变，不混入主项目代码。

## 范围与动机

旧GitHub main/v0.7.2均为`944bbb07`，此前尚无远端v0.8.0。本批包括以下既有独立提交及本次配置修复：

- `f453350`：把请求级Bearer鉴权、指纹缓存、ContextVar凭据分派和只读策略抽出为weknora_mcp_auth；保留官方工具模块形状，避免升级时混入认证实现。
- `1b16874`：开放官方30工具，后端APIKey仍决定实际能力/KB范围；文件上传仅限显式目录，生产以只读挂载提供上传目录。开放工具不等于扩大后端权限。
- `2020f63`：v0.8.0 clean兼容验收与隔离范围记录。
- `226f2aa`：v0.8.0生产后端路由和Qwen RAG验收记录。
- 本次 `fix(config): pin production MCP backend to v080 app`：production override补WEKNORA_BASE_URL=`http://WeKnora-app:8080/api/v1`，README同步，包description更新当前v0.8.0。原镜像标签与官方v0.7.2底座说明保留为历史事实。

## Standards

已审旧远端到226f2aa的14文件差异及请求接线：动态Key不回退静态Key、ContextVar身份回收、403/503语义和上传目录限制未发现新增阻断问题。原单日“无源码修改”验收记录不覆盖整个待推送范围，故新增本汇总记录。

## Spec

已修复P2：生产实际已用明确容器名防止共享网络app别名串到clean，但原可复用override缺失该值且README仍推荐通用app。现已固化，复审关闭。未改认证接口或请求级权限逻辑。

## 验证

在独立源码目录使用既有兼容镜像，断网、1 CPU/1GiB运行：

`python -m unittest discover -s tests -p 'test*auth*.py'`

15项通过。覆盖凭据隔离、拒绝非法Key、缓存、上下文释放、能力策略及SDK接线。Windows默认解释器缺依赖，未计作通过；有效结果来自依赖齐备的隔离容器。`git diff --check`通过。

历史clean28项及生产9类×3轮是真实发布证据，不冒充本轮重新执行；本轮不创建业务文档、APIKey或调用生产RAG。回归摘要见[验证回执](v080-publish-validation.json)。

## 部署与回滚

本次仅代码/配置发布，没有部署新镜像；生产仍复用`local/weknora-mcp-dispatch:v0.7.2-mwe-1b16874`，后端为v0.8.0生产App。main是本分支祖先，可普通快进到同一提交；不force、不推旧分支或标签。

恢复源码可revert本次配置提交；实际部署路由回退须按目标环境核对，不能把生产后端指向clean。请求权限由WeKnora后端再校验；验证缓存TTL、管理员静态配置和真实跨租户覆盖限制仍按README及历史记录说明。GitHub默认分支和两个ref的最终状态由发布后读回确认。
