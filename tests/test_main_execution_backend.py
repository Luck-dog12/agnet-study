"""Stage 5B3b CLI backend selection and lazy runtime assembly tests."""

from __future__ import annotations

import io
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.config import Settings
from app.schemas import TurnStartEventData
from app.workflow import SessionEventWorkflow, SessionStoreBusyError
from app.workspace_checkpoint import WorkspaceCheckpointError
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot


class _FakeSandboxSettings:
    def __init__(self) -> None:
        self.require_calls = 0

    def require_enabled(self) -> None:
        self.require_calls += 1


class _StubAgent:
    max_tool_rounds = 8
    max_plan_clarifications = 5

    def __init__(self, workflow) -> None:
        self.workflow = workflow
        self.prompts: list[str] = []
        self.close_calls = 0

    def pending_recovery(self):
        return ()

    def run(self, prompt, *, on_text_delta=None):
        del on_text_delta
        self.prompts.append(prompt)
        return "done"

    def plan(self, prompt, *, on_text_delta=None):
        return self.run(prompt, on_text_delta=on_text_delta)

    def close(self, *, wait=True):
        del wait
        self.close_calls += 1
        self.workflow.close()


class MainExecutionBackendTests(unittest.TestCase):
    def test_checkpoint_layout_preflight_fails_before_backend_construction(self):
        from app import main as main_module

        path = "/".join([f"d{index}" for index in range(64)] + ["file.txt"])
        body = b"x"
        snapshot = WorkspaceSeedSnapshot(
            (
                WorkspaceSeedEntry(
                    path=path,
                    size=1,
                    sha256=hashlib.sha256(body).hexdigest(),
                    data=body,
                ),
            )
        )
        backend_factory = Mock(side_effect=AssertionError("backend must not exist"))
        with patch.object(
            main_module,
            "capture_workspace_seed",
            return_value=snapshot,
        ), patch(
            "app.execution_backend.AliyunSandboxExecutionBackend",
            backend_factory,
        ), self.assertRaises(WorkspaceCheckpointError):
            main_module.create_execution_runtime(
                "aliyun-sandbox",
                "a" * 32,
                object(),
            )
        backend_factory.assert_not_called()

    def test_remote_seed_capture_failure_creates_no_backend_client_or_cloud(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            sandbox_settings = _FakeSandboxSettings()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                ) as cleanup,
                patch.object(
                    main_module,
                    "capture_workspace_seed",
                    side_effect=RuntimeError("SECRET host race"),
                ) as capture,
                patch(
                    "app.execution_backend.AliyunSandboxExecutionBackend",
                    side_effect=AssertionError("must not construct backend"),
                ) as backend_factory,
                patch.object(main_module, "create_client") as client_factory,
                patch("builtins.input", return_value="trigger"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                self.assertEqual(
                    main_module.main(
                        [
                            "--new-session",
                            "seed-failure",
                            "--execution-backend",
                            "aliyun-sandbox",
                            "--plain",
                        ]
                    ),
                    2,
                )
            capture.assert_called_once_with()
            cleanup.assert_not_called()
            backend_factory.assert_not_called()
            client_factory.assert_not_called()
            self.assertNotIn("SECRET", stderr.getvalue())
            with SessionEventWorkflow(
                storage_directory=sessions / "seed-failure",
                execution_backend_name="aliyun-sandbox",
            ):
                pass

    def test_lazy_factory_binds_exact_backend_and_cleans_partial_construction(self):
        from app import main as main_module

        settings = object()
        backend = Mock()
        runtime = Mock()
        snapshot = object()
        initializer = Mock()
        with patch.object(
            main_module, "capture_workspace_seed", return_value=snapshot
        ) as capture, patch.object(
            main_module,
            "create_workspace_seed_initializer",
            return_value=initializer,
        ) as initializer_factory, patch(
            "app.execution_backend.AliyunSandboxExecutionBackend",
            return_value=backend,
        ) as backend_factory, patch(
            "app.execution_runtime.ExecutionToolRuntime",
            return_value=runtime,
        ) as runtime_factory:
            self.assertIs(
                main_module.create_execution_runtime(
                    "aliyun-sandbox", "a" * 32, settings
                ),
                runtime,
            )
        capture.assert_called_once_with()
        initializer_factory.assert_called_once_with(snapshot)
        backend_factory.assert_called_once_with(
            "a" * 32,
            settings,
            workspace_initializer=initializer,
        )
        runtime_factory.assert_called_once_with(backend)
        backend.ensure_ready.assert_not_called()
        backend.close.assert_not_called()

        failed_backend = Mock()
        with patch.object(
            main_module, "capture_workspace_seed", return_value=snapshot
        ), patch.object(
            main_module,
            "create_workspace_seed_initializer",
            return_value=initializer,
        ), patch(
            "app.execution_backend.AliyunSandboxExecutionBackend",
            return_value=failed_backend,
        ), patch(
            "app.execution_runtime.ExecutionToolRuntime",
            side_effect=RuntimeError("SECRET runtime construction"),
        ):
            with self.assertRaisesRegex(RuntimeError, "SECRET"):
                main_module.create_execution_runtime(
                    "aliyun-sandbox", "b" * 32, settings
                )
        failed_backend.close.assert_called_once_with()

    def test_runtime_factory_enables_orphan_gc_only_with_explicit_token(self):
        from app import main as main_module

        snapshot = object()
        initializer = Mock()
        backend = Mock()
        runtime = Mock()
        with (
            patch.object(
                main_module,
                "capture_workspace_seed",
                return_value=snapshot,
            ),
            patch.object(
                main_module,
                "create_workspace_seed_initializer",
                return_value=initializer,
            ) as initializer_factory,
            patch(
                "app.execution_backend.AliyunSandboxExecutionBackend",
                return_value=backend,
            ),
            patch(
                "app.execution_runtime.ExecutionToolRuntime",
                return_value=runtime,
            ),
        ):
            self.assertIs(
                main_module.create_execution_runtime(
                    "aliyun-sandbox",
                    "a" * 32,
                    object(),
                    collect_orphans=True,
                ),
                runtime,
            )
        initializer_factory.assert_called_once_with(
            snapshot,
            collect_orphans=True,
        )

        with self.assertRaises(TypeError):
            main_module.create_execution_runtime(
                "aliyun-sandbox",
                "a" * 32,
                object(),
                collect_orphans=1,
            )

    def test_parser_defaults_accepts_explicit_and_rejects_invalid_backend(self):
        from app import main as main_module

        parser = main_module._build_argument_parser()
        self.assertIsNone(parser.parse_args([]).execution_backend)
        self.assertEqual(
            parser.parse_args(
                ["--execution-backend", "aliyun-sandbox"]
            ).execution_backend,
            "aliyun-sandbox",
        )
        help_text = parser.format_help()
        self.assertIn("AGENT_DEFAULT_EXECUTION_BACKEND", help_text)
        self.assertIn("缺省", help_text)
        self.assertIn("local", help_text)
        self.assertIn("恢复会话省略时只采用其持久绑定", help_text)
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--execution-backend", "auto"])

    def test_local_first_request_never_loads_sandbox_config_or_runtime(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            client = Mock()
            observer = Mock()
            maintenance = Mock()
            built: list[_StubAgent] = []

            def build_agent(_client, _settings, **kwargs):
                self.assertNotIn("execution_runtime", kwargs)
                agent = _StubAgent(kwargs["session_workflow"])
                built.append(agent)
                return agent

            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    side_effect=AssertionError("local must not load sandbox config"),
                ),
                patch.object(
                    main_module,
                    "create_execution_runtime",
                    side_effect=AssertionError("local must not create runtime"),
                ),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "CodingAgent", side_effect=build_agent),
                patch.object(
                    main_module, "MaintenanceService", return_value=maintenance
                ),
                patch("builtins.input", side_effect=["work", "exit"]),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "local", "--plain"]),
                    0,
                )
            self.assertEqual(built[0].prompts, ["work"])
            self.assertEqual(built[0].close_calls, 1)
            with SessionEventWorkflow(
                storage_directory=sessions / "local",
            ) as restored:
                self.assertEqual(restored.execution_backend_name, "local")

    def test_omitted_new_direct_exit_never_loads_sandbox_or_model_runtime(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            forbidden = {
                "load_sandbox_settings": Mock(
                    side_effect=AssertionError("local exit loaded sandbox config")
                ),
                "create_execution_runtime": Mock(
                    side_effect=AssertionError("local exit created runtime")
                ),
                "create_client": Mock(
                    side_effect=AssertionError("local exit created client")
                ),
            }
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.multiple(main_module, **forbidden),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "local-exit", "--plain"]),
                    0,
                )
            for factory in forbidden.values():
                factory.assert_not_called()
            with SessionEventWorkflow(
                storage_directory=sessions / "local-exit",
                recover=False,
            ) as restored:
                self.assertEqual(restored.execution_backend_name, "local")

    def test_configured_remote_default_binds_new_named_and_current_sessions(
        self,
    ) -> None:
        from app import main as main_module

        for name, arguments, relative in (
            (
                "named",
                ["--new-session", "remote-default", "--plain"],
                "remote-default",
            ),
            ("current", ["--plain"], "current"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                sessions = Path(root) / "sessions"
                sandbox_settings = _FakeSandboxSettings()
                forbidden = {
                    "capture_workspace_seed": Mock(
                        side_effect=AssertionError("direct exit scanned workspace")
                    ),
                    "create_execution_runtime": Mock(
                        side_effect=AssertionError("direct exit created runtime")
                    ),
                    "create_client": Mock(
                        side_effect=AssertionError("direct exit created client")
                    ),
                }
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_default_execution_backend",
                        return_value="aliyun-sandbox",
                    ),
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        return_value=sandbox_settings,
                    ) as sandbox_loader,
                    patch.multiple(main_module, **forbidden),
                    patch("builtins.input", return_value="exit"),
                    patch("sys.stdout", io.StringIO()),
                    patch("sys.stderr", io.StringIO()),
                ):
                    self.assertEqual(main_module.main(arguments), 0)

                sandbox_loader.assert_called_once_with()
                self.assertEqual(sandbox_settings.require_calls, 1)
                for factory in forbidden.values():
                    factory.assert_not_called()
                with SessionEventWorkflow(
                    storage_directory=sessions / relative,
                    recover=False,
                ) as restored:
                    self.assertEqual(
                        restored.execution_backend_name,
                        "aliyun-sandbox",
                    )

    def test_explicit_backends_ignore_invalid_default(self) -> None:
        from app import main as main_module

        for backend in ("local", "aliyun-sandbox"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as root:
                sessions = Path(root) / "sessions"
                sandbox_settings = _FakeSandboxSettings()
                default_loader = Mock(
                    side_effect=ValueError("SECRET invalid default")
                )
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_default_execution_backend",
                        default_loader,
                    ),
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        return_value=sandbox_settings,
                    ) as sandbox_loader,
                    patch("builtins.input", return_value="exit"),
                    patch("sys.stdout", io.StringIO()),
                    patch("sys.stderr", io.StringIO()),
                ):
                    self.assertEqual(
                        main_module.main([
                            "--new-session",
                            f"explicit-{backend}",
                            "--execution-backend",
                            backend,
                            "--plain",
                        ]),
                        0,
                    )
                default_loader.assert_not_called()
                self.assertEqual(
                    sandbox_loader.call_count,
                    1 if backend == "aliyun-sandbox" else 0,
                )
                self.assertEqual(
                    sandbox_settings.require_calls,
                    1 if backend == "aliyun-sandbox" else 0,
                )
                with SessionEventWorkflow(
                    storage_directory=sessions / f"explicit-{backend}",
                    recover=False,
                ) as restored:
                    self.assertEqual(restored.execution_backend_name, backend)

    def test_remote_default_failure_creates_no_new_named_or_current_store(
        self,
    ) -> None:
        from app import main as main_module

        for name, arguments in (
            ("named", ["--new-session", "remote-failure"]),
            ("current", []),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                sessions = Path(root) / "sessions"
                sandbox_settings = Mock()
                sandbox_settings.require_enabled.side_effect = ValueError(
                    "SECRET remote default disabled"
                )
                forbidden = {
                    "capture_workspace_seed": Mock(
                        side_effect=AssertionError("failure scanned workspace")
                    ),
                    "create_execution_runtime": Mock(
                        side_effect=AssertionError("failure created runtime")
                    ),
                    "create_client": Mock(
                        side_effect=AssertionError("failure created client")
                    ),
                }
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_default_execution_backend",
                        return_value="aliyun-sandbox",
                    ),
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        return_value=sandbox_settings,
                    ) as sandbox_loader,
                    patch.multiple(main_module, **forbidden),
                    patch("sys.stderr", io.StringIO()) as stderr,
                ):
                    self.assertEqual(main_module.main(arguments), 2)

                sandbox_loader.assert_called_once_with()
                sandbox_settings.require_enabled.assert_called_once_with()
                for factory in forbidden.values():
                    factory.assert_not_called()
                self.assertFalse(sessions.exists())
                self.assertNotIn("SECRET", stderr.getvalue())

    def test_new_default_backend_race_rejects_opposite_injected_binding(self):
        from app import main as main_module

        for configured_backend, injected_backend in (
            ("aliyun-sandbox", "local"),
            ("local", "aliyun-sandbox"),
        ):
            with (
                self.subTest(
                    configured=configured_backend,
                    injected=injected_backend,
                ),
                tempfile.TemporaryDirectory() as root,
            ):
                sessions = Path(root) / "sessions"
                session_name = f"race-{configured_backend}"
                session_path = sessions / session_name
                sandbox_settings = _FakeSandboxSettings()
                sandbox_loader = Mock(return_value=sandbox_settings)
                injected_json: dict[str, bytes] = {}
                observed_arguments: dict[str, object] = {}

                def inject_opposite_binding(**kwargs):
                    observed_arguments.update(kwargs)
                    with SessionEventWorkflow(
                        storage_directory=kwargs["storage_directory"],
                        execution_backend_name=injected_backend,
                        recover=False,
                    ):
                        pass
                    injected_json.update(
                        {
                            path.name: path.read_bytes()
                            for path in session_path.glob("*.json")
                        }
                    )
                    return SessionEventWorkflow(**kwargs)

                forbidden = {
                    "capture_workspace_seed": Mock(
                        side_effect=AssertionError("race scanned workspace")
                    ),
                    "create_execution_runtime": Mock(
                        side_effect=AssertionError("race created runtime")
                    ),
                    "create_client": Mock(
                        side_effect=AssertionError("race created client")
                    ),
                }
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_default_execution_backend",
                        return_value=configured_backend,
                    ) as default_loader,
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        sandbox_loader,
                    ),
                    patch(
                        "app.workflow.SessionEventWorkflow",
                        side_effect=inject_opposite_binding,
                    ) as workflow_factory,
                    patch.multiple(main_module, **forbidden),
                    patch("sys.stderr", io.StringIO()) as stderr,
                ):
                    self.assertEqual(
                        main_module.main(
                            ["--new-session", session_name, "--plain"]
                        ),
                        2,
                    )

                default_loader.assert_called_once_with()
                workflow_factory.assert_called_once()
                self.assertEqual(
                    observed_arguments["execution_backend_name"],
                    configured_backend,
                )
                self.assertEqual(
                    observed_arguments["new_session_backend_name"],
                    configured_backend,
                )
                self.assertIs(
                    observed_arguments["require_existing_identity"],
                    False,
                )
                self.assertEqual(
                    sandbox_loader.call_count,
                    1 if configured_backend == "aliyun-sandbox" else 0,
                )
                self.assertEqual(
                    sandbox_settings.require_calls,
                    1 if configured_backend == "aliyun-sandbox" else 0,
                )
                for factory in forbidden.values():
                    factory.assert_not_called()
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in session_path.glob("*.json")
                    },
                    injected_json,
                )
                self.assertEqual(
                    tuple(session_path.glob("event-*.json")),
                    (),
                )
                self.assertNotIn("SECRET", stderr.getvalue())
                with SessionEventWorkflow(
                    storage_directory=session_path,
                    execution_backend_name=injected_backend,
                    recover=False,
                ) as restored:
                    self.assertEqual(
                        restored.execution_backend_name,
                        injected_backend,
                    )

    def test_invalid_default_fails_only_unbound_omitted_paths_without_artifacts(
        self,
    ) -> None:
        from app import main as main_module

        for name, arguments in (
            ("named", ["--new-session", "invalid-default"]),
            ("current", []),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                sessions = Path(root) / "sessions"
                forbidden = {
                    "load_sandbox_settings": Mock(
                        side_effect=AssertionError("invalid default loaded sandbox")
                    ),
                    "capture_workspace_seed": Mock(
                        side_effect=AssertionError("invalid default scanned workspace")
                    ),
                    "create_execution_runtime": Mock(
                        side_effect=AssertionError("invalid default created runtime")
                    ),
                    "create_client": Mock(
                        side_effect=AssertionError("invalid default created client")
                    ),
                }
                default_loader = Mock(
                    side_effect=ValueError(
                        "AGENT_DEFAULT_EXECUTION_BACKEND 必须是 "
                        "local 或 aliyun-sandbox。"
                    )
                )
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_default_execution_backend",
                        default_loader,
                    ),
                    patch.multiple(main_module, **forbidden),
                    patch("sys.stderr", io.StringIO()) as stderr,
                ):
                    self.assertEqual(main_module.main(arguments), 2)

                default_loader.assert_called_once_with()
                for factory in forbidden.values():
                    factory.assert_not_called()
                self.assertFalse(sessions.exists())
                self.assertEqual(
                    stderr.getvalue(),
                    "启动失败：请检查项目配置和环境变量。\n",
                )

    def test_invalid_default_is_ignored_by_resume_and_existing_current(self) -> None:
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            for relative, backend in (
                ("local-resume", "local"),
                ("remote-resume", "aliyun-sandbox"),
                ("current", "local"),
            ):
                with SessionEventWorkflow(
                    storage_directory=sessions / relative,
                    execution_backend_name=backend,
                    recover=False,
                ):
                    pass
            before = {
                relative: (sessions / relative / "execution.json").read_bytes()
                for relative in ("local-resume", "remote-resume", "current")
            }
            sandbox_settings = _FakeSandboxSettings()
            default_loader = Mock(
                side_effect=ValueError("SECRET invalid default")
            )

            for arguments in (
                ["--resume", "local-resume", "--plain"],
                ["--resume", "remote-resume", "--plain"],
                ["--plain"],
            ):
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_default_execution_backend",
                        default_loader,
                    ),
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        return_value=sandbox_settings,
                    ),
                    patch("builtins.input", return_value="exit"),
                    patch("sys.stdout", io.StringIO()),
                    patch("sys.stderr", io.StringIO()),
                ):
                    self.assertEqual(main_module.main(arguments), 0)

            default_loader.assert_not_called()
            self.assertEqual(sandbox_settings.require_calls, 1)
            for relative, binding in before.items():
                self.assertEqual(
                    (sessions / relative / "execution.json").read_bytes(),
                    binding,
                )

    def test_remote_config_failure_precedes_new_directory_creation(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    side_effect=ValueError("SECRET invalid sandbox config"),
                ),
                patch.object(main_module, "create_client") as client_factory,
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                self.assertEqual(
                    main_module.main(
                        [
                            "--new-session",
                            "remote-not-created",
                            "--execution-backend",
                            "aliyun-sandbox",
                        ]
                    ),
                    2,
                )
            self.assertFalse(sessions.exists())
            client_factory.assert_not_called()
            self.assertNotIn("SECRET", stderr.getvalue())

    def test_inferred_remote_config_failure_preserves_store_and_releases_lock(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            session_path = sessions / "remote-resume"
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as workflow:
                workflow.append(
                    "turn/start",
                    TurnStartEventData(turn_id=uuid4().hex),
                )
                session_id = workflow.session_id
            before = {
                path.name: path.read_bytes()
                for path in session_path.glob("*.json")
            }
            runtime_factory = Mock(
                side_effect=AssertionError("must not create a runtime")
            )
            client_factory = Mock(
                side_effect=AssertionError("must not create a client")
            )
            seed_capture = Mock(
                side_effect=AssertionError("must not inspect the host workspace")
            )

            def fail_inferred_config() -> object:
                self.assertEqual(
                    (session_path / "execution.json").read_bytes(),
                    before["execution.json"],
                )
                with self.assertRaises(SessionStoreBusyError):
                    SessionEventWorkflow(
                        storage_directory=session_path,
                        recover=False,
                    )
                raise ValueError("SECRET invalid inferred config")

            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    side_effect=fail_inferred_config,
                ) as sandbox_loader,
                patch.object(
                    main_module,
                    "create_execution_runtime",
                    runtime_factory,
                ),
                patch.object(main_module, "create_client", client_factory),
                patch.object(main_module, "capture_workspace_seed", seed_capture),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                self.assertEqual(
                    main_module.main(["--resume", "remote-resume"]),
                    2,
                )

            sandbox_loader.assert_called_once_with()
            runtime_factory.assert_not_called()
            client_factory.assert_not_called()
            seed_capture.assert_not_called()
            self.assertNotIn("SECRET", stderr.getvalue())
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in session_path.glob("*.json")
                },
                before,
            )
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as reopened:
                self.assertEqual(reopened.session_id, session_id)
                self.assertEqual(len(reopened.events), 1)

    def test_explicit_remote_resume_requires_config_after_binding_and_unlocks(
        self,
    ) -> None:
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            session_path = sessions / "explicit-remote"
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as workflow:
                workflow.append(
                    "turn/start",
                    TurnStartEventData(turn_id=uuid4().hex),
                )
            before = {
                path.name: path.read_bytes()
                for path in session_path.glob("*.json")
            }
            settings = Mock()

            def require_enabled() -> None:
                self.assertEqual(
                    (session_path / "execution.json").read_bytes(),
                    before["execution.json"],
                )
                with self.assertRaises(SessionStoreBusyError):
                    SessionEventWorkflow(
                        storage_directory=session_path,
                        recover=False,
                    )
                raise ValueError("SECRET disabled remote config")

            settings.require_enabled.side_effect = require_enabled
            forbidden = {
                "create_execution_runtime": Mock(
                    side_effect=AssertionError("config failure created runtime")
                ),
                "create_client": Mock(
                    side_effect=AssertionError("config failure created client")
                ),
                "capture_workspace_seed": Mock(
                    side_effect=AssertionError("config failure captured seed")
                ),
            }
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=settings,
                ) as sandbox_loader,
                patch.multiple(main_module, **forbidden),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                self.assertEqual(
                    main_module.main([
                        "--resume",
                        "explicit-remote",
                        "--execution-backend",
                        "aliyun-sandbox",
                    ]),
                    2,
                )

            sandbox_loader.assert_called_once_with()
            settings.require_enabled.assert_called_once_with()
            for factory in forbidden.values():
                factory.assert_not_called()
            self.assertNotIn("SECRET", stderr.getvalue())
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in session_path.glob("*.json")
                },
                before,
            )
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ):
                pass

    def test_binding_only_crash_window_is_listed_and_auto_resumed(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            session_path = sessions / "remote-crash-window"
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as workflow:
                session_id = workflow.session_id
            (session_path / "session.json").unlink()
            binding = (session_path / "execution.json").read_bytes()
            sandbox_settings = _FakeSandboxSettings()
            runtime_factory = Mock(
                side_effect=AssertionError("direct exit created runtime")
            )
            client_factory = Mock(
                side_effect=AssertionError("direct exit created client")
            )

            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "create_execution_runtime",
                    runtime_factory,
                ),
                patch.object(main_module, "create_client", client_factory),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module._list_sessions(),
                    ("remote-crash-window",),
                )
                self.assertEqual(
                    main_module.main(
                        ["--resume", "remote-crash-window", "--plain"]
                    ),
                    0,
                )

            self.assertEqual(sandbox_settings.require_calls, 1)
            runtime_factory.assert_not_called()
            client_factory.assert_not_called()
            self.assertEqual(
                (session_path / "execution.json").read_bytes(),
                binding,
            )
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as restored:
                self.assertEqual(restored.session_id, session_id)
                self.assertEqual(restored.events, ())

    def test_remote_exit_and_cancelled_plan_bind_session_without_runtime_or_client(self):
        from app import main as main_module

        for name, inputs in (
            ("remote-exit", ["exit"]),
            ("remote-plan-cancel", ["/plan", "", "exit"]),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                sessions = Path(root) / "sessions"
                sandbox_settings = _FakeSandboxSettings()
                runtime_factory = Mock(
                    side_effect=AssertionError("no model request means no runtime")
                )
                client_factory = Mock(
                    side_effect=AssertionError("no model request means no client")
                )
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        return_value=sandbox_settings,
                    ),
                    patch.object(
                        main_module,
                        "create_execution_runtime",
                        runtime_factory,
                    ),
                    patch.object(main_module, "create_client", client_factory),
                    patch("builtins.input", side_effect=inputs),
                    patch("sys.stdout", io.StringIO()),
                    patch("sys.stderr", io.StringIO()),
                ):
                    self.assertEqual(
                        main_module.main(
                            [
                                "--new-session",
                                name,
                                "--execution-backend",
                                "aliyun-sandbox",
                                "--plain",
                            ]
                        ),
                        0,
                    )
                self.assertEqual(sandbox_settings.require_calls, 1)
                runtime_factory.assert_not_called()
                client_factory.assert_not_called()
                with SessionEventWorkflow(
                    storage_directory=sessions / name,
                    execution_backend_name="aliyun-sandbox",
                ) as restored:
                    self.assertEqual(restored.events, ())

    def test_remote_first_request_passes_exact_binding_settings_and_runtime(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            with SessionEventWorkflow(
                storage_directory=sessions / "remote-run",
                execution_backend_name="aliyun-sandbox",
            ) as existing:
                bound_session_id = existing.session_id
            sandbox_settings = _FakeSandboxSettings()
            runtime = Mock()
            client = Mock()
            observer = Mock()
            maintenance = Mock()
            captured: dict[str, object] = {}

            call_order: list[str] = []

            def cleanup_remote(settings, session_id):
                call_order.append("cleanup")
                self.assertIs(settings, sandbox_settings)
                self.assertEqual(session_id, bound_session_id)

            def create_runtime(backend_name, session_id, settings, **kwargs):
                call_order.append("runtime")
                captured["runtime_args"] = (
                    backend_name,
                    session_id,
                    settings,
                    kwargs,
                )
                return runtime

            def build_agent(_client, _settings, **kwargs):
                captured["agent_runtime"] = kwargs.get("execution_runtime")
                captured["workflow"] = kwargs["session_workflow"]
                return _StubAgent(kwargs["session_workflow"])

            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "create_execution_runtime",
                    side_effect=create_runtime,
                ) as runtime_factory,
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    side_effect=cleanup_remote,
                ) as cleanup,
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "CodingAgent", side_effect=build_agent),
                patch.object(
                    main_module, "MaintenanceService", return_value=maintenance
                ),
                patch("builtins.input", side_effect=["inspect", "exit"]),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(
                        [
                            "--resume",
                            "remote-run",
                            "--plain",
                        ]
                    ),
                    0,
                )
            runtime_factory.assert_called_once()
            backend_name, session_id, exact_settings, runtime_kwargs = captured[
                "runtime_args"
            ]
            self.assertEqual(backend_name, "aliyun-sandbox")
            self.assertIs(exact_settings, sandbox_settings)
            self.assertEqual(session_id, bound_session_id)
            self.assertEqual(session_id, captured["workflow"].session_id)
            self.assertEqual(runtime_kwargs, {"collect_orphans": True})
            self.assertEqual(call_order, ["runtime", "cleanup"])
            cleanup.assert_called_once_with(sandbox_settings, bound_session_id)
            self.assertIs(captured["agent_runtime"], runtime)
            runtime.close.assert_not_called()
            client.close.assert_called_once_with()

    def test_remote_first_request_cleanup_failure_prevents_runtime_and_releases_lock(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            session_path = sessions / "remote-cleanup-first-request"
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
            ) as existing:
                session_id = existing.session_id
            sandbox_settings = _FakeSandboxSettings()

            def fail_cleanup(settings, observed_session_id):
                self.assertIs(settings, sandbox_settings)
                self.assertEqual(observed_session_id, session_id)
                with self.assertRaises(SessionStoreBusyError):
                    SessionEventWorkflow(
                        storage_directory=session_path,
                        recover=False,
                    )
                raise RuntimeError("provider-secret-cleanup")

            runtime = Mock()
            runtime_factory = Mock(return_value=runtime)
            client_factory = Mock(side_effect=AssertionError("client"))
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    side_effect=fail_cleanup,
                ) as cleanup,
                patch.object(
                    main_module,
                    "create_execution_runtime",
                    runtime_factory,
                ),
                patch.object(main_module, "create_client", client_factory),
                patch("builtins.input", return_value="trigger"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                self.assertEqual(
                    main_module.main(
                        ["--resume", "remote-cleanup-first-request", "--plain"]
                    ),
                    2,
                )
            cleanup.assert_called_once_with(sandbox_settings, session_id)
            runtime_factory.assert_called_once_with(
                "aliyun-sandbox",
                session_id,
                sandbox_settings,
                collect_orphans=True,
            )
            runtime.close.assert_called_once_with()
            client_factory.assert_not_called()
            self.assertNotIn("provider-secret", stderr.getvalue())
            with SessionEventWorkflow(
                storage_directory=session_path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ):
                pass

    def test_initialization_failures_cleanup_and_release_remote_session_lock(self):
        from app import main as main_module

        cases = ("factory", "client", "observer", "agent")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                sessions = Path(root) / "sessions"
                sandbox_settings = _FakeSandboxSettings()
                runtime = Mock()
                client = Mock()
                observer = Mock()
                runtime_factory = Mock(return_value=runtime)
                client_factory = Mock(return_value=client)
                observer_factory = Mock(return_value=observer)
                agent_factory = Mock(side_effect=RuntimeError("SECRET agent"))
                if case == "factory":
                    runtime_factory.side_effect = RuntimeError("SECRET factory")
                elif case == "client":
                    client_factory.side_effect = RuntimeError("SECRET client")
                elif case == "observer":
                    observer_factory.side_effect = RuntimeError("SECRET observer")

                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        return_value=sandbox_settings,
                    ),
                    patch.object(
                        main_module,
                        "create_execution_runtime",
                        runtime_factory,
                    ),
                    patch.object(
                        main_module,
                        "cleanup_recovered_remote_session",
                    ),
                    patch.object(main_module, "create_client", client_factory),
                    patch.object(main_module, "Observability", observer_factory),
                    patch.object(main_module, "CodingAgent", agent_factory),
                    patch("builtins.input", return_value="trigger"),
                    patch("sys.stdout", io.StringIO()),
                    patch("sys.stderr", io.StringIO()) as stderr,
                ):
                    self.assertEqual(
                        main_module.main(
                            [
                                "--new-session",
                                f"failure-{case}",
                                "--execution-backend",
                                "aliyun-sandbox",
                                "--plain",
                            ]
                        ),
                        2,
                    )
                self.assertNotIn("SECRET", stderr.getvalue())
                if case != "factory":
                    runtime.close.assert_called_once_with()
                else:
                    client_factory.assert_not_called()
                if case in {"observer", "agent"}:
                    client.close.assert_called_once_with()
                if case == "agent":
                    observer.flush_pending.assert_called_once_with()
                with SessionEventWorkflow(
                    storage_directory=sessions / f"failure-{case}",
                    execution_backend_name="aliyun-sandbox",
                ):
                    pass

    def test_resume_adopts_binding_or_rejects_explicit_mismatch_without_rewrite(
        self,
    ):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            with SessionEventWorkflow(
                storage_directory=sessions / "local-session",
                execution_backend_name="local",
            ):
                pass
            with SessionEventWorkflow(
                storage_directory=sessions / "remote-session",
                execution_backend_name="aliyun-sandbox",
            ):
                pass
            local_binding = (sessions / "local-session" / "execution.json").read_bytes()
            remote_binding = (sessions / "remote-session" / "execution.json").read_bytes()

            local_sandbox_loader = Mock(
                side_effect=AssertionError("local binding must not load sandbox config")
            )
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    local_sandbox_loader,
                ),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(main_module.main(["--resume", "local-session"]), 0)
            local_sandbox_loader.assert_not_called()

            inferred_remote_settings = _FakeSandboxSettings()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=inferred_remote_settings,
                ),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(["--resume", "remote-session"]),
                    0,
                )
            self.assertEqual(inferred_remote_settings.require_calls, 1)

            for name, backend_args, should_load_sandbox in (
                (
                    "remote-session",
                    ["--execution-backend", "local"],
                    False,
                ),
                (
                    "local-session",
                    ["--execution-backend", "aliyun-sandbox"],
                    False,
                ),
            ):
                mismatch_settings = _FakeSandboxSettings()
                sandbox_loader = Mock(return_value=mismatch_settings)
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(
                        main_module,
                        "load_settings",
                        return_value=Settings(api_key="fake"),
                    ),
                    patch.object(
                        main_module,
                        "load_sandbox_settings",
                        sandbox_loader,
                    ),
                    patch("sys.stderr", io.StringIO()),
                ):
                    self.assertEqual(
                        main_module.main(["--resume", name, *backend_args]),
                        2,
                    )
                self.assertEqual(sandbox_loader.called, should_load_sandbox)
                self.assertEqual(
                    mismatch_settings.require_calls,
                    1 if should_load_sandbox else 0,
                )
            self.assertEqual(
                (sessions / "local-session" / "execution.json").read_bytes(),
                local_binding,
            )
            self.assertEqual(
                (sessions / "remote-session" / "execution.json").read_bytes(),
                remote_binding,
            )

    def test_list_and_help_never_load_model_sandbox_or_runtime_factories(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            with SessionEventWorkflow(
                storage_directory=sessions / "remote",
                execution_backend_name="aliyun-sandbox",
            ):
                pass
            forbidden = {
                "load_settings": Mock(side_effect=AssertionError("model")),
                "load_default_execution_backend": Mock(
                    side_effect=AssertionError("default backend")
                ),
                "load_sandbox_settings": Mock(side_effect=AssertionError("sandbox")),
                "capture_workspace_seed": Mock(side_effect=AssertionError("seed")),
                "create_execution_runtime": Mock(side_effect=AssertionError("runtime")),
                "cleanup_recovered_remote_session": Mock(
                    side_effect=AssertionError("recovery cleanup")
                ),
                "create_client": Mock(side_effect=AssertionError("client")),
                "CodingAgent": Mock(side_effect=AssertionError("agent")),
            }
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.multiple(main_module, **forbidden),
                patch("sys.stdout", io.StringIO()),
            ):
                self.assertEqual(main_module.main(["--list-sessions"]), 0)
                with self.assertRaises(SystemExit) as caught:
                    main_module.main(["--help"])
                self.assertEqual(caught.exception.code, 0)
            for factory in forbidden.values():
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
