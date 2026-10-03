# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import time
from typing import Any, Optional

from seine.distributed.common.models import expired_text, format_expiry_reason
from seine.progress import elapsed
from seine.utils import format_size, format_timestamp

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
    return format_size(item["read"])


def extract_remote_artifacts(builds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the deliverables of the builds into one row each, newest first."""
    arts: list[dict[str, Any]] = []
    for b in builds:
        timestamp = float(b.get("finished_at") or b.get("created_at") or 0.0)
        origin = {
            "build_id": str(b.get("id") or b.get("build_id") or ""),
            "project": str(b.get("project") or ""),
            "target_arch": str(b.get("target_arch") or b.get("architecture") or ""),
            "timestamp": timestamp,
            "created_at": b.get("created_at"),
            "finished_at": b.get("finished_at"),
        }
        if b.get("artifacts_expired_at"):
            origin.update(expired=True, expired_at=b["artifacts_expired_at"],
                          expired_reason=b.get("artifacts_expired_reason"))
        metas = [m for m in b.get("artifact_meta") or [] if isinstance(m, dict)]
        if not metas and origin.get("expired"):
            # builds expired before metadata was kept
            arts.append({"name": "[expired artifact]", **origin})
        for m in metas:
            arts.append({
                "name": m.get("name", "artifact"),
                "size": m.get("size", 0),
                "sha256": m.get("sha256", ""),
                "key": m.get("key", ""),
                **origin,
            })
    arts.sort(key=lambda a: a.get("timestamp", 0.0), reverse=True)
    return arts


def artifact_download_label(a: dict[str, Any], progress: Optional[dict[str, Any]] = None) -> str:
    """The DOWNLOAD column: expired, a transfer state, or nothing."""
    return "expired" if a.get("expired") else _download_label(progress)


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
        size_str = format_size(a["size"]) if "size" in a else "-"
        build_id = str(a.get("build_id") or "")
        short_id = build_id[:12] if len(build_id) > 12 else build_id
        project = str(a.get("project") or "")
        short_proj = project[:12] if len(project) > 12 else project
        arch = str(a.get("target_arch") or a.get("architecture") or "")
        download = artifact_download_label(a, progress.get((build_id, name)))

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
        lines.append(" [n] New User   [t] Issue PAT   [a] Toggle Admin   [x] Toggle Active   [D] Delete   [↑/↓] Select")
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

        date_str = format_timestamp(u.get("created_at"), date_only=True)

        lines.append(
            f"{prefix}{status_str:<10} {short_uid:<20} {role_str:<12} {pat_count:<6} {date_str}"
        )

    lines.append("")
    lines.append(" [n] New User   [t] Issue PAT   [a] Toggle Admin   [x] Toggle Active   [D] Delete   [↑/↓] Select")
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

        date_str = format_timestamp(p.get("created_at"), date_only=True)

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


def _section(title: str, rows: list[tuple[str, Any]]) -> list[str]:
    """A titled block of 'label: value' lines; unset values are left out."""
    shown = [(k, v) for k, v in rows if v not in (None, "")]
    if not shown:
        return []
    out = ["", f" {title}", " " + "─" * 30]
    out.extend(f"   {k + ':':<14} {v}" for k, v in shown)
    return out


def _detail(title: str, sections: list[list[str]]) -> str:
    lines = [f" {title}", " " + "═" * 30]
    for sec in sections:
        lines.extend(sec)
    return "\n".join(lines)


def render_build_detail(b: dict[str, Any]) -> str:
    mark, label = _status_mark(b.get("status", "queued"))
    started, finished = b.get("started_at"), b.get("finished_at")
    spent = None
    if started is not None:
        spent = elapsed(max(0, int((finished if finished is not None else time.time()) - started)))
    arts = [
        f"   {m.get('name', 'artifact')}  {format_size(m.get('size', 0))}\n     {m.get('sha256', '')}"
        for m in (b.get("artifact_meta") or []) if isinstance(m, dict)
    ]
    sections = [
        _section("BUILD", [
            ("ID", b.get("id") or b.get("build_id")),
            ("Project", b.get("project")),
            ("Arch", b.get("target_arch") or b.get("architecture")),
            ("Status", f"{mark} {label}"),
            ("Release", "yes" if b.get("is_release") else None),
        ]),
        _section("TIMING", [
            ("Submitted by", b.get("user_id")),
            ("Queued", format_timestamp(b.get("created_at")) if b.get("created_at") else None),
            ("Started", format_timestamp(started) if started else None),
            ("Finished", format_timestamp(finished) if finished else None),
            ("Elapsed", spent),
        ]),
        _section("EXECUTION", [
            ("Worker", b.get("worker_id")),
            ("Spec", b.get("spec_file")),
            ("Worktree", b.get("worktree_digest")),
            ("Error", b.get("error_message")),
        ]),
    ]
    if arts:
        sections.append(["", " DELIVERABLES", " " + "─" * 30, *arts])
    if b.get("artifacts_expired_at"):
        sections.append(_section("RETENTION", [
            ("Artifacts", expired_text(b.get("artifacts_expired_reason"))),
            ("Expired at", format_timestamp(b["artifacts_expired_at"])),
        ]))
    return _detail("BUILD DETAIL", sections)


def render_worker_detail(w: dict[str, Any], now: Optional[float] = None) -> str:
    st = (w.get("status") or "online").lower()
    mark = {"online": "● online", "paused": "⏸ paused"}.get(st, f"✖ {st}")
    seen = w.get("last_seen")
    age = elapsed(max(0, int((now or time.time()) - seen))) + " ago" if seen else None
    scores = w.get("arch_scores") or {}
    sections = [
        _section("WORKER", [
            ("ID", w.get("id") or w.get("worker_id")),
            ("Hostname", w.get("hostname")),
            ("Status", mark),
        ]),
        _section("HEALTH", [
            ("Last seen", f"{format_timestamp(seen)} ({age})" if seen else None),
        ]),
        _section("CAPABILITIES", [("Native arch", w.get("native_arch"))]
                 + [(f"  {a}", f"{sc:.1f}") for a, sc in sorted(scores.items())]),
        _section("CAPACITY", [
            ("Slots", w.get("concurrency_slots", 1)),
            ("Free disk", f"{float(w.get('free_disk_gb') or 0.0):.1f} GB"),
        ]),
    ]
    return _detail("WORKER DETAIL", sections)


def render_artifact_detail(a: dict[str, Any], progress: Optional[dict[str, Any]] = None) -> str:
    expired = a.get("expired")
    transfer = None
    if progress:
        transfer = _download_label(progress)
        if progress.get("total"):
            transfer += f" ({format_size(progress['read'])} / {format_size(progress['total'])})"
    sections = [
        _section("ARTIFACT", [
            ("Name", a.get("name")),
            ("Size", format_size(a["size"]) if "size" in a else None),
        ]),
        _section("INTEGRITY", [("SHA-256", a.get("sha256")), ("Storage key", a.get("key"))]),
        _section("ORIGIN", [
            ("Build", a.get("build_id")),
            ("Project", a.get("project")),
            ("Arch", a.get("target_arch") or a.get("architecture")),
            ("Created", format_timestamp(a.get("created_at")) if a.get("created_at") else None),
            ("Finished", format_timestamp(a.get("finished_at")) if a.get("finished_at") else None),
        ]),
        _section("STATUS", [
            ("State", "expired" if expired else "available"),
            ("Expiry reason", format_expiry_reason(a.get("expired_reason")) if expired else None),
            ("Expired at", format_timestamp(a["expired_at"]) if a.get("expired_at") else None),
            ("Download", expired_text(a.get("expired_reason")) if expired else transfer),
        ]),
    ]
    return _detail("ARTIFACT DETAIL", sections)


def render_user_detail(u: dict[str, Any], tokens: Optional[list[dict[str, Any]]] = None) -> str:
    uid = u.get("id")
    mine = [t for t in (tokens or []) if t.get("user_id") == uid]
    sections = [
        _section("USER", [
            ("ID", uid),
            ("UID", u.get("uid")),
            ("Role", "System Administrator" if u.get("is_admin") else "Member"),
            ("State", "Active" if u.get("active", True) else "Disabled"),
        ]),
        _section("ATTRIBUTES", [
            ("Default project", u.get("default_project") or "none"),
            ("Created", format_timestamp(u.get("created_at"))),
        ]),
    ]
    sections.append(["", f" TOKENS ({len(mine)})", " " + "─" * 30,
                     *[f"   {t.get('id')}  {format_timestamp(t.get('created_at'))}" for t in mine]])
    return _detail("USER DETAIL", sections)


def render_project_detail(p: dict[str, Any], builds: Optional[list[dict[str, Any]]] = None) -> str:
    name = p.get("name") or p.get("id")
    mine = [b for b in (builds or []) if b.get("project") == name]
    quota = p.get("quota_gb")
    sections = [
        _section("PROJECT", [
            ("Name", name),
            ("Created", format_timestamp(p.get("created_at"))),
        ]),
        _section("CONFIGURATION", [
            ("Mode", "dev-only" if p.get("dev_only") else "dev + prod"),
            ("Dev bucket", p.get("dev_bucket")),
            ("Prod bucket", None if p.get("dev_only") else p.get("prod_bucket")),
            ("Quota", f"{quota:g} GB" if quota else "none"),
        ]),
        _section("ACTIVITY", [
            ("Queued", sum(1 for b in mine if b.get("status") == "queued")),
            ("Running", sum(1 for b in mine if b.get("status") == "running")),
        ]),
    ]
    members = p.get("members") or []
    if members:
        sections.append(["", " MEMBERS", " " + "─" * 30,
                         *[f"   {m.get('user_id')}  {m.get('role')}" for m in members]])
    return _detail("PROJECT DETAIL", sections)


def render_ops_detail(settings: dict[str, Any], stats: dict[str, Any]) -> str:
    lat = settings.get("ping_ms")
    return _detail("SERVER DETAIL", [
        _section("DIAGNOSTICS", [
            ("Endpoint", settings.get("url")),
            ("Latency", f"{lat} ms" if lat is not None else None),
            ("Identity", settings.get("user_id")),
        ]),
        _section("QUEUE", [(k.capitalize(), stats.get(k, 0)) for k in ("queued", "running")]),
        _section("FLEET", [
            ("Online", stats.get("workers_online", 0)),
            ("Paused", stats.get("workers_paused", 0)),
        ]),
    ])


def render_remote_detail(tab: int, item: Optional[dict[str, Any]], connected: bool = True, **extra: Any) -> str:
    """Details of the selected item of a cockpit tab, as plain text."""
    if not connected:
        return " No active server connection"
    if tab == 6:
        return render_ops_detail(extra.get("settings") or {}, extra.get("stats") or {})
    if item is None:
        return " Select an item to view details"
    if tab == 1:
        return render_build_detail(item)
    if tab == 2:
        return render_worker_detail(item)
    if tab == 3:
        return render_artifact_detail(item, extra.get("progress"))
    if tab == 4:
        return render_user_detail(item, extra.get("tokens"))
    if tab == 5:
        return render_project_detail(item, extra.get("builds"))
    return ""
