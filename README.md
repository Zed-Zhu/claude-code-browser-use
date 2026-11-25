# claude-code-browser-use

**让 Claude Code 直接操作你正在用的浏览器，带着你的登录态做事。**

这是一个 Claude Code skill（也可以单独当命令行工具用）。它通过 Chrome DevTools Protocol（CDP）连接你本机已经打开的 Chrome / Edge / Brave，用于打开网页、点击、输入、滚动、读取页面、截图，以及抓取网页接口返回的原始 JSON。

- **零依赖**：一个 Python 文件，只用标准库（Python 3.8+）。不装 Playwright，不下载浏览器，不用 `pip install`。
- **用的是你的真实会话**：不导出 cookie，不需要重新登录，那些需要登录才能看的页面也能直接访问。
- **只需要确认一次**：一个后台守护进程保持唯一的 CDP 连接，整个会话里浏览器只弹一次「允许远程调试」。
- **自动识别各种初始状态**：没装浏览器、没开调试开关、版本太旧、已就绪，`doctor` 都会给出下一步该做什么。

## 和 Playwright 的区别

Playwright（以及 Puppeteer、Selenium）的默认做法是**另起一个干净的自动化浏览器**。这种做法适合写测试，但让 AI 替你在网上办事时就不太顺手：

| | Playwright 常规用法 | 本项目（CDP 直连） |
|---|---|---|
| 浏览器 | 另外下载一个 Chromium（每个版本约 150 MB 以上），或者另起一个新进程 | 直接用你已经打开的浏览器 |
| 登录态 | 每次从零开始；想要登录态，就得导出、注入 cookie 文件 | 就是你本人的会话；不搬凭证，没有明文 cookie 落盘 |
| 网站看到的 | 一个全新的无头 profile：没有浏览历史，没有扩展，指纹特征明显 | 和你平时浏览一样：同一个 profile、同一套扩展和设置 |
| 依赖 | `pip install playwright` 加 `playwright install` | 无 |
| 用户能否看到、插手 | 无头模式下看不到 | 在你的浏览器里单独开一个后台窗口操作；遇到登录、二次验证可以随时自己接手。浏览器会显示"正受到自动测试软件控制"的提示 |
| 抓数据 | 一般靠解析页面 DOM | 内置 `watch` / `captured`，直接拿到网页接口返回的原始 JSON |
| 授权 | 无 | 浏览器弹窗由用户确认；默认只能操作自己开的标签页 |

**一个真实例子**：抓取某内容平台一篇帖子的全部评论和楼中楼回复。用逆向接口的命令行工具抓，回复接口每次都被验证码拦下，冷却后重试也没用。换成 Playwright，得先从浏览器导出 cookie 再注入，最后虽然拿到了，但流程很绕。改用 CDP 直连自己的浏览器后，211 条评论全部拿到，没有遇到一次验证码，也没有搬任何凭证。

**仍然应该用 Playwright 的情况**：CI 或服务器上的自动化测试、需要大量并发且互相隔离的浏览器上下文、需要 Firefox 或 WebKit、需要每次都从同一个干净初始状态开始复现。本项目也提供了**隔离模式**（`cbu.py launch`），可以兼顾"不碰真实 profile"的需求。

## 安装

**作为 Claude Code skill**：

```bash
git clone https://github.com/Zed-Zhu/claude-code-browser-use ~/.claude/skills/cdp-browser-use
```

之后跟 Claude 说"打开某某网页帮我看看……"、"把这个页面的评论全抓下来"之类的需求，它就会调用这个 skill。

**单独当命令行工具用**：直接运行 `python3 scripts/cbu.py --help`。

## 第一次使用

```bash
python3 scripts/cbu.py doctor
```

`doctor` 会检测本机装了哪些浏览器、调试开关有没有打开，然后在 `next` 字段里告诉你下一步：

1. **已就绪**：直接用。第一次连接时，浏览器会弹窗询问是否允许远程调试，点「允许」。
2. **没开调试开关**（Chrome 144 及以上）：运行 `cbu.py setup`。它会打开 `chrome://inspect/#remote-debugging`，勾选 **Allow remote debugging for this browser instance** 即可，脚本会自动等到开关生效。
   > 从 Chrome 136 起，出于安全考虑，不能再用 `--remote-debugging-port` 命令行参数调试默认 profile，所以需要这个浏览器内的开关。
3. **浏览器版本太旧，或者不想动日常使用的 profile**：运行 `cbu.py launch [--headless]`，它会用一个独立 profile（`~/.cache/claude-browser-use/profile`）启动浏览器。这种模式不弹窗，但没有你平时的登录态；在里面登录一次之后会一直保留。
4. **没装浏览器**：`next` 里会给出各平台的安装命令（例如 `brew install --cask google-chrome`、`winget install Google.Chrome`）。不想在系统里装 Chrome 的话，可以用 Playwright 下载的 Chromium：`launch --browser-path <路径>`。

支持 macOS、Linux、Windows，以及 Chrome（含 Beta、Canary）、Edge、Brave、Chromium。Edge 和 Brave 的浏览器内开关以它们实际提供的为准，没有开关时用隔离模式。

## 命令一览

```bash
cbu.py open <url>                  # 在一个单独的后台窗口里打开（不打扰你当前的窗口）
cbu.py snap                        # 列出可交互元素及其 ref 编号
cbu.py click --ref 5 | --selector S | --text "展开"
cbu.py type "文字" --ref 3 [--clear] [--enter]
cbu.py press Enter | scroll [--bottom] | wait --text T   # click 和 scroll 发的是真实鼠标、滚轮事件
cbu.py text [--selector S]         # 读页面文字
cbu.py eval "await fetch('/api').then(r => r.json())"
cbu.py shot [--full]               # 截图
cbu.py watch 'api/comment'         # 按正则监听接口响应
cbu.py captured --out data.json    # 导出抓到的原始 JSON
cbu.py run job.py [args]           # 批量/循环逻辑写成 Python 脚本，脚本里预置了 b(Browser)
cbu.py raw <CDP方法> '<json参数>'    # 直接发任意 CDP 命令
cbu.py stop                        # 关闭 cbu 开的标签页并断开连接
```

示例脚本 [`examples/expand_and_capture.py`](examples/expand_and_capture.py) 会循环点击"展开更多"、往下滚动，同时抓取接口数据，适用于评论区、无限滚动列表这类页面。

## 安全设计

- 守护进程只监听 `127.0.0.1`，每次请求要带随机 token 和自定义请求头，网页无法跨站调用它。
- **默认不能操作你自己打开的标签页**，必须显式加 `--allow-user-tab`。
- 不读取、不导出 cookie 和密码。
- 闲置 30 分钟后自动断开连接（可以用 `CBU_IDLE` 调整）。`stop --quit-browser` 只能用来关闭隔离模式启动的浏览器，绝不会关掉你自己的浏览器。
- SKILL.md 里要求 Agent 在执行付款、发消息、发帖、删除这类不可撤回的操作前先征得你同意。
- 用完以后建议回到 `chrome://inspect/#remote-debugging` 关掉开关：开关打开期间，本机任何程序都能控制这个浏览器。

请遵守目标网站的服务条款，合理控制访问频率。

---

## English

**Let Claude Code drive the browser you're already using, with your real logged-in session, over raw Chrome DevTools Protocol.** One stdlib-only Python file: no Playwright, no browser download, no cookie export. A tiny daemon holds a single CDP connection, so the browser's "Allow remote debugging?" prompt is answered once per session.

Why not Playwright? Playwright launches a fresh automation browser: no logins (unless you export and inject cookies), a pristine headless profile, and a 150 MB+ browser download. This tool attaches to your real Chrome/Edge/Brave, works in background tabs you can watch or take over, and can capture the page's own XHR/fetch JSON (`watch` / `captured`). Use Playwright for CI, isolated parallel contexts, or Firefox/WebKit; this tool also has an isolated-profile mode (`launch`).

Start with `python3 scripts/cbu.py doctor`. It detects installed browsers and the debugging state, and prints the next step: `setup` opens `chrome://inspect/#remote-debugging` (Chrome 144+) for the one-click toggle; `launch` starts an isolated profile for old browsers or when you'd rather not touch your main profile; if no browser is installed, it prints install commands. Install as a Claude Code skill with `git clone https://github.com/Zed-Zhu/claude-code-browser-use ~/.claude/skills/cdp-browser-use`. MIT licensed.
