# 下载与安装

当前版本：[v0.1.0-beta.1 · Windows 测试版](https://github.com/redimocteer/voxlate/releases/tag/v0.1.0-beta.1)。自动结果仍需校对，请先用短片测试。

## 下载哪个文件

打开 [Voxlate Releases](https://github.com/redimocteer/voxlate/releases)，查看标有 **Pre-release** 的测试版。

- 普通用户：`Voxlate-v0.1.0-beta.1-windows-x64.zip`。
- 校验文件：`SHA256SUMS.txt`，用于比对下载文件的 SHA-256。
- `Voxlate-v0.1.0-beta.1-third-party-sources.zip` 是对应依赖源码，运行时无需下载。
- `Source code (zip)` / `Source code (tar.gz)` 是源码，不能直接双击运行。

## 第一次运行

1. 完整解压到可写目录，例如 `D:\Apps\Voxlate`；不要在压缩包内直接运行，也不要只搬走 EXE。
2. 双击 `voxlate.exe`，无需预装 Python。
3. 进入 **资源配置**，选择磁盘空间足够的目录，点击 **一键准备**。模型与环境需要另外下载，合计数十 GB。
4. 拖入视频，选择音轨及 **英文 → 中文** 或 **日文 → 中文**。
5. 点击 **一键导出**；需要校对时按四步分别执行，在句子右侧试听和重配。

当前默认需要 NVIDIA CUDA 显卡；最低内存／显存尚未系统验证，先用短片测试。详细功能和限制见 [README](../README.md)。

## 文件与更新

- 视频项目保存在视频旁的 `视频名.voxlate` 文件夹；导出默认也在视频旁。
- 配置和共享资源默认在 `%LOCALAPPDATA%\voxlate`。删除程序文件夹不会删除它们，也不会删除视频项目。
- 更新时先退出旧程序，将新版解压到独立文件夹；保留完整发行目录。修改重要项目之前，先备份其 `.voxlate` 文件夹。
- 程序包未做商业代码签名。遇到系统提示，先确认下载来源与哈希；不要关闭杀毒软件或系统保护。

## 反馈

请到 [Issues](https://github.com/redimocteer/voxlate/issues) 说明版本、Windows 版本、显卡、发生在哪一步及最短复现过程。日志可能包含对白和本地路径，分享前先删去私人信息；不要上传无授权的视频、声音或凭据。
