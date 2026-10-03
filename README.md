# 跨区域智能网联汽车道路测试许可协同后端

面向智能网联汽车大会期间多车队联合路测场景的许可协同系统。申请方提交**带版本**的车辆资质、
驾驶员授权、测试能力与路线时窗；管理方按辖区规则完成审查、补件、会签、签发、暂停与撤销；
跨辖区互认时保留各方**实际认可过的范围**。所有决策只追加不可变事件，**任一检查时点都能还原
当时有效的许可、责任人与审批依据**。

纯 Python 标准库实现，无第三方依赖，可直接 `python -m` 启动 HTTP/JSON 服务。

## 核心设计

```
POST/PUT 决策                      JSONL 事件日志（唯一事实来源）              时点查询
─────────────                      ──────────────────────────────              ────────
申请方/审查员  ──▶ PermitService ──▶ EventStore (append only)  ──▶ rebuild() ──▶ 任意 as_of
                  规则引擎/冲突检测   全局连续 seq + 乐观锁 + 防篡改               状态/授权/依据
```

- **事件溯源**：系统状态全部由事件流折叠而成；事件含全局序号、流内版本、发生时刻、操作责任人。
- **授权（grant）是最小效力单元**：每辆车/驾驶员/能力/时窗各有独立 grant，携带签发事件、规则版本、
  会签事件等完整依据；变更时显式失效并记录原因（`item_changed` / `removed_in_new_version` /
  `road_closure_reschedule` / `road_closure_cancelled` / `permit_revoked`）与替代关系。
- **内容寻址的版本去重**：每次提交计算内容摘要，同内容重复提交不产生新版本；`idempotency_key`
  支持客户端重试去重。车辆资质内容（资质版本、有效期、能力等）变化会得到新的条目身份，
  从而强制旧批准失效、新条目重新走审批。
- **许可号唯一**：重复提交/换发始终沿用同一许可号，绝不产生多份有效许可。
- **占用模型**：同一路段的时窗按半开区间判重，签发时硬拦截；已登记的临时封路同样拦截签发。
- **临时封路工单**：登记封路即对所有有效许可扫描冲突，生成可追踪冲突单；管理方可自动/手工
  提出改期 → 申请方响应 → 会签 → 关闭并对许可做带依据的修正（amendment）；拒绝改期则取消该时窗。
- **跨区域互认**：认可范围必须是源许可实际有效范围的子集；认可记录留档各方认可时的快照依据；
  源许可条目失效后，认可记录保留历史范围，同时给出"当前仍有效"的交集视图；范围只能收窄，
  扩大需重新互认；源许可暂停期间认可冻结。
- **辖区规则版本化**：每个辖区一份 `JurisdictionRules`（规则版本号写入签发依据），校验
  资质有效期、人车能力匹配、证书、时窗长度、开放路段清单等。

## 快速开始

需要 Python 3.11+，无第三方依赖。

```bash
# 启动 HTTP 服务（事件日志落盘，重启自动回放）
PYTHONPATH=src python3 -m permit_coordination --port 8080 --event-log data/events.jsonl

# 或安装后使用入口脚本
pip install -e . && permit-coordination --port 8080

# 命令行冒烟
python run_cli.py

# 全部自动化测试
python -m unittest discover -s tests -v

# 编译检查
python -m compileall -q src tests run_cli.py
```

## 操作人身份

所有写操作必须带头信息，随每条事件持久化：

| 头 | 说明 |
| --- | --- |
| `X-User-Id` | 操作人编号（必填） |
| `X-Role` | 角色：`fleet_contact` / `reviewer` / `safety_officer` / `traffic_police` |
| `X-Jurisdiction` | 所属辖区（如 `SH_DEMO`、`SZ_DEMO`） |
| `X-User-Name` | 姓名（百分号编码，支持中文） |

## HTTP 接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /api/applications` | 建立申请（application_id、fleet_id、jurisdiction、responsible） |
| `POST /api/applications/{id}/versions` | 提交版本（车辆/驾驶员/能力/时窗，可带 idempotency_key） |
| `POST /api/applications/{id}/info-requests` | 要求补件 |
| `POST /api/applications/{id}/rule-checks` | 执行辖区规则校验，返回 blocker/warning |
| `POST /api/applications/{id}/countersignatures` | 按角色会签（校验通过后） |
| `POST /api/applications/{id}/issue` | 签发/换发（含占用冲突拦截、显式失效与沿用） |
| `POST /api/applications/{id}/suspend` `/resume` `/revoke` | 暂停 / 恢复 / 撤销 |
| `GET  /api/applications/{id}` | 申请全量视图（可加 `?as_of=` 取历史时点） |
| `GET  /api/applications/{id}/effective?at=` | **检查时点视图**：当时有效的许可、责任人、授权与依据 |
| `GET  /api/applications/{id}/timeline` | 完整审批事件流 |
| `POST /api/closures` | 登记临时封路，自动生成冲突单 |
| `GET  /api/conflicts` `/api/conflicts/{cid}` | 冲突单列表/详情 |
| `POST /api/conflicts/{cid}/proposal` | 提出改期（空体则系统自动改到封路结束后） |
| `POST /api/conflicts/{cid}/response` | 申请方接受/拒绝 |
| `POST /api/conflicts/{cid}/countersign` | 改期会签（重新过规则与占用检查） |
| `POST /api/conflicts/{cid}/close` | 关闭冲突：rescheduled 修正许可 / cancelled 取消时窗 |
| `POST /api/recognitions` | 跨辖区互认（scope 为子集，省略表示全部） |
| `POST /api/recognitions/{rid}/amend` `/withdraw` | 收窄认可范围 / 撤回认可 |
| `GET  /api/recognitions[/{rid}]` | 认可记录及"当前仍有效范围"交集（支持 `?as_of=`） |
| `GET  /api/audit/events` | 全局不可变事件审计流（支持 `?as_of=`） |

### 端到端示例

```bash
H='-H Content-Type:application/json -H X-User-Id:u1 -H X-Role:fleet_contact -H X-Jurisdiction:SH_DEMO'
curl -X POST localhost:8080/api/applications $H \
  -d '{"application_id":"AP-1","fleet_id":"F1","jurisdiction":"SH_DEMO",
       "responsible":{"person_id":"p1","name":"Wang"}}'
curl -X POST localhost:8080/api/applications/AP-1/versions $H -d @version.json
# 审查：rule-checks → 两个角色 countersignatures → issue
curl "localhost:8080/api/applications/AP-1/effective?at=2026-10-21T03:00:00%2B00:00"
```

## 需求对照

| 场景诉求 | 落地方式 | 测试 |
| --- | --- | --- |
| 重复提交不能生成多份有效许可 | 内容摘要去重 + 幂等键 + 许可号恒定 | `test_submission_versioning.py` |
| 车辆/路线变更须明确既有批准失效 | grant 级 diff：changed/removed/carried/added，失效原因与替代链 | `test_issuance_changes.py` |
| 同段重复占用拦截 | 半开区间占用检测，签发返回 409 冲突明细 | `test_occupancy_closure.py` |
| 过期许可不被现场引用 | effective 视图区分许可登记时窗与当刻可执行时窗 | `test_lifecycle_forensics.py` |
| 临时封路产生可追踪冲突与改期 | 冲突单状态机 + 自动改期 + 会签 + 许可修正事件 | `test_occupancy_closure.py` / HTTP E2E |
| 跨区域互认保留各方实际认可范围 | 子集校验、快照依据、交集生效视图、只收窄、撤回/冻结 | `test_recognition.py` |
| 任一时点还原许可/责任人/依据 | 事件溯源 + `as_of` 回放，历史不被现状污染 | `test_lifecycle_forensics.py` / `test_event_store.py` |
| 规则审查、补件、会签、暂停、撤销 | 完整状态机与前置条件校验 | `test_review_workflow.py` |
| HTTP/JSON 证明可用 | 真实起服务器的端到端测试（含中文头编码） | `test_http_api.py` |

## 代码结构

```
src/permit_coordination/
  contracts.py      # 值对象：资质/授权/能力/时窗/版本快照、状态枚举、内容哈希
  rules.py          # 辖区规则集（版本化）与 blocker/warning 评估
  event_store.py    # 只追加事件存储（JSONL、乐观锁、序号防篡改、操作人）
  aggregate.py      # 事件回放：许可/冲突单/互认聚合与时点判定
  service.py        # 用例服务：提交、审批、签发、封路改期、互认、取证视图
  http_api.py       # stdlib http.server 路由与错误码映射
  clock.py          # 可注入时钟（测试用只进不退的假时钟）
  serialization.py  # JSON ↔ 值对象，时区强制校验
tests/              # 63 个自动化测试（领域 + 持久化 + HTTP E2E）
```
