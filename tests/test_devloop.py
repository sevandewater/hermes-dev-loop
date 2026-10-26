"""CLI behavior tests with a credential-free Docker double and real settings files."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import devloop


class DockerDouble:
    def __init__(self):
        self.calls = []
        self.logged_in = False
        self.running = True
        self.health = False
        self.models = ["nous/model-a", "nous/model-b"]
        self.model_error = None
        self.fail_login = False
        self.checkout_exists = False
        self.broker_running = False
        self.live_workflows = ''
        self.networks = []
        self.routes = [{"dst": "default", "dev": "eth0"}]
        self.config = {
            "services": {name: {} for name in devloop.SERVICES},
            "networks": {"development": {"internal": True, "ipam": {"config": [{"subnet": "10.237.80.0/24"}]}}},
        }
        self.config["services"]["hermes"]["environment"] = {"OPENAI_BASE_URL": "http://llm-gateway-api:8645/v1"}
        self.config['services']['llm-gateway']['networks'] = {'inference': {'ipv4_address': '10.237.81.2'}}

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ["docker", "version"]:
            return json.dumps({"Version": "29.8.1", "Os": "linux"})
        if args[:2] == ["docker", "info"]:
            return '["name=seccomp,profile=builtin"]'
        if args[:3] == ["docker", "network", "ls"]:
            return "network-id" if self.networks else ""
        if args[:3] == ["docker", "network", "inspect"]:
            return json.dumps(self.networks)
        if args[:3] == ["ip", "-j", "route"]:
            return json.dumps(self.routes)
        if args[0] == sys.executable:
            if args[1].endswith("init-runtime.py"):
                subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
            return ""
        if args[:2] != ["docker", "compose"]:
            raise AssertionError(args)
        command = args[8:]
        if command[:2] == ['--profile', 'github']:
            command = command[2:]
        if command[:1] == ["config"]:
            return json.dumps(self.config)
        if command[:1] == ["version"]:
            return "5.5.1"
        if command[:1] == ["ps"]:
            names = devloop.SERVICES + (('github-broker',) if self.broker_running else ())
            return "\n".join(json.dumps({"Service": name, "State": "running" if self.running else "exited", "Health": "healthy" if name == "squid" else ""}) for name in names)
        if command[:1] == ["up"]:
            self.running = True
            if command[-1] == 'github-broker':
                self.broker_running = True
                self.live_workflows = kwargs['env']['BROKER_WORKFLOW_PUBLICATION']
        if command[:1] == ["stop"]:
            self.running = False
        if command[:1] == ["restart"]:
            self.health = self.logged_in
        if "--no-browser" in command:
            if self.fail_login:
                raise devloop.Error("Login cancelled")
            self.logged_in = True
        if "logout" in command:
            self.logged_in = False
        if "-c" in command:
            if 'BROKER_WORKFLOW_PUBLICATION' in command[-1]:
                return 'enabled' if self.live_workflows == 'verified' else 'disabled'
            if "p.is_symlink()" in command[-1]:
                return "yes" if self.checkout_exists else "no"
            if "print('yes'" in command[-1]:
                return "yes" if self.logged_in else "no"
            return ""
        if kwargs.get("data") == devloop.PROBE:
            kind = command[-1]
            if kind == "health":
                return json.dumps({"ok": self.health, "authenticated": self.logged_in and self.health})
            if self.model_error is not None:
                return json.dumps(self.model_error)
            return json.dumps({"ok": self.logged_in, "models": self.models})
        return ""

    def logs(self, args):
        self.calls.append((args, {}))


class DevLoopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "scripts").mkdir()
        shutil.copy(devloop.ROOT / "scripts/init-runtime.py", self.root / "scripts/init-runtime.py")
        (self.root / ".env.example").write_text("HERMES_CPUS=2\n")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "scripts/init-runtime.py", ".env.example"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=DevLoop Test", "-c", "user.email=devloop@example.invalid",
                        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Fixture source"], check=True)
        self.runner = DockerDouble()
        self.app = devloop.DevLoop(self.root, self.runner)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.ip = patch("scripts.devloop.shutil.which", return_value="/usr/sbin/ip")
        self.ip.start()

    def tearDown(self):
        self.ip.stop()
        self.redirect.__exit__(None, None, None)
        self.directory.cleanup()

    def prepare(self):
        self.app.prepare()

    def ready(self):
        self.prepare()
        self.runner.logged_in = True
        self.runner.health = True

    def commands(self):
        return [args[8:] for args, _ in self.runner.calls if args[:2] == ["docker", "compose"]]

    def test_setup_is_repeatable_and_preserves_existing_state(self):
        (self.root / ".env").write_text("HERMES_CPUS=3\n")
        self.app.setup()
        workspace = self.root / "runtime/hermes/workspace/project.txt"
        workspace.write_text("keep my project")
        gateway = self.root / "runtime/gateway/sentinel"
        gateway.write_text("not-a-real-token")
        self.app.save_model("nous/model-a")
        self.app.setup()
        self.assertEqual((self.root / ".env").read_text(), "HERMES_CPUS=3\n")
        self.assertEqual(workspace.read_text(), "keep my project")
        self.assertEqual(gateway.read_text(), "not-a-real-token")
        self.assertEqual(self.app.selected_model(), "nous/model-a")
        self.assertFalse(any("--force-recreate" in args for args in self.commands()))

    def test_fresh_env_is_private(self):
        self.prepare()
        self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)

    def test_project_refresh_exports_only_committed_source(self):
        self.prepare()
        (self.root / ".env").write_text("private-local-value")
        (self.root / "scripts/init-runtime.py").write_text("uncommitted-local-change")
        self.app.project(refresh=True)
        reference = self.root / "runtime/reference/hermes-dev-loop"
        self.assertFalse((reference / ".env").exists())
        self.assertFalse((reference / ".git").exists())
        self.assertFalse((reference / "runtime").exists())
        self.assertNotIn("uncommitted-local-change", (reference / "scripts/init-runtime.py").read_text())
        self.assertIn("isolated App broker", self.output.getvalue())
        self.assertFalse(any("up" in args for args in self.commands()))

    def test_project_checkout_runs_only_inside_hermes(self):
        self.prepare()
        self.app.project(checkout=True)
        calls = [(args, options) for args, options in self.runner.calls if options.get("data") == devloop.PROJECT_CHECKOUT]
        self.assertEqual(len(calls), 1)
        self.assertIn("hermes", calls[0][0])
        self.assertIn("-I", calls[0][0])
        self.assertFalse(any(args[0] == "git" for args, _ in self.runner.calls))

    def test_project_checkout_refuses_existing_work_without_fetching_or_resetting(self):
        self.prepare()
        self.runner.checkout_exists = True
        with self.assertRaisesRegex(devloop.Error, "existing work was preserved"):
            self.app.project(checkout=True)
        self.assertFalse(any(options.get("data") == devloop.PROJECT_CHECKOUT for _, options in self.runner.calls))

    def test_project_menu_offers_reference_refresh(self):
        self.prepare()
        with patch("scripts.devloop.terminal", return_value=True), patch("builtins.input", return_value="2"):
            self.app.project()
        self.assertIn("Prepare a new writable checkout", self.output.getvalue())

    def test_workflow_status_defaults_disabled_and_reports_broker_unavailable(self):
        cli = devloop.parser()
        args = cli.parse_args(['github', '--workflows', 'status'])
        devloop.dispatch(self.app, args, cli)
        self.assertIn('Workflow publication configured: disabled', self.output.getvalue())
        self.assertIn('Workflow publication live: unavailable', self.output.getvalue())
        self.assertFalse(self.app.settings.exists())
        self.assertEqual(self.commands(), [['ps', '--all', '--format', 'json']])

    def test_workflow_enable_persists_applies_only_broker_and_verifies_live_state(self):
        self.app.save_model('nous/model-a')
        settings = json.loads(self.app.settings.read_text())
        settings['custom'] = 'preserve'
        self.app.settings.write_text(json.dumps(settings))
        self.runner.broker_running = True
        self.runner.live_workflows = ''
        with patch.dict(os.environ, {'BROKER_WORKFLOW_PUBLICATION': 'untrusted'}):
            self.app.github(workflows='enable')
        saved = json.loads(self.app.settings.read_text())
        self.assertIs(saved['workflow_publication'], True)
        self.assertEqual(saved['custom'], 'preserve')
        self.assertEqual(saved['model'], 'nous/model-a')
        self.assertEqual(self.app.settings.stat().st_mode & 0o777, 0o600)
        up = [(args, options) for args, options in self.runner.calls if '--force-recreate' in args]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0][0][8:], ['--profile', 'github', 'up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '60', 'github-broker'])
        self.assertEqual(up[0][1]['env']['BROKER_WORKFLOW_PUBLICATION'], 'verified')
        self.assertIn('Workflow publication live: enabled', self.output.getvalue())
        self.assertFalse(any('restart' in command or 'build' in command for command in self.commands()))

    def test_github_interactive_workflow_submenu_dispatches_control(self):
        with patch('scripts.devloop.terminal', return_value=True), \
             patch('builtins.input', side_effect=['6', '3']):
            self.app.github()
        self.assertIn('Workflow publication', self.output.getvalue())
        self.assertIn('Workflow publication configured: disabled', self.output.getvalue())

    def test_workflow_change_reports_the_verified_read_without_second_probe(self):
        self.runner.broker_running = True
        with patch.object(self.app, 'live_workflow_publication', side_effect=['enabled', 'unavailable']) as probe:
            self.app.github(workflows='enable')
        self.assertEqual(probe.call_count, 1)
        self.assertIn('Workflow publication live: enabled', self.output.getvalue())

    def test_workflow_disable_persists_across_lifecycle_and_model_changes(self):
        self.prepare()
        self.runner.broker_running = True
        self.app.github(workflows='enable')
        self.app.save_model('nous/model-a')
        (self.app.control / 'github-enabled').write_text('enabled\n')
        fresh = devloop.DevLoop(self.root, self.runner)
        fresh.start()
        fresh.rebuild()
        self.assertEqual(self.runner.live_workflows, 'verified')
        fresh.github(workflows='disable')
        fresh.save_model('nous/model-b')
        fresh.start()
        fresh.rebuild()
        self.assertIs(json.loads(fresh.settings.read_text())['workflow_publication'], False)
        self.assertEqual(self.runner.live_workflows, '')
        self.assertIn('Workflow publication live: disabled', self.output.getvalue())

    def test_workflow_status_shows_configured_live_mismatch_without_writes(self):
        self.app.private_control()
        self.app.settings.write_text('{"workflow_publication": true}')
        self.runner.broker_running = True
        before = self.app.settings.read_bytes()
        self.app.github(workflows='status')
        self.assertIn('configured: enabled', self.output.getvalue())
        self.assertIn('live: disabled', self.output.getvalue())
        self.assertEqual(self.app.settings.read_bytes(), before)
        self.assertTrue(all(command[0] in ('ps', 'exec') for command in self.commands()))

    def test_workflow_apply_failure_retains_desired_setting_without_claiming_success(self):
        for failure in ('stopped', 'recreate', 'mismatch', 'unavailable'):
            with self.subTest(failure=failure):
                self.runner.broker_running = failure != 'stopped'
                run = self.runner.run
                def broken(args, **options):
                    if failure == 'recreate' and '--force-recreate' in args:
                        raise devloop.Error('Docker failed')
                    return run(args, **options)
                with patch.object(self.runner, 'run', side_effect=broken), \
                     patch.object(self.app, 'live_workflow_publication', return_value='disabled' if failure == 'mismatch' else 'unavailable'):
                    with self.assertRaisesRegex(devloop.Error, 'saved, but live application was not verified'):
                        self.app.github(workflows='enable')
                self.assertIs(json.loads(self.app.settings.read_text())['workflow_publication'], True)
        self.assertNotIn('live: enabled', self.output.getvalue())

    def test_workflow_control_rejects_unsafe_or_invalid_settings_without_docker(self):
        self.app.private_control()
        for content in ('corrupt', '[]', '{"workflow_publication": "false"}'):
            self.app.settings.write_text(content)
            with self.subTest(content=content), self.assertRaises(devloop.Error):
                self.app.github(workflows='enable')
            self.assertEqual(self.app.settings.read_text(), content)
        self.app.settings.unlink()
        outside = self.root / 'outside.json'
        outside.write_text('{}')
        self.app.settings.symlink_to(outside)
        with self.assertRaises(devloop.Error):
            self.app.github(workflows='disable')
        self.assertEqual(outside.read_text(), '{}')
        self.assertEqual(self.runner.calls, [])

    def test_workflow_control_obeys_administration_lock(self):
        with self.app.administration(), self.assertRaisesRegex(devloop.Error, 'Another DevLoop'):
            self.app.github(workflows='enable')
        self.assertFalse(self.app.settings.exists())
        self.assertEqual(self.runner.calls, [])

    def test_workflow_default_overrides_ambient_environment_for_all_lifecycle_calls(self):
        with patch.dict(os.environ, {'BROKER_WORKFLOW_PUBLICATION': 'verified'}):
            self.app.dc('config', '--format', 'json')
        self.assertEqual(self.runner.calls[-1][1]['env']['BROKER_WORKFLOW_PUBLICATION'], '')

    def test_runner_passes_host_setting_to_real_subprocess(self):
        value = devloop.Runner().run([sys.executable, '-I', '-c', "import os; print(os.environ['BROKER_WORKFLOW_PUBLICATION'])"],
                                    env={**os.environ, 'BROKER_WORKFLOW_PUBLICATION': 'verified'})
        self.assertEqual(value, 'verified')

    def test_github_status_has_no_docker_or_remote_actions(self):
        store = devloop.AppSettings(self.root / 'private/github')
        with patch('scripts.devloop.AppSettings', return_value=store), patch('scripts.devloop.terminal', return_value=False):
            self.app.github()
        self.assertIn('does not enable PR publishing', self.output.getvalue())
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(store.directory.exists())

    def test_github_setup_requires_a_terminal_before_changing_configuration(self):
        store = devloop.AppSettings(self.root / 'private/github')
        with patch('scripts.devloop.AppSettings', return_value=store), patch('scripts.devloop.terminal', return_value=False):
            with self.assertRaisesRegex(devloop.Error, 'interactive terminal'):
                self.app.github(setup=True)
        self.assertFalse(store.directory.exists())

    def test_github_setup_preserves_known_installation_and_does_not_enable_publishing(self):
        store = devloop.AppSettings(self.root / 'private/github')
        store.configure(installation_id=173550314)
        with patch('scripts.devloop.AppSettings', return_value=store), patch('scripts.devloop.terminal', return_value=True), \
             patch('builtins.input', side_effect=['12345', '', '']):
            self.app.github(setup=True)
        self.assertEqual(store.load()['app_id'], '12345')
        self.assertEqual(store.load()['installation_id'], 173550314)
        self.assertNotIn('publishing_enabled', store.status())
        self.assertEqual(self.runner.calls, [])

    def test_github_start_requires_credentials_before_docker_actions(self):
        store = devloop.AppSettings(self.root / 'private/github')
        with patch('scripts.devloop.AppSettings', return_value=store):
            with self.assertRaisesRegex(devloop.Error, 'credential setup'):
                self.app.github(start=True)
        self.assertEqual(self.runner.calls, [])

    def test_github_start_builds_isolated_broker_and_registers_without_remote_writes(self):
        self.prepare()
        info = {'app_id': '5402740', 'installation_id': 173550314, 'key_present': True, 'key_path': 'private/key.pem'}
        with patch('scripts.devloop.AppSettings') as store:
            store.return_value.status.return_value = info
            self.app.github(start=True)
        commands = self.commands()
        self.assertIn(['--profile', 'github', 'build', 'github-broker'], commands)
        configured = [options for _, options in self.runner.calls if options.get('data') == devloop.GITHUB_MCP_CONFIGURE]
        self.assertEqual(len(configured), 1)
        self.assertTrue((self.app.control / 'github-enabled').is_file())
        self.assertFalse(any('check_github_broker.py' in str(args) for args, _ in self.runner.calls))

    def test_github_checks_separate_read_only_from_explicit_canary_creation(self):
        self.prepare()
        self.runner.broker_running = True
        store = devloop.AppSettings(self.root / 'private/github')
        with patch('scripts.devloop.AppSettings', return_value=store):
            self.app.github(check=True)
            self.app.github(proposal_test=True)
        checks = [args for args, _ in self.runner.calls if 'check_github_broker.py' in str(args)]
        self.assertEqual(len(checks), 2)
        self.assertNotIn('--proposal', checks[0])
        self.assertIn('--proposal', checks[1])

    def test_enabled_broker_participates_in_start_stop_and_rebuild(self):
        self.prepare()
        self.app.private_control()
        (self.app.control / 'github-enabled').write_text('enabled\n')
        self.app.start()
        self.app.stop()
        self.app.rebuild()
        commands = self.commands()
        self.assertIn(['--profile', 'github', 'stop'], commands)
        self.assertTrue(any(args[:3] == ['--profile', 'github', 'up'] and 'github-broker' in args for args in commands))
        self.assertIn(['--profile', 'github', 'build', 'github-broker'], commands)
        self.assertFalse(any('down' in args for args in commands))

    def test_model_survives_a_new_cli_instance_outside_agent_mounts(self):
        self.app.save_model("nous/model-a")
        self.assertEqual(devloop.DevLoop(self.root, self.runner).selected_model(), "nous/model-a")
        self.assertEqual(self.app.settings.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("hermes", self.app.settings.relative_to(self.root / "runtime").parts)

    def test_model_write_preserves_unknown_settings(self):
        self.app.save_model("nous/model-a")
        settings = {"model": "nous/model-a", "custom": "preserve"}
        self.app.settings.write_text(json.dumps(settings))
        self.app.save_model("nous/model-b")
        self.assertEqual(json.loads(self.app.settings.read_text())["custom"], "preserve")

    def test_corrupt_settings_are_not_overwritten(self):
        self.app.private_control()
        self.app.settings.write_text("corrupt")
        with self.assertRaises(devloop.Error):
            self.app.save_model("nous/model-a")
        self.assertEqual(self.app.settings.read_text(), "corrupt")

    def test_symlinks_cannot_redirect_model_writes(self):
        self.app.private_control()
        outside = self.root / "outside.json"
        outside.write_text("unchanged")
        self.app.settings.symlink_to(outside)
        with self.assertRaises(devloop.Error):
            self.app.save_model("nous/model-a")
        self.assertEqual(outside.read_text(), "unchanged")

    def test_conflicting_admin_actions_are_rejected(self):
        with self.app.administration():
            with self.assertRaisesRegex(devloop.Error, "Another DevLoop"):
                with devloop.DevLoop(self.root, self.runner).administration():
                    self.fail("Concurrent administration was allowed")

    def test_status_distinguishes_pending_login_without_inference(self):
        self.app.status()
        self.assertIn("Nous login required", self.output.getvalue())
        self.assertFalse(any(args[-1:] == ["models"] for args in self.commands()))

    def test_status_does_not_claim_stored_credentials_prove_provider_access(self):
        self.runner.logged_in = self.runner.health = True
        self.app.status()
        self.assertIn("Provider access has not been checked", self.output.getvalue())
        self.assertFalse(any(args[-1:] == ["models"] for args in self.commands()))

    def test_models_are_verified_before_persisting_default(self):
        self.ready()
        self.app.models(select="nous/model-b")
        self.assertEqual(self.app.selected_model(), "nous/model-b")
        with self.assertRaises(devloop.Error):
            self.app.models(select="invented/model")
        self.assertEqual(self.app.selected_model(), "nous/model-b")

    def test_noninteractive_models_list_does_not_prompt_or_save(self):
        self.ready()
        with patch("scripts.devloop.terminal", return_value=False), patch("builtins.input", side_effect=AssertionError("prompted")):
            self.app.models()
        self.assertIn("nous/model-a", self.output.getvalue())
        self.assertIsNone(self.app.selected_model())

    def test_model_authorization_error_is_actionable(self):
        self.ready()
        self.runner.model_error = {"ok": False, "http_status": 401}
        with self.assertRaisesRegex(devloop.Error, "login"):
            self.app.models()

    def test_empty_model_list_does_not_invent_a_default(self):
        self.ready()
        self.runner.models = []
        with self.assertRaisesRegex(devloop.Error, "no available models"):
            self.app.models()
        self.assertIsNone(self.app.selected_model())

    def test_login_restarts_verifies_and_prompts_for_model(self):
        self.prepare()
        with patch("scripts.devloop.terminal", return_value=True), patch("builtins.input", return_value="2"):
            self.app.login()
        commands = self.commands()
        login = next(i for i, args in enumerate(commands) if "--no-browser" in args)
        restart = next(i for i, args in enumerate(commands) if args[:1] == ["restart"])
        verify = next(i for i, args in enumerate(commands) if args[-1:] == ["models"])
        self.assertLess(login, restart)
        self.assertLess(restart, verify)
        self.assertEqual(self.app.selected_model(), "nous/model-b")
        self.assertFalse(any("chat" in args for args in commands))

    def test_cancelled_login_does_not_restart_or_claim_success(self):
        self.prepare()
        self.runner.fail_login = True
        with patch("scripts.devloop.terminal", return_value=True):
            with self.assertRaises(devloop.Error):
                self.app.login()
        self.assertFalse(any(args[:1] == ["restart"] for args in self.commands()))
        self.assertNotIn("login verified", self.output.getvalue())

    def test_interactive_actions_refuse_headless_execution_before_changes(self):
        with patch("scripts.devloop.terminal", return_value=False):
            for action in (self.app.login, self.app.chat, self.app.shell):
                with self.subTest(action=action):
                    with self.assertRaisesRegex(devloop.Error, "interactive terminal"):
                        action()
        self.assertEqual(self.runner.calls, [])

    def test_logout_clears_only_gateway_login_and_preserves_user_data(self):
        self.ready()
        sentinel = self.root / "runtime/hermes/workspace/project.txt"
        sentinel.write_text("keep")
        self.app.save_model("nous/model-a")
        self.app.logout()
        self.assertFalse(self.runner.logged_in)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual(self.app.selected_model(), "nous/model-a")
        self.assertTrue(any("_clear_shared_nous_state" in str(args) for args in self.commands()))
        self.assertTrue(any(args[:1] == ["restart"] for args in self.commands()))

    def test_chat_uses_selected_model_and_dummy_key_without_persisting_override(self):
        self.ready()
        self.app.save_model("nous/model-a")
        with patch("scripts.devloop.terminal", return_value=True):
            self.app.chat(model="nous/model-b")
        chat = next(args for args in self.commands() if "chat" in args)
        self.assertEqual(chat[chat.index("--api-key") + 1], "unused")
        self.assertEqual(chat[chat.index("--model") + 1], "nous/model-b")
        self.assertEqual(chat[chat.index("--base-url") + 1], "http://llm-gateway-api:8645/v1")
        self.assertEqual(self.app.selected_model(), "nous/model-a")

    def test_stale_model_is_reselected_before_chat(self):
        self.ready()
        self.app.save_model("retired/model")
        with patch("scripts.devloop.terminal", return_value=True), patch("builtins.input", return_value="1"):
            self.app.chat()
        self.assertEqual(self.app.selected_model(), "nous/model-a")

    def test_cancelled_selection_cannot_launch_a_retired_model(self):
        self.ready()
        self.app.save_model("retired/model")
        with patch("scripts.devloop.terminal", return_value=True), patch("builtins.input", return_value=""):
            with self.assertRaisesRegex(devloop.Error, "No model selected"):
                self.app.chat()
        self.assertFalse(any("chat" in args for args in self.commands()))

    def test_status_probe_uses_trusted_gateway_and_isolated_python(self):
        self.app.status()
        probes = [args for args, options in self.runner.calls if options.get("data") == devloop.PROBE]
        self.assertEqual(len(probes), 1)
        self.assertIn("llm-gateway", probes[0])
        self.assertIn("/usr/local/bin/python3", probes[0])
        self.assertIn("-I", probes[0])

    def test_stop_preserves_data_and_model(self):
        self.ready()
        self.app.save_model("nous/model-a")
        self.app.stop()
        self.assertFalse(self.runner.running)
        self.assertEqual(self.app.selected_model(), "nous/model-a")
        self.assertFalse(any("down" in args for args in self.commands()))

    def test_persistence_and_authenticated_checks_are_explicit(self):
        self.ready()
        self.app.test()
        self.app.test(persistence=True, authenticated=True)
        calls = [args for args, _ in self.runner.calls if args[0] == sys.executable and args[1].endswith("security.py")]
        self.assertNotIn("--persistence", calls[0])
        self.assertNotIn("--authenticated", calls[0])
        self.assertIn("--persistence", calls[1])
        self.assertIn("--authenticated", calls[1])

    def test_rebuild_keeps_the_pinned_sources_and_runtime(self):
        self.ready()
        self.app.save_model("nous/model-a")
        self.app.rebuild()
        self.assertIn(["build"], self.commands())
        self.assertTrue(any("--force-recreate" in args for args in self.commands()))
        self.assertFalse(any("--pull" in args or "down" in args for args in self.commands()))
        self.assertEqual(self.app.selected_model(), "nous/model-a")

    def test_doctor_treats_missing_login_as_an_action_not_a_broken_install(self):
        self.prepare()
        self.assertEqual(self.app.doctor(), 0)
        self.assertIn("ACTION: Nous login required", self.output.getvalue())

    def test_subnet_overlap_is_reported_without_mutating_networks(self):
        self.runner.networks = [{"Labels": {}, "IPAM": {"Config": [{"Subnet": "10.237.0.0/16"}]}}]
        with self.assertRaisesRegex(devloop.Error, "overlaps another Docker"):
            self.app.network_check()
        self.assertFalse(any("create" in args or "rm" in args for args, _ in self.runner.calls))

    def test_own_networks_and_other_ipv6_networks_do_not_conflict(self):
        self.runner.networks = [
            {"Id": "a" * 64, "Labels": {"com.docker.compose.project": devloop.PROJECT}, "IPAM": {"Config": [{"Subnet": "10.237.80.0/24"}]}},
            {"Labels": {}, "IPAM": {"Config": [{"Subnet": "fd00:1::/64"}]}}]
        self.app.network_check()

    def test_lan_route_conflict_is_reported(self):
        self.runner.routes = [{"dst": "10.237.80.0/24", "dev": "vpn0"}]
        with self.assertRaisesRegex(devloop.Error, "host/LAN/VPN route"):
            self.app.network_check()

    def test_log_filter_masks_common_credentials_and_terminal_escapes(self):
        text = '\x1b[31mAuthorization: Bearer very-secret\n{"refresh_token":"another-secret"}\naccess_token=value\n'
        output = devloop.redact(text)
        self.assertNotIn("very-secret", output)
        self.assertNotIn("another-secret", output)
        self.assertNotIn("=value", output)
        self.assertNotIn("\x1b", output)
        self.assertIn("[redacted]", output)

    def test_menu_dispatches_actions_and_returns_after_an_error(self):
        cli = devloop.parser()
        with patch("builtins.input", side_effect=["invalid", "status", "help", "q"]):
            self.assertEqual(devloop.menu(self.app, cli), 0)
        self.assertIn("Choose a listed action", self.output.getvalue())
        self.assertIn("Nous login required", self.output.getvalue())

    def test_entrypoint_works_from_another_directory_without_docker(self):
        for arguments in ([], ["help"], ["--help"]):
            result = subprocess.run([sys.executable, str(devloop.ROOT / "devloop"), *arguments], cwd=self.root, input="", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("setup", result.stdout)
            self.assertIn("login", result.stdout)


if __name__ == "__main__":
    unittest.main()
