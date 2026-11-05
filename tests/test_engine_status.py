"""Ollama readiness requires an actual /api/tags response, not merely HTTP 200."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import server


class OllamaStatusTests(unittest.IsolatedAsyncioTestCase):
    async def status_for(self, payload):
        response = SimpleNamespace(status=200, json=AsyncMock(return_value=payload))
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = response
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = session
        with patch.object(server.aiohttp, 'ClientSession', factory):
            return await server.get_ollama_status('http://selected-ollama:11434')

    async def test_valid_empty_model_list_is_online(self):
        self.assertEqual(await self.status_for({'models': []}), {'connected': True, 'models': []})

    async def test_wrong_service_or_malformed_payload_is_not_online(self):
        for payload in ({}, {'message': 'proxy ready'}, [], {'models': None},
                        {'models': {}}, {'models': [None]}, {'models': [{'size': 1}]},
                        {'models': [{'name': 123}]}):
            with self.subTest(payload=payload):
                self.assertEqual(await self.status_for(payload), {'connected': False, 'models': []})

    async def test_installed_models_are_reported_as_online(self):
        result = await self.status_for({'models': [{'name': 'review:latest', 'size': 1024,
                                                  'details': {'family': 'gemma'}}]})
        self.assertTrue(result['connected'])
        self.assertEqual(result['models'][0]['name'], 'review:latest')
        self.assertEqual(result['models'][0]['details']['family'], 'gemma')


if __name__ == '__main__':
    unittest.main()
