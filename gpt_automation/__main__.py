"""gpt_automation — ChatGPT 账号自动注册系统入口点."""

from __future__ import annotations

import argparse

from gpt_automation.orchestrator import main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GPT Automation")
    parser.add_argument(
        "--resume",
        dest="resume_session_id",
        metavar="SESSION_ID",
        help="恢复一个保留现场的账户任务",
    )
    parser.add_argument(
        "--list-resumable",
        action="store_true",
        help="列出可恢复或可查看的 session，并显示输入源中的完整邮箱",
    )
    parser.add_argument(
        "--inspect-session",
        dest="inspect_session_id",
        metavar="SESSION_ID",
        help="仅打开持久化浏览器现场，不执行注册业务",
    )
    parser.add_argument(
        "--inspect-profile",
        dest="inspect_profile_id",
        metavar="PROFILE_ID",
        help="仅打开未映射的旧 profile 进行诊断",
    )
    parser.add_argument(
        "--abandon-session",
        dest="abandon_session_id",
        metavar="SESSION_ID",
        help="安全放弃一个 session，并按副作用分别处置邮箱和信用卡",
    )
    parser.add_argument(
        "--check-cloakbrowser",
        action="store_true",
        help="检查并准备 CloakBrowser 二进制，不启动注册流程",
    )
    parser.add_argument(
        "--check-browser",
        action="store_true",
        help="Run preflight for the browser backend selected by BROWSER_BACKEND.",
    )
    parser.add_argument(
        "--check-dependencies",
        action="store_true",
        help="检查运行时依赖（greenlet/playwright/VC++ 运行库），不启动注册流程",
    )
    return parser


def list_resumable_sessions() -> int:
    import config
    from gpt_automation.state import (
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
        print("当前没有可恢复或可查看的 session。")
        return 0
    for index, candidate in enumerate(candidates, start=1):
        print(render_session_candidate(candidate, index))
    if unmapped:
        print("未映射旧 profile（只能诊断查看）：")
        for profile in unmapped:
            print(
                f"profile_{profile.profile_id}  "
                f"state={profile.state.value}  "
                f"inspect=python -m gpt_automation --inspect-profile {profile.profile_id}"
            )
    return 0


async def _inspect_profile(
    profile_id: str,
    *,
    display_name: str,
    network_identity: str,
) -> int:
    """Open one profile for diagnostics without running registration."""
    import config
    from pathlib import Path
    from gpt_automation.browser.ip_guard import close_browser
    from gpt_automation.phases.phase1 import phase1_open_login
    from gpt_automation.phases.screen_detector import ensure_screen_detected
    from gpt_automation.network.strategy import create_network_strategy
    from gpt_automation.state import inspect_profile_state

    if not profile_id or any(separator in profile_id for separator in ("/", "\\", "..")):
        print("profile 标识不合法。")
        return 2
    profile_root = Path(config.PROFILE_DIR).resolve()
    profile_path = (profile_root / f"profile_{profile_id}").resolve()
    if profile_path.parent != profile_root or not profile_path.is_dir():
        print("找不到可用的 profile。")
        return 2
    profile_state = inspect_profile_state(profile_path)
    if profile_state.value in {"LOCKED", "ACTIVE"}:
        print("该 profile 正被其他进程占用，拒绝重复打开。")
        return 2

    mode = config.IP_ROTATION_CONFIG.get("rotation_mode", "ikuai-single")
    strategy = create_network_strategy(mode)
    browser = None
    try:
        preparation = await strategy.prepare_account(network_identity)
        if not preparation.ready:
            print(f"网络准备失败：{preparation.status}")
            return 1
        acquired = await strategy.acquire_browser(None, set(), profile_id)
        if not acquired.ready or acquired.browser is None or acquired.page is None:
            print(f"浏览器/IP 获取失败：{acquired.status}")
            return 1
        browser = acquired.browser
        page = acquired.page
        await phase1_open_login(page)
        screen_state = await ensure_screen_detected(page)
        print(f"已进入：{display_name}")
        print(f"当前页面状态：{screen_state.value}")
        input("查看完成后按 Enter 关闭浏览器：")
        return 0
    finally:
        await close_browser(browser)
        await strategy.finish_account()
        await strategy.close()


async def inspect_session(session_id: str) -> int:
    """Open one managed profile for diagnostics without running registration."""
    import config
    from gpt_automation.state import SessionCatalog, StateStore

    state_store = StateStore(config.STATE_DB_PATH)
    state_store.initialize()
    candidate = next(
        (
            item
            for item in SessionCatalog(
                state_store,
                input_dir=config.INPUT_DIR,
                profile_dir=config.PROFILE_DIR,
                lock_dir=config.EXECUTION_LOCK_DIR,
            ).list_candidates()
            if item.session_id == session_id
        ),
        None,
    )
    if candidate is None:
        print(f"找不到 session：{session_id}")
        return 1
    if not candidate.email or not candidate.profile_id:
        print("该 session 无法解析原始邮箱或 profile，不能进入查看。")
        return 2
    if candidate.profile_state.value in {"LOCKED", "ACTIVE"}:
        print("该 profile 正被其他进程占用，拒绝重复打开。")
        return 2
    if candidate.profile_state.value not in {"AVAILABLE", "LEGACY"}:
        print("该 session 没有可用的持久化 profile。")
        return 2
    return await _inspect_profile(
        candidate.profile_id,
        display_name=candidate.email,
        network_identity=candidate.email,
    )


async def inspect_profile(profile_id: str) -> int:
    """Open an unmapped legacy profile for diagnostics only."""
    return await _inspect_profile(
        profile_id,
        display_name=f"profile_{profile_id}",
        network_identity="legacy-profile@example.invalid",
    )


def abandon_session(session_id: str) -> int:
    """Require explicit confirmation before abandoning a managed session."""
    import config
    from gpt_automation.orchestrator import _abandon_session_safely, _is_safe_session_id
    from gpt_automation.state import StateStore, StateTransitionError

    if not _is_safe_session_id(session_id):
        print("session 标识不合法。")
        return 2
    answer = input(
        f"确认安全放弃 session {session_id}？邮箱和信用卡将分别处置。(y/N): "
    ).strip().lower()
    if answer != "y":
        print("已取消放弃操作。")
        return 0

    store = StateStore(config.STATE_DB_PATH)
    store.initialize()
    try:
        _abandon_session_safely(
            store,
            session_id,
            lock_dir=config.EXECUTION_LOCK_DIR,
        )
    except StateTransitionError as exc:
        print(f"无法放弃 session：{exc}")
        return 2
    print(f"已安全放弃 session：{session_id}")
    return 0


def check_cloakbrowser() -> int:
    """Run the explicit CloakBrowser binary preflight without starting a batch."""
    from gpt_automation.browser.engine import (
        ensure_cloakbrowser_latest,
        prepare_cloakbrowser_binary,
    )

    problems = check_dependencies()
    if problems:
        return 1
    try:
        ensure_cloakbrowser_latest()
        prepare_cloakbrowser_binary(allow_update=True)
    except Exception as exc:
        print(f"CloakBrowser 预检失败：{exc}")
        return 1
    return 0


def check_browser() -> int:
    """Preflight the backend selected by BROWSER_BACKEND (default cloak)."""
    import os

    problems = check_dependencies()
    if problems:
        return 1
    backend = (os.environ.get("BROWSER_BACKEND") or "cloak").strip().lower()
    try:
        if backend == "firefox_private":
            from gpt_automation.browser.firefox_private import (
                check_firefox_installed,
            )

            ok, detail = check_firefox_installed()
            if not ok:
                raise RuntimeError(detail)
            print(f"Firefox private preflight passed: {detail}")
        else:
            from gpt_automation.browser.engine import (
                ensure_cloakbrowser_latest,
                prepare_cloakbrowser_binary,
            )

            ensure_cloakbrowser_latest()
            prepare_cloakbrowser_binary(allow_update=True)
            print("CloakBrowser preflight passed")
    except Exception as exc:
        print(f"browser preflight failed ({backend}): {exc}")
        return 1
    return 0


def check_dependencies() -> list[str]:
    """Print runtime dependency problems and return the list (empty = OK)."""
    from gpt_automation.utils.dependency_check import check_runtime_dependencies

    problems = check_runtime_dependencies()
    if problems:
        print("运行时依赖检查失败：")
        for problem in problems:
            print(f"  - {problem}")
        print("修复后请重新运行本检查。")
    else:
        print("运行时依赖检查通过（greenlet / playwright / VC++ 运行库）。")
    return problems


def cli(argv: list[str] | None = None) -> int:
    import asyncio

    args = build_parser().parse_args(argv)
    if args.list_resumable:
        return list_resumable_sessions()
    if args.inspect_session_id:
        return asyncio.run(inspect_session(args.inspect_session_id))
    if args.inspect_profile_id:
        return asyncio.run(inspect_profile(args.inspect_profile_id))
    if args.abandon_session_id:
        return abandon_session(args.abandon_session_id)
    if args.check_cloakbrowser:
        return check_cloakbrowser()
    if args.check_browser:
        return check_browser()
    if args.check_dependencies:
        return 0 if not check_dependencies() else 1
    return asyncio.run(main(args.resume_session_id))


if __name__ == "__main__":
    raise SystemExit(cli())
