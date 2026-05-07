# 本地服务 launchd scaffold 约定

适用范围：
- 本仓库所有需要在 macOS 本地长期运行的服务
- 包括 HTTP 服务（如 8011、55801）和非 HTTP daemon（如 production_ops_daemon）

目标：
- 新增本地服务时，直接按同一模板落地
- 避免再出现 launchd 指向 restart/control 脚本、health 假通过、脚本风格漂移

---

## 1. 固定目录结构

新增一个本地服务时，最少应包含：

- `scripts/run_<service>.sh`
  - 纯长期运行入口
  - 只做环境加载、工作目录切换、最终 `exec`
  - 供 launchd `ProgramArguments` 使用

- `scripts/start_<service>.sh` 或 `scripts/restart_<service>.sh`
  - 运维控制入口
  - 负责 refresh plist、launchctl restart、必要的端口清理、最终校验
  - 不能作为 launchd 的长期运行入口

- `scripts/create_local_service_scaffold.py`
  - 从模板一键生成整套 service scaffold
  - 默认读取 `scripts/templates/local_service_scaffold/`

- `scripts/install_<service>_launch_agent.sh`
  - 安装/刷新 launch agent

- `scripts/status_<service>.sh`
  - 输出统一 JSON 状态

- `scripts/uninstall_<service>_launch_agent.sh`
  - 卸载 launch agent

- `scripts/launchd/com.chauncey.mcn.<service>.plist`
  - launchd 配置
  - `ProgramArguments` 必须指向 `run_<service>.sh`

如服务是 HTTP 服务，且会占固定端口：
- 复用 `scripts/lib/port_utils.sh`

所有 launchd 交互：
- 复用 `scripts/lib/launchd_service.sh`

---

## 2. 绝对禁止事项

1. 不要让 LaunchAgent 指向 `start_*.sh` / `restart_*.sh`
2. 不要只以 `launchctl bootstrap` 返回码判断成功或失败
3. 不要只看 `/health` 就算接管成功
4. 不要在每个脚本里重复手写 `launchctl print | grep state` 逻辑
5. 不要在多个脚本里复制粘贴端口 kill 逻辑

---

## 3. 成功判据

### 3.1 HTTP 服务
必须同时满足：
- `launchd_state(label) == running`
- `curl -sf <health_url>` 成功

示例：
- 8011 backend: `http://127.0.0.1:8011/health`
- 55801 bridge: `http://127.0.0.1:55801/healthz`

### 3.2 非 HTTP daemon
必须同时满足：
- `launchd_state(label) == running`
- `pgrep -f '<stable command pattern>'` 命中
- 如有状态文件，再把状态文件作为辅助证据输出

示例：
- production_ops_daemon
  - 进程判据：`pgrep -f 'production_ops_daemon.py'`
  - 辅助状态文件：`data/production_ops_daemon_status.json`

---

## 4. 统一 helper

### 4.1 `scripts/lib/port_utils.sh`
用途：
- 固定端口 HTTP 服务重启前清监听端口

标准能力：
- `terminate_listener <port>`
  - graceful kill
  - 轮询释放
  - 必要时 `kill -9`
  - 仍不释放则返回失败

### 4.2 `scripts/lib/launchd_service.sh`
用途：
- 统一 launchd 控制和等待校验

当前标准能力：
- `launchd_gui_domain`
- `launchd_state <label>`
- `launchd_last_exit_code <label>`
- `launchd_bootstrap_service <label> <plist_target>`
- `launchd_uninstall_service <label> <plist_target>`
- `wait_for_launchd_http_service <label> <url> [attempts] [sleep_seconds]`
- `wait_for_launchd_process_service <label> <pgrep_pattern> [attempts] [sleep_seconds]`

新增服务时，优先扩展这两个 helper，而不是在业务脚本里另起一套。

---

## 5. 新增 HTTP 服务的最小步骤

1. 创建 `scripts/run_<service>.sh`
2. 创建 launchd plist，`ProgramArguments -> run_<service>.sh`
3. 创建 `install/start/status/uninstall` 四个脚本
4. 在 `install/start` 中：
   - refresh plist
   - 如占固定端口，先 `terminate_listener <port>`
   - `launchd_bootstrap_service`
   - `wait_for_launchd_http_service`
5. 在 `status` 中输出：
   - pid
   - listening
   - health
   - launchd.label/state/last_exit_code
6. `bash -n` 检查所有脚本
7. 执行 install
8. 再执行 status + `launchctl print` + health 三重验证

---

## 6. 新增非 HTTP daemon 的最小步骤

1. 创建 `scripts/run_<service>.sh`
2. 创建 launchd plist，`ProgramArguments -> run_<service>.sh`
3. 创建 `install/start/status/uninstall` 四个脚本
4. 在 `install/start` 中：
   - refresh plist
   - `launchd_bootstrap_service`
   - `wait_for_launchd_process_service`
5. 在 `status` 中输出：
   - pid/running
   - 状态文件内容（若存在）
   - launchd.label/state/last_exit_code
6. `bash -n` 检查所有脚本
7. 执行 install
8. 再执行 status + `launchctl print` + `pgrep` 三重验证

---

## 7. 当前仓库里的标准实现

HTTP 服务：
- registration-group backend
  - run: `scripts/run_registration_group_backend.sh`
  - control: `scripts/restart_intake_backend_with_bind.sh`
  - install: `scripts/install_registration_group_backend_launch_agent.sh`
  - status: `scripts/status_registration_group_backend.sh`
  - uninstall: `scripts/uninstall_registration_group_backend_launch_agent.sh`
  - plist: `scripts/launchd/com.chauncey.mcn.registration-group-backend.plist`

- official-group bridge
  - run: `scripts/run_official_group_bridge.sh`
  - control: `scripts/start_official_group_bridge.sh`
  - install: `scripts/install_official_group_bridge_launch_agent.sh`
  - status: `scripts/status_official_group_bridge.sh`
  - uninstall: `scripts/uninstall_official_group_bridge_launch_agent.sh`
  - plist: `scripts/launchd/com.chauncey.mcn.official-group-bridge.plist`

非 HTTP daemon：
- production-ops-daemon
  - run: `scripts/run_production_ops_daemon.sh`
  - control: `scripts/start_production_ops_daemon.sh`
  - install: `scripts/install_production_ops_daemon_launch_agent.sh`
  - status: `scripts/status_production_ops_daemon.sh`
  - uninstall: `scripts/uninstall_production_ops_daemon_launch_agent.sh`
  - plist: `scripts/launchd/com.chauncey.mcn.production-ops-daemon.plist`

---

## 8. 模板来源

新服务不要从零写。
可选两种方式：

1. 直接运行生成器：
   - `python3 scripts/create_local_service_scaffold.py --service-slug sample-http --mode http --exec-command '/bin/echo sample' --port 9011 --health-url http://127.0.0.1:9011/health --path-export '/usr/local/bin:/usr/bin:/bin' --venv-path /tmp/sample-venv --home-dir /Users/demo --user-name demo`
   - `python3 scripts/create_local_service_scaffold.py --service-slug sample-daemon --mode process --exec-command '/usr/bin/python3 worker.py' --pgrep-pattern 'worker.py' --status-file data/sample-daemon-status.json --home-dir /Users/demo --user-name demo`
   - 预演但不写文件：在命令后加 `--dry-run`
   - 覆盖已存在 scaffold：在命令后加 `--force`
   - 输出单行 JSON：在命令后加 `--json`
   - 只打印生成文件绝对路径：在命令后加 `--print-paths-only`
   - 自定义 launchd label 前缀：加 `--label-prefix com.example.ops`，或兼容别名 `--company-prefix com.example.ops`
   - 自定义人类可读服务名前缀：加 `--service-name-prefix 'Acme '`
2. 手工从以下模板骨架复制：
- `scripts/templates/local_service_scaffold/run_http_service.sh.template`
- `scripts/templates/local_service_scaffold/restart_http_service.sh.template`
- `scripts/templates/local_service_scaffold/install_http_launch_agent.sh.template`
- `scripts/templates/local_service_scaffold/status_http_service.sh.template`
- `scripts/templates/local_service_scaffold/uninstall_launch_agent.sh.template`
- `scripts/templates/local_service_scaffold/run_process_service.sh.template`
- `scripts/templates/local_service_scaffold/restart_process_service.sh.template`
- `scripts/templates/local_service_scaffold/install_process_launch_agent.sh.template`
- `scripts/templates/local_service_scaffold/status_process_service.sh.template`
- `scripts/templates/local_service_scaffold/launchd.plist.template`

---

## 9. 最终原则

新增本地服务时，优先做结构统一，再做业务实现。

如果一个脚本里同时出现：
- 长期运行入口
- restart 控制逻辑
- launchctl 管理逻辑
- health/proc 校验逻辑

那就说明职责还没拆干净，需要继续按本 scaffold 收口。
