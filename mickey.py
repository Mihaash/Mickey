#!/usr/bin/env python3
"""Live Kubernetes terminal dashboard for Mickey."""

from __future__ import annotations

import argparse
import curses
import json
import locale
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

RESOURCES = [
    ("Pods", "pods", True),
    ("Nodes", "nodes", False),
    ("Deployments", "deployments", True),
    ("Services", "services", True),
    ("Ingress", "ingresses", True),
    ("Network", "networkpolicies", True),
    ("Storage", "persistentvolumeclaims", True),
    ("Events", "events", True),
    ("Namespaces", "namespaces", False),
]
COLUMNS = {
    "pods": ["NS", "NAME", "STATUS", "CPU", "MEMORY", "RESTARTS"],
    "nodes": ["NAME", "STATUS", "VERSION"],
    "deployments": ["NAMESPACE", "NAME", "READY", "DESIRED", "AGE"],
    "services": ["NAMESPACE", "NAME", "TYPE", "PORTS", "EXTERNAL-IP"],
    "ingresses": ["NAMESPACE", "NAME", "HOSTS", "AGE"],
    "networkpolicies": ["NAMESPACE", "NAME", "INGRESS", "EGRESS", "AGE"],
    "persistentvolumeclaims": ["NAMESPACE", "NAME", "STATUS", "CAPACITY", "VOLUME"],
    "events": ["NAMESPACE", "TYPE", "REASON", "OBJECT", "AGE", "MESSAGE"],
    "namespaces": ["NAME", "STATUS", "AGE"],
}


def kubectl(*args: str, timeout: float = 8) -> str:
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "kubectl command failed")
    return result.stdout


def age(timestamp: str | None) -> str:
    if not timestamp:
        return "--"
    try:
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    except ValueError:
        return "--"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def pod_metrics() -> dict[tuple[str, str], tuple[str, str]]:
    try:
        lines = kubectl("top", "pods", "-A", "--no-headers").splitlines()
    except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    result = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 4:
            result[(parts[0], parts[1])] = (parts[2], parts[3])
    return result


def item_values(resource: str, item: dict[str, Any], metrics: dict) -> list[str]:
    metadata = item.get("metadata", {})
    name = metadata.get("name", "--")
    namespace = metadata.get("namespace", "--")
    status = item.get("status", {})
    spec = item.get("spec", {})
    created = metadata.get("creationTimestamp")
    if resource == "pods":
        restarts = sum(c.get("restartCount", 0) for c in status.get("containerStatuses", []))
        cpu, memory = metrics.get((namespace, name), ("--", "--"))
        return [namespace, name, status.get("phase", "Unknown"), cpu, memory, str(restarts)]
    if resource == "nodes":
        ready = next((c.get("status") for c in status.get("conditions", []) if c.get("type") == "Ready"), "Unknown")
        version = status.get("nodeInfo", {}).get("kubeletVersion", "--")
        return [name, "Ready" if ready == "True" else "NotReady", version]
    if resource == "deployments":
        return [namespace, name, str(status.get("readyReplicas", 0)), str(spec.get("replicas", 1)), age(created)]
    if resource == "services":
        ports = ",".join(str(port.get("port", "--")) for port in spec.get("ports", [])) or "--"
        ingress = status.get("loadBalancer", {}).get("ingress", [])
        external_ip = (ingress[0].get("ip") or ingress[0].get("hostname")) if ingress else "--"
        return [namespace, name, spec.get("type", "ClusterIP"), ports, external_ip]
    if resource == "ingresses":
        hosts = sorted({rule.get("host", "") for rule in spec.get("rules", []) if rule.get("host")})
        return [namespace, name, ",".join(hosts) or "--", age(created)]
    if resource == "networkpolicies":
        return [namespace, name, str(len(spec.get("ingress", []))), str(len(spec.get("egress", []))), age(created)]
    if resource == "persistentvolumeclaims":
        capacity = spec.get("resources", {}).get("requests", {}).get("storage", "--")
        return [namespace, name, status.get("phase", "Pending"), capacity, spec.get("volumeName", "--")]
    if resource == "events":
        event_age = item.get("lastTimestamp") or item.get("eventTime") or created
        obj = item.get("involvedObject", {}).get("name", "--")
        return [namespace, item.get("type", "Normal"), item.get("reason", "--"), obj, age(event_age), item.get("message", "--")]
    if resource == "namespaces":
        return [name, status.get("phase", "Active"), age(created)]
    return [name]


class Dashboard:
    def __init__(self, interval: float, namespace: str | None):
        self.interval = interval
        self.namespace = namespace
        self.resource_index = 0
        self.selected = 0
        self.offset = 0
        self.focus_table = False
        self.data: list[dict[str, Any]] = []
        self.metrics: dict = {}
        self.namespaces: list[str] = []
        self.error = ""
        self.updated = "Connecting..."
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.refresh_event.set()

    @property
    def resource(self) -> tuple[str, str, bool]:
        return RESOURCES[self.resource_index]

    def request_refresh(self) -> None:
        self.refresh_event.set()

    def refresh_loop(self) -> None:
        while not self.stop_event.is_set():
            self.refresh_event.wait(self.interval)
            self.refresh_event.clear()
            if self.stop_event.is_set():
                break
            _, resource, namespaced = self.resource
            args = ["get", resource]
            if namespaced:
                args.extend(["-A"] if self.namespace is None else ["-n", self.namespace])
            args.extend(["-o", "json"])
            try:
                items = json.loads(kubectl(*args)).get("items", [])
                metrics = pod_metrics() if resource == "pods" else {}
                namespaces = self.namespaces
                if resource == "namespaces":
                    namespaces = sorted(i.get("metadata", {}).get("name", "") for i in items)
                with self.lock:
                    self.data = items
                    self.metrics = metrics
                    self.namespaces = namespaces
                    self.error = ""
                    self.updated = datetime.now().astimezone().strftime("%H:%M:%S")
                    self.selected = min(self.selected, max(0, len(items) - 1))
            except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                with self.lock:
                    self.error = str(exc).splitlines()[0][:120]
                    self.data = []
                    self.updated = datetime.now().astimezone().strftime("%H:%M:%S")

    def selected_item(self) -> dict[str, Any] | None:
        with self.lock:
            if 0 <= self.selected < len(self.data):
                return self.data[self.selected]
        return None


def safe_add(window, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
    height, screen_width = window.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= screen_width:
        return
    available = screen_width - x - 1
    if width is not None:
        available = min(available, width)
    try:
        window.addnstr(y, x, str(text).replace("\n", " "), max(0, available), attr)
    except curses.error:
        pass


def draw(stdscr, dashboard: Dashboard, message: str = "") -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 14 or width < 80:
        safe_add(stdscr, 0, 0, "Resize terminal to at least 80 columns x 14 rows.")
        stdscr.refresh()
        return
    stdscr.border()
    title = curses.color_pair(3) | curses.A_BOLD
    highlight = curses.color_pair(2) | curses.A_BOLD
    safe_add(stdscr, 1, 1, "MICKEY - KUBERNETES".center(width - 2), title)
    safe_add(stdscr, 2, 1, "Interactive Kubernetes TUI".center(width - 2), curses.A_DIM)
    stdscr.hline(3, 1, curses.ACS_HLINE, width - 2)
    divider = 18
    stdscr.vline(4, divider, curses.ACS_VLINE, height - 7)
    safe_add(stdscr, 5, 2, "RESOURCES", curses.A_BOLD)
    safe_add(stdscr, 5, divider + 2, dashboard.resource[0].upper(), curses.A_BOLD)
    safe_add(stdscr, 6, divider + 2, f"Namespace: {dashboard.namespace or 'all'}  |  Updated: {dashboard.updated}", curses.A_DIM)
    resource_capacity = max(1, height - 11)
    first_resource = max(0, min(dashboard.resource_index, len(RESOURCES) - resource_capacity))
    for index in range(first_resource, min(len(RESOURCES), first_resource + resource_capacity)):
        label = RESOURCES[index][0]
        y = 7 + index - first_resource
        active = index == dashboard.resource_index and not dashboard.focus_table
        safe_add(stdscr, y, 2, ("> " if index == dashboard.resource_index else "  ") + label, highlight if active else 0)
    with dashboard.lock:
        items = list(dashboard.data)
        metrics = dict(dashboard.metrics)
        error = dashboard.error
    columns = COLUMNS[dashboard.resource[1]]
    content_x = divider + 3
    content_width = width - content_x - 2
    gap = 1
    if dashboard.resource[1] == "pods":
        widths = [8, 16, 8, 5, 7, 8]
        widths[1] += max(0, content_width - sum(widths) - gap * (len(columns) - 1))
    else:
        base = max(8, (content_width - gap * (len(columns) - 1)) // len(columns))
        widths = [base] * len(columns)
        widths[-1] += max(0, content_width - sum(widths) - gap * (len(columns) - 1))
    x = content_x
    for column, column_width in zip(columns, widths):
        safe_add(stdscr, 8, x, column, curses.A_BOLD, column_width)
        x += column_width + gap
    stdscr.hline(9, content_x, curses.ACS_HLINE, content_width)
    visible = max(1, height - 13)
    if dashboard.selected < dashboard.offset:
        dashboard.offset = dashboard.selected
    if dashboard.selected >= dashboard.offset + visible:
        dashboard.offset = dashboard.selected - visible + 1
    for row_index, item in enumerate(items[dashboard.offset:dashboard.offset + visible]):
        y = 10 + row_index
        x = content_x
        active = dashboard.focus_table and dashboard.offset + row_index == dashboard.selected
        for value, column_width in zip(item_values(dashboard.resource[1], item, metrics), widths):
            safe_add(stdscr, y, x, value, highlight if active else 0, column_width)
            x += column_width + gap
    if error or message:
        safe_add(stdscr, height - 3, 2, error and f"kubectl: {error}" or message, curses.color_pair(4) | curses.A_BOLD, width - 4)
    stdscr.hline(height - 2, 1, curses.ACS_HLINE, width - 2)
    footer = "Tab Focus  ↑↓ Move  Enter Details  Esc Close  D Delete  R Restart  L Logs  N Scope  Q Quit"
    safe_add(stdscr, height - 1, 2, footer, 0, width - 4)
    stdscr.refresh()


def flatten_details(prefix: str, value: Any, lines: list[str]) -> None:
    if isinstance(value, dict):
        if not value:
            lines.append(f"{prefix}: {{}}")
            return
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flatten_details(child_prefix, child, lines)
        return
    if isinstance(value, list):
        if not value:
            lines.append(f"{prefix}: []")
            return
        for index, child in enumerate(value):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            flatten_details(child_prefix, child, lines)
        return
    lines.append(f"{prefix}: {json.dumps(value, sort_keys=True)}")


def popup(stdscr, title: str, lines: list[str], confirm: bool = False) -> bool:
    height, width = stdscr.getmaxyx()
    box_height = min(height - 4, max(7, min(len(lines) + 4, height - 4)))
    box_width = min(width - 4, max(50, min(width - 4, max((len(line) for line in lines), default=0) + 4)))
    top, left = max(1, (height - box_height) // 2), max(1, (width - box_width) // 2)
    win = curses.newwin(box_height, box_width, top, left)
    win.border()
    safe_add(win, 1, 2, title, curses.A_BOLD, box_width - 4)
    for index, line in enumerate(lines[:box_height - 4]):
        safe_add(win, 2 + index, 2, line, 0, box_width - 4)
    if confirm:
        prompt = "Y to confirm, Esc to cancel"
    else:
        prompt = "Esc to close"
    safe_add(win, box_height - 2, 2, prompt, curses.A_DIM, box_width - 4)
    win.refresh()
    stdscr.timeout(10)
    try:
        while True:
            key = stdscr.getch()
            if key == 27:
                return False
            if confirm and key in (ord("y"), ord("Y")):
                return True
            if confirm and key not in (-1,):
                return False
            if not confirm and key == -1:
                continue
    finally:
        stdscr.timeout(250)


def run_action(stdscr, dashboard: Dashboard, action: str) -> str:
    item = dashboard.selected_item()
    if not item:
        return "No resource selected"
    metadata = item.get("metadata", {})
    name = metadata.get("name", "")
    namespace = metadata.get("namespace")
    kind = dashboard.resource[1]
    scoped = ["-n", namespace] if namespace else []
    if action == "details":
        lines: list[str] = []
        flatten_details("metadata", metadata, lines)
        flatten_details("spec", item.get("spec", {}), lines)
        flatten_details("status", item.get("status", {}), lines)
        popup(stdscr, f"{kind.rstrip('s').upper()} {name}", lines or ["No details available"])
        return ""
    if action == "logs":
        if kind != "pods":
            return "Logs are available for pods"
        try:
            output = kubectl("logs", *scoped, name, "--all-containers=true", "--tail=100", timeout=15)
            lines = output.splitlines()
        except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            lines = str(exc).splitlines()
        popup(stdscr, f"LOGS {name}", lines)
        return ""
    if action == "delete":
        if not popup(stdscr, f"DELETE {name}?", [f"Delete {kind.rstrip('s')} {name}? This cannot be undone."], confirm=True):
            return "Delete cancelled"
        command = ["delete", kind, name, *scoped]
    elif action == "restart":
        target_kind, target_name = kind, name
        if kind == "pods":
            owner = next((owner for owner in metadata.get("ownerReferences", []) if owner.get("controller")), None)
            if not owner:
                return "Cannot restart an unmanaged pod"
            target_kind, target_name = owner.get("kind", "").lower(), owner.get("name", "")
            if target_kind == "replicaset":
                try:
                    replica = json.loads(kubectl("get", f"replicaset/{target_name}", *scoped, "-o", "json"))
                except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                    return str(exc).splitlines()[0][:120]
                deployment = next((owner for owner in replica.get("metadata", {}).get("ownerReferences", []) if owner.get("controller") and owner.get("kind") == "Deployment"), None)
                if not deployment:
                    return "Cannot find the Deployment for this pod"
                target_kind, target_name = "deployments", deployment.get("name", "")
        if target_kind not in ("deployments", "statefulsets", "daemonsets"):
            return "Restart works on pods and workload controllers"
        command = ["rollout", "restart", f"{target_kind}/{target_name}", *scoped]
    else:
        return "Unknown action"
    try:
        output = kubectl(*command, timeout=20).strip()
        dashboard.request_refresh()
        return output.splitlines()[-1] if output else "Action completed"
    except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return str(exc).splitlines()[0][:120]


def main_loop(stdscr, dashboard: Dashboard) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    stdscr.keypad(True)
    curses.mousemask(curses.BUTTON1_PRESSED)
    stdscr.timeout(250)
    worker = threading.Thread(target=dashboard.refresh_loop, daemon=True)
    worker.start()
    message = ""
    try:
        while True:
            draw(stdscr, dashboard, message)
            key = stdscr.getch()
            if key == -1:
                continue
            message = ""
            if key in (ord("q"), ord("Q")):
                break
            if key == curses.KEY_MOUSE:
                _, mouse_x, mouse_y, _, button_state = curses.getmouse()
                if button_state & curses.BUTTON1_PRESSED:
                    height, width = stdscr.getmaxyx()
                    if 7 <= mouse_y <= 7 + len(RESOURCES) and 2 <= mouse_x <= 18:
                        clicked_index = mouse_y - 7 + max(0, min(dashboard.resource_index, len(RESOURCES) - max(1, height - 11)))
                        if 0 <= clicked_index < len(RESOURCES):
                            dashboard.resource_index = clicked_index
                            dashboard.selected = 0
                            dashboard.offset = 0
                            dashboard.focus_table = False
                            dashboard.request_refresh()
                            continue
                    if 10 <= mouse_y < height - 3 and 18 <= mouse_x < width - 2:
                        with dashboard.lock:
                            selected = dashboard.offset + (mouse_y - 10)
                            if 0 <= selected < len(dashboard.data):
                                dashboard.selected = selected
                                dashboard.focus_table = True
                        message = run_action(stdscr, dashboard, "details")
                continue
            if key == 9:
                dashboard.focus_table = not dashboard.focus_table
            elif key in (curses.KEY_UP, ord("k")):
                if dashboard.focus_table:
                    with dashboard.lock:
                        dashboard.selected = max(0, dashboard.selected - 1)
                else:
                    dashboard.resource_index = max(0, dashboard.resource_index - 1)
                    dashboard.selected = dashboard.offset = 0
                    dashboard.request_refresh()
            elif key in (curses.KEY_DOWN, ord("j")):
                if dashboard.focus_table:
                    with dashboard.lock:
                        dashboard.selected = min(max(0, len(dashboard.data) - 1), dashboard.selected + 1)
                else:
                    dashboard.resource_index = min(len(RESOURCES) - 1, dashboard.resource_index + 1)
                    dashboard.selected = dashboard.offset = 0
                    dashboard.request_refresh()
            elif key in (10, 13, curses.KEY_ENTER):
                if not dashboard.focus_table:
                    dashboard.selected = 0
                    dashboard.offset = 0
                    dashboard.request_refresh()
                else:
                    message = run_action(stdscr, dashboard, "details")
            elif key in (ord("d"), ord("D")):
                message = run_action(stdscr, dashboard, "delete")
            elif key in (ord("r"), ord("R")):
                message = run_action(stdscr, dashboard, "restart")
            elif key in (ord("l"), ord("L")):
                message = run_action(stdscr, dashboard, "logs")
            elif key in (ord("n"), ord("N")):
                with dashboard.lock:
                    namespaces = list(dashboard.namespaces)
                if namespaces:
                    choices: list[str | None] = [None, *namespaces]
                    try:
                        current = choices.index(dashboard.namespace)
                    except ValueError:
                        current = 0
                    dashboard.namespace = choices[(current + 1) % len(choices)]
                    dashboard.selected = dashboard.offset = 0
                    dashboard.request_refresh()
                else:
                    message = "Open Namespaces to load namespace choices"
            elif key == ord(" "):
                dashboard.request_refresh()
    finally:
        dashboard.stop_event.set()
        dashboard.request_refresh()
        worker.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live interactive Kubernetes dashboard for Mickey")
    parser.add_argument("-n", "--namespace", help="Show namespaced resources in this namespace (default: all)")
    parser.add_argument("-r", "--refresh", type=float, default=3.0, help="Refresh interval in seconds (default: 3)")
    args = parser.parse_args()
    if args.refresh <= 0:
        parser.error("--refresh must be greater than zero")
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(main_loop, Dashboard(args.refresh, args.namespace))


if __name__ == "__main__":
    main()
