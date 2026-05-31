from services.hwfit import hardware


def test_remote_macos_detects_apple_silicon_metal(monkeypatch):
    responses = {
        "sysctl -n hw.memsize 2>/dev/null": str(16 * 1024**3),
        "vm_stat 2>/dev/null": (
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            "Pages free: 1024.\n"
            "Pages inactive: 2048.\n"
            "Pages speculative: 1024.\n"
        ),
        "sysctl -n hw.logicalcpu 2>/dev/null": "10",
        "sysctl -n machdep.cpu.brand_string 2>/dev/null": "Apple M3 Pro",
        "uname -m 2>/dev/null": "arm64",
    }

    monkeypatch.setattr(hardware, "_run", lambda cmd: responses.get(cmd))
    hardware._cache_by_host.clear()

    result = hardware.detect_system(host="me@mac", platform="macos", fresh=True)

    assert result["total_ram_gb"] == 16.0
    assert result["cpu_cores"] == 10
    assert result["backend"] == "metal"
    assert result["has_gpu"] is True
    assert result["gpu_vram_gb"] == 12.0
    assert result["gpus"][0]["name"] == "Apple M3 Pro GPU"
