# DMX 灯具地址编排台

本地运行的 DMX 灯具地址编排 Web 应用：导入灯具型录、建立灯具实例与最多四条 512 通道线路、设置保留区，
由 OR-Tools CP-SAT 求解器在不跨线路、通道连续、无重叠的前提下完成地址编排，支持预览、原子发布与 CSV 导出。

## 功能

- **灯具型录管理**：按 `manufacturer,name,mode,channels` 导入 CSV/JSON；同一型号支持多个模式（占用通道数不同），重复模式更新通道数。
- **灯具实例**：为灯具选择型号/模式，记录上一版线路与起始地址，可锁定既有线路与起始地址。
- **线路与保留区**：固定 4 条 × 512 通道线路，可重命名、停用；每条线路可设置保留通道区。
- **编排规则**：每台灯具获得一段连续通道，不跨线路、不与其他灯具或保留区重叠；锁定灯具必须留在原位。
- **优化目标（按优先级）**：
  1. 已配灯具的**迁移台数最少**；
  2. 在此前提下**启用线路数最少**；
  3. 再按灯具 ID 排序，逐台取 `(线路 ID, 起始地址)` **字典序最小**的确定方案。
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

## 技术栈

- 后端：Python 3.11、FastAPI、SQLAlchemy 2.0、SQLite（WAL）、Google OR-Tools CP-SAT
- 前端：原生 HTML/CSS/JavaScript（无构建步骤、无框架依赖）
- 部署：Docker / Docker Compose 单容器，数据卷持久化
