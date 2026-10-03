# 跨区域道路测试许可协同后端

智能网联汽车大会期间，多支外地车队联合申请道路测试。本服务让申请方提交**带版本**的
车辆资质、驾驶员授权、测试能力与路线时窗，管理人员按**辖区规则**完成审查、补件、
会签、签发、暂停、恢复与撤销，跨区域互认时**保留各方实际认可的范围**，临时封路
产生**可追踪的冲突与改期/取消结果**。

核心保证：所有状态变更都是只追加事件，**任一历史时点都能重放还原当时有效的许可、
责任人和审批依据**（`GET /snapshot?at=...`）。

## 设计

```
HTTP/JSON (httpapi.py, 仅标准库)
        │
PermitService (service.py)   命令校验、会签、冲突检测、幂等
        │
规则引擎 rules.py             各辖区资质/能力/路段规则（纯函数）
        │
事件 events.py + 投影 domain.py   16 种不可变事件 → 当前状态 / 任意时点快照
        │
EventStore (store.py)        内存 或 JSON Lines（重启重放）
```

- **版本与幂等**：同一申请按内容哈希比较；内容不变的重复提交只留痕（`duplicate_of`），
  不新增有效版本、不能再签发出第二份许可；支持 `idempotency_key` 防网络重试。
- **修订失效链**：车辆或路线变更产生新版本；新版签发时旧许可收到 `PermitSuperseded`
  事件，明确记录 `replaced_by_permit_id`、`changed_vehicles`、`changed_segments`。
- **审查补件**：开审即按辖区规则自动检查（资质/保险有效期、安全员授权、培训、
  能力等级与功能项、开放路段目录等）；阻断项未补件不能批准。
- **会签签发**：路线涉及多个辖区时，必须每个辖区都有“已批准”审查才能签发；
  许可保存每个辖区的审查人、决定时间、附加条件作为 `review_basis`。
- **占用互斥**：签发时校验与其他**已签发**许可的路段时窗重叠及在效封路；
  暂停的许可不占用路段；恢复前重新检测（暂停期间路段可能已被批给他人）。
- **跨区互认**：互认范围（车辆、驾驶员、路段、时窗）不得超出原许可；同一辖区
  重新登记的更窄/更宽范围会把旧互认标记为 `superseded`；互认可单独撤销并留痕。
- **封路冲突**：登记封路自动对所有在效许可检出冲突单；改期只能调时间不能换路段
  （换路段须重新申请），改期目标仍冲突会被拒；取消则移除该时窗。结果回写许可
  的“有效时窗”，快照按改期后的安排还原。

## 运行

仅需 Python 3.10+（使用标准库，无第三方依赖）。

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 端到端冒烟（提交→补件/审查→会签签发→互认→封路改期→时点还原）
python3 run_cli.py

# 启动 HTTP 服务（默认内存存储）
PYTHONPATH=src python3 -m permit_coordination.httpapi --port 8080
# 带事件持久化（JSON Lines，重启自动重放）
PYTHONPATH=src python3 -m permit_coordination.httpapi --port 8080 --event-log data/events.jsonl
```

## HTTP/JSON 接口

所有写操作必须带头 `X-Actor: <责任人标识>`；时间字段统一为带时区 ISO-8601。
错误返回 `{"error": code, "message": ..., "details"?: ...}`，404/409/400 语义化状态码。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/applications/submissions` | 提交/修订申请版本（重复内容自动识别，支持 `idempotency_key`） |
| POST | `/reviews` | 辖区开审 `{application_id, version, jurisdiction}`，自动规则检查 |
| POST | `/reviews/{rid}/supplement-request` | 人工追加补件要求 |
| POST | `/reviews/{rid}/supplements` | 申请方补件并声明解决的检查项 |
| POST | `/reviews/{rid}/approve` / `reject` | 辖区批准（可带 conditions）/驳回 |
| POST | `/permits/issue` | 会签齐全且无占用冲突后签发 |
| POST | `/permits/{id}/suspend` / `reinstate` / `revoke` | 暂停 / 恢复 / 撤销 |
| POST | `/recognitions` | 跨辖区登记互认（scope 可缩窄车辆/驾驶员/路段/时窗） |
| POST | `/recognitions/{gid}/revoke` | 撤销互认 |
| POST | `/road-closures` | 登记临时封路，自动产生冲突单 |
| POST | `/conflicts/{cid}/resolve` | `rescheduled`（带 new_window）或 `cancelled` |
| GET | `/permits` | 当前（或 `?at=`）有效许可 |
| GET | `/permits/{id}` | 许可详情：状态变更链、审批依据、冲突、互认 |
| GET | `/reviews/{rid}` | 审查单：检查项、补件材料、决定 |
| GET | `/applications/{aid}/history` | 全部提交版本（含重复/取代标记）与许可链 |
| GET | `/conflicts?state=open` | 冲突单列表 |
| GET | `/snapshot?at=2026-10-21T08:30:00%2B00:00` | **时点还原**：当时有效许可、责任人、依据、冲突、互认 |
| GET | `/journal` | 全量事件日志（审计） |

### 申请载荷示例

```json
{
  "application_id": "AP-7",
  "fleet_id": "FLEET-A",
  "jurisdiction": "JINGHAI",
  "idempotency_key": "fleet-a-20261021-01",
  "vehicles": {
    "V-1": {
      "plate": "沪D10001",
      "qualifications": [{"type": "road_test_qualification",
        "valid_from": "2026-10-11T00:00:00+00:00",
        "valid_to": "2026-11-20T00:00:00+00:00"}],
      "insurance": {"policy_no": "INS-1",
        "valid_from": "2026-10-11T00:00:00+00:00",
        "valid_to": "2026-11-20T00:00:00+00:00"}
    }
  },
  "drivers": {
    "D-1": {"name": "张三", "authorizations": [
      {"type": "safety_driver", "valid_from": "...", "valid_to": "..."},
      {"type": "automated_driving_training"}]}
  },
  "capability": {"level": "L4",
    "functions": ["emergency_stop", "remote_monitoring", "data_recording"],
    "certifications": ["technical_guidelines_compliance"]},
  "route_windows": [
    {"segment_code": "R-JH-01",
     "starts_at": "2026-10-21T08:00:00+00:00",
     "ends_at": "2026-10-21T10:00:00+00:00"}]
}
```

内置辖区：`JINGHAI`（路段前缀 `R-JH-`）、`JIADING`（`R-JD-`）、`LIN_GANG`（`R-LG-`），
各自规则见 `src/permit_coordination/rules.py`，可直接扩展。

## 测试覆盖

- `tests/test_contracts.py`：基础契约。
- `tests/test_service.py`：版本与幂等、自动审查与补件、跨辖区会签、占用互斥、
  修订作废旧许可、暂停/恢复/撤销、互认范围与取代、封路冲突/改期/取消、任意时点快照。
- `tests/test_httpapi.py`：真实 HTTP 端口上的完整协作流程（含 409 冲突、400 校验）。
- `tests/test_persistence.py`：JSONL 落盘、重启重放、幂等表恢复、跨进程快照一致。
