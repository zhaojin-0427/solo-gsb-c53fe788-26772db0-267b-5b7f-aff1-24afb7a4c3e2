# DMX 灯具地址编排台

本地运行的 DMX 灯具地址编排 Web 应用：导入灯具型录、建立灯具实例与最多四条 512 通道线路、设置保留区，
由 OR-Tools CP-SAT 求解器在不跨线路、通道连续、无重叠的前提下完成地址编排，支持预览、原子发布与 CSV 导出。

## 功能

- **灯具型录管理**：按 `manufacturer,name,mode,channels` 导入 CSV/JSON；同一型号支持多个模式（占用通道数不同），重复模式更新通道数。
- **灯具实例**：为灯具选择型号/模式，记录上一版线路与起始地址，可锁定既有线路与起始地址。
- **线路与保留区**：固定 4 条 × 512 通道线路，可重命名、停用；每条线路可设置保留通道区。
- **编排规则**：每台灯具获得一段连续通道，不跨线路、不与其他灯具或保留区重叠；锁定灯具必须留在原位。
- **优化目标（按优先级，逐级**证明**最优）**：
  1. 已配灯具的**迁移台数最少**；
  2. 在此前提下**启用线路数最少**；
  3. 再按灯具 ID 排序，逐台取 `(线路 ID, 起始地址)` **字典序最小**的确定方案。

  三个阶段都由 CP-SAT 返回 `OPTIMAL`（而非限时内仅找到可行解的 `FEASIBLE`）才算证明完成；
  前一级未证最优时**不会**固定其目标值继续求下一级，第三级按灯具 ID 逐台证明——
  某台灯未证最优就停在该台灯，绝不固定其值继续向后求。
- **最优性状态（`optimality_status`）**：
  - `proven_optimal`：三级目标全部证明最优，只有这种预览**可发布**；
  - `unproven`：某级在限时内只得到可行解或连可行解都未找到（**不等于无解**）。
    接口照常返回 200，并在有现任解时附带仅供查看的排布，但**不会生成可发布草稿**；
  - `infeasible`：已证明无解（容量、锁定冲突等，`conflicts` 给出具体原因）。
  状态随草稿持久化：查询预览、发布响应、已发布快照中都带该字段；升级前遗留的、
  无证明状态的草稿在发布时一律拒绝（HTTP 409）。
- **预览**：线路占用可视化、迁移清单；无解时给出具体冲突约束（容量不足、锁定越界、锁定块互相重叠/与保留区冲突、碎块不可排布等）。
- **原子发布**：预览绑定配置版本；配置一旦变化，旧预览立即失效并拒绝发布（返回 409），需重新生成预览。发布在单个数据库事务内完成，并写入不可变快照。
- **CSV 导出**：已发布地址表（UTF-8 带 BOM，Excel 可直接打开）。

## 快速开始（Docker Compose，推荐）

需要本机已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build -d
```

启动后用浏览器访问：

> **http://localhost:8000**

数据保存在 Docker 卷 `dmx-data`（容器内 `/data/dmx.db`）中，容器重建不丢数据。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

## 本地直接运行（不使用 Docker）

需要 Python 3.11+。

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

然后访问 http://localhost:8000 。SQLite 数据库默认位于 `./data/dmx.db`，可用环境变量
`DMX_DB_PATH` 修改位置。

## 使用流程

1. 打开 **灯具型录** 页签，点击“载入示例 CSV”再“导入文本内容”（或上传自己的 `.csv`/`.json`）。
2. 打开 **灯具实例**，添加灯具；对已在现场配过地址的灯具填写“既有线路/起始地址”，需要保持不动的勾选“锁定”。
3. 打开 **线路与保留区**，按需停用线路、添加保留通道区。
4. 回到 **编排**，点击“生成预览”：
   - 查看四条线路的占用条与迁移清单；
   - 若出现红色冲突面板，按提示调整约束后重新生成；
   - 预览期间如果改了任何配置，顶部出现黄色横幅且发布按钮禁用，需重新生成预览。
5. 点击“按此预览原子发布”。发布后在 **已发布** 页签查看地址表并下载 CSV。
6. 发布成功后，方案中的地址自动成为各灯具的“既有地址”，下一轮编排会优先保持它们不动。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/catalog` | 列出/新建型号 |
| POST | `/api/catalog/import` | 上传 CSV/JSON 批量导入型录 |
| DELETE | `/api/catalog/{id}` | 删除型号（仍被引用时拒绝） |
| GET/POST | `/api/fixtures` | 灯具列表/新建 |
| PUT/DELETE | `/api/fixtures/{id}` | 编辑/删除灯具 |
| GET | `/api/lines` / PATCH `/api/lines/{id}` | 线路列表/重命名/启停 |
| GET/POST/DELETE | `/api/reservations[/{id}]` | 保留区管理 |
| GET | `/api/plans/version` | 当前配置版本与最近发布信息 |
| POST | `/api/plans/preview` | 求解并生成预览 |
| POST | `/api/plans/{id}/publish` | 按预览原子发布（版本不符返回 409） |
| GET | `/api/plans/published/latest` | 最近一次发布的完整地址表 |
| GET | `/api/plans/published/{id}/csv` | 下载已发布地址表 CSV |

交互式文档：启动后访问 http://localhost:8000/docs 。

## 预览与发布 HTTP API 示例

以下示例假设服务运行在 `http://localhost:8000`。

### 1. 查看配置版本

```bash
curl -s http://localhost:8000/api/plans/version
```

```json
{
  "config_version": 12,
  "latest_published_plan_id": 3,
  "latest_published_at": "2026-09-30T08:14:22.401Z"
}
```

### 2. 生成预览（`POST /api/plans/preview`，无请求体）

**已证最优（`proven_optimal`）**——返回 200，落库为可发布草稿，携带 `plan_id`：

```bash
curl -s -X POST http://localhost:8000/api/plans/preview
```

```json
{
  "feasible": true,
  "optimality_status": "proven_optimal",
  "id": 17,
  "plan_id": 17,
  "config_version": 12,
  "status": "draft",
  "migration_count": 1,
  "used_lines": 2,
  "created_at": "2026-10-01T03:11:09.220Z",
  "allocations": [
    {"fixture_id": 1, "name": "PAR-左1", "line_id": 1, "start": 1, "end": 9,
     "channels": 9, "migrated": false, "locked": false}
  ],
  "migrations": [
    {"fixture_id": 7, "name": "Beam-7", "from_line": 2, "from_start": 100,
     "to_line": 1, "to_start": 501, "locked": false}
  ],
  "occupancy": [
    {"line_id": 1, "name": "线路 1", "is_active": true, "size": 512,
     "used_channels": 20, "free_channels": 492, "fixtures": [], "reservations": []}
  ],
  "conflicts": []
}
```

**未证最优（`unproven`）**——灯具很多、求解器限时内只得到 `FEASIBLE` 时同样返回 200，
但与“无解”区分：没有 `plan_id`/`id`，**不会生成草稿、不能发布**。若找到现任解，
`allocations`/`occupancy` 仅供参考；连现任解都没有时它们为空数组：

```json
{
  "feasible": false,
  "optimality_status": "unproven",
  "config_version": 12,
  "conflicts": [],
  "unproven_phase": "migration",
  "unproven_fixture_id": null,
  "message": "第一阶段（已配灯具迁移台数最少）在限定时间内未能证明最优。限时内既未证明最优、也未找到可行解；这不是“无解”结论。请稍后重试，或减少灯具数量/放宽部分约束。",
  "migration_count": null,
  "used_lines": null,
  "allocations": [],
  "migrations": [],
  "occupancy": []
}
```

字典序阶段超时的响应形如：`"unproven_phase": "lexicographic"`、
`"unproven_fixture_id": 52`（停在灯具 ID 52，其后的灯具均未求解）。

**真正无解（`infeasible`）**——200 响应，`conflicts` 给出已证明的具体冲突：

```json
{
  "feasible": false,
  "optimality_status": "infeasible",
  "config_version": 12,
  "conflicts": [
    {"type": "capacity",
     "message": "通道总需求 620 超过启用线路的可用容量 512（1 条 × 512，扣除保留区）。",
     "total_demand": 620, "total_capacity": 512}
  ],
  "occupancy": []
}
```

### 3. 查询预览（核对最优性状态）

```bash
curl -s http://localhost:8000/api/plans/17
```

响应与已证最优的预览相同，其中 `optimality_status` 持久可核对；
升级前遗留的无证明草稿该字段为 `unproven`。

### 4. 发布（`POST /api/plans/{plan_id}/publish`，无请求体）

只有 `status=draft`、`optimality_status=proven_optimal` 且 `config_version` 与当前一致
时才发布成功（200）：

```bash
curl -s -X POST http://localhost:8000/api/plans/17/publish
```

```json
{
  "ok": true,
  "plan_id": 17,
  "config_version": 13,
  "optimality_status": "proven_optimal",
  "published_at": "2026-10-01T03:12:40.015Z"
}
```

拒绝场景均为 HTTP 409，例如：

- 配置版本过期：`配置已变化（预览版本 12，当前版本 13），请基于最新配置重新生成预览后再发布。`
- 未证最优/旧版无证明草稿：`该预览未持有三级目标（迁移台数→启用线路数→字典序）的最优性证明，不可发布；请重新生成预览并在返回 proven_optimal 后再发布。`
- 重复发布：`该预览已为 published 状态，不能重复发布`。

### 5. 已发布地址表与 CSV

```bash
curl -s http://localhost:8000/api/plans/published/latest
curl -s -OJ http://localhost:8000/api/plans/published/17/csv
```

## 求解时限（可选）

每阶段求解时间可用环境变量覆盖（默认值适合规模在数百台灯以内的场景）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DMX_SOLVE_SECONDS_PHASE` | `5.0` | 迁移最少 / 启用线路最少两级各自的时限（秒） |
| `DMX_SOLVE_SECONDS_TIE` | `1.5` | 字典序阶段每台灯的证明时限（秒） |
| `DMX_SOLVE_SECONDS_TOTAL` | `60.0` | 一次预览求解的总时限兜底（秒） |

任一级到时仍未证明最优，预览即返回 `unproven`，可稍后重试；数据规模较小时三级通常在
亚秒级全部证完。Docker 方式可在 `docker-compose.yml` 的 `environment:` 中覆盖这些变量。

## 技术栈

- 后端：Python 3.11、FastAPI、SQLAlchemy 2.0、SQLite（WAL）、Google OR-Tools CP-SAT
- 前端：原生 HTML/CSS/JavaScript（无构建步骤、无框架依赖）
- 部署：Docker / Docker Compose 单容器，数据卷持久化
