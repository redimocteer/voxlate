# TODO

## 优先 / Priority

- 发行维护：Windows 目录包已加入许可、文件清单和对应依赖源码附件；升级依赖时重新核对。继续完善另行下载资源的许可保留和版本记录；发布模型整合包或商业服务前，核对 IndexTTS 条款和生成内容标识。见 `docs/RELEASE_REVIEW.md`。

- 中、英、日六向互译：沿用现有识别、Hy-MT2 7B 和 IndexTTS 2.5，增加六个语言方向；接通目标语言、翻译提示及配音语言，按语言方向隔离译文、配音缓存和导出，兼容旧项目。保持本地处理，分别验证各方向的翻译、读音、跨语言音色和时长。
- 中英文界面：界面语言与翻译方向分别设置；方向标签随界面语言显示，英文界面可用 ZH → EN、JA → ZH 等简写。选择日语输出不自动切成日文界面。英文界面完成后，再更新英文 README 和对应截图；当前先维护中文文档。
- 手动分句已支持局部波形、拖选新增、逐句试听、撤销重做、草稿和备份；未改句子复用结果，仅删除不调用模型。继续优化选区与快捷键，评估可关闭的静音吸附、应用后的界面内恢复。
- 配音慢句排查：固定译文、参考音频和随机种子，重复对比同一句；记录逐句耗时及显存峰值，再判断长参考、内存调度或模型内部步骤的影响。优化须同时验证音质，不将一次异常当作模型速度排名。
- 综合识别评估：当前以 v3 分句为主，局部补漏不能保证改善说话轮次；先用标注样本评估分句、漏词和时间误差，再决定是否调整默认模式，不直接拼接三模型输出。

## 持续验证 / Further validation

- Qwen 短块识别：继续验证日文、音乐背景、长停顿与跨块词句，核对文字准确性；排查 Windows 底层异常，区分诊断探针和运行库问题。生成超时按步骤检查，仍需评估底层 GPU 调用卡住时的硬超时恢复。

- CAMPPlus 自动分组及手动角色管理已加入。继续验证日文对白、短句、情绪变化与一句多人场景的准确性；当前分组仅作为可人工修正的候选，不作为可靠人物识别。
- 若轻量分组效果不足，再评估可离线运行的专用说话人区分流程。
- 验证长视频的界面响应、缓存增长、停止恢复，以及极长 PCM 音轨的文件格式限制。

## English summary

- Add all six translation directions among Chinese, English and Japanese using the existing ASR, Hy-MT2 7B and IndexTTS 2.5 models. Carry the target language through prompts and synthesis; separate translation, speech caches and exports by direction while preserving existing projects. Keep processing local and validate translation, pronunciation, cross-language voice and timing for each direction.
- Manual segmentation supports bounded waveforms, drag-to-create ranges, preview, undo/redo, drafts and backups. Unchanged lines retain their results; deletion-only edits do not load models. Refine gestures/shortcuts and evaluate optional silence snapping and in-app restoration after applying.
- Reproduce slow synthesis with fixed text, references and seeds; measure per-line runtime and peak VRAM before changing inference behavior.
- Evaluate combined recognition against annotated speaker turns and timings before changing defaults or adding a third-model fusion.
- Validate Japanese dialogue, short/emotional clips, overlapping speakers, long-video responsiveness, cache growth, recovery and very large PCM files. Consider dedicated offline diarization only if lightweight grouping proves insufficient.
