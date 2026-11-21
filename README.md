# 主数据清洗与黄金记录平台

面向多源主数据的匹配识别、冲突消解、合并清洗与黄金记录管理的主数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 来源系统注册 | `source_system` | 来源系统 | 系统编号、系统名称、系统类型 |
| 实体模型定义 | `entity_model` | 实体模型 | 模型编号、模型名称、业务域 |
| 匹配规则引擎 | `match_rule` | 匹配规则 | 规则编号、实体模型、匹配字段 |
| 疑似重复簇 | `dup_cluster` | 重复簇 | 簇编号、实体模型、簇大小 |
| 合并追溯日志 | `merge_log` | 合并记录 | 记录编号、黄金ID、来源记录 |
| 黄金记录管理 | `golden_record` | 黄金记录 | 黄金ID、实体模型、关联来源 |
| 字段标准化清洗 | `field_cleanse` | 清洗规则 | 规则编号、实体模型、目标字段 |
| 优胜策略配置 | `survivorship_rule` | 优胜策略 | 策略编号、实体模型、选择字段 |
| 数据入库规则 | `data_import_rule` | 入库规则 | 规则编号、来源系统、实体模型 |
| 批次导入管理 | `import_batch` | 导入批次 | 批次编号、来源系统、实体模型 |
| 记录级血缘 | `record_lineage` | 血缘记录 | 血缘编号、黄金ID、来源记录 |
| 主数据分发同步 | `mdm_sync` | 同步配置 | 配置编号、黄金实体、目标系统 |
| 黄金记录版本 | `mdm_version` | 版本记录 | 版本编号、黄金ID、版本号 |
| 自动匹配任务 | `auto_match_job` | 匹配任务 | 任务编号、实体模型、匹配规则 |
| 主数据质量统计 | `mdm_quality` | 质量统计 | 统计编号、实体模型、完备率 |
| 参考数据管理 | `mdm_reference` | 参考数据 | 数据编号、数据类别、标准编码 |
| 主数据查询接口 | `mdm_api` | 查询接口 | 接口编号、实体模型、查询字段 |
| 主数据治理看板 | `mdm_dashboard` | 看板配置 | 看板编号、看板名称、监控实体 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 地址版本与回退

街道与区县是一套完整地址：确认、发布、回退都必须成组处理。相关实现：

- `backend/app/services/address_group.py`：地址角色解析、字段快照解析、按黄金ID的并发锁。
- `backend/app/services/mdm_version.py`：版本确认、发布 `publish_version`、回退 `rollback_to_version`。
  每个已确认版本固化自己的完整字段组合；回退会整体替换黄金记录字段并写入可查询的
  `回退字段数据`，同一回退重放只生效一次。
- `backend/app/services/golden_record.py`：`生效字段组合` 保存当前已发布版本的字段组合。
- `backend/app/services/mdm_api.py`：地址读取入口 `query_customer_address`，只消费已生效组合；
  街道与区县不齐时 `ready_for_dispatch=false`。
- `frontend/src/views/AddressDispatchView.vue`：客服地址改派页面（`/address-dispatch`）。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/address/{golden_id}` | 读取顾客地址与可改派状态 |
| GET | `/api/address/{golden_id}/combination` | 读取当前生效字段组合与回退字段数据 |
| GET | `/api/address/{golden_id}/versions` | 版本时间线（含每版字段组合） |
| GET | `/api/versions/{golden_id}/confirmed` | 存量确认结果（证据不齐也可查询） |
| GET | `/api/versions/{golden_id}/{version}/fields` | 指定版本的完整字段组合 |
| POST | `/api/versions/{golden_id}/publish` | 发布版本并流转当前版本 |
| POST | `/api/versions/{golden_id}/rollback` | 执行回退并恢复字段 |
| POST | `/api/versions/{golden_id}/corrections` | 提交地址更正（街道/区县成组补齐） |
| GET | `/api/versions/{golden_id}/dispatch-address` | 客服改派地址（只返回整套可用地址） |

回归测试：

```bash
cd backend
.venv/bin/python -m app.tests.test_address_rollback
.venv/bin/python -m app.tests.test_business_chains
```

领域：数据工程
子方向：数仓与数据湖
