# libsurvive (Windows build)

`bin/` holds a prebuilt 64-bit Windows build of [libsurvive](https://github.com/collabora/libsurvive)
at upstream commit `44b1e2f0cb95e5697bc0b86f58fd8520a813b713` with
`patches/0001-windows-mingw-studio.patch` applied. The public Windows release could not
enumerate the tracker / Watchman dongle on our PC, so it was rebuilt from source.

| file | purpose |
|---|---|
| `libsurvive.dll`, `plugins/*.dll` | core library and the drivers/posers it loads from `plugins/` next to it |
| `libc++.dll`, `libunwind.dll` | C++ runtime from llvm-mingw (Apache-2.0 WITH LLVM-exception) |
| `survive-cli.exe` | upstream command-line tool, handy for diagnostics |

The patch contains:

- MinGW / clang compiler flags and Windows header fixes
- Windows USB enumeration filtering and the HID pairing report
- `studio_metrics.c`: a small exported function reporting per-device optical age, IMU and
  battery state, used by `survive_worker.py`

## Rebuilding

Toolchain used: [llvm-mingw](https://github.com/mstorsjo/llvm-mingw) 20261104 (UCRT, x86_64),
CMake and Ninja (both available from pip).

```powershell
git clone https://github.com/collabora/libsurvive
cd libsurvive
git checkout 44b1e2f0cb95e5697bc0b86f58fd8520a813b713
git apply ..\patches\0001-windows-mingw-studio.patch
cmake -S . -B build-win -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_C_COMPILER=x86_64-w64-mingw32-clang -DCMAKE_CXX_COMPILER=x86_64-w64-mingw32-clang++ `
  -DBUILD_APPLICATIONS=OFF -DENABLE_TESTS=OFF -DUSE_EIGEN=ON -DLIB_INSTALL_DIR=bin/
cmake --build build-win
```

Copy `build-win/libsurvive.dll`, `build-win/plugins/*.dll` and the llvm-mingw
`libc++.dll` / `libunwind.dll` into `bin/`.

libsurvive is MIT licensed; see `LICENSE`.
