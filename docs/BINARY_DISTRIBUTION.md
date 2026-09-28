# Windows 二进制分发记录

本次使用 PyInstaller 的目录模式：`voxlate.exe` 和 `_internal/` 必须一同保留。Qt / PySide6 / Shiboken 库以独立 DLL／PYD 文件提供，不加密、不锁定库哈希。

## Qt 与用户修改

本应用使用 Qt / PySide6 / Shiboken 6.8.3 的 LGPL 授权选项，具体第三方组件仍适用各自许可。用户可为修改这些库而进行必要的调试、逆向工程、替换和重新链接，不受本项目额外限制。

关闭程序后，可在发行目录 `_internal` 及其 `PySide6`、`shiboken6` 子目录替换 ABI、架构及 Python 版本兼容的库。更换版本通常需要重建绑定；完整应用源码及构建步骤见仓库 README，构建脚本不要求私有密钥。请在副本中操作。

应用未使用、也不分发 Qt Virtual Keyboard 插件和 Qt PDF 图像插件。发行清单需确认相应插件及 DLL 均未混入。Qt 自带的 FFmpeg 播放库与资源页另下载的 FFmpeg 程序是不同构建，不应混为一项。

## 本次构建的记录方式

- `BUILD_INFO.json`：构建 Python、PyInstaller、PySide6、Shiboken 版本。
- `FILE_MANIFEST.json`：发行目录内文件的相对路径、大小与 SHA-256，不包含构建机器路径。
- `licenses/`：本地包附带许可和源码归档内的许可、版权、NOTICE。
- `THIRD_PARTY_SOURCES.json`：对应上游源码归档的 URL、文件名与 SHA-256。
- 同版本依赖源码附件：提供已收集的上游源码；用户无需下载此附件即可运行。

对应源码从本版本 [Release 附件](https://github.com/redimocteer/voxlate/releases/tag/v0.1.0-beta.2)下载 `Voxlate-v0.1.0-beta.2-third-party-sources.zip`，无需注册、付费或联系维护者。内含未经 Voxlate 修改的 Qt 6.8.3（qtbase、qtdeclarative、qtmultimedia、qtsvg、qtimageformats）、PySide/Shiboken 6.8.3 和 FFmpeg 7.1 的完整上游源码归档与构建文件。

播放器 FFmpeg 7.1 使用上游 PySide6 wheel 的构建，库报告 LGPL 2.1-or-later，配置如下：

```text
--prefix=installed --disable-programs --disable-doc --disable-debug --enable-network --disable-lzma --enable-pic --disable-vulkan --disable-v4l2-m2m --disable-decoder=truemotion1 --toolchain=msvc --enable-shared --disable-static
```

Python 随附许可及其完整依赖声明、OpenSSL 3.5.8 的 Apache 2.0 许可、libffi 许可、Mesa llvmpipe 声明也在 `licenses/` 中。源码归档中另收集了 Qt 的第三方版权及许可；记录不代表应用使用了归档中的每个可选组件。Windows 运行库的特定分发条件仍适用，见 Python 随附许可中的 Microsoft Distributable Code 段落。

这些记录不是法律认证。升级或重新打包时应再次核对 DLL 的来源、版本、对应源码与声明。具体状态见 [发行检查](RELEASE_REVIEW.md)。
