# my-docker-images

这个仓库用于维护基于 Docker Hub 或 GHCR 公开镜像的小型扩展。GitHub Actions 每天检查上游标签，按订阅规则选择最新版，将上游清单摘要固定到生成的 Dockerfile，再构建并发布到 GHCR。同一标签的上游摘要变化也会触发重建。镜像发布成功后，工作流才将 Dockerfile 和发布状态提交到 `master`。

## 添加镜像

仓库根目录的 `README.md` 和 `.github/` 放公共文件，其他一级目录各对应一个镜像。新建 `<名称>/subscription.yaml` 和 `<名称>/Dockerfile.template`；目录名就是 GHCR 镜像名。建议先设 `enabled: false`，运行检查模式，确认选中的版本和平台后再启用。`alpine/` 是默认禁用的示例。订阅文件第一行用相对路径关联 [订阅 schema](.github/subscription.schema.json)，编辑器可据此检查配置。

```yaml
# yaml-language-server: $schema=../.github/subscription.schema.json
enabled: true
upstream: docker.io/library/alpine
tag_regex: '^\d+\.\d+\.\d+$'
sort: semver
platforms:
  - linux/amd64
  - linux/arm64
```

- `upstream` 必须是以 `docker.io/` 或 `ghcr.io/` 开头的完整仓库名。Docker Hub 官方镜像使用 `docker.io/library/<名称>`。
- `tag_regex` 是匹配**整个标签**的 Python 正则表达式，用于限定可选择的上游版本。
- `sort` 可为 `semver`（默认）或 `lexicographic`。前者要求所有匹配的标签都是有效语义版本；日期型标签可使用后者，但数字字段宜补零。
- `platforms` 按偏好顺序列出目标 Linux 平台。实际构建平台取该列表与上游支持平台的交集；没有交集则报错。以后可在配置中增加构建器支持的其他平台。

模板至少需要一行 `FROM <upstream>:${latest}`。`${latest}` 只能用于这类 `FROM` 行，渲染后为 `<选中标签>@sha256:<上游清单摘要>`，例如 `FROM docker.io/library/alpine:3.24.2@sha256:...`。其他 Dockerfile 指令照常编写。`COPY` 和 `ADD` 使用的文件放在同一镜像目录下；订阅配置、模板、生成的 Dockerfile 和状态文件不会进入构建上下文。

发布后，镜像位于 `ghcr.io/<仓库所有者>/<仓库名>/<镜像目录名>`，例如 `ghcr.io/user/my-docker-images/alpine`，同时带有 `:<选中标签>` 和 `:latest`。如果上游重发同一标签，这两个目标标签会随之更新。模板或上下文改变、目标标签丢失或变化、手动选择 `force_rebuild` 时也会重建；无变化时跳过。每月首次运行会提交含实际检查结果的 `.github/last-check.json`，同月出现新的版本或错误时也会更新它。

## 检查与发布

在 **Actions → Sync upstream images → Run workflow** 中选 `check_only`，可只读检查所有订阅，包括禁用的示例；不会修改文件或发布。也可在本地安装 `PyYAML==6.0.3` 和 [`oras==0.2.43`](https://pypi.org/project/oras/) 后运行 `python .github/scripts/sync_images.py --check`。检查模式会查询 Docker Hub 和 GHCR 上游，但不检查目标 GHCR 标签。发布模式需要设置 `GITHUB_REPOSITORY=<所有者>/<仓库名>`，并准备好 Docker Buildx 和 GHCR 登录。测试命令为 `python .github/scripts/test_sync_images.py`。

发布工作流需要 `contents: write`、`packages: write` 权限，且 `master` 必须允许它推送生成文件。GHCR 首次创建的包默认是私有的；如需他人匿名拉取，请在包设置中改为 Public。GitHub 可能在公开仓库长期没有活动后停用定时工作流。每月检查记录可降低这个风险，但不能保证定时任务每天都触发；若更新停止，请查看 Actions 运行记录。
