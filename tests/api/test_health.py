from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from blackjack_ai.api.app import create_app
from blackjack_ai.config import Settings


class ServiceStatusEndpointTests(unittest.TestCase):
    def test_service_status_endpoints(self) -> None:
        app = create_app(
            Settings(
                environment="test",
                database_url="sqlite:///:memory:",
            )
        )

        with TestClient(app) as client:
            for path in ("/health", "/status"):
                with self.subTest(path=path):
                    response = client.get(path)

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json(),
                        {
                            "status": "ok",
                            "app_name": "Blackjack AI Service",
                            "environment": "test",
                            "database": {
                                "backend": "sqlite",
                                "connected": True,
                            },
                        },
                    )
