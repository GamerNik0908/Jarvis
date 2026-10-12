from __future__ import annotations

import json
from pathlib import Path

from jarvis.core.config import PROJECT_ROOT, load_config
from jarvis.core.hardware import (
    GPUInfo,
    HardwareInfo,
    detect_hardware,
    parse_nvidia_smi,
    recommend_local_models,
    registry_memory_to_bytes,
    save_hardware_report,
    vendor_from_name,
)

TIERS = load_config(PROJECT_ROOT / "config.yaml").config.hardware.local_model_tiers


def _hw(ram: float = 32, gpus: list[GPUInfo] | None = None, disk: float = 500) -> HardwareInfo:
    return HardwareInfo(
        os_name="Windows",
        os_version="Windows 11 (Build 26100)",
        machine="AMD64",
        cpu_name="Test CPU",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        ram_total_gb=ram,
        ram_available_gb=ram / 2,
        disk_free_gb=disk,
        python_version="3.12.7",
        gpus=gpus or [],
    )


def _nvidia(vram_gb: float, free_gb: float | None = None) -> GPUInfo:
    free = int((free_gb if free_gb is not None else vram_gb) * 1024)
    return GPUInfo(
        name="NVIDIA GeForce RTX",
        vendor="nvidia",
        vram_total_mb=int(vram_gb * 1024),
        vram_free_mb=free,
        source="nvidia-smi",
    )


def _recommend(hw: HardwareInfo) -> str:
    return recommend_local_models(hw, TIERS, embedding_model="nomic-embed-text").tier


def test_parse_nvidia_smi() -> None:
    output = (
        "NVIDIA GeForce RTX 4080, 16376, 15000, 560.94\n"
        "NVIDIA GeForce GTX 1060, 6144, 6000, 560.94\n"
    )
    gpus = parse_nvidia_smi(output)
    assert [g.name for g in gpus] == ["NVIDIA GeForce RTX 4080", "NVIDIA GeForce GTX 1060"]
    assert gpus[0].vram_total_mb == 16376
    assert gpus[0].vram_free_mb == 15000
    assert gpus[0].driver == "560.94"
    assert gpus[0].accelerated


def test_parse_nvidia_smi_ignores_garbage() -> None:
    assert parse_nvidia_smi("No devices were found\n") == []


def test_registry_memory_formats() -> None:
    assert registry_memory_to_bytes(8 * 1024**3) == 8 * 1024**3
    assert registry_memory_to_bytes((4 * 1024**3).to_bytes(8, "little")) == 4 * 1024**3
    assert registry_memory_to_bytes(b"") is None
    assert registry_memory_to_bytes(None) is None


def test_vendor_detection() -> None:
    assert vendor_from_name("NVIDIA GeForce RTX 4090") == "nvidia"
    assert vendor_from_name("AMD Radeon RX 7900 XTX") == "amd"
    assert vendor_from_name("Intel(R) UHD Graphics 770") == "intel"


def test_tier_selection_by_vram() -> None:
    assert _recommend(_hw(gpus=[_nvidia(24)])) == "gpu-24gb"
    assert _recommend(_hw(gpus=[_nvidia(16)])) == "gpu-16gb"
    assert _recommend(_hw(gpus=[_nvidia(12)])) == "gpu-12gb"
    assert _recommend(_hw(gpus=[_nvidia(8)])) == "gpu-8gb"
    assert _recommend(_hw(gpus=[_nvidia(6)])) == "gpu-6gb"


def test_small_gpu_falls_back_to_cpu_tiers() -> None:
    assert _recommend(_hw(ram=32, gpus=[_nvidia(4)])) == "cpu-32gb"
    assert _recommend(_hw(ram=16, gpus=[_nvidia(4)])) == "cpu-16gb"
    assert _recommend(_hw(ram=8)) == "minimal"


def test_integrated_graphics_is_not_used() -> None:
    igpu = GPUInfo(name="Intel(R) UHD Graphics", vendor="intel", vram_total_mb=128)
    hw = _hw(ram=16, gpus=[igpu])
    rec = recommend_local_models(hw, TIERS, embedding_model="nomic-embed-text")
    assert rec.tier == "cpu-16gb"
    assert hw.gpu_backend == "cpu"
    assert any("Intel" in note for note in rec.notes)


def test_best_gpu_is_the_largest_accelerated_one() -> None:
    hw = _hw(gpus=[_nvidia(8), _nvidia(24)])
    assert hw.best_gpu is not None and hw.usable_vram_gb == 24
    assert hw.gpu_backend == "cuda"


def test_notes_for_busy_vram_and_low_disk() -> None:
    rec = recommend_local_models(
        _hw(gpus=[_nvidia(16, free_gb=4)], disk=12), TIERS, embedding_model="e"
    )
    assert any("VRAM belegt" in note for note in rec.notes)
    assert any("frei" in note for note in rec.notes)


def test_pull_commands_are_deduplicated() -> None:
    rec = recommend_local_models(_hw(gpus=[_nvidia(16)]), TIERS, embedding_model="nomic-embed-text")
    commands = rec.ollama_pull_commands()
    assert len(commands) == len(set(commands))
    assert "ollama pull nomic-embed-text" in commands


def test_detect_hardware_on_this_machine(tmp_path: Path) -> None:
    hw = detect_hardware(tmp_path)
    assert hw.cpu_logical_cores >= 1
    assert hw.ram_total_gb > 0
    rec = recommend_local_models(hw, TIERS, embedding_model="nomic-embed-text")
    report = tmp_path / "hardware.json"
    save_hardware_report(hw, rec, report)
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["recommendation"]["tier"] == rec.tier
