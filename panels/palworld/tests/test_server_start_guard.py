import unittest
from pathlib import Path

from panels.palworld.app import main


class ServerStartGuardTests(unittest.TestCase):
    def test_start_api_rejects_request_when_engine_is_not_installed(self):
        original_require_auth = main.require_auth
        original_install_check = main.has_any_official_runtime_install_marker
        main.require_auth = lambda request: "admin"
        main.has_any_official_runtime_install_marker = lambda: False

        try:
            with self.assertRaises(main.HTTPException) as context:
                main.start_server(request=None)
        finally:
            main.require_auth = original_require_auth
            main.has_any_official_runtime_install_marker = original_install_check

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(
            context.exception.detail,
            "아직 서버를 설치하지 않았으므로 서버를 기동할 수 없습니다.",
        )

    def test_dashboard_checks_install_status_before_start_request(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        start = source.index("    async function startServer()")
        end = source.index("    function setServerStopModalState", start)
        function_source = source[start:end]

        self.assertIn('fetch("/api/install/status")', function_source)
        self.assertIn("아직 서버를 설치하지 않았으므로 서버를 기동할 수 없습니다.", function_source)
        self.assertIn(
            'setServerStopModalState("unavailable", notInstalledMessage)',
            function_source,
        )
        self.assertNotIn("alert(notInstalledMessage)", function_source)
        self.assertLess(
            function_source.index('fetch("/api/install/status")'),
            function_source.index('fetch("/api/server/start"'),
        )


if __name__ == "__main__":
    unittest.main()
