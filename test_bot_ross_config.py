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

import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import bot_ross

REPO = os.path.dirname(os.path.abspath(__file__))
BASE_ENV = {"OPENAI_API_KEY": "test-openai-key", "DISCORD_BOT_TOKEN": "test-discord-token"}

# The 14 globals load_config() reassigns.
_CONFIG_GLOBALS = (
    "OPENAI_API_KEY", "DISCORD_BOT_TOKEN", "LIMIT", "IMAGE_MODEL", "IMAGE_MODERATION",
    "MEME_MODEL", "MAGIC_PAINT_RATE", "DRAIN_TIMEOUT", "BOT_TIMEZONE", "BOT_ZONE",
    "DAILY_IMAGE_ENABLED", "_raw_daily_channel", "DAILY_IMAGE_CHANNEL_ID",
    "DAILY_CHANNEL_MISCONFIGURED",
)


def _record(level, msg="hello", exc_info=None):
    # A minimal LogRecord; name/pathname/lineno are arbitrary but fixed --
    # only levelno/msg/exc_info vary across the tests that use this.
    return logging.LogRecord("bot_ross", level, __file__, 1, msg, (), exc_info)


class _TtyStringIO(io.StringIO):
    """A StringIO that claims to be a real terminal -- the only way to drive
    the color path in-process, since a real tty isn't available under test."""

    def isatty(self):
        return True


class ConfigMutationTestCase(unittest.TestCase):
    """Base class for any test that calls load_config() in-process.

    load_config() mutates process-wide module state (bot_ross's globals) --
    without snapshot/restore, a leaked override from one test would silently
    change the "unconfigured import" defaults every OTHER test in this
    process relies on, in whatever order unittest happens to run them.
    setUp/tearDown make each test's mutation local to itself.
    """

    def setUp(self):
        self._snapshot = {name: getattr(bot_ross, name) for name in _CONFIG_GLOBALS}

    def tearDown(self):
        for name, value in self._snapshot.items():
            setattr(bot_ross, name, value)


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

    def test_coloredlogs_import_is_gone(self):
        # C5/16 -- before C6 rebuilt the .venv, coloredlogs was still
        # installed (leftover from the pre-refresh dependency set), so a
        # stale `import coloredlogs` left in bot_ross.py would still import
        # cleanly and pass every other test in this gate; this subprocess
        # check, inspecting sys.modules after import, was the only thing
        # that could catch it. Now that C6 has rebuilt the .venv (and the
        # Docker image) on 3.14, coloredlogs is no longer installed at all,
        # so the same stray import would make the subprocess exit non-zero
        # on ImportError -- this check still has teeth, just via a
        # different mechanism (the assertEqual(returncode, 0) below now
        # catches that too).
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import bot_ross, sys; "
                "sys.exit(1 if 'coloredlogs' in sys.modules else 0)",
            ],
            cwd=self.tmpdir,
            env={"PYTHONPATH": REPO},
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            f"coloredlogs was imported: {result.stderr.decode()}",
        )

    def test_default_setup_logging_to_a_pipe_emits_no_escape_codes(self):
        # C5/17 -- the only test exercising the default stream=None ->
        # sys.stderr path. A subprocess's stderr, captured through a pipe,
        # is a non-tty -- exactly how Docker captures the process.
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import bot_ross; bot_ross.setup_logging(); "
                "bot_ross.logger.warning('pipe smoke')",
            ],
            cwd=self.tmpdir,
            env={"PYTHONPATH": REPO},
            capture_output=True,
            timeout=60,
        )
        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 0, f"stderr was: {stderr}")
        self.assertIn("bot_ross[", stderr)
        self.assertIn("pipe smoke", stderr)
        self.assertNotIn("\x1b", stderr)


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
        self.assertEqual(bot_ross.IMAGE_MODEL, "gpt-image-2.5-flare-low")
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
        self.assertEqual(bot_ross.IMAGE_MODEL, "gpt-image-2.5-flare-low")
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

    # M7 (test_openai_api_key_sdk_global_is_kept_in_sync) retired in C4: its
    # subject, the legacy `openai.api_key` SDK global, no longer exists in
    # production -- C4 deleted `import openai` and get_meme_prompt now
    # authenticates via _fetch_meme_prompt's OPENAI_API_KEY closure, same as
    # fetch_image/fetch_image_edit. The property it protected -- that the
    # secret from load_config() actually reaches the fetchers -- is now
    # carried by test_minimal_env_matches_unconfigured_defaults's (M6)
    # `assertEqual(bot_ross.OPENAI_API_KEY, "test-openai-key")` above, plus
    # test_bot_ross_source.FetchFunctionsUseOpenAIKeyConstantTest, which pins
    # that the fetchers read the OPENAI_API_KEY constant by name.

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


class AnsiLevelFormatterTest(unittest.TestCase):
    """Pure formatter tests -- no I/O, no tmpdir, no root-logger state."""

    def test_non_color_output_has_no_escape_codes(self):
        # 1 -- ERROR on purpose: the *most* colorable level must still be
        # plain when color is off. This is the docker-logs guarantee at
        # formatter level.
        out = bot_ross.AnsiLevelFormatter(use_color=False).format(_record(logging.ERROR))
        self.assertNotIn("\x1b", out)

    def test_non_color_line_shape_keeps_milliseconds(self):
        # 2 -- pins the full line shape: date, comma-milliseconds (the
        # milliseconds=True coloredlogs was configured for), name[pid],
        # levelname, message -- and, via $, that nothing else crept in
        # (like the dropped hostname field).
        out = bot_ross.AnsiLevelFormatter(use_color=False).format(_record(logging.INFO))
        self.assertRegex(
            out, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} bot_ross\[\d+\] INFO hello$"
        )

    def test_info_renders_plain_even_in_color_mode(self):
        # 3 -- INFO is deliberately uncolored, mirroring coloredlogs' own
        # uncolored 'info' style. Same record object both times so asctime
        # is identical.
        record = _record(logging.INFO)
        self.assertEqual(
            bot_ross.AnsiLevelFormatter(use_color=True).format(record),
            bot_ross.AnsiLevelFormatter(use_color=False).format(record),
        )

    def test_each_colored_level_gets_its_exact_prefix(self):
        # 4
        cases = (
            (logging.DEBUG, "\x1b[32m"),
            (logging.WARNING, "\x1b[33m"),
            (logging.ERROR, "\x1b[31m"),
            (logging.CRITICAL, "\x1b[1;31m"),
        )
        for level, prefix in cases:
            with self.subTest(level=level):
                out = bot_ross.AnsiLevelFormatter(use_color=True).format(_record(level))
                self.assertTrue(out.startswith(prefix))
                self.assertTrue(out.endswith("\x1b[0m"))

    def test_color_wraps_but_never_alters_the_line(self):
        # 5 -- color is wrapping only: grep/alert tooling reading docker
        # logs and a human on a TTY must see the same text.
        record = _record(logging.WARNING)
        plain = bot_ross.AnsiLevelFormatter(use_color=False).format(record)
        colored = bot_ross.AnsiLevelFormatter(use_color=True).format(record)
        self.assertEqual(colored, "\x1b[33m" + plain + "\x1b[0m")

    def test_color_prefixes_are_pairwise_distinct(self):
        # 6 -- every colored level renders distinctly (and INFO is distinct
        # from all four by having no prefix at all; that's test 3).
        colors = bot_ross.AnsiLevelFormatter.LEVEL_COLORS.values()
        self.assertEqual(len(set(colors)), 4)
        self.assertNotIn("", colors)

    def test_unknown_levelno_renders_plain_not_keyerror(self):
        # 7 -- bad input must degrade to plain, never raise. 25 mirrors a
        # custom level a la coloredlogs' NOTICE.
        out = bot_ross.AnsiLevelFormatter(use_color=True).format(_record(25))
        self.assertNotIn("\x1b", out)
        self.assertIn("Level 25", out)

    def test_exception_block_wrapped_as_one_unit(self):
        # 8 -- the multi-line traceback is wrapped once as a block, not
        # reprocessed line-by-line (which would be subtly easy to get wrong).
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record(logging.ERROR, exc_info=sys.exc_info())
        out = bot_ross.AnsiLevelFormatter(use_color=True).format(record)
        self.assertTrue(out.startswith("\x1b[31m"))
        self.assertTrue(out.endswith("\x1b[0m"))
        self.assertEqual(out.count("\x1b[31m"), 1)
        self.assertEqual(out.count("\x1b[0m"), 1)
        self.assertIn("ValueError: boom", out)


class SetupLoggingTest(unittest.TestCase):
    """setup_logging() mutates the root logger -- a leaked handler holding a
    dead stream would swallow or duplicate log output for every other test
    in this process, in whatever order unittest runs them. Snapshot/restore
    on every test, the same reasoning as ConfigMutationTestCase."""

    def setUp(self):
        root = logging.getLogger()
        self._handlers = root.handlers[:]
        self._level = root.level

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:] = self._handlers
        root.setLevel(self._level)

    def test_non_tty_stream_gets_no_escape_codes(self):
        # 9 -- the production property: docker logs stay escape-free
        # end-to-end, not just at the formatter. Must log at a colored
        # level (WARNING, not INFO) -- INFO is never colored regardless of
        # use_color, so an INFO-only assertion can't distinguish "no color
        # because non-tty" from "no color because INFO is always plain"
        # and would pass even if TTY detection were deleted from the wiring.
        buf = io.StringIO()  # StringIO.isatty() really returns False
        bot_ross.setup_logging(stream=buf)
        logging.getLogger("bot_ross").warning("non tty check")
        output = buf.getvalue()
        self.assertNotIn("\x1b", output)
        self.assertRegex(output, r"bot_ross\[\d+\] WARNING non tty check\n$")

    def test_tty_stream_gets_colors(self):
        # 10
        tty = _TtyStringIO()
        bot_ross.setup_logging(stream=tty)
        logging.getLogger("bot_ross").warning("tty check")
        output = tty.getvalue()
        self.assertIn("\x1b[33m", output)
        self.assertIn("\x1b[0m", output)

    def test_root_wiring_matches_the_old_coloredlogs_end_state(self):
        # 11 -- coloredlogs put its handler on root and left the bot_ross
        # logger bare; drifting from that would double-print or orphan
        # discord.py's logs.
        buf = io.StringIO()
        bot_ross.setup_logging(stream=buf)
        root = logging.getLogger()
        self.assertEqual(len(root.handlers), 1)
        self.assertEqual(root.level, logging.INFO)
        self.assertIsInstance(root.handlers[0].formatter, bot_ross.AnsiLevelFormatter)
        self.assertIs(root.handlers[0].stream, buf)
        self.assertEqual(logging.getLogger("bot_ross").handlers, [])
        self.assertTrue(logging.getLogger("bot_ross").propagate)

    def test_calling_twice_installs_exactly_one_handler(self):
        # 12 -- failure mode: every line logged twice forever after any
        # second call.
        buf = io.StringIO()
        bot_ross.setup_logging(stream=buf)
        bot_ross.setup_logging(stream=buf)
        self.assertEqual(len(logging.getLogger().handlers), 1)

    def test_other_loggers_flow_through_the_root_handler(self):
        # 13 -- pins that the single root handler still serves
        # discord.py/aiohttp logs, as the coloredlogs root-replacement did;
        # losing those would blind the only observability the bot has.
        buf = io.StringIO()
        bot_ross.setup_logging(stream=buf)
        logging.getLogger("discord.client").info("gateway ok")
        self.assertIn("discord.client[", buf.getvalue())

    def test_debug_is_filtered_at_info(self):
        # 14 -- same net level filtering as coloredlogs.install(level='INFO').
        buf = io.StringIO()
        bot_ross.setup_logging(stream=buf)
        logging.getLogger("bot_ross").debug("hidden")
        self.assertEqual(buf.getvalue(), "")

    def test_isatty_failure_means_no_color_not_a_crash(self):
        # 15 -- a startup crash here would crash-loop under run.sh's
        # --restart=unless-stopped.
        class _RaisingIsatty:
            def isatty(self):
                raise ValueError("I/O operation on closed file")

        cases = (
            (_TtyStringIO(), True),
            (object(), False),
            (_RaisingIsatty(), False),
        )
        for stream, expected in cases:
            with self.subTest(stream=type(stream).__name__):
                self.assertEqual(bot_ross._stream_supports_color(stream), expected)


if __name__ == "__main__":
    unittest.main()
