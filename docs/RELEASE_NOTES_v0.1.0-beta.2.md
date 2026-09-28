# Voxlate v0.1.0-beta.2 · 日语识别修复与文字复制

修复选择「日文 → 中文」、使用单个 Whisper v3 或 turbo 模型时，识别在模型加载后报 `AttributeError: 'WhisperModel' object has no attribute 'is_multilingual'` 并退出的问题。

新增原文和译文复制：拖动框选多个文字格后按 `Ctrl+C`；多句按行排列，同时选择原文和译文时按两列复制。`Ctrl+单击` 可选择单个或不连续的文字格，不改变句子的选取／舍弃状态。译文编辑时仍可只复制选中的部分文字。

## 下载与更新

- 下载 **Voxlate-v0.1.0-beta.2-windows-x64.zip**，完整解压后运行 `voxlate.exe`，保留 `_internal` 文件夹；无需预装 Python。
- 先退出旧版，再运行新版。默认沿用原配置和已安装模型，无需重新下载模型；使用自定义数据目录的用户继续指定原目录。
- 回到原视频项目重试「识别」。不需要清空项目；已经完成的其他结果仍保存在原项目目录。
- `SHA256SUMS.txt` 提供附件校验值；`third-party-sources.zip` 为对应依赖源码，普通用户无需下载。

## 范围与验证

本版仍是英文／日文 → 中文测试版，不包含三语开发功能。修正模型属性访问，并补充能够复现此错误的回归检查；已使用本机安装的 Whisper turbo 和合成静音音频验证日语识别入口。

程序包不含 AI 模型、外部推理环境或用户媒体。首次准备资源前请阅读 [下载选择与上游条款](https://github.com/redimocteer/voxlate/blob/main/docs/RESOURCE_DOWNLOADS.md)。原有模型许可和使用限制继续适用；开发分支的逐项下载确认及导出说明未包含在本版中。

默认需要 NVIDIA CUDA 显卡及数十 GB 资源空间；最低硬件要求、全新机器安装及长视频稳定性未系统验证。自动结果仍需校对，本次检查不代表日语识别质量评测。

有问题请到 [Issues](https://github.com/redimocteer/voxlate/issues) 反馈版本和复现步骤；分享日志前删去私人路径、对白和凭据。
