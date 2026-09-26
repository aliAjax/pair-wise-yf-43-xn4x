# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`authorization`：器具×量程的授权条款；`result`：检测结果。

## 授权条款与放行

方法换版后不再有跨量程的通用范围，每个方法版本按"器具 × 量程"登记授权条款（`authorization`，由 authorizer 创建），字段包括：

- `method_id` / `instrument_id`：条款绑定的方法与器具；
- `clause_no` / `clause_version`：条款编号与条款版本（同一方法下活动条款编号唯一）；
- `range_name`、`lower_limit`、`upper_limit`：量程名与上下限；
- `max_uncertainty`：扩展不确定度上限；
- `valid_until`：失效日期（当天仍有效，含当日）。

条款可执行 `revoke`（需填 `reason`）撤回，撤回后不再放行。

分析员对 `result` 执行 `release` 时必须提交 `instrument_id`、`method_id`、`value`（测得值）、`expanded_uncertainty`（扩展不确定度）、`used_at`（使用日期，YYYY-MM-DD）。规则按顺序校验：仪器 active → 最新校准合格且在有效期内 → 方法仍为 validated → 存在覆盖该器具的有效条款 → 测得值落在上下限内 → 不确定度不超上限 → 使用日期不晚于失效日期。多条款重叠时取跨度最窄者。命中后结果写入 `authorization_id`、`clause_no`、`clause_version` 与提交时的 `method_version` 快照。

任一条件不满足时返回 HTTP 422，响应体带机器可读的 `reason`（`calibration_failed` / `calibration_expired` / `method_revoked` / `clause_missing` / `value_out_of_range` / `uncertainty_exceeded` / `clause_expired` 等），结果保持 `pending` 不变并写入一条 `release_rejected` 审计；补充条件（如新增覆盖量程的条款）后可用同一条结果再次提交。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
