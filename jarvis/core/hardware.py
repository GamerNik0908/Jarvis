"""Hardware-Erkennung (CPU, RAM, GPU, VRAM, Speicherplatz) und Modell-Empfehlung.

Unter Windows wird der VRAM bewusst *nicht* über ``Win32_VideoController.AdapterRAM``
gelesen: Dieser WMI-Wert ist ein 32-Bit-Feld und meldet bei Karten ab 4 GB
falsche Werte. Stattdessen:

1. ``nvidia-smi`` für NVIDIA-Karten (liefert auch freien VRAM),
2. die Registry (``HardwareInformation.qwMemorySize``) für alle anderen Karten.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import psutil

from jarvis.core.config import LocalModelTier

log = logging.getLogger(__name__)

_GPU_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_IGNORED_ADAPTERS = (
    "microsoft basic",
    "remote display",
    "virtual",
    "parsec",
    "citrix",
    "spacedesk",
)
_MIN_DISCRETE_VRAM_MB = 2048
#: Hersteller, deren GPUs Ollama unter Windows beschleunigen kann.
_ACCELERATED_VENDORS = ("nvidia", "amd")


@dataclass
class GPUInfo:
    name: str
    vendor: str
    vram_total_mb: int | None = None
    vram_free_mb: int | None = None
    driver: str | None = None
    source: str = "unknown"

    @property
    def vram_total_gb(self) -> float:
        return round((self.vram_total_mb or 0) / 1024, 1)

    @property
    def accelerated(self) -> bool:
        """Eigenständige GPU, die für lokale Modelle nutzbar ist."""
        return (
            self.vendor in _ACCELERATED_VENDORS
            and (self.vram_total_mb or 0) >= _MIN_DISCRETE_VRAM_MB
        )


@dataclass
class HardwareInfo:
    os_name: str
    os_version: str
    machine: str
    cpu_name: str
    cpu_physical_cores: int
    cpu_logical_cores: int
    ram_total_gb: float
    ram_available_gb: float
    disk_free_gb: float | None
    python_version: str
    gpus: list[GPUInfo] = field(default_factory=list)

    @property
    def best_gpu(self) -> GPUInfo | None:
        candidates = [g for g in self.gpus if g.accelerated]
        return max(candidates, key=lambda g: g.vram_total_mb or 0, default=None)

    @property
    def usable_vram_gb(self) -> float:
        gpu = self.best_gpu
        return gpu.vram_total_gb if gpu else 0.0

    @property
    def gpu_backend(self) -> str:
        gpu = self.best_gpu
        if gpu is None:
            return "cpu"
        return "cuda" if gpu.vendor == "nvidia" else "rocm"

    def summary(self) -> str:
        gpu = self.best_gpu
        gpu_text = f"{gpu.name} ({gpu.vram_total_gb:g} GB VRAM)" if gpu else "keine nutzbare GPU"
        return (
            f"{self.cpu_name} · {self.cpu_physical_cores} Kerne/{self.cpu_logical_cores} Threads · "
            f"{self.ram_total_gb:g} GB RAM · {gpu_text}"
        )


@dataclass
class ModelRecommendation:
    tier: str
    general: str
    coder: str
    fast: str
    embedding: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    notes: list[str] = field(default_factory=list)

    def ollama_pull_commands(self) -> list[str]:
        models = dict.fromkeys([self.general, self.coder, self.fast, self.embedding])
        return [f"ollama pull {m}" for m in models]


# --------------------------------------------------------------------------- Erkennung


def _run(command: list[str], timeout: float = 8.0) -> str | None:
    """Führt ein Kommando ohne Konsolenfenster aus; ``None`` bei Fehlern."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Kommando %s fehlgeschlagen: %s", command[0], exc)
        return None
    if result.returncode != 0:
        log.debug("Kommando %s: Exit-Code %s", command[0], result.returncode)
        return None
    return str(result.stdout)


def parse_nvidia_smi(output: str) -> list[GPUInfo]:
    """Parst ``nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version``."""
    gpus: list[GPUInfo] = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or not parts[0]:
            continue
        gpus.append(
            GPUInfo(
                name=parts[0],
                vendor="nvidia",
                vram_total_mb=_to_int(parts[1]),
                vram_free_mb=_to_int(parts[2]),
                driver=parts[3] or None,
                source="nvidia-smi",
            )
        )
    return gpus


def _to_int(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def _detect_nvidia() -> list[GPUInfo]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return []
    output = _run(
        [
            executable,
            "--query-gpu=name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    return parse_nvidia_smi(output) if output else []


def vendor_from_name(name: str) -> str:
    lowered = name.lower()
    if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered:
        return "nvidia"
    if "amd" in lowered or "radeon" in lowered:
        return "amd"
    if "intel" in lowered:
        return "intel"
    return "other"


def registry_memory_to_bytes(value: object) -> int | None:
    """Registry speichert VRAM als QWORD, DWORD oder Binärwert (little endian)."""
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, bytes | bytearray) and value:
        number = int.from_bytes(value, "little")
        return number if number > 0 else None
    return None


def _detect_windows_registry_gpus() -> list[GPUInfo]:
    if sys.platform != "win32":
        return []
    import winreg

    gpus: list[GPUInfo] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS_KEY)
    except OSError:
        return gpus
    with root:
        index = 0
        while True:
            try:
                sub_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sub_name.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub_name) as key:
                    values: dict[str, object] = {}
                    for value_name in (
                        "DriverDesc",
                        "DriverVersion",
                        "HardwareInformation.qwMemorySize",
                        "HardwareInformation.MemorySize",
                    ):
                        try:
                            values[value_name] = winreg.QueryValueEx(key, value_name)[0]
                        except OSError:
                            continue
            except OSError:  # z. B. Zugriff verweigert auf "Properties"
                continue
            name = str(values.get("DriverDesc") or "").strip()
            if not name or any(token in name.lower() for token in _IGNORED_ADAPTERS):
                continue
            memory = registry_memory_to_bytes(
                values.get("HardwareInformation.qwMemorySize")
            ) or registry_memory_to_bytes(values.get("HardwareInformation.MemorySize"))
            driver = values.get("DriverVersion")
            gpus.append(
                GPUInfo(
                    name=name,
                    vendor=vendor_from_name(name),
                    vram_total_mb=memory // (1024 * 1024) if memory else None,
                    driver=str(driver) if driver else None,
                    source="registry",
                )
            )
    return gpus


def _merge_gpus(nvidia: list[GPUInfo], other: list[GPUInfo]) -> list[GPUInfo]:
    """NVIDIA-Werte aus nvidia-smi haben Vorrang; Duplikate aus der Registry entfallen."""
    merged = list(nvidia)
    seen = {g.name.lower() for g in nvidia}
    for gpu in other:
        if gpu.vendor == "nvidia" and nvidia:
            continue
        if gpu.name.lower() in seen:
            continue
        seen.add(gpu.name.lower())
        merged.append(gpu)
    return merged


def _cpu_name() -> str:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CPU_KEY) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "Unbekannte CPU"


def _os_version() -> str:
    if sys.platform == "win32":
        # Windows 11 meldet sich intern als "10" – Build >= 22000 ist Windows 11.
        version = sys.getwindowsversion()
        product = "11" if version.build >= 22000 else platform.release()
        return f"Windows {product} (Build {version.build})"
    return f"{platform.system()} {platform.release()}"


def detect_hardware(disk_path: Path | None = None) -> HardwareInfo:
    """Ermittelt die Hardware. Dauert typischerweise unter einer Sekunde."""
    memory = psutil.virtual_memory()
    disk_free: float | None = None
    try:
        usage = shutil.disk_usage(disk_path or Path.home())
        disk_free = round(usage.free / 1024**3, 1)
    except OSError:
        pass
    nvidia = _detect_nvidia()
    gpus = _merge_gpus(nvidia, _detect_windows_registry_gpus())
    return HardwareInfo(
        os_name=platform.system(),
        os_version=_os_version(),
        machine=platform.machine(),
        cpu_name=_cpu_name(),
        cpu_physical_cores=psutil.cpu_count(logical=False) or os.cpu_count() or 1,
        cpu_logical_cores=psutil.cpu_count(logical=True) or os.cpu_count() or 1,
        ram_total_gb=round(memory.total / 1024**3, 1),
        ram_available_gb=round(memory.available / 1024**3, 1),
        disk_free_gb=disk_free,
        python_version=platform.python_version(),
        gpus=gpus,
    )


# --------------------------------------------------------------------------- Empfehlung


def recommend_local_models(
    hardware: HardwareInfo, tiers: list[LocalModelTier], *, embedding_model: str
) -> ModelRecommendation:
    """Wählt die erste Stufe (von oben), die die Hardware erfüllt."""
    vram = hardware.usable_vram_gb
    ram = hardware.ram_total_gb
    chosen = tiers[-1]
    for tier in tiers:
        if tier.requires_gpu and (vram < tier.min_vram_gb or hardware.best_gpu is None):
            continue
        if ram < tier.min_ram_gb:
            continue
        chosen = tier
        break

    notes: list[str] = []
    gpu = hardware.best_gpu
    if gpu is None:
        notes.append(
            "Keine nutzbare GPU erkannt – lokale Modelle laufen auf der CPU (langsamer). "
            "Für einfache Aufgaben reicht das, für Planung/Code nutzt JARVIS die Cloud."
        )
    elif gpu.vendor == "amd":
        notes.append(
            "AMD-GPU erkannt: Ollama unterstützt unter Windows nur bestimmte Radeon-Modelle "
            "(ROCm). Falls deine Karte nicht dabei ist, läuft Ollama auf der CPU."
        )
    if gpu is not None and gpu.vram_free_mb is not None and gpu.vram_total_mb:
        used_ratio = 1 - gpu.vram_free_mb / gpu.vram_total_mb
        if used_ratio > 0.5:
            notes.append(
                f"Aktuell sind {used_ratio:.0%} des VRAM belegt (Spiele/andere Programme). "
                "Lokale Modelle können dann langsamer laden oder auf die CPU ausweichen."
            )
    if hardware.disk_free_gb is not None and hardware.disk_free_gb < 30:
        notes.append(
            f"Nur {hardware.disk_free_gb:g} GB frei – lokale Modelle brauchen je 1–20 GB Platz."
        )
    if any(g.vendor == "intel" for g in hardware.gpus) and gpu is None:
        notes.append("Intel-Grafik wird von Ollama derzeit nicht als Beschleuniger genutzt.")

    return ModelRecommendation(
        tier=chosen.name,
        general=chosen.general,
        coder=chosen.coder,
        fast=chosen.fast,
        embedding=embedding_model,
        whisper_model=chosen.whisper_model,
        whisper_device=chosen.whisper_device,
        whisper_compute_type=chosen.whisper_compute_type,
        notes=notes,
    )


def save_hardware_report(
    hardware: HardwareInfo, recommendation: ModelRecommendation, path: Path
) -> None:
    """Schreibt den Report als JSON (für Dashboard und spätere Phasen)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "detected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hardware": asdict(hardware),
        "usable_vram_gb": hardware.usable_vram_gb,
        "gpu_backend": hardware.gpu_backend,
        "recommendation": asdict(recommendation),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
