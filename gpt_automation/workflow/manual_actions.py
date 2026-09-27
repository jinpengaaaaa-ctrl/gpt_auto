"""Operator instructions for every detectable browser screen."""

from __future__ import annotations

from dataclasses import dataclass

from gpt_automation.phases.screen_detector import ScreenState


@dataclass(frozen=True)
class ManualActionSpec:
    title: str
    instruction: str
    expected_result: str
    critical: bool = False


MANUAL_ACTIONS: dict[ScreenState, ManualActionSpec] = {
    ScreenState.LOGIN_EMAIL: ManualActionSpec(
        "登录邮箱",
        "在当前浏览器输入邮箱并提交。",
        "页面进入密码、验证码或注册资料页面。",
    ),
    ScreenState.LOGIN_PASSWORD: ManualActionSpec(
        "登录密码",
        "输入邮箱对应的登录密码并提交。",
        "密码页面消失，进入验证码、欢迎页或主页。",
    ),
    ScreenState.EMAIL_VERIFY_CODE: ManualActionSpec(
        "邮箱验证码",
        "从邮箱取得验证码，在当前浏览器输入并提交。",
        "验证码输入框消失。",
    ),
    ScreenState.SIGNUP_PASSWORD: ManualActionSpec(
        "创建账户密码",
        "在当前页面创建 ChatGPT 密码并提交；不要返回旧安全设置页。",
        "密码页面消失，进入注册资料、欢迎页或主页。",
        critical=True,
    ),
    ScreenState.REGISTRATION_FORM: ManualActionSpec(
        "注册资料",
        "检查姓名和生日，修正后提交。",
        "资料表单消失，进入欢迎页或主页。",
    ),
    ScreenState.LOGIN_MODAL: ManualActionSpec(
        "登录弹窗",
        "在弹窗内重新登录；不要关闭当前浏览器。",
        "登录弹窗消失并回到主页。",
    ),
    ScreenState.WELCOME_MODAL: ManualActionSpec(
        "欢迎弹窗",
        "完成或关闭欢迎弹窗。",
        "欢迎弹窗消失。",
    ),
    ScreenState.SURVEY: ManualActionSpec(
        "问卷页面",
        "完成、跳过或关闭问卷。",
        "问卷页面消失。",
    ),
    ScreenState.MAIN_PAGE: ManualActionSpec(
        "ChatGPT 主页",
        "确认账户已经登录；需要时手动打开下一业务入口。",
        "页面保持登录状态并可进入安全或套餐设置。",
    ),
    ScreenState.PRICING_PAGE: ManualActionSpec(
        "套餐选择页",
        "确认目标 Plus 套餐卡的首期金额；免费优惠时应显示 ¥0 或「無料オファー」。",
        "套餐选择页可见，目标计划金额可确认。",
    ),
    ScreenState.SETTINGS: ManualActionSpec(
        "安全设置",
        "完成 MFA 设置，或关闭设置面板。",
        "MFA 状态已更新，或设置面板关闭并回到主页。",
        critical=True,
    ),
    ScreenState.STRIPE_PAYMENT: ManualActionSpec(
        "Stripe 支付",
        "检查账单资料和卡片；如需提交支付，只点击一次。",
        "账户侧能够验证订阅状态。",
        critical=True,
    ),
    ScreenState.STRIPE_BILLING: ManualActionSpec(
        "订阅管理",
        "完成取消订阅或检查取消状态。",
        "页面明确显示取消结果或到期日期。",
        critical=True,
    ),
    ScreenState.UNKNOWN: ManualActionSpec(
        "未知页面",
        "检查截图和当前页面，关闭遮挡弹窗或完成页面要求。",
        "程序重新检测为一个已知页面。",
        critical=True,
    ),
}
