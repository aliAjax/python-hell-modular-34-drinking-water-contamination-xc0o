# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/dispatch.py`：备用水源登记、调度单校验、按序预留与改单、送水/失败/复检/撤销/补送计划。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、调度台账和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限和版本冲突。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。

## 备用水源调度

备用水源有限，多个污染事件可同时申请水源和送水片区：

- `POST /api/backup-sources`：登记备用水源（`source_code`、`name`、`capacity`），角色 `coordinator`/`regulator`。
- `GET /api/backup-sources`：各水源总水量、已预留/送达和剩余水量。
- `POST /api/items/<id>/dispatch`：提交调度单（`source_code`、`zones: [{zone_id, amount}]`，可选 `request_id` 幂等去重）。提交后按请求顺序预留水量，水量不足时后到的请求按剩余份额改单；已关闭（取消/恢复）的事件不能申请。
- `POST /api/dispatch/<id>/actions`：调度操作，`action` 取值为
  - `deliver`（`zone_id`）：片区送水送达；
  - `fail`（`zone_id`、`delivered_amount`）：送水失败，保留已送达部分，失败片区的预留退回并生成补送待办；
  - `recheck`（`result: clear|contaminated`）：复检发现污染时立即释放未执行的预留，已送达记录保留；
  - `revoke`（`reason`）：撤销调度单，同样释放未执行预留；
  - `redeliver`（`todo_id`）：按当前剩余水量完成补送待办。
- `GET /api/items/<id>/dispatch`：事件的调度单、预留、补送待办和退补台账。
- `GET /api/dispatch/summary`：首页看板数据（水源剩余水量、每个事件的片区覆盖、退补记录和通知）。

升级前只有单一 `alternate_source_id` 字段的旧事件仍可正常查看，页面以“旧版备用水源”标注。所有调度变更写入审计链和退补台账。
