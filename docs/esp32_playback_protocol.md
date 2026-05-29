# ESP32 下行播放序列协议（pb）

BotServer 经 WebSocket（例如 `/asr_chat` 等业务通道）**下行**到 ESP32：按时间片组合 **PCM 音频**、**屏幕矢量动画**、**舵机**，支持优先级队列与上行回压。

本文档版本：**pb v2**（根对象 `pb_ver: 2`；缺省或无 `pb_ver` 时固件可按 v1 兼容，但 v2 起 **`action` 为 `replace` / `append` / `default`**，并新增 **`level`**）。

---

## 1. 传输模型

| 项目 | 约定 |
|------|------|
| 文本帧 | UTF-8 JSON 对象，单行或多行均可，解析后为一条消息。 |
| 二进制帧 | 原始 PCM 字节流，**无**额外包头；格式由同 `req` 的 `pb_start`（链式首包）或 **`pb_single`**（单片且含音频时）中的 `sr` / `fmt` / `ch` 描述。 |
| 顺序 | 当某条 JSON 含 `audio.next_bin === 1` 时，**下一条 WebSocket 消息必须是二进制帧**，且长度等于该片 PCM 字节数（须满足 **R6** 与 `chunk_ms`/`sr` 的整除关系）；再之后才继续发 JSON。 |

**BotServer 出站顺序**：对同一 `/asr_chat` WebSocket，所有文本/binary 帧经 **单连接发送锁** 串行化；带 `audio.next_bin` 的 JSON 与其 PCM **在同一把锁内连续写出**，避免 `face_info` 等其它 JSON 插在中间导致下位机「expect binary 却收到 JSON」、进而误判新序列或丢弃 PCM。

---

## 2. 全局原则

| 编号 | 规则 |
|------|------|
| R0 | `pb_start` / `pb_chunk` / `pb_end` / **`pb_single`** 中，`audio` / `servo` / `anim` **至少出现一项**；未使用的键省略。 |
| R1 | **`level` 与 `action` 共同决定入队策略**（见 §2.1）。仅 **`pb_start`**（链式首包）与 **`pb_single`** 触发队列决策；同 `req` 后续 `pb_chunk` / `pb_end` 随首包已入队的序列继续下发，**不再**重复决策。 |
| R2 | 含 `audio.next_bin: 1` 时，**紧随其后的下一条 WS 消息为 binary PCM**（`fmt` 一般为 `s16le`）。 |
| R3 | 若设备端预期收到 binary 却收到 JSON（或长度不符），视为 **协议错位**：应丢弃当前 `req` 的后续片、清空队列，并可向服务端上报或等待 `pb_cancel`。 |
| R4 | 同一 `req` 下，所有分片的 **`idx` 从 0 起严格递增**（步长 1），与发送顺序一致。 |
| R5 | **`chunk_ms`**：该片对应的 **口型/动画保持时长**（毫秒），与该片 PCM 时长应对齐（由服务端保证）；设备在 `[t, t+chunk_ms)` 内展示该片的 `anim`（及同步播放该片音频）。 |
| R6 | **PCM 长度与 `chunk_ms` 一致（整除语义）**：mono `s16le` 时，该片 binary 字节数应等于 `(chunk_ms * sr // 1000) * 2`（与 C/Java 等整数除法一致）。BotServer 会对音素切片做 trim/pad 以满足该式；设备也可用 **实际 binary 长度** 推算播放时长，避免边界差几个采样即判错。 |

### 2.1 队列决策（`pb_start` / `pb_single`）

设备维护 **当前队列优先级 `queue_level`**（队列空时可视为 `-1` 或 `0`，实现自定但须与下表一致）。

收到 **`pb_start`** 或 **`pb_single`** 时，读取 **`level`**（整数 0–3，缺省建议按 **1** 处理）与 **`action`**（`replace` \| `append` \| `default`，缺省 **`replace`**）：

| 条件 | 行为 |
|------|------|
| **`level` > `queue_level`** | **清空队列**，停止当前播放，按新指令执行；更新 `queue_level = level`。（**与 `action` 无关**，高优先级始终抢占。） |
| **`level` < `queue_level`** 且 **`action` = `append`** | **不清空**；将本 `req` 全部分片 **追加到队列末尾**；`queue_level` 不变。 |
| **`level` == `queue_level`** 且 **`action` = `append`** | 将本 `req` **追加到队列末尾**；`queue_level` 不变。 |
| **`level` == `queue_level`** 且 **`action` = `replace`** | **清空队列**，停止当前播放，按新指令执行；`queue_level` 保持为 `level`。 |
| **`level` <= `queue_level`** 且 **`action` = `default`** | 见 §2.2（智能追加 / 丢弃，**不打断**同级或更高优先级）。 |

**`action` 取值**

| 值 | 含义 |
|----|------|
| **`replace`** | 同级时 **取代**：清空队列后执行（缺省）。 |
| **`append`** | 同级或更低优先级相对队列时 **无条件追加** 到队尾。 |
| **`default`** | **智能追加**：在 **不打断** 同级/更高优先级的前提下，尽量让指令 **连续排队**；若已有过多更高优先级序列则 **丢弃本条**，避免队列过长。 |

### 2.2 `action = default` 细则

当 **`level` <= `queue_level`**（本条 **不能** 抢占当前队列）时：

1. 统计播放队列中（**含当前正在播放** 的序列，按 `req` 计）满足 **`seq.level > level`**（严格大于本条 `level`）的 **序列个数**，记为 **`n_high`**。
2. 若 **`n_high` <= 1**：行为同 **`append`**——将本 `req` 追加到队尾，`queue_level` 不变。
3. 若 **`n_high` > 1**：**丢弃本条**（不入队、不打断、不替换），建议打日志。

**设计意图**

- **`n_high` == 0**：无更高优先级挡路 → 追加，同级指令可 **连续执行**。
- **`n_high` == 1**：至多一条更高优先级（如在播口播）→ 追加到其 **后面**，待其播完再执行。
- **`n_high` > 1**：已有多条更高优先级排队 → **不再追加**，防止低优先级 idle 等 **无脑堆长队**。

**示例（本条 `level=0` idle）**

| 队列状态 | `n_high` | `default` 结果 |
|----------|----------|----------------|
| 空闲 / 仅 level=0 在播 | 0 | 追加 |
| 1 条 level=1 在播 | 1 | 追加（口播后再打盹） |
| level=1 在播 + 1 条 level=1 排队 | 2 | **丢弃** |
| level=2 在播 + 1 条 level=1 排队 | 2 | **丢弃** |

**`level` 语义（BotServer 约定）**

| 值 | 含义 |
|----|------|
| **0** | idle 状态（打盹、空闲低头等低优先级待机） |
| **1** | task 状态（口播 TTS、对话表情等正常任务） |
| **2** | 紧急安全状态（急停、告警等，可抢占 task/idle） |
| **3** | 调试状态（调试页手动舵机、手动场景等） |

**链式多片**：同一 `req` 内各分片 **`action` / `level` 应一致**（与首包 `pb_start` 相同），避免固件歧义。

**v1 兼容**：若收到 `action: "opportunistic"`（已废弃），建议按 **`level=0` + `action=append`** 降级处理并打日志。

---

## 3. 下行 JSON 公共字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | `pb_start` \| `pb_chunk` \| `pb_end` \| **`pb_single`** \| `pb_cancel` |
| `req` | string | 是 | 本次播放序列 ID，建议 **16 位小写十六进制**（8 字节随机）。 |
| `idx` | number | 见下 | 分片序号，非负整数。`pb_start` / **`pb_single`** 若省略则视为 `0`。 |
| `chunk_ms` | number | 建议 | 该片时长（ms）；含 `anim` 或含 `audio.next_bin` 时**应填**，便于口型同步。 |
| `sr` | number | 条件 | 采样率。在 **每个 `req` 的第一条携带音频参数的包**上必填：链式时首包为 `pb_start`；**整轮仅一条 JSON** 时用 **`pb_single`** 并在该条带齐 `sr` / `fmt` / `ch`。 |
| `fmt` | string | 条件 | 音频编码，当前约定：`s16le`（16 位有符号小端 PCM）。 |
| `ch` | number | 条件 | 声道数，当前约定：`1`（单声道）。 |
| `audio` | object | 否 | 见 §5。 |
| `servo` | object | 否 | 见 §6。 |
| `anim` | object | 否 | 见 §4。 |
| `level` | number | 建议 | 优先级 **0–3**，见 §2.1。`pb_start` / `pb_single` **应填**；链式后续片建议与首包相同。 |
| `action` | string | 否 | 入队策略：`replace`（取代，默认）\| `append`（无条件追加）\| `default`（智能追加，见 §2.2）。 |

**分片类型与 `idx`**

- **`pb_single`（单片自成一轮）**：整段序列**只有这一条 JSON**（无前置 `pb_start`、无后续 `pb_chunk`/`pb_end`）。`idx` 一般为 `0`。可仅含 `servo` / 仅含 `anim` / 或含 `audio.next_bin`+PCM；若含音频，**本条**须带齐 `sr` / `fmt` / `ch`。调试舵机、口播仅一片等均用此类型。
- **链式多片 `N > 1`**：第 1 条必须为 **`pb_start`**（`idx === 0`），中间 **`pb_chunk`**（`idx === 1 … N-2`），最后一条 **`pb_end`**（`idx === N-1`）。**禁止**在无 `pb_start` 时单发一条 `pb_end` 当作单片（与 `pb_single` 混淆时由下位机拒绝）。
- **链式单片 `N === 1` 且走链式语义**：仍发一条 **`pb_single`**（与上条一致），不再使用「孤立的 `pb_end`」表示单片。

**可选调试字段（设备可忽略）**

- `phoneme`：字符串，该片对应音素符号，仅日志/调试。
- `pb_ver`：数字，协议版本；v2 起为 `2`。

### 3.1 `face_info` 上的 `action`（非 pb，可选）

经 BotServer 转发到 `/asr_chat` 的 **`face_info`**（跟随人脸、注视感知等）**不参与 pb 队列**，无 `level` 字段。可选 `action: "append"` 表示不抢占口播序列；设备可将「头部/注视目标」维护为独立通道。

---

## 4. 动画 `anim`

### 4.1 结构

```json
{
  "anim": {
    "elements": {
      "mouth": [],
      "nose": [],
      "eye_l": [],
      "eye_r": [],
      "extra": []
    }
  }
}
```

- 键名：`mouth`、`nose`、`eye_l`、`eye_r` 为 **固定主层**；**`extra`**（可选）为 **附加图元层**，与脸 bundle 中 `extra` 态一致，用于腮红、汗滴、符号等 **非眼鼻嘴** 的装饰或情绪表达，每项仍为 §4.3 图元对象。
- 每个值为 **图元数组**；某层无内容时可省略该键或给空数组 `[]`。

### 4.2 逻辑坐标系（canonical）

- 与 BotServer 仿真页一致：**宽 128、高 64** 逻辑像素。
- 原点 **左上角** `(0,0)`，`x` 向右、`y` 向下。
- 合法范围建议：`x ∈ [0,127]`，`y ∈ [0,63]`（图元可部分越界，由产品决定是否裁剪）。

设备可将逻辑坐标按比例映射到物理 OLED 分辨率。

### 4.3 图元类型（`shape`）

数组中每个元素为对象，**必须**含 `shape`。

**完整 shape 主名、别名、字段与 `face_bundle` 配置**见 [pb_face_bundle_and_shape_protocol.md](./pb_face_bundle_and_shape_protocol.md)（含 `text` 图元与音素 offset）。

以下为最小子集说明（兼容旧实现）；固件建议按专文实现全表。

#### `rect`（矩形，嘴型默认用矩形）

| 字段 | 类型 | 说明 |
|------|------|------|
| `shape` | `"rect"` | 固定 |
| `x`, `y` | number | 左上角 |
| `w`, `h` | number | 宽、高（正整数） |

#### `circle`（圆，眼、鼻默认可用圆）

| 字段 | 类型 | 说明 |
|------|------|------|
| `shape` | `"circle"` | 固定 |
| `x`, `y` | number | 圆心 |
| `r` | number | 半径（正整数） |

#### `line`（线段）

| 字段 | 类型 | 说明 |
|------|------|------|
| `shape` | `"line"` | 固定 |
| `x1`, `y1`, `x2`, `y2` | number | 端点 |

未知 `shape` 的图元：**跳过**绘制，勿整包失败。

### 4.4 时间语义

- 收到带 `anim` 的 `pb_*` 且带 `chunk_ms`：在 **该片音频播放区间**（若有）内展示 `elements`；区间长度为 `chunk_ms`。
- 多片序列：第 `idx` 片与第 `idx` 片 PCM 一一对应；动画切换时刻与 PCM 片边界对齐。

### 4.5 绘制顺序（建议）

为与 Web 仿真一致，建议 **从后到前** 绘制：`nose` → `mouth` → `eye_l` → `eye_r` → **`extra`**（`extra` 画在最上层，便于装饰盖住五官）。若实现不同，需在固件侧固定一种顺序并写死。

---

## 5. 音频 `audio`

下行仅使用以下约定（**不要**依赖调试页里的 `_pcm_bytes` 等私有字段；真实下发不应包含下划线前缀字段）。

```json
"audio": { "next_bin": 1 }
```

含义：本条 JSON 处理完后，**下一条 WS 消息**为 **binary**，内容为该片 PCM。mono `s16le` 时字节数须满足 **R6**：与 `chunk_ms`、`sr` 的整除公式一致（BotServer 已对齐）；若自行实现校验，亦可直接用 **binary 帧实际长度** ÷ 2 ÷ `sr` 得到该片时长。

- 若该片无音频：省略 `audio`，且下一条仍为 JSON。
- PCM 参数以本 `req` **首条**带 `sr`/`fmt`/`ch` 的消息为准；后续片相同。

---

## 6. 舵机 `servo`

```json
"servo": {
  "xm": 0,
  "ym": 0,
  "x": 90,
  "y": 90,
  "ms": 200
}
```

| 字段 | 说明 |
|------|------|
| `xm` / `ym` | 模式：`0` = **绝对**位置；`1` = **相对**当前位置增量；`2` = **本轴本包内不驱动**（保持）。 |
| `x` / `y` | 整数，单位依舵机安装定义（如角度或脉宽档位）。 |
| `ms` | 整数，本指令期望在 **多少毫秒内** 完成到位或完成相对行程（产品可简化为插值时间）。 |

可与 `anim` / `audio` 同片下发。

**保持 / 延时（BotServer 生成）**：当 LLM 在 `servo` 计划中要求 **hold** 时，服务端会插入仅含静音 PCM 的分片，并在该片附带 `servo`：`xm=2`、`ym=2`、`x`/`y` 为 `0`、`ms` 与该分片 `chunk_ms` 一致（双轴本包不驱动、时长与口型片对齐）。固件应按 `chunk_ms` 播放该片静音并保持舵机姿态，勿将「无位移」误判为错误包。

---

## 7. 下行 `pb_cancel`

服务端检测到错误或主动中止时下发：

```json
{ "type": "pb_cancel", "req": "a1b2c3d4e5f67890" }
```

设备应：**停止**该 `req`（若 `req` 省略则停止当前序列，由实现约定）、清空队列、丢弃未消费的 binary 预期。字段可扩展，未知键忽略。

---

## 8. 上行回压 `pb_ack`

设备在消费完某片或缓冲变化时上报，便于服务端控流：

```json
{ "type": "pb_ack", "req": "a1b2c3d4e5f67890", "idx": 12, "audio_buf_ms": 360 }
```

| 字段 | 说明 |
|------|------|
| `req` | 对应下行序列 ID。 |
| `idx` | 已稳定播放或已确认的 **最大分片序号**（实现可约定为「已入队」或「已播放完」）。 |
| `audio_buf_ms` | 当前设备端音频 jitter 缓冲估算（ms），无则可填 `0`。 |
| `servo` | **可选**。当前舵机反馈：实时位置与建议行程边界（整数），供 BotServer 注入 LLM 规划相对 `servo` 下发。无则省略；未知子键忽略。 |

**带舵机反馈的扩展示例（可选字段）**

```json
{
  "type": "pb_ack",
  "req": "a1b2c3d4e5f67890",
  "idx": 12,
  "audio_buf_ms": 360,
  "servo": {
    "x": 64,
    "y": 104,
    "x_min": 65,
    "x_max": 115,
    "y_min": 45,
    "y_max": 135
  }
}
```

| `servo` 子字段 | 说明 |
|----------------|------|
| `x` / `y` | 当前双轴读数或内部估计位置（与下行 `servo` 同一量纲约定）。 |
| `x_min` / `x_max` / `y_min` / `y_max` | 建议安全行程（软限位或标定边界）；服务端可据此提示模型勿超范围。 |

---

## 9. 时序示例

### 9.1 两片：首片有音频（task 级打断）

1. JSON：`{ "type":"pb_start","req":"01…","idx":0,"chunk_ms":120,"sr":24000,"fmt":"s16le","ch":1,"pb_ver":2,"level":1,"action":"replace","audio":{"next_bin":1},"anim":{...} }`
2. **Binary**：长度 = `24000 * 0.12 * 1 * 2` = 5760 字节的 s16le mono PCM。
3. JSON：`{ "type":"pb_end","req":"01…","idx":1,"chunk_ms":80,"level":1,"action":"replace","anim":{...} }`（第二片若无音频则无 `audio`）

### 9.2 仅动画、无音频（idle 智能追加）

```json
{ "type":"pb_single","req":"ab…","idx":0,"chunk_ms":500,"pb_ver":2,"level":0,"action":"default","anim":{"elements":{"mouth":[{"shape":"rect","x":46,"y":46,"w":36,"h":9}]}} }
```

若当前仅 1 条 `level=1` 口播在播，此包 **排队等待**；若已有 2 条更高优先级序列，则 **丢弃**。

### 9.3 调试舵机抢占

```json
{ "type":"pb_single","req":"cd…","idx":0,"chunk_ms":500,"pb_ver":2,"level":3,"action":"replace","servo":{"xm":0,"ym":0,"x":90,"y":135,"ms":500} }
```

`level=3` 高于队列中 `level=1` 时，**清空队列**并立即执行。

---

## 10. ESP32 实现清单（摘要）

1. 维护当前 `req`、期望下一帧是 **JSON** 还是 **binary**、以及 **`queue_level`**。
2. 收到 **`pb_start` / `pb_single`** 时按 §2.1 / §2.2 比较 **`level`** 与 **`action`**；`default` 须实现 **`n_high`** 计数。`pb_chunk` / `pb_end` 仅续传同 `req`。
3. 解析 `anim.elements` **主四层**及可选 **`extra`** 图元数组，按 §4.3 绘制矢量图元。
4. `chunk_ms` 与 PCM 片长一致时，口型与声音对齐。
5. 适当频率发送 `pb_ack`，避免缓冲区溢出或欠载。

---

## 修订记录

- **pb v2**（2026-05）：移除 `opportunistic`；`action` 为 `replace` / `append` / `default`；新增 `level`（0–3）及 §2.1 / §2.2 队列决策。
- **pb v1**（2026-05）：`pb_start`/`pb_chunk`/`pb_end`/`pb_single`、binary PCM、`anim.elements`、`servo`、`pb_ack`/`pb_cancel`、`action` 枚举（含 `opportunistic`）。
- 图元与 `face_bundle` 配置见 [pb_face_bundle_and_shape_protocol.md](./pb_face_bundle_and_shape_protocol.md).
