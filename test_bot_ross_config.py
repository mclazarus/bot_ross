"""Behavioral tests for bot_ross.py's C1 config surface: _require(),
setup_logging(), load_config(), main(), and the import-safe module-scope
defaults.

Two flavors of test here, for two different things that can only be proven
one way each:

- ImportSafetyTest runs bot_ross as a *subprocess* (`python -c "import
  bot_ross"` / `python bot_ross.py`) because nothing in-process can prove
  import-time behavior once the module is already imported into this test
  process -- the whole point is to observe a FRESH interpreter's environment
  reads, log handler installs, and filesystem writes (or lack of them).
- Everything else runs in-process against the already-imported `bot_ross`
  module, calling load_config()/_require() directly and asserting on the
  module globals they reassign.

Run from the repo root:  python -m unittest test_bot_ross_config -v
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import bot_ross

REPO = os.path.dirname(os.path.abspath(__file__))
BASE_ENV = {"OPENAI_API_KEY": "test-openai-key", "DISCORD_BOT_TOKEN": "test-discord-token"}

# The 14 globals load_config() reassigns, plus openai.api_key -- the legacy
# SDK's own auth global, which load_config also sets as a transitional
# side effect (see load_config's `# transitional` comment).
_CONFIG_GLOBALS = (
    "OPENAI_API_KEY", "DISCORD_BOT_TOKEN", "LIMIT", "IMAGE_MODEL", "IMAGE_MODERATION",
    "MEME_MODEL", "MAGIC_PAINT_RATE", "DRAIN_TIMEOUT", "BOT_TIMEZONE", "BOT_ZONE",
    "DAILY_IMAGE_ENABLED", "_raw_daily_channel", "DAILY_IMAGE_CHANNEL_ID",
    "DAILY_CHANNEL_MISCONFIGURED",
)


class ConfigMutationTestCase(unittest.TestCase):
    """Base class for any test that calls load_config() in-process.

    load_config() mutates process-wide module state (bot_ross's globals,
    plus the openai SDK's own `openai.api_key` global) -- without snapshot/
    restore, a leaked override from one test would silently change the
    "unconfigured import" defaults every OTHER test in this process relies
    on, in whatever order unittest happens to run them. setUp/tearDown make
    each test's mutation local to itself.
    """

    def setUp(self):
        self._snapshot = {name: getattr(bot_ross, name) for name in _CONFIG_GLOBALS}
        self._snapshot_openai_api_key = bot_ross.openai.api_key

    def tearDown(self):
        for name, value in self._snapshot.items():
            setattr(bot_ross, name, value)
        bot_ross.openai.api_key = self._snapshot_openai_api_key


class ImportSafetyTest(unittest.TestCase):
    """Subprocess tests: nothing in-process can prove import-time behavior,
    since this test process has already imported bot_ross once (at module
    load, above) -- these need a genuinely fresh interpreter."""

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_ctx.name

    def tearDown(self):
        self._tmpdir_ctx.cleanup()

    def test_import_with_no_environment_is_clean(self):
        # M1 -- the commit's headline acceptance test. returncode==0 proves
        # no env read raises with a fully empty environment (every config
        # global must have an import-safe default); the empty tmpdir proves
        # the os.makedirs/_seed_*() import-time filesystem writes are gone
        # (they write relative data/... paths, which would land in tmpdir,
        # our cwd for the subprocess).
        result = subprocess.run(
            [sys.executable, "-c", "import bot_ross"],
            cwd=self.tmpdir,
            env={"PYTHONPATH": REPO},
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            f"import bot_ross failed with an empty environment: {result.stderr.decode()}",
        )
        self.assertEqual(
            os.listdir(self.tmpdir), [],
            "import bot_ross wrote to the filesystem at import time -- the "
            "os.makedirs/_seed_*() calls must live inside main(), not at "
            "module scope",
        )

    def test_import_installs_no_root_log_handler(self):
        # M2 -- pins that logging.basicConfig moved out of import into
        # setup_logging(); a module-scope basicConfig() installs a root
        # handler that pollutes every test process that imports bot_ross.
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import bot_ross, logging, sys; "
                "sys.exit(1 if logging.getLogger().handlers else 0)",
            ],
            cwd=self.tmpdir,
            env={"PYTHONPATH": REPO},
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            f"import bot_ross installed a root log handler: {result.stderr.decode()}",
        )

    def test_missing_openai_api_key_exits_with_legible_message(self):
        # M3 -- proves the whole entry path: the __main__ guard fires,
        # main() calls load_config(), which raises a legible SystemExit
        # instead of a bare KeyError traceback.
        result = subprocess.run(
            [sys.executable, os.path.join(REPO, "bot_ross.py")],
            cwd=self.tmpdir,
            env={},
            capture_output=True,
            timeout=60,
        )
        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 1, f"stderr was: {stderr}")
        self.assertIn("OPENAI_API_KEY", stderr)
        self.assertNotIn(
            "KeyError", stderr,
            "a bare KeyError traceback means _require() isn't being used -- "
            "run.sh restarts this container forever, so each crash-loop "
            "iteration must print one legible line, not a stack trace",
        )

    def test_missing_discord_token_exits_with_legible_message(self):
        # M4 -- proves secrets are checked individually, each named on its
        # own failure, not just "something is missing".
        result = subprocess.run(
            [sys.executable, os.path.join(REPO, "bot_ross.py")],
            cwd=self.tmpdir,
            env={"OPENAI_API_KEY": "k"},
            capture_output=True,
            timeout=60,
        )
        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 1, f"stderr was: {stderr}")
        self.assertIn("DISCORD_BOT_TOKEN", stderr)


class LoadConfigDefaultsTest(unittest.TestCase):
    """M5: the module's own import-safe defaults, asserted directly against
    the already-imported bot_ross module -- no load_config() call involved.
    These are literals (see bot_ross.py's defaults block), so this is
    meaningful regardless of the developer's own real environment; it is
    NOT a test that load_config() was never called in this process (other
    tests do call it, but always restore via ConfigMutationTestCase)."""

    def test_module_defaults_match_current_fallbacks(self):
        self.assertIsNone(bot_ross.OPENAI_API_KEY)
        self.assertIsNone(bot_ross.DISCORD_BOT_TOKEN)
        self.assertEqual(bot_ross.LIMIT, 100)
        self.assertEqual(bot_ross.IMAGE_MODEL, "gpt-image-2-low")
        self.assertEqual(bot_ross.IMAGE_MODERATION, "low")
        self.assertEqual(bot_ross.MEME_MODEL, "gpt-5.4-mini")
        self.assertEqual(bot_ross.MAGIC_PAINT_RATE, 0.05)
        self.assertEqual(bot_ross.DRAIN_TIMEOUT, 300.0)
        self.assertEqual(bot_ross.BOT_TIMEZONE, "America/New_York")
        self.assertEqual(str(bot_ross.BOT_ZONE), "America/New_York")
        self.assertIs(bot_ross.DAILY_IMAGE_ENABLED, True)
        self.assertIsNone(bot_ross._raw_daily_channel)
        self.assertIsNone(bot_ross.DAILY_IMAGE_CHANNEL_ID)
        self.assertIs(bot_ross.DAILY_CHANNEL_MISCONFIGURED, False)


class LoadConfigParsingTest(ConfigMutationTestCase):
    """M6-M16: load_config()'s parsing behavior, in-process."""

    def test_minimal_env_matches_unconfigured_defaults(self):
        # M6 -- the "defaults equal the fallbacks" invariant: a minimal env
        # (just the two required secrets) must reproduce every other
        # global's M5 default exactly.
        bot_ross.load_config(dict(BASE_ENV))
        self.assertEqual(bot_ross.OPENAI_API_KEY, "test-openai-key")
        self.assertEqual(bot_ross.DISCORD_BOT_TOKEN, "test-discord-token")
        self.assertEqual(bot_ross.LIMIT, 100)
        self.assertEqual(bot_ross.IMAGE_MODEL, "gpt-image-2-low")
        self.assertEqual(bot_ross.IMAGE_MODERATION, "low")
        self.assertEqual(bot_ross.MEME_MODEL, "gpt-5.4-mini")
        self.assertEqual(bot_ross.MAGIC_PAINT_RATE, 0.05)
        self.assertEqual(bot_ross.DRAIN_TIMEOUT, 300.0)
        self.assertEqual(bot_ross.BOT_TIMEZONE, "America/New_York")
        self.assertEqual(str(bot_ross.BOT_ZONE), "America/New_York")
        self.assertIs(bot_ross.DAILY_IMAGE_ENABLED, True)
        self.assertIsNone(bot_ross._raw_daily_channel)
        self.assertIsNone(bot_ross.DAILY_IMAGE_CHANNEL_ID)
        self.assertIs(bot_ross.DAILY_CHANNEL_MISCONFIGURED, False)

    def test_openai_api_key_sdk_global_is_kept_in_sync(self):
        # M7 -- get_meme_prompt still authenticates via the v0.27 SDK global
        # until C4; forgetting this assignment doesn't degrade &meme, it
        # kills it outright (auth raises before the two-black-cats fallback
        # is ever reached).
        bot_ross.load_config(dict(BASE_ENV))
        self.assertEqual(bot_ross.openai.api_key, "test-openai-key")

    def test_non_secret_values_are_parsed(self):
        # M8
        bot_ross.load_config({
            **BASE_ENV,
            "API_LIMIT": "25",
            "IMAGE_MODEL": "gpt-image-2",
            "IMAGE_MODERATION": "auto",
            "MEME_MODEL": "other-model",
            "MAGIC_PAINT_RATE": "0.5",
            "DRAIN_TIMEOUT": "10",
        })
        self.assertEqual(bot_ross.LIMIT, 25)
        self.assertIsInstance(bot_ross.LIMIT, int)
        self.assertEqual(bot_ross.IMAGE_MODEL, "gpt-image-2")
        self.assertEqual(bot_ross.IMAGE_MODERATION, "auto")
        self.assertEqual(bot_ross.MEME_MODEL, "other-model")
        self.assertEqual(bot_ross.MAGIC_PAINT_RATE, 0.5)
        self.assertEqual(bot_ross.DRAIN_TIMEOUT, 10.0)

    def test_bad_magic_paint_rate_falls_back_to_default(self):
        # M9
        for value in ("abc", "2.0", "-0.1"):
            with self.subTest(value=value):
                bot_ross.load_config({**BASE_ENV, "MAGIC_PAINT_RATE": value})
                self.assertEqual(bot_ross.MAGIC_PAINT_RATE, 0.05)

    def test_bad_drain_timeout_falls_back_to_default(self):
        # M10
        for value in ("abc", "-5"):
            with self.subTest(value=value):
                bot_ross.load_config({**BASE_ENV, "DRAIN_TIMEOUT": value})
                self.assertEqual(bot_ross.DRAIN_TIMEOUT, 300.0)

    def test_bad_timezone_falls_back_to_utc_and_warns(self):
        # M11 -- proves the warning survived the move into load_config()
        # (the reason get_zone() returns the error string instead of
        # raising).
        with self.assertLogs("bot_ross", level="WARNING") as cm:
            bot_ross.load_config({**BASE_ENV, "BOT_TIMEZONE": "Not/AZone"})
        self.assertEqual(str(bot_ross.BOT_ZONE), "UTC")
        self.assertEqual(bot_ross.BOT_TIMEZONE, "Not/AZone")
        self.assertTrue(
            any("BOT_TIMEZONE problem, falling back to UTC" in line for line in cm.output),
            cm.output,
        )

    def test_daily_image_channel_id_parses_mention_syntax(self):
        # M12
        bot_ross.load_config({**BASE_ENV, "DAILY_IMAGE_CHANNEL_ID": "<#12345>"})
        self.assertEqual(bot_ross.DAILY_IMAGE_CHANNEL_ID, 12345)
        self.assertIs(bot_ross.DAILY_CHANNEL_MISCONFIGURED, False)

    def test_daily_image_channel_id_with_inline_comment_is_misconfigured(self):
        # M13 -- the direct behavioral pin on _raw_daily_channel being in
        # load_config's `global` list: reading it as a bot_ross MODULE
        # ATTRIBUTE can only see the global, never a function local, so if
        # _raw_daily_channel were omitted from the `global` statement this
        # assertion would see the module's unchanged default (None) instead
        # of the raw value -- this is the production day-of-lost-daily-
        # images bug, pinned directly.
        bot_ross.load_config({**BASE_ENV, "DAILY_IMAGE_CHANNEL_ID": "12345 # prod channel"})
        self.assertIsNone(bot_ross.DAILY_IMAGE_CHANNEL_ID)
        self.assertIs(bot_ross.DAILY_CHANNEL_MISCONFIGURED, True)
        self.assertEqual(bot_ross._raw_daily_channel, "12345 # prod channel")

    def test_daily_image_enabled_inline_comment_warns(self):
        # M14 -- deliberately not API_LIMIT: its bare int() parse crashes on
        # an inline comment today (a preserved behavior), so it can't
        # demonstrate the lenient-parse-plus-warning path the way a
        # parse_bool-backed field can.
        with self.assertLogs("bot_ross", level="WARNING") as cm:
            bot_ross.load_config({**BASE_ENV, "DAILY_IMAGE_ENABLED": "true # yes"})
        self.assertIs(bot_ross.DAILY_IMAGE_ENABLED, True)
        self.assertTrue(
            any("DAILY_IMAGE_ENABLED" in line and "inline" in line for line in cm.output),
            cm.output,
        )

    def test_missing_secrets_raise_systemexit_naming_the_variable(self):
        # M15
        with self.assertRaises(SystemExit) as cm:
            bot_ross.load_config({"DISCORD_BOT_TOKEN": "t"})
        self.assertIn("OPENAI_API_KEY", str(cm.exception))

        with self.assertRaises(SystemExit) as cm:
            bot_ross.load_config({"OPENAI_API_KEY": "k"})
        self.assertIn("DISCORD_BOT_TOKEN", str(cm.exception))

        with self.assertRaises(SystemExit) as cm:
            bot_ross.load_config({})
        # OPENAI_API_KEY is checked first, matching today's line order.
        self.assertIn("OPENAI_API_KEY", str(cm.exception))

        with self.assertRaises(SystemExit) as cm:
            bot_ross.load_config({"OPENAI_API_KEY": "", "DISCORD_BOT_TOKEN": "t"})
        # The `docker --env-file` `NAME=` empty-string case: empty counts
        # as missing too.
        self.assertIn("OPENAI_API_KEY", str(cm.exception))

    def test_env_none_defaults_to_os_environ(self):
        # M16
        with mock.patch.dict(os.environ, {**BASE_ENV, "API_LIMIT": "7"}, clear=True):
            bot_ross.load_config()
        self.assertEqual(bot_ross.LIMIT, 7)


if __name__ == "__main__":
    unittest.main()
