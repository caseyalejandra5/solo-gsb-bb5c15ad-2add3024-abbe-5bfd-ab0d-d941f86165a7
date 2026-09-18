# 批次资金流水核销 API

基于 Python + FastAPI 的批次资金流水核销服务。接收两侧（left / right）各最多 12 笔流水，
在满足核销规则的全部合法候选组中，选出**核销金额绝对值总和最大**的互斥核销组集合。

## 快速启动

```bash
docker compose up --build
```

一条命令即可启动，服务地址：**http://localhost:8000**

- 接口文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

也可以不用 Docker 本地运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口说明

### `POST /reconcile`

**请求体**

```json
{
  "left":  [{ "id": "L1", "time": "2026-09-17T00:00:00Z", "amount": 300 }],
  "right": [
    { "id": "R1", "time": "2026-09-17T01:00:00Z", "amount": 100 },
    { "id": "R2", "time": "2026-09-17T02:00:00Z", "amount": 200 }
  ],
  "locked_groups": [
    { "left_ids": ["L1"], "right_ids": ["R1", "R2"] }
  ],
  "excluded_pairs": [
    { "left_id": "L1", "right_id": "R3" }
  ]
}
```

| 字段   | 说明                                                                 |
| ------ | -------------------------------------------------------------------- |
| `id`   | 非空字符串，全批次（两侧合并）唯一；重复 ID 返回 **400**              |
| `time` | ISO-8601 时间；带时区会归一化到 UTC，无时区按 UTC 处理                |
| `amount` | 非零带符号整数（单位：分）；`0`、布尔、浮点、字符串均返回 **422**   |

每侧 0～12 笔，超过 12 笔返回 **422**。

### 人工确认组（`locked_groups`，可选）

`locked_groups` 为可选字段，用于提交人工已确认的核销组；数组中每组通过
`left_ids` / `right_ids` 引用**本批次** `left` / `right` 中的流水。不传该字段或传
`[]` / `null` 时，响应与原行为完全一致。

锁定组必须满足：

- `left_ids` 只能引用本批次 left 的 ID，`right_ids` 只能引用本批次 right 的 ID；
- 符合上文全部核销组规则（五种组形之一、组内同号、两侧金额之和相等、时间跨度 ≤ 48 小时）；
- 同一组内不得有重复 ID；任意一笔流水不得被多个锁定组重复使用。

任一条件不满足均返回 **422**，错误 `loc` 为
`["body", "locked_groups", <序号>]`，`<序号>` 为出问题组从 0 开始的下标（按数组顺序报告首个错误）。例如：

```json
{
  "detail": [
    {
      "type": "locked_group.invalid",
      "loc": ["body", "locked_groups", 1],
      "msg": "locked_groups[1]: transaction reused by another group: A",
      "input": { "left_ids": ["A"], "right_ids": ["Y"] }
    }
  ]
}
```

求解时**全部锁定组都会被保留**，其流水从候选池中移除；只在剩余流水上执行原有的
“核销金额绝对值总和最大 + 组编码列表字典序最小”求解。最终 `groups` 为锁定组与
自由求解结果的合并（仍按组编码升序），`total_amount` 与 `unmatched_*` 均按合并结果计算。

### 排除对（`excluded_pairs`，可选）

`excluded_pairs` 用于让操作员声明**绝不能归入同一个核销组**的左右流水对；每项形如
`{"left_id": "L1", "right_id": "R1"}`。不传该字段或传 `[]` / `null` 时，响应与原行为
完全一致。

校验规则（任一不满足均返回 **422**，错误 `loc` 为
`["body", "excluded_pairs", <序号>]`，`<序号>` 为出问题项从 0 开始的下标，按数组顺序
报告首个错误）：

- `left_id` 必须引用本批次 left 的 ID：不存在报 `unknown left transaction id`，
  实际是 right 侧 ID 时报侧别错误；
- `right_id` 必须引用本批次 right 的 ID：不存在报 `unknown right transaction id`，
  实际是 left 侧 ID 时报侧别错误；
- 同一个 `(left_id, right_id)` 对不得重复出现，否则报 `duplicate excluded pair`。

```json
{
  "detail": [
    {
      "type": "excluded_pair.invalid",
      "loc": ["body", "excluded_pairs", 0],
      "msg": "excluded_pairs[0]: unknown left transaction id: NOPE",
      "input": { "left_id": "NOPE", "right_id": "R1" }
    }
  ]
}
```

校验通过后，生成候选组时**只要组内同时包含某个排除对的两侧 ID 就直接剔除该候选组**
（一对一与一对多组形均适用），其余候选组继续按“最大核销额 + 字典序最小”规则求解；
响应结构不变，被排除规则影响而无法成组的流水照常进入 `unmatched_*`。

若某个人工 `locked_groups` 命中排除对（锁定组中同时包含该对两侧 ID），同样返回
**422**，错误 `loc` 为 `["body", "locked_groups", <锁定组序号>]`——排除规则不会被
静默忽略。排除对与未命中的锁定组可以并存：锁定组照旧强制保留，自由候选池在剔除
违规候选后继续优化。

与其它请求字段一样，`excluded_pairs` 参与 `Idempotency-Key` 的请求指纹：相同键搭配
不同的排除对集合会返回 **409**。


**核销组规则**

- 组形：一对一（1-1），或一侧单笔对另一侧 2～3 笔的一对多（1-2 / 1-3，两个方向均可）；
- 组内全部金额同号（全正或全负）；
- 组内最早与最晚时间差 ≤ 48 小时（含边界）；
- 两侧金额之和相等；
- 每笔流水最多进入一个核销组。

**优化目标与并列规则**

在所有合法候选组中选择互斥集合，使核销金额绝对值总和最大；存在多个最优解时，
按组编码 `(升序左侧 ID 列表, 升序右侧 ID 列表)` 排序后的列表字典序最小者胜出。

**响应示例**

```json
{
  "groups": [
    { "left_ids": ["L1"], "right_ids": ["R1", "R2"], "amount": 300 }
  ],
  "total_amount": 300,
  "unmatched_left_ids": [],
  "unmatched_right_ids": []
}
```

- `groups`：核销组列表（按组编码升序），`amount` 为该组带符号核销金额（分）；
- `total_amount`：各组核销金额绝对值之和（分）；
- `unmatched_left_ids` / `unmatched_right_ids`：两侧未匹配的 ID（升序）。

### 幂等重放（`Idempotency-Key` 请求头，可选）

`POST /reconcile` 支持通过 `Idempotency-Key` 请求头实现幂等重放：

- 请求头为可选；取值长度必须为 **1～128 个字符**（空值返回 **400**，缺失则按普通请求处理）。
- 携带该头时，服务端对**已通过校验的完整请求体**按对象键排序并做紧凑 JSON 编码，
  再以 **SHA-256** 计算请求指纹（fingerprint）。键、指纹和成功响应（HTTP 200 的响应体）
  会持久化到 SQLite。
- 首次请求正常求解，且**仅在返回 200 后**写入数据库；400 / 422 / 5xx 等失败响应
  不会占用该键，可用相同键重试。
- 相同键 + 相同指纹再次请求：**不会重新求解**，原样返回首次存储的响应，并附加响应头
  `Idempotency-Replayed: true`。
- 相同键 + 不同指纹（同一键被用于不同请求体）：返回 **409 Conflict**。
- 同一键的并发请求只会执行一次求解：其余请求阻塞等待并复用首个请求的结果
  （成功则重放，失败则自行重试）。

示例：

```bash
curl -X POST http://localhost:8000/reconcile \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-2026-09-18-0001' \
  -d '{"left":[{"id":"L1","time":"2026-09-17T00:00:00Z","amount":100}],"right":[]}'
```

幂等数据默认存储在 SQLite 文件中；使用 `docker compose` 启动时该文件位于命名卷
`idempotency-data`（容器内路径 `/data/idempotency.db`，由 `IDEMPOTENCY_DB_PATH`
环境变量配置），**容器重启后仍可重放**。本地直接运行时默认写入工作目录下的
`idempotency.db`。

### `POST /reconcile/unmatched-summary`

按指定时区汇总未匹配流水。请求体在 `POST /reconcile` 全部字段（`left` / `right` /
`locked_groups` / `excluded_pairs`，校验规则与求解结果完全一致）之上，额外要求一个
`timezone` 字段：

```json
{
  "left":  [{ "id": "L1", "time": "2026-09-17T16:00:00Z", "amount": 100 }],
  "right": [],
  "timezone": "Asia/Shanghai"
}
```

- `timezone` 必须是合法的 IANA 时区名（如 `UTC`、`Asia/Shanghai`、`America/New_York`）；
  非法取值返回 **422**，错误 `loc` 为 `["body", "timezone"]`。
- 服务端先按 `POST /reconcile` 相同的逻辑完成校验与求解（重复 ID 返回 **400**，
  `locked_groups` / `excluded_pairs` 校验失败返回 **422**），再把两侧**未匹配**流水
  （已匹配的流水不计入）的时间戳转换到请求时区，按**真实本地日期**归属——跨夏令时
  切换的日期按当时实际偏移计算。
- 该接口不参与 `Idempotency-Key` 幂等重放。

**响应示例**

```json
{
  "timezone": "Asia/Shanghai",
  "days": [
    {
      "date": "2026-09-18",
      "positive": { "count": 2, "amount": 300 },
      "negative": { "count": 1, "amount": 50 }
    }
  ],
  "total": {
    "positive": { "count": 2, "amount": 300 },
    "negative": { "count": 1, "amount": 50 }
  }
}
```

- `days`：有未匹配流水的本地日期（`YYYY-MM-DD`，升序），每日按正负号分别给出
  `count`（笔数）与 `amount`（绝对金额之和，分）；
- `total`：全批次跨日期的正负号总计；
- 没有未匹配流水时 `days` 为空数组，`total` 各项为 0。

### `POST /reconcile/audit`

审计**外部给出的完整核销方案**。请求体复用 `POST /reconcile` 的全部批次字段
（`left` / `right` / `locked_groups` / `excluded_pairs`，校验规则完全一致），
并**额外必传** `proposed_groups`——待审计的完整方案，每组的结构与
`locked_groups` 相同：

```json
{
  "left":  [
    { "id": "A", "time": "2026-09-17T00:00:00Z", "amount": 100 },
    { "id": "B", "time": "2026-09-17T00:00:00Z", "amount": 50 }
  ],
  "right": [
    { "id": "X", "time": "2026-09-17T00:00:00Z", "amount": 150 },
    { "id": "Y", "time": "2026-09-17T00:00:00Z", "amount": 100 }
  ],
  "proposed_groups": [
    { "left_ids": ["A", "B"], "right_ids": ["X"] }
  ]
}
```

**逐组校验规则。** 服务端对 `proposed_groups` **逐组独立**校验，按固定优先级
检查下列规则，单组错误**不会中止**其余组的检查；每组最多报告其第一条违规，
错误按下标稳定升序返回（HTTP **422**，`loc` 为
`["body", "proposed_groups", <下标>]`）：

1. 组内同一侧出现重复 ID（`proposed_group.duplicate_id`）；
2. 引用不存在的 ID（`proposed_group.unknown_id`）；引用了对侧 ID 报侧别错误
   （`proposed_group.wrong_side`）；
3. 跨组互斥：该组使用了更早（下标更小）的组或 `locked_groups` 已占用的流水，
   违规记在**后出现的组**上（`proposed_group.transaction_reused`）；
4. 组内同时包含某个排除对的两侧 ID（`proposed_group.excluded_pair`）；
5. 五种组形之一（`proposed_group.bad_shape`）、组内金额同号
   （`proposed_group.mixed_sign`）、两侧金额之和相等
   （`proposed_group.amount_mismatch`）、时间跨度 ≤ 48 小时
   （`proposed_group.outside_48h`）。

```json
{
  "detail": [
    {
      "type": "proposed_group.unknown_id",
      "loc": ["body", "proposed_groups", 0],
      "msg": "proposed_groups[0]: unknown transaction id: NOPE",
      "input": { "left_ids": ["A"], "right_ids": ["NOPE"] }
    },
    {
      "type": "proposed_group.bad_shape",
      "loc": ["body", "proposed_groups", 2],
      "msg": "proposed_groups[2]: illegal group shape: 2-2",
      "input": { "left_ids": ["A", "B"], "right_ids": ["X", "Y"] }
    }
  ]
}
```

方案中的组**原样复述**某个 `locked_groups`（编码完全相同）时视为该锁定组本身，
不计为跨组复用（完整方案本就包含锁定组）；同一锁定组复述两次时，第二组按复用
报错。批次级校验与 `/reconcile` 完全一致：批次内重复 ID 返回 **400**，
`excluded_pairs` / `locked_groups` 自身非法返回 **422**（各自的 `loc`）。

**方案合法时**，服务端在同一批次（含锁定组、排除对约束）上计算规范最优解
（核销总额最大；并列时组编码排序列表字典序最小），并与方案比较：

- `valid`：方案全部通过逐组校验（合法响应恒为 `true`，非法时整体返回 422）；
- `optimal`：方案与规范最优解**完全一致**（组集合相同；组的提交顺序不影响）；
- `suboptimal_reason`：合法但非最优的原因——
  - `"amount"`：**金额次优**，`proposed_total < optimal_total`；
  - `"lexicographic"`：**金额相同但未命中字典序规范解**；
  - 最优时为 `null`；
- `proposed_total` / `optimal_total`：方案与规范最优解各自的核销金额绝对值
  之和（分；规范解包含全部锁定组）；
- `groups`：**规范最优解的核销组**（按组编码升序），供调用方直接据此修正方案。

方案合法且命中最优时的响应：

```json
{
  "valid": true,
  "optimal": true,
  "suboptimal_reason": null,
  "proposed_total": 150,
  "optimal_total": 150,
  "groups": [
    { "left_ids": ["A", "B"], "right_ids": ["X"], "amount": 150 }
  ]
}
```

金额次优示例（方案只核销 100，规范最优为 150）：

```json
{
  "valid": true,
  "optimal": false,
  "suboptimal_reason": "amount",
  "proposed_total": 100,
  "optimal_total": 150,
  "groups": [
    { "left_ids": ["A", "B"], "right_ids": ["X"], "amount": 150 }
  ]
}
```

金额相同但未命中字典序规范解示例（两种配对都核销 200，
`(A,X)+(B,Y)` 字典序更小）：

```json
{
  "valid": true,
  "optimal": false,
  "suboptimal_reason": "lexicographic",
  "proposed_total": 200,
  "optimal_total": 200,
  "groups": [
    { "left_ids": ["A"], "right_ids": ["X"], "amount": 100 },
    { "left_ids": ["B"], "right_ids": ["Y"], "amount": 100 }
  ]
}
```

该接口不参与 `Idempotency-Key` 幂等重放。

### `POST /reconcile/diagnostics`

诊断**最优解中每笔未匹配流水**的原因。请求体与 `POST /reconcile` 完全相同
（`left` / `right` / `locked_groups` / `excluded_pairs`，校验规则与 400/422 行为
完全一致），服务端先执行相同的最优核销求解，再对每笔未匹配流水给出诊断：

```json
{
  "left":  [
    { "id": "A", "time": "2026-09-17T00:00:00Z", "amount": 100 },
    { "id": "B", "time": "2026-09-17T00:00:00Z", "amount": 100 }
  ],
  "right": [
    { "id": "X", "time": "2026-09-17T00:00:00Z", "amount": 100 }
  ]
}
```

**诊断规则。** 对每笔未匹配流水，枚举所有包含它、且不占用锁定组流水的
1-1 / 1-2 / 1-3 / 2-1 / 3-1 组合（已匹配但未锁定的流水仍可参与组合），
然后按固定顺序依次施加过滤，**首个使候选组合清空的过滤条件即为原因**：

| `reason`                 | 含义                                                     |
| ------------------------ | -------------------------------------------------------- |
| `no_counterparty`        | 对侧没有可用流水（对侧为空，或全部对侧流水被锁定组占用） |
| `sign_mismatch`          | 没有任何同号组合（对侧可用流水符号全部相反）             |
| `amount_mismatch`        | 有同号组合，但没有任何两侧金额之和相等的组合             |
| `time_window`            | 有等额组合，但全部超出 48 小时时间窗                     |
| `excluded_pair`          | 有合法组合，但全部命中排除对                             |
| `optimization_conflict`  | 存在完全合法的候选组合，但最优解未选用其中任何一个       |

**响应示例**（最优解为 `(A,X)`，`B` 与 `X` 本可合法成组但未被选中）：

```json
{
  "groups": [
    { "left_ids": ["A"], "right_ids": ["X"], "amount": 100 }
  ],
  "total_amount": 100,
  "unmatched_left_ids": ["B"],
  "unmatched_right_ids": [],
  "diagnostics": [
    { "id": "B", "side": "left", "reason": "optimization_conflict", "candidates": ["X"] }
  ]
}
```

- `groups` / `total_amount` / `unmatched_left_ids` / `unmatched_right_ids`：与
  `POST /reconcile` 的响应完全相同；
- `diagnostics`：仅包含未匹配流水的诊断，按**左侧在前、同侧按输入顺序**排列；
  - `side`：`"left"` / `"right"`；
  - `candidates`：仅当 `reason` 为 `optimization_conflict` 时非空——与该流水共同出现在
    至少一个合法候选组合中的**对侧流水 ID**（去重，按对侧输入顺序排列），其余原因下为空数组；
- 没有未匹配流水时 `diagnostics` 为空数组。

该接口不参与 `Idempotency-Key` 幂等重放。

## 算法

1. 枚举全部合法候选组（每侧 ≤12 笔，候选上限约 7000 个）；
2. 以位掩码表示流水集合，用带备忘录的分支限界（MRV 选点 + 双侧金额和上界 +
   贪心下界）求任意子集的最大可核销权值；
3. 在达到最大权值的前提下，逐步选取能构成残余最优解中编码最小的候选组，
   得到字典序最小的最优解。

存在 `locked_groups` 时，先将全部锁定组（已通过同样的合法性校验）对应的流水从
位掩码中剔除，再对剩余掩码执行步骤 2、3，最后把锁定组并入结果。

存在 `excluded_pairs` 时，步骤 1 枚举候选组会直接丢弃任何同时包含某个排除对两侧
ID 的候选组（锁定组若命中排除对则在求解前返回 422），步骤 2、3 在剩余候选集上
照常执行。

实测 12×12 极端稠密批次求解耗时约 50ms 以内。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

包含 API 行为测试与求解器对暴力枚举的随机差分测试（含带 `locked_groups`、
`excluded_pairs` 以及二者组合的场景），以及 `/reconcile/audit` 的逐组错误、
金额次优与字典序次优场景测试、`/reconcile/diagnostics` 的六种诊断原因与
候选列表（对照候选池的随机校验）测试。

## 项目结构

```
app/
  main.py         # FastAPI 入口与 /reconcile、/reconcile/audit、
                  # /reconcile/unmatched-summary、/reconcile/diagnostics 端点
  models.py       # 请求/响应模型与校验
  solver.py       # 候选生成 + 最优互斥集求解 + 未匹配诊断
  idempotency.py  # Idempotency-Key 指纹与 SQLite 持久化
tests/            # pytest 测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
