"""Systeminformationen: CPU, RAM, Akku, Netzwerk, Datenträger, laufende Programme."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import psutil

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool


def system_snapshot() -> dict[str, Any]:
    """Momentaufnahme für Werkzeug und Dashboard."""
    memory = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage("C:\\" if psutil.WINDOWS else "/")
        disk_info = {
            "total_gb": round(disk.total / 1024**3, 1),
            "free_gb": round(disk.free / 1024**3, 1),
            "percent": disk.percent,
        }
    except OSError:
        disk_info = {}
    battery = None
    try:
        sensor = psutil.sensors_battery()
    except (AttributeError, NotImplementedError, OSError):
        sensor = None
    if sensor is not None:
        battery = {
            "percent": round(sensor.percent),
            "plugged": bool(sensor.power_plugged),
            "minutes_left": None
            if sensor.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN)
            else int(sensor.secsleft / 60),
        }
    net = psutil.net_io_counters()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(logical=True),
        "ram_percent": memory.percent,
        "ram_used_gb": round((memory.total - memory.available) / 1024**3, 1),
        "ram_total_gb": round(memory.total / 1024**3, 1),
        "disk": disk_info,
        "battery": battery,
        "net_sent_mb": round(net.bytes_sent / 1024**2),
        "net_recv_mb": round(net.bytes_recv / 1024**2),
        "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
    }


def top_processes(limit: int = 8) -> list[dict[str, Any]]:
    procs: list[tuple[str, int]] = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            mem = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
            procs.append((str(proc.info["name"] or "?"), round(mem / 1024**2)))
        except (psutil.Error, AttributeError):
            continue
    merged: dict[str, int] = {}
    for name, ram_mb in procs:
        merged[name] = merged.get(name, 0) + ram_mb
    ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"name": name, "ram_mb": ram} for name, ram in ranked]


@tool(
    name="system_status",
    description=(
        "Get the current PC status: CPU and RAM usage, free disk space, battery, network "
        "traffic, uptime and the programs using the most memory."
    ),
    parameters=obj({}),
    category="system",
)
async def system_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.5)
    snapshot = await asyncio.to_thread(system_snapshot)
    processes = await asyncio.to_thread(top_processes)
    lines = [
        f"CPU: {snapshot['cpu_percent']:.0f} % ({snapshot['cpu_count']} Threads)",
        f"RAM: {snapshot['ram_used_gb']} von {snapshot['ram_total_gb']} GB belegt ({snapshot['ram_percent']:.0f} %)",
    ]
    if snapshot["disk"]:
        lines.append(
            f"Laufwerk: {snapshot['disk']['free_gb']} GB frei von {snapshot['disk']['total_gb']} GB"
        )
    battery = snapshot["battery"]
    if battery:
        state = "lädt" if battery["plugged"] else "Akkubetrieb"
        rest = f", noch ca. {battery['minutes_left']} min" if battery.get("minutes_left") else ""
        lines.append(f"Akku: {battery['percent']} % ({state}{rest})")
    lines.append(
        f"Netzwerk seit Start: {snapshot['net_recv_mb']} MB empfangen, {snapshot['net_sent_mb']} MB gesendet"
    )
    lines.append(f"Laufzeit seit Start: {snapshot['uptime_h']} h")
    lines.append(
        "Größte Programme (RAM): " + ", ".join(f"{p['name']} {p['ram_mb']} MB" for p in processes)
    )
    hardware = ctx.app.hardware
    if hardware is not None:
        lines.append(f"Hardware: {hardware.summary()}")
    return ToolResult("\n".join(lines), data={"snapshot": snapshot, "processes": processes})
