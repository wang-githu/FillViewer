# WinCC V8.1 REST 服务使用教程（v2 · 全部接口实测）

> 适用：WinCC V8.1（Upd7 实测）。目标读者：要把 WinCC 实时数据接到网页/第三方系统的人。
> 本文所有"实测"都是在现场这台 WinCC 站上真实跑过的结果，含失败样例，可直接照抄命令验证。
> 与 11 月 16 日那版相比，**第 6、7 章是重大更正**（批量读、批量写其实都能用，之前判定"不可用"是编码踩坑导致的误判）。

---

## 0. 这套东西能做什么

WinCC 自带一个 **只读/可写的过程数据 HTTP 接口**，跑在运行系统里，端口默认 **8092**，协议 **HTTPS**，认证 **HTTP Basic**。

| 能做 | 不能做 |
| --- | --- |
| 读实时值（单个 / 批量） | 订阅推送（没有 WebSocket，只能轮询） |
| 写实时值（单个 / 批量） | 访问未开 REST 授权的变量 |
| 读变量组态（名字、类型、注释、canRead/canWrite） | 绕过 WinCC 用户权限 |
| 读过程值归档的历史曲线 | 读"原始数据变量""文本参考"两类变量 |
| 读报警消息 | 用 `OR`/`IN` 组合过滤（只支持 AND） |

典型用法：网页 HMI、报表定时抓数、把数据转给 MES/看板、给现场大屏做 3D 可视化。

---

## 1. 前置条件（先确认这 6 条，能省掉一半排错）

1. **WinCC 版本 ≥ V8.1**，且已激活运行系统。
2. **许可证**：REST 属于"连通性"功能，缺许可证会返回 **HTTP 402**。本项目用 **WinCC/Connectivity Pack** 或 WinCC Unified/RT 相应授权。
3. **端口 8092 未被占用**；跨机访问要放行防火墙。
4. **客户端必须用主机名**（`https://DESKTOP-XXXX:8092/...`），**不要用 IP**——证书绑的是主机名，用 IP 会证书校验失败（或必须关校验）。
5. 客户端要能访问该主机名（同一台机器没问题；跨机需要 DNS 或 hosts）。
6. **每个变量都要在变量属性里勾 REST 读/写授权**（见第 4 章），不勾就读不到、写不进——这是最常见的"接口通了但变量 403"。

---

## 2. 开启 REST 服务

### 2.1 在 WinCC 项目里勾选

WinCC 项目管理器 → 计算机 → 属性 → **启动** 选项卡 → 勾选 **"REST 服务"** → 确定 → **重新激活运行系统**（只改不重启无效）。

### 2.2 确认端口与主机名

端口默认 8092，写在注册表里（一般不用改）：

```
HKEY_LOCAL_MACHINE\SOFTWARE\Siemens\WinCC\Setup
```

如需改端口，改完必须重启运行系统。

### 2.3 确认服务进程

```powershell
Get-Service CCRestService | Format-List Name,DisplayName,Status,StartType
# Status    : Running      ← 运行系统激活后应为 Running
# StartType : Manual       ← 由运行系统拉起，不要改成"自动"
```

### 2.4 确认真的在监听

```powershell
netstat -ano | findstr :8092
# TCP 0.0.0.0:8092 ... LISTENING

Get-NetTCPConnection -LocalPort 8092 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess
```

### 2.5 证书

```powershell
netsh http show sslcert | Select-String -Pattern "IP:port|Certificate Hash|Application ID" -Context 0,0
# IP:port          : 0.0.0.0:8092
# Certificate Hash : <hash>
```

自签证书客户端会报警；测试时可以关校验（见附录 A 的 `ssl` 设置），生产建议导入该证书到受信任根。

### 2.6 跨机访问：放行防火墙

```powershell
New-NetFirewallRule -DisplayName "WinCC REST 8092" -Direction Inbound -Protocol TCP -LocalPort 8092 -Action Allow
```

---

## 3. 认证

**HTTP Basic**，用 WinCC 用户管理里的用户名/密码。没有"匿名访问"。

```bash
# curl
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD "https://YOUR-WINCC-HOST:8092/WinCCRestService"
```

```powershell
# PowerShell（注意别把密码写进脚本历史，可改用 Get-Credential）
$u="YOUR-WINCC-USER"; $p="YOUR-WINCC-PASSWORD"
$b = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${u}:${p}"))
Invoke-RestMethod -Uri "https://YOUR-WINCC-HOST:8092/WinCCRestService" `
  -Headers @{ Authorization = "Basic $b" } -SkipCertificateCheck
```

```python
# Python
import base64, urllib.request, ssl
AUTH = "Basic " + base64.b64encode(b"YOUR-WINCC-USER:YOUR-WINCC-PASSWORD").decode()
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request("https://YOUR-WINCC-HOST:8092/WinCCRestService",
                             headers={"Authorization": AUTH})
print(urllib.request.urlopen(req, timeout=15, context=ctx).read().decode())
```

**用户权限**由 WinCC 用户管理决定；权限不足直接 **401**。

---

## 4. 变量权限（最容易卡住的地方）

### 4.1 两个概念别混

| | 是什么 | 表现 |
| --- | --- | --- |
| **用户授权** | 这个 WinCC 用户能不能登录 | 不行 → `401` |
| **变量 REST 授权** | 这个变量允不允许被 REST 读写 | 不行 → 该变量读不到 / 写失败，页面显示 403 |

### 4.2 以元数据为准（最可靠）

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD \
 "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagManagement/Variable/我的变量"
```

```json
{"variableName":"我的变量","dataType":2,"comment":"溢流池液位",
 "canRead":true,"canWrite":false, ...}
```

`canRead=false` ⇒ 读会产生错误；`canWrite=false` ⇒ 写会被拒。批量读里表现为该项 `{"error":"Not Found"}` 或整条 403。

### 4.3 怎么打开

变量管理器 → 选中变量 → 属性 → **REST API** 分组 → 勾选读/写授权 → 编译 → 激活运行系统。

---

## 5. 接口总览

### 5.1 四族根路径

| 族 | URL 前缀 | 用途 |
| --- | --- | --- |
| 变量 | `/WinCCRestService/tagManagement/` | 实时值、组态 |
| 过程值归档 | `/WinCCRestService/tagLogging/` | 历史曲线 |
| 报警 | `/WinCCRestService/alarmLogging/` | 报警消息 |
| 根 | `/WinCCRestService` | 服务信息（描述/版本/项目名/主机名） |

根信息实测：

```json
{"description":"SIMATIC WinCC SCADA",
 "possibleCollections":["namespaces","tagManagement","alarmLogging","tagLogging"],
 "winccVersion":"V8.1 Upd7","projectName":"YourWinCCProject1131","serverName":"YOUR-WINCC-HOST"}
```

### 5.2 常用端点

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `tagManagement/Value/<变量名>` | 读一个实时值 |
| **POST** | `tagManagement/Values` | **批量读**（body `{"variableNames":[..]}`） |
| GET | `tagManagement/Variable/<变量名>` | 读一个变量的组态（**本版实测路径**，见 5.4） |
| GET | `tagManagement/Variables?itemLimit=200&continuationPoint=1` | 列出变量组态（分页） |
| PUT | `tagManagement/Value/<变量名>` | 写一个（body `{"value":"12.5"}`） |
| **PUT** | `tagManagement/Values` | **批量写**（body `[{"variableName":"x","value":"1"},…]`） |
| GET | `tagLogging/Archives` | 列出归档 |
| GET | `tagLogging/Variable/<变量名>` | 查某变量是否被归档 |
| POST | `tagLogging/Values` | 取历史值 |
| GET | `alarmLogging/Message/<编号>` / `alarmLogging/Messages` | 报警 |

### 5.3 三条通用规则

1. **HTTP 方法语义**：GET 读；PUT 写单个；POST 用来"寻址 body 里的资源"（批量读、取历史）。
2. **路径里的 `[ ]` 必须编码** 成 `%5B %5D`（变量名带数组下标时，如 `MyTag.%5BNewElement%5D`）。
3. **分页**：列表类接口用 `itemLimit`（默认最多 1000）+ `continuationPoint`；过滤器只支持 AND。

其他限制（官方手册明确写出）：

- 不支持 **原始数据变量**、**文本参考**两种数据类型。
- 语言用请求头 `Content-Language` / `Accept-Language` 指定，缺省 `en-US`。
- 错误码：`400` 请求无效、`401` 无权限、`402` 无许可证、`404` URL 与资源类型不匹配、`416` URL 结构不对、`423` 资源被锁（正在读取）、`500` 内部错误、`501` 未实现。

### 5.4 【更正】官方手册写的 `tag/<变量名>` 在本版是 416

这是本次实测最有价值的一条经验：**官方 PDF 第 3 章端点表里的 `tagManagement/tag/<TagName>` 和 `tagManagement/tags`，在本机 V8.1 Upd7 上直接返回 HTTP 416**（URL 结构不对），不是权限问题。

但 416 的返回体本身就把**合法资源全列出来了**——这是"接口自述"，比手册可靠：

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagManagement/tag/teat"
```

```json
{"description":"Collection to access the WinCC Tagmanangement",
 "possibleCollections":[
   {"path":"Value",             "description":"Get the online value of a WinCC variable. Item is the name of the variable"},
   {"path":"Values",            "description":"Get online values of multiple WinCC variables. A body is needed …"},
   {"path":"Variable",          "description":"Get the configuration of a WinCC variable. Item is the name of the variable."},
   {"path":"Variables",         "description":"Get the configuration of all WinCC variables. … itemLimit … continuationPoint"},
   {"path":"Connection",        "description":"…"},
   {"path":"Connections",       "description":"…"},
   {"path":"Group",             "description":"…"},
   {"path":"Groups",            "description":"…"},
   {"path":"StructureType",     "description":"…"},
   {"path":"StructureTypes",    "description":"…"},
   {"path":"StructureVariable", "description":"…"},
   {"path":"StructureVariables","description":"…"}]}
```

对照实测（2026-11-23，`YourWinCCProject1131`）：

| 路径 | 结果 |
| --- | --- |
| `GET tagManagement/tag/teat` | **416** |
| `GET tagManagement/tags` | **416** |
| `GET tagManagement/Variable/teat` | **200** ✅ 变量组态 |
| `GET tagManagement/variables`（小写） | **200** ✅ 变量列表 |
| `GET tagManagement/Variables?itemLimit=200&continuationPoint=1` | **200** ✅ 分页列表 |
| `GET tagManagement/Variables/teat` | **404**（`Variables` 是集合，不接受 Item） |

`GET tagManagement/Variable/<变量名>` 实测返回：

```json
{"variableName":"teat","dataType":2,"comment":"",
 "canRead":true,"canWrite":true,"lastChange":"2026-11-20 01:45:43.000","namespace":""}
```

> 用途：**先查 `canRead` / `canWrite`**，能提前判掉 403/401，不用盲写。
> 通用技巧：任何路径打不通时，先看返回体里有没有 `possibleCollections` —— 有，就照它列出的路径改。


---

## 6. 读变量值（实测）

### 6.1 读单个

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD \
 "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagManagement/Value/GS1%E6%95%B0%E6%8D%AE_YL1YW1_PJ1"
```

```json
{"variableName":"Demo_YL1YW1_PJ1","dataType":2,"value":"0",
 "timestamp":"2026-11-16T03:25:30.550Z","qualitycode":128,"errorcode":0,"namespace":""}
```

要点：

- **`value` 永远是字符串**，要自己转数字/布尔（`"true"`/`"false"`）。
- **`timestamp` 是 UTC**，显示要 +8。
- 中文变量名要 URL 编码：Python `urllib.parse.quote(name, safe="")`。

### 6.2 批量读：【更正】可用，且是官方推荐做法

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD -X POST \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{"variableNames":["teat","模型名称","Demo_ZB1_YX1","不存在的变量XYZ"]}' \
  "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagManagement/Values"
```

实测 **HTTP 200**，返回数组：

```json
[
 {"variableName":"teat","dataType":2,"value":"0","timestamp":"2026-11-23T02:12:24.150Z","qualitycode":76,"errorcode":0,"namespace":""},
 {"variableName":"模型名称","dataType":5,"value":"","timestamp":"…","qualitycode":76,"errorcode":0,"namespace":""},
 {"variableName":"Demo_ZB1_YX1","dataType":1,"value":"false","timestamp":"…","qualitycode":8,"errorcode":0,"namespace":""},
 {"variableName":"不存在的变量XYZ","error":"Not Found","namespace":""}
]
```

- 不存在的变量**不会让整个请求失败**，只是那一项变成 `{"error":"Not Found"}`。
- 性能：4 个变量 **14–16 ms** 往返（一次网络请求搞定）。
- **只能用 POST**：用 GET 形式（把变量名放查询串）会返回 **400**，并明确提示 `"A body is needed which contains the names of the variables as array."`

> **为什么上一版说它 500 不可用？**
> 因为当时用 Python `requests` 的 `json=` 参数，默认 `ensure_ascii=True` 把中文变量名转成 `HC1\u6570\u636e_SN1_YX1`，WinCC 只按字面量匹配 ⇒ 全部 `Not Found`，叠加当时的错误处理就表现成 500。
> **正确做法：body 必须是 UTF-8 原文中文**（`json.dumps(..., ensure_ascii=False).encode("utf-8")`，并带 `charset=utf-8`）。这一条对读写、历史全适用。

### 6.3 列出所有变量（分页）

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD \
 "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagManagement/Variables?itemLimit=200&continuationPoint=1"
```

```json
{"continuationPoint":201,"moreAvailable":true,
 "variables":[{"variableName":"@SCRIPT_COUNT_TAGS","dataType":2,"comment":"",
               "canRead":false,"canWrite":false,"lastChange":"2026-08-01 15:02:48.000"}]}
```

本机共约 **4252–4490 个变量**，200/页 ≈ 23 页。

### 6.4 质量码（实测）

| 值 | 含义 | 显示建议 |
| --- | --- | --- |
| `128`（0x80）/ `192`（0xC0）/ `0` | 好 | 绿 |
| `76`（0x4C） | **uncertain / 初始值**（刚启动、还没被写过） | 灰/黄 |
| `8`（0x08） | 坏（常见：PLC 未连接） | 红 |

> 排查口诀：**"值全是 0 或空" 先看 `qualitycode`**。质量 8 = 数据源没连上，不是程序读错；质量 76 = 变量还没产生过值。

---

## 7. 写变量值（实测）

### 7.1 写单个

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD -X PUT \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{"value":"12.5"}' \
  "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagManagement/Value/%E6%88%91%E7%9A%84%E5%8F%98%E9%87%8F"
```

```json
{"variableName":"我的变量","errorcode":0,"namespace":""}
```

`errorcode=0` 即成功。POST 与 PUT 对单值都可（官方"HTTP 方法"里有说明），推荐 **PUT**。

### 7.2 批量写：【更正】可用，方法是 PUT + 数组

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD -X PUT \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '[{"variableName":"teat","value":"7"}]' \
  "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagManagement/Values"
```

```json
[{"variableName":"teat","errorcode":0,"namespace":""}]
```

实测读回 `value="7"` ✅。

**实测过的写法对照（别踩）**

| 写法 | 结果 |
| --- | --- |
| `PUT tagManagement/Values` + `[{"variableName":…,"value":…}]` | ✅ 200，写入成功 |
| `PUT tagManagement/Value/<名>` + `{"value":…}` | ✅ 200（单写） |
| `POST tagManagement/Values` + 数组 | ❌ 500（POST 不走批量写） |
| `PUT tagManagement/Values` + `{"values":[…]}` | ❌ 500 |
| `POST tagManagement/Values` + `{"variables":[…]}` | ❌ 500 |
| `POST tagManagement/Values` + `{"variableNames":[…],"values":[…]}` | ⚠️ 200 且**确实会写**，但属未文档化用法，别依赖 |

### 7.3 写失败对照（实测）

| 场景 | 返回 |
| --- | --- |
| 写负数到无符号变量 | `errorcode -1073446904`（0xC0048008，值非法/类型不符） |
| PLC 断开时写 | `errorcode -1073446906`（0xC0048006） |
| 写 `canWrite=false` 的变量 | 拒绝（错误码或 403） |
| 变量名不存在 | 该项 `{"error":"Not Found"}` |

### 7.4 安全提醒

写到真实过程点会**立刻作用到 PLC**。测试阶段：

1. 先建一个内部变量（如 `teat`）并开写权限，用它验证链路；
2. 上真机前"读 → 写同值"验证，再改工艺点；
3. 页面上的写操作一定要加白名单（只允许勾选过的变量可写）。

---

## 8. 历史曲线（tagLogging）

### 8.1 前提：变量必须被归档

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD \
 "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagLogging/Variable/Demo_YL1YW1_PJ1"
```

```json
{"variableName":"Demo_YL1YW1_PJ1","archiveName":"供水系统","dataType":"analog",
 "comment":"溢流池液位","acquisitionCycle":"500 ms","archivingCycle":"500 ms","canRead":true}
```

没归档的变量在这里查不到，`tagLogging/Values` 也会返回 `Not Found`。

### 8.2 取历史值

```json
{"archives":[{"name":"供水系统",
  "variables":[{"name":"Demo_YL1YW1_PJ1","range":3600,"maxValues":600}]}]}
```

| 字段 | 说明 |
| --- | --- |
| `archives[].name` | **归档名**（不是变量名），取自 8.1 的 `archiveName` |
| `variables[].name` | 归档变量名 |
| `range` | 最近多少秒（与 `timeFrom`/`timeTo` 二选一） |
| `timeFrom` / `timeTo` | ISO 时间，如 `2026-11-16T03:00:00.000Z` |
| `maxValues` | 最多返回点数（服务端会抽稀） |

```bash
curl -k -u YOUR-WINCC-USER:YOUR-WINCC-PASSWORD -X POST \
 -H "Content-Type: application/json; charset=utf-8" \
 --data-binary '{"archives":[{"name":"供水系统","variables":[{"name":"Demo_YL1YW1_PJ1","range":3600,"maxValues":600}]}]}' \
 "https://YOUR-WINCC-HOST:8092/WinCCRestService/tagLogging/Values"
```

### 8.3 返回结构

```json
[{"archive":"供水系统","variableName":"Demo_YL1YW1_PJ1","unit":"","errorcode":0,
  "values":[{"value":"0","timestamp":"2026-11-16T03:25:30.550Z","qualitycode":128}]}]
```

按时间升序，间隔 = 归档周期。画图前把 `value` 转 Number、`timestamp` 转 Date。

---

## 9. 浏览器直连的 CORS 问题

WinCC REST **不返回 CORS 头**，浏览器里 `fetch` 跨源会被拦（页面在 8099、接口在 8092 ⇒ 不同源）。

三种解法：

1. **走后端代理**（推荐，本项目就是这么做的）：浏览器只跟 `http://127.0.0.1:8099/api/*` 说话，由本机 Python 转发到 8092。
2. 页面和接口同源（把页面也放到 8092 上，不现实）。
3. 启动浏览器时关同源检查（**只用于调试**）：`--disable-web-security --user-data-dir=...`。

代理的关键几行：

```python
target = REST_BASE + path
req = urllib.request.Request(target, data=body, headers={
    "Authorization": AUTH, "Content-Type": "application/json; charset=utf-8"},
    method=method)
with urllib.request.urlopen(req, timeout=8, context=CTX) as r:   # CTX 关掉证书校验
    data = r.read()
```

---

## 10. 性能与限流（重要）

**限流**：官方"一个变量一次请求"的模型下，实测约 **1000 次 / 10 秒**；超过后开始出现 `badjson`/超时类错误。

所以：

| 变量数 | 推荐方式 | 估算 |
| --- | --- | --- |
| 1–2 | 单值 GET | 10 ms/次 |
| 3 个以上 | **批量读 POST Values** | 一次 14–16 ms |
| 上百个 | 批量读 + 分片（每片 ≤ 100）+ 缓存 200 ms | — |

如果被迫用"并发单读"（旧方案）：本机实测 14 个变量串行 165 ms、6 线程并行 **61–92 ms**；而改成批量读后 **15 ms 左右**，所以**优先批量读**。

**客户端缓存**：把读到的值缓存 200 ms，多个页面/多个组件共享同一份，能把压力再降一个数量级。

---

## 11. 排错手册

| 现象 | 排查顺序 |
| --- | --- |
| 连不上 / 超时 | `netstat` 看 8092 是否 LISTENING → 服务是否 Running → 防火墙 → 主机名能否解析（**别用 IP**） |
| `401` | 用户名密码；用户是否被禁用；WinCC 用户管理权限 |
| `402` | 缺许可证（Connectivity Pack 等），这跟前两条最容易混 |
| `404` | URL 拼错 / 资源类型不匹配（如把 `Value` 写成 `Values`）；变量名要 URL 编码 |
| `416` | URL 结构不对（少了资源名、`[ ]` 没编码） |
| `423` | 该资源正被读取，稍后重试 |
| `500` | 请求体形状不对（最常见是**用了 POST 做批量写**，应改 PUT + 数组） |
| 变量全是 0/空 | 看 `qualitycode`：8 = PLC 未连；76 = 还没产生值 |
| 中文变量一律 `Not Found` | **`ensure_ascii=False`**，body 发 UTF-8 原文（本文最大坑） |
| 写不进去 | `canWrite` 是否为 true；变量类型/范围；负数写入无符号变量会失败 |
| 浏览器 `CORS` 报错 | 走后端代理（第 9 章） |

---

## 12. 本机实测环境事实（供对照）

```
WinCC 版本        V8.1 Upd7
项目名            YourWinCCProject1131
服务主机          YOUR-WINCC-HOST（必须用主机名）
端口              8092（HTTPS，自签证书）
根路径            /WinCCRestService
变量总数          约 4252（本项目变量表）；全项目约 4490
批量读            POST tagManagement/Values + {"variableNames":[…]} → 200，4 变量 14–16 ms
批量写            PUT  tagManagement/Values + [{"variableName":…,"value":…}] → 200
归档周期          500 ms（示例归档"供水系统"）
限流             约 1000 次 / 10 秒
```

---

## 附录 A：可直接运行的 Python 客户端（读 / 批量读 / 写 / 批量写 / 历史）

```python
# -*- coding: utf-8 -*-
import base64, json, ssl, urllib.parse, urllib.request, urllib.error

BASE = "https://YOUR-WINCC-HOST:8092/WinCCRestService"
AUTH = "Basic " + base64.b64encode(b"YOUR-WINCC-USER:YOUR-WINCC-PASSWORD").decode()
CTX  = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

def _call(method, path, body=None, timeout=15):
    data = None
    h = {"Authorization": AUTH, "Accept": "application/json"}
    if body is not None:
        # 关键：ensure_ascii=False + charset=utf-8，否则中文变量名会 Not Found
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def read_one(name):
    return _call("GET", "/tagManagement/Value/" + urllib.parse.quote(name, safe=""))

def read_many(names):
    return _call("POST", "/tagManagement/Values", {"variableNames": names})

def write_one(name, value):
    return _call("PUT", "/tagManagement/Value/" + urllib.parse.quote(name, safe=""),
                 {"value": str(value)})

def write_many(pairs):           # [("tag1","1"), ("tag2","0")]
    return _call("PUT", "/tagManagement/Values",
                 [{"variableName": k, "value": str(v)} for k, v in pairs])

def history(archive, name, seconds=3600, max_values=600):
    return _call("POST", "/tagLogging/Values",
                 {"archives": [{"name": archive,
                                "variables": [{"name": name, "range": seconds,
                                               "maxValues": max_values}]}]})

if __name__ == "__main__":
    print(read_one("teat"))
    print(read_many(["teat", "模型名称"]))
    print(write_one("teat", 0))
```

---

## 附录 B：命令速查

```bash
H='https://YOUR-WINCC-HOST:8092/WinCCRestService'
U='YOUR-WINCC-USER:YOUR-WINCC-PASSWORD'

# 根信息
curl -k -u $U "$H"

# 读一个
curl -k -u $U "$H/tagManagement/Value/teat"

# 批量读
curl -k -u $U -X POST -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{"variableNames":["teat","模型名称"]}' "$H/tagManagement/Values"

# 变量元数据（canRead / canWrite）
curl -k -u $U "$H/tagManagement/Variable/teat"

# 变量列表（分页）
curl -k -u $U "$H/tagManagement/Variables?itemLimit=200&continuationPoint=1"

# 写一个
curl -k -u $U -X PUT -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{"value":"12.5"}' "$H/tagManagement/Value/teat"

# 批量写
curl -k -u $U -X PUT -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '[{"variableName":"teat","value":"7"}]' "$H/tagManagement/Values"

# 变量列表（分页）
curl -k -u $U "$H/tagManagement/Variables?itemLimit=200&continuationPoint=1"

# 归档变量配置
curl -k -u $U "$H/tagLogging/Variable/Demo_YL1YW1_PJ1"

# 历史值（注意 UTF-8 原文）
curl -k -u $U -X POST -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{"archives":[{"name":"供水系统","variables":[{"name":"Demo_YL1YW1_PJ1","range":3600,"maxValues":600}]}]}' \
  "$H/tagLogging/Values"
```

---

## 附：与本项目（FillViewer）的对接关系

- 浏览器页面在 `http://127.0.0.1:8099`，**不直接**访问 8092（跨源 + 证书问题）。
- 由 `<安装目录>_发布\server.py` 统一代理：`/api/live`（批量读 + 200 ms 缓存）、`/api/read`（单/多值）、`/api/vt`（变量表）、`/api/history`（历史）。
- WinCC 地址写在 `<安装目录>_发布\config.json` 的 `restBase`（**必须主机名**）。
- 只读/只写权限仍由 WinCC 侧决定：页面写入前还会再校验"该变量是否勾了可写白名单"。
