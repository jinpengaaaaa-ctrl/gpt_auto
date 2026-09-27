"""
gpt_automation — ChatGPT 账号自动注册系统。

模块化结构：
  orchestrator  — 批量注册编排
  browser/      — CloakBrowser 启动 + IP 轮换守卫
  phases/       — 4 阶段浏览器自动化
  verification/ — 邮箱验证码获取（Outlook API + IMAP）
  network/      — V2Ray + iKuai + IP 历史管理
  utils/        — CSV 输出、重试、提示音、开发模式
"""

__version__ = "2.0.0"
