# 用 Postman 调 WinCC REST 服务

> 适用：WinCC V8.1 Upd7 + REST 服务（`https://<主机名>:8092/WinCCRestService`）
> 本文配套文件（可直接导入 Postman）：
> - 集合：`docs\postman\WinCC_REST.postman_collection.json`（10 条用例，自带断言）
> - 环境：`docs\postman\WinCC_REST.postman_environment.json`（主机、账号、测试变量）
>
> 已用 `newman` 在本机实测：**10 个请求 / 20 条断言 / 0 失败**。

---

## 1. 为什么用 Postman 而不是 curl

| 场景 | curl | Postman |
| --- | --- | --- |
| 临时试一个接口 | 够用 | 略重 |
| 反复调同一组接口 | 每次拼命令、查历史 | 保存成集合，点一下就发 |
| 看返回体/格式化 JSON | 靠终端 | 自动高亮、可折叠 |
| **保存/共享**给别人 | 要另存脚本 | 集合 JSON 直接发文件 |
| 批量回归 | 写脚本 | Runner 一键跑完出报告 |
| 自动断言（值是否合法） | 要写代码 | `Tests` 里写两行 `pm.test` |

结论：**探索阶段用 Postman，自动化回归用 newman（同一份集合）**。

---

## 2. 安装与首次启动

1. 官网下载 Windows 版，默认安装即可（本机已装：`C:\Users\wangshuai\AppData\Local\Postman`）。
2. 首次启动会让你登录/注册账号 —— **可以跳过**：
   点左下角 `Skip and go to the app`（旧版在右上角 `Continue without an account`）。
   不登录也能导入集合、发请求；只是「同步到云端」不可用。
3. 关闭自动更新（可选）：`Settings → General → Updates` 里关掉，避免现场机器联网受限时反复提示。

---

## 3. 导入集合与环境（两分钟）

1. 左上角 `Import` → 选文件 → 把下面两个文件都拖进去：
   - `<安装目录>\docs\postman\WinCC_REST.postman_collection.json`
   - `<安装目录>\docs\postman\WinCC_REST.postman_environment.json`
2. 右上角环境下拉框（默认 `No Environment`）→ 选 **`WinCC REST · 本机(YourWinCCProject1131)`**。
   > 选了环境，集合里的 `{{baseUrl}}`、`{{tag1}}` 才有值，否则请求会变成 `{{baseUrl}}/...` 直接报错。
3. 打开集合，能看到 10 条用例（00～09）。每条后面的绿色数字就是断言条数。

---

## 4. 关键一步：关掉 SSL 证书校验

WinCC REST 用的是**自签证书**，不关这个，所有请求都会报
`Error: self-signed certificate in certificate chain`（或 `SSL Error`）。

`File → Settings → General →` 找到 **`SSL certificate verification`**，**关掉（OFF）**。

> 命令行等价物：curl 加 `-k`；newman 加 `--insecure`。
> 只对自签/内网证书这么做，公网请求请保持开启。

---

## 5. 认证（Basic Auth 已配在集合级）

集合已设置 `Authorization → Basic Auth`，用户名/密码取环境变量 `{{user}}` / `{{pass}}`（默认 `YOUR-WINCC-USER` / `YOUR-WINCC-PASSWORD`）。

检查方法：点集合名 → `Authorization` 页 → `Type = Basic Auth`，`Username = {{user}}`、`Password = {{pass}}`。
子请求的 Authorization 页显示 `Inherit auth from parent`，即继承集合设置，不用每条都填。

> 改密码：改环境变量即可，不用动 10 条请求。

---

## 6. 逐条运行

点任意一条 → 右侧 `Send` → 下方看三块：

| 区域 | 看什么 |
| --- | --- |
| `Body` | 返回的 JSON（Pretty 自动格式化） |
| `Test Results` | 断言是否通过（每条用例 2 条左右） |
| `Headers` / `Status` | HTTP 码、耗时、返回体大小 |

对 `Body` 里的中文乱码：确认 `Body` 页签是 `Pretty` + `JSON`（不是 `Raw` 的 GBK 显示问题，那只是终端错觉）。

---

## 7. 批量跑（Collection Runner / newman）

### 7.1 图形界面

集合右侧 `Run collection` → 勾选环境 → `Delay = 200 ms` → `Run`。
**Delay 不要设 0**：WinCC REST 的限流是「1000 次 / 10 秒」，10 条请求虽不至于触发，但现场批量调试时 200 ms 间隔最稳。

### 7.2 命令行（推荐，能进 CI）

```powershell
npx --yes newman run "<安装目录>\docs\postman\WinCC_REST.postman_collection.json" `
  -e "<安装目录>\docs\postman\WinCC_REST.postman_environment.json" `
  --insecure --delay-request 200
```

实测输出末尾：

```
requests      10 / 0 failed
assertions    20 / 0 failed
total run duration: 3.2s
```

要在别的机器跑，只需复制这两个 json，然后
`newman run <集合> -e <环境> --insecure --env-var "baseUrl=https://另一台:8092/WinCCRestService"`。

---

## 8. 中文 body 必须是 UTF-8 原文

这是本套接口最容易踩的坑：**变量名里带中文时，请求体必须是 UTF-8 原文，不能被转义成 `\uXXXX`**。

- Postman：默认就是 UTF-8，`Body → raw → JSON`，直接写
  ```json
  {"variableNames":["teat","模型名称","Demo_ZB1_YX1"]}
  ```
  如果是从别处粘来的 `"HC1\u6570\u636e_SN1_YX1"`，要**手动改回中文**，否则 WinCC 按字面量匹配，全部返回 `Not Found`。
- 代码里：`json.dumps(obj, ensure_ascii=False).encode("utf-8")`，并带
  `Content-Type: application/json; charset=utf-8`。

---

## 9. 换主机 / 换测试变量

全部走环境变量，改一处生效全部：

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `baseUrl` | 服务根地址 | `https://YOUR-WINCC-HOST:8092/WinCCRestService` |
| `user` / `pass` | Basic 认证 | `YOUR-WINCC-USER` / `YOUR-WINCC-PASSWORD` |
| `tag1` | 测试用可读写变量 | `teat` |
| `tag2` | 中文名变量（测编码） | `模型名称` |
| `tagArc` | 已归档变量（测历史） | `Demo_YL1YW1_PJ1` |
| `notExist` | 故意不存在 | `不存在的变量XYZ` |

点环境名 → 表格里改 `Current Value` → `Ctrl+S`。

> `baseUrl` 里的主机名**必须用主机名，不能用 IP**：证书是签给主机名的，用 IP 会证书不匹配。

---

## 10. 把请求变成代码

任一条请求右侧 **`</>`（Code）** 按钮 → 选 `cURL` / `Python - requests` / `PowerShell`，直接复制到脚本里。
这是从「能跑」到「写进程序」最短的路，比自己拼 header 快且不会漏 `-k`。

---

## 11. 常见错误对照表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `self-signed certificate` | 自签证书 | 关 SSL verification / `-k` / `--insecure` |
| `401 Unauthorized` | 账号密码错，或变量没开 REST 权限 | 查 `variable` 接口的 `canRead/canWrite` |
| `402` | 缺 REST 许可证 | 服务器侧装许可证 |
| `403 Forbidden` | 变量没给该用户 REST 读/写授权 | WinCC 变量属性 → REST 页勾选 |
| `404 Not Found` | 变量名不存在（**也可能是 body 被转义**） | 先试 `Variable/<名>`，再看 §8 |
| `416` | URL 结构不对（用了不存在的资源段） | 看返回体 `possibleCollections` 照抄路径（见教程 v2 §5.4） |
| `423` | 资源被锁 | 稍后重试 |
| `500` | 请求体格式不对（如批量读写错 body 形状） | 批量读要 `{"variableNames":[…]}`；批量写要数组 |
| Body 里 `{"error":"Not Found"}` 但 HTTP 200 | 单个变量不存在（批量读的整体状态仍是 200） | 逐个看 `error` 字段 |
| 请求超时 | 变量太多/网络慢 | Settings → `Request timeout` 调到 30000 ms |

---

## 12. 用它验证 FillViewer 的配置

FillViewer 的 `/api/live` 走的就是这套 REST。调试顺序：

1. Postman 先确认 `GET {{baseUrl}}/tagManagement/Value/<变量>` 能读到；
2. 再打开 `http://127.0.0.1:8099/api/read?one=<变量>`，看服务端是否也能读到；
3. 最后开运行画面 `runtime.html`，看展板/颜色是否跟着变。

**Postman 与浏览器 CORS 无关**：Postman 不做 CORS 检查，所以「Postman 能通、网页不通」通常是网页侧的问题（跨域、代理、静态资源），不是 WinCC 侧。

---

## 13. 一页速查

```
导入：Import → 选 collection.json + environment.json → 右上角选环境
证书：Settings → SSL certificate verification → OFF
认证：集合级 Basic Auth，值取 {{user}}/{{pass}}
单条：Send → 看 Body / Test Results
批量：Run collection（Delay 200ms）或 newman run ... --insecure
改主机：改环境变量 baseUrl（必须主机名，不能 IP）
导出代码：</> Code → cURL / Python
中文：请求体保持 UTF-8 原文，不要 \uXXXX
```
---

## 14. 追加：写入值（2026-11-23 实测）

### 14.1 两个请求，记这两行就够

| 用途 | 方法 | URL | Body（raw / JSON） |
| --- | --- | --- | --- |
| 写一个 | **PUT** | `{{baseUrl}}/tagManagement/Value/{{tag1}}` | `{"value":"12.5"}` |
| 批量写 | **PUT** | `{{baseUrl}}/tagManagement/Values` | `[{"variableName":"teat","value":"5"},{"variableName":"模型单击动作","value":"0"}]` |

- Header 只需要 `Content-Type: application/json; charset=utf-8`（Postman 选 `Body -> raw -> JSON` 会自动带）。
- `value` **永远是字符串**（写 `"5"`，不能写 `5`）。
- 批量写的 body 是**数组**，不是对象；写错形状会 500（见 14.4）。

### 14.2 Postman 里怎么点（按顺序 6 步）

1. 左侧点开集合 → 点 **`05 写单个变量（PUT Value）`**（集合里已建好）。
2. 右上角确认环境已选（不是 `No Environment`）。
3. 中间 `Method` 应是 **PUT**；`URL` 应是 `{{baseUrl}}/tagManagement/Value/{{tag1}}`。
4. 切到 **`Body`** 页签 → 选 **`raw`** → 右边下拉选 **`JSON`**（选 Text 也能通，但 JSON 有高亮/校验）。
5. 在下面输入框里写：`{"value":"12.5"}`。
6. 点 **`Send`**，然后点 `01 读单个变量` 读回来确认。

成功返回（HTTP 200）：

```json
{"variableName":"teat","errorcode":0,"namespace":""}
```

读回时 `value` 变成 `"12.5"` —— **写完必须读回**，这是唯一可靠的验收方式。

### 14.3 【最重要】判成功不能看 HTTP 码

本接口写失败也返回 **HTTP 200**，失败信息在 body 里。实测四种结果：

| 场景 | HTTP | 返回体 | 怎么判 |
| --- | --- | --- | --- |
| 写成功 | 200 | `{"variableName":"teat","errorcode":0,"namespace":""}` | 有 `errorcode:0` ✅ |
| 变量不存在 | **200** | `{"variableName":"不存在的变量XYZ","error":"Not Found",...}` | 有 `error` 字段 ❌ |
| 变量只读（canWrite=false） | **200** | `{"variableName":"Demo_YL1YW1_PJ1","error":"Forbidden",...}` | 有 `error` 字段 ❌ |
| 批量里部分失败 | 200 | `[...,{"errorcode":0},...,{"error":"Not Found"}]` | **逐项看** `errorcode` |

判据：**`errorcode == 0` 才算成功；出现 `error` 字段就是失败**
（`Not Found` = 变量名错；`Forbidden` = 没写权限）。

写入前先查权限，能省一半调试时间：

`GET {{baseUrl}}/tagManagement/Variable/{{tag1}}` → 看 `"canWrite": true`。

### 14.4 写错形状会 500（实测对照）

| Body | 结果 |
| --- | --- |
| `PUT Value/<名>` + `{"value":"5"}` | 200 ✅ |
| `PUT Values` + `[{...},{...}]` | 200 ✅ |
| `PUT Values` + `{"values":[...]}` | **500** |
| `POST Values` + `[...]`（用 POST 去写） | **500** |
| `POST Values` + `{"variableNames":[...],"values":[...]}` | 200 且真写（未文档化，不建议用） |

记两条：**写用 PUT，不是 POST**；**批量写要裸数组**。

### 14.5 中文变量名

Postman 的 URL 栏直接写中文也能发（自动编码），实测：

```
PUT {{baseUrl}}/tagManagement/Value/模型单击动作   → 200，errorcode=0
```

代码里必须自己编码：`urllib.parse.quote(name, safe="")`。
**Body 里的中文不要转义**（别出现 `\u6570\u636e` 这种），否则 WinCC 按字面量匹配，返回 `Not Found`。

### 14.6 三种值的写法

| 变量类型 | 写什么 | 例子 |
| --- | --- | --- |
| 数值（整型/浮点） | 数字字符串 | `{"value":"12.5"}`；整数变量写 `"12"`（写 `12.5` 会被拒或截断） |
| 布尔 / 二进制 | `"1"` / `"0"` | `{"value":"1"}`；也接受 `"true"/"false"`，但**以读回为准** |
| 文本 | 原样字符串 | `{"value":"1#充填泵"}` |

### 14.7 三个现场纪律

1. **命令位是脉冲**：`*_DK2`（打开）、`*_TZ2`（停止）这类写到 1 会真的下发，写完立刻写回 `0`，别让设备保持"常按"状态。
2. **PLC 会覆盖**：变量连着 PLC 且 PLC 在周期写时，你写进去的值可能几秒后被刷回。判断是"REST 没写进去"还是"被 PLC 覆盖"：写完后**立刻连读 3 次**看值走向。
3. **别拿生产变量做实验**：先在内部变量（`teat`、`模型单击动作`）上把流程验证通，再换目标变量。

### 14.8 质量码会变好（旁证）

写入成功后读回，`qualitycode` 会从 `76`（初始值 / uncertain）变成 **`128`**（good），说明值真的被采集刷新了。

