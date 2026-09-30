# TODO

## 优先 / Priority

- 发行维护：开发版已加入第三方下载选择、许可与版本留存、FFmpeg 声明保留、导出分享材料及发行内容拦截；需随下一版发布。继续核对 CAMPPlus 声明差异、已有资源的版本及模型输出下游条款；评估合成内容标识的完整实现与输入标识保留。免费分享也不能免除这些要求。见 `docs/RELEASE_REVIEW.md`、`docs/LEGAL_REVIEW.md`。

- 中、英、日六向互转已接入开发分支，旧中文输出缓存保持兼容。继续用真实短片验证各方向的分句、翻译、日语读音、跨语言音色与时长；验证完成后另发 0.2 测试版，不覆盖 0.1 里程碑。
- 中英文界面：界面语言与翻译方向分别设置；方向标签随界面语言显示，英文界面可用 ZH → EN、JA → ZH 等简写。选择日语输出不自动切成日文界面。英文界面完成后，再更新英文 README 和对应截图；当前先维护中文文档。
- 手动分句已支持局部波形、拖选新增、逐句试听、撤销重做、草稿和备份；未改句子复用结果，仅删除不调用模型。继续优化选区与快捷键，评估可关闭的静音吸附、应用后的界面内恢复。
- 配音慢句排查：固定译文、参考音频和随机种子，重复对比同一句；记录逐句耗时及显存峰值，再判断长参考、内存调度或模型内部步骤的影响。优化须同时验证音质，不将一次异常当作模型速度排名。
- 综合识别评估：当前以 v3 分句为主，局部补漏不能保证改善说话轮次；先用标注样本评估分句、漏词和时间误差，再决定是否调整默认模式，不直接拼接三模型输出。

## 持续验证 / Further validation

- Qwen 短块识别：继续验证日文、音乐背景、长停顿与跨块词句，核对文字准确性；排查 Windows 底层异常，区分诊断探针和运行库问题。生成超时按步骤检查，仍需评估底层 GPU 调用卡住时的硬超时恢复。
- Qwen 已对明显不可靠的时间定位自动保留原声。继续评估自动语音检测和仅针对可疑片段的跨模型复核，优先减少假台词，允许部分漏识别；不要将文本生成概率当成真实语音置信度，也不要把短块重试得到的有效时间当成台词正确的证明。

- CAMPPlus 自动分组及手动角色管理已加入。继续验证日文对白、短句、情绪变化与一句多人场景的准确性；当前分组仅作为可人工修正的候选，不作为可靠人物识别。
- 若轻量分组效果不足，再评估可离线运行的专用说话人区分流程。
- 验证长视频的界面响应、缓存增长、停止恢复，以及极长 PCM 音轨的文件格式限制。

## English summary

- Six directions are connected on the development branch, with separate projects and exports and compatible Chinese-output caches. Validate real short clips for translation, pronunciation, cross-language voice and timing before a separate 0.2 release; retain the 0.1 milestone.
- Manual segmentation supports bounded waveforms, drag-to-create ranges, preview, undo/redo, drafts and backups. Unchanged lines retain their results; deletion-only edits do not load models. Refine gestures/shortcuts and evaluate optional silence snapping and in-app restoration after applying.
- Reproduce slow synthesis with fixed text, references and seeds; measure per-line runtime and peak VRAM before changing inference behavior.
- Evaluate combined recognition against annotated speaker turns and timings before changing defaults or adding a third-model fusion.
- Validate Japanese dialogue, short/emotional clips, overlapping speakers, long-video responsiveness, cache growth, recovery and very large PCM files. Consider dedicated offline diarization only if lightweight grouping proves insufficient.
