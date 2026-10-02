# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import time
from typing import Any

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
