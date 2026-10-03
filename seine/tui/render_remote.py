# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import time
from typing import Any, Optional

from seine.progress import elapsed

def _status_mark(status: str) -> tuple[str, str]:
    st = (status or "").lower()
    if st in ("completed", "done", "success"):
        return "✔", "completed"
    if st in ("running", "building", "in_progress"):
        return "●", "running"
    if st in ("failed", "error"):
        return "✖", "failed"
    if st in ("cancelled", "canceled"):
        return "⊘", "cancelled"
    return "⋯", st or "queued"

def render_remote_builds(builds: list[dict[str, Any]], selected_index: int = 0) -> str:
    lines = [
        " REMOTE BUILDS",
        " ══════════════════════════════════════════════════════════════════════════════",
    ]
    if not builds:
        lines.append("")
        lines.append("   No remote builds recorded.")
        lines.append("")
        lines.append(" [Enter] Stream Logs   [c] Cancel Build   [d] Download Artifacts   [↑/↓] Select")
        return "\n".join(lines)

    lines.append(
        f"   {'STATUS':<13} {'ID':<14} {'PROJECT':<14} {'ARCH':<8} {'ELAPSED':<10} {'USER'}"
    )
    lines.append("   " + "─" * 72)

    for i, b in enumerate(builds):
        prefix = " ▸ " if i == selected_index else "   "
        mark, st_label = _status_mark(b.get("status", "queued"))
        status_str = f"{mark} {st_label}"

        build_id = str(b.get("id") or b.get("build_id") or "")
        short_id = build_id[:12] if len(build_id) > 12 else build_id

        project = str(b.get("project") or "")
        short_proj = project[:12] if len(project) > 12 else project

        arch = str(b.get("target_arch") or b.get("architecture") or "")

        started = b.get("started_at")
        finished = b.get("finished_at")
        if started is not None:
            now = finished if finished is not None else time.time()
            spent = max(0, int(now - started))
            elapsed_str = elapsed(spent)
        else:
            elapsed_str = "--:--"

        user = str(b.get("user_id") or "")

        lines.append(
            f"{prefix}{status_str:<13} {short_id:<14} {short_proj:<14} {arch:<8} {elapsed_str:<10} {user}"
        )

    lines.append("")
    lines.append(" [Enter] Stream Logs   [c] Cancel Build   [d] Download Artifacts   [↑/↓] Select")
    return "\n".join(lines)

def render_remote_workers(workers: list[dict[str, Any]], selected_index: int = 0) -> str:
    lines = [
        " REMOTE WORKERS",
        " ══════════════════════════════════════════════════════════════════════════════",
    ]
    if not workers:
        lines.append("")
        lines.append("   No remote workers registered.")
        lines.append("")
        lines.append(" [p] Pause/Resume Worker   [r] Deregister Worker   [↑/↓] Select")
        return "\n".join(lines)

    lines.append(
        f"   {'STATUS':<12} {'WORKER ID':<14} {'HOSTNAME':<16} {'NATIVE':<8} {'SLOTS':<6} {'FREE DISK':<10} {'SCORES'}"
    )
    lines.append("   " + "─" * 74)

    for i, w in enumerate(workers):
        prefix = " ▸ " if i == selected_index else "   "
        st = (w.get("status") or "online").lower()
        if st == "online":
            status_str = "● online"
        elif st == "paused":
            status_str = "⏸ paused"
        else:
            status_str = f"✖ {st}"

        worker_id = str(w.get("id") or w.get("worker_id") or "")
        short_id = worker_id[:12] if len(worker_id) > 12 else worker_id

        hostname = str(w.get("hostname") or "")
        short_host = hostname[:14] if len(hostname) > 14 else hostname

        native = str(w.get("native_arch") or "")
        slots = str(w.get("concurrency_slots", 1))

        free_gb = float(w.get("free_disk_gb", 0.0) or 0.0)
        disk_str = f"{free_gb:.1f} GB"

        scores = w.get("arch_scores") or {}
        score_str = " ".join(f"{a}:{s:.1f}" for a, s in sorted(scores.items()))

        lines.append(
            f"{prefix}{status_str:<12} {short_id:<14} {short_host:<16} {native:<8} {slots:<6} {disk_str:<10} {score_str}"
        )

    lines.append("")
    lines.append(" [p] Pause/Resume Worker   [r] Deregister Worker   [↑/↓] Select")
    return "\n".join(lines)

def _format_size(size_bytes: Any) -> str:
    try:
        val = float(size_bytes or 0)
    except (TypeError, ValueError):
        return "0 B"
    if val < 1024:
        return f"{int(val)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        val /= 1024.0
        if val < 1024.0 or unit == "TB":
            return f"{val:.1f} {unit}"
    return f"{val:.1f} TB"

def _download_label(item: Optional[dict[str, Any]]) -> str:
    if not item:
        return ""
    if item["state"] == "done":
        return "✔ done"
    if item["state"] == "failed":
        return "✖ failed"
    if item["state"] == "queued":
        return "queued"
    if item["total"]:
        return f"{min(100, item['read'] * 100 // item['total'])}%"
    return _format_size(item["read"])


def render_remote_artifacts(
    artifacts: list[dict[str, Any]],
    selected_index: int = 0,
    progress: Optional[dict[tuple[str, str], dict[str, Any]]] = None,
) -> str:
    progress = progress or {}
    lines = [
        " REMOTE ARTIFACTS",
        " ══════════════════════════════════════════════════════════════════════════════",
    ]
    if not artifacts:
        lines.append("")
        lines.append("   No artifacts found in remote builds.")
        lines.append("")
        lines.append(" [Enter] Download Artifact   [d] Download All   [↑/↓] Select")
        return "\n".join(lines)

    lines.append(
        f"   {'NAME':<26} {'SIZE':<10} {'BUILD ID':<14} {'PROJECT':<14} {'ARCH':<8} {'DOWNLOAD'}"
    )
    lines.append("   " + "─" * 82)

    for i, a in enumerate(artifacts):
        prefix = " ▸ " if i == selected_index else "   "
        name = str(a.get("name") or "")
        short_name = name[:24] + ".." if len(name) > 26 else name
        size_str = "-" if a.get("expired") else _format_size(a.get("size", 0))
        build_id = str(a.get("build_id") or "")
        short_id = build_id[:12] if len(build_id) > 12 else build_id
        project = str(a.get("project") or "")
        short_proj = project[:12] if len(project) > 12 else project
        arch = str(a.get("target_arch") or a.get("architecture") or "")
        download = _download_label(progress.get((build_id, name)))

        lines.append(
            f"{prefix}{short_name:<26} {size_str:<10} {short_id:<14} {short_proj:<14} {arch:<8} {download}".rstrip()
        )

    lines.append("")
    lines.append(" [Enter] Download Artifact   [d] Download All   [↑/↓] Select")
    return "\n".join(lines)

def render_remote_users(
    users: list[dict[str, Any]],
    tokens: Optional[list[dict[str, Any]]] = None,
    selected_index: int = 0,
) -> str:
    lines = [
        " USER ROSTER",
        " ══════════════════════════════════════════════════════════════════════════════",
    ]
    if not users:
        lines.append("")
        lines.append("   No users registered.")
        lines.append("")
        lines.append(" [n] New User   [t] Issue PAT   [a] Toggle Admin   [x] Toggle Active   [↑/↓] Select")
        return "\n".join(lines)

    token_counts: dict[str, int] = {}
    for tok in tokens or []:
        u = tok.get("user_id")
        if u:
            token_counts[u] = token_counts.get(u, 0) + 1

    lines.append(
        f"   {'STATUS':<10} {'USERNAME':<20} {'ROLE':<12} {'PATS':<6} {'CREATED'}"
    )
    lines.append("   " + "─" * 68)

    for i, u in enumerate(users):
        prefix = " ▸ " if i == selected_index else "   "
        active = u.get("active", True)
        status_str = "● active" if active else "○ disabled"

        uid = str(u.get("id") or "")
        short_uid = uid[:18] if len(uid) > 18 else uid

        role_str = "admin" if u.get("is_admin") else "member"
        pat_count = str(token_counts.get(uid, 0))

        created = u.get("created_at")
        if created:
            try:
                date_str = time.strftime("%Y-%m-%d", time.localtime(created))
            except Exception:
                date_str = "--"
        else:
            date_str = "--"

        lines.append(
            f"{prefix}{status_str:<10} {short_uid:<20} {role_str:<12} {pat_count:<6} {date_str}"
        )

    lines.append("")
    lines.append(" [n] New User   [t] Issue PAT   [a] Toggle Admin   [x] Toggle Active   [↑/↓] Select")
    return "\n".join(lines)

def render_remote_projects(
    projects: list[dict[str, Any]],
    selected_index: int = 0,
) -> str:
    lines = [
        " PROJECT INVENTORY",
        " ══════════════════════════════════════════════════════════════════════════════",
    ]
    if not projects:
        lines.append("")
        lines.append("   No projects configured.")
        lines.append("")
        lines.append(" [n] New Project   [m] Manage Members   [d] Delete Project   [↑/↓] Select")
        return "\n".join(lines)

    lines.append(
        f"   {'PROJECT':<18} {'DEV BUCKET':<24} {'PROD BUCKET':<24} {'CREATED'}"
    )
    lines.append("   " + "─" * 74)

    for i, p in enumerate(projects):
        prefix = " ▸ " if i == selected_index else "   "
        pname = str(p.get("name") or p.get("id") or "")
        short_pname = pname[:16] if len(pname) > 16 else pname

        dev = str(p.get("dev_bucket") or "-")
        short_dev = dev[:22] if len(dev) > 22 else dev

        prod = str(p.get("prod_bucket") or "-")
        short_prod = prod[:22] if len(prod) > 22 else prod

        created = p.get("created_at")
        if created:
            try:
                date_str = time.strftime("%Y-%m-%d", time.localtime(created))
            except Exception:
                date_str = "--"
        else:
            date_str = "--"

        lines.append(
            f"{prefix}{short_pname:<18} {short_dev:<24} {short_prod:<24} {date_str}"
        )

    lines.append("")
    lines.append(" [n] New Project   [m] Manage Members   [d] Delete Project   [↑/↓] Select")
    return "\n".join(lines)

def render_remote_ops(settings: dict[str, Any], stats: dict[str, Any]) -> str:
    lat = f"{settings.get('ping_ms')} ms" if settings.get("ping_ms") is not None else "--"
    lines = [
        " SERVER & CLUSTER OPERATIONS",
        " ══════════════════════════════════════════════════════════════════════════════",
        "",
        " CONFIGURATION",
        " ──────────────────────────────────────────────────────────────────────────────",
        f"   Server:          {settings.get('url', 'local engine')}",
        f"   Latency:         {lat}",
        f"   Active User:     {settings.get('user_id', '--')}" + (" [admin]" if settings.get("is_admin") else ""),
        f"   Active Project:  {settings.get('active_project', '--')}",
        "",
        " BUILD QUEUE METRICS",
        " ──────────────────────────────────────────────────────────────────────────────",
        f"   Queued:     {stats.get('queued', 0):<8} Running:    {stats.get('running', 0):<8} Completed: {stats.get('completed', 0)}",
        f"   Failed:     {stats.get('failed', 0):<8} Cancelled:  {stats.get('cancelled', 0):<8} Total:     {stats.get('total_builds', 0)}",
        "",
        " FLEET CAPACITY",
        " ──────────────────────────────────────────────────────────────────────────────",
        f"   Online Nodes:    {stats.get('workers_online', 0):<8} Paused Nodes: {stats.get('workers_paused', 0)}",
        f"   Total Capacity:  {stats.get('total_slots', 0)} slots   Free Storage: {stats.get('free_disk_gb', 0.0):.1f} GB",
        "",
        " [r] Refresh Stats   [Tab] Switch Pane   [1-6] Navigation",
    ]
    return "\n".join(lines)


