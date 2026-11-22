# 单盘写回缓存演练（Write-Back Cache Lab）

用 Flask 做的单盘写回缓存教学/演练页：盘体是本地镜像文件，按 **4 KiB 整块**寻址；
普通写入只进内存缓存即应答，读盘返回最新值，FLUSH/FUA 才真正写镜像。
可在网页上演示交错写入、FLUSH 暂停、FUA 直写、模拟掉电，以及 FLUSH 落盘途中的
**短写故障**（本轮结束、标出失败块、镜像保住该块原值、故障解除后可重试）。

## 语义约定（对应 SBC 块层写缓存行为）

| 操作 | 行为 |
| --- | --- |
| 普通写 `write` | 校验整块 4096 字节后写入内存缓存并立即应答；镜像不动，读返回缓存最新值 |
| 读 `read` | 缓存命中返回缓存值；未命中从本地镜像读取 |
| `FLUSH` | **每次只跑一轮**。受理时对待刷块拍快照：受理前的脏块落盘；受理后的普通写入不随本轮落盘。可在块与块之间暂停/恢复，一轮结束后回到 idle |
| 暂停期间 | 普通读写照常；可发 `FUA`：仅指定块立即落盘后才应答，其余块照旧 |
| 恢复 FLUSH | 落盘前比对版本号，快照比盘上旧时记 `skipped-stale` 跳过，**旧值绝不覆盖已落盘的新值** |
| 短写故障 `short write` | 本轮对某块只写入前 N 字节即报错。**本轮立即结束**（状态 `failed`，网页标出失败块）：已刷完的块保留落盘值；失败块与未刷块保留各自最新缓存，仍为脏块 |
| 短写后的镜像 | 失败块镜像里仍保住**原先的完整 4 KiB 值**：落盘前先把旧块写入 sidecar undo 日志，失败后只回写被覆盖的前 N 字节（后半截本就没动），因此即使该块后半截仍写不了也能恢复；回滚本身写不了时 undo 日志继续生效，读盘回退到它，掉电/重启时重放修复镜像。镜像布局不变（仍是 4 KiB 整块） |
| 故障解除后重试 | 清除短写故障后可重新选块发起 FLUSH；失败块的新值仍在缓存，重试落盘的就是它 |
| 模拟掉电 | 清空缓存、作废未完成 FLUSH（状态置 `aborted` 且不可恢复），之后读盘只反映镜像内容（若有未回滚的 undo 日志则先重放） |
| 复位 | 演示辅助：重建初始镜像、清空全部状态（含故障与 undo 日志） |

落盘均带 `flush() + fsync()`，镜像即"盘上会留下什么"的事实来源。
单块写回的额外 I/O 是常数（读一次旧块 + 写一次固定大小的 undo 记录 + 写该块），
与镜像容量无关；`GET /api/state` 的 `io` 字段与页面上的 I/O 计数可核对。

## 目录结构

```
wbcache-demo/
├── app/
│   ├── __init__.py
│   ├── disk.py              # 写回缓存/FLUSH/FUA/掉电 核心模型（线程安全）
│   ├── server.py            # Flask 路由与 REST API
│   └── templates/index.html # 单页演练界面（块网格、轮询、事件日志）
├── tests/
│   ├── test_disk.py         # 核心语义单元测试（含短写/undo/常数 I/O）
│   └── test_api.py          # Flask 端到端 HTTP 测试（含网页短写流程）
├── demo.sh                  # 可选：对运行中的实例跑一遍完整演示
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 用 Docker Compose 启动

```bash
cd wbcache-demo
docker compose up --build
# 浏览器打开 http://localhost:8000
```

镜像文件位于容器卷 `diskimg` 内的 `/data/disk.img`（32 块 × 4 KiB = 128 KiB，
首次启动自动初始化，每块写入 `INIT-BLOCK-NN` 标记）。
可用环境变量调整：`BLOCK_COUNT`（块数）、`FLUSH_DELAY`（FLUSH 每块间隔秒数，
便于人工暂停，默认 0.6）、`IMAGE_PATH`。

停止：`docker compose down`；连镜像卷一起清掉：`docker compose down -v`。

## 本地直接运行（无 Docker 时）

```bash
python -m pip install Flask==3.1.3
IMAGE_PATH=./disk.img BLOCK_COUNT=32 FLUSH_DELAY=0.6 python -m app.server
```

## 网页演示脚本（交错写入 + 掉电 + 短写）

1. 块 0 写 `A-v1`、块 1 写 `B-v1`、块 0 再写 `A-v2`；读块 0 → 来自缓存的 `A-v2`，
   块视图里两块为橙色脏块，镜像尚未变化。
2. 点击选中块 0、块 1，点 **发起 FLUSH**，立刻再给块 1 写 `B-v2`。
   轮次结束：块 0、块 1 的受理时快照落盘；块 1 仍显示脏块，读它得到缓存里的 `B-v2`。
3. 给块 2、3、4 写值后发起 FLUSH，等块 2 刷完点 **暂停**：
   - 暂停时给块 4 普通写新值（照常应答、仍在缓存）；
   - 给块 3 勾 **FUA** 写新值 → 应答时块 3 已落盘，块 4 仍是镜像初值；
   - 点 **恢复**：块 3 记 `skipped-stale`，盘上保留 FUA 新值；块 4 落的是旧快照，
     暂停期间的新值仍在缓存。
4. 给块 5 写一个只在缓存里的值，发起一轮 FLUSH，刷到一半点 **模拟掉电**：
   - FLUSH 状态变 `aborted`，块视图全部回到镜像态；
   - 已落盘的块数据还在，块 5 读回 `INIT-BLOCK-05`，受理后/未落盘的写入全部消失；
   - 掉电后不能 resume，但可以重新发起新的 FLUSH。
5. **短写演练**（本轮新建）：
   1. 给块 0–3 各写一个值，在「短写故障注入」里把故障块设为 `2`、字节数 `1000`，
      勾选「故障持续到手动解除」，点 **布置短写**；
   2. 选中块 0–3 发起 FLUSH。块 0、1 正常落盘，块 2 只写了前 1000 字节即失败：
      本轮状态 `failed`，页面红色 ✕ 标出块 2 并写明失败原因，进度停在 2/3；
   3. 分别读块 0–3：块 0/1 来自 platter，块 2/3 来自缓存（最新值仍在），
      失败块和未刷块都是脏块；
   4. 点 **模拟掉电**：块 2 读回 `INIT-BLOCK-02`（镜像里的原值完整保留，
      不是半新半旧），块 0/1 的落盘值仍在；
   5. 点 **解除故障**，重新给块 2 写值并选中它发起 FLUSH → 状态 `done`，
      块 2 从 platter 读回新值。中途若再布置短写并重试，仍会以同样方式失败。

「短写」只影响下一轮 FLUSH 的写回，不影响普通写/FUA；普通写入应答语义不变。

## REST API 摘要

| 方法/路径 | 说明 |
| --- | --- |
| `GET /` | 演练页面 |
| `GET /api/state` | 全量状态（每块位置/版本、FLUSH 进度、事件日志） |
| `POST /api/write` | `{block, data, pad?, fua?, encoding?}`；`data` 默认 UTF-8 文本，`pad=true` 零填充到 4096；也可 `encoding=hex` |
| `GET /api/read?block=N` | 返回来源（cache/platter/image/undo）、版本号、预览与 hex |
| `POST /api/flush` | `{selected:[块号...]}`，受理一轮；可选 `fault:{block, bytes?, persistent?}` 顺带布置短写 |
| `POST /api/flush/pause` `/api/flush/resume` | 暂停/恢复当前轮 |
| `POST /api/fault/short-write` | `{block, bytes?, persistent?}`：下一轮 FLUSH 写该块只写 `bytes`（默认 2048）后报错 |
| `POST /api/fault/clear` | 解除所有短写故障，之后可重试 |
| `POST /api/power-loss` | 模拟掉电（如有 undo 日志先重放修复镜像） |
| `POST /api/reset` | 重建镜像、复位状态 |

FLUSH 状态：`idle / running / paused / done / failed / aborted`。
`failed` 时 `flush.failed_block` 与 `flush.error` 说明失败块与原因，
`flush.results` 逐块给出 `persisted / skipped-clean / skipped-stale / failed-short-write / failed-write`。
`GET /api/state` 另外返回 `fault`（已布置短写）、`undo`（待重放的 undo 记录）、
`last_failure`、`io`（累计读写字节）以及每块的 `failed` / `undo_pending` 标记。

错误返回 `400`（参数/整块校验）或 `409`（FLUSH 忙、暂停/恢复状态不符）。

## 自测

```bash
python -m unittest discover -s tests -v
```

覆盖：整块校验、缓存读最新值、FLUSH 快照与受理后写入隔离、单轮互斥、
暂停期间读写/FUA 隔离、恢复时跳过陈旧快照、掉电作废轮次且镜像为唯一事实来源，
以及短写场景：本轮以 `failed` 结束并标出失败块、已刷块保留落盘值、
失败/未刷块保留最新缓存、失败块镜像原值完整（掉电与重启后均能读回）、
故障未解除时重试仍失败/解除后可重试成功、undo 日志兜底与重放、
单块写回额外 I/O 不随容量增长、HTTP 端到端短写流程。
