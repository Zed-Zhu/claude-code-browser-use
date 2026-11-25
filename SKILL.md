---
name: cdp-browser-use
description: "用 Chrome DevTools Protocol 直接驱动用户本机正在使用的 Chrome/Edge/Brave，带着用户真实登录态做浏览器操作：打开网页、点击、输入、滚动、读页面文字、截图、抓取网页接口返回的原始 JSON。零依赖（只用 Python 标准库），不装 Playwright、不下载浏览器、不导出 cookie。当任务需要操作浏览器、网页需要登录才能看、WebFetch/curl/CLI 拿不全或被验证码/反爬拦下、需要展开评论/无限滚动列表、需要拿 XHR 数据、需要截取线上页面时使用。Use for browser automation / browser use / web scraping with the user's logged-in browser."
---

# CDP Browser Use

通过 CDP 操作**用户自己正在用的浏览器**，而不是另起一个干净的自动化浏览器。
所有操作都通过一个脚本完成：`python3 $SKILL/scripts/cbu.py <命令>`，输出都是 JSON。
`$SKILL` 指本 SKILL.md 所在目录。

## 第 0 步：doctor，按 `next` 走

```bash
python3 $SKILL/scripts/cbu.py doctor
```

根据 `status` 处理：

| status | 含义 | 做法 |
|---|---|---|
| `ready` | 有浏览器开着调试，或守护进程已连上 | 直接干活。第一次连接时浏览器会弹「允许远程调试？」，**先告诉用户去点允许** |
| `debugging_off` | 装了浏览器，但没开调试开关 | 运行 `cbu.py setup`：它会打开 `chrome://inspect/#remote-debugging`，请用户勾选 **Allow remote debugging for this browser instance**，脚本会等到开关生效 |
| `browser_too_old` | Chrome 版本低于 144，没有这个开关 | 请用户升级 Chrome；或者用 `cbu.py launch` 隔离模式 |
| `no_browser` | 没装任何 Chromium 内核浏览器 | 问用户是否安装 Chrome（`next` 里有各平台命令）；不想装就用 Playwright 下载的 Chromium 加 `launch --browser-path` |

**隔离模式**（`cbu.py launch [--headless]`）：用一个独立的 profile 启动浏览器（默认位置 `~/.cache/claude-browser-use/profile`），不需要用户开开关，也不会弹窗。代价是里面没有用户平时的登录态；用户可以在这个窗口里登录一次，之后会一直保留。适用场景：用户不想开调试开关、浏览器版本太旧、或者只是访问不需要登录的公开页面。

规则：**不要猜调试端口**。9222 常被 VS Code、Obsidian 等 Electron 应用占用。端口一律从浏览器自己写的 `DevToolsActivePort` 文件里读，脚本已经处理好了。

## 常用命令

```bash
C="python3 $SKILL/scripts/cbu.py"
$C open <url>                    # 在一个单独的后台窗口里打开，它会成为当前标签页
$C snap                          # 列出可交互元素，每个带 ref 编号（包括 cursor:pointer 的 div 按钮）
$C click --ref 12                # 也可以 --selector '.btn' 或 --text '展开 3 条回复'
$C type "关键词" --ref 3 --enter  # 加 --clear 先清空
$C press Escape                  # 支持 Enter/Tab/Escape/ArrowDown/PageDown 等
$C scroll [--selector .list] [--by 800 | --bottom]   # 默认发真实滚轮事件，加 --js 改为直接设置 scrollTop
$C wait --text "加载完成"         # 或 --selector
$C text [--selector main]        # 读页面文字（默认最多 20000 字）
$C eval "await fetch('/api/x').then(r=>r.json())"   # 执行任意 JS，支持顶层 await
$C shot [--full] [--out a.png]   # 截图后打印文件路径，再用 Read 工具查看
$C watch 'api/comment'           # 开始监听：URL 匹配这个正则的响应，都会保存原始 body
$C captured --out data.json      # 导出捕获到的响应（JSON 会自动解析）
$C tabs | use --tab <id前缀> | goto <url> | close [--all]
$C raw Page.reload '{}'          # 兜底：直接发任意 CDP 方法
$C stop                          # 关掉 cbu 打开的标签页，断开连接
```

- **ref 会过期**：页面变化后要重新 `snap`。
- **为什么默认开后台窗口，而不是后台标签页**：隐藏的后台标签页 Chrome 不渲染，真实点击、滚轮事件不会被处理，依赖 IntersectionObserver 的懒加载（评论、无限滚动列表）也会暂停。单独的后台窗口不会抢你的焦点，但页面会正常渲染。`open --background-tab` 仍然可用，只是点击和滚动会自动降级为 JS 方式，返回里的 `via` 字段会注明。
- **尽量一个标签页用到底**：用 `goto` 切换网址，不要反复 `open`，否则会开出很多窗口。
- **批量或循环操作写成脚本**：`$C run job.py arg1 ...`。脚本里预置了 `b`（Browser 对象，方法和上面的命令对应：`b.open/click/type/eval/text/snap/scroll/watch/captured/shot/cdp`）、`args`、`json`、`time`。参考 `examples/expand_and_capture.py`：边点「展开更多」边滚动，同时抓接口数据。
- **拿数据优先用 `watch` 加 `captured`**：接口返回的原始 JSON 比解析页面 DOM 稳定、完整（点赞数、id、时间戳都有），也不受页面样式改版影响。

## 安全和礼仪（必须遵守）

这是**用户的真实浏览器和真实账号**：

1. **只在 cbu 自己开的后台标签页里操作。** 用户自己的标签页默认禁止操作；只有用户明确要求时，才加 `--allow-user-tab`。
2. **不可撤回的操作先问用户**：提交订单或付款、发消息、发帖、评论、点赞、删除、修改账号设置。只读浏览和抓取不需要问。
3. **不读取、不导出 cookie 和密码**，也不把页面上的个人信息带出当前任务的范围。
4. **控制频率**：循环里每次操作之间停 1–3 秒，不要并发刷接口。
5. **收尾**：任务结束后运行 `stop`。如果开关是你请用户打开的，提醒用户可以回到 `chrome://inspect/#remote-debugging` 把勾去掉：开着的时候，本机任何程序都能控制这个浏览器。

## 排错

| 现象 | 处理 |
|---|---|
| 一直卡在 connecting | 浏览器在等用户点「允许」；请用户切到浏览器窗口看一下 |
| `prompt denied` / `could not connect` | 用户点了拒绝，或者开关被关了；重新运行 `doctor` |
| `tab was not opened by cbu` | 这是保护规则在生效，不是 bug；见上面安全第 1 条 |
| 点击没反应 | 先 `snap` 看元素在不在；元素被遮住时会自动改用 JS 点击（返回里 `via` 字段会说明）；也可以直接 `eval` 调页面自己的函数 |
| `captured` 是空的 | `watch` 必须在触发请求**之前**调用；用 `raw Network.enable` 确认接口地址，正则写宽一点 |
| 滚动或点击之后没有加载新内容 | 确认标签页是用默认的窗口模式打开的（`via` 字段应该是 `wheel` 或 `mouse`）；有的站点只响应真实滚轮事件，不响应直接修改 `scrollTop` |
| 截图卡住 | 后台标签页不渲染导致的；用默认的窗口模式，或者 `open --foreground` |
| 闲置 30 分钟后自动断开 | 设计如此（环境变量 `CBU_IDLE` 可调）；下一条命令会自动重连，可能需要再点一次允许 |

状态文件和日志在 `~/.cache/claude-browser-use/`（可以用 `CBU_HOME` 改位置）。
