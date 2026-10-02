# 管理全球授权范围与收益分配基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；另含**版本化海外授权与收益台账**（`domain.py`/`review.py`/`service_ledger.py`/`ledger_api.py`/`acceptance_ledger.py`）；
- fixtures/：离线验收使用的研究协议与结构化实验记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.acceptance --workspace .
PYTHONPATH=src python3 -m discovery_lab.acceptance --workspace .
PYTHONPATH=src python3 -m licensing_ops.acceptance
~~~

三条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析及交易风险处置，不访问外部网络。

第四条命令验收版本化授权与收益台账的完整业务闭环：

~~~bash
PYTHONPATH=src python3 -m licensing_ops.acceptance_ledger
~~~

## 版本化海外授权与收益台账

候选药海外授权谈判中，条款（地区、适应症、开发阶段、排他、再许可范围、共同开发义务、首付款/里程碑/销售分成）以“协议 → 不可变修订版本”组织，满足以下不变量：

- **条款引用当时有效的权利来源**：每个版本带内容哈希（`content_sha256`）；收益事件按“事件发生日当时在效的版本”登记（含已被取代版本的历史期间），新版本只令旧版本转为 `superseded`，永不改写旧行。
- **签署前评审**（`review.py`，纯函数、结构化阻断项）：范围重叠、双排他冲突、权利来源覆盖缺口（无链授权）、再许可范围/指名方越权、共同开发义务缺口；祖先行（上游授权）与续签链前序协议按合法派生豁免。
- **续约不自动延长旧权利**：续约版本最早只能在旧协议到期日次日生效（`renewal_overlaps_prior_term`），旧权利不因谈判而延续。
- **收益先假设后确认**：假设集带哈希；预测只依赖“假设哈希 + as_of 当日各协议在效版本快照”，相同输入命中同一 `forecast_id`，可复算。真实里程碑/销售事件逐期登记，按基点拆分到收款方（尾差按比例补给最大方，合计严格等于面额）。
- **争议只冻结相关份额**：争议按收款方维度冻结未结算份额，其余份额照常确认与结算；已结算流水（`settlements`）一经生成即锁定，不可冻结、冲正或被新版本倒改。
- **可追溯**：从任一地区（`/regions/{code}/trace`）、候选药（`/candidates/{id}/trace`）或协议（`/agreements/{id}/chain`）追溯完整授权链、未满足义务和每笔分配依据（`/distributions/{id}/basis`）；所有变更写入前向链接的哈希审计链（`/audit/chain`）。

角色（`lic_users`）：`bd_manager`（起草/评审）、`dealmaker`（签署/终止）、`finance`（假设、事件、争议、确认、结算）、`auditor`（只读审计）。

主要接口：`POST /agreements`、`POST /agreements/{id}/versions`、`POST /versions/{id}/review`、`POST /versions/{id}/sign`、`POST /assumptions/{id}/forecast`、`POST /events`、`POST /events/{id}/disputes`、`POST /distributions/{id}/confirm|settle|reverse`、`GET /agreements/{id}/chain`、`GET /regions/{code}/trace`、`GET /candidates/{id}/trace`、`GET /distributions/{id}/basis`、`GET /ledger`、`GET /audit/chain`。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m licensing_ops.ledger_api --database licensing_ledger.sqlite3 --host 127.0.0.1 --port 8083
~~~

台账服务使用 `X-Actor-Id` 请求头标识操作角色（首个用户经 `POST /users` 引导创建）。服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
