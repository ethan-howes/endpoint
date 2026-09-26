#!/usr/bin/env python3
"""
Quick GPU/CUDA sanity check, specifically watching for the "unsupported
architecture" warning that can occur with very recent GPUs (e.g. RTX 50-
series / Blackwell, sm_120) on PyTorch builds that predate kernel support
for that compute capability.

Usage:
    python scripts/sanity_check_cuda.py
"""
import sys

try:
    import torch
except ImportError:
    print("ERROR: torch is not installed. Install it first (see requirements.txt).")
    sys.exit(1)

print(f"torch version: {torch.__version__}")
print(f"CUDA available: {torch.cuda.is_available()}")

if not torch.cuda.is_available():
    print("ERROR: CUDA not available to PyTorch. Check driver/toolkit install.")
    sys.exit(1)

device_name = torch.cuda.get_device_name(0)
capability = torch.cuda.get_device_capability(0)
print(f"Device: {device_name}")
print(f"Compute capability: sm_{capability[0]}{capability[1]}")

try:
    a = torch.randn(2000, 2000, device="cuda")
    b = torch.randn(2000, 2000, device="cuda")
    c = a @ b
    torch.cuda.synchronize()
    print(f"Matmul OK, result sample: {c[0, 0].item():.4f}")
except Exception as e:  # noqa: BLE001
    print(f"ERROR running a CUDA matmul: {e}")
    print(
        "If this mentions 'no kernel image is available' or an unsupported "
        "architecture, your PyTorch build likely predates sm_"
        f"{capability[0]}{capability[1]} support -- try a PyTorch nightly "
        "build with a newer CUDA toolkit version."
    )
    sys.exit(1)

print("\nAll checks passed.")
