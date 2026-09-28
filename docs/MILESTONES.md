# 版本里程碑

## 0.1：英／日 → 中文

- 固定标签：[v0.1.0-beta.1](https://github.com/redimocteer/voxlate/tree/v0.1.0-beta.1)。
- 可下载版本：[Windows 测试版及对应源码](https://github.com/redimocteer/voxlate/releases/tag/v0.1.0-beta.1)。
- 维护基线：[milestone/chinese-output](https://github.com/redimocteer/voxlate/tree/milestone/chinese-output)。
- 基线提交：`f419374979acace20123a26d37b8f1ddb5d699d4`。
- 后续修复版：[v0.1.0-beta.2](https://github.com/redimocteer/voxlate/releases/tag/v0.1.0-beta.2)，修复日语识别启动错误并支持框选原文／译文后复制。

三语言开发不移动这个标签，也不替换已有 Release 附件。旧版修复在 `main` 另发新版本，`milestone/chinese-output` 保留初始基线。

## 0.2 开发：中、英、日互转

开发分支 `feature/three-language`，程序版本 `0.2.0-dev`；尚未替换主分支或发布新的公开下载。

- 六个方向独立选择，界面仍为中文；不是自动源语言检测。
- 复用现有多语言 Whisper／Qwen3-ASR、Hy-MT2 7B 和 IndexTTS 2.5，不新增大模型。
- 翻译、配音、手动分句和导出贯穿目标语言；导出的配音音轨标记相应语言。
- 原英→中、日→中项目继续使用原目录及缓存。新方向使用 `zh-en-识别模式` 等目录；译文及配音不跨方向复用。
- 新导出名包含方向，例如 `movie.zh-en.mp4`。旧导出不改名，播放器可使用当前项目记录的旧导出。
- 开发版窗口标明 `0.2.0-dev`，设置和运行代码放在 `%LOCALAPPDATA%/voxlate-dev`，与旧版分开；首次启动可读取旧版资源配置，复用已下载模型，不修改旧版配置。

验证包括自动回归及合成短句的实际模型测试。合成语音回识别只能检查基础链路，不能证明日语自然度、跨语言音色或长片质量；正式发布前仍需真实短片试听。

具体测试结果与已知限制见 [三语言验证记录](THREE_LANGUAGE_VALIDATION.md)。

发布时另定 `v0.2.0-…` 版本并更新发布元数据、下载说明和验证记录。打包脚本检查版本一致性，拒绝写入已有版本的发行目录。
