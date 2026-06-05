# GPU Compute — Adreno 630 (freedreno / Turnip)

The POCO F1's **Adreno 630** GPU is usable for general-purpose compute (point clouds,
matrices, signal processing) headlessly. This documents exactly what is installed, how to
reach the GPU from code, and which math libraries to use.

> The phone is headless (no display output), but the GPU **render node**
> `/dev/dri/renderD128` works for off-screen compute. No X/Wayland needed.

## What works

| API | Status | Driver | Version |
|-----|--------|--------|---------|
| **OpenCL** | ✅ working | `rusticl` (Mesa, LLVM-backed) | OpenCL **3.0** (device OpenCL C 1.2) |
| **Vulkan** | ✅ working | `turnip` (Mesa) | Vulkan **1.3.348**, Mesa 26.1.1 |
| OpenGL ES | ✅ present | `freedreno` (Mesa) | GLES via `mesa-gles` |
| CUDA / ROCm | ❌ n/a | — | not NVIDIA/AMD; do not attempt |

GPU reported as **`FD630`** (OpenCL) / **`Turnip Adreno (TM) 630`** (Vulkan).

## Device capabilities (OpenCL / rusticl)

| Property | Value |
|----------|-------|
| Compute units | 2 |
| Max clock | 710 MHz |
| Global memory | ~2.71 GiB (shared with system RAM) |
| Max mem alloc / constant buffer | 64 MiB |
| Local memory | 32 KiB (type: Global) |
| Max work-group size | 2048 |
| Max work-item sizes | 1024 × 1024 × 64 |
| SPIR-V IL | 1.0 – 1.6 |
| Notable extensions | `cl_khr_fp16` (half), `cles_khr_int64`, `cl_khr_3d_image_writes`, `cl_khr_integer_dot_product`, image2d/3d support |

> ⚠️ **No native double precision** advertised — design kernels around `float` (fp32) and
> `half` (fp16). 64-bit integers are available (`cles_khr_int64`).
> Local memory is only 32 KiB and is "Global" type — tiling/blocking that assumes large
> fast shared memory will not help here.

## CRITICAL: enabling rusticl

`rusticl` only exposes the freedreno device when this environment variable is set:

```sh
export RUSTICL_ENABLE=freedreno
```

`clinfo` happens to list the device without it, but **real OpenCL applications must export
`RUSTICL_ENABLE=freedreno`** or they will see zero devices. Set it in the app's systemd
unit (`Environment=RUSTICL_ENABLE=freedreno`) or shell profile.

## Installed packages

```
mesa-vulkan-freedreno   26.1.1     # Turnip Vulkan driver
mesa-rusticl            26.1.1     # OpenCL 3.0 (Rust/LLVM CL state tracker)
vulkan-loader           1.4.347    # the Vulkan ICD loader (REQUIRED, separate pkg)
vulkan-tools            1.4.347    # vulkaninfo, vkcube
opencl-icd-loader       (opencl 2026.05.29)   # libOpenCL.so loader
clinfo                  3.0.x      # OpenCL diagnostics
mesa-gl / mesa-egl / mesa-gbm / mesa-gles      # GL/EGL/GBM/GLES
# pulled in as deps: llvm22-libs, clang22-libs, libclc, spirv-llvm-translator-libs
```

ICD registration files:
- Vulkan: `/usr/share/vulkan/icd.d/freedreno_icd.aarch64.json` → `/usr/lib/libvulkan_freedreno.so`
- OpenCL: `/etc/OpenCL/vendors/rusticl.icd`

Render node: `/dev/dri/renderD128`.

## Which math / compute libraries to use when writing software

Pick based on the workload:

### OpenCL (recommended default for number crunching)
- **PyOpenCL** — Python bindings; pair with **NumPy** for host arrays. Best fit for point
  clouds, matrix ops, custom kernels. Write kernels in **OpenCL C** (device is OpenCL C 1.2).
- **CLBlast** — tuned BLAS (GEMM etc.) on OpenCL, good for matrix multiply.
- **clpeak** — micro-benchmark to measure real throughput.
- Always `export RUSTICL_ENABLE=freedreno` before running.

### Vulkan compute (when you want lower overhead or graphics interop)
- **Kompute** (C++/Python) — general-purpose Vulkan compute framework.
- **VkFFT / pyVkFFT** — fast FFTs on Vulkan, useful for signal processing.
- Shaders are SPIR-V (compile GLSL/HLSL compute with `glslang`/`shaderc`).

### Practical guidance for this GPU
- Use **fp32** as the working type; use **fp16** (`cl_khr_fp16`) to cut memory bandwidth
  where precision allows. **Avoid fp64** (not supported).
- Keep buffers **≤ 64 MiB** per allocation; stream large point clouds in tiles.
- This GPU is modest (2 CUs @ 710 MHz, ~150 GFLOPS-class). It wins on **parallel
  elementwise / vector** work (transforms, filtering, projection of point clouds), not on
  problems dominated by small serial steps. Benchmark vs. the 8-core CPU before committing.

## Quick verification commands

```sh
# OpenCL device present?
RUSTICL_ENABLE=freedreno clinfo | grep -E "Device Name|Driver Version|Max compute units"

# Vulkan device present?
vulkaninfo --summary | grep -E "deviceName|driverName|apiVersion"
```

## Firmware dependency

The GPU needs `a630_sqe.fw` + `a630_gmu.bin` microcode, which must be available at early
boot. See [ARCHITECTURE.md](ARCHITECTURE.md#gpu) and
[SCRIPTS_AND_FILES.md](SCRIPTS_AND_FILES.md) — the firmware is bundled into the initramfs
via `/etc/mkinitfs/files-extra/adreno-fw`. If the GPU stops initializing after a kernel or
firmware update, re-run `sudo mkinitfs` and reboot.
