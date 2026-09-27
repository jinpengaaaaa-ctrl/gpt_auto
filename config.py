import os

# ==========================================
# 0. Load .env file (if exists)
# ==========================================
try:
    from dotenv import load_dotenv
    _ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(_ENV_FILE):
        load_dotenv(_ENV_FILE, override=False)
except ImportError:
    # Fallback: manual .env parsing if python-dotenv is not installed
    _ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(_ENV_FILE):
        with open(_ENV_FILE, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                # Strip inline comments (anything after the value that follows " #")
                _comment_idx = _line.find(" #")
                if _comment_idx != -1:
                    _line = _line[:_comment_idx].strip()
                # Strip export prefix
                if _line.startswith("export "):
                    _line = _line[7:].strip()
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip().strip('"').strip("'")
                if _key and _key not in os.environ:
                    os.environ[_key] = _val

# ==========================================
# 0.1 Base Directory
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================
# 1. 路径管理
# ==========================================
INPUT_CSV = "emails.csv"         # 输入流：包含邮箱、密码、卡号、日本地址等 12 列数据
OUTPUT_CSV = os.path.join("accounts", "results.txt")  # 最终结果（已开通账户）：帐号----密码----2FA----邮箱密码
PENDING_FILE = os.path.join("accounts", "accounts_pending.txt")  # 中间产物：已创建未开通账户（明文四列）
SKIPPED_FILE = os.path.join("accounts", "accounts_skipped.txt")  # 连续模式跳过邮箱（人工待处理队列，非黑名单）
HELD_FILE = os.path.join("accounts", "accounts_held.txt")  # 开通失败待处理账户（明文四列+原因，非黑名单）  # 连续模式跳过邮箱（人工待处理队列，非黑名单）
BATCH_REPORT_DIR = os.path.join("runtime", "reports")  # 每批次独立的诊断错误报告目录
OFFER_REPORT_DIR = os.path.join(BASE_DIR, "runtime", "offer_reports")  # 创建账户 Offer 验收 HTML 报告目录（绝对路径）
CHECKOUT_OFFER_REPORT_DIR = os.path.join(BASE_DIR, "runtime", "checkout_offer_reports")  # 开通会员无免费会员 HTML 报告目录（绝对路径）
TMP_DIR = os.path.join(BASE_DIR, "TMP")  # 统一临时目录：测试/运行临时产物集中存放，可整体清理
os.makedirs(TMP_DIR, exist_ok=True)
PROFILE_DIR = os.path.join(BASE_DIR, "profiles")   # 独立持久化浏览器环境根目录
EVIDENCE_DIR = os.path.join(BASE_DIR, "evidence")  # 熔断机制下的状态截图归档目录
IP_HISTORY_FILE = os.path.join(BASE_DIR, "runtime", "ip_history.json")  # IP 使用诊断账本（项目内）
CSV_FIELDS = ["email", "email_pwd", "name", "age",
              "card_no", "card_expiry", "card_cvc",
              "zip_code", "pref", "city", "addr1", "addr2"]  # 输入 CSV 表头

# ==========================================
# 1.5 Input Management (Feature: 1:1 email:card tracking)
# ==========================================
# Input file directory
INPUT_DIR = os.path.join(BASE_DIR, "input")

# Email file (---- separated: email----password----client_id----refresh_token)
EMAIL_FILE = os.path.join(INPUT_DIR, "email.txt")

# Cards file (CSV: card_no,card_expiry,card_cvc)
CARDS_FILE = os.path.join(INPUT_DIR, "cards.csv")
CARDS_PASSED_FILE = os.path.join("accounts", "cards_passed.csv")
CARDS_DECLINED_FILE = os.path.join("accounts", "cards_declined.csv")
CHECKOUT_CARD_REPORT_DIR = os.path.join(BASE_DIR, "runtime", "checkout_reports")

# Address file (CSV: zip_code,pref,city,addr1,addr2)
ADDRESS_JP_FILE = os.path.join(INPUT_DIR, "address_jp.csv")

# Used tracking files
USED_EMAIL_FILE = os.path.join(INPUT_DIR, "used_email.txt")
USED_CARDS_FILE = os.path.join(INPUT_DIR, "used_cards.txt")

# Local workflow checkpoints and resource leases. The database stores only
# fingerprints and state metadata; input secrets remain in INPUT_DIR.
STATE_DB_PATH = os.path.join(BASE_DIR, "runtime", "workflow_state.db")
CREDENTIAL_STORE_DIR = os.path.join(BASE_DIR, "runtime", "credentials")
OUTPUT_DIR = os.path.join(BASE_DIR, "runtime", "exports")
EXECUTION_LOCK_DIR = os.path.join(BASE_DIR, "runtime", "session_locks")

# ==========================================
# 2. OpenAI 页面定位器 (Selectors)
# ==========================================
SELECTORS = {
    "reg_email_input": "input[name='email']",
    "reg_password_input": "input[name='password']",
    "age_input": "input[name='age']",
    "name_input": "input[name='name']",

    # 熔断核心：检测首页是否存在免费特惠按钮
    "free_offer_btn": "button:has-text('無料オファー'), button:has-text('オファーを受け取る'), button:has-text('Free offer'), button:has-text('Receive offer')",

    # 定价页确认（支付页金额判定，探查确认的真实结构）
    "upgrade_entry_btn": "button:has-text('アップグレードする'), button:has-text('プランをアップグレード'), button:has-text('Upgrade'), button:has-text('プランをアップグレードする')",
    "target_plan_btn": "button[data-testid='select-plan-button-plus-upgrade']",
    "plan_card_container": "[data-testid='plus-pricing-modal-column'], div:has(button[data-testid='select-plan-button-plus-upgrade']), div:has(button:has-text('Plus にアップグレード'))",
    "target_plan_price": "[data-testid='plus-pricing-column-cost']",
    # 套餐页导航（单文本 selector，避免 CloakBrowser 合并 has-text 误判）
    "free_offer_btn_exact": "button:has-text('オファーを受け取る')",
    "upgrade_entry_btn_exact": "button:has-text('アップグレードする')",
    # 主页免费入口：右上角/左下角「無料オファー」一类入口（单文本，CloakBrowser 兼容）
    "free_offer_banner_exact": "button:has-text('無料オファー')",
    "pricing_page_indicator": "[data-testid='pricing-modal-plan-grid']",
    "plan_badge": "[data-testid='pricing-column-badge']",

    # Checkout 实付金额二次确认（阶段三）。
    # 真实 DOM 结构待探针脚本输出后填写（逗号分隔多个 selector）：容器只读
    # 「今天应付/首期」金额，不能命中续费价或免责声明。为空字符串时二次门禁
    # 保持跳过并记录日志，避免在真实结构未确认前误拦或误放行。
    "checkout_amount_containers": "",
    "checkout_amount_label": "text=\u4eca\u65e5\u306e\u652f\u6255\u3044, text=\u672c\u65e5\u306e\u304a\u652f\u6255\u984d, text=Today's total, text=本日の合計, text=Due today, text=初回支払い",
    "checkout_submit_btn": "button[type='submit'], button:has-text('サブスクリプションを登録する'), button:has-text('Subscribe')",

    # 安全加固与 2FA
    "avatar_btn": "[data-testid='accounts-profile-button'], span.header-wordmark",
    "settings_btn": "[data-testid='settings-button'], a:has-text('設定'), a:has-text('Settings'), button:has-text('設定'), button:has-text('Settings')",
    "security_tab": "div:has-text('セキュリティ'), div:has-text('Security'), .security-pane",
    "mfa_toggle": "input[type='checkbox'].mfa-authenticator-switch, button:has-text('2段階認証'), button:has-text('Two-factor')",
    "mfa_cant_scan_link": "a:has-text('読み込みで問題が発生した場合'), a:has-text('Trouble scanning'), a:has-text('問題')",
    "mfa_secret_text": ".mfa-secret-key-display, [data-testid='secret-key'], code.verification-secret",
    "mfa_verify_input": "input[placeholder*='コード'], input[placeholder*='code'], input[placeholder*='Code'], input[maxLength='6']",

    # MFA 邮箱验证码弹窗 (#9)
    "mfa_email_input": "input[maxLength='6']",
    "mfa_email_verify_btn": "button:has-text('確認'), button:has-text('Verify'), button:has-text('続行')",

    # Stripe 信用卡开通与退订
    "stripe_card_input": "#card-number-element input, [data-stripe-element='cardNumber'] input",
    "stripe_expiry_input": "#card-expiry-element input, [data-stripe-element='cardExpiry'] input",
    "stripe_cvc_input": "#card-cvc-element input, [data-stripe-element='cardCvc'] input",
    "stripe_submit_btn": "button:has-text('\u30b5\u30d6\u30b9\u30af\u30ea\u30d7\u30b7\u30e7\u30f3\u3092\u767b\u9332'), button:has-text('\u7121\u6599\u30aa\u30d5\u30a1\u30fc\u3092\u53d7\u3051\u53d6\u308b'), button:has-text('Subscribe'), button[type='submit']",
    "manage_sub_btn": "button:has-text('キャンセルする'), button:has-text('Cancel plan'), button:has-text('管理する'), button:has-text('Manage subscription'), button:has-text('Manage')",
    "cancel_sub_link": "a:has-text('サブスクリプションをキャンセルする'), a:has-text('Cancel subscription'), a:has-text('キャンセル')",
    "dont_use_go_btn": "button:has-text('利用しない'), button:has-text('Cancel anyway'), button:has-text('キャンセルする')",
    "final_cancel_btn": "button:has-text('キャンセルする'), button:has-text('Confirm cancel'), button:has-text('確認')",

    # Stripe 日本地址 (#13 P0)
    "stripe_zip": "input[name='postalCode'], input[placeholder*='郵便'], input[name='zip'], input[autocomplete='postal-code'], input[name='billingPostalCode']",
    "stripe_pref": "select[name='prefecture'], select[name='state'], select[name='billingState'], input[name='prefecture'], input[name='state']",
    "stripe_city": "input[name='city'], input[placeholder*='市区'], input[autocomplete='address-level2'], input[name='billingCity']",
    "stripe_addr1": "input[name='line1'], input[name='address1'], input[placeholder*='住所1'], input[autocomplete='address-line1'], input[name='billingAddressLine1']",
    "stripe_addr2": "input[name='line2'], input[name='address2'], input[placeholder*='住所2'], input[autocomplete='address-line2'], input[name='billingAddressLine2']",
    "stripe_name": "input[name='name'], input[name='billingName'], input[placeholder*='名前'], input[placeholder*='Name'], input[autocomplete='name']",

    # 退订后状态确认 (#16)
    "plus_indicator": "text=/Plus/, .plus-badge",
    "cancelled_indicator": "text=/キャンセル/, text=/cancel/i, text=/解約/, text=/cancelled/i",
}

# ==========================================
# 2.5 Stripe 拒绝模式文本（去重：phase3 / stripe 共享）
# ==========================================
STRIPE_REJECTION_PATTERNS = [
    "text=決済手段を認証できませんでした",
    "text=別の決済手段を選択してください",
    "text=認証できません",
    "text=別の決済手段",
    "text=Your payment method could not be verified",
    "text=payment method was declined",
    "text=カードが拒否されました",
    "text=支払いが拒否されました",
    "text=お支払いが拒否されました",
    "text=別のカードをお試しください",
    "text=Your card was declined",
    "text=Card declined",
]

# Stripe 成功确认文本
STRIPE_SUCCESS_TEXTS = [
    "text=ありがとう",
    "text=Thank you",
    "text=登録が完了",
    "text=subscription confirmed",
]

# ==========================================
# 2.6 日本姓名数据（去重：orchestrator / phase3 共享）
# ==========================================
JP_SURNAMES = [
    "鈴木", "佐藤", "高橋", "田中", "伊藤", "山本", "中村", "小林", "加藤", "吉田",
    "山田", "佐々木", "山口", "松本", "井上", "木村", "林", "斎藤", "清水", "山崎",
]

JP_GIVENS = [
    "太郎", "次郎", "三郎", "健一", "健二", "健三", "翔太", "雄太", "大輔", "康介",
    "花子", "幸子", "和美", "陽子", "智子", "裕子", "久美子", "直美", "愛", "彩",
]

# ==========================================
# 3. IMAP Configuration
# ==========================================
IMAP_CONFIG = {
    "timeout": 60,              # Max wait for verification code (seconds)
    "poll_interval": 5.0,       # Poll interval (seconds)
    "fallback_to_manual": True, # Fall back to manual input on timeout
}

# ==========================================
# 3.5 Outlook Verification — Polling Parameters (Feature #10)
# ==========================================
# NOTE: Outlook 验证码读取通过 Microsoft Graph API / IMAP XOAUTH2 实现，
# OAuth2 凭证（client_id + refresh_token）来自 email.csv 每行数据。
# 此处仅配置 timeout / poll_interval，供 helpers.py 中的 phase_handle_email_code 使用。
OUTLOOK_CONFIG = {
    "timeout": 60.0,         # 邮件到达最大等待时间（秒）
    "poll_interval": 3.0,    # 轮询间隔（秒）
}

# ==========================================
# 3.6 邮箱验证码 3 轮取码 + 再送信
# ==========================================
EMAIL_CODE_CONFIG = {
    "round1_timeout": float(os.environ.get("EMAIL_CODE_ROUND1_TIMEOUT_S", "60")),
    "round2_timeout": float(os.environ.get("EMAIL_CODE_ROUND2_TIMEOUT_S", "45")),
    "round3_timeout": float(os.environ.get("EMAIL_CODE_ROUND3_TIMEOUT_S", "45")),
    "max_resend": int(os.environ.get("EMAIL_CODE_MAX_RESEND", "2")),
    "poll_interval": float(os.environ.get("EMAIL_CODE_POLL_INTERVAL_S", "3")),
    "enable_resend": os.environ.get("EMAIL_CODE_ENABLE_RESEND", "true").lower()
    in ("true", "1", "yes"),
}

PASSWORD_CONTINUATION_PROBE_S = float(
    os.environ.get("PASSWORD_CONTINUATION_PROBE_S", "8")
)

# ==========================================
# 4. Development Mode
# ==========================================
# Set to True to use local HTML fixtures instead of live ChatGPT
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")

MAX_ACCOUNTS = int(os.environ.get("MAX_ACCOUNTS", "0"))  # 0 = unlimited

# ==========================================
# 4.5 ADSL ↔ Xray 节点映射表 + Xray 直控配置 + IP 检测配置
# ==========================================

# ADSL ↔ Xray 节点映射表 (ikuai 重启后对应的 Xray 目标节点)
ADSL_XRAY_MAP = {
    "adsl3": "adsl6-reality",
    "adsl4": "adsl7-reality",
    "adsl5": "adsl8-reality",
    "adsl6": "adsl9-reality",
    "adsl7": "adsl10-reality",
}

# Xray 直控配置：XRAY_BASE_DIR 同时确定 xray.exe、JSON 配置目录、v2rayN DB
_XRAY_BASE_DIR = os.environ.get("XRAY_BASE_DIR", ".")
XRAY_CONFIG = {
    "base_dir": _XRAY_BASE_DIR,
    "executable": os.path.join(_XRAY_BASE_DIR, "bin", "xray", "xray.exe"),
    "config_dir": os.path.join(_XRAY_BASE_DIR, "guiConfigs"),
    "db_path": os.path.join(_XRAY_BASE_DIR, "guiConfigs", "guiNDB.db"),
    "startup_delay": float(os.environ.get("XRAY_STARTUP_DELAY", "3.0")),
}

# IP 检测配置
IP_CHECK_CONFIG = {
    "timeout": float(os.environ.get("IP_CHECK_TIMEOUT", "8.0")),
    "max_retries": int(os.environ.get("IP_CHECK_MAX_RETRIES", "3")),
}

# ==========================================
# 5. iKuai 路由器配置
# ==========================================
# 端口分流 → 新建连接数 模式下，每次 CloakBrowser 重启自动随机分配 WAN。
# 以下为 API 配置，#11A 模式启用。
# 可通过环境变量 IKUAI_USERNAME / IKUAI_PASSWORD / IKUAI_API_URL 覆盖。

# ADSL VLAN ID 映射表：用户只填名字，代码自动匹配 VLAN ID
ADSL_MAP = {
    "adsl3": "5",
    "adsl4": "6",
    "adsl5": "7",
    "adsl6": "8",
    "adsl7": "9",
}

_TARGET_WAN_NAME = os.environ.get("IKUAI_TARGET_WAN_NAME", "adsl3")
_TARGET_WAN_ID = ADSL_MAP.get(_TARGET_WAN_NAME)
if _TARGET_WAN_ID is None:
    raise ValueError(
        f"Unknown IKUAI_TARGET_WAN_NAME='{_TARGET_WAN_NAME}' — must be one of: {list(ADSL_MAP.keys())}"
    )

# 一致性检查：iKuai target_wan_name 必须同时存在于 ADSL_MAP 和 ADSL_XRAY_MAP
if _TARGET_WAN_NAME not in ADSL_XRAY_MAP:
    raise ValueError(
        f"Missing Xray mapping for: '{_TARGET_WAN_NAME}' in ADSL_XRAY_MAP. "
        f"Valid: {list(ADSL_XRAY_MAP.keys())}"
    )

IKUAI_CONFIG = {
    "api_url": os.environ.get("IKUAI_API_URL", "http://192.168.1.1"),
    "username": os.environ.get("IKUAI_USERNAME", ""),
    "password": os.environ.get("IKUAI_PASSWORD", ""),
    "endpoints": {
        "wan_status": "/api/wan/status",          # WAN 口状态查询
        "change_ip":  "/api/wan/change",          # 指定 WAN 口重拨
    },
    "target_wan_name": _TARGET_WAN_NAME,
    "target_wan_id":   _TARGET_WAN_ID,
    "redial_delay": float(os.environ.get("IKUAI_REDIAL_DELAY", "5.0")),
}

# ==========================================
# 6. IP 轮换检测配置
# ==========================================
# 可通过环境变量 IP_ROTATION_MODE 覆盖 rotation_mode。
IP_ROTATION_CONFIG = {
    "max_retries": 10,
    "retry_delay": 5.0,
    "strict_mode": os.environ.get("IP_STRICT_MODE", "true").lower() in ("true", "1", "yes"),
    "ip_history_max": 1000,              # ip_history.json 最大记录数
    "rotation_mode": os.environ.get("IP_ROTATION_MODE", "ikuai-single"),
}

# ==========================================
# 7. 代理配置（Socks5/HTTP 动态住宅代理）
# ==========================================
# 可通过环境变量 PROXY_ENABLED / PROXY_SERVER / PROXY_USERNAME / PROXY_PASSWORD 覆盖。
PROXY_CONFIG = {
    "enabled": os.environ.get("PROXY_ENABLED", "").lower() in ("true", "1", "yes"),
    "server": os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:1080"),
    "username": os.environ.get("PROXY_USERNAME", ""),
    "password": os.environ.get("PROXY_PASSWORD", ""),
    "dynamic_api_url": os.environ.get("PROXY_DYNAMIC_API_URL", ""),
    "host": os.environ.get("PROXY_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PROXY_PORT", "10808")),
}

# ── PROXY_ENABLED 自动推导（不写死，根据模式动态决定） ──
# manual-hotspot 模式不需要代理，自动覆写 PROXY_CONFIG["enabled"] = False
# 其余模式保持 .env 中 PROXY_ENABLED 的原始值
if IP_ROTATION_CONFIG.get("rotation_mode") == "manual-hotspot":
    PROXY_CONFIG["enabled"] = False

# ==========================================
# 8. 全局异常处理 / 重试配置 (Feature #18)
# ==========================================
RETRY_CONFIG = {
    "max_attempts": 2,                  # 每步骤最大尝试次数
    "base_delay": 1.0,                  # 第一次重试前等待（秒）
    "max_delay": 30.0,                  # 指数退避上限
    "exponential_base": 2.0,            # 退避基数（2 → 1s, 2s, 4s, ...）
    "jitter": 0.5,                      # 随机抖动比例（0~1）
    "csv_flush_per_row": True,          # 每个账号完成后立即 flush CSV
    "screenshot_on_error": True,        # 失败时自动截图
}

# ==========================================
# 8.5 CloakBrowser 启动预热探活
# 免费版二进制的崩溃高发于启动初期；拿完出口 IP 后先做一轮
# 心跳探活，预热期内死亡的会话按启动失败处理，不进入业务阶段。
# ==========================================
CLOAKBROWSER_WARMUP = {
    "enabled": True,       # False 关闭预热（调试用）
    "duration_s": 8.0,     # 总预热窗口（秒）
    "interval_s": 2.0,     # 心跳间隔（秒）
}

# ==========================================
# 9. CAPTCHA 提示音 (Feature #5)
# ==========================================
CAPTCHA_BEEP = {
    "frequency": 880,       # Hz
    "duration_ms": 600,     # 单次蜂鸣时长
    "repeat": 1,            # 连响次数
    "pause_ms": 300,        # 连响之间间隔
    "sound_file": "sounds/bach_cello_suite.wav",  # relative to BASE_DIR; WAV wins over Beep
    "fallback_message": "检测到验证码（CAPTCHA），请在浏览器中手动完成。",
}

# ==========================================
# 9.5 处理完成提示音
# ==========================================
COMPLETION_SOUND = {
    "sound_file": "sounds/sakura.wav",   # relative to BASE_DIR
    "repeat": 2,                          # 连播次数
    "pause_ms": 500,                       # 间隔
}

# ==========================================
# 9.6 ChatGPT 帐号密码
# ==========================================
# 旧诊断入口的可选兼容值。生产工作流按 session 生成并加密保存独立密码。
# 禁止在仓库中设置公开默认密码。
CHATGPT_PASSWORD = os.environ.get("CHATGPT_PASSWORD", "")
# 每个新 session 生成的 ChatGPT 密码长度（默认 18；最小 12，仅字母数字）。
CHATGPT_PASSWORD_LENGTH = int(os.environ.get("CHATGPT_PASSWORD_LENGTH", "18"))

# ==========================================
# 9.7 创建账户 Offer 资格验收配置
# ==========================================
# 目标优惠由配置决定，不写死在业务代码里；修改后只影响新批次，
# 已启动 session 使用启动时固定的策略快照（见 workflow_evidence）。
# 判定仍然要求：chatgpt.com 域名 + 真实 pricing 页 + 单一且精确匹配的
# promo_campaign；这三条安全规则不随配置放宽。
OFFER_ELIGIBILITY_CONFIG = {
    "target_promo_campaign": "plus-1-month-free",
}

# ==========================================
# 10. 手动 IP 切换模式（手机热点 + 飞行模式）
# ==========================================
MANUAL_MODE_CONFIG = {
    "prompt_message": "📱 请切换手机飞行模式（开→关），等待网络恢复后按 Enter 继续...",
    "beep_repeat": 3,                  # 提示音连响次数
    "beep_pause_ms": 500,              # 提示音间隔（ms）
    "ip_check_timeout": float(os.environ.get("MANUAL_IP_CHECK_TIMEOUT_S", "8.0")),  # 手动切换后单次 IP 检测超时（秒）
    # 自动轮询检测：无需人工按键，按固定间隔检测出口 IP 是否变化；
    # 断网时持续自动检测，不设内部重试次数
    "poll_interval_s": float(os.environ.get("MANUAL_IP_POLL_INTERVAL_S", "10.0")),  # 检测间隔（秒）
    "poll_window_s": float(os.environ.get("MANUAL_IP_POLL_WINDOW_S", "540.0")),    # 无变化确认窗口（秒，默认 9 分钟）
}

# ==========================================
# 11. 配置验证 — 启动时即发现错误配置
# ==========================================
_config_errors: list[str] = []

# Timeout / interval 必须为正数
for _name, _val in [
    ("IMAP_CONFIG.timeout", IMAP_CONFIG["timeout"]),
    ("IMAP_CONFIG.poll_interval", IMAP_CONFIG["poll_interval"]),
    ("OUTLOOK_CONFIG.timeout", OUTLOOK_CONFIG["timeout"]),
    ("OUTLOOK_CONFIG.poll_interval", OUTLOOK_CONFIG["poll_interval"]),
    ("EMAIL_CODE_CONFIG.round1_timeout", EMAIL_CODE_CONFIG["round1_timeout"]),
    ("EMAIL_CODE_CONFIG.round2_timeout", EMAIL_CODE_CONFIG["round2_timeout"]),
    ("EMAIL_CODE_CONFIG.round3_timeout", EMAIL_CODE_CONFIG["round3_timeout"]),
    ("EMAIL_CODE_CONFIG.poll_interval", EMAIL_CODE_CONFIG["poll_interval"]),
    ("IP_CHECK_CONFIG.timeout", IP_CHECK_CONFIG["timeout"]),
    ("IP_ROTATION_CONFIG.retry_delay", IP_ROTATION_CONFIG["retry_delay"]),
    ("RETRY_CONFIG.base_delay", RETRY_CONFIG["base_delay"]),
    ("RETRY_CONFIG.max_delay", RETRY_CONFIG["max_delay"]),
    ("MANUAL_MODE_CONFIG.ip_check_timeout", MANUAL_MODE_CONFIG["ip_check_timeout"]),
]:
    if not isinstance(_val, (int, float)) or _val <= 0:
        _config_errors.append(f"{_name} must be positive, got {_val!r}")

# 手动 IP 轮询窗口必须覆盖至少一个检测间隔
if (
    not isinstance(MANUAL_MODE_CONFIG.get("poll_interval_s"), (int, float))
    or not isinstance(MANUAL_MODE_CONFIG.get("poll_window_s"), (int, float))
    or MANUAL_MODE_CONFIG["poll_interval_s"] <= 0
    or MANUAL_MODE_CONFIG["poll_window_s"] <= 0
    or MANUAL_MODE_CONFIG["poll_window_s"]
    < MANUAL_MODE_CONFIG["poll_interval_s"]
):
    _config_errors.append(
        "MANUAL_MODE_CONFIG.poll_interval_s and poll_window_s must be "
        "positive, and poll_window_s >= poll_interval_s"
    )

# 再送信次数必须 >= 0 且不超过 3（取码轮次 = max_resend + 1）
if (
    not isinstance(PASSWORD_CONTINUATION_PROBE_S, (int, float))
    or PASSWORD_CONTINUATION_PROBE_S <= 0
):
    _config_errors.append(
        "PASSWORD_CONTINUATION_PROBE_S must be positive, got "
        f"{PASSWORD_CONTINUATION_PROBE_S!r}"
    )

if (
    not isinstance(EMAIL_CODE_CONFIG["max_resend"], int)
    or EMAIL_CODE_CONFIG["max_resend"] < 0
    or EMAIL_CODE_CONFIG["max_resend"] > 3
):
    _config_errors.append(
        "EMAIL_CODE_CONFIG.max_resend must be 0..3, got "
        f"{EMAIL_CODE_CONFIG['max_resend']!r}"
    )

# 重试次数必须 >= 1
for _name, _val in [
    ("IP_CHECK_CONFIG.max_retries", IP_CHECK_CONFIG["max_retries"]),
    ("IP_ROTATION_CONFIG.max_retries", IP_ROTATION_CONFIG["max_retries"]),
    ("RETRY_CONFIG.max_attempts", RETRY_CONFIG["max_attempts"]),
]:
    if not isinstance(_val, int) or _val < 1:
        _config_errors.append(f"{_name} must be >= 1, got {_val!r}")

# ChatGPT 密码长度必须 >= 12（策略下限）
if (
    not isinstance(CHATGPT_PASSWORD_LENGTH, int)
    or CHATGPT_PASSWORD_LENGTH < 12
):
    _config_errors.append(
        f"CHATGPT_PASSWORD_LENGTH must be >= 12, got {CHATGPT_PASSWORD_LENGTH!r}"
    )

# 目标 promo campaign：必须是非空字符串，且不允许混入 URL 分隔符/空白，
# 防止把多个活动或带参数的畸形值当作单一目标。
_target_campaign = OFFER_ELIGIBILITY_CONFIG.get("target_promo_campaign", "")
if (
    not isinstance(_target_campaign, str)
    or not _target_campaign.strip()
    or _target_campaign != _target_campaign.strip()
    or any(ch in _target_campaign for ch in (" ", "&", "=", "?", "#", ","))
):
    _config_errors.append(
        "OFFER_ELIGIBILITY_CONFIG.target_promo_campaign must be a single "
        f"non-empty campaign token without separators, got {_target_campaign!r}"
    )

# 代理端口范围
_proxy_port = PROXY_CONFIG.get("port", 0)
if not isinstance(_proxy_port, int) or not (1 <= _proxy_port <= 65535):
    _config_errors.append(f"PROXY_CONFIG.port must be 1-65535, got {_proxy_port!r}")

# jitter 范围 0~1
_jitter = RETRY_CONFIG.get("jitter", 0)
if not isinstance(_jitter, (int, float)) or not (0 <= _jitter <= 1):
    _config_errors.append(f"RETRY_CONFIG.jitter must be 0~1, got {_jitter!r}")

if _config_errors:
    _msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in _config_errors)
    raise ValueError(_msg)
