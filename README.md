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

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`authorization`：器具与量程的授权条款；`result`：检测结果。

方法换版后同一台仪器在不同量程的合格范围不同，因此方法验证不再使用通用范围，而是按
**器具 + 方法版本 + 量程**拆成授权条款登记。创建 `authorization` 时需提供：

- `clause_no`：条款编号（全局唯一）；`instrument_id` / `method_id`：授权对象
- `range_name`：量程名称；`lower_limit` / `upper_limit`：合格上下限
- `uncertainty_limit`：扩展不确定度上限；`expires_at`：失效日期（可选 `effective_at` 生效日期）
- `method_version`：登记时自动固化的方法版本快照

条款可由 authorizer 执行 `withdraw` 撤回。仅 `active` 且在使用日期处于有效期内的条款可用于放行。

## 结果放行

分析员（analyst）对 `pending` 的结果执行 `release`，数据必须包含
`instrument_id`、`method_id`、`value`（测得值）、`expanded_uncertainty`（扩展不确定度）、
`used_at`（使用日期，ISO 日期）。命中条件全部满足才放行：

1. 仪器处于 active，最近一次校准未失败，且校准证书在使用日期内有效；
2. 方法处于 validated（撤回的方法直接驳回）；
3. 存在该器具 + 方法、在使用日期有效的授权条款，量程上下限覆盖测得值，
   且扩展不确定度不超过该条款的不确定度上限（多量程重叠时取最窄量程）。

仪器校准不合格、方法撤回、条款失效或条款越界均返回中文驳回原因（HTTP 400）；
**原结果保持 `pending`，驳回原因和当次提交内容写入审计**（`release_rejected`），
补充条件（重新校准、登记新条款、更正数据）后可用同一条结果再次提交。
放行成功时结果数据保存 `clause_id`、`clause_no`、`method_version`。

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
