from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agy_acp.context import ContextInspector, ContextSettings, PatchStatus, model_context_limit, resolve_plan
from agy_acp.preflight import PreflightError, prepare_home


class ContextPolicyTests(unittest.TestCase):
    def patch(self, state='unlocked'):
        return PatchStatus(state, 'test', '/test/checker.py')

    def test_default_is_512k(self):
        settings = ContextSettings.from_config({})
        plan = resolve_plan(settings, self.patch())
        self.assertEqual((settings.window, plan.tokens, plan.replay_bytes), ('512K', 512000, 1024000))

    def test_all_presets(self):
        for window, tokens in [('256K', 256000), ('512K', 512000), ('900K', 900000), ('Default', 983040)]:
            plan = resolve_plan(ContextSettings(window), self.patch())
            self.assertEqual(plan.tokens, tokens)
            self.assertEqual(plan.settings_value, '' if window == 'Default' else window)

    def test_unverified_large_windows_fail(self):
        for state in ('locked', 'unknown'):
            for window in ('512K', '900K'):
                with self.assertRaises(PreflightError):
                    resolve_plan(ContextSettings(window), self.patch(state))

    def test_small_and_auto_windows_fail_safe(self):
        for state in ('locked', 'unknown'):
            for window in ('256K', 'Default'):
                plan = resolve_plan(ContextSettings(window), self.patch(state))
                self.assertEqual((plan.tokens, plan.replay_bytes), (256000, 160000))

    def test_budget_validation(self):
        self.assertEqual(resolve_plan(ContextSettings(replay_bytes=600000), self.patch()).replay_bytes, 600000)
        for settings, state in [(ContextSettings(replay_bytes=1024001), 'unlocked'), (ContextSettings('256K', 160001), 'locked')]:
            with self.assertRaises(PreflightError):
                resolve_plan(settings, self.patch(state))

    def test_bad_config_is_rejected(self):
        for data in [{'context_window': '1M'}, {'context_window': None}, {'replay_budget_bytes': -1}, {'replay_budget_bytes': True}, {'replay_budget_bytes': '512000'}, {'replay_budget_bytes': 2200001}, {'patch_script': 'relative.py'}]:
            with self.assertRaises(PreflightError):
                ContextSettings.from_config(data)

    def test_model_capacity_bound(self):
        self.assertEqual(model_context_limit('gpt-oss-120b-medium', 512000), 128000)
        self.assertEqual(model_context_limit('gemini-3.8-flash-high', 512000), 512000)

    def test_settings_preserve_unrelated_keys_and_clear_default(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'isolated'
            host = Path(temp) / 'empty-host'
            for relative in ('agy/settings.json', 'antigravity-cli/settings.json'):
                path = home / relative
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'model': 'keep-me', 'useG1Credits': False, 'contextWindow': '256K'}))
            prepare_home(str(home), str(host), context_window='512K')
            for relative in ('agy/settings.json', 'antigravity-cli/settings.json'):
                data = json.loads((home / relative).read_text())
                self.assertEqual(data, {'model': 'keep-me', 'useG1Credits': False, 'contextWindow': '512K', 'toolPermission': 'request-review', 'agentMode': ''})
            prepare_home(str(home), str(host), context_window='')
            self.assertEqual(json.loads((home / 'agy/settings.json').read_text())['contextWindow'], '')


class InspectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.binary = self.root / 'binary'
        self.binary.write_bytes(b'test binary')
        self.script = self.root / 'check.py'
        self.inspector = ContextInspector()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def checker(self, state='unlocked', omit=None):
        lines = ["import sys", "assert '--dry-run' in sys.argv and '--no-env-fix' in sys.argv", "assert '--in-place' not in sys.argv"]
        for gate in ('1', '2', '3A', '3B', '4A', '4B'):
            if gate != omit:
                mark, message = ('=', 'already patched') if state == 'unlocked' else ('*', 'would patch')
                lines.append(f"print({f'[{mark}] [Gate {gate}] fixture: {message}'!r})")
        message = '[=] all gates already unlocked; nothing to write' if state == 'unlocked' else '[*] dry run complete: sha256 a -> b'
        lines.append(f'print({message!r})')
        self.script.write_text('\n'.join(lines) + '\n')

    async def inspect(self):
        return await self.inspector.inspect(str(self.binary), str(self.script))

    async def test_unlocked_is_read_only_and_cached(self):
        self.checker()
        before = self.binary.read_bytes()
        first = await self.inspect()
        self.assertEqual(first.state, 'unlocked')
        self.assertIs(first, await self.inspect())
        self.assertEqual(self.binary.read_bytes(), before)

    async def test_upgrade_invalidates_cache(self):
        self.checker()
        first = await self.inspect()
        self.binary.write_bytes(b'upgraded binary')
        second = await self.inspect()
        self.assertEqual(second.state, 'unlocked')
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    async def test_script_change_invalidates_cache(self):
        self.checker()
        self.assertEqual((await self.inspect()).state, 'unlocked')
        self.checker('locked')
        self.assertEqual((await self.inspect()).state, 'locked')

    async def test_missing_script_is_unknown(self):
        self.assertEqual((await self.inspect()).state, 'unknown')

    async def test_missing_gate_is_not_a_valid_unlock(self):
        self.checker(omit='4B')
        self.assertEqual((await self.inspect()).state, 'unknown')

    async def test_failed_checker_is_unknown(self):
        self.script.write_text('raise SystemExit("unsupported binary")\n')
        status = await self.inspect()
        self.assertEqual(status.state, 'unknown')
        self.assertIn('unsupported binary', status.detail)


if __name__ == '__main__':
    unittest.main(verbosity=2)
