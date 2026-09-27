#!/usr/bin/env python3
"""
ChatGPT 自动注册 — 交互式启动器。

由 start.bat 调用，负责显示中文菜单、收集用户选择、
设置环境变量后启动 orchestrator。
"""

import os
import subprocess
import sys

# 确保 UTF-8 输出
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# 切到项目根目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

# Load the project .env before evaluating launcher-only feature flags.
import config  # noqa: E402,F401

IP_MODE_OPTIONS = [
    "手动（手机热点 + 飞行模式切换；每 10 秒自动检测 IP，变化即自动放行，无需按键）",
    "自动（iKuai 路由器 + Xray 代理）",
]
IP_MODE_MAP = {1: "manual-hotspot", 2: "ikuai-single"}
DEFAULT_IP_CHOICE = 1
CHECKOUT_GOAL_OPTIONS = [
    "完整开通（登录 → 套餐 → 支付 → 取消订阅）",
    "仅验证套餐页（登录 → 套餐选择画面，不进入支付）",
]
CHECKOUT_GOAL_MAP = {1: "complete", 2: "pricing_page"}
BROWSER_BACKEND_OPTIONS = [
    "CloakBrowser（现有 Chromium + 持久 profile）",
    "Firefox 隐私模式（每账户全新非持久会话）",
]
BROWSER_BACKEND_MAP = {1: "cloak", 2: "firefox_private"}
DEFAULT_BROWSER_CHOICE = 1
INTERACTION_MODE_OPTIONS = [
    "连续执行（账户步骤遇阻后自动跳过，继续下一个账户）",
    "标准模式（失败时等待人工确认）",
]
INTERACTION_MODE_MAP = {1: "continue_on_blocked", 2: "assisted"}
DEFAULT_INTERACTION_MODE_CHOICE = 1
TASK_TYPE_OPTIONS = [
    "创建账号（注册 + 密码 + 2FA，产出待开通账户）",
    "开通会员（从已创建账户中选择，完成支付开通）",
]
TASK_TYPE_MAP = {1: "account_provision", 2: "checkout_fulfillment"}
SAMPLE_EMAILS = {"account@example.com", "account@example.com"}
SAMPLE_CARD_NUMBERS = {"4111111111111111", "5500000000000004"}
RECOVERY_SECTION_ENABLED = os.environ.get(
    "RECOVERY_SECTION_ENABLED",
    "false",
).lower() in {"true", "1", "yes"}
FIREFOX_BACKEND_ENABLED = os.environ.get(
    "FIREFOX_BACKEND_ENABLED",
    "false",
).lower() in {"true", "1", "yes"}


def build_recovery_command(action: str, session_id: str) -> list[str]:
    """Dispatch recovery actions through the canonical Python CLI."""
    if action == "resume":
        flag = "--resume"
    elif action == "inspect":
        flag = "--inspect-session"
    elif action == "inspect-profile":
        flag = "--inspect-profile"
    elif action == "abandon":
        flag = "--abandon-session"
    else:
        raise ValueError(f"unsupported recovery action: {action}")
    return [sys.executable, "-u", "-m", "gpt_automation", flag, session_id]


def choose_recovery_action() -> tuple[str, str] | None:
    """Offer existing sessions before asking for a new batch configuration."""
    # The launcher menu is intentionally interactive.  Test runners and
    # scheduled/background invocations must fall through to the normal batch
    # configuration instead of consuming their own stdin as menu choices.
    if not getattr(sys.stdin, "isatty", lambda: False)():
        return None

    import config
    from gpt_automation.state import (
        RecoveryCapability,
        SessionCatalog,
        StateStore,
        render_session_candidate,
    )

    state_store = StateStore(config.STATE_DB_PATH)
    state_store.initialize()
    catalog = SessionCatalog(
        state_store,
        input_dir=config.INPUT_DIR,
        profile_dir=config.PROFILE_DIR,
        lock_dir=config.EXECUTION_LOCK_DIR,
    )
    candidates = catalog.list_candidates()
    unmapped = catalog.list_unmapped_profiles()
    if not candidates and not unmapped:
        return None

    print("\n  检测到历史 session：")
    for index, candidate in enumerate(candidates, start=1):
        print(f"    {render_session_candidate(candidate, index)}")
    if unmapped:
        print("    L) 查看未映射旧 profile")
    print("    N) 开始新批次")
    print("    Q) 退出")

    while True:
        raw = input("\n  请选择 session 编号、N 或 Q（默认 N）：").strip().lower()
        if raw in {"", "n"}:
            return None
        if raw == "q":
            return ("quit", "")
        if raw == "l" and unmapped:
            print("\n  未映射旧 profile：")
            for profile in unmapped:
                print(f"    profile_{profile.profile_id}  state={profile.state.value}")
            profile_id = input("  输入 profile 标识（不含 profile_ 前缀）：").strip()
            if any(separator in profile_id for separator in ("/", "\\", "..")):
                print("  profile 标识不合法。")
                continue
            return ("inspect-profile", profile_id)
        try:
            index = int(raw) - 1
            candidate = candidates[index]
        except (ValueError, IndexError):
            print("  无效选择。")
            continue

        if candidate.capability is RecoveryCapability.UNAVAILABLE:
            print("  该 session 当前不可进入，请先处理 profile 锁或缺失资源。")
            continue
        if candidate.capability is RecoveryCapability.RESUME:
            action = input("  [R] 继续工作流，[I] 仅进入查看（默认 R）：").strip().lower()
            return ("inspect" if action == "i" else "resume", candidate.session_id)
        return ("inspect", candidate.session_id)


def maybe_choose_recovery_action() -> tuple[str, str] | None:
    """Keep recovery available but hidden from the default launcher flow."""
    if not RECOVERY_SECTION_ENABLED:
        return None
    return choose_recovery_action()


def launcher_result_message(return_code: int) -> str:
    """Describe whether a batch ran, was blocked, or failed."""
    if return_code == 0:
        return "执行完毕。请查看 results.txt 确认结果。"
    if return_code == 2:
        return "批次已停止：存在被占用或待核验的资源，需要人工处理。"
    return f"执行失败（退出码：{return_code}）。"


def ask_choice(title: str, options: list[str], default: int = 1) -> int:
    """显示选项菜单，返回用户选择的编号（1-based）。"""
    print(f"\n  {title}")
    print()
    for i, opt in enumerate(options, 1):
        print(f"    {i}) {opt}")
    print()
    while True:
        raw = input(f"  请选择 (1-{len(options)}, 默认={default}): ").strip()
        if not raw:
            return default
        try:
            choice = int(raw)
            if 1 <= choice <= len(options):
                return choice
        except ValueError:
            pass
        print(f"  无效输入，请输入 1-{len(options)} 之间的数字。")


def select_account_ids(accounts, mode: str, count: int = 0, custom_indexes: tuple[int, ...] = ()) -> list[str]:
    """Map a launcher selection onto READY account ids.

    mode: "all"（全部）| "count"（按数量，超出池自动截断）| "custom"（按 1-based 编号，越界自动过滤）。
    """
    ids = [account.account_id for account in accounts]
    if mode == "all":
        return ids
    if mode == "count":
        return ids[: max(count, 0)]
    if mode == "custom":
        return [ids[index - 1] for index in custom_indexes if 1 <= index <= len(ids)]
    raise ValueError(f"unsupported selection mode: {mode}")


def detect_sample_input(
    run_scope: str,
    input_dir: str = "input",
    checkout_goal: str = "complete",
) -> list[str]:
    """检测 input 数据是否仍为示例或缺失；返回问题列表。

    - account_provision 只检测邮箱（不需要卡）；
    - checkout_fulfillment 的账号来自 READY 库存，不检测邮箱队列；
      完整开通（complete）再检测卡，仅验证套餐页（pricing_page）不要求卡。
    """
    problems: list[str] = []
    email_path = os.path.join(input_dir, "email.txt")
    if run_scope != "checkout_fulfillment":
        if not os.path.exists(email_path):
            problems.append("input/email.txt：缺失")
        else:
            with open(email_path, "r", encoding="utf-8") as fh:
                lines = [
                    ln.strip()
                    for ln in fh
                    if ln.strip() and "----" in ln
                ]
            if not lines:
                problems.append("input/email.txt：为空")
            elif any(ln.split("----", 1)[0].strip() in SAMPLE_EMAILS for ln in lines):
                problems.append("input/email.txt：仍是示例数据（account@example.com 等）")

    if run_scope == "checkout_fulfillment":
        pending_path = getattr(config, "PENDING_FILE", os.path.join("accounts", "accounts_pending.txt"))
        if os.path.exists(pending_path):
            with open(pending_path, "r", encoding="utf-8") as fh:
                p_lines = [ln.strip() for ln in fh if ln.strip() and "----" in ln]
            # 过滤表头
            data_plines = [ln for ln in p_lines if not ln.startswith("帐号") and not ln.startswith("email")]
            if data_plines and all(ln.split("----", 1)[0].strip() in {"first@example.com", "account@example.com", "account@example.com"} for ln in data_plines):
                problems.append("accounts/accounts_pending.txt：仍是示例数据；开通仍使用库存已注册账户，示例行不会被选中。")

    if run_scope == "checkout_fulfillment" and checkout_goal == "complete":
        cards_path = os.path.join(input_dir, "cards.csv")
        if not os.path.exists(cards_path):
            problems.append("input/cards.csv：缺失")
        else:
            with open(cards_path, "r", encoding="utf-8") as fh:
                rows = [ln.strip() for ln in fh if ln.strip()]
            data_rows = rows[1:]  # 跳过表头（与 input_manager 解析器一致）
            if not data_rows:
                problems.append("input/cards.csv：为空")
            else:
                sample_card_hit = False
                for row in data_rows:
                    parts = row.split(",")
                    if len(parts) >= 2 and parts[1].strip() in SAMPLE_CARD_NUMBERS:
                        sample_card_hit = True
                        break
                if sample_card_hit:
                    problems.append("input/cards.csv：仍是示例数据（测试卡号）")
    elif run_scope == "full":
        cards_path = os.path.join(input_dir, "cards.csv")
        if not os.path.exists(cards_path):
            problems.append("input/cards.csv：缺失")
        else:
            with open(cards_path, "r", encoding="utf-8") as fh:
                rows = [ln.strip() for ln in fh if ln.strip()]
            data_rows = rows[1:]  # 跳过表头（与 input_manager 解析器一致）
            if not data_rows:
                problems.append("input/cards.csv：为空")
            else:
                sample_card_hit = False
                for row in data_rows:
                    parts = row.split(",")
                    if len(parts) >= 2 and parts[1].strip() in SAMPLE_CARD_NUMBERS:
                        sample_card_hit = True
                        break
                if sample_card_hit:
                    problems.append("input/cards.csv：仍是示例数据（测试卡号）")
    return problems


def ask_account_ids(accounts) -> tuple[list[str], str]:
    """交互选择可开通账号；返回 (account_ids, 用户可读描述)。"""
    total = len(accounts)
    print(f"\n  当前可开通会员的账号：{total} 个")
    if total == 0:
        print("  暂无已创建账号，请先执行「创建账号」。")
        return [], ""
    choice = ask_choice(
        "账号选择",
        [f"全部开通（{total} 个）", "按数量开通（1 / 5 / 10 / 自定义）", "自选账号"],
        default=1,
    )
    if choice == 1:
        return select_account_ids(accounts, "all"), f"全部 {total} 个"
    if choice == 2:
        count_choice = ask_choice("开通数量", ["1 个", "5 个", "10 个", "自定义数量"], default=1)
        count = {1: 1, 2: 5, 3: 10}.get(count_choice)
        if count_choice == 4:
            raw = input("  请输入开通数量 (正整数): ").strip()
            try:
                count = int(raw)
                if count <= 0:
                    raise ValueError
            except ValueError:
                print("  无效输入，请输入正整数。")
                return [], ""
        if count > total:
            print(f"  当前只有 {total} 个可开通账号，将按全部处理。")
            count = total
        return select_account_ids(accounts, "count", count=count), f"{count} 个"
    print("\n  可开通账号列表：")
    for index, account in enumerate(accounts, 1):
        print(f"    {index}) {account.email}")
    raw = input("  请输入要开通的账号编号（逗号分隔，如 1,3,5）: ").strip()
    indexes: list[int] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            indexes.append(int(part))
        except ValueError:
            continue
    if not indexes:
        print("  未选择任何账号。")
        return [], ""
    return (
        select_account_ids(accounts, "custom", custom_indexes=tuple(indexes)),
        f"自选 {len(indexes)} 个",
    )


def mask_secret(secret: str) -> str:
    """Return a fixed mask so terminal output never reveals a secret or its length."""
    return "********" if secret else "(not set)"


def main() -> int:
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   ChatGPT 自动注册 — 批量启动器         ║")
    print("  ╚══════════════════════════════════════════╝")

    recovery_choice = maybe_choose_recovery_action()
    if recovery_choice is not None:
        action, session_id = recovery_choice
        if action == "quit":
            return 0
        result = subprocess.run(
            build_recovery_command(action, session_id),
            cwd=PROJECT_ROOT,
        )
        if result.returncode != 0:
            print(f"\n  [错误] session 操作失败。(code: {result.returncode})")
        input("\n  按 Enter 退出...")
        return result.returncode

    # Step 1: 任务类型
    task_choice = ask_choice(
        "[1] 请选择任务类型:",
        TASK_TYPE_OPTIONS,
        default=1,
    )
    run_scope = TASK_TYPE_MAP[task_choice]

    # Step 2: 模式专属参数
    account_ids: list[str] = []
    selected_display = "—"
    if run_scope == "checkout_fulfillment":
        from gpt_automation.inventory.service import RegisteredAccountService
        from gpt_automation.security.credential_store import CredentialStore
        from gpt_automation.state import StateStore

        inventory = RegisteredAccountService(
            state_store=StateStore(config.STATE_DB_PATH),
            credential_store=CredentialStore(config.CREDENTIAL_STORE_DIR),
            output_dir=config.OUTPUT_DIR,
        )
        account_ids, selected_display = ask_account_ids(
            inventory.list_accounts(statuses=("READY",))
        )
        if not account_ids:
            input("\n  按 Enter 退出...")
            return 1
        max_accounts = str(len(account_ids))
    else:
        count_choice = ask_choice(
            "[2] 请选择注册数量:",
            ["1 个（测试用）", "5 个", "10 个", "自定义"],
            default=1,
        )
        if count_choice == 4:
            while True:
                raw = input("  请输入注册数量 (正整数): ").strip()
                try:
                    custom_count = int(raw)
                    if custom_count > 0:
                        max_accounts = str(custom_count)
                        break
                    print("  请输入大于 0 的数字。")
                except ValueError:
                    print("  无效输入，请输入正整数。")
        else:
            max_accounts_map = {1: "1", 2: "5", 3: "10"}
            max_accounts = max_accounts_map[count_choice]

    # Step 3: IP 切换模式
    ip_choice = ask_choice(
        "[3] 请选择 IP 切换模式:",
        IP_MODE_OPTIONS,
        default=DEFAULT_IP_CHOICE,
    )
    ip_rotation_mode = IP_MODE_MAP[ip_choice]

    # Step 3.5: 开通会员执行目标已从日常菜单移除，固定完整开通。
    # 「仅验证套餐页」保留为底层测试/诊断目标（CHECKOUT_GOAL=pricing_page）。
    checkout_goal = "complete"
    goal_choice = 1

    # Step 3.6: 浏览器执行后端（Firefox 隐私模式先隐藏，日常默认固定 CloakBrowser）
    if FIREFOX_BACKEND_ENABLED:
        browser_choice = ask_choice(
            "[3.6] 选择浏览器执行后端",
            BROWSER_BACKEND_OPTIONS,
            default=DEFAULT_BROWSER_CHOICE,
        )
        browser_backend = BROWSER_BACKEND_MAP[browser_choice]
        browser_display = BROWSER_BACKEND_OPTIONS[browser_choice - 1].split("（")[0]
    else:
        browser_backend = os.environ.get("BROWSER_BACKEND", "cloak")
        browser_display = "CloakBrowser" if browser_backend == "cloak" else "Firefox 隐私模式"

    # Step 3.7: 异常处理模式（创建账号与开通会员默认连续执行；Full 强制标准模式）
    if run_scope in ("account_provision", "checkout_fulfillment"):
        mode_choice = ask_choice(
            "[3.7] 请选择异常处理模式",
            INTERACTION_MODE_OPTIONS,
            default=DEFAULT_INTERACTION_MODE_CHOICE,
        )
        interaction_mode = INTERACTION_MODE_MAP[mode_choice]
    else:
        interaction_mode = "assisted"

    # The desktop launcher always uses assisted/standard interaction.  The
    # unattended policy remains available to direct CLI callers, but is not
    # exposed through start.bat; INTERACTION_MODE carries the chosen mode.
    auto_mode = "false"

    # ── 读取可用资源数量 ──
    available_emails = 0
    available_cards = 0
    try:
        from gpt_automation.state import StateStore
        from gpt_automation.utils.input_manager import InputManager

        _state_store = StateStore(config.STATE_DB_PATH)
        _state_store.initialize()
        _input_mgr = InputManager(config.INPUT_DIR, state_store=_state_store)
        _report = _input_mgr.get_resource_report()
        available_emails = _report.email.available
        available_cards = _report.card.available
    except Exception:
        pass

    # ── 确认画面 ──
    task_display = TASK_TYPE_OPTIONS[task_choice - 1].split("（")[0]
    accounts_display = (
        f"{selected_display}（已创建账号）"
        if run_scope == "checkout_fulfillment"
        else f"{max_accounts} 个"
    )
    goal_display = (
        CHECKOUT_GOAL_OPTIONS[goal_choice - 1]
        if run_scope == "checkout_fulfillment"
        else "—"
    )
    ip_display = "自动 (iKuai)" if ip_rotation_mode == "ikuai-single" else "手动 (手机热点)"
    mode_display = (
        "连续执行 (自动跳过)"
        if interaction_mode == "continue_on_blocked"
        else "标准 (人工介入)"
    )
    browser_display_line = f"浏览器:     {browser_display}"

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   执行确认                               ║")
    print("  ╠══════════════════════════════════════════╣")
    print(f"  ║  任务类型:   {task_display}")
    print(f"  ║  注册数量:   {accounts_display}")
    print(f"  ║  执行目标:   {goal_display}")
    print(f"  ║  可用邮箱:   {available_emails} 个")
    print(f"  ║  可用信用卡: {available_cards} 张")
    print(f"  ║  IP 模式:    {ip_display}")
    print(f"  ║  {browser_display_line}")
    print("  ║  密码:       每个 session 随机生成并加密保存")
    print(f"  ║  执行模式:   {mode_display}")
    print("  ╚══════════════════════════════════════════╝")
    print()

    confirm = input("  确认执行？(Y/n): ").strip().lower()
    if confirm == "n":
        print("  已取消。")
        return 0

    # 防呆：检测 input 数据是否仍为示例或缺失（按任务类型区分范围）
    input_problems = detect_sample_input(
        run_scope,
        checkout_goal=checkout_goal,
    )
    if input_problems:
        print()
        print("  ⚠️ 检测到 input 数据仍为示例或缺失：")
        for problem in input_problems:
            print(f"    - {problem}")
        proceed = input("  仍要执行？(y/N): ").strip().lower()
        if proceed != "y":
            print("  已取消，请先替换 input 中的示例数据。")
            return 1

    # ── 设置环境变量 ──
    env = os.environ.copy()
    env["MAX_ACCOUNTS"] = max_accounts
    env["IP_ROTATION_MODE"] = ip_rotation_mode
    env["AUTO_MODE"] = auto_mode
    env["INTERACTION_MODE"] = interaction_mode
    env["RUN_SCOPE"] = run_scope
    env["BROWSER_BACKEND"] = browser_backend
    if run_scope == "checkout_fulfillment":
        env["CHECKOUT_GOAL"] = checkout_goal
    if account_ids:
        env["ACCOUNT_IDS"] = ",".join(account_ids)

    # 浏览器预检：只检查所选后端；失败时不启动批次、不占用资源。
    preflight_cmd = [sys.executable, "-u", "-m", "gpt_automation", "--check-browser"]
    preflight_result = subprocess.run(preflight_cmd, env=env, cwd=PROJECT_ROOT)
    if preflight_result.returncode != 0:
        print()
        if browser_backend == "cloak":
            print("  [错误] 浏览器预检或运行时依赖检查失败，批次未启动。")
        else:
            print(f"  [错误] {browser_display} 预检失败，批次未启动。")
        input(chr(10) + "  按 Enter 退出...")
        return preflight_result.returncode or 1
    # 预检标记必须绑定具体后端：Cloak 的预检结果不得跳过 Firefox 预检。
    env["BROWSER_PREFLIGHT_DONE"] = "1"
    env["BROWSER_PREFLIGHT_BACKEND"] = browser_backend

    print()
    print("  启动中...")
    print()

    # ── 启动 orchestrator ──
    cmd = [sys.executable, "-u", "-m", "gpt_automation.orchestrator"]
    result = subprocess.run(cmd, env=env, cwd=PROJECT_ROOT)

    if result.returncode not in {0, 2}:
        print()
        print(f"  [错误] 执行过程中发生错误。(code: {result.returncode})")

    print()
    print(f"  {launcher_result_message(result.returncode)}")
    print()
    input("  按 Enter 退出...")
    return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已中断。")
        sys.exit(130)
