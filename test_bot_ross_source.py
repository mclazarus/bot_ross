"""Static (AST-level) regression checks against bot_ross.py.

bot_ross.py ends in bot.run(...) at module scope, so it can NEVER be imported
under test (importing it starts the bot). Every non-trivial piece of logic in
this codebase lives in a pure module instead -- but do_the_art's quiet-mode
"never post the prompt" promise is small, security/privacy-sensitive, and
lives entirely inside bot_ross.py itself, so it has no pure-module home. This
file is the pragmatic fallback for that one case: it parses bot_ross.py with
`ast` and asserts structural properties of do_the_art's source, without ever
executing it.

Specifically this guards against a real regression: do_the_art derived the
Discord attachment's filename (via generate_file_name(prompt)) and alt-text
description (via `response['revised_prompt'] or prompt`) unconditionally, so
even under quiet=True (the daily scheduler) the deterministic daily prompt --
and any hidden magic mixin appended to it -- was fully readable by hovering
over or downloading the posted image, defeating the documented "the prompt is
logged, never posted" promise (CLAUDE.md, Daily Image of the Day / spec_daily
section 4.9) even though no code path ever printed the prompt as text.

Run from the repo root:  python -m unittest test_bot_ross_source -v
"""

import ast
import os
import unittest

BOT_ROSS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_ross.py")


def _load_function(name):
    """Parse bot_ross.py and return the (async or sync) top-level-walked
    FunctionDef/AsyncFunctionDef node named `name`, without ever executing
    the module (which would start the bot)."""
    with open(BOT_ROSS_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in bot_ross.py")


def _load_do_the_art():
    return _load_function("do_the_art")


def _string_constants(node):
    """All literal string ast.Constant values anywhere inside `node`,
    including the literal segments of an f-string (ast.JoinedStr wraps its
    literal parts as ast.Constant too)."""
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _calls_named(node, name):
    """All ast.Call nodes inside `node` whose function is the bare name `name`."""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def _calls_attr(node, module_name, func_name):
    """All ast.Call nodes inside `node` shaped like `module_name.func_name(...)`
    (an attribute call, e.g. daily_schedule.validate_slot(...)) -- the counterpart
    to _calls_named for calls made through an imported module rather than a bare
    name."""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == func_name
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == module_name
    ]


def _assigns_to(func_node, name):
    """All ast.Assign nodes inside func_node whose target is the bare name `name`."""
    return [
        node
        for node in ast.walk(func_node)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]


def _references(node, name):
    """Whether `name` appears as a bare Name anywhere inside `node`."""
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _enclosing_if_tests(func_node, target_call):
    """All ast.If.test expressions inside func_node whose subtree contains
    target_call (by node identity) -- lets a test ask "what condition, if
    any, gates this specific call" without caring how deeply the call is
    nested inside that test's own boolean expression (e.g. `and`/`or`)."""
    return [
        node.test for node in ast.walk(func_node)
        if isinstance(node, ast.If) and target_call in ast.walk(node.test)
    ]


def _quiet_true_branch_value(test, body, orelse):
    """Given an if/ternary's `test` plus its two branches, return whichever
    branch executes when `quiet` is True -- or None if `test` doesn't
    reference `quiet` at all. Handles the negated form (`if not quiet:` /
    `x if not quiet else y`) by swapping which branch is "true"."""
    if not _references(test, "quiet"):
        return None
    negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    return orelse if negated else body


def _quiet_branch_assign_values(func_node, name):
    """All expressions that `name` is assigned to specifically in the branch
    that runs when quiet is True -- checking BOTH shapes a `quiet`-gated
    assignment can take, so a benign refactor between them (ternary <->
    if/else statement) doesn't break callers of this helper:

      1. Ternary: `name = a if not quiet else b` (an ast.Assign whose value
         is an ast.IfExp).
      2. If/else statement: `if quiet: name = a` / `if not quiet: ... else:
         name = b` (an ast.If whose relevant branch contains an ast.Assign
         to `name`).
    """
    values = []
    for assign in _assigns_to(func_node, name):
        if isinstance(assign.value, ast.IfExp):
            branch = _quiet_true_branch_value(assign.value.test, assign.value.body, assign.value.orelse)
            if branch is not None:
                values.append(branch)
    for node in ast.walk(func_node):
        if not isinstance(node, ast.If):
            continue
        branch = _quiet_true_branch_value(node.test, node.body, node.orelse)
        if branch is None:
            continue
        for stmt in branch:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in stmt.targets
            ):
                values.append(stmt.value)
    return values


class DoTheArtQuietPromptLeakTest(unittest.TestCase):
    """do_the_art must not let `prompt` reach the posted Discord attachment's
    filename or alt-text description when quiet=True (the daily scheduler)."""

    @classmethod
    def setUpClass(cls):
        cls.do_the_art = _load_do_the_art()

    def test_file_name_is_computed_conditionally_on_quiet(self):
        # A regression to an unconditional `file_name = generate_file_name(prompt)`
        # (today's/pre-fix behavior) would fail this: no assignment to file_name
        # would reference `quiet` at all. Checked via _quiet_branch_assign_values
        # so this tolerates EITHER the ternary form (`file_name = a if not quiet
        # else b`) or an equivalent if/else statement form -- a purely cosmetic
        # refactor between the two must not fail this test.
        assigns = _assigns_to(self.do_the_art, "file_name")
        self.assertTrue(assigns, "do_the_art must assign a local named file_name")
        self.assertTrue(
            _quiet_branch_assign_values(self.do_the_art, "file_name"),
            "file_name must be assigned a distinct value specifically when "
            "quiet is True -- the quiet path needs a prompt-free filename, "
            "not the one derived from the prompt text",
        )

    def test_quiet_file_name_branch_does_not_reference_prompt(self):
        # Precise check: of whichever branch (ternary or if/else statement)
        # runs when quiet is True, assert THAT branch never references
        # `prompt`. Tolerates a refactor between the two shapes -- see
        # _quiet_branch_assign_values.
        quiet_branch_values = _quiet_branch_assign_values(self.do_the_art, "file_name")
        self.assertTrue(
            quiet_branch_values,
            "expected a `quiet`-gated assignment to file_name in do_the_art "
            "(ternary or if/else statement form)",
        )
        for value in quiet_branch_values:
            self.assertFalse(
                _references(value, "prompt"),
                "the file_name expression used when quiet=True must not "
                "reference `prompt` (it would leak the hidden prompt/magic "
                "mixin as the Discord attachment's filename)",
            )

    def test_description_assigned_under_quiet_does_not_reference_prompt(self):
        # Same leak, second half: the alt-text `description` passed to
        # discord.File. A regression to the old unconditional
        # `description = (response['revised_prompt'] or prompt)[:1024]` would
        # fail this: either no `if quiet:` branch would assign description at
        # all, or the one that did would still reference `prompt`.
        if_nodes = [
            node
            for node in ast.walk(self.do_the_art)
            if isinstance(node, ast.If) and _references(node.test, "quiet")
        ]
        self.assertTrue(if_nodes, "expected an `if quiet:`-shaped branch in do_the_art")

        gated = False
        for if_node in if_nodes:
            # `if quiet:` -> node.body runs when quiet is True.
            for stmt in if_node.body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "description" for t in stmt.targets
                ):
                    gated = True
                    self.assertFalse(
                        _references(stmt.value, "prompt"),
                        "description assigned inside `if quiet:` must not "
                        "reference `prompt` (it would leak it as the Discord "
                        "attachment's alt text)",
                    )
        self.assertTrue(
            gated,
            "expected `description` to be assigned inside an `if quiet:` branch",
        )


class QuietFileNameIsFeatureAgnosticTest(unittest.TestCase):
    """The quiet-mode filename must not claim every quiet=True attachment is a
    daily image: quiet=True is also the mechanism pipe-chain segments (Feature
    2) use, and a filename hardcoded to "daily_image_..." would mislabel every
    chained-paint image as a scheduler post. Regression: the quiet branch was
    `f"daily_image_{int(time.time())}.png"`; it must instead be a neutral name
    that says nothing about which feature produced it."""

    @classmethod
    def setUpClass(cls):
        cls.do_the_art = _load_do_the_art()

    def test_quiet_file_name_literal_does_not_say_daily_image(self):
        quiet_branch_values = _quiet_branch_assign_values(self.do_the_art, "file_name")
        self.assertTrue(quiet_branch_values, "expected a quiet-gated assignment to file_name")
        for value in quiet_branch_values:
            for literal in _string_constants(value):
                self.assertNotIn(
                    "daily_image", literal,
                    "the quiet-mode file_name must be feature-agnostic (e.g. "
                    "'painting_...'), not hardcoded to 'daily_image_...' -- "
                    "pipe-chain segments (Feature 2) also use quiet=True and "
                    "are not daily images",
                )


class DailyRetrySkipsSleepOnOverLimitTest(unittest.TestCase):
    """_do_the_art_with_retry must not sleep 2 minutes and retry a failure that
    was actually the monthly cap: do_the_art already posts "Monthly limit
    reached..." on the first attempt, and a retry would just hit the same cap
    and post the same message again -- once API_LIMIT is reached this would
    otherwise repeat for every remaining slot each day (announcement + two
    "Monthly limit reached" posts + a wasted 2-minute stall, per slot).
    Regression: the pre-fix version went straight from a falsy first result to
    `_retry_delay()`/asyncio.sleep(120) with no over_limit check in between."""

    @classmethod
    def setUpClass(cls):
        cls.func = _load_function("_do_the_art_with_retry")

    def test_over_limit_is_checked_before_the_retry_delay(self):
        over_limit_calls = _calls_named(self.func, "over_limit")
        self.assertTrue(
            over_limit_calls,
            "_do_the_art_with_retry must call over_limit(...) to short-circuit "
            "the retry when the failure was the monthly cap",
        )
        retry_delay_calls = _calls_named(self.func, "_retry_delay")
        self.assertTrue(retry_delay_calls, "_do_the_art_with_retry must still retry via _retry_delay()")
        # Straight-line code with early returns: source order is control-flow
        # order here, so the over_limit check must appear (in the source) before
        # the 2-minute retry-delay sleep it's meant to skip.
        self.assertLess(
            over_limit_calls[0].lineno, retry_delay_calls[0].lineno,
            "over_limit(...) must be checked BEFORE _retry_delay()'s 2-minute "
            "sleep, so an over-the-cap failure short-circuits instead of "
            "sleeping and retrying into the same cap",
        )

    def test_over_limit_is_called_with_freshly_loaded_data(self):
        # over_limit(data) needs a freshly reloaded `data` dict (the module counter
        # do_the_art itself just incremented on a successful call elsewhere, and
        # over_limit reads data[current_month] -- a stale/cached dict would never
        # observe a cap crossed by a concurrent request). Guards against someone
        # "optimizing" this into over_limit(some_stale_local) later.
        over_limit_calls = _calls_named(self.func, "over_limit")
        self.assertTrue(over_limit_calls)
        call = over_limit_calls[0]
        self.assertEqual(len(call.args), 1, "over_limit must be called with exactly one argument")
        arg = call.args[0]
        self.assertTrue(
            isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "load_data",
            "over_limit's argument must be a fresh load_data() call, not a "
            "stale/cached data dict",
        )


class GetCurrentMonthIsTimezoneAwareTest(unittest.TestCase):
    """get_current_month() must read datetime.now(BOT_ZONE), not the naive
    datetime.now() (the container's own UTC clock) it used before the daily
    scheduler landed. This is a deliberate, documented behavior change to the
    monthly API_LIMIT boundary (see CLAUDE.md, 'get_current_month() is
    timezone-aware, not a UTC-only helper'): the spend-limit month now rolls
    over at local midnight in BOT_TIMEZONE, up to ~5 hours earlier or later
    than the old UTC-midnight boundary, once a month.

    Regression this guards against: 3636c189's commit message originally
    claimed the opposite -- that get_current_month() was "unaffected" and
    there was "no change to the monthly-limit boundary in this commit" --
    while the commit's own diff made exactly this change. The code and
    CLAUDE.md were always correct; only the commit message lied. There is no
    way for a unit test to inspect a historical commit message, but this
    test pins the underlying code property the false message denied, so any
    future regression back to a naive, non-timezone-aware datetime.now()
    call -- which would silently reintroduce the exact "unaffected" claim as
    true, contradicting CLAUDE.md's documented promise -- fails loudly here
    instead of shipping quietly."""

    @classmethod
    def setUpClass(cls):
        cls.func = _load_function("get_current_month")

    def test_calls_datetime_now_with_bot_zone_argument(self):
        now_calls = [
            n for n in ast.walk(self.func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "now"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "datetime"
        ]
        self.assertTrue(now_calls, "get_current_month must call datetime.now(...)")
        self.assertEqual(
            len(now_calls), 1,
            "expected exactly one datetime.now(...) call in get_current_month",
        )
        call = now_calls[0]
        self.assertTrue(
            call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "BOT_ZONE",
            "get_current_month's datetime.now(...) call must be passed BOT_ZONE "
            "as its argument -- a bare, argument-less datetime.now() reads the "
            "container's own (UTC) clock instead of the configured BOT_TIMEZONE, "
            "silently moving the monthly API_LIMIT boundary back to UTC midnight",
        )

    def test_does_not_call_bare_datetime_now(self):
        # Belt-and-suspenders: even if a future refactor renamed the BOT_ZONE
        # argument check above into something that could be fooled, a bare
        # `datetime.now()` call (zero args) anywhere in this function is
        # itself the exact regression -- fail directly on its presence.
        for n in ast.walk(self.func):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "now"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "datetime"
            ):
                self.assertTrue(
                    n.args,
                    "get_current_month must not call datetime.now() with no "
                    "arguments -- that reads the container's own UTC clock, "
                    "not BOT_TIMEZONE",
                )


class DataDirCreatedBeforeSeedingTest(unittest.TestCase):
    """os.makedirs(DAILY_IMAGES_DIR, exist_ok=True) -- the only thing that
    actually creates data/ on a fresh checkout run outside Docker (Docker's
    bind-mounted volume supplies data/ for free, which is why this was never
    noticed there) -- must execute BEFORE _seed_magic_library(),
    _seed_macro_library(), and _seed_daily_schedule(), not after. Those three
    seed functions write their working copy straight to a data/... path via
    json_library.seed_library, which fails open (catches OSError, logs
    "Failed to seed ...") rather than raising, so on a fresh checkout with no
    data/ yet, seeding before the directory exists silently no-ops every
    library on the first run -- they only actually seed on the second start.

    Regression: bot_ross.py originally called the three _seed_* functions
    first, and only then os.makedirs(DAILY_IMAGES_DIR, exist_ok=True) at the
    very bottom of the module. Since os.makedirs creates every missing
    intermediate directory, that call is also what creates data/ itself, so
    it has to run first."""

    @classmethod
    def setUpClass(cls):
        with open(BOT_ROSS_PATH, "r", encoding="utf-8") as f:
            source = f.read()
        cls.tree = ast.parse(source)

    def _module_level_makedirs_calls(self, tree=None):
        # os.makedirs(...) as a bare top-level statement (an ast.Expr whose
        # value is the Call) -- deliberately restricted to MODULE scope (not
        # ast.walk, which would also match a makedirs call nested inside some
        # unrelated function) since ordering only means something among
        # statements that actually run in source order at import time.
        #
        # Further restricted to the call that actually creates the data
        # directory -- os.makedirs(DAILY_IMAGES_DIR, ...) (or a literal
        # "data/..." string) -- so an unrelated module-level os.makedirs(...)
        # added above the seed calls in the future can't make this test pass
        # while the real DAILY_IMAGES_DIR call still regresses below them.
        tree = tree if tree is not None else self.tree
        calls = []
        for stmt in tree.body:
            if not isinstance(stmt, ast.Expr):
                continue
            call = stmt.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "makedirs"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "os"
                and call.args
            ):
                continue
            first_arg = call.args[0]
            is_data_dir = (
                isinstance(first_arg, ast.Name) and first_arg.id == "DAILY_IMAGES_DIR"
            ) or (
                isinstance(first_arg, ast.Constant)
                and isinstance(first_arg.value, str)
                and first_arg.value.startswith("data/")
            )
            if is_data_dir:
                calls.append(call)
        return calls

    def _module_level_seed_call_linenos(self, tree=None):
        tree = tree if tree is not None else self.tree
        seed_names = {"_seed_magic_library", "_seed_macro_library", "_seed_daily_schedule"}
        linenos = []
        for stmt in tree.body:
            if not isinstance(stmt, ast.Expr):
                continue
            call = stmt.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in seed_names:
                linenos.append(call.lineno)
        return linenos

    def test_makedirs_precedes_first_seed_call(self):
        makedirs_calls = self._module_level_makedirs_calls()
        self.assertTrue(
            makedirs_calls,
            "expected a module-level os.makedirs(...) call that creates data/ "
            "at startup",
        )
        seed_linenos = self._module_level_seed_call_linenos()
        self.assertEqual(
            len(seed_linenos), 3,
            "expected all three of _seed_magic_library()/_seed_macro_library()/"
            "_seed_daily_schedule() to be called at module scope",
        )
        earliest_makedirs_lineno = min(c.lineno for c in makedirs_calls)
        self.assertLess(
            earliest_makedirs_lineno, min(seed_linenos),
            "os.makedirs(...) (which creates data/) must run BEFORE the "
            "_seed_*() calls -- json_library.seed_library fails open on a "
            "missing data/ directory (catches OSError and just logs), so "
            "seeding before the directory exists silently no-ops every "
            "library's first-run seed instead of creating it",
        )

    def test_unrelated_makedirs_does_not_mask_a_regressed_data_dir_call(self):
        """Regression test for the ordering check itself: an unrelated
        module-level os.makedirs(...) that does NOT create the data
        directory (e.g. os.makedirs('logs', ...)) must not be mistaken for
        the real data-dir call. Without the DAILY_IMAGES_DIR/'data/' filter
        in _module_level_makedirs_calls, an early unrelated makedirs would
        satisfy test_makedirs_precedes_first_seed_call's ordering check even
        while the actual DAILY_IMAGES_DIR makedirs had regressed to AFTER
        the seed calls -- exactly the bug this class exists to catch."""
        source = (
            "import os\n"
            "os.makedirs('logs', exist_ok=True)\n"
            "_seed_magic_library()\n"
            "_seed_macro_library()\n"
            "_seed_daily_schedule()\n"
            "os.makedirs(DAILY_IMAGES_DIR, exist_ok=True)\n"
        )
        tree = ast.parse(source)

        makedirs_calls = self._module_level_makedirs_calls(tree)
        self.assertEqual(
            len(makedirs_calls), 1,
            "the unrelated os.makedirs('logs', ...) call must be filtered "
            "out -- only the DAILY_IMAGES_DIR call creates the data "
            "directory",
        )

        seed_linenos = self._module_level_seed_call_linenos(tree)
        earliest_makedirs_lineno = min(c.lineno for c in makedirs_calls)
        self.assertGreater(
            earliest_makedirs_lineno, min(seed_linenos),
            "sanity check on the synthetic source: the real data-dir "
            "makedirs() is deliberately placed AFTER the seed calls here, "
            "so the filtered result must reflect that regression rather "
            "than being masked by the earlier unrelated makedirs('logs')",
        )


class DailyCommandsValidateBeforeSaveTest(unittest.TestCase):
    """&daily_add/&daily_update/&daily_remove/&daily_toggle mutate
    data/daily_schedule.json. Two structural invariants, both from
    daily_schedule.py's module docstring (see its "&daily_* command-surface
    command-surface helpers" section and CLAUDE.md's Daily Image of the Day
    notes): a write must be validated before it's saved, and no `await` may
    separate the load from the save (a single-threaded event loop can't
    interleave two edits as long as nothing yields control in between --
    but a `ctx.send` partway through would). Checked purely via `ast` since
    bot_ross.py can never be imported under test."""

    MUTATING_COMMANDS = ("daily_add", "daily_update", "daily_remove", "daily_toggle")
    VALIDATE_GATED_COMMANDS = ("daily_add", "daily_update", "daily_toggle")

    @classmethod
    def setUpClass(cls):
        cls.functions = {name: _load_function(name) for name in cls.MUTATING_COMMANDS}

    def test_daily_commands_exist(self):
        for name in ("daily_list", "daily_show") + self.MUTATING_COMMANDS:
            with self.subTest(name=name):
                func = _load_function(name)
                self.assertIsInstance(func, ast.AsyncFunctionDef, f"{name} must be an async command")

    def test_validate_slot_precedes_save_in_add_update_toggle(self):
        # daily_remove is deliberately excluded: removing an entry can never
        # produce an invalid one, so it has nothing to validate before saving.
        for name in self.VALIDATE_GATED_COMMANDS:
            with self.subTest(command=name):
                func = self.functions[name]
                validate_calls = _calls_attr(func, "daily_schedule", "validate_slot")
                save_calls = _calls_named(func, "_save_daily_schedule")
                self.assertTrue(validate_calls, f"{name} must call daily_schedule.validate_slot before saving")
                self.assertTrue(save_calls, f"{name} must call _save_daily_schedule")
                first_validate_lineno = min(c.lineno for c in validate_calls)
                for call in save_calls:
                    self.assertGreater(
                        call.lineno, first_validate_lineno,
                        f"{name}: _save_daily_schedule at line {call.lineno} must come after the "
                        f"first daily_schedule.validate_slot(...) call at line {first_validate_lineno} "
                        "-- a schedule edit must be validated before it's written",
                    )

    def test_no_await_between_load_and_save(self):
        for name in self.MUTATING_COMMANDS:
            with self.subTest(command=name):
                func = self.functions[name]
                load_calls = _calls_named(func, "_load_daily_schedule")
                save_calls = _calls_named(func, "_save_daily_schedule")
                self.assertTrue(load_calls, f"{name} must call _load_daily_schedule")
                self.assertTrue(save_calls, f"{name} must call _save_daily_schedule")
                load_lineno = min(c.lineno for c in load_calls)
                save_lineno = max(c.lineno for c in save_calls)
                for a in ast.walk(func):
                    if not isinstance(a, ast.Await):
                        continue
                    self.assertTrue(
                        a.lineno < load_lineno or a.lineno > save_lineno,
                        f"{name}: an await at line {a.lineno} falls between the load "
                        f"(line {load_lineno}) and the save (line {save_lineno}) -- a "
                        "ctx.send in between would let two concurrent edits interleave "
                        "and lose one",
                    )

    def test_seed_file_never_referenced_in_a_daily_command(self):
        for name in self.MUTATING_COMMANDS + ("daily_list", "daily_show"):
            with self.subTest(command=name):
                func = _load_function(name)
                self.assertNotIn(
                    "daily_schedule.json", _string_constants(func),
                    f"{name} must write only through _save_daily_schedule (bound to "
                    "DAILY_SCHEDULE_FILE) -- never the literal seed filename",
                )

    def test_scheduler_tick_reloads_the_schedule_fresh(self):
        # Guards against caching the schedule in a module global, which would
        # silently make every &daily_* edit require a restart to take effect.
        func = _load_function("_run_due_daily_slots")
        calls = _calls_attr(func, "daily_schedule", "load_schedule")
        self.assertTrue(calls, "_run_due_daily_slots must call daily_schedule.load_schedule(...) every tick")


class DailyImageRepostDoesNotRegenerateTest(unittest.TestCase):
    """&daily_image's headline promise: if today's image was already painted, it is
    REPOSTED, not repainted. That is what keeps a manual catch-up from silently
    spending a monthly request (and re-rolling magic) on an image already on disk.

    The whole guarantee is the early `return` in the `if png_bytes is not None:`
    branch -- one deleted line turns every repost into a fresh generation, and no
    test that avoids importing bot_ross could otherwise notice. So assert it
    structurally: nothing inside that branch may reach a generation call.
    """

    GENERATION_CALLS = ("_do_the_art_with_retry", "do_the_art", "_save_daily_image")

    def _repost_branch(self):
        """The `if png_bytes is not None:` branch body of daily_image_cmd."""
        func = _load_function("daily_image_cmd")
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "png_bytes"
                and any(isinstance(op, ast.IsNot) for op in test.ops)
            ):
                return node
        raise AssertionError("daily_image_cmd has no `if png_bytes is not None:` branch")

    def test_repost_branch_makes_no_generation_call(self):
        branch = self._repost_branch()
        for name in self.GENERATION_CALLS:
            with self.subTest(call=name):
                found = [
                    n for n in ast.walk(ast.Module(body=branch.body, type_ignores=[]))
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == name
                ]
                self.assertEqual(
                    found, [],
                    f"the repost path calls {name}() -- reposting must never spend a "
                    f"generation or rewrite the retained image",
                )

    def test_repost_branch_rolls_no_magic(self):
        # The retained PNG already baked in whatever the original roll decided; a
        # second roll would both mis-count the `magic` stat and imply, via the 🖌️
        # tell, that this particular image got a mixin when it may not have.
        branch = self._repost_branch()
        module = ast.Module(body=branch.body, type_ignores=[])
        for name in ("maybe_apply_magic_paint", "_apply_random_magic_entry", "_bump_magic_counter"):
            with self.subTest(call=name):
                self.assertEqual(
                    [n for n in ast.walk(module)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name],
                    [],
                    f"the repost path calls {name}() -- a repost must not re-roll magic",
                )

    def test_repost_branch_returns_before_the_generate_path(self):
        branch = self._repost_branch()
        self.assertTrue(
            any(isinstance(n, ast.Return) for n in ast.walk(ast.Module(body=branch.body, type_ignores=[]))),
            "the repost branch must return -- without it, execution falls through "
            "into the generate path and repaints the image anyway",
        )

    def test_generate_path_does_still_generate(self):
        # Guards the inverse regression: a refactor that made the whole command a
        # no-op would pass every assertion above.
        func = _load_function("daily_image_cmd")
        self.assertTrue(
            _calls_named(func, "_do_the_art_with_retry"),
            "daily_image_cmd never generates at all",
        )
        self.assertTrue(
            _calls_named(func, "_save_daily_image"),
            "daily_image_cmd never retains what it generated, so the next run would "
            "repaint instead of reposting",
        )


class DailyUpdateDoesNotEchoUnboundedTextTest(unittest.TestCase):
    """&daily_update's success reply must not echo a raw message/edit_prompt value
    verbatim through a bare ctx.send: those fields are free text of arbitrary
    length, and Discord's 2000-char message cap means a long enough value makes
    the CONFIRMATION fail (discord.HTTPException) even though the write already
    succeeded -- the user sees no reply and has no reason to believe the edit
    landed. Regression: `&daily_update lunch message <1972 x's>` (1972 is exactly
    what fits after the ~28-char reply prefix in 2000 chars) produced a
    2006-character reply that raised on send.
    """

    @classmethod
    def setUpClass(cls):
        cls.func = _load_function("daily_update")

    def test_calls_daily_schedule_truncate_text(self):
        calls = _calls_attr(self.func, "daily_schedule", "truncate_text")
        self.assertTrue(
            calls,
            "daily_update must route the message/edit_prompt display value through "
            "daily_schedule.truncate_text(...) before building its reply -- echoing "
            "the raw value can exceed Discord's 2000-char message cap",
        )


class DailyUpdateWarnsWhenDisablingLastGenerateSlotTest(unittest.TestCase):
    """&daily_toggle and &daily_remove both warn when a write leaves no enabled
    `generate` slot (edit slots would then silently repaint the base image
    themselves every time). &daily_update <id> enabled off is a DOCUMENTED
    equivalent spelling of &daily_toggle (CLAUDE.md's field-setter examples
    include it) and performs the identical write, but originally emitted no
    warning at all -- a user reaching for the field-setter form got no signal
    that they'd just disabled the day's base-image generator.
    """

    @classmethod
    def setUpClass(cls):
        cls.func = _load_function("daily_update")

    def test_references_has_enabled_generate_slot(self):
        calls = _calls_attr(self.func, "daily_schedule", "has_enabled_generate_slot")
        self.assertTrue(
            calls,
            "daily_update must check daily_schedule.has_enabled_generate_slot(...) "
            "after a save, the same guard daily_toggle/daily_remove use",
        )

    def test_references_the_shared_warning_constant(self):
        self.assertTrue(
            _references(self.func, "NO_ENABLED_GENERATE_WARNING"),
            "daily_update must append the same NO_ENABLED_GENERATE_WARNING constant "
            "daily_toggle/daily_remove use, not a hand-copied (and driftable) string",
        )

    def test_toggle_and_remove_also_use_the_shared_constant(self):
        # Belt-and-suspenders: pins that the three call sites were actually
        # unified onto one constant, not just that daily_update grew its own.
        for name in ("daily_toggle", "daily_remove"):
            with self.subTest(command=name):
                func = _load_function(name)
                self.assertTrue(
                    _references(func, "NO_ENABLED_GENERATE_WARNING"),
                    f"{name} must reference the shared NO_ENABLED_GENERATE_WARNING constant",
                )

    def test_warning_check_is_not_gated_to_only_the_enabled_field(self):
        # Regression: the check was originally written as
        #   if normalized_field == "enabled" and ... has_enabled_generate_slot(...):
        # which warns when &daily_update flips `enabled` off, but NOT when
        # `&daily_update <id> type edit` is the write that empties the last
        # enabled generate slot -- that leaves the schedule with zero generate
        # slots exactly as surely as disabling one would, silently. The fix is
        # to check has_enabled_generate_slot(...) unconditionally after every
        # successful save, so no ast.If gating that call may also compare
        # normalized_field to the literal "enabled".
        func = _load_function("daily_update")
        calls = _calls_attr(func, "daily_schedule", "has_enabled_generate_slot")
        self.assertTrue(calls, "daily_update must call daily_schedule.has_enabled_generate_slot(...)")
        for call in calls:
            for test in _enclosing_if_tests(func, call):
                gating_compares = [
                    n for n in ast.walk(test)
                    if isinstance(n, ast.Compare)
                    and any(isinstance(c, ast.Constant) and c.value == "enabled" for c in n.comparators)
                ]
                self.assertEqual(
                    gating_compares, [],
                    "the has_enabled_generate_slot(...) check must not be gated behind "
                    "`normalized_field == \"enabled\"` -- a `type` change to \"edit\" can "
                    "also leave zero enabled generate slots, and the warning must fire then too",
                )


class DailyNoSuchSlotReplyIsBoundedTest(unittest.TestCase):
    """&daily_show/&daily_update/&daily_remove/&daily_toggle all echo
    daily_schedule.normalize_slot_id(slot_id) back in a "No daily slot with
    id `...`" reply when the id isn't found. slot_id is raw, unbounded user
    text at that point -- is_valid_slot_id's 1-32-char check only runs on
    &daily_add's happy path, never before this message -- so an untruncated
    echo of a long enough slot_id can itself exceed Discord's 2000-char
    message cap and the "sorry, no such slot" reply silently fails to send.
    Regression: `&daily_show` with a ~1980-char id produced a 2005-char reply.
    """

    COMMANDS = ("daily_show", "daily_update", "daily_remove", "daily_toggle")

    def test_normalize_slot_id_is_wrapped_in_truncate_text(self):
        for name in self.COMMANDS:
            with self.subTest(command=name):
                func = _load_function(name)
                wrapped = [
                    call for call in _calls_attr(func, "daily_schedule", "truncate_text")
                    if any(
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "normalize_slot_id"
                        for arg in call.args
                    )
                ]
                self.assertTrue(
                    wrapped,
                    f"{name} must route daily_schedule.normalize_slot_id(slot_id) through "
                    "daily_schedule.truncate_text(...) before echoing it in a 'No daily slot "
                    "with id' reply -- slot_id is unbounded user text at that point",
                )


class DailyListAddGuardAgainstUnparseableScheduleTest(unittest.TestCase):
    """&daily_list and &daily_add must distinguish "no schedule yet" from "the
    schedule file exists but doesn't parse as JSON" -- load_schedule fails open
    to [] in both cases. Without the distinction, a hand-edit typo (e.g. a
    trailing comma) makes &daily_list report "empty" and the natural next step,
    &daily_add, overwrites the whole (still-there-but-unparseable) file with a
    single new entry, silently discarding every existing slot.
    """

    def test_daily_list_checks_schedule_file_is_corrupt(self):
        func = _load_function("daily_list")
        calls = _calls_attr(func, "daily_schedule", "schedule_file_is_corrupt")
        self.assertTrue(
            calls,
            "daily_list must call daily_schedule.schedule_file_is_corrupt(...) when "
            "the loaded schedule is empty, to distinguish a broken file from a "
            "genuinely empty one",
        )

    def test_daily_add_checks_schedule_file_is_corrupt(self):
        func = _load_function("daily_add")
        calls = _calls_attr(func, "daily_schedule", "schedule_file_is_corrupt")
        self.assertTrue(
            calls,
            "daily_add must call daily_schedule.schedule_file_is_corrupt(...) before "
            "treating an empty-looking schedule as safe to add the first entry to",
        )


if __name__ == "__main__":
    unittest.main()
