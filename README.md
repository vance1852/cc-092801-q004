# 管理全球授权范围与收益分配基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；
- src/deal_governance/：版本化海外授权（地区×适应症×开发阶段、排他与再许可、共同开发义务）、签署前范围/排他/义务检查、可复算收益预测、逐期确认的授权链瀑布分配、争议份额冻结与续约管控；
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
PYTHONPATH=src python3 -m deal_governance.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析、交易风险处置，以及授权链版本修订、签署前冲突识别、收益瀑布回流、争议冻结和到期续约管控，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m deal_governance.api --database deal_governance.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

## 授权与收益治理（deal_governance）

围绕一份候选药合同（agreement）管理其条款版本（term revision）与收益回流，核心约定：

- **版本化与权利来源**：条款修订以 `agreementId-rN` 形成不可变快照（draft → effective → superseded/terminated），快照与哈希落库；每次修订通过 `source_revision_id` 引用签署当时有效的上游版本，再许可的地区/适应症/阶段、期限和排他等级不得超出上游授权与再许可权限。
- **签署前检查**（`POST /revisions/{id}/pre-sign`，签署时强制复核）：三维范围相交产生 `scope_overlap`；exclusive/sole 与任何外部重叠构成 `exclusivity_conflict` 阻断项；覆盖临床/上市阶段却缺少共同开发或商业义务产生 `obligation_gap`；非排他共存仅告警。
- **可复算预测**：`POST /projections` 只依赖条款快照哈希与显式假设（里程碑概率、年度净销售额），纯函数展开首付款/里程碑/分成明细，相同输入得到相同 `lines_hash`，可先于真实事件提交。
- **逐期确认与授权链瀑布**：`POST /payment-events`（幂等键）对有效版本确认 upfront/milestone/royalty/sublicense；首付款/里程碑按上游 `sublicense_income_share` 逐级回流并保留本级留存记账行，分成按授权链各版本快照比例分别计提，每行记录公式与来源版本。
- **争议只冻结相关份额**：`POST /disputes` 仅把指定的 pending 份额置为 held，其余照常结算；已结算份额不可冻结。裁决 `release` 解冻或 `uphold` 冲销，差额以 adjustment 行留痕，历史金额与已结算行永不修改，条款新版本不能倒改历史。
- **续约不自动延长**：版本到期后再登记事件会被拒绝，必须显式签署新合同/新版本。
- **追溯**：`GET /revisions/{id}/chain`（授权链及 as-of 有效性）、`GET /trace/rights?candidate_id=&territory=`、`GET /trace/obligations`（未满足义务与逾期）、`GET /trace/allocations`（每笔分配依据）、`GET /audit/chain`（哈希链审计）。

角色（X-Actor-Id 头）：bd（谈判与起草/签署）、manager（管理与义务豁免）、finance（预测、收款确认、结算、争议）、auditor（只读追溯与审计）。
