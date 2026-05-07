import json
import subprocess
from pathlib import Path


def test_generate_http_scaffold_creates_expected_files(tmp_path):
    from scripts.create_local_service_scaffold import generate_scaffold

    root = tmp_path / 'repo'
    scripts_dir = root / 'scripts'
    templates_dir = scripts_dir / 'templates' / 'local_service_scaffold'
    launchd_dir = scripts_dir / 'launchd'
    lib_dir = scripts_dir / 'lib'
    docs_dir = root / 'docs'
    templates_dir.mkdir(parents=True)
    launchd_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    plan = generate_scaffold(
        root_dir=root,
        service_slug='demo-http-service',
        mode='http',
        port=9123,
        health_url='http://127.0.0.1:9123/healthz',
        exec_command='/usr/bin/python3 -m http.server 9123',
        path_export='/usr/local/bin:/usr/bin:/bin',
        launchd_path='/usr/local/bin:/usr/bin:/bin',
        venv_path='/tmp/demo-venv',
        home_dir='/Users/demo',
        user_name='demo',
    )

    assert plan['label'] == 'com.chauncey.mcn.demo-http-service'
    assert (scripts_dir / 'run_demo-http-service.sh').exists()
    assert (scripts_dir / 'start_demo-http-service.sh').exists()
    assert (scripts_dir / 'install_demo-http-service_launch_agent.sh').exists()
    assert (scripts_dir / 'status_demo-http-service.sh').exists()
    assert (scripts_dir / 'uninstall_demo-http-service_launch_agent.sh').exists()
    assert (launchd_dir / 'com.chauncey.mcn.demo-http-service.plist').exists()

    run_text = (scripts_dir / 'run_demo-http-service.sh').read_text()
    assert 'exec /usr/bin/python3 -m http.server 9123' in run_text
    assert 'export VIRTUAL_ENV=/tmp/demo-venv' in run_text

    restart_text = (scripts_dir / 'start_demo-http-service.sh').read_text()
    assert 'terminate_listener 9123' in restart_text
    assert 'launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"' in restart_text
    assert 'chmod 644 "$PLIST_TARGET"' in restart_text
    assert 'chmod +x ' in restart_text
    assert "wait_for_launchd_http_service \"$LABEL\" 'http://127.0.0.1:9123/healthz'" in restart_text

    install_text = (scripts_dir / 'install_demo-http-service_launch_agent.sh').read_text()
    assert 'launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"' in install_text
    assert 'chmod 644 "$PLIST_TARGET"' in install_text
    assert 'chmod +x "$ROOT_DIR/scripts/run_demo-http-service.sh" "$ROOT_DIR/scripts/start_demo-http-service.sh"' in install_text

    plist_text = (launchd_dir / 'com.chauncey.mcn.demo-http-service.plist').read_text()
    assert '/scripts/run_demo-http-service.sh' in plist_text


def test_generate_process_scaffold_creates_expected_files(tmp_path):
    from scripts.create_local_service_scaffold import generate_scaffold

    root = tmp_path / 'repo'
    scripts_dir = root / 'scripts'
    templates_dir = scripts_dir / 'templates' / 'local_service_scaffold'
    launchd_dir = scripts_dir / 'launchd'
    templates_dir.mkdir(parents=True)
    launchd_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    plan = generate_scaffold(
        root_dir=root,
        service_slug='demo-worker',
        mode='process',
        exec_command='/usr/bin/python3 worker.py',
        pgrep_pattern='worker.py',
        status_file='data/demo-worker-status.json',
        home_dir='/Users/demo',
        user_name='demo',
        launchd_path='/usr/local/bin:/usr/bin:/bin',
        env_load_snippet='export DEMO=1',
    )

    assert plan['label'] == 'com.chauncey.mcn.demo-worker'
    assert (scripts_dir / 'run_demo-worker.sh').exists()
    assert (scripts_dir / 'start_demo-worker.sh').exists()
    assert (scripts_dir / 'install_demo-worker_launch_agent.sh').exists()
    assert (scripts_dir / 'status_demo-worker.sh').exists()
    assert (scripts_dir / 'uninstall_demo-worker_launch_agent.sh').exists()
    assert (launchd_dir / 'com.chauncey.mcn.demo-worker.plist').exists()

    run_text = (scripts_dir / 'run_demo-worker.sh').read_text()
    assert 'export DEMO=1' in run_text
    assert 'exec /usr/bin/python3 worker.py' in run_text

    install_text = (scripts_dir / 'install_demo-worker_launch_agent.sh').read_text()
    assert "wait_for_launchd_process_service \"$LABEL\" 'worker.py'" in install_text
    assert 'chmod 644 "$PLIST_TARGET"' in install_text
    assert 'chmod +x "$ROOT_DIR/scripts/run_demo-worker.sh" "$ROOT_DIR/scripts/start_demo-worker.sh"' in install_text

    restart_text = (scripts_dir / 'start_demo-worker.sh').read_text()
    assert 'launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"' in restart_text
    assert 'chmod 644 "$PLIST_TARGET"' in restart_text
    assert 'chmod +x ' in restart_text

    status_text = (scripts_dir / 'status_demo-worker.sh').read_text()
    assert "status_json=\"$(cat 'data/demo-worker-status.json' 2>/dev/null || true)\"" in status_text


def test_generate_scaffold_dry_run_reports_files_without_writing(tmp_path):
    from scripts.create_local_service_scaffold import generate_scaffold

    root = tmp_path / 'repo'
    scripts_dir = root / 'scripts'
    templates_dir = scripts_dir / 'templates' / 'local_service_scaffold'
    templates_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    plan = generate_scaffold(
        root_dir=root,
        template_root=root,
        service_slug='dry-http',
        mode='http',
        port=9555,
        health_url='http://127.0.0.1:9555/health',
        exec_command='/bin/echo dry-http',
        path_export='/usr/local/bin:/usr/bin:/bin',
        launchd_path='/usr/local/bin:/usr/bin:/bin',
        venv_path='/tmp/dry-venv',
        home_dir='/Users/demo',
        user_name='demo',
        dry_run=True,
    )

    assert plan['dry_run'] is True
    assert 'scripts/run_dry-http.sh' in plan['files']
    assert not (scripts_dir / 'run_dry-http.sh').exists()
    assert not (scripts_dir / 'lib' / 'launchd_service.sh').exists()


def test_generate_scaffold_refuses_to_overwrite_without_force(tmp_path):
    from scripts.create_local_service_scaffold import generate_scaffold

    root = tmp_path / 'repo'
    scripts_dir = root / 'scripts'
    templates_dir = scripts_dir / 'templates' / 'local_service_scaffold'
    templates_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    existing = scripts_dir / 'run_demo-http.sh'
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('original-content\n')

    try:
        generate_scaffold(
            root_dir=root,
            template_root=root,
            service_slug='demo-http',
            mode='http',
            port=9444,
            health_url='http://127.0.0.1:9444/health',
            exec_command='/bin/echo demo-http',
            path_export='/usr/local/bin:/usr/bin:/bin',
            launchd_path='/usr/local/bin:/usr/bin:/bin',
            venv_path='/tmp/demo-http-venv',
            home_dir='/Users/demo',
            user_name='demo',
        )
    except FileExistsError as exc:
        assert 'run_demo-http.sh' in str(exc)
    else:
        raise AssertionError('expected generate_scaffold to refuse overwriting existing files without force')

    assert existing.read_text() == 'original-content\n'

    plan = generate_scaffold(
        root_dir=root,
        template_root=root,
        service_slug='demo-http',
        mode='http',
        port=9444,
        health_url='http://127.0.0.1:9444/health',
        exec_command='/bin/echo demo-http',
        path_export='/usr/local/bin:/usr/bin:/bin',
        launchd_path='/usr/local/bin:/usr/bin:/bin',
        venv_path='/tmp/demo-http-venv',
        home_dir='/Users/demo',
        user_name='demo',
        force=True,
    )

    assert plan['force'] is True
    assert existing.read_text() != 'original-content\n'


def test_cli_print_paths_only_outputs_generated_paths(tmp_path):
    root = tmp_path / 'repo'
    templates_dir = root / 'scripts' / 'templates' / 'local_service_scaffold'
    templates_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    result = subprocess.run(
        [
            'python3',
            'scripts/create_local_service_scaffold.py',
            '--service-slug',
            'cli-http',
            '--mode',
            'http',
            '--exec-command',
            '/bin/echo cli-http',
            '--port',
            '9333',
            '--health-url',
            'http://127.0.0.1:9333/health',
            '--path-export',
            '/usr/local/bin:/usr/bin:/bin',
            '--venv-path',
            '/tmp/cli-http-venv',
            '--home-dir',
            '/Users/demo',
            '--user-name',
            'demo',
            '--root-dir',
            str(root),
            '--template-root',
            str(root),
            '--print-paths-only',
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
    )

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines
    assert all(line.startswith(str(root)) for line in lines)
    assert any(line.endswith('scripts/run_cli-http.sh') for line in lines)
    assert not result.stderr.strip()


def test_cli_json_output_is_single_line_json(tmp_path):
    root = tmp_path / 'repo'
    templates_dir = root / 'scripts' / 'templates' / 'local_service_scaffold'
    templates_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    result = subprocess.run(
        [
            'python3',
            'scripts/create_local_service_scaffold.py',
            '--service-slug',
            'json-http',
            '--mode',
            'http',
            '--exec-command',
            '/bin/echo json-http',
            '--port',
            '9445',
            '--health-url',
            'http://127.0.0.1:9445/health',
            '--path-export',
            '/usr/local/bin:/usr/bin:/bin',
            '--venv-path',
            '/tmp/json-http-venv',
            '--home-dir',
            '/Users/demo',
            '--user-name',
            'demo',
            '--root-dir',
            str(root),
            '--template-root',
            str(root),
            '--json',
            '--dry-run',
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload['service_slug'] == 'json-http'
    assert payload['dry_run'] is True
    assert payload['force'] is False
    assert 'scripts/run_json-http.sh' in payload['files']
    assert not result.stderr.strip()


def test_generate_scaffold_accepts_custom_label_prefix_and_service_name_prefix(tmp_path):
    from scripts.create_local_service_scaffold import generate_scaffold

    root = tmp_path / 'repo'
    templates_dir = root / 'scripts' / 'templates' / 'local_service_scaffold'
    templates_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    plan = generate_scaffold(
        root_dir=root,
        template_root=root,
        service_slug='permata-gateway',
        mode='http',
        port=9666,
        health_url='http://127.0.0.1:9666/health',
        exec_command='/bin/echo permata-gateway',
        path_export='/usr/local/bin:/usr/bin:/bin',
        launchd_path='/usr/local/bin:/usr/bin:/bin',
        venv_path='/tmp/permata-venv',
        home_dir='/Users/demo',
        user_name='demo',
        label_prefix='com.acme.ops',
        service_name_prefix='Acme ',
    )

    assert plan['label'] == 'com.acme.ops.permata-gateway'
    plist_text = (root / 'scripts' / 'launchd' / 'com.acme.ops.permata-gateway.plist').read_text()
    assert 'com.acme.ops.permata-gateway' in plist_text
    restart_text = (root / 'scripts' / 'start_permata-gateway.sh').read_text()
    assert 'Acme permata gateway launch agent restart timed out' in restart_text


def test_cli_accepts_company_prefix_alias_for_label_prefix(tmp_path):
    root = tmp_path / 'repo'
    templates_dir = root / 'scripts' / 'templates' / 'local_service_scaffold'
    templates_dir.mkdir(parents=True)

    source_templates = Path('scripts/templates/local_service_scaffold')
    for template in source_templates.glob('*.template'):
        (templates_dir / template.name).write_text(template.read_text())

    result = subprocess.run(
        [
            'python3',
            'scripts/create_local_service_scaffold.py',
            '--service-slug',
            'custom-http',
            '--mode',
            'http',
            '--exec-command',
            '/bin/echo custom-http',
            '--port',
            '9777',
            '--health-url',
            'http://127.0.0.1:9777/health',
            '--path-export',
            '/usr/local/bin:/usr/bin:/bin',
            '--venv-path',
            '/tmp/custom-http-venv',
            '--home-dir',
            '/Users/demo',
            '--user-name',
            'demo',
            '--root-dir',
            str(root),
            '--template-root',
            str(root),
            '--company-prefix',
            'com.acme.bot',
            '--json',
            '--dry-run',
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload['label'] == 'com.acme.bot.custom-http'
