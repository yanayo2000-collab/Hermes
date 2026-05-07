from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict


def _default_service_name(service_slug: str) -> str:
    return service_slug.replace('-', ' ')


def _service_key(service_slug: str) -> str:
    return service_slug.replace('-', '_')


def _load_template(template_root: Path, template_name: str) -> str:
    template_path = template_root / 'scripts' / 'templates' / 'local_service_scaffold' / template_name
    return template_path.read_text()


def _render_template(template: str, replacements: Dict[str, str]) -> str:
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(f'{{{{{key}}}}}', value)
    return rendered


def _write_script(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _ensure_helper(root_dir: Path, template_root: Path, relative_path: str, *, dry_run: bool = False) -> None:
    source = template_root / relative_path
    target = root_dir / relative_path
    if target.exists() or not source.exists() or dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text())
    target.chmod(0o755)


def _collect_existing_paths(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.exists()]


def _persist_files(files: dict[Path, str], *, force: bool, dry_run: bool) -> None:
    existing_paths = _collect_existing_paths(list(files))
    if existing_paths and not force:
        joined = ', '.join(str(path) for path in existing_paths)
        raise FileExistsError(f'refusing to overwrite existing scaffold files: {joined}')
    if dry_run:
        return
    for path, content in files.items():
        if path.suffix == '.plist':
            _write_text(path, content)
        else:
            _write_script(path, content)


def _base_replacements(
    *,
    root_dir: Path,
    service_slug: str,
    label: str,
    service_name: str,
    home_dir: str,
    user_name: str,
    launchd_path: str,
) -> Dict[str, str]:
    return {
        'LABEL': label,
        'SERVICE_SLUG': service_slug,
        'SERVICE_NAME': service_name,
        'SERVICE_KEY': _service_key(service_slug),
        'WORKDIR': str(root_dir),
        'HOME_DIR': home_dir,
        'USER_NAME': user_name,
        'LAUNCHD_PATH': launchd_path,
        'LOG_BASENAME': service_slug.replace('-', '_'),
    }


def generate_scaffold(
    *,
    root_dir: Path,
    service_slug: str,
    mode: str,
    exec_command: str,
    home_dir: str,
    user_name: str,
    launchd_path: str,
    label: str | None = None,
    label_prefix: str = 'com.chauncey.mcn',
    service_name: str | None = None,
    service_name_prefix: str = '',
    path_export: str | None = None,
    venv_path: str | None = None,
    extra_env_exports: str = '',
    port: int | None = None,
    health_url: str | None = None,
    pgrep_pattern: str | None = None,
    status_file: str | None = None,
    env_load_snippet: str = '',
    template_root: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    root_dir = root_dir.resolve()
    template_root = (template_root or root_dir).resolve()
    label = label or f'{label_prefix}.{service_slug}'
    service_name = service_name or f'{service_name_prefix}{_default_service_name(service_slug)}'
    base = _base_replacements(
        root_dir=root_dir,
        service_slug=service_slug,
        label=label,
        service_name=service_name,
        home_dir=home_dir,
        user_name=user_name,
        launchd_path=launchd_path,
    )

    generated_files: dict[str, str] = {}
    scripts_dir = root_dir / 'scripts'
    launchd_dir = scripts_dir / 'launchd'

    if mode == 'http':
        _ensure_helper(root_dir, template_root, 'scripts/lib/launchd_service.sh', dry_run=dry_run)
        _ensure_helper(root_dir, template_root, 'scripts/lib/port_utils.sh', dry_run=dry_run)
        if port is None or not health_url:
            raise ValueError('http mode requires port and health_url')
        if not path_export or not venv_path:
            raise ValueError('http mode requires path_export and venv_path')
        replacements = {
            **base,
            'PATH_EXPORT': path_export,
            'VENV_PATH': venv_path,
            'EXTRA_ENV_EXPORTS': extra_env_exports or '# no extra env exports',
            'EXEC_COMMAND': exec_command,
            'PORT': str(port),
            'HEALTH_URL': health_url,
            'OPTIONAL_STOP_SNIPPET': (
                f"if lsof -tiTCP:{port} -sTCP:LISTEN >/dev/null 2>&1; then\n"
                f"  kill $(lsof -tiTCP:{port} -sTCP:LISTEN) || true\n"
                'fi'
            ),
        }
        files = {
            scripts_dir / f'run_{service_slug}.sh': _render_template(_load_template(template_root, 'run_http_service.sh.template'), replacements),
            scripts_dir / f'start_{service_slug}.sh': _render_template(_load_template(template_root, 'restart_http_service.sh.template'), replacements),
            scripts_dir / f'install_{service_slug}_launch_agent.sh': _render_template(_load_template(template_root, 'install_http_launch_agent.sh.template'), replacements),
            scripts_dir / f'status_{service_slug}.sh': _render_template(_load_template(template_root, 'status_http_service.sh.template'), replacements),
            scripts_dir / f'uninstall_{service_slug}_launch_agent.sh': _render_template(_load_template(template_root, 'uninstall_launch_agent.sh.template'), replacements),
            launchd_dir / f'{label}.plist': _render_template(_load_template(template_root, 'launchd.plist.template'), replacements),
        }
    elif mode == 'process':
        _ensure_helper(root_dir, template_root, 'scripts/lib/launchd_service.sh', dry_run=dry_run)
        if not pgrep_pattern or not status_file:
            raise ValueError('process mode requires pgrep_pattern and status_file')
        replacements = {
            **base,
            'ENV_LOAD_SNIPPET': env_load_snippet or '# no env load snippet',
            'EXTRA_ENV_EXPORTS': extra_env_exports or '# no extra env exports',
            'EXEC_COMMAND': exec_command,
            'PGREP_PATTERN': pgrep_pattern,
            'STATUS_FILE': status_file,
            'OPTIONAL_STOP_SNIPPET': (
                f"if pgrep -f '{pgrep_pattern}' >/dev/null 2>&1; then\n"
                f"  pkill -f '{pgrep_pattern}' || true\n"
                'fi'
            ),
        }
        files = {
            scripts_dir / f'run_{service_slug}.sh': _render_template(_load_template(template_root, 'run_process_service.sh.template'), replacements),
            scripts_dir / f'start_{service_slug}.sh': _render_template(_load_template(template_root, 'restart_process_service.sh.template'), replacements),
            scripts_dir / f'install_{service_slug}_launch_agent.sh': _render_template(_load_template(template_root, 'install_process_launch_agent.sh.template'), replacements),
            scripts_dir / f'status_{service_slug}.sh': _render_template(_load_template(template_root, 'status_process_service.sh.template'), replacements),
            scripts_dir / f'uninstall_{service_slug}_launch_agent.sh': _render_template(_load_template(template_root, 'uninstall_launch_agent.sh.template'), replacements),
            launchd_dir / f'{label}.plist': _render_template(_load_template(template_root, 'launchd.plist.template'), replacements),
        }
    else:
        raise ValueError(f'unsupported mode: {mode}')

    _persist_files(files, force=force, dry_run=dry_run)

    for path in files:
        generated_files[str(path.relative_to(root_dir))] = str(path)

    return {
        'service_slug': service_slug,
        'mode': mode,
        'label': label,
        'files': generated_files,
        'dry_run': dry_run,
        'force': force,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Generate local launchd service scaffold files from repo templates.')
    parser.add_argument('--service-slug', required=True)
    parser.add_argument('--mode', choices=['http', 'process'], required=True)
    parser.add_argument('--exec-command', required=True)
    parser.add_argument('--root-dir', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--template-root')
    parser.add_argument('--label')
    parser.add_argument('--label-prefix', default='com.chauncey.mcn')
    parser.add_argument('--company-prefix')
    parser.add_argument('--service-name')
    parser.add_argument('--service-name-prefix', default='')
    parser.add_argument('--home-dir', default=os.environ.get('HOME', ''))
    parser.add_argument('--user-name', default=os.environ.get('USER', ''))
    parser.add_argument('--launchd-path', default='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin')
    parser.add_argument('--path-export')
    parser.add_argument('--venv-path')
    parser.add_argument('--extra-env-exports', default='')
    parser.add_argument('--port', type=int)
    parser.add_argument('--health-url')
    parser.add_argument('--pgrep-pattern')
    parser.add_argument('--status-file')
    parser.add_argument('--env-load-snippet', default='')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--print-paths-only', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root_dir = Path(args.root_dir)
    default_template_root = Path(__file__).resolve().parents[1]
    candidate_template_root = Path(args.template_root) if args.template_root else root_dir
    template_root = candidate_template_root
    template_dir = template_root / 'scripts' / 'templates' / 'local_service_scaffold'
    if not template_dir.exists():
        template_root = default_template_root
    plan = generate_scaffold(
        root_dir=root_dir,
        service_slug=args.service_slug,
        mode=args.mode,
        exec_command=args.exec_command,
        home_dir=args.home_dir,
        user_name=args.user_name,
        launchd_path=args.launchd_path,
        label=args.label,
        label_prefix=args.company_prefix or args.label_prefix,
        service_name=args.service_name,
        service_name_prefix=args.service_name_prefix,
        path_export=args.path_export,
        venv_path=args.venv_path,
        extra_env_exports=args.extra_env_exports,
        port=args.port,
        health_url=args.health_url,
        pgrep_pattern=args.pgrep_pattern,
        status_file=args.status_file,
        env_load_snippet=args.env_load_snippet,
        template_root=template_root,
        dry_run=args.dry_run,
        force=args.force,
    )
    if args.print_paths_only:
        for path in plan['files'].values():
            print(path)
        return 0
    if args.json:
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
