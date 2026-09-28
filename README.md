# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围按版本调整、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计，以及通知和监管上报版本。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8213`。身份通过 `X-Actor` 与 `X-Role` 请求头模拟，角色为 `manufacturer`、`regulator`、`dealer`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/dealers`、`POST /api/vehicles`：登记网点和车辆。
- `POST /api/vehicles/{vin}/transfer`：更新车辆所在国家和车主。
- `POST /api/recalls`、`POST /api/recalls/{id}/submit`：创建并提交召回。
- `POST /api/recalls/{id}/review`：监管发布或退回。
- `POST /api/recalls/{id}/scope`：调整召回范围并生成新版本通知/上报。
- `POST /api/recalls/{id}/parts`：维修网点入库。
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修。
- `POST /api/appeals`：网点按原维修单提交打回申诉（附 `new_evidence_hash` 与 `explanation`，同一维修单仅一份待审）。
- `POST /api/appeals/{id}/decision`：监管判定 `uphold`（维持打回，原单继续作废）或 `reverse`（改判通过，原维修恢复完成）。
- `GET /api/appeals`：按 `pending`/`upheld`/`reversed` 分组的申诉档案，含原维修、每次处理流水与当前库存状态；`GET /api/appeals/{id}` 查单份。页面入口：`/appeals`。
- 改判时若回退零件已被其他车辆领走：登记 `part_gaps` 库存缺口并暂停该网点新维修；`parts` 补货优先填平缺口，清零后网点自动恢复。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/state`、`GET /api/health`：状态与健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为本地原型：跨境规则用许可字符串模拟，零件库存与维修记录是简化模型，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。

打回申诉按三层分处维护：`appeals.py` 只做判定（提交约束、维持/改判、缺口与停复牌），`records.py` 只负责申诉单、处理流水与库存缺口档案，`static/appeals.html` + `/appeals` 为独立页面入口；`errors.py` 提供跨模块共享的 `ApiError`。
