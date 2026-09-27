"""Native Windows desktop console for GPT Automation.

The console starts the existing loopback control-plane server in-process and
uses its JSON API for every operation.  This keeps the desktop surface and
the browser dashboard on one workflow implementation, including production
preflight, resource leases, operator decisions and durable history.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
    _data_dir = os.environ.get("GPT_AUTOMATION_DATA_DIR")
    DATA_ROOT = Path(_data_dir) if _data_dir else Path(
        os.environ.get("LOCALAPPDATA", Path.home())
    ) / "GPTAutomation"
else:
    APP_ROOT = Path(__file__).resolve().parent
    DATA_ROOT = Path(os.environ.get("GPT_AUTOMATION_DATA_DIR", APP_ROOT))


def _copy_seed_files() -> None:
    """Create user data directories and a safe .env seed on first launch."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    for name in ("input", "accounts", "runtime", "profiles", "evidence", "TMP"):
        (DATA_ROOT / name).mkdir(parents=True, exist_ok=True)
    env_path = DATA_ROOT / ".env"
    seed = APP_ROOT / ".env.example"
    if not env_path.exists() and seed.exists():
        env_path.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        payload = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                message = detail.get("error", {}).get("message", str(exc))
            except Exception:
                message = str(exc)
            raise RuntimeError(message) from exc
        except URLError as exc:
            raise RuntimeError(f"本机服务不可用：{exc.reason}") from exc
        return json.loads(raw.decode("utf-8")) if raw else {}


class DashboardApp(tk.Tk):
    NAV = (("概览", "overview"), ("开始运行", "run"), ("资源管理", "resources"),
           ("已创建账户", "accounts"), ("运行记录", "history"), ("配置中心", "settings"))

    def __init__(self) -> None:
        super().__init__()
        self.title("GPT Automation · 桌面控制台")
        self.geometry("1220x780")
        self.minsize(980, 640)
        self.configure(bg="#f7f8f5")
        self._jobs: queue.Queue = queue.Queue()
        self._views: dict[str, tk.Frame] = {}
        self._server = None
        self._server_thread = None
        self.api: ApiClient | None = None
        self.current_view = "overview"
        self.run_state: dict = {}
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._start_backend()
        self._build_shell()
        self.show_view("overview")
        self.after(120, self._drain_jobs)
        self.after(2200, self._poll_run)
        if getattr(sys, "frozen", False):
            self.after(1600, self._check_for_update)

    def _start_backend(self) -> None:
        _copy_seed_files()
        os.environ.setdefault("GPT_AUTOMATION_DATA_DIR", str(DATA_ROOT))
        from gpt_automation.web.server import serve

        dist = APP_ROOT / "frontend" / "dist"
        self._server = serve(
            project_root=APP_ROOT,
            input_dir=DATA_ROOT / "input",
            db_path=DATA_ROOT / "runtime" / "workflow_state.db",
            snapshot_path=DATA_ROOT / "runtime" / "demo_runs.json",
            dist_dir=dist if dist.exists() else APP_ROOT,
            host="127.0.0.1",
            port=0,
        )
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="gpt-dashboard-http",
            daemon=True,
        )
        self._server_thread.start()
        port = self._server.server_address[1]
        self.api = ApiClient(f"http://127.0.0.1:{port}")

    def _build_shell(self) -> None:
        self.sidebar = tk.Frame(self, bg="#1f2921", width=230)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg="#1f2921")
        brand.pack(fill="x", padx=18, pady=(24, 28))
        tk.Label(brand, text="G", bg="#a9d6b1", fg="#17321f", width=3, height=1,
                 font=("Segoe UI", 18, "bold"), relief="flat").pack(side="left")
        text = tk.Frame(brand, bg="#1f2921")
        text.pack(side="left", padx=10)
        tk.Label(text, text="GPT Automation", bg="#1f2921", fg="white",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(text, text="桌面控制台", bg="#1f2921", fg="#a8b9aa",
                 font=("Segoe UI", 9)).pack(anchor="w")
        self.nav_buttons: dict[str, tk.Button] = {}
        for label, key in self.NAV:
            button = tk.Button(self.sidebar, text=label, anchor="w", padx=20, pady=10,
                               borderwidth=0, relief="flat", cursor="hand2",
                               bg="#1f2921", fg="#bac8bc", activebackground="#344438",
                               activeforeground="white", font=("Segoe UI", 10),
                               command=lambda k=key: self.show_view(k))
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[key] = button
        self.health = tk.Label(self.sidebar, text="●  正在连接本机服务…", bg="#1f2921",
                               fg="#a8b9aa", anchor="w", padx=20, font=("Segoe UI", 9))
        self.health.pack(side="bottom", fill="x", pady=18)
        self.content = tk.Frame(self, bg="#f7f8f5")
        self.content.pack(side="left", fill="both", expand=True)
        header = tk.Frame(self.content, bg="#f7f8f5")
        header.pack(fill="x", padx=34, pady=(26, 18))
        self.eyebrow = tk.Label(header, text="LOCAL WORKSPACE", bg="#f7f8f5", fg="#737a70",
                                font=("Segoe UI", 8, "bold"))
        self.eyebrow.pack(anchor="w")
        self.page_title = tk.Label(header, text="概览", bg="#f7f8f5", fg="#20231f",
                                   font=("Segoe UI", 24, "bold"))
        self.page_title.pack(anchor="w")
        self.body = tk.Frame(self.content, bg="#f7f8f5")
        self.body.pack(fill="both", expand=True, padx=34, pady=(0, 28))

    def _new_view(self, key: str) -> tk.Frame:
        if key in self._views:
            self._views[key].destroy()
        frame = tk.Frame(self.body, bg="#f7f8f5")
        self._views[key] = frame
        return frame

    def show_view(self, key: str) -> None:
        self.current_view = key
        for name, button in self.nav_buttons.items():
            button.configure(bg="#344438" if name == key else "#1f2921",
                             fg="white" if name == key else "#bac8bc")
        labels = {key: label for label, key in self.NAV}
        self.page_title.configure(text=labels[key])
        for child in self.body.winfo_children():
            child.destroy()
        frame = self._new_view(key)
        frame.pack(fill="both", expand=True)
        getattr(self, f"_render_{key}")(frame)

    def _card(self, parent: tk.Widget, title: str = "") -> tk.Frame:
        card = tk.Frame(parent, bg="white", highlightbackground="#e6e9e1", highlightthickness=1)
        if title:
            tk.Label(card, text=title, bg="white", fg="#20231f", font=("Segoe UI", 12, "bold"))\
                .pack(anchor="w", padx=18, pady=(15, 8))
        return card

    def _button(self, parent: tk.Widget, text: str, command, primary: bool = False) -> tk.Button:
        return tk.Button(parent, text=text, command=command, padx=13, pady=7, borderwidth=0,
                         bg="#28724b" if primary else "#f7f8f5", fg="white" if primary else "#20231f",
                         activebackground="#225e3d" if primary else "#e6e9e1", cursor="hand2")

    def _async(self, fn, on_done=None) -> None:
        def worker():
            try:
                result = fn()
                self._jobs.put((on_done, result, None))
            except Exception as exc:  # noqa: BLE001
                self._jobs.put((on_done, None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_jobs(self) -> None:
        while True:
            try:
                callback, result, error = self._jobs.get_nowait()
            except queue.Empty:
                break
            if error:
                messagebox.showerror("操作失败", str(error), parent=self)
            elif callback:
                callback(result)
        self.after(120, self._drain_jobs)

    def _poll_run(self) -> None:
        if self.api:
            self._async(lambda: self.api.request("/api/runs/current"), self._on_run_state)
        self.after(2200, self._poll_run)

    def _check_for_update(self) -> None:
        def check():
            from desktop_updater import latest_release

            return latest_release()

        def done(release):
            if not release:
                return
            if not messagebox.askyesno(
                "发现新版本",
                f"GPT Automation {release['version']} 已发布。\n\n现在下载并安装更新吗？",
                parent=self,
            ):
                return
            try:
                from desktop_updater import install_release

                install_release(release, silent=True)
                messagebox.showinfo("正在更新", "更新程序已启动，当前窗口将关闭。", parent=self)
                self.close()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("更新失败", str(exc), parent=self)

        self._async(check, done)

    def _on_run_state(self, state: dict) -> None:
        self.run_state = state if state else {}
        if self.current_view == "run":
            self._render_run(self._views.get("run") or self._new_view("run"))
        if self.current_view == "overview":
            self._render_overview(self._views.get("overview") or self._new_view("overview"))

    def _render_overview(self, frame: tk.Frame) -> None:
        for child in frame.winfo_children(): child.destroy()
        metrics = tk.Frame(frame, bg="#f7f8f5")
        metrics.pack(fill="x")
        labels = [("可用邮箱", "email"), ("可用卡片", "card"), ("READY 账户", "accounts"), ("当前运行", "run")]
        for label, key in labels:
            card = self._card(metrics)
            card.pack(side="left", fill="both", expand=True, padx=(0, 12))
            value = tk.Label(card, text="…", bg="white", fg="#20231f", font=("Segoe UI", 24, "bold"))
            value.pack(anchor="w", padx=18, pady=(16, 0))
            tk.Label(card, text=label, bg="white", fg="#737a70", font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(0, 14))
            def update_metric(data, widget=value):
                if widget.winfo_exists():
                    widget.configure(text=str(data))
            self._async(lambda k=key: self._summary(k), update_metric)
        lower = tk.Frame(frame, bg="#f7f8f5")
        lower.pack(fill="both", expand=True, pady=(18, 0))
        current = self._card(lower, "当前任务")
        current.pack(side="left", fill="both", expand=True, padx=(0, 12))
        if self.run_state:
            tk.Label(current, text=self.run_state.get("status", ""), bg="white", fg="#28724b",
                     font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=18, pady=10)
            tk.Label(current, text=self.run_state.get("current_action") or self.run_state.get("waiting_reason", ""),
                     bg="white", fg="#596159", wraplength=450, justify="left").pack(anchor="w", padx=18, pady=6)
        else:
            tk.Label(current, text="当前没有运行中的任务。", bg="white", fg="#737a70").pack(anchor="w", padx=18, pady=28)
            self._button(current, "开始一次运行", lambda: self.show_view("run"), True).pack(anchor="w", padx=18, pady=(0, 18))
        quick = self._card(lower, "快速入口")
        quick.pack(side="left", fill="both", expand=True)
        for label, key in (("创建账号", "run"), ("管理邮箱/卡片", "resources"), ("查看 READY 账户", "accounts"), ("检查运行配置", "settings")):
            self._button(quick, label, lambda k=key: self.show_view(k)).pack(fill="x", padx=18, pady=5)
        self.health.configure(text="●  本机服务已连接", fg="#74c787")

    def _summary(self, key: str):
        if key == "run": return self.run_state.get("status", "空闲") if self.run_state else "空闲"
        if key == "accounts": return self.api.request("/api/registered-accounts/summary").get("ready", 0)
        return self.api.request(f"/api/resources/summary?type={key}").get("available", 0)

    def _render_run(self, frame: tk.Frame) -> None:
        for child in frame.winfo_children(): child.destroy()
        left = self._card(frame, "新建运行")
        left.pack(side="left", fill="both", expand=True, padx=(0, 14))
        right = self._card(frame, "运行状态")
        right.pack(side="left", fill="both", expand=True)
        form = tk.Frame(left, bg="white"); form.pack(fill="x", padx=18, pady=8)
        vars_: dict[str, tk.StringVar] = {}
        options = {"task_type": [("创建账号", "provision"), ("开通会员", "checkout"), ("完整流程", "full")],
                   "mode": [("新建", "new"), ("恢复", "resume")],
                   "interaction": [("标准模式", "assisted"), ("无人值守", "unattended")],
                   "network": [("手动热点", "manual"), ("自动 iKuai/Xray", "auto")],
                   "browser_backend": [("CloakBrowser", "cloak"), ("Firefox 隐私模式", "firefox_private")],
                   "checkout_goal": [("完整开通", "complete"), ("仅验证套餐页", "pricing_page")]}
        row = 0
        for key, label in (("task_type", "任务类型"), ("mode", "运行方式"), ("interaction", "交互策略"), ("network", "网络策略"), ("browser_backend", "浏览器后端"), ("checkout_goal", "开通目标")):
            tk.Label(form, text=label, bg="white", fg="#596159").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=7)
            var = tk.StringVar(value=options[key][0][1]); vars_[key] = var
            ttk.Combobox(form, textvariable=var, values=[v for _l, v in options[key]], state="readonly", width=20).grid(row=row, column=1, sticky="ew", pady=7)
            row += 1
        tk.Label(form, text="目标数量", bg="white", fg="#596159").grid(row=row, column=0, sticky="w", pady=7)
        target = tk.StringVar(value="1"); vars_["target_count"] = target
        ttk.Spinbox(form, from_=1, to=100, textvariable=target, width=20).grid(row=row, column=1, sticky="w", pady=7); row += 1
        tk.Label(form, text="READY 账户 ID", bg="white", fg="#596159").grid(row=row, column=0, sticky="w", pady=7)
        ids = tk.StringVar(); vars_["account_ids"] = ids
        ttk.Entry(form, textvariable=ids).grid(row=row, column=1, sticky="ew", pady=7)
        form.columnconfigure(1, weight=1)
        tk.Label(left, text="真实运行会操作浏览器。支付、取消订阅和未知副作用步骤会停在人工确认点。", bg="#fff4df", fg="#865b19", wraplength=500, justify="left", padx=10, pady=8).pack(fill="x", padx=18, pady=12)
        actions = tk.Frame(left, bg="white"); actions.pack(fill="x", padx=18, pady=(0, 18))
        self._button(actions, "预检并开始", lambda: self._start_run(vars_), True).pack(side="right")
        self._render_run_state(right)

    def _start_run(self, vars_: dict[str, tk.StringVar]) -> None:
        payload = {key: value.get() for key, value in vars_.items()}
        payload.update(execution_mode="production")
        def done(check):
            blocked = [item for item in check.get("checks", []) if item.get("status") != "ready"]
            if blocked:
                messagebox.showwarning("预检未通过", "\n".join(f"{x['label']}：{x['detail']}" for x in blocked), parent=self); return
            self._async(lambda: self.api.request("/api/runs", "POST", payload), lambda s: setattr(self, "run_state", s))
            messagebox.showinfo("任务已启动", "真实运行已启动，浏览器窗口和人工决策会在需要时出现。", parent=self)
        self._async(lambda: self.api.request("/api/runs/preflight", "POST", payload), done)

    def _render_run_state(self, parent: tk.Frame) -> None:
        for child in parent.winfo_children(): child.destroy()
        if not self.run_state:
            tk.Label(parent, text="提交预检后，实时状态会显示在这里。", bg="white", fg="#737a70", wraplength=400, justify="left").pack(anchor="w", padx=18, pady=28); return
        tk.Label(parent, text=self.run_state.get("status", ""), bg="white", fg="#28724b", font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(parent, text=self.run_state.get("current_action") or self.run_state.get("waiting_reason", ""), bg="white", fg="#596159", wraplength=400, justify="left").pack(anchor="w", padx=18, pady=6)
        request = self.run_state.get("operator_request")
        if request:
            box = tk.Frame(parent, bg="#fff4df"); box.pack(fill="x", padx=18, pady=12)
            tk.Label(box, text=request.get("title", "需要人工处理"), bg="#fff4df", fg="#865b19", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
            tk.Label(box, text=request.get("instruction", request.get("description", "")), bg="#fff4df", fg="#865b19", wraplength=380, justify="left").pack(anchor="w", padx=10, pady=4)
            labels = {
                "verify_manual": "已完成，请验证",
                "advance_after_manual": "继续下一步",
                "retry_auto": "重试自动步骤",
                "skip_account": "跳过账户",
                "defer_account": "稍后处理",
                "abort_batch": "终止批次",
                "keep_open": "保持浏览器打开",
                "restart_browser": "重启浏览器",
                "confirm_network": "确认网络已切换",
                "retry_network": "重新检查网络",
                "switch_network": "切换网络后重试",
                "reuse_ip": "沿用当前 IP",
            }
            for decision in request.get("allowed_decisions", []):
                self._button(box, labels.get(decision, decision), lambda d=decision: self._decide(d)).pack(fill="x", padx=10, pady=3)
        actions = tk.Frame(parent, bg="white")
        actions.pack(fill="x", padx=18, pady=10)
        self._button(actions, "安全停止", self._stop_run).pack(side="right")
        if self.run_state.get("status") == "STOPPING" or (
            self.run_state.get("status") == "NEEDS_OPERATOR"
            and not request
            and self.run_state.get("last_error")
        ):
            self._button(actions, "强制结束", self._force_stop).pack(side="right", padx=8)

    def _decide(self, decision: str) -> None: self._async(lambda: self.api.request("/api/runs/decisions", "POST", {"decision": decision}), lambda s: setattr(self, "run_state", s))
    def _stop_run(self) -> None: self._async(lambda: self.api.request("/api/runs/stop", "POST"), lambda s: setattr(self, "run_state", s))
    def _force_stop(self) -> None: self._async(lambda: self.api.request("/api/runs/force-stop", "POST"), lambda s: setattr(self, "run_state", s))

    def _render_resources(self, frame: tk.Frame) -> None:
        for child in frame.winfo_children(): child.destroy()
        card = self._card(frame, "邮箱与卡片资源"); card.pack(fill="both", expand=True)
        notebook = ttk.Notebook(card); notebook.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        for resource_type, label in (("email", "邮箱"), ("card", "卡片")):
            tab = tk.Frame(notebook, bg="white"); notebook.add(tab, text=label)
            toolbar = tk.Frame(tab, bg="white"); toolbar.pack(fill="x", padx=8, pady=(8, 0))
            self._button(toolbar, f"添加{label}", lambda t=resource_type: self._resource_dialog(t)).pack(side="left")
            tree = ttk.Treeview(tab, columns=("display", "state", "updated"), show="headings", height=15)
            self._button(toolbar, "编辑选中", lambda t=resource_type, tr=tree: self._edit_selected_resource(t, tr)).pack(side="left", padx=6)
            self._button(toolbar, "纠正状态", lambda t=resource_type, tr=tree: self._correct_selected_resource(t, tr)).pack(side="left")
            self._button(toolbar, "刷新", self.show_view_resources).pack(side="right")
            for col, heading in (("display", "资源"), ("state", "状态"), ("updated", "更新时间")): tree.heading(col, text=heading); tree.column(col, width=260 if col=="display" else 150)
            tree.pack(fill="both", expand=True, padx=8, pady=8)
            tree._resource_items = {}  # type: ignore[attr-defined]
            def load(t=resource_type):
                return self.api.request(f"/api/resources?type={t}")
            def populate(data, tr=tree):
                if not tr.winfo_exists():
                    return
                for child in tr.get_children():
                    tr.delete(child)
                tr._resource_items = {}  # type: ignore[attr-defined]
                for item in data.get("items", []):
                    iid = tr.insert("", "end", values=(item.get("display",""),item.get("state",""),item.get("updated_at","")))
                    tr._resource_items[iid] = item  # type: ignore[attr-defined]
            self._async(load, populate)
        hint=tk.Label(card,text="敏感字段只在本机提交给资源服务，列表中不会显示密码、token、完整卡号。",bg="white",fg="#737a70");hint.pack(anchor="w",padx=18,pady=(0,14))

    def show_view_resources(self) -> None:
        self.show_view("resources")

    def _selected_resource(self, resource_type: str, tree: ttk.Treeview):
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("请选择资源", "请先在列表中选择一项资源。", parent=self)
            return None
        item = getattr(tree, "_resource_items", {}).get(selected[0])
        if not item:
            messagebox.showwarning("请刷新资源", "列表已过期，请刷新后重试。", parent=self)
            return None
        return item

    def _edit_selected_resource(self, resource_type: str, tree: ttk.Treeview) -> None:
        item = self._selected_resource(resource_type, tree)
        if item is None:
            return
        if not item.get("editable"):
            messagebox.showwarning("资源不可编辑", "占用中、已使用或隔离的资源不能直接编辑。", parent=self)
            return
        self._resource_dialog(resource_type, str(item.get("resource_id", "")))

    def _correct_selected_resource(self, resource_type: str, tree: ttk.Treeview) -> None:
        item = self._selected_resource(resource_type, tree)
        if item is None:
            return
        actions = [action for action in item.get("state_actions", []) if action.get("enabled")]
        if not actions:
            messagebox.showwarning("无法纠正状态", "当前资源没有可用的状态纠正操作。", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("纠正资源状态")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("430x250")
        tk.Label(dialog, text=f"资源：{item.get('display', '')}", anchor="w").pack(fill="x", padx=18, pady=(18, 8))
        tk.Label(dialog, text="状态纠正会写入本机执行履历，请确认你已核对真实资源状态。", fg="#865b19", wraplength=380, justify="left").pack(fill="x", padx=18, pady=8)
        reason = tk.StringVar()
        ttk.Entry(dialog, textvariable=reason).pack(fill="x", padx=18, pady=8)
        buttons = tk.Frame(dialog); buttons.pack(fill="x", padx=18, pady=12)
        for action in actions:
            def submit(a=action):
                if not messagebox.askyesno("确认状态纠正", f"确定将资源状态改为“{a.get('label', a.get('target_state'))}”吗？", parent=dialog):
                    return
                body = {"target": a.get("target_state", ""), "reason": reason.get(), "confirmed": True}
                self._async(
                    lambda: self.api.request(f"/api/resources/{'emails' if resource_type == 'email' else 'cards'}/{quote(str(item.get('resource_id', '')), safe='@+._-')}/state-corrections", "POST", body),
                    lambda _result: (dialog.destroy(), self.show_view("resources")),
                )
            self._button(buttons, str(action.get("label", action.get("target_state", "操作"))), submit).pack(side="left", padx=3)
        self._button(dialog, "取消", dialog.destroy).pack(pady=(0, 12))

    def _resource_dialog(self, resource_type: str, resource_id: str | None = None) -> None:
        editing = bool(resource_id)
        is_email = resource_type == "email"
        title = ("编辑" if editing else "添加") + ("邮箱资源" if is_email else "卡片资源")
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("540x360" if is_email else "430x270")
        fields = [("邮箱", "email", False), ("邮箱密码", "password", True), ("OAuth Client ID", "client_id", False), ("Refresh Token", "refresh_token", True)] if is_email else [("卡号", "card_no", True), ("有效期（MMYY 或 MM/YYYY）", "expiry", False), ("CVC", "cvc", True)]
        values: dict[str, tk.StringVar] = {}
        body = tk.Frame(dialog); body.pack(fill="both", expand=True, padx=20, pady=16)
        for row, (label, key, secret) in enumerate(fields):
            tk.Label(body, text=label, anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
            var = tk.StringVar()
            values[key] = var
            entry = ttk.Entry(body, textvariable=var, show="•" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=7)
            if editing and key in {"email", "card_no"}:
                entry.configure(state="disabled")
        body.columnconfigure(1, weight=1)
        tk.Label(dialog, text="敏感信息仅写入本机资源文件，列表不会回显完整值。", fg="#737a70", wraplength=470, justify="left").pack(fill="x", padx=20, pady=(0, 8))
        buttons = tk.Frame(dialog); buttons.pack(fill="x", padx=20, pady=(0, 16))
        def submit() -> None:
            payload = {key: var.get().strip() for key, var in values.items() if not (editing and key in {"email", "card_no"})}
            if not editing:
                payload.update({key: var.get().strip() for key, var in values.items()})
            endpoint = "/api/resources/emails" if is_email else "/api/resources/cards"
            method = "POST"
            if editing:
                endpoint += "/" + quote(str(resource_id), safe="@+._-")
                method = "PATCH"
            self._async(
                lambda: self.api.request(endpoint, method, payload),
                lambda _result: (dialog.destroy(), self.show_view("resources"), messagebox.showinfo("已保存", "资源已保存到本机。", parent=self)),
            )
        self._button(buttons, "保存", submit, True).pack(side="right")
        self._button(buttons, "取消", dialog.destroy).pack(side="right", padx=8)

    def _render_accounts(self, frame: tk.Frame) -> None: self._render_table(frame, "已创建账户", "/api/registered-accounts", ("email", "status", "offer_status", "browser_backend", "updated_at"))
    def _render_history(self, frame: tk.Frame) -> None: self._render_table(frame, "运行记录", "/api/history", ("session_id", "stage", "status", "last_error", "updated_at"))
    def _render_table(self, frame: tk.Frame, title: str, endpoint: str, columns: tuple[str, ...]) -> None:
        card=self._card(frame,title);card.pack(fill="both",expand=True); tree=ttk.Treeview(card,columns=columns,show="headings");
        for col in columns: tree.heading(col,text=col);tree.column(col,width=170)
        tree.pack(fill="both",expand=True,padx=14,pady=14)
        def load():
            return self.api.request(endpoint)
        def populate(data):
            if not tree.winfo_exists():
                return
            for child in tree.get_children():
                tree.delete(child)
            for item in data.get("items",[]):
                tree.insert("","end",values=tuple(item.get(c,"-") for c in columns))
        self._async(load, populate)

    def _render_settings(self, frame: tk.Frame) -> None:
        card=self._card(frame,"运行配置");card.pack(fill="both",expand=True); body=tk.Frame(card,bg="white");body.pack(fill="both",expand=True,padx=18,pady=10)
        def done(data):
            if not body.winfo_exists():
                return
            entries={}
            for item in data.get("items",[]):
                row=tk.Frame(body,bg="white");row.pack(fill="x",pady=5);tk.Label(row,text=f"{item['label']}  ({item['key']})",bg="white",fg="#596159",width=34,anchor="w").pack(side="left")
                var=tk.StringVar(value="" if item.get("secret") else str(item.get("value","")));entries[item["key"]]=var
                if item.get("type")=="select": widget=ttk.Combobox(row,textvariable=var,values=item.get("options",[]),state="readonly",width=35)
                elif item.get("type")=="boolean": widget=ttk.Combobox(row,textvariable=var,values=["true","false"],state="readonly",width=35)
                else: widget=ttk.Entry(row,textvariable=var,show="•" if item.get("secret") else "",width=38)
                widget.pack(side="left",fill="x",expand=True);tk.Label(row,text=("已配置" if item.get("secret") and item.get("configured") else ""),bg="white",fg="#28724b").pack(side="left",padx=8)
            self._button(card,"保存配置",lambda:self._save_settings(entries),True).pack(anchor="e",padx=18,pady=(0,18))
        self._async(lambda:self.api.request("/api/settings"),done)
    def _save_settings(self, entries):
        body={key:var.get() for key,var in entries.items() if var.get()};self._async(lambda:self.api.request("/api/settings","PUT",body),lambda _s:messagebox.showinfo("已保存","配置已写入本机 .env，下一次运行生效。",parent=self))

    def close(self) -> None:
        try:
            if self._server: self._server.shutdown(); self._server.server_close()
        finally: self.destroy()


def main() -> int:
    app = DashboardApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
