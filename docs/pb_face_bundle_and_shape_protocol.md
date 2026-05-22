# PB 表情数据与矢量图元协议（v1）

本文档约定 **BotServer 下发的 pb 动画** 与 **独立 JSON 表情配置**（`face_bundle`）的字段、**图元 `shape` 全集及别名**、以及 **服务端组帧逻辑**。供固件（ESP32 / Adafruit_GFX 系）、仿真页、与工具链对齐实现。

关联文档：[ESP32 下行播放协议（pb）](./esp32_playback_protocol.md)（传输层、`anim.elements` 容器结构）。

---

## 1. 坐标系与单位

| 项目 | 约定 |
|------|------|
| 画布 | 逻辑像素 **宽 128 × 高 64**（与当前仿真页一致） |
| 原点 | 左上角 `(0,0)`，`x` 向右、`y` 向下 |
| 数值 | JSON 中为 **number**（实现侧建议整数化）；角度为 **度**（°） |
| 裁剪 | 图元可部分越界；设备端可裁剪或允许越界绘制 |

固件将逻辑坐标映射到物理分辨率时，应保持 **宽高比或统一缩放**，避免只缩 `x` 不缩 `y`。

---

## 2. 表情配置（`face_bundle`）总结构

表情数据用于 **音素对齐 TTS（pb 模式）** 下，由服务端根据音素序列与时间相位生成每片的 `anim.elements`。可来自：

- 内置：`default_pb_face_bundle()` / `demo_pb_face_bundle()`（代码）
- 外置：**JSON 或 YAML 文件**（推荐生产可编辑）；配置项 `tts.pb_face_bundle_json` 或环境变量 `DESKBOT_PB_FACE_BUNDLE_JSON`；**保存文件后按 `mtime` 热重载**（无需重启进程）

### 2.1 顶层键

| 键 | 类型 | 说明 |
|----|------|------|
| `mouth_by_phoneme` | `object` | 音素字符串 → **单音素口型**（`elements` + `offset` 或图元列表）；**不含**共享条 |
| `mouth_by_phoneme_groups` | `array` | 可选。每项为 **共享条**（`states` + `elements` + `offset`），见 §2.2。与上一项一起 **展开** 后查表（§3.3） |
| `eye_l` | `object` | 左眼三态图元；可与 `eye_l_groups` 混排（见 §2.3） |
| `eye_l_groups` | `array` | 可选。眼 **共享条**：`states` ⊆ `{default,open,close}` + `elements`（无 `offset`） |
| `eye_r` | `object` | 右眼：同左眼 |
| `eye_r_groups` | `array` | 可选。右眼共享条 |
| `nose` | `object` | 鼻：`default` 图元；可与 `nose_groups` 混排（见 §2.4） |
| `nose_groups` | `array` | 可选。鼻共享条：`states` 仅 `"default"` + `elements` |
| `extra` | `object` | 可选。**附加装饰层**：任意 **态名字符串** → 图元数组（与 `eye_l.default` 等同结构，见 §2.5）；缺省可省略，运行时等价 `{"default": []}` |
| `extra_groups` | `array` | 可选。附加层共享条：`states`（非空字符串列表）+ `elements`，**无** `offset`（与 `eye_*_groups` 形状相同、语义不同） |
| `metadata` | `object` | 时间相位控制（眨眼、鼻轮换等），可扩展（见 §5） |

可选保留键 `_comment`、`_doc` 等以下划线开头字段：**实现须忽略**，不参与组帧。

### 2.2 口型：`mouth_by_phoneme` 与 `mouth_by_phoneme_groups`

**单音素**（`mouth_by_phoneme` 中键为音素符号、值为口型对象）：

```json
{
  "elements": [ { "shape": "rect", "x": 49, "y": 52, "w": 30, "h": 4 } ],
  "offset": { "x": 0, "y": 0 }
}
```

**共享条**（推荐放在 **`mouth_by_phoneme_groups` 数组** 中；`states` 须为非空 **字符串** 列表，且与眼的 `states` 图元帧数组区分）。展开后等价于为 `states` 中每个音素各写一份上表。

```json
{
  "states": ["x", "y"],
  "elements": [ { "shape": "rect", "x": 57, "y": 44, "w": 14, "h": 9 } ],
  "offset": { "x": 0, "y": 1 }
}
```

| 字段 | 说明 |
|------|------|
| `states` | （仅共享条）音素符号列表；展开后每个音素映射到同一份 `elements`/`offset` |
| `elements` | **图元数组**（§6），描述嘴部矢量；**不参与**音素脸偏移 |
| `offset` | **整型** `x`,`y`：该片在合成 **鼻、左眼、右眼** 时，对图元坐标做 **平移 `(dx,dy)`**；嘴部 **不** 应用此偏移 |

同一音素在共享条与单键中均有定义时：**单键优先**（后写入的覆盖先展开的共享条）。

未知音素：使用键 **`"_"`** 的口型条目；若仍缺失则使用服务端内置默认口型。

### 2.3 眼（`eye_l` / `eye_r`）与 `eye_l_groups` / `eye_r_groups`

与 **音素无关**；与 `metadata.blink` 配合做眨眼。规范结果为每只眼 **对象** 含 `default` / `open` / `close`，值均为 **图元数组**。

**单态直写**（对象内三键）：

```json
{
  "default": [ { "shape": "circle", "x": 42, "y": 26, "r": 4 } ],
  "open": [ { "shape": "circle", "x": 42, "y": 26, "r": 7 } ],
  "close": [ { "shape": "line", "x1": 34, "y1": 26, "x2": 50, "y2": 26 } ]
}
```

**共享条**（推荐顶层 **`eye_l_groups` / `eye_r_groups` 数组**；每项无 `offset`）：

```json
{
  "states": ["default", "open"],
  "elements": [ { "shape": "ellipse_fill", "x": 42, "y": 26, "rw": 5, "rh": 4 } ]
}
```

| 键 | 说明 |
|----|------|
| `default` | **不眨眼**（`open_ms + close_ms <= 0`）时整段使用的眼图元；或作 `open`/`close` 缺省时的回退 |
| `open` | 眨眼周期中 **睁眼段** 使用的图元；缺省时回退到 `default` |
| `close` | 眨眼周期中 **闭眼段** 使用的图元；缺省时回退到 `default` |

### 2.4 鼻（`nose`）与 `nose_groups`

与 **音素无关**；**无多状态轮换**，逻辑上仅 `default`。

**单键**：

```json
{
  "default": [ { "shape": "circle", "x": 64, "y": 34, "r": 5 } ]
}
```

**共享条**（可选顶层 **`nose_groups`**；`states` 目前仅 `["default"]`）：

```json
{
  "states": ["default"],
  "elements": [ { "shape": "circle", "x": 64, "y": 34, "r": 5 } ]
}
```

| 键 | 说明 |
|----|------|
| `default` | 鼻图元列表 |

眼、鼻、**extra** 图元在写入 `anim.elements` 前均叠加 **当前音素口型的 `offset`**（§7）。

### 2.5 附加层 `extra` 与 `extra_groups`

与 **音素无关**；用于眼、鼻、嘴之外的 **装饰或情绪**（符号、腮红、汗滴等）。结构与 **单眼某态** 相同：对象上 **任意态名字符串** → **图元数组**。

**多态直写**：

```json
"extra": {
  "default": [],
  "playful": [
    { "shape": "circle", "x": 8, "y": 8, "r": 3 },
    { "shape": "circle", "x": 120, "y": 8, "r": 3 }
  ]
}
```

**共享条**（可选顶层 **`extra_groups`**；每项 `states` + `elements`，**无** `offset`；与 `eye_l_groups` 形状相同，但 **不得** 放入 `eye_l_groups`）：

```json
"extra_groups": [
  {
    "states": ["curious", "thinking"],
    "elements": [{ "shape": "line", "x1": 20, "y1": 4, "x2": 28, "y2": 0 }]
  }
]
```

| 键 / 字段 | 说明 |
|-----------|------|
| 态名（如 `default`、`playful`） | 图元数组；**`default`** 建议始终存在，可为空数组 |
| `metadata.extra_state` | 字符串，指定当前整段 TTS 使用哪一 **extra** 态；缺省为 `"default"`。若该态不存在则回退到 `default` 态图元 |

---

## 3. 组帧逻辑（服务端 → 每片 `anim`）

实现参考：`deskbot-server/pb_anim_defaults.py` 中 `phoneme_seq_to_anim_seq`。

### 3.1 输入

- **分片列表** `segments`：`[{ "phoneme": string, "ms": int, ... }, ...]`（与 PaddleSpeech 音素流对齐顺序一致）
- **`face_bundle`**：§2 结构

### 3.2 时间轴（相位）

- 定义 **`elapsed_ms`**：从 **首片起点** 起算的累计毫秒（取 **当前片起始时刻** 的累计值；即第 `i` 片使用 `sum(segments[0..i-1].ms)`）。
- **眨眼（左右眼同一相位）**：由 `metadata.blink` 的 `open_ms` / `close_ms` 决定当前处于 **睁眼段** 还是 **闭眼段**，从而选用眼的 **`open`** 或 **`close`** 图元；若 `open_ms + close_ms <= 0`，则恒用 **`default`**。
- **鼻**：始终使用 **`nose.default`**，无时间切换。
- **附加层 `extra`**：由 `metadata.extra_state`（缺省 `"default"`）选定态，取 `extra.<态名>` 图元列表；缺态则回退 **`extra.default`**；仍经 **§7 偏移** 写入 `anim.elements.extra`。

### 3.3 每片输出

对第 `i` 片：

1. 将 `mouth_by_phoneme_groups` 与 `mouth_by_phoneme` 合并展开（`expand_mouth_by_phoneme`）后，取 `ph = phoneme`（trim），查表，缺则用 `"_"`，再缺则默认口型。
2. `mouth = elements`（**不加** offset）。
3. `(dx,dy) = offset`。
4. 根据 `elapsed_ms` 与 `metadata.blink` 得到相位，从 `eye_l` / `eye_r` 取 **`open` 或 `close` 或 `default`** 图元列表（按 §4.1 回退规则），经 **§7 偏移** 得到下发 `eye_l`、`eye_r`。
5. 取 `nose.default`，经 **§7 偏移** 得到 `nose`。
6. 取 `extra` 中 `metadata.extra_state` 对应态（回退 `default`），经 **§7 偏移** 得到 `extra`。
7. 输出一行：`{ "idx", "chunk_ms", "phoneme", "anim": { "elements": { mouth, nose, eye_l, eye_r, extra } } }`（`extra` 恒为数组，可为空）。

随后与 PCM 对齐、`pb_start`/`pb_chunk`/`pb_end` 链式或单片 `pb_single` 封装见 [esp32_playback_protocol.md](./esp32_playback_protocol.md)。

### 3.4 绘制顺序（建议）

与下行协议建议一致：**`nose` → `mouth` → `eye_l` → `eye_r` → `extra`**（`extra` 画在最上层）。固件与仿真应统一，避免「换皮」后叠放顺序不一致。

---

## 4. 元数据 `metadata`

### 4.1 `metadata.blink`（左右眼共用）

| 字段 | 类型 | 说明 |
|------|------|------|
| `open_ms` | int | 单周期内 **睁眼段** 时长（毫秒），该段使用眼的 **`open`** 图元 |
| `close_ms` | int | 单周期内 **闭眼段** 时长（毫秒），该段使用眼的 **`close`** 图元 |
**相位**：`cycle = open_ms + close_ms`。若 `cycle <= 0`：整段 TTS 使用眼的 **`default`** 图元。若 `cycle > 0`：`pos = elapsed_ms % cycle`，`pos < open_ms` 时用 **`open`**，否则用 **`close`**（缺项回退见 §2.3）。

### 4.2 `metadata.extra_state`（附加层态）

| 字段 | 类型 | 说明 |
|------|------|------|
| `extra_state` | string | 选用 `extra` 对象上的哪一个 **态名** 的图元列表写入每片 `anim.elements.extra`；缺省 **`"default"`**。若该键不存在或对应态无数组，则回退 **`extra.default`**（参见 §2.5）。 |

### 4.3 扩展

新增键应由 **文档 + 版本号** 约定；旧固件 **忽略未知键**。服务端同理。

---

## 5. 配置文件与热切换

- **主配置路径**：`tts.pb_face_bundle_json` 或 `DESKBOT_PB_FACE_BUNDLE_JSON`（相对 `deskbot-server` 或绝对路径）。
- **叠加层**（可选）：`tts.pb_face_bundle_file` / `DESKBOT_PB_FACE_BUNDLE_FILE`，在「JSON 主配置」或「内置 profile」之上 **浅合并** 覆盖部分字段（实现见 `merge_pb_face_bundle`）。
- **热切换**：每次组帧前比较文件 **`mtime`**，变化则重新解析并更新缓存；换文件内容保存即可生效。

仓库示例：`deskbot-server/data/pb_face_bundle_demo.json`。预置场景见 `deskbot-server/data/pb_scenes_idle_sleep_guard.json`。

**编写与校验**见本文 §10。

---

## 6. 图元 `shape` 与参数（主名称 + 别名）

以下 **主 `shape` 字符串** 为协议规范名；**别名**在 JSON 中 **与主名等价**，解析层应在绘制前 **归一化**到主名或统一走同一绘制分支。

**通用规则**

- 除另有说明外，所有 **坐标类字段** 在应用 **音素 `offset`** 时均加上 `(dx, dy)`（§7）。
- 长度、半径、角度 **不** 因 offset 改变。
- 未知 `shape`：**跳过**该图元，**不** 判整包失败（与 pb 协议一致）。

### 6.1 对照表（主名称 | 别名 | Adafruit_GFX 语义 | 必填 JSON 字段 | 说明）

| 主 `shape` | 别名（等价） | GFX API 参考 | 必填字段 | 说明 |
|------------|----------------|----------------|----------|------|
| `rect` | `fill_rect`, `fillRect` | `fillRect` | `x`, `y`, `w`, `h` | 实心矩形；建议 `w>0` 且 `h>0` |
| `rect_outline` | `draw_rect`, `drawRect` | `drawRect` | `x`, `y`, `w`, `h` | 矩形描边 |
| `circle` | `fill_circle`, `fillCircle` | `fillCircle` | `x`, `y`, `r` | 实心圆；建议 `r>0` |
| `circle_outline` | `draw_circle`, `drawCircle` | `drawCircle` | `x`, `y`, `r` | 圆描边 |
| `line` | `drawLine`（建议小写 `drawline` 归一） | `drawLine` | `x1`, `y1`, `x2`, `y2` | 线段 |
| `pixel` | `point`, `drawPixel` | `drawPixel` | `x`, `y` | 单点 |
| `hline` | `h_line`, `drawFastHLine` | `drawFastHLine` | `x`, `y`, `w` | 水平线；`w` 可为负，与库语义一致 |
| `vline` | `v_line`, `drawFastVLine` | `drawFastVLine` | `x`, `y`, `h` | 垂直线；`h` 可为负，与库语义一致 |
| `ellipse` | `draw_ellipse`, `drawEllipse` | `drawEllipse` | `x`, `y`，以及 **`rw`+`rh`** 或 **`w`+`h`** 表示两轴半轴 | 中心 + 半轴；两轴均 `>0` 才绘制（实现侧约定） |
| `ellipse_fill` | `fill_ellipse`, `fillEllipse` | `fillEllipse` | 同上 | 实心椭圆 |
| `triangle` | `draw_triangle`, `drawTriangle` | `drawTriangle` | 三顶点坐标 | 见 §6.2 |
| `triangle_fill` | `fill_triangle`, `fillTriangle` | `fillTriangle` | 同上 | 实心三角形 |
| `round_rect` | `fill_round_rect`, `fillRoundRect` | `fillRoundRect` | `x`, `y`, `w`, `h`，圆角 **`radius` 或 `r`** | 圆角实心矩形；建议 `w,h,r > 0` |
| `round_rect_outline` | `draw_round_rect`, `drawRoundRect` | `drawRoundRect` | 同上 | 圆角矩形描边 |
| `rotated_rect_outline` | `draw_rotated_rect`, `drawRotatedRect` | `drawRotatedRect` | `x`, `y`, `w`, `h`, `angle` | **`x`,`y` 为矩形中心**；度；建议 `w,h > 0` |
| `rotated_rect_fill` | `fill_rotated_rect`, `fillRotatedRect` | `fillRotatedRect` | 同上 | 绕中心旋转的实心矩形 |
| `text` | — | 文本 | `x`, `y`, `text`, `c`, `size` | UTF-8 字符串；`c` 为调色板索引；`size` 为字号档位（固件定义）；锚点须与固件约定一致（建议左上角） |

**`text` 示例**（装饰层 `extra` 中常见）：

```json
{ "shape": "text", "x": 0, "y": 56, "text": "?", "c": 1, "size": 1 }
```

口播路径下若需随音素 offset 平移 `text`，服务端应对 `x`/`y` 与几何图元同样加上 `(dx, dy)`。

**别名匹配建议**：比较前将 `shape` 转 **小写**，再查别名表；未命中则视为主名原样。

### 6.2 三角形顶点字段（两种等价写法）

**写法 A（推荐）**

```json
{ "shape": "triangle", "x0": 10, "y0": 50, "x1": 30, "y1": 20, "x2": 50, "y2": 55 }
```

**写法 B（第一点用 `x`,`y`）**

```json
{ "shape": "triangle", "x": 10, "y": 50, "x1": 30, "y1": 20, "x2": 50, "y2": 55 }
```

`triangle_fill` 字段相同。解析时二选一，**不得**与 `rect`（含 `w`,`h`）混淆：三角形 **无** `w`/`h`。

### 6.3 椭圆半轴字段

以下 **两组二选一**（同图元内不要混用语义冲突的组合）：

- `rw`, `rh`：水平 / 垂直 **半轴**长度（像素）。
- 或使用 `w`, `h` 表示两轴半轴（与 `rect` 的宽高含义不同，**文档与工具须标明上下文为 ellipse**）。

中心均为 `(x, y)`。

---

## 7. 音素 `offset` 应用规则（服务端与仿真）

对 **鼻、左眼、右眼** 的图元列表，在写入 `anim.elements` 前，对 **所有「位置类」坐标** 加上 `(dx, dy)`：

| 主 `shape` | 平移字段 |
|------------|----------|
| `rect`, `rect_outline`, `round_rect`, `round_rect_outline` | `x`, `y`（左上角） |
| `circle`, `circle_outline` | `x`, `y`（圆心） |
| `line` | `x1`, `y1`, `x2`, `y2` |
| `pixel` | `x`, `y` |
| `hline`, `vline` | `x`, `y`（起点） |
| `ellipse`, `ellipse_fill` | `x`, `y`（中心） |
| `triangle`, `triangle_fill` | 三顶点：写法 A 则 `x0,y0,x1,y1,x2,y2`；写法 B 则 `x,y,x1,y1,x2,y2` |
| `rotated_rect_outline`, `rotated_rect_fill` | `x`, `y`（中心）；**不修改** `angle` |
| `text` | `x`, `y`（锚点） |

**嘴部 `mouth` 图元**：**不**应用 `offset`。

实现参考：`deskbot_server/pb/anim_defaults.py` → `apply_offset_to_primitives`。

---

## 8. 版本与扩展

| 项目 | 建议 |
|------|------|
| 配置内可选 | `"schema_version": 1` 顶层字段，便于以后 breaking 升级 |
| 固件 | 遇未知 `shape` 跳过；遇未知 `metadata` 键忽略 |
| pb 线协议 | 遵循 `pb_ver` / [esp32_playback_protocol.md](./esp32_playback_protocol.md) |

---

## 9. 代码索引（本仓库）

| 模块 | 路径 |
|------|------|
| 图元 offset、脸包合并、热加载 | `src/deskbot_server/pb/anim_defaults.py` |
| 音素分片 → 动画行 | `src/deskbot_server/pb/anim_defaults.py` |
| pb 组帧与下发 | `src/deskbot_server/pipeline/flow.py` |
| 传输与 `anim.elements` 容器 | [esp32_playback_protocol.md](./esp32_playback_protocol.md) |

---

## 10. 编写与校验 JSON

| 用途 | 路径 / 配置 |
|------|-------------|
| 主脸包 | `data/pb_face_bundle_demo.json` ← `tts.pb_face_bundle_json` 或 `DESKBOT_PB_FACE_BUNDLE_JSON` |
| 覆盖层 | `data/pb_face_bundle_demo_overlay.yaml` ← `tts.pb_face_bundle_file` 或 `DESKBOT_PB_FACE_BUNDLE_FILE` |
| 预置场景 | `data/pb_scenes_idle_sleep_guard.json`（调试页 `/api/device_pb_scenes`） |

- 保存 JSON 后按文件 **mtime 热重载**，无需重启进程。
- 语法校验：`python3 -m json.tool deskbot-server/data/pb_face_bundle_demo.json`
- 以下划线开头的键（如 `_comment`）仅供人类阅读，服务端忽略。
- 顶层结构与字段含义见本文 §2；图元字段见 §6。

---

*修改 shape 表或组帧逻辑时，请同步更新本文与 `apply_offset_to_primitives`。*
