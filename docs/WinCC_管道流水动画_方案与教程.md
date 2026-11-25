# WinCC 画面管道流水动画 · 方案与教程

> 场景：WinCC V8.1 画面里用 3D 控件加载 `demo.glb`，要让管道"看起来有介质在流"。
> 本文给 4 条路，其中 **方案 A 的脚本已用 cscript 桩实测跑通**、**方案 C 已在自建网页里实现并截图验证**。

---

## 0. 先看结论

| 方案 | 载体 | 视觉颗粒度 | 要改模型吗 | 每帧开销 | 部署难度 | 建议 |
|---|---|---|---|---|---|---|
| **A. 逐段高亮推进** | 原生 3D 控件 | 按管道分段（本项 0.1~14 m） | 否（管道已拆好） | 2~6 次 COM 调用 | ★☆☆ | **先用这个** |
| **B. 移动光条** | 原生 3D 控件 | 连续 | 是（生成光条对象） | 2~6 次 COM 调用 | ★★☆ | 想要更顺滑再做 |
| **C. 光点巡游** | 自建网页 | 连续平滑 | 否 | 十几个 draw call | ★☆☆ | **已实现，见 §4** |
| **D. UV 流动贴图** | 自建网页 | 最像水 | 需给管道 UV | 1 个材质 | ★★☆ | 最终形态 |

**核心约束**：WinCC 原生 3D 控件**不支持播放 glTF 内嵌动画**（手册 62 页 0 处 "动画/Animation"），也不支持纹理坐标动画。它能做的只有四件事：

```
SetObjectMaterialColorExt   改颜色
SetObjectPositionExt        改位置
SetObjectScaleExt           改缩放
SetObjectRotationExt        改旋转
```

所以原生控件里的"流水"只有两条实现路径：**变色**（方案 A）或**移动**（方案 B）。

---

## 1. 方案 A：逐段高亮推进（推荐先用）

### 1.1 前提：管道已经拆成独立段

本项目 `Line107.001` 在 Blender 里已按拐弯拆成 **36 段**（`Demo_Pipe_seg01` ~ `seg36`），
用 36 个对象的中心点串成 **4 条管链**：

| 链 | 段数 | 总长 | 首段 | 尾段 |
|---|---|---|---|---|
| 1 | 30 | 133.18 m | `Demo_Pipe_seg14` | `Demo_Pipe_seg03` |
| 2 | 2 | 5.00 m | `Demo_Pipe_seg26` | `Demo_Pipe_seg27` |
| 3 | 2 | 1.82 m | `Demo_Pipe_seg33` | `Demo_Pipe_seg32` |
| 4 | 2 | 1.82 m | `Demo_Pipe_seg35` | `Demo_Pipe_seg34` |

> 管道没拆段也能做，只是"水头"会一次跳一整根管子，效果差。拆段方法见 §7。

### 1.2 部署：4 步

1. 图形编辑器 → 菜单 **全局脚本** → **VBS 编辑器**
2. 左侧「项目模块」下 **新建动作**（Action）
3. 编辑器里 `Ctrl+A` 全选 → 删除 → **粘贴 `WinCC_管道流水动画.vbs` 全文**
4. 给这个动作加触发器：**周期触发 250 毫秒** → 编译 → 激活

> ⚠️ WinCC 的动作外壳已经是 `Function action ... End Function`，
> 所以脚本里**不能**再写 `Function xxx`（VBScript 不允许函数里套函数），也不能有 `Sub Main`。
> 本文提供的脚本是**平铺写法的过程体**，直接粘就行。

### 1.3 参数（脚本顶部 6 行，改完重编译）

```vb
Const CTRL_NAME  = "cwc1"                 ' 3D 控件对象名
Const SPEED_MS   = 8                       ' 流速（米/秒）
Const TAIL_M     = 1.2                     ' 光带长度（米）
Const TICK_MS    = 250                     ' 触发周期，必须与触发器一致
Const COLOR_RUN  = "rgb(0,255,220)"        ' 流动颜色
Const RUNNING    = True                    ' False = 停流
```

### 1.4 它怎么算的

每一帧：

```
光带头部 head = (Timer × 流速) mod 链总长        ← 沿链前进
某段末端 s1 落在 [head-step, head)  → 该段刚被水头越过 → 点亮
某段末端 s1 落在 [head-step-TAIL, head-TAIL) → 该段刚离开光带尾 → 恢复原色
```

**关键设计：只对"状态发生变化的段"发命令**，所以每帧只有 **2~6 次** `SetObjectMaterialColorExt`，
而不是 36 次。这是它能在软件渲染的 3D 控件上跑而不卡的原因。

### 1.5 实测（cscript 桩）

用假控件记录调用次数，跑 5 帧（每帧间隔 400 ms）：

```
第 1 次: 调用=2  点亮=1  熄灭=1
第 2 次: 调用=4  点亮=2  熄灭=2
第 3 次: 调用=3  点亮=2  熄灭=1
第 4 次: 调用=1  点亮=0  熄灭=1
第 5 次: 调用=4  点亮=1  熄灭=3
```

调用明细确认是**逐段接力**（`seg26 → seg27 → seg01 → seg02 …`），不是整片刷新。

### 1.6 可调花样

- **多管道不同速**：把 `SPEED_MS` 改成 `SPEEDS(i)` 数组，每条链一个速度
- **按流量调速**：`Const` 改 `Dim`，然后
  ```vb
  SPEED_MS = HMIRuntime.Tags("Demo_XS1LL1_PJ1").Read * 0.6
  ```
- **按电机状态开关**：`RUNNING = (HMIRuntime.Tags("Demo_AMDL1_PJ1").Read > 0.1)`
- **两种颜色交替**（模拟脉冲）：`COLOR_RUN` 按 `Int(head) Mod 2` 切

---

## 2. 方案 B：移动光条（更顺滑）

思路：在管道上放一根**很短的亮条**（0.8~1.5 m 的细圆柱），每帧用 `SetObjectPositionExt` 沿链方向平移。

### 2.1 模型侧要做的

对每条链，复制一段管道几何 → 缩短到 1 m → 换成发光材质 → 命名 `flow01_a`、`flow01_b`（同一链放 2~3 根错开）。

Blender 里可以脚本化：

```python
# 伪代码
for chain in chains:
    for k in range(3):
        seg = chain[k % len(chain)]          # 借用某段的截面
        new = duplicate(seg); scale_length(new, 1.0)
        new.name = f"flow{chain.id:02d}_{chr(97+k)}"
        new.data.materials.clear()
        new.data.materials.append(glow_mat)  # 自发光材质
```

### 2.2 脚本侧

```vb
' 每帧把每根光条推到链上对应位置
pos = (headK + k * 间距) Mod 链总长
找到 pos 落在哪一段 → 取该段方向 → 沿方向偏移
ctl.SetObjectPositionExt "flow01_a", x, y, z
```

> 注意：原生控件的 `SetObjectPositionExt` 是**绝对坐标**（对象局部），
> 所以要么在建模时把光条原点放在管道起点，要么每帧现算。

### 2.3 和方案 A 比

| | 方案 A | 方案 B |
|---|---|---|
| 视觉 | 一段一段跳 | 连续滑动 |
| 改模型 | 不用 | 要做光条 |
| 调参 | 改颜色/速度 | 改速度 + 光条长度 |
| 风险 | 极低 | 光条要贴住管道，否则会"飘" |

---

## 3. 方案 C：网页端光点巡游（已实现）

自建页面（`C:\FillViewer\index.html`）里已经加好，效果见 `preview_flow.png`：青色光点沿管道跑。

### 3.1 数据从哪来

`_pipe_runs.json` 是从 GLB 里**直接算出来的**（不用手工量）：

```json
{
  "segs": [ { "name": "Demo_Pipe_seg01", "c": [6.210, 4.737, -6.403], "axis": "Y", "len": 5.205 }, ... ],
  "runs_ordered": [ { "total": 133.18,
                      "segs": [ {"name":"Demo_Pipe_seg14","s0":0,"s1":4.903}, ... ] } ]
}
```

### 3.2 代码（约 45 行）

```js
function buildFlow (data) {
  FLOW.group = new THREE.Group(); scene.add(FLOW.group);
  const geo = new THREE.SphereGeometry(0.28, 10, 8);
  const mat = new THREE.MeshBasicMaterial({
    color: 0x18ffd8, transparent: true, opacity: 0.95,
    depthTest: false, depthWrite: false      // 关键：让光点永远浮在管道上方
  });

  data.runs_ordered.forEach(run => {
    // 把每段中心串成一条曲线
    const pts = run.segs.map(s => {
      const g = data.segs.find(x => x.name === s.name);
      return new THREE.Vector3(g.c[0], g.c[1], g.c[2]);
    });
    const curve = new THREE.CatmullRomCurve3(pts, false, 'catmullrom', 0.0);
    const len = curve.getLength();
    const n = Math.max(2, Math.round(len / 14));   // 每 14 m 一个光点

    for (let k = 0; k < n; k++) {
      const m = new THREE.Mesh(geo, mat);
      m.userData = { curve, len, off: k / n };
      m.renderOrder = 999;
      FLOW.group.add(m);
    }
  });
}

function tickFlow () {
  const t = performance.now() * 0.001, V = 8;      // 8 m/s
  FLOW.beads.forEach(b => {
    const u = ((b.userData.off + t * V / b.userData.len) % 1 + 1) % 1;
    b.userData.curve.getPointAt(u, b.position);
  });
}
```

主循环里：

```js
if (FLOW.on) { tickFlow(); needRender = true; }   // 只在开启时重绘（保留脏渲染）
```

### 3.3 两个坑

**① 光点被管道挡住看不见** → 必须 `depthTest: false` + `renderOrder = 999`  
（第一次做就是这个原因，光点在管腔里，全被管壁遮住）

**② 关掉流水后场景不能停摆** → `tickFlow` 只在 `FLOW.on` 为真时调用、
且只在开启时把 `needRender` 置真；关闭时立刻回到"静止 0 重绘"。

### 3.4 开关

- 页面上点 **流水** 按钮
- 或在 URL 加 `?flow=1` 自动开
- 代码里 `window.viewer.flow(true/false)`

---

## 4. 方案 D：UV 流动贴图（最终形态）

想要"真正像水"，得让**管道表面本身的纹理流动**。

### 4.1 前提

管道的 UV 必须**沿管长方向均匀展开**（Blender 里 `U` 沿轴向、`V` 绕圆周）。
本项目现有管段是 10 边形低模，UV 不可靠 → 需要重新展 UV（Blender：`Smart UV Project` 或沿路径 `Follow Active Quads`）。

### 4.2 three.js 里最简单的做法（滚动贴图）

```js
// 用一张黑白渐变的 CanvasTexture 作为 map
tex.wrapS = THREE.RepeatWrapping;

function tick () {
  tex.offset.x -= dt * 0.8;      // 沿 U 方向滚动
  renderer.render(scene, camera);
}
```

### 4.3 不需要贴图的 shader 版（更轻）

在 `onBeforeCompile` 里注入一段：按 UV 和时间的相位做斜条纹，

```glsl
float s = fract(vUv.x * 6.0 - uTime * 1.5);
float band = smoothstep(0.0, 0.15, s) * smoothstep(0.5, 0.35, s);
gl_FragColor.rgb = mix(gl_FragColor.rgb, vec3(0.1, 1.0, 0.85), band * 0.9);
```

优点：零贴图、零额外 draw call、可随流量调速改 `uTime` 系数。

### 4.4 代价

- 要重展 UV（一次性工作，但管道有 36 段）
- 换材质（会覆盖原有棕色管色，需要混色）

---

## 5. 数据驱动：让流水"有依据"

| 想表达 | 读哪个变量 | 怎么用 |
|---|---|---|
| 有流量才流 | `Demo_XS1LL1_PJ1`（1#底流泵管路流量） | `RUNNING = (值 > 0.5)` |
| 流量大流得快 | 同上 | `SPEED = 值 × 0.6` |
| 泵转才流 | `Demo_ZB1DL1_PJ1`（1#底流泵电流） | `RUNNING = (电流 > 0.1)` |
| 多组分色 | 不同链用不同颜色 | `COLOR` 数组 |

**方案 C（网页）**里直接读 `/api/live` 的 `values` 即可，页面每 500ms 已经在轮询。

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 编译报"语法错误 在文本行中 N" | 动作里嵌套了 `Function` | 删掉所有 `Function ... End Function`，只留过程体 |
| 一段都不亮 | 对象名对不上 | 确认 GLB 里确实是 `Demo_Pipe_seg01…`（用页面点设备看名字） |
| 只亮不灭 | 材质颜色被激活后没还原 | 用 `SetObjectMaterialColorExt name, False, "", 1, 1.0, 0` |
| 流水一顿一顿 | 触发周期太长 | 250 ms 起步，最快 150 ms（再快运行系统会忙） |
| 运行系统 CPU 高 | 每秒几十次重绘 | 拉长触发周期 / 只对一条主管道开流水 |
| 光点看不见（网页） | 被管道挡住 | `depthTest:false` + `renderOrder=999` |
| 光点位置全挤在原点 | 模型没加载完就建光点 | 在 GLTF `onLoad` 回调里建 |

---

## 7. 附：把整根管道拆成段（方案 A 的前置）

如果某根管道还是一整块（比如 `Line085`），要先拆：

1. Blender 里选中对象 → `Tab` 编辑模式 → `P` → **按松散块分离**（拆不连通的块）
2. 想按**拐弯**拆：选中拐角处一圈边 → `Ctrl+E` → **边断开**（Edge Split）→ 再按松散块分离
3. 导出 GLB 时确认 `export_apply=False`、对象名会被带进 GLB

本项目 `Line107.001` 已按拐弯拆成 36 段，`Line100` 拆成了 `p1/p2/p3`。

---

## 8. 交付文件

| 文件 | 说明 |
|---|---|
| [`WinCC_管道流水动画.vbs`](C:\FillViewer\WinCC_管道流水动画.vbs) | **方案 A 成品脚本**，直接粘进 WinCC 全局脚本动作 |
| [`_pipe_runs.json`](C:\FillViewer\data\_pipe_runs.json) | 36 段管道的中心/长轴/长度 + 4 条链的弧长排序 |
| [`_pipe_runs.py`](C:\FillViewer\_pipe_runs.py) | 从 GLB 自动算出上面的 JSON（管道改了重跑一次） |
| [`preview_flow.png`](C:\FillViewer\preview_flow.png) | 方案 C 的效果截图 |
| [`index.html`](C:\FillViewer\index.html) | 方案 C 的完整实现（`buildFlow` / `tickFlow`） |
| [`_test_flow.vbs`](C:\FillViewer\_test_flow.vbs) | 方案 A 的 cscript 桩测试（验证调用次数） |
