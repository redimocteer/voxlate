# 第三方组件与模型说明

核对日期：2026-09-27。此文是来源索引和主要注意事项，不替代完整许可证，也不是完整的软件物料清单或法律合规认证。下表以本项目配置及所链接的上游说明为依据；模型卡标签不能替代实际版本的许可正文。

Voxlate 的 MIT 仅适用于贡献者有权授权的原创部分。各第三方名称、商标、代码与权重归各自权利人所有；引用名称是为了标明来源，不表示合作或背书。模型通常由用户通过资源配置另行下载，源码仓库不包含这些权重。另行下载也不免除使用与分发义务。

## 主要模型与推理组件

| 组件与来源 | 用途／许可线索 | 本项目使用情况 |
| --- | --- | --- |
| [OpenAI Whisper](https://github.com/openai/whisper/blob/main/LICENSE)；转换权重 [SYSTRAN large-v3](https://huggingface.co/Systran/faster-whisper-large-v3)、[Dropbox turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo) | 识别；MIT | 下载转换权重，应同时保留原模型及转换来源的声明 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE)、[CTranslate2](https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE) | 识别运行库；MIT | 安装在独立 Python 环境；传递依赖仍有各自条款 |
| [Qwen3-ASR 1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)、[ForcedAligner 0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | 可选识别与时间对齐；官方标注 Apache 2.0 | 单独下载，保留许可、版权及适用的 NOTICE |
| [Hy-MT2 7B GGUF](https://huggingface.co/tencent/Hy-MT2-7B-GGUF)、[固定版本许可](https://huggingface.co/tencent/Hy-MT2-7B-GGUF/resolve/ab8472660ac61fac25f1af43fac2599d52a8a775/LICENSE.txt) | 翻译；已核实该版本正文为 Apache 2.0 | Q4_K_M；不可套用旧 HY-MT／Hunyuan 版本协议。开发版下载器同步保留许可与模型说明 |
| [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) | 翻译推理程序；MIT | 下载 Windows Vulkan 构建；其随附运行库应分别核对 |
| [Demucs](https://github.com/facebookresearch/demucs#license) | 人声分离；上游以 MIT 发布代码与模型 | 自动准备 htdemucs；其他自选模型另行核对 |
| [IndexTTS 固定源码版本](https://github.com/index-tts/index-tts/blob/ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c/LICENSE)、[IndexTTS 2.5 模型协议](https://huggingface.co/IndexTeam/IndexTTS-2.5/blob/main/LICENSE) | 音色克隆；bilibili Model Use License Agreement，非 MIT | 专用条款涉及模型、输出与衍生物、下游分发及用途，不能仅按普通宽松开源许可理解 |
| [w2v-BERT 2.0](https://huggingface.co/facebook/w2v-bert-2.0)、[BigVGAN](https://huggingface.co/nvidia/bigvgan_v2_22khz_80band_256x) | 克隆辅助模型；模型卡标注 MIT | 各自下载，不能只保留 IndexTTS 的许可 |
| [FunASR CAMPPlus](https://huggingface.co/funasr/campplus)、[上游模型协议](https://github.com/modelscope/FunASR/blob/2d4566d4a4c84d73e1f828efa2dedfa88f33677f/MODEL_LICENSE) | 声纹特征；模型卡标注 Apache 2.0，上游另有模型专用协议 | 两份声明均保留，需用户核对适用条件，不宣称无条件商用；分组不是真实人物身份认证 |

IndexTTS 协议尤其需要注意：其衍生物定义包含输出；用户或关联方达到前一月月活超过一亿、或前一年年收入超过人民币十亿元等约定条件时，需要另行书面授权。还存在许可与版权声明保留、下游义务及用于改进其他 AI 模型的限制。未达到门槛也不等于可以忽略其余条款。以上只是摘要，请阅读对应版本的完整协议。

其第 3.4 条还要求向下游落实协议并保留原始声明及协议副本，第 4.1(a) 条规定分发衍生物时的非背书声明。开发版导出时在项目 `export-notices/` 中保存参考协议及说明；使用者仍需核对实际模型、附带所需材料并落实下游条款。该功能不代表自动满足所有义务，旧版 EXE 尚不包含。发布生成音视频时也应核对，不只是重新打包模型时才需要考虑。

任何对原模型的修改均不代表原权利人的认可、担保或保证，原权利人不对这些修改承担责任。Voxlate 也不代上游承诺生成内容可以任意传播或商用。

## 桌面程序与安装工具

| 组件 | 许可与发行注意事项 |
| --- | --- |
| [Qt / PySide6 / Shiboken](https://doc.qt.io/qtforpython-6/licenses.html) | 涉及 LGPL/GPL 或商业授权。实际使用模块、插件和 Qt 自带第三方库须逐项核对；MIT 不覆盖它们。详见 [Qt 官方 LGPL 义务说明](https://www.qt.io/development/open-source-lgpl-obligations) |
| [PyInstaller](https://pyinstaller.org/en/stable/license.html) | GPL 并带有允许分发构建产物的例外；该例外不会免除被打包依赖的许可证义务 |
| [FFmpeg](https://ffmpeg.org/legal.html) | 基础许可及可选 GPL 组件取决于实际构建；项目下载 [Gyan essentials](https://www.gyan.dev/ffmpeg/builds/) 并调用独立进程，不能将此二进制直接标成 MIT。分发还需检查编解码器／专利及所在地要求 |
| Python、uv、PyTorch、TorchAudio、Transformers、Hugging Face Hub、SentencePiece、Sacremoses 及其他传递依赖 | 由各包实际版本的许可证与 NOTICE 约束。此处未穷举每个二进制、CUDA 运行库或 Python 包，不代表未列项目没有义务 |

## 当前核对范围与版本

- 查阅了源码依赖清单、下载器、打包配置及主要上游许可页面；没有对每行 AI 辅助代码进行外部相似度／完整版权溯源审计。
- Whisper large-v3 固定 revision：`edaa852ec7e145841d8ffdb056a99866b5f0a478`；turbo：`0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`。
- Hy-MT2 GGUF 固定 revision：`ab8472660ac61fac25f1af43fac2599d52a8a775`；llama.cpp：`b11157`。已取得该 GGUF 版本的 `LICENSE.txt`，正文为 Apache 2.0，11,635 字节，SHA-256：`746750afa6af28fe4f8b326751ad2a40c700d2e5c459c0a1f6a2e76d99ace224`。这是单项许可核验，不代表全部依赖和用途已完成审计。
- Qwen 两项 revision 见 `voxlate/qwen_manifest.py`；IndexTTS 源码固定 revision 为上述链接。开发版的 IndexTTS 2.5 和三项辅助权重 revision 见 `voxlate/resource_terms.py`；下载声明及固定来源保存在资源 `licenses/`。旧版及用户自选模型应核对各自版本，不能据此推定一致。
- 本机打包依赖中 PySide6/Shiboken 为 6.8.3，元数据列有 LGPL/GPL 选项；这不能证明最终 EXE 已满足全部分发条件。

## 再分发边界

分发源码、EXE、模型整合包以及用模型生成的内容，是不同的许可与权利问题。README 链接和感谢列表不替代应随分发物提供的许可正文、版权、NOTICE、对应源码或可替换／重链接安排。

本次 Windows 目录版提供随包许可、文件清单、Qt 库替换说明及同版本依赖源码附件，见 [二进制分发记录](docs/BINARY_DISTRIBUTION.md)。这不覆盖用户另外下载的整套模型与环境；不要将资源目录上传为“全 MIT”整合包。后续版本仍按 [发行检查](docs/RELEASE_REVIEW.md)逐项核对。
