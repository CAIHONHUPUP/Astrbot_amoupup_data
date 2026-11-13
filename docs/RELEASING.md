# 发版说明（能记）

> 只在用户明确说「上传」时发版；本机代码就是主干。

## 一、命令

```bash
python work/refactor/upload.py data --dry   # 只打包 + 检查仓库
python work/refactor/upload.py data         # 真上传
```

版本号从 `metadata.yaml` 读；发版前跑 `python tools/check_version.py`。

## 二、Release 里放什么（2026-11-13 起）

| 附件 | 内容 | 用途 |
|---|---|---|
| `Astrbot_amoupup_data-<版本>.zip` | **只含运行时文件** | AstrBot 直接安装 |
| `Astrbot_amoupup_data-<版本>-source.zip` | **全量代码**（运行时 + 测试 + 工具 + CI） | 阅读/二次开发 |

安装包**排除**：`tests/`、`tools/`、`.github/`、`.coveragerc`、`mypy.ini`、
`requirements-dev.txt`、`run_tests.py`。两个包大小必须不同。

## 三、每个版本只留一个 Release

- 同一版本号只允许一个 Release；
- 同版本重发后，删掉上一个同版本 tag + Release；不同版本的历史保留；
- `DELETE /repos/CAIHONHUPUP/Astrbot_amoupup_data/releases/<id>` + `DELETE /git/refs/tags/<tag>`。

## 四、CI 会检查什么

| 步骤 | 内容 |
|---|---|
| Version | 四处版本一致 |
| Type check | `tools/mypy_check.py`（严格模式 `--check-untyped-defs`，无缓存） |
| Quality gate | schema（29 项）、编译、路由数（34）、面板 JS、pyflakes |
| Run tests | 319 项 + 覆盖率底线 88% |

`mypy` 钉在 `2.4.1`。本地跑法：

```bash
python tools/mypy_check.py
python tools/quality_gate.py --fast
python run_tests.py
python -m coverage run -m pytest tests -q && python -m coverage report
```

## 五、发完核对

1. main 指向新 commit；
2. Release 两个附件、大小不同；
3. CI success；
4. 线上重载后 `我能永远记住你 <版本> 已加载`，面板 `/status` 正常；
5. 每个版本只有一个 Release。
