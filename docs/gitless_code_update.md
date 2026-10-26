# 工控机无 Git 的代码更新与回退

本地用 Git 保存提交，工控机只接收带清单的 ZIP 更新包。更新前自动保存现场旧文件，回退恢复这些备份，并删除本次新增文件。**回退目标是更新前的现场状态，不是 main 分支。**

工具：`tools/code_update.py`。两端使用 Python 3.10 或更高版本，仅依赖标准库；只有本地打包需要 Git。Linux 更新和回退要求项目由 systemd 管理，服务的 `WorkingDirectory` 必须对应 `--root`。工具不会启动或停止硬件服务，也不会自动判断采集效果。

## 1. 管理范围与首次接入

- 管理 `devices/`、`workflow/`、`tools/`、`tests/`、`docs/`、`third_party/` 下变更的 `.py`、`.md` 文件。
- 配置、标定、数据、日志、模型、相机 SDK、虚拟环境、依赖清单、启动脚本及 systemd 配置不在包内。涉及这些内容的版本需要另行制定更新与回退步骤。
- 打包结果和 ZIP 内的 `manifest.json` 会列出 `excluded_changes`；非空时必须检查，不能认为目标提交已经完整部署。
- 校验的是包内涉及文件的基线，允许 LF/CRLF 差异；这不证明现场所有其他文件与某个提交完全一致。回退按原始字节恢复，保留旧文件的权限和 Linux 所有者。
- 首次接入，先把工控机现有代码和现场配置额外备份到独立目录。把以往在工控机直接修改过的代码带回本地核对、提交，再确定正确的打包基线。不要用 main 覆盖未知版本。
- 工具不能处理数据库迁移，也不会回退测试期间产生的数据或配置变化。

## 2. 本地制作更新包

先提交准备部署的代码。包从 Git 提交读取内容，不包含尚未提交的修改。

以上是 `build` 的提交版流程。需要先实机测试、暂不提交时，新版工具另支持显式选文件的工作区快照：

```powershell
& 'C:\miniforge3\envs\ca\python.exe' tools/code_update.py build-worktree --base 2267472 --path devices/motion/modbus.py --path devices/motion/communication_timing.py --output outputs/code-releases/example-worktree.zip
```

实际使用时，`--path` 必须列全这一功能依赖的所有修改文件；示例只展示语法。未选中的文件不会入包，`excluded_changes: []` 不表示整个工作区干净。快照包的 `target_commit` 为 `null`，用 `target_snapshot: worktree-sha256:...` 标识选定路径、基线和文件内容，不冒充已提交的 Git 版本。建议同时上传新版 `code_update.py`，使部署记录和 `status` 保留这个快照标识。基线检查、备份、更新与回退命令不变；更新包仍不能覆盖已存在的同名 ZIP。测试通过后，可另行提交代码，保留快照到提交的对应关系。

当前示例：从取证提交 `739d8ec` 更新到逐点停稳拍照提速提交 `2267472`，共 7 个文件，未排除其他变更。

Windows PowerShell：

```powershell
Set-Location 'C:\colony_system.worktrees\deploy-linux-20260902'
& 'C:\miniforge3\envs\ca\python.exe' tools/code_update.py build --base 739d8ec --target 2267472 --output outputs/code-releases/stop-shoot-739d8ec-to-2267472.zip
```

本次已生成此 ZIP。相同输出文件名不能重复打包覆盖；后续版本换一个文件名。

以后 `--base` 填工控机当前部署所对应的提交，`--target` 填准备部署的提交。回退后，下一包的基线也要回到回退后的版本。

保存每个版本的 ZIP、打包输出中的 SHA-256、Git 提交号及测试结果。本地分支可以继续开发，工控机版本由部署记录确认。

## 3. 传输与检查

通过 WinSCP、U 盘或 scp，把下面两个文件放到工控机的独立发布目录，例如 `/home/colony/releases/`：

1. `tools/code_update.py`（部署工具本身）。
2. `outputs/code-releases/stop-shoot-739d8ec-to-2267472.zip`。

工具放在项目目录外，避免回退项目代码时把部署工具一并删除。

以下路径是仓库 systemd 模板的示例，使用前核对实际配置：

```bash
systemctl cat colony-system-api
```

确认服务名、`WorkingDirectory` 和 Python 路径后，在 Linux Bash 中设置：

```bash
PROJECT=/opt/colony_system
SERVICE=colony-system-api
PYTHON="$PROJECT/.venv/bin/python"
UPDATER=/home/colony/releases/code_update.py
PACKAGE=/home/colony/releases/stop-shoot-739d8ec-to-2267472.zip

"$PYTHON" --version
sha256sum "$PACKAGE"
sudo "$PYTHON" "$UPDATER" check --root "$PROJECT" --package "$PACKAGE"
```

SHA-256 应与本地打包结果一致。只有出现 `"check": "passed"` 才进入更新步骤。

若出现 `site baseline differs`，表示某个待修改文件不是预期旧版本，或本应新增的文件已经存在。工具拒绝覆盖，并列出冲突文件。先把这些文件取回核对；不要强制覆盖、删除文件来绕过校验。如果现场已经应用过此提速版本，也不应再次应用同一个包。

### 已确认的现场例外：缺少耗时汇总脚本

本次工控机检查仅报告 `tools/summarize_stage_diagnostics.py` 不一致，用户进一步确认结果为 `MISSING`。因此另行生成：

`outputs/code-releases/stop-shoot-739d8ec-to-2267472-site-missing-summary.zip`

该包只把此脚本的更新前状态改为“不存在”，使其作为新增文件部署；其余 6 个文件的基线检查与全部目标文件内容保持不变。它仍部署相同的提速代码，但现场基线并非完整的 `739d8ec`，差异与原包 SHA-256 已记录在包内 `manifest.json` 的 `site_adaptation` 中。

此工控机应上传这个适配包，并把后续命令的 `PACKAGE` 改成该文件的实际路径。已经上传的 `code_update.py` 无需更换。必须先重新 `check`，成功后再停服 `apply`；回退会删除这个本次新增的汇总脚本，恢复到现场原先缺少它的状态。

适配包 SHA-256：`ee6544ee45a00d03f23d50724bfc6e2f994bd0b7bddca0fd417a28cf8c362eaa`。

已在本地临时目录验证：更新前缺少脚本时校验通过、7 文件部署成功、逐文件精确回退成功、汇总脚本回退后不存在、现场配置不变；脚本意外存在时仍拒绝更新。此验证不代表已在工控机部署。

## 4. 停服务、更新、启动测试

先等待观察、对焦等硬件任务结束，确认设备空闲，再停止服务。不能在载台运动中直接执行部署。

```bash
sudo systemctl stop "$SERVICE"
sudo "$PYTHON" "$UPDATER" apply --root "$PROJECT" --package "$PACKAGE" --service "$SERVICE"
```

**仅当 apply 成功并输出 `"status": "applied"` 后才启动：**

```bash
sudo systemctl start "$SERVICE"
systemctl status "$SERVICE" --no-pager
curl --fail http://127.0.0.1:8000/health
sudo journalctl -u "$SERVICE" -n 100 --no-pager
```

健康检查通过后执行一次已知的单孔观察任务，检查照片、载台位置和 result.json 的耗时与异常指标。健康接口成功只证明 API 可用，不能替代实际采集验收。测试通过后继续保留当前代码和备份，无需再覆盖或“保存一份”。

工具在 `$PROJECT/.code-updates/` 保存历史、清单和每次更新前的文件。新增文件记为“原先不存在”；删除文件也会先备份。不要手工清理此目录，否则相应回退能力会丢失。

## 5. 测试不通过时回退

等待任务结束并确认设备空闲，再执行：

```bash
sudo systemctl stop "$SERVICE"
sudo "$PYTHON" "$UPDATER" rollback --root "$PROJECT" --service "$SERVICE"
```

**仅当输出 `"status": "rolled_back"` 后才重新启动与验证：**

```bash
sudo systemctl start "$SERVICE"
curl --fail http://127.0.0.1:8000/health
```

每次 `rollback` 回退最近一次尚未撤销的更新，连续运行会继续回退更早的更新。不能跳过中间版本任意回退。

查看记录：

```bash
sudo "$PYTHON" "$UPDATER" status --root "$PROJECT"
```

如果更新后有人又手动修改了受管理文件，回退会拒绝覆盖，避免丢失现场修改；先将这些修改单独保存并核对。备份损坏时也会拒绝回退。

## 6. 异常与限制

- 写入失败时，工具尝试恢复本次已经修改的文件。恢复成功记录为 `apply_failed_restored`；失败记录为 `recovery_required`，此时保持服务停止，检查报错与备份，不要继续部署下一版。
- 回退中断后可以重新执行 `rollback`；工具接受该次操作已恢复的文件和尚待恢复的文件。
- 若进程被强制杀死或断电，可能遗留 `.code-updates/lock`。先确认服务已停止、没有更新进程在运行，保留备份，然后只移除空锁目录：`sudo rmdir "$PROJECT/.code-updates/lock"`，再查看状态并执行 `rollback`。不要递归删除 `.code-updates`。断电导致的文件系统或备份损坏需要人工恢复；逐文件替换不等于整目录原子切换。
- systemd 检查不能识别另一个手工启动的同项目进程。更新前要确保没有终端、其他服务或定时任务启动的第二个实例。如果现在仅用终端启动，先按项目部署说明接入 systemd，本工具没有跳过停服检查的参数。
- 更新期间不要同时手动修改代码，不要在两个账号中并行部署。
- 此方案适合目前的 Python 代码增量更新。将来涉及大量依赖、启动方式变化或数据库迁移时，应升级为独立版本目录与受控启动入口，并为环境和数据另设回退方案。

## 7. 本地离线验证

```powershell
& 'C:\miniforge3\envs\ca\python.exe' -m pytest tests/test_code_update.py -q
```

测试使用临时目录与模拟服务状态，覆盖新增/修改/删除文件、原始换行恢复、基线冲突、现场后续修改保护、损坏包与备份、写入失败自动恢复、回退中断重试、逐版本回退，以及服务停止与工作目录检查。它们没有控制真实载台、相机或 Linux 服务，现场仍需按上述流程验收。
