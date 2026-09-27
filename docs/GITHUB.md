# 从建仓库到发布下载包

目标仓库：[redimocteer/voxlate](https://github.com/redimocteer/voxlate)。首版标签：`v0.1.0-beta.1`，公开测试版。此页是可重复使用的操作说明，不代表每一步已在远端完成。

## 1. 创建空仓库

登录 GitHub，打开 <https://github.com/new>，Owner 选 `redimocteer`，名称 `voxlate`，可见性 **Public**。不要自动生成 README、License 或 .gitignore，本地已经有。

Description 填：

> Windows 本地视频配音工具｜英/日→中文、音色克隆、角色分组、逐句试听与重配、手动分句。Offline video dubbing with sentence-level editing.

## 2. 初始化和提交源码

在源码仓库目录打开 PowerShell。以下使用 GitHub 公开用户名和 noreply 邮箱；已有仓库无需重复初始化。

```powershell
git init -b main
git config user.name redimocteer
git config user.email 334384166+redimocteer@users.noreply.github.com
git add .
git diff --cached --stat
git diff --cached --name-only
```

确认清单只有源码、测试、文档和示例图，不包含真实视频、对白、模型、凭据、临时目录或安装包，再提交：

```powershell
git commit -m "Prepare first Windows beta release"
```

`.gitignore` 不会移除已经跟踪的文件，每次提交仍需检查清单。

## 3. 登录并推送

安装 GitHub 官方命令行工具后，可通过 `gh auth login --web` 登录。密码和授权码只在 GitHub 页面填写，不放进代码或远程地址。

```powershell
git remote add origin https://github.com/redimocteer/voxlate.git
git push -u origin main
```

如果 origin 已存在，先执行 `git remote -v` 核对，不重复添加，不使用强制推送。

## 4. 填写 About 和 Topics

在仓库首页右侧 **About → 齿轮**，填写第 1 步的 Description，Topics 填：

```text
video-dubbing voice-cloning offline speech-recognition translation text-to-speech speaker-diarization windows python pyside6
```

同样的值保存在 [GITHUB_METADATA.json](GITHUB_METADATA.json)。Topics 帮助搜索归类，不保证获得推荐流量。不添加嘴型同步、六向互转等尚未实现的标签。

## 5. 准备版本附件

普通用户不需要执行构建。维护者按 README 安装构建依赖后，可执行：

```powershell
python scripts/prepare_release.py
```

脚本构建目录版 EXE，下载对应 Qt / PySide6 / FFmpeg 源码，收集许可，生成文件清单与 SHA-256；不会登录或上传 GitHub。当前锁定 Qt 6.8.3，升级库后必须同步核对来源清单。已有同名候选目录时脚本会停止，避免混入旧文件。

输出在 `dist/releases/v0.1.0-beta.1/`：

- `Voxlate-v0.1.0-beta.1-windows-x64.zip`：用户运行包。
- `Voxlate-v0.1.0-beta.1-third-party-sources.zip`：对应第三方源码，用户运行时不需要下载。
- `SHA256SUMS.txt`：两份附件的校验值。

必须先完成 [发行核对](RELEASE_REVIEW.md)，解压到独立测试目录，检查 EXE 启动、播放器、截图和许可材料。不包含模型和用户项目；不要上传整个 `dist` 或资源目录。

## 6. 发布公开测试版

在 GitHub 仓库点击 **Releases → Draft a new release**：

1. 创建标签 `v0.1.0-beta.1`，Target 选择本轮完成检查的提交。
2. Title：`Voxlate v0.1.0-beta.1 · Windows 测试版`。
3. 正文复制 [首版发布说明](RELEASE_NOTES_v0.1.0-beta.1.md)。
4. 上传上面三份附件，等待全部上传完成。
5. 勾选 **This is a pre-release**。测试版不使用 `releases/latest` 作为下载入口。
6. 核对无误后 **Publish release**；还有未解决的发行检查项时使用 **Save draft**，草稿附件不对公众可见。

正式发布时将 README 和 [下载页](DOWNLOAD.md)的“正在准备”改为已发布，并链接准确的版本页。退出登录后再检查仓库、截图、下载链接和附件是否可访问。

## 7. 后续更新

```powershell
git status
git add .
git diff --cached --stat
git commit -m "Describe this change"
git push
```

新版使用新标签，例如 `v0.1.0-beta.2`；不要覆盖旧版本附件或移动已发布标签。中日英互转可在 `feature/language-directions` 分支开发，验证后合入。

## 可以让助手代办哪些步骤

可以代办本地整理、构建检查、提交推送、创建仓库、填写 About/Topics、准备及发布 Release。前提是你指定账号和公开范围，并通过 GitHub 官方流程授权。密码、双重验证、设备授权由你自己在浏览器完成，不需要发送凭据。

官方参考：[首次推送](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)、[Topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics)、[发布版本](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)。
