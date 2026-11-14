# GPU Compute — Adreno 630 (freedreno / Turnip)

The Adreno 630 is usable for headless general-purpose compute through the render node
`/dev/dri/renderD128` (no X/Wayland). roadsense runs its road model on it (ncnn/Vulkan).

## What works

| API | Status | Driver | Version |
|-----|--------|--------|---------|
| **OpenCL** | working | `rusticl` (Mesa, LLVM-backed) | OpenCL **3.0** (device OpenCL C 1.2) |
| **Vulkan** | working | `turnip` (Mesa) | Vulkan **1.3.348**, Mesa 26.1.1 |
| OpenGL ES | present | `freedreno` (Mesa) | GLES via `mesa-gles` |
| CUDA / ROCm | n/a | — | not NVIDIA/AMD; do not attempt |

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

No native double precision is advertised: use `float` (fp32) and `half` (fp16).
64-bit integers are available (`cles_khr_int64`). Local memory is 32 KiB of "Global"
type, so tiling that assumes large fast shared memory does not help.

## Enabling rusticl

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

## Libraries

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
- 2 CUs @ 710 MHz, ~150 GFLOPS class (estimate, not benchmarked here): suited to
  parallel elementwise/vector work, not to small serial steps. Benchmark against the
  CPU before committing.

## Quick verification commands

```sh
# OpenCL device present?
RUSTICL_ENABLE=freedreno clinfo | grep -E "Device Name|Driver Version|Max compute units"

# Vulkan device present?
vulkaninfo --summary | grep -E "deviceName|driverName|apiVersion"
```

## Firmware dependency

The GPU needs the `a630_sqe.fw` + `a630_gmu.bin` microcode at early boot; it is bundled
into the initramfs via `/etc/mkinitfs/files-extra/adreno-fw`
([ARCHITECTURE.md](ARCHITECTURE.md#gpu-firmware)). If the GPU stops initializing after a
kernel or firmware update, run `sudo mkinitfs` and reboot.
