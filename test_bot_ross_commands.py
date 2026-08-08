"""Fake-Context harness and behavioral tests for bot_ross.py's 29 command callbacks
and key internals (do_the_art, _ChannelContext, _run_daily_slot,
_reject_while_draining).

Run from the repo root:  python -m unittest test_bot_ross_commands -v

This module makes ZERO production changes -- every other file in the repo is
byte-for-byte unchanged. It relies on C1's contract (bot_ross.py is importable, with
no environment, with no filesystem writes at import time -- see ImportSafetyTest,
which proves that contract directly).

Three hard rules the whole harness is built around:

  1. All SIX data/ path globals (DATA_FILE, MAGIC_PROMPTS_FILE, MACROS_FILE,
     DAILY_SCHEDULE_FILE, DAILY_STATE_FILE, DAILY_IMAGES_DIR) are patched into a
     tmpdir UNCONDITIONALLY by BotTestCase.setUp, every time, for every test class
     below. Missing even one would let a test write into the developer's real
     data/ directory -- gitignored, so the corruption would be invisible in
     `git status`.
  2. discord.File stays REAL (never faked) -- tests assert the actual filename and
     alt-text description that would reach Discord, not a stand-in's approximation
     of them.
  3. FakeChannel.send captures a sent file's bytes via `file.fp.getvalue()`, NEVER
     `.read()`. do_the_art rebuilds its BytesIO from scratch after a failed reply
     send, specifically because the first one was already consumed by that failed
     attempt -- a harness that itself consumed the buffer via .read() would either
     mask that rebuild (make it look unnecessary) or manufacture the very bug it
     exists to prevent (a second send of a valid File reading back empty).

fetch_image/fetch_image_edit are both looked up as bot_ross module globals AT CALL
TIME (never imported into another module's namespace), so patching them here is
sufficient to guarantee no test in this file ever touches aiohttp or the network --
see FakeImageAPI, installed unconditionally in BotTestCase.setUp.
"""

import base64
import importlib
import json
import os
import re
import struct
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from typing import NamedTuple
from unittest import mock

import discord

import bot_ross
import daily_schedule
import image_size
import macros
import magic_paint
import pipe_chain
import release_image

# --- Sentinels ------------------------------------------------------------------

# What the patched get_random_bob_ross_quote returns. get_random_bob_ross_quote()
# picks randomly from a function-local (unexported) list -- asserting equality
# against a real quote would be flaky, and exporting the list would itself be a
# production change (out of scope for this commit). Patching the function and
# asserting against this fixed sentinel sidesteps both problems.
QUOTE_SENTINEL = "TEST-QUOTE"

# The sole entry in the test magic library. The hyphenation is purely a
# readability convenience (it reads as a phrase, not a token) -- it is NOT
# evidence of anything about filename sanitization. generate_file_name() runs
# every prompt through re.sub(r'[^0-9a-zA-Z]', '_', prompt)[:50] (bot_ross.py),
# which turns MAGIC_TEXT into MAGIC_SENTINEL_TEXT before it can reach a
# filename. assertNoMagicLeak checks the filename against BOTH forms (see
# MAGIC_TEXT_SANITIZED below) precisely so that transformation can't hide a
# leak instead of proving one absent.
MAGIC_TEXT = "MAGIC-SENTINEL-TEXT"
MAGIC_LIBRARY = [{"id": "test-mixin", "text": MAGIC_TEXT}]

# generate_file_name()'s sanitized form of MAGIC_TEXT (bot_ross.py:2138's exact
# rule: re.sub(r'[^0-9a-zA-Z]', '_', prompt)). A filename can never carry the
# raw hyphenated MAGIC_TEXT, so checking only the raw form would be a
# tautology that always passes regardless of what the filename actually
# contains -- checking this sanitized form too is what makes the filename leg
# of assertNoMagicLeak capable of failing.
MAGIC_TEXT_SANITIZED = re.sub(r'[^0-9a-zA-Z]', '_', MAGIC_TEXT)

# Sentinel distinguishing "guild not given" (-> default FakeGuild(1111)) from an
# explicit guild=None (a DM/no-guild context) in FakeContext/BotTestCase.make_ctx.
_UNSET = object()


def _make_png(width, height):
    """A real, minimally-valid PNG byte string that image_size.png_dimensions
    genuinely parses back to (width, height) -- NOT a stub like b"fake". A fake
    payload would push every pipe-chain edit segment onto the AUTO size fallback,
    so the size-threading path (png_dimensions run on the previous segment's own
    output) would never actually execute under test."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def cmd(name):
    """bot_ross.bot.get_command(name).callback -- the module-level names
    (bot_ross.paint, bot_ross.daily_image_cmd, ...) are commands.Command objects,
    NOT coroutine functions; this is the only way to get a directly-drivable
    coroutine function. Note &daily_image's callback is named daily_image_cmd, so
    lookup has to go through the Discord-facing command name ("daily_image"),
    not the Python function name."""
    command = bot_ross.bot.get_command(name)
    if command is None:
        raise AssertionError(f"no such command registered: {name!r}")
    return command.callback


# --- Fake Discord objects --------------------------------------------------------

class FakePermissions:
    """Attribute bag mirroring the three permission flags
    _resolve_linked_images actually reads off channel.permissions_for(...)."""

    def __init__(self, view_channel=True, read_message_history=True, manage_threads=False):
        self.view_channel = view_channel
        self.read_message_history = read_message_history
        self.manage_threads = manage_threads


class FakeAuthor:
    def __init__(self, name, id):
        self.name = name
        self.id = id


class FakeGuild:
    def __init__(self, id):
        self.id = id


class FakeAttachment:
    def __init__(self, width=None, height=None, content_type="image/png",
                 filename="input.png", data=b""):
        self.width = width
        self.height = height
        self.content_type = content_type
        self.filename = filename
        self.data = data

    async def read(self):
        return self.data


_next_message_id = 1000


def _next_id():
    global _next_message_id
    _next_message_id += 1
    return _next_message_id


class FakeMessage:
    """The object FakeChannel.send returns, and also used as a reply-resolution
    fixture (ctx.message, or a linked/replied-to message's stand-in)."""

    def __init__(self, attachments=None, reference=None, guild=None):
        self.id = _next_id()
        self.attachments = list(attachments) if attachments else []
        self.reference = reference
        self.guild = guild


class SentMessage(NamedTuple):
    content: object
    file: object
    file_bytes: object
    filename: object
    description: object
    reference: object
    message: object


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, file=None, reference=None, **kwargs):
        record = SentMessage(
            content,
            file,
            file.fp.getvalue() if file is not None else None,  # NEVER .read() -- see module docstring rule 3
            file.filename if file is not None else None,
            file.description if file is not None else None,
            reference,
            FakeMessage(),
        )
        self.sent.append(record)
        return record.message

    @property
    def texts(self):
        return [s.content for s in self.sent if s.file is None and s.content is not None]

    @property
    def files(self):
        return [s for s in self.sent if s.file is not None]


class FailingOnceChannel(FakeChannel):
    """Raises `exception` on the very FIRST send() call that carries a reference=
    kwarg, recording that attempt (file identity included) before raising; every
    later call succeeds normally and is recorded too. Used for T51: proving
    do_the_art rebuilds its discord.File from scratch (rather than reusing the
    already-consumed BytesIO) on the retry."""

    def __init__(self, exception):
        super().__init__()
        self.exception = exception
        self.attempts = []
        self._raised_once = False

    async def send(self, content=None, *, file=None, reference=None, **kwargs):
        file_bytes = file.fp.getvalue() if file is not None else None
        filename = file.filename if file is not None else None
        description = file.description if file is not None else None
        if reference is not None and not self._raised_once:
            self._raised_once = True
            self.attempts.append(SentMessage(content, file, file_bytes, filename, description, reference, None))
            raise self.exception
        record = SentMessage(content, file, file_bytes, filename, description, reference, FakeMessage())
        self.attempts.append(record)
        self.sent.append(record)
        return record.message


class FakeContext:
    def __init__(self, channel=None, author=None, guild=_UNSET, message=None):
        self.channel = channel if channel is not None else FakeChannel()
        self.author = author if author is not None else FakeAuthor("tester", 1)
        self.guild = FakeGuild(1111) if guild is _UNSET else guild
        self.message = message if message is not None else FakeMessage()

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


class FakeLinkedChannel:
    """Installed via mock.patch.object(bot_ross.bot, "get_channel", lambda cid:
    fake). Not a discord.Thread, so isinstance(channel, discord.Thread) is False
    and the private-thread membership branch is never entered -- see the module
    docstring's non-goals note near RemixMessageLinkTest below."""

    def __init__(self, guild_id, perms=None, message=None, fetch_error=None):
        self.guild = types.SimpleNamespace(id=guild_id) if guild_id is not None else None
        self._perms = perms if perms is not None else FakePermissions()
        self._message = message
        self._fetch_error = fetch_error
        self.fetch_message_calls = 0

    def permissions_for(self, member):
        return self._perms

    async def fetch_message(self, message_id):
        # Incremented BEFORE any raise -- lets a test assert "the message's bytes
        # were never requested" (== 0) even on the error path.
        self.fetch_message_calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._message


class FakeImageAPI:
    """Patched over bot_ross.fetch_image / bot_ross.fetch_image_edit. Both are
    looked up as bot_ross module globals at call time, so patching here reaches
    every call site -- no aiohttp is ever touched by a test using this."""

    def __init__(self, png_bytes=None):
        self.png_bytes = png_bytes if png_bytes is not None else _make_png(1024, 1024)
        self.revised_prompt = None
        self.fail_on = set()
        self.exception = Exception("boom")
        self.calls = []

    @property
    def generate_calls(self):
        return [c for c in self.calls if c.kind == "generate"]

    @property
    def edit_calls(self):
        return [c for c in self.calls if c.kind == "edit"]

    @property
    def call_count(self):
        return len(self.calls)

    async def fetch_image(self, prompt, model, size=None):
        self.calls.append(types.SimpleNamespace(kind="generate", prompt=prompt, model=model, size=size, images=None))
        if len(self.calls) in self.fail_on:
            raise self.exception
        return {"image": base64.b64encode(self.png_bytes).decode(), "revised_prompt": self.revised_prompt}

    async def fetch_image_edit(self, prompt, model, images, size=None):
        self.calls.append(types.SimpleNamespace(kind="edit", prompt=prompt, model=model, size=size, images=list(images)))
        if len(self.calls) in self.fail_on:
            raise self.exception
        return {"image": base64.b64encode(self.png_bytes).decode(), "revised_prompt": None}


# --- BotTestCase ------------------------------------------------------------------

class BotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

        data_file = os.path.join(self.tmp, "request_data.json")
        magic_file = os.path.join(self.tmp, "magic_prompts.json")
        macros_file = os.path.join(self.tmp, "macros.json")
        schedule_file = os.path.join(self.tmp, "daily_schedule.json")
        state_file = os.path.join(self.tmp, "daily_state.json")
        images_dir = os.path.join(self.tmp, "daily_images")

        # Rule 1 (see module docstring): all SIX data/ path globals, unconditionally.
        self.enterContext(mock.patch.object(bot_ross, "DATA_FILE", data_file))
        self.enterContext(mock.patch.object(bot_ross, "MAGIC_PROMPTS_FILE", magic_file))
        self.enterContext(mock.patch.object(bot_ross, "MACROS_FILE", macros_file))
        self.enterContext(mock.patch.object(bot_ross, "DAILY_SCHEDULE_FILE", schedule_file))
        self.enterContext(mock.patch.object(bot_ross, "DAILY_STATE_FILE", state_file))
        self.enterContext(mock.patch.object(bot_ross, "DAILY_IMAGES_DIR", images_dir))
        os.makedirs(images_dir, exist_ok=True)

        with open(magic_file, "w") as f:
            json.dump(MAGIC_LIBRARY, f)
        with open(macros_file, "w") as f:
            json.dump([], f)

        # Reset the five process-global flags every test starts from a clean slate.
        self.enterContext(mock.patch.object(bot_ross, "active_requests", 0))
        self.enterContext(mock.patch.object(bot_ross, "draining", False))
        self.enterContext(mock.patch.object(bot_ross, "_signals_installed", False))
        self.enterContext(mock.patch.object(bot_ross, "_daily_task", None))
        self.enterContext(mock.patch.object(bot_ross, "_last_schedule_errors", []))

        # Determinism: pin the rate rather than seed random. random.random() < 0.0
        # is always False, < 1.0 is always True -- no RNG seeding needed anywhere.
        self.enterContext(mock.patch.object(bot_ross, "MAGIC_PAINT_RATE", 0.0))
        self.enterContext(mock.patch.object(bot_ross, "get_random_bob_ross_quote", lambda: QUOTE_SENTINEL))

        self.api = FakeImageAPI()
        self.enterContext(mock.patch.object(bot_ross, "fetch_image", self.api.fetch_image))
        self.enterContext(mock.patch.object(bot_ross, "fetch_image_edit", self.api.fetch_image_edit))

        # The real _retry_delay is `await asyncio.sleep(120)` -- one accidentally
        # -failing daily-scheduler test would otherwise hang the whole suite for
        # two minutes. Tests that need "retry suppressed" re-patch this to False.
        self.retry_delays = 0

        async def _stub_retry_delay():
            self.retry_delays += 1
            return True

        self.enterContext(mock.patch.object(bot_ross, "_retry_delay", _stub_retry_delay))

    def tearDown(self):
        # Free leak detector: proves every do_the_art/_piped bracket released its
        # count on every code path this test exercised.
        self.assertEqual(bot_ross.active_requests, 0, "active_requests leaked across a test")

    def make_ctx(self, *, guild=_UNSET, attachments=(), reference=None, author_name="tester"):
        message = FakeMessage(attachments=attachments, reference=reference)
        author = FakeAuthor(author_name, 1)
        if guild is _UNSET:
            return FakeContext(author=author, message=message)
        return FakeContext(author=author, message=message, guild=guild)

    def read_data(self):
        if not os.path.exists(bot_ross.DATA_FILE):
            return {}
        with open(bot_ross.DATA_FILE) as f:
            return json.load(f)

    def assertNoMagicLeak(self, channel, skip_description=(), skip_filename=()):
        """No sent content anywhere, no filename outside `skip_filename`, and no
        description outside `skip_description`, contains MAGIC_TEXT (filenames
        are checked against MAGIC_TEXT_SANITIZED too -- see its own comment for
        why the raw form alone would be a tautology).

        Content must NEVER leak it: it is only ever set explicitly (the bare
        "printf" tell), so no legitimate exception exists for it. Filename and
        description are different: do_the_art's NON-quiet path (any first/only
        segment -- see CLAUDE.md's do_the_art section) sets BOTH the file's name
        (generate_file_name(prompt)) and the attachment's alt-text description to
        the full prompt whenever the model has no revised_prompt, which is true
        for every gpt-image-2 config this bot uses. That is PRE-EXISTING,
        non-pipe-specific behavior -- it already applies to a magic-free prompt
        too (see PipeChainTest.test_no_pipe_prompt_is_a_plain_single_generation's
        description == prompt and filename-prefix assertions) -- so a
        magic-appended prompt legitimately, unavoidably carries MAGIC_TEXT in
        BOTH of those fields for that one non-quiet segment. Confirmed
        empirically against the real (unmodified) do_the_art before writing this
        helper: `&paint a fox` at rate 1.0 posts a file literally named
        `a_fox_MAGIC_SENTINEL_TEXT_<rand>.png` with description 'a fox
        MAGIC-SENTINEL-TEXT'. `skip_filename`/`skip_description` name the
        SentMessage(s) whose filename/description is expected to carry it for
        that reason -- callers must pass the SAME segment-1 SentMessage to both,
        since both fields leak together on that path (this is a pre-existing
        production secrecy hole in the NON-quiet path, flagged separately; it is
        not something this test harness can fix). Everywhere else -- every QUIET
        pipe-segment reveal, which is the actual secrecy mechanism this helper
        exists to protect -- content, filename, AND description must all stay
        clean.
        """
        for msg in channel.sent:
            if msg.content:
                self.assertNotIn(MAGIC_TEXT, msg.content)
            if msg.filename and msg not in skip_filename:
                self.assertNotIn(MAGIC_TEXT, msg.filename)
                self.assertNotIn(MAGIC_TEXT_SANITIZED, msg.filename)
            if msg.description and msg not in skip_description:
                self.assertNotIn(MAGIC_TEXT, msg.description)


# =========================================================================== #
# A. Import safety
# =========================================================================== #

def _fresh_import(tmpdir, env=None):
    """Import a FRESH copy of bot_ross with `env` (default {}) as os.environ and
    `tmpdir` as cwd. Restores the real cwd and re-registers the ORIGINAL bot_ross
    module object into sys.modules in a `finally` -- leaving the fresh module
    registered would make every later test in this process patch one module
    object while the bot's already-bound command callbacks dispatch through
    another."""
    if env is None:
        env = {}
    original_module = sys.modules.get("bot_ross")
    original_cwd = os.getcwd()
    sys.modules.pop("bot_ross", None)
    try:
        with mock.patch.dict(os.environ, env, clear=True):
            os.chdir(tmpdir)
            return importlib.import_module("bot_ross")
    finally:
        os.chdir(original_cwd)
        if original_module is not None:
            sys.modules["bot_ross"] = original_module
        else:
            sys.modules.pop("bot_ross", None)


class ImportSafetyTest(unittest.TestCase):
    """NOT a BotTestCase -- proving import safety must not depend on the harness
    that is itself only meaningful once import safety already holds."""

    def test_bot_ross_imports_cleanly_with_no_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = _fresh_import(tmp)
            # Proves the C1 goal directly: no data/ creation, no seeding, no
            # state file written merely by `import bot_ross`.
            self.assertEqual(os.listdir(tmp), [])
            # Proves import-time command registration survived C1's refactor.
            self.assertIsNotNone(mod.bot.get_command("paint"))

    def test_fresh_import_defaults_are_the_documented_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = _fresh_import(tmp)
            self.assertEqual(mod.LIMIT, 100)
            self.assertEqual(mod.IMAGE_MODEL, "gpt-image-2-low")
            self.assertEqual(mod.MAGIC_PAINT_RATE, 0.05)
            self.assertEqual(mod.DRAIN_TIMEOUT, 300.0)
            self.assertIs(mod.DAILY_IMAGE_ENABLED, True)
            self.assertIsNone(mod.DAILY_IMAGE_CHANNEL_ID)
            self.assertIs(mod.DAILY_CHANNEL_MISCONFIGURED, False)
            self.assertIsNone(mod.DISCORD_BOT_TOKEN)
            self.assertIsNone(mod.OPENAI_API_KEY)

    def test_misconfigured_channel_is_distinguished_from_unset(self):
        # DEVIATION from the literal spec: as written, this test asks for the
        # misconfigured-vs-unset distinction to be visible from a bare `import
        # bot_ross` with the env var merely set. Under C1's finished design, ALL
        # env parsing lives inside load_config() (called only from main()) --
        # never at module scope -- so a bare import always sees the same
        # import-safe defaults regardless of os.environ (see the test above).
        # Proving the regression therefore requires calling load_config()
        # explicitly, exactly like test_bot_ross_config.py's
        # test_daily_image_channel_id_with_inline_comment_is_misconfigured
        # already does against the main already-imported module. This test adds
        # the one thing that one doesn't cover: the same behavior holds on a
        # module obtained via _fresh_import, proving the regression can't hide
        # in a module that has never been configured before.
        with tempfile.TemporaryDirectory() as tmp:
            mod = _fresh_import(tmp)
            mod.load_config({
                "OPENAI_API_KEY": "k", "DISCORD_BOT_TOKEN": "t",
                "DAILY_IMAGE_CHANNEL_ID": "not-a-number",
            })
            self.assertIsNone(mod.DAILY_IMAGE_CHANNEL_ID)
            self.assertIs(mod.DAILY_CHANNEL_MISCONFIGURED, True)


class OpenAISDKNotImportedTest(unittest.TestCase):
    """T9 (C4): guards a lurking direct/transitive `import openai` anywhere in
    the import graph. Before C6 rebuilt the venv, openai was still installed
    (leftover from the pre-refresh dependency set), so this sys.modules check
    was the only thing that could catch a stray import -- the module would
    still import cleanly either way. Now that C6 has rebuilt the .venv (and the
    Docker image) on 3.14, openai is no longer installed at all, so a stray
    `import openai` left anywhere in the graph raises ImportError and fails
    the module import outright; this test still has teeth, just via a
    different mechanism -- it now also documents that sys.modules can't even
    contain the key. NOT a BotTestCase: this is a property of the module
    already imported at the top of this file, not something that needs the
    data/-redirecting harness."""

    def test_openai_is_not_in_sys_modules(self):
        self.assertNotIn("openai", sys.modules)


# =========================================================================== #
# B. Harness self-checks
# =========================================================================== #

class HarnessSelfCheckTest(BotTestCase):
    def test_all_six_data_paths_point_into_the_tmpdir(self):
        for name in ("DATA_FILE", "MAGIC_PROMPTS_FILE", "MACROS_FILE",
                     "DAILY_SCHEDULE_FILE", "DAILY_STATE_FILE", "DAILY_IMAGES_DIR"):
            with self.subTest(name=name):
                self.assertTrue(getattr(bot_ross, name).startswith(self.tmp))

    def test_make_png_roundtrips_through_png_dimensions(self):
        self.assertEqual(image_size.png_dimensions(_make_png(320, 208)), (320, 208))
        self.assertIsNone(image_size.png_dimensions(b"not a png"))


# =========================================================================== #
# C. Pipe chains
# =========================================================================== #

class PipeChainTest(BotTestCase):
    async def test_five_pipes_refused_with_exactly_one_message(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a|b|c|d|e|f")
        self.assertEqual(ctx.channel.texts, [pipe_chain.TOO_MANY_MESSAGE])
        self.assertEqual(self.api.call_count, 0)
        self.assertEqual(self.read_data(), {})

    async def test_all_empty_segments_refused(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="| |")
        self.assertEqual(ctx.channel.texts, ["...I need something to paint besides the pipes."])
        self.assertEqual(self.api.call_count, 0)

    async def test_dropped_segment_note_then_chain_completes(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a | | b")
        texts = ctx.channel.texts
        self.assertEqual(texts[0], pipe_chain.dropped_note(1))
        self.assertEqual(texts[-1], pipe_chain.chain_complete_message(2))
        self.assertEqual([c.kind for c in self.api.calls], ["generate", "edit"])
        data = self.read_data()
        self.assertEqual(data.get("pipes"), 1)
        self.assertEqual(data.get("pipe_segments"), 2)

    async def test_no_pipe_prompt_is_a_plain_single_generation(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a lighthouse")
        texts = ctx.channel.texts
        self.assertEqual(texts[0], QUOTE_SENTINEL)
        self.assertTrue(texts[1].startswith("Generated in "))
        self.assertTrue(texts[1].endswith(" | Monthly requests: 1"))
        files = ctx.channel.files
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].filename.startswith("a_lighthouse_"))
        self.assertTrue(files[0].filename.endswith(".png"))
        self.assertEqual(files[0].description, "a lighthouse")
        self.assertEqual(files[0].file_bytes, self.api.png_bytes)
        self.assertEqual(self.api.generate_calls[0].size, "1024x1024")
        self.assertEqual(self.api.generate_calls[0].prompt, "a lighthouse")
        self.assertEqual(self.read_data().get(bot_ross.get_current_month()), 1)

    async def test_edit_segment_size_comes_from_previous_segment_png(self):
        self.api.png_bytes = _make_png(1536, 1024)
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a fox | make it night")
        self.assertEqual(self.api.edit_calls[0].size, "1536x1024")
        self.assertEqual(self.api.edit_calls[0].images, [(self.api.png_bytes, "image/png")])

    async def test_chain_stops_at_failing_step_with_honest_numbering(self):
        self.api.fail_on = {2}
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a | b")
        texts = ctx.channel.texts
        self.assertEqual(
            texts[-2:],
            ["No painting this time, exception for this request: boom",
             pipe_chain.chain_stopped_message(2, 2)],
        )
        data = self.read_data()
        self.assertEqual(data.get("pipes", 0), 0)
        self.assertEqual(data.get("pipe_segments"), 1)
        self.assertEqual(self.api.call_count, 2)

    async def test_every_chained_image_replies_to_the_first_image(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a | b | c")
        files = ctx.channel.files
        self.assertEqual(len(files), 3)
        self.assertIsNone(files[0].reference)
        self.assertIs(files[1].reference, files[0].message)
        self.assertIs(files[2].reference, files[0].message)

    async def test_quiet_segment_magic_tell_without_prompt_leak(self):
        self.enterContext(mock.patch.object(bot_ross, "MAGIC_PAINT_RATE", 1.0))
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a | b")
        files = ctx.channel.files
        self.assertEqual(len(files), 2)
        seg2 = files[1]
        self.assertEqual(seg2.content, "\U0001f58c️")
        self.assertTrue(seg2.filename.startswith("painting_"))
        self.assertIsNone(seg2.description)
        self.assertTrue(self.api.generate_calls[0].prompt.endswith(" " + MAGIC_TEXT))
        self.assertTrue(self.api.edit_calls[0].prompt.endswith(" " + MAGIC_TEXT))
        # See assertNoMagicLeak's own docstring for exactly why segment 1's file
        # is excluded from BOTH legs here: do_the_art's non-quiet path puts the
        # full (magic-included) prompt into both the filename and the alt-text
        # description -- pre-existing, non-pipe-specific behavior, confirmed
        # empirically against unmodified bot_ross.py, not something this test
        # can fix. Segment 2 (the quiet pipe segment) is NOT excluded from
        # either leg -- it is the actual secrecy mechanism under test here.
        self.assertNoMagicLeak(ctx.channel, skip_description={files[0]}, skip_filename={files[0]})
        self.assertEqual(self.read_data().get("magic"), 2)

    async def test_flags_only_segment_uses_the_creative_fallback(self):
        ctx = self.make_ctx()
        await cmd("hpaint")(ctx, prompt="a | --portrait")
        self.assertEqual(self.api.edit_calls[0].prompt, "creatively reinterpret this image")
        self.assertEqual(self.api.edit_calls[0].size, "1024x1536")


# =========================================================================== #
# D. Generation size flags
# =========================================================================== #

class SizeFlagTest(BotTestCase):
    async def test_invalid_res_refused_before_any_api_call(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a fox --res banana")
        self.assertEqual(
            ctx.channel.texts,
            ["`--res banana` isn't a size I understand — use `WIDTHxHEIGHT`, e.g. `--res 1920x1080`."],
        )
        self.assertEqual(self.api.call_count, 0)

    async def test_flags_only_prompt_refused(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="--landscape")
        self.assertEqual(ctx.channel.texts, ["...I need something to paint besides the size flags."])
        self.assertEqual(self.api.call_count, 0)

    async def test_res_below_pixel_floor_is_coerced_with_notice(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a fox --res 100x100")
        self.assertIn(
            "Using `816x816` (adjusted from `100x100` to fit the size limits).",
            ctx.channel.texts,
        )
        self.assertEqual(self.api.generate_calls[0].size, "816x816")

    async def test_res_overrides_orientation_and_coerces(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="a fox --res 1920x1080 --landscape")
        texts = ctx.channel.texts
        i_override = texts.index("(`--res` overrides `--landscape`)")
        i_coerce = texts.index("Using `1920x1088` (adjusted from `1920x1080` to fit the size limits).")
        self.assertLess(i_override, i_coerce)
        self.assertEqual(self.api.generate_calls[0].size, "1920x1088")


# =========================================================================== #
# E. &remix core
# =========================================================================== #

class RemixTest(BotTestCase):
    async def test_no_image_no_prompt_refused(self):
        ctx = self.make_ctx()
        await cmd("remix")(ctx)
        self.assertEqual(
            ctx.channel.texts,
            ["We need a happy little image to work with before we can remix anything. "
             "Attach one, or reply to a message that has one!"],
        )
        self.assertEqual(self.api.call_count, 0)

    async def test_attachment_drives_edit_size(self):
        att = FakeAttachment(800, 600, data=_make_png(800, 600))
        ctx = self.make_ctx(attachments=[att])
        await cmd("remix")(ctx, prompt="oilify")
        expected_size = image_size.coerce_generation_size(800, 600)
        self.assertEqual(expected_size, "960x720")
        self.assertEqual(self.api.edit_calls[0].size, expected_size)
        self.assertEqual(self.api.edit_calls[0].images, [(att.data, "image/png")])
        self.assertEqual(self.api.edit_calls[0].prompt, "oilify")
        self.assertEqual(self.read_data().get("remixes"), 1)

    async def test_non_image_attachment_skipped_then_generation_fallback(self):
        att = FakeAttachment(content_type="text/plain")
        ctx = self.make_ctx(attachments=[att])
        await cmd("remix")(ctx, prompt="a fox")
        self.assertEqual(ctx.channel.texts[0], "Skipping 1 attachment(s) that aren't images.")
        self.assertEqual([c.kind for c in self.api.calls], ["generate"])

    async def test_reply_image_is_remixed(self):
        image_attachment = FakeAttachment(640, 480, data=_make_png(640, 480))
        resolved_message = FakeMessage(attachments=[image_attachment])

        class _FetchableChannel(FakeChannel):
            async def fetch_message(self, message_id):
                return resolved_message

        channel = _FetchableChannel()
        ref = types.SimpleNamespace(resolved=None, message_id=999)
        ctx = FakeContext(channel=channel, message=FakeMessage(attachments=[], reference=ref))
        await cmd("remix")(ctx, prompt="oilify")
        self.assertEqual(self.api.edit_calls[0].images, [(image_attachment.data, "image/png")])

    async def test_image_only_remix_is_always_magic(self):
        att = FakeAttachment(800, 600, data=_make_png(800, 600))
        ctx = self.make_ctx(attachments=[att])
        await cmd("remix")(ctx)
        self.assertEqual(ctx.channel.texts[0], QUOTE_SENTINEL + " \U0001f58c️")
        self.assertEqual(self.api.edit_calls[0].prompt, "creatively reinterpret this image " + MAGIC_TEXT)
        self.assertEqual(self.read_data().get("magic"), 1)


# =========================================================================== #
# F. &remix message links -- the exfiltration boundary
# =========================================================================== #

class RemixMessageLinkTest(BotTestCase):
    # NOTE (non-goal, matching the spec): private-thread membership requires a
    # real isinstance(channel, discord.Thread); forging that via object.__new__
    # would couple this suite to discord.py internals across the
    # 2.3.2->2.7.1 bump. The pure decision function is covered directly in
    # test_message_links.py; the surrounding skip/bucket behavior is what T23/T24
    # below cover instead.
    LINK = "https://discord.com/channels/1111/222/333"

    def _install_channel(self, fake):
        self.enterContext(mock.patch.object(bot_ross.bot, "get_channel", lambda cid: fake))

    async def test_link_requester_cannot_read_is_skipped_and_never_fetched(self):
        for perms in (FakePermissions(view_channel=False), FakePermissions(read_message_history=False)):
            with self.subTest(perms=vars(perms)):
                fake = FakeLinkedChannel(1111, perms=perms)
                self._install_channel(fake)
                ctx = self.make_ctx()
                await cmd("remix")(ctx, prompt=self.LINK)
                self.assertEqual(fake.fetch_message_calls, 0)
                self.assertIn(
                    "Skipping 1 message link(s) I couldn't fetch — the message may be "
                    "gone, or you may not have access to that channel.",
                    ctx.channel.texts,
                )
                self.assertIn(
                    "We need a happy little image to work with before we can remix anything. "
                    "Attach one, or reply to a message that has one!",
                    ctx.channel.texts,
                )
                self.assertEqual(self.api.call_count, 0)

    async def test_resolved_cross_guild_channel_is_skipped_despite_matching_url(self):
        fake = FakeLinkedChannel(9999)  # resolved channel's ACTUAL guild != the URL's claimed 1111
        self._install_channel(fake)
        ctx = self.make_ctx()
        await cmd("remix")(ctx, prompt=self.LINK)
        self.assertEqual(fake.fetch_message_calls, 0)
        self.assertIn(
            "Skipping 1 message link(s) I couldn't fetch — the message may be "
            "gone, or you may not have access to that channel.",
            ctx.channel.texts,
        )

    async def test_links_refused_outside_a_guild(self):
        ctx = self.make_ctx(guild=None)
        await cmd("remix")(ctx, prompt=self.LINK)
        self.assertIn(
            "Message links only work in a server channel — skipping 1 link(s).",
            ctx.channel.texts,
        )
        self.assertEqual(self.api.call_count, 0)

    async def test_valid_link_appends_after_own_attachment_and_own_attachment_drives_size(self):
        own = FakeAttachment(800, 600, data=_make_png(800, 600))
        linked_att = FakeAttachment(1024, 1024, data=_make_png(1024, 1024))
        fake = FakeLinkedChannel(1111, message=FakeMessage(attachments=[linked_att]))
        self._install_channel(fake)
        ctx = self.make_ctx(attachments=[own])
        await cmd("remix")(ctx, prompt=self.LINK)
        self.assertEqual(fake.fetch_message_calls, 1)
        self.assertEqual(
            self.api.edit_calls[0].images,
            [(own.data, "image/png"), (linked_att.data, "image/png")],
        )
        self.assertEqual(self.api.edit_calls[0].size, "960x720")

    async def test_link_is_stripped_from_the_prompt(self):
        linked_att = FakeAttachment(1024, 1024, data=_make_png(1024, 1024))
        fake = FakeLinkedChannel(1111, message=FakeMessage(attachments=[linked_att]))
        self._install_channel(fake)
        ctx = self.make_ctx()
        await cmd("remix")(ctx, prompt=f"make it art {self.LINK}")
        self.assertEqual(self.api.edit_calls[0].prompt, "make it art")


# =========================================================================== #
# G. &daily_image
# =========================================================================== #

class DailyImageCommandTest(BotTestCase):
    SCHEDULE = [{"id": "morning", "time": "07:00", "type": "generate",
                 "message": "It's the image of the day for {date}"}]

    def _write_schedule(self):
        with open(bot_ross.DAILY_SCHEDULE_FILE, "w") as f:
            json.dump(self.SCHEDULE, f)

    async def test_repost_makes_no_api_call_and_spends_nothing(self):
        self._write_schedule()
        day = datetime.now(bot_ross.BOT_ZONE).date()
        png = _make_png(64, 64)
        with open(bot_ross._daily_image_path(day), "wb") as f:
            f.write(png)
        ctx = self.make_ctx()
        await cmd("daily_image")(ctx)
        expected = daily_schedule.render_message(self.SCHEDULE[0]["message"], day)
        self.assertEqual(ctx.channel.texts, [expected + "\n♻️ (repost — already painted today)"])
        files = ctx.channel.files
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, daily_schedule.daily_image_filename(day))
        self.assertEqual(files[0].file_bytes, png)
        self.assertEqual(self.api.call_count, 0)
        self.assertEqual(self.read_data(), {})
        self.assertFalse(os.path.exists(bot_ross.DAILY_STATE_FILE))

    async def test_generate_path_retains_prunes_and_marks_fired(self):
        self._write_schedule()
        day = datetime.now(bot_ross.BOT_ZONE).date()
        ctx = self.make_ctx()
        await cmd("daily_image")(ctx)
        expected = daily_schedule.render_message(self.SCHEDULE[0]["message"], day)
        self.assertEqual(ctx.channel.texts, [expected])
        files = ctx.channel.files
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].filename.startswith("painting_"))
        expected_prompt = release_image.build_release_prompt(daily_schedule.seed_source_for(day))[0]
        self.assertEqual(self.api.generate_calls[0].prompt, expected_prompt)
        with open(bot_ross._daily_image_path(day), "rb") as f:
            retained = f.read()
        self.assertEqual(retained, self.api.png_bytes)
        self.assertEqual(daily_schedule.load_state(bot_ross.DAILY_STATE_FILE), {"morning": day.isoformat()})
        data = self.read_data()
        self.assertEqual(data.get("daily_images"), 1)
        self.assertEqual(data.get(bot_ross.get_current_month()), 1)

    async def test_failure_posts_failure_message_once_without_marking_fired(self):
        self._write_schedule()

        async def _no_retry():
            return False

        self.enterContext(mock.patch.object(bot_ross, "_retry_delay", _no_retry))
        self.api.fail_on = {1}
        day = datetime.now(bot_ross.BOT_ZONE).date()
        ctx = self.make_ctx()
        await cmd("daily_image")(ctx)
        expected = daily_schedule.render_message(self.SCHEDULE[0]["message"], day)
        self.assertEqual(
            ctx.channel.texts,
            [expected, "No painting this time, exception for this request: boom", bot_ross.FAILURE_MESSAGE],
        )
        self.assertFalse(os.path.exists(bot_ross._daily_image_path(day)))
        self.assertFalse(os.path.exists(bot_ross.DAILY_STATE_FILE))
        self.assertEqual(self.api.call_count, 1)

    async def test_failure_retries_exactly_once(self):
        self._write_schedule()
        self.api.fail_on = {1, 2}
        ctx = self.make_ctx()
        await cmd("daily_image")(ctx)
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(self.retry_delays, 1)
        self.assertEqual(ctx.channel.texts.count(bot_ross.FAILURE_MESSAGE), 1)


# =========================================================================== #
# H. &daily_* management commands
# =========================================================================== #

SEED_SCHEDULE = [
    {"id": "morning", "time": "07:00", "type": "generate",
     "message": "It's the image of the day for {date}"},
    {"id": "lunch", "time": "12:00", "type": "edit",
     "message": "Lunchtime for {date}", "edit_prompt": "add soup"},
]


class DailyScheduleCommandsTest(BotTestCase):
    def _write_schedule(self, entries):
        with open(bot_ross.DAILY_SCHEDULE_FILE, "w") as f:
            json.dump(entries, f)

    async def test_update_with_bad_time_replies_error_and_leaves_file_byte_identical(self):
        self._write_schedule(SEED_SCHEDULE)
        with open(bot_ross.DAILY_SCHEDULE_FILE, "rb") as f:
            before = f.read()
        ctx = self.make_ctx()
        await cmd("daily_update")(ctx, slot_id="lunch", field="time", value="25:99")
        self.assertEqual(
            ctx.channel.texts,
            ["Couldn't update `lunch`: slot time out of range: '25:99'. Use 24-hour HH:MM, like 07:00 or 17:30."],
        )
        with open(bot_ross.DAILY_SCHEDULE_FILE, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)

    async def test_add_edit_slot_persists_with_provenance(self):
        self._write_schedule(SEED_SCHEDULE)
        ctx = self.make_ctx()
        await cmd("daily_add")(
            ctx, slot_id="teatime", slot_time="15:30", slot_type="edit",
            rest="Tea time! :: everyone stops for tea",
        )
        self.assertEqual(
            ctx.channel.texts,
            ["Added daily slot `teatime` — 15:30 edit. Switch it off with `&daily_toggle teatime`, "
             "remove it with `&daily_remove teatime`."],
        )
        entries = daily_schedule.load_schedule(bot_ross.DAILY_SCHEDULE_FILE)
        entry = next(e for e in entries if e["id"] == "teatime")
        expected_subset = {
            "id": "teatime", "time": "15:30", "type": "edit",
            "message": "Tea time!", "edit_prompt": "everyone stops for tea",
            "author": "tester", "added": date.today().isoformat(),
        }
        self.assertEqual({k: entry.get(k) for k in expected_subset}, expected_subset)

    async def test_add_duplicate_id_refused_without_write(self):
        self._write_schedule(SEED_SCHEDULE)
        with open(bot_ross.DAILY_SCHEDULE_FILE, "rb") as f:
            before = f.read()
        ctx = self.make_ctx()
        await cmd("daily_add")(
            ctx, slot_id="morning", slot_time="08:00", slot_type="generate", rest="Different message",
        )
        self.assertEqual(
            ctx.channel.texts,
            ["`morning` already exists. Use `&daily_update morning <field> <value>` to change it, "
             "or `&daily_remove morning` first."],
        )
        with open(bot_ross.DAILY_SCHEDULE_FILE, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)

    async def test_removing_last_generate_slot_warns(self):
        self._write_schedule(SEED_SCHEDULE)
        ctx = self.make_ctx()
        await cmd("daily_remove")(ctx, slot_id="morning")
        self.assertEqual(ctx.channel.texts, ["Removed daily slot `morning`." + bot_ross.NO_ENABLED_GENERATE_WARNING])
        entries = daily_schedule.load_schedule(bot_ross.DAILY_SCHEDULE_FILE)
        self.assertEqual([e["id"] for e in entries], ["lunch"])

    async def test_list_refuses_to_treat_corrupt_file_as_empty(self):
        with open(bot_ross.DAILY_SCHEDULE_FILE, "wb") as f:
            f.write(b"{not json")
        ctx = self.make_ctx()
        await cmd("daily_list")(ctx)
        self.assertEqual(len(ctx.channel.texts), 1)
        self.assertTrue(
            ctx.channel.texts[0].startswith(
                f"`{bot_ross.DAILY_SCHEDULE_FILE}` exists but could not be parsed as JSON — fix it by hand."
            )
        )
        self.assertNotIn("schedule is empty", ctx.channel.texts[0])

    async def test_toggle_disables_and_persists(self):
        self._write_schedule(SEED_SCHEDULE)
        ctx = self.make_ctx()
        await cmd("daily_toggle")(ctx, slot_id="morning")
        text = ctx.channel.texts[0]
        self.assertTrue(
            text.startswith("Daily slot `morning` is now **disabled** — it stays in the schedule but won't fire.")
        )
        self.assertIn(bot_ross.NO_ENABLED_GENERATE_WARNING, text)
        entries = daily_schedule.load_schedule(bot_ross.DAILY_SCHEDULE_FILE)
        entry = next(e for e in entries if e["id"] == "morning")
        self.assertIs(entry["enabled"], False)
        self.assertEqual(entry["editor"], "tester")


# =========================================================================== #
# I. _run_daily_slot -- driven directly (also a second, direct exercise of the
#    production _ChannelContext, since _run_daily_slot constructs one internally)
# =========================================================================== #

class RunDailySlotTest(BotTestCase):
    DAY = date(2026, 3, 3)  # fixed -- no clock dependence

    async def test_generate_slot_announces_generates_and_retains(self):
        entry = {"id": "morning", "time": "07:00", "type": "generate", "message": "Daily for {date}"}
        channel = FakeChannel()
        await bot_ross._run_daily_slot(channel, entry, self.DAY)
        self.assertEqual(channel.texts, [daily_schedule.render_message(entry["message"], self.DAY)])
        expected_prompt = release_image.build_release_prompt(daily_schedule.seed_source_for(self.DAY))[0]
        self.assertEqual(self.api.generate_calls[0].prompt, expected_prompt)
        with open(bot_ross._daily_image_path(self.DAY), "rb") as f:
            self.assertEqual(f.read(), self.api.png_bytes)
        self.assertEqual(self.read_data().get("daily_images"), 1)

    async def test_edit_slot_recovers_missing_base_silently_then_edits(self):
        entry = {"id": "lunch", "time": "12:00", "type": "edit", "message": "Lunch {date}", "edit_prompt": "add soup"}
        self.api.png_bytes = _make_png(816, 816)
        channel = FakeChannel()
        await bot_ross._run_daily_slot(channel, entry, self.DAY)
        self.assertEqual([c.kind for c in self.api.calls], ["generate", "edit"])
        # Recovery posts no announcement of its own -- the only TEXT sent is the
        # lunch announcement; the recovery image and the edit image are both file
        # sends (order: file, text, file).
        self.assertEqual(channel.texts, [daily_schedule.render_message(entry["message"], self.DAY)])
        self.assertEqual(self.api.edit_calls[0].images, [(self.api.png_bytes, "image/png")])
        self.assertEqual(self.api.edit_calls[0].size, "816x816")
        self.assertEqual(self.api.edit_calls[0].prompt, "add soup")
        data = self.read_data()
        self.assertEqual(data.get("daily_images"), 1)
        self.assertEqual(data.get("daily_edits"), 1)


# =========================================================================== #
# J. Magic-management commands
# =========================================================================== #

class MagicCommandsTest(BotTestCase):
    async def test_magic_add_show_remove_round_trip(self):
        text = "In the background, a squirrel juggles acorns."
        expected_id = magic_paint.slugify_magic_id(text, {"test-mixin"})

        ctx = self.make_ctx()
        await cmd("magic_add")(ctx, text=text)
        self.assertEqual(
            ctx.channel.texts,
            [f"Added magic mixin `{expected_id}`. Remove it with `&magic_remove {expected_id}`."],
        )
        with open(bot_ross.MAGIC_PROMPTS_FILE) as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 2)
        new_entry = next(e for e in entries if e["id"] == expected_id)
        self.assertEqual(new_entry["author"], "tester")
        self.assertEqual(new_entry["added"], date.today().isoformat())

        ctx2 = self.make_ctx()
        await cmd("magic_show")(ctx2, entry_id=expected_id)
        self.assertEqual(len(ctx2.channel.texts), 1)
        self.assertIn(text, ctx2.channel.texts[0])
        self.assertIn("Author: tester", ctx2.channel.texts[0])

        ctx3 = self.make_ctx()
        await cmd("magic_remove")(ctx3, entry_id=expected_id)
        with open(bot_ross.MAGIC_PROMPTS_FILE) as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "test-mixin")

    async def test_magic_update_sets_editor_and_keeps_provenance(self):
        ctx = self.make_ctx()
        await cmd("magic_update")(ctx, entry_id="test-mixin", text="NEW TEXT")
        self.assertEqual(ctx.channel.texts, ["Updated magic mixin `test-mixin`."])
        with open(bot_ross.MAGIC_PROMPTS_FILE) as f:
            entries = json.load(f)
        entry = next(e for e in entries if e["id"] == "test-mixin")
        self.assertEqual(entry["text"], "NEW TEXT")
        self.assertEqual(entry["editor"], "tester")
        self.assertEqual(entry["edited"], date.today().isoformat())
        self.assertNotIn("author", entry)

    async def test_magic_rate_set_updates_global_and_history(self):
        ctx = self.make_ctx()
        await cmd("magic_rate")(ctx, value="10")
        self.assertEqual(ctx.channel.texts, ["Magic rate set to 10%."])
        self.assertEqual(bot_ross.MAGIC_PAINT_RATE, 0.10)
        history = self.read_data()["magic_rate_history"]
        self.assertEqual(history[-1]["user"], "tester")
        self.assertEqual(history[-1]["rate"], 0.10)

    async def test_magic_rate_invalid_value_rejected(self):
        ctx = self.make_ctx()
        await cmd("magic_rate")(ctx, value="banana")
        self.assertEqual(
            ctx.channel.texts,
            ["Couldn't read that rate. Try `10`, `.1`, `10%`, or `.1%` (values map to a 0–100% chance)."],
        )
        self.assertEqual(bot_ross.MAGIC_PAINT_RATE, 0.0)


# =========================================================================== #
# K. Macros through a real command
# =========================================================================== #

class MacroExpansionTest(BotTestCase):
    def _write_macros(self, entries):
        with open(bot_ross.MACROS_FILE, "w") as f:
            json.dump(entries, f)

    async def test_macro_expansion_echoes_and_feeds_the_api(self):
        self._write_macros([{"id": "rhe", "text": "X,"}])
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="A ;rhe here")
        self.assertIn("expanded prompt: A X, here", ctx.channel.texts)
        self.assertEqual(self.api.generate_calls[0].prompt, "A X, here")
        data = self.read_data()
        self.assertEqual(data.get("macros"), 1)
        # DEVIATION from the literal spec ("no macro_misses key"): expand_prompt_
        # macros always writes BOTH counters together in the same save whenever it
        # saves at all (`data['macro_misses'] = data.get('macro_misses', 0) +
        # len(misses)` runs unconditionally alongside the 'macros' increment) -- so
        # the key is present with value 0, never simply absent. Assert the value
        # instead of the key's absence.
        self.assertEqual(data.get("macro_misses", 0), 0)

    async def test_macro_miss_gets_dice_line_and_fallback(self):
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="A ;nope here")
        miss_text = next(t for t in ctx.channel.texts if t.startswith("\U0001f3b2"))
        lines = miss_text.split("\n")
        self.assertEqual(lines[0], "\U0001f3b2 `;nope` (macro not found, good luck)")
        self.assertTrue(lines[1].startswith("expanded prompt: A "))
        prompt = self.api.generate_calls[0].prompt
        self.assertTrue(any(fb in prompt for fb in macros.FALLBACK_EXPANSIONS))
        self.assertEqual(self.read_data().get("macro_misses"), 1)

    async def test_macro_echo_never_reveals_magic(self):
        self._write_macros([{"id": "rhe", "text": "X,"}])
        self.enterContext(mock.patch.object(bot_ross, "MAGIC_PAINT_RATE", 1.0))
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="A ;rhe here")
        self.assertIn("expanded prompt: A X, here", ctx.channel.texts)  # post-macro, PRE-magic
        self.assertEqual(self.api.generate_calls[0].prompt, "A X, here " + MAGIC_TEXT)
        self.assertIn(QUOTE_SENTINEL + " \U0001f58c️", ctx.channel.texts)
        files = ctx.channel.files
        self.assertEqual(len(files), 1)
        # Segment 1 here is the ONLY segment (no pipe), and it's the non-quiet
        # path -- same pre-existing filename+description leak documented on
        # assertNoMagicLeak and exercised in test_quiet_segment_magic_tell_
        # without_prompt_leak. There is no quiet segment in this test to prove
        # clean, so this assertion is now a no-op given the single-message list
        # -- kept for structural symmetry with the other magic tests, and
        # because the content leg still fires for real.
        self.assertNoMagicLeak(ctx.channel, skip_description={files[0]}, skip_filename={files[0]})


# =========================================================================== #
# L. Drain
# =========================================================================== #

class DrainTest(BotTestCase):
    async def test_check_refuses_and_notifies_while_draining(self):
        self.enterContext(mock.patch.object(bot_ross, "draining", True))
        ctx = self.make_ctx()
        result = await bot_ross._reject_while_draining(ctx)
        self.assertFalse(result)
        self.assertEqual(
            ctx.channel.texts,
            ["\U0001f9f9 Bot Ross is wrapping up and restarting for an update — try again in a moment."],
        )

    async def test_check_passes_when_not_draining(self):
        ctx = self.make_ctx()
        result = await bot_ross._reject_while_draining(ctx)
        self.assertTrue(result)
        self.assertEqual(ctx.channel.sent, [])

    def test_drain_check_is_registered_as_a_global_bot_check(self):
        # .callback (used everywhere else in this file) bypasses @bot.check by
        # design (verified fact) -- registration is the one property
        # invocation-level tests structurally cannot cover, so assert it
        # directly against the private _checks list (present on the installed
        # discord.py 2.x, verified against this repo's own .venv).
        self.assertIn(bot_ross._reject_while_draining, bot_ross.bot._checks)


# =========================================================================== #
# M. do_the_art internals
# =========================================================================== #

class DoTheArtTest(BotTestCase):
    async def test_over_limit_refuses_before_calling_the_api(self):
        self.enterContext(mock.patch.object(bot_ross, "LIMIT", 1))
        month = bot_ross.get_current_month()
        with open(bot_ross.DATA_FILE, "w") as f:
            json.dump({month: 1}, f)
        ctx = self.make_ctx()
        await cmd("paint")(ctx, prompt="x")
        self.assertEqual(
            ctx.channel.texts,
            [QUOTE_SENTINEL, "Monthly limit reached. Please wait until next month to make more paint requests."],
        )
        self.assertEqual(self.api.call_count, 0)
        self.assertEqual(self.read_data(), {month: 1})

    async def test_failed_reply_send_rebuilds_the_file_from_scratch(self):
        exc = discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"), "reference_unknown")
        channel = FailingOnceChannel(exc)
        anchor = FakeMessage()
        ctx = FakeContext(channel=channel)
        result = await bot_ross.do_the_art(
            ctx, "p", "pipe", "gpt-image-2",
            images=[(_make_png(816, 816), "image/png")], size="816x816",
            reply_to=anchor, quiet=True,
        )
        self.assertTrue(bool(result))
        self.assertEqual(len(channel.attempts), 2)
        self.assertIsNot(channel.attempts[0].file, channel.attempts[1].file)
        self.assertIs(channel.attempts[0].reference, anchor)
        self.assertIsNone(channel.attempts[1].reference)
        self.assertEqual(channel.attempts[1].file_bytes, self.api.png_bytes)

    async def test_send_failure_without_reply_is_the_loud_failure_path(self):
        exc = discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"), "reference_unknown")

        class _AlwaysFailOnFileChannel(FakeChannel):
            async def send(self, content=None, *, file=None, reference=None, **kwargs):
                if file is not None:
                    raise exc
                return await super().send(content, file=file, reference=reference, **kwargs)

        ctx = FakeContext(channel=_AlwaysFailOnFileChannel())
        result = await bot_ross.do_the_art(ctx, "p", "paint", "gpt-image-2")
        self.assertFalse(result)
        texts = ctx.channel.texts
        self.assertTrue(texts[-1].startswith("No painting for: p, exception for this request:"))
        self.assertEqual(self.read_data(), {})

    async def test_art_result_fields_and_truthiness(self):
        ctx = self.make_ctx()
        result = await bot_ross.do_the_art(ctx, "p", "paint", "gpt-image-2", size="1024x1024")
        self.assertTrue(bool(result))
        self.assertIs(result.message, ctx.channel.files[0].message)
        self.assertEqual(result.image_bytes, self.api.png_bytes)
        self.assertEqual(result.size, "1024x1024")
        self.assertGreaterEqual(result.elapsed, 0.0)


# =========================================================================== #
# N. Production _ChannelContext -- converts do_the_art's docstring claim ("only
#    ever touches ctx.send and ctx.author.name") into a checked one.
# =========================================================================== #

class ChannelContextTest(BotTestCase):
    async def test_quiet_generation_through_channel_context(self):
        channel = FakeChannel()
        ctx = bot_ross._ChannelContext(channel)
        result = await bot_ross.do_the_art(ctx, "secret prompt", "daily_image", "gpt-image-2", quiet=True)
        self.assertTrue(bool(result))
        files = channel.files
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].filename.startswith("painting_"))
        self.assertIsNone(files[0].description)
        for msg in channel.sent:
            if msg.content:
                self.assertNotIn("secret prompt", msg.content)
        self.assertEqual(len(channel.texts), 0)
        self.assertEqual(ctx.author.name, "daily_schedule")
        self.assertEqual(self.read_data().get("daily_images"), 1)

    async def test_over_limit_through_channel_context(self):
        self.enterContext(mock.patch.object(bot_ross, "LIMIT", 1))
        month = bot_ross.get_current_month()
        with open(bot_ross.DATA_FILE, "w") as f:
            json.dump({month: 1}, f)
        channel = FakeChannel()
        ctx = bot_ross._ChannelContext(channel)
        result = await bot_ross.do_the_art(ctx, "p", "daily_image", "gpt-image-2")
        self.assertFalse(result)
        self.assertEqual(
            channel.texts,
            ["Monthly limit reached. Please wait until next month to make more paint requests."],
        )

    async def test_reply_and_content_forwarded_through_channel_context(self):
        channel = FakeChannel()
        ctx = bot_ross._ChannelContext(channel)
        anchor = FakeMessage()
        result = await bot_ross.do_the_art(
            ctx, "p", "pipe", "gpt-image-2",
            images=[(_make_png(816, 816), "image/png")], size=None,
            reply_to=anchor, content="\U0001f58c️", quiet=True,
        )
        self.assertTrue(bool(result))
        files = channel.files
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].content, "\U0001f58c️")
        self.assertIs(files[0].reference, anchor)


# =========================================================================== #
# O. &meme
# =========================================================================== #

# C4: get_meme_prompt no longer calls the openai SDK -- it POSTs through
# _fetch_meme_prompt (bot_ross module global, resolved at call time), in
# fetch_image's raw-aiohttp style. GetMemePromptTest (T1-T5) drives
# get_meme_prompt() directly with _fetch_meme_prompt faked, so no test in
# this section ever touches aiohttp or the network. MemeCommandTest's T6-T8
# additions drive the same fake through the real &meme command, on top of
# FakeImageAPI, to prove the rewritten prompt actually reaches fetch_image
# (or doesn't, on a transport failure) end to end.

class GetMemePromptTest(BotTestCase):
    def _patch_fetch(self, fake):
        self.enterContext(mock.patch.object(bot_ross, "_fetch_meme_prompt", fake))

    async def test_content_reaches_caller_stripped(self):
        # T1
        async def fake(payload):
            return {"choices": [{"message": {"content": "  A frog in a suit  "}}]}

        self._patch_fetch(fake)
        self.assertEqual(await bot_ross.get_meme_prompt("frogs"), "A frog in a suit")

    async def test_payload_is_exactly_model_and_messages(self):
        # T2 -- the live-400 guard: no max_tokens, no temperature, nothing but
        # what the old SDK call sent.
        captured = {}

        async def fake(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "x"}}]}

        self._patch_fetch(fake)
        await bot_ross.get_meme_prompt("frogs")
        self.assertEqual(set(captured.keys()), {"model", "messages"})
        self.assertEqual(captured["model"], bot_ross.MEME_MODEL)
        self.assertEqual(len(captured["messages"]), 2)
        self.assertEqual([m["role"] for m in captured["messages"]], ["system", "user"])
        self.assertEqual(
            captured["messages"][1]["content"],
            "Create a prompt for an image meme based on the following idea: frogs",
        )

    async def test_no_suggestion_uses_wildest_imagination_line(self):
        # T3
        captured = {}

        async def fake(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "x"}}]}

        self._patch_fetch(fake)
        await bot_ross.get_meme_prompt(None)
        self.assertEqual(
            captured["messages"][1]["content"],
            "Create a prompt for an image meme based on your wildest imagination.",
        )

    async def test_every_bad_shape_falls_back_and_logs_both_lines(self):
        # T4 -- the assertLogs requirement: the fallback can never again be
        # silent. `data` not a dict raises TypeError, missing/empty `choices`
        # raises KeyError/IndexError, missing message/content raises KeyError,
        # content=None raises AttributeError on .strip() -- all five must
        # converge on the same fallback string AND the same two log lines.
        bad_shapes = [
            {},
            {"choices": []},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": None}}]},
            [],
        ]
        for data in bad_shapes:
            with self.subTest(data=data):
                async def fake(payload, _data=data):
                    return _data

                self._patch_fetch(fake)
                with self.assertLogs("bot_ross", level="WARNING") as cm:
                    result = await bot_ross.get_meme_prompt("x")
                self.assertEqual(
                    result, "Two fluffy black cats trying to fix a broken robot based on Bob Ross"
                )
                error_records = [r for r in cm.records if r.levelname == "ERROR"]
                warning_records = [r for r in cm.records if r.levelname == "WARNING"]
                self.assertTrue(
                    any("Meme prompt response had an unexpected shape" in r.getMessage() for r in error_records)
                )
                self.assertTrue(
                    any("Falling back to the hardcoded meme prompt" in r.getMessage() for r in warning_records)
                )

    async def test_transport_failure_raises_never_falls_back(self):
        # T5 -- the anti-"two-black-cats-forever" test: a 401/wrong-URL must
        # abort, not paint. Proves the try/except around the parse can't
        # swallow a transport/status failure raised OUTSIDE it.
        async def fake(payload):
            raise RuntimeError("boom")

        self._patch_fetch(fake)
        with self.assertRaises(RuntimeError):
            await bot_ross.get_meme_prompt("x")


class MemeCommandTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.meme_calls = []

        async def _stub(user_prompt):
            self.meme_calls.append(user_prompt)
            return "MEME-PROMPT"

        self.enterContext(mock.patch.object(bot_ross, "get_meme_prompt", _stub))

    async def test_meme_with_suggestion_flows_prompt_to_api_and_counts(self):
        ctx = self.make_ctx()
        await cmd("meme")(ctx, prompt="cats")
        self.assertEqual(ctx.channel.texts[0], "Generating meme prompt based on: cats")
        self.assertEqual(ctx.channel.texts[1], "Generated prompt: MEME-PROMPT")
        self.assertEqual(self.api.generate_calls[0].prompt, "MEME-PROMPT")
        self.assertIsNone(self.api.generate_calls[0].size)
        self.assertEqual(self.read_data().get("memes"), 1)

    async def test_meme_without_suggestion_uses_the_wild_imagination_line(self):
        ctx = self.make_ctx()
        await cmd("meme")(ctx)
        self.assertEqual(ctx.channel.texts[0], "Generating meme prompt based on GPTs wildest imagination.")
        self.assertEqual(self.meme_calls, [None])


class MemeCommandFetchIntegrationTest(BotTestCase):
    """Drives &meme with bot_ross._fetch_meme_prompt faked directly (rather than
    get_meme_prompt, as MemeCommandTest does above) -- proves the rewritten chat
    call's result actually reaches fetch_image end to end, and that a transport
    failure spends no image generation."""

    def _patch_fetch(self, fake):
        self.enterContext(mock.patch.object(bot_ross, "_fetch_meme_prompt", fake))

    async def test_meme_prompt_reaches_fetch_image_end_to_end(self):
        # T6
        async def fake(payload):
            return {"choices": [{"message": {"content": "A meme about chairs"}}]}

        self._patch_fetch(fake)
        ctx = self.make_ctx()
        await cmd("meme")(ctx, prompt="office chairs")
        self.assertEqual(self.api.generate_calls[0].prompt, "A meme about chairs")
        self.assertEqual(self.api.generate_calls[0].model, bot_ross.IMAGE_MODEL)
        self.assertIn("Generated prompt: A meme about chairs", ctx.channel.texts)
        self.assertEqual(self.read_data().get("memes"), 1)

    async def test_transport_failure_spends_no_image(self):
        # T7 -- confirms the failure ordering: prompt fetch fails => zero API spend.
        async def fake(payload):
            raise RuntimeError("boom")

        self._patch_fetch(fake)
        ctx = self.make_ctx()
        with self.assertRaises(RuntimeError):
            await cmd("meme")(ctx, prompt="office chairs")
        self.assertEqual(self.api.call_count, 0)
        self.assertFalse(any(t.startswith("Generated prompt:") for t in ctx.channel.texts))
        self.assertNotIn(bot_ross.get_current_month(), self.read_data())

    async def test_fallback_path_still_paints_and_warns(self):
        # T8 -- the degraded mode is intentionally still functional; it just
        # can no longer be quiet about it.
        async def fake(payload):
            return {}

        self._patch_fetch(fake)
        ctx = self.make_ctx()
        with self.assertLogs("bot_ross", level="WARNING"):
            await cmd("meme")(ctx, prompt="office chairs")
        self.assertEqual(self.api.call_count, 1)
        self.assertEqual(
            self.api.generate_calls[0].prompt,
            "Two fluffy black cats trying to fix a broken robot based on Bob Ross",
        )


# =========================================================================== #
# P. &release_image
# =========================================================================== #

class ReleaseImageCommandTest(BotTestCase):
    async def test_release_image_is_deterministic_and_immune_to_magic(self):
        self.enterContext(mock.patch.object(bot_ross, "MAGIC_PAINT_RATE", 1.0))
        ctx = self.make_ctx()
        await cmd("release_image")(ctx, args="abc123")
        prompt, seed, ver = release_image.build_release_prompt("abc123", None, False)
        self.assertEqual(ctx.channel.texts[0], QUOTE_SENTINEL)  # no printf -- release images are magic-immune
        expected_announce = f"Release image for `abc123` | seed {seed} | algo v{ver}\n**Prompt**: {prompt}"
        self.assertEqual(ctx.channel.texts[1], expected_announce)
        self.assertEqual(self.api.generate_calls[0].prompt, prompt)
        data = self.read_data()
        self.assertEqual(data.get("release_images"), 1)
        self.assertNotIn("magic", data)

    async def test_release_image_empty_args_refused(self):
        ctx = self.make_ctx()
        await cmd("release_image")(ctx)
        self.assertEqual(ctx.channel.texts, ["Give me a git hash or any text to immortalize as a release image..."])
        self.assertEqual(self.api.call_count, 0)


# =========================================================================== #
# Q. &stats
# =========================================================================== #

class StatsCommandTest(BotTestCase):
    async def test_stats_is_one_message_carrying_the_new_counters(self):
        month = bot_ross.get_current_month()
        with open(bot_ross.DATA_FILE, "w") as f:
            json.dump({month: 3, "pipes": 2, "pipe_segments": 5}, f)
        ctx = self.make_ctx()
        await cmd("stats")(ctx)
        self.assertEqual(len(ctx.channel.sent), 1)
        text = ctx.channel.texts[0]
        self.assertIn("Monthly limit: 100", text)
        self.assertIn("Monthly requests: 3", text)
        self.assertIn("Pipe chains: 2", text)
        self.assertIn("Pipe segments: 5", text)


if __name__ == "__main__":
    unittest.main()
