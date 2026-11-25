# 参与贡献

1. Fork 本仓库，`git checkout -b feature/xxx`。
2. 本地 `python server.py` 用 `demo` 项目自测：运行画面/配置台/变量页三个页面都不报错。
3. 改 `app/*.html` 后请顺手跑一次语法检查（浏览器控制台无 `Uncaught/TypeError` 即可）。
4. 提交 PR，说明**动机 + 复现步骤 + 截图**（涉及 UI 时）。

约定：
- 只用 Python 标准库，不引入第三方依赖。
- 前端不引入构建链，`<script type="module">` 直接跑。
- 新增配置项一律走 `config.json` / `project.json`，不要写死在代码里。
- 提交前确认没有把真实主机名、账号、公司模型带进仓库。
