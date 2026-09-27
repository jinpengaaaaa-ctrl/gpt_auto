# gpt-automation

## 安全结果与恢复

`results.txt` 只保存会话 ID、账户指纹和状态，不保存邮箱、密码、TOTP 密钥或卡片完整信息。成功账户的敏感凭据保存在 `runtime/credentials/<session_id>.bin`，Windows 使用当前用户的 DPAPI 加密。断点恢复或人工接管时，如果凭据记录不完整，流程会停在 `NEEDS_OPERATOR`，不会伪造成功。

ChatGPT 账户创建自动化。项目只保留一套账户业务流程：手动 IP、自动 IP、辅助模式和无人值守模式都调用 `RegistrationWorkflow`；模式差异仅由网络策略和交互策略表达。

程序自动操作失败时不会盲目跳过。辅助模式允许用户在浏览器中手动完成操作，程序验证页面后继续；无人值守模式保存 checkpoint 并返回 `NEEDS_OPERATOR`。

## 快速开始

```bash
pip install -r requirements.txt
# 复制 .env.example 为 .env，并填写邮箱、卡片、地址和网络配置
python -m gpt_automation
```

生产流程不再询问或共享固定的 ChatGPT 密码。每个 session 自动生成独立密码，在 MFA 开始前写入 Windows DPAPI 加密凭据文件；`results.txt` 只保留 session、账户指纹和状态。

双击 `start.bat` 时只提供标准模式，默认第一项为手动 IP，自动 IP 为第二项。两种模式都使用同一套辅助交互策略：程序自动执行可验证步骤，遇到需要人工处理的页面时保持浏览器现场并等待用户确认。无人值守模式仍保留给直接 CLI 调用，不在 `start.bat` 菜单中暴露。

创建账号时 `[3.7]` 可以选择「连续执行」或「标准模式」：连续执行遇到账户步骤受阻时自动跳过当前账户并继续下一个；标准模式保留人工确认。开通会员与 Full 任务强制标准模式，支付相关步骤永不自动跳过。

手动 IP 模式在批次开始时会先检测当前公网 IP，按 Enter 可直接使用（默认），输入 `S` 才切换手机热点。后续账户只需保证与上一个账户 IP 不同：程序每 10 秒自动检测一次，IP 变化即自动放行，不再检查 7 天历史。辅助模式下 9 分钟无变化会向用户确认（C=使用上一 IP 继续（一次性）/ Enter=继续等待 / Q=取消）；连续执行模式则无限等待，全程无人确认。批次结束会显示每个失败／跳过账户的原因，并生成 `runtime/reports/batch_errors_*.txt` 错误报告。

浏览器通过 IP 验证后统一打开 `https://chatgpt.com/auth/login`。已有有效登录会话时由 ChatGPT 自动重定向到 `https://chatgpt.com/`；未登录时停留在登录页并输入当前任务的邮箱。页面检测器不会为了识别未知页面而自动跳转。

安装项目后也可以使用同步 CLI 入口：

```bash
gpt-register
```

## 输入文件

运行输入位于 `input/`，该目录已被 Git 忽略：

- `email.txt`：每行 `email----password----client_id----refresh_token`；
- `cards.csv`：信用卡号、有效期和 CVC；
- `address_jp.csv`：日本账单地址；
- `used_email.txt`、`used_cards.txt`：旧版使用记录，仅首次导入 SQLite，正式运行不再覆盖。

公开仓库只包含 `input/cards.example.csv` 和 `input/email.example.txt`。
首次使用时复制示例文件并替换为自己的邮箱、OAuth token、卡片和地址数据；
这些文件包含敏感信息，已被 `.gitignore` 排除，不能提交到公开仓库。

结果写入 `results.txt`。输入、输出、浏览器 profile、截图、日志和运行数据库均不得提交到 Git。

`input/email.txt` 是活动邮箱队列：成功创建的邮箱在凭据完整转存后会从 input 移除；
连续模式跳过的邮箱会移入 `accounts/accounts_skipped.txt` 并恢复为可用状态。该文件
不是黑名单，用户把对应行复制回 `input/email.txt` 即可在下一批次重新执行；同一批次内
每个邮箱最多执行一次。

新任务创建时只预留邮箱。信用卡会在优惠确认并进入 Checkout、准备填写卡片前才申请；支付确认成功后信用卡标记为已消费，不会重复使用。旧 session 如果停在 Checkout 之前且没有支付动作，正常启动时会释放其过早预留的信用卡，但保留邮箱和恢复现场。

## 人工接管

辅助模式遇到页面失败时提供以下决策：

- `Enter`：用户已经手动操作，程序重新检测页面并验证后置条件；
- `R`：仅对允许安全重试的自动动作重新执行；
- `D`：保存当前 session 后关闭浏览器；
- `S`：跳过当前账户；
- `Q`：终止批次。

支付提交和取消订阅是非幂等动作。动作结果未知时会记录为 `UNKNOWN_SIDE_EFFECT`，相关邮箱和卡片进入隔离状态，禁止自动重试。

无人值守模式通过环境变量启用：

```bash
set AUTO_MODE=true
python -m gpt_automation
```

无人值守模式不会等待标准输入，也不会把 MFA、支付或取消订阅默认为成功；需要人工时返回退出码 `2`。

## 中断恢复

运行状态保存在 `runtime/workflow_state.db`。数据库与 IP 历史只保存账户／资源指纹、checkpoint、动作状态和资源租约，不保存邮箱密码、token、卡号或 CVC 明文。

`start.bat` 默认隐藏恢复区，不会在启动时展示恢复选项。显式 CLI 恢复命令仍然可用；以后需要重新显示启动器恢复区时，将 `RECOVERY_SECTION_ENABLED` 设为 `true` 即可。

```bash
# 列出可恢复或可查看的 session；交互式列表显示输入源中的完整邮箱
python -m gpt_automation --list-resumable

# 显式恢复一个 session
python -m gpt_automation --resume SESSION_ID

# 明确放弃一个 session；程序会分别处置邮箱和信用卡，并要求二次确认
python -m gpt_automation --abandon-session SESSION_ID

# 单独检查并准备 CloakBrowser，不启动注册流程
python -m gpt_automation --check-cloakbrowser

# 仅进入持久化浏览器现场查看，不执行注册业务
python -m gpt_automation --inspect-session SESSION_ID

# 仅查看未映射的旧 profile 现场
python -m gpt_automation --inspect-profile PROFILE_ID
```

恢复时程序从输入源重新匹配已租用资源，重新检测浏览器页面，并保持 checkpoint 单调前进。旧支付记录为 `UNKNOWN` 时，程序只允许人工核查或可靠页面验证，不会再次点击支付。session 列表中的完整邮箱只用于当前交互式展示，不写入日志、checkpoint、结果文件或 SQLite 明文；无法从输入源或加密凭据解析的旧 profile 会标记为未映射现场。

恢复阶段早于 Checkout 的 session 可以暂时没有信用卡租约；进入 Checkout 后必须重新取得信用卡。放弃任务时，尚未创建账号的邮箱可以释放，已经进入密码／MFA／优惠阶段的邮箱不会重新放回注册池；支付结果未知时邮箱和信用卡都会隔离。

`--inspect-session` 和 `--inspect-profile` 都是诊断入口：它们可以更新浏览器 cookies、缓存和 profile 文件，但不会推进 workflow checkpoint、改变资源租约，也不会执行注册、MFA、支付或取消订阅动作。已被其他进程占用的 session 或浏览器 profile 会拒绝打开，程序不会删除 Chromium 锁文件。

启动批次前会先在当前 Python 环境中执行 `python -m pip install --upgrade cloakbrowser`，再输出实际的 wrapper 和 Chromium binary 版本，并调用官方二进制预检。这样不固定某个 wrapper 版本，每次运行都以当时可用的最新环境为准；如果网络、权限或授权检查失败，批次会在打开浏览器前停止。

## 退出码

- `0`：批次正常结束或资源正常耗尽；
- `1`：网络、输入或环境导致批次失败；
- `2`：存在需要用户继续处理的账户。

## 桌面端控制台

项目附带一个本机桌面端流程控制台，用于查看 Phase 1–4 流程、配置运行参数、管理邮箱与信用卡资源、查询执行履历。

```powershell
# 1. 构建前端（首次或前端代码变更后）
cd frontend
npm install
npm run build
cd ..

# 2. 启动本机服务（Windows 下若 python 命令不可用，请使用项目 .venv 或本机可用解释器）
python -m gpt_automation.web --port 8765
```

启动后打开 http://127.0.0.1:8765 即可使用。

注意：

- 服务只绑定本机回环地址，没有远程访问能力。
- 「开始运行」默认执行真实生产流程：会调用现有 `RegistrationWorkflow`，并在浏览器、网络、支付或取消订阅等需要人工确认的节点暂停。开始前必须先完成预检。
- 资源管理可以新增／编辑可用邮箱与信用卡；占用中、已使用、隔离资源保持只读；卡号、CVC、邮箱密码与 token 不会回显。
- CLI 和 `start.bat` 仍可用于无桌面环境；桌面控制台与 CLI 共用同一套工作流、资源租约和 SQLite 执行履历。

## Windows 桌面版

GitHub Releases 会提供 `GPTAutomation-Setup-<version>.exe` 安装包。安装后，
桌面控制台可以完成任务参数、IP／浏览器策略、邮箱与卡片资源、READY 账户、
运行记录和人工接管操作；真实任务仍由同一个 `RegistrationWorkflow` 执行。

首次启动会在当前用户的 `%LOCALAPPDATA%\GPTAutomation` 创建数据目录和 `.env`，
敏感输入只保存在本机。开发者发布新的 `v*` 标签后，GitHub Actions 自动构建
安装包并创建 Release；客户端启动时检查最新 Release，发现新版本会提示下载并
运行安装包完成更新。

本地构建 Windows 安装包：

```powershell
.\packaging\build_windows.ps1
```

脚本需要 Python 3.12、Inno Setup 和项目依赖；没有 Inno Setup 时仍会生成
`dist/GPTAutomation/` 的可运行目录。

发布新版本时先更新根目录 `VERSION`，再创建同名标签并推送：

```powershell
git tag v0.3.3
git push origin main v0.3.3
```

GitHub Actions 会校验标签与 `VERSION` 一致，构建并把
`GPTAutomation-Setup-<version>.exe` 附加到对应 Release。

## 核心结构

```text
gpt_automation/
├── workflow/
│   ├── engine.py              # 唯一业务入口 RegistrationWorkflow
│   ├── account_flow.py        # 唯一账户创建编排
│   ├── models.py              # 输入、结果、步骤与阶段类型
│   ├── interaction_policy.py  # 辅助与无人值守模式
│   └── runtime_state.py       # checkpoint 与非幂等动作桥接
├── state/
│   └── store.py               # SQLite session、动作日志、资源租约
├── orchestrator.py            # 批次、网络、输入资源和结果汇总
├── phases/                    # 页面动作与后置验证
├── network/
│   └── strategy.py            # 手动／自动 IP 的统一生命周期
├── verification/              # 邮箱验证码读取
└── utils/
    └── input_manager.py       # 事务式邮箱／卡片配对
```

详细状态约束见 [docs/workflow-state-machine.md](docs/workflow-state-machine.md) ，当前开发进度见 [ROADMAP.md](ROADMAP.md) 。

## 验证

```bash
python -m pytest -q
```

真实支付和取消订阅测试会产生外部副作用，必须使用单账户、人工监控，并在执行前单独确认。

## 浏览器后端

本项目支持两种浏览器执行后端，共用同一套 `RegistrationWorkflow`：

- `cloak`（默认）：CloakBrowser（Chromium + 持久 profile）。
- `firefox_private`：Firefox 隐私模式（每账户全新进程与非持久会话，关闭后不保留登录态）。

跨后端开通会员：创建账户与开通会员是两次独立任务，后端可以不同（Cloak→Firefox、Firefox→Cloak、同后端均可）。跨后端只传递加密账户凭据与业务状态；每次新开通都会创建新的 session 并记录浏览器后端，恢复 session 时沿用其原后端。开通前必须完成目标账户身份一致性验证，支付结果未知时禁止换后端重试。

选择方式：`start.bat` 启动器菜单可选浏览器；或设置环境变量 `BROWSER_BACKEND=firefox_private` 后直接运行 `python -m gpt_automation.orchestrator`。

安装 Firefox 运行时：

```powershell
.\.venv\Scripts\python.exe -m playwright install firefox
```
