"""Static (AST/artifact-level) regression checks on bot_ross.py and the Dockerfile.

Since bot_ross.py grew load_config()/main() it is importable under test, and
test_bot_ross_commands.py drives the real command callbacks behaviorally --
anything with observable behavior belongs THERE, asserted on the output, not
on the code shape that produces it. This file's charter is deliberately
narrower: properties that are genuinely about source or artifact shape, where
no deterministic test can observe the behavior at all (get_current_month()'s
timezone-aware read, the no-await load/save invariant, the seed-filename
negative check, main()'s makedirs-before-seed ordering, and the Dockerfile's
Python floor) -- plus main()'s and load_config()'s own statement-level shape
(the `__main__` guard, no import-time side effects, setup_logging-before-
load_config-before-makedirs-before-seed ordering, the completeness of
load_config's `global` list, and the fetchers authenticating via the
OPENAI_API_KEY constant) -- plus a handful of classes that were
candidates for retirement here but are still AST-only because
test_bot_ross_commands.py does not yet drive the specific scenario that would
supersede them. Each such class's docstring says so explicitly, and the
retiring commit's message records the gap so it isn't lost. Docs-consistency
checks on CLAUDE.md/README.md (DocsTruthTest) also live here, on the same
theory: CLAUDE.md is a build artifact like the Dockerfile, and this file
already owns artifact-shape properties.

Everything with a landed behavioral replacement was retired in favor of it,
except where retiring one half of a paired check would split a class's
coverage across two files -- those are called out in place (see
DailyListAddGuardAgainstUnparseableScheduleTest's docstring for the current
example). The commit that retired each one names its replacement; see that
commit's message for the itemized list, including the several planned
retirements that were kept instead because no replacement exists yet.

Run from the repo root:  python -m unittest test_bot_ross_source -v
"""

import ast
import os
import re
import sys
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


class DailyRetrySkipsSleepOnOverLimitTest(unittest.TestCase):
    """_do_the_art_with_retry must not sleep 2 minutes and retry a failure that
    was actually the monthly cap: do_the_art already posts "Monthly limit
    reached..." on the first attempt, and a retry would just hit the same cap
    and post the same message again -- once API_LIMIT is reached this would
    otherwise repeat for every remaining slot each day (announcement + two
    "Monthly limit reached" posts + a wasted 2-minute stall, per slot).
    Regression: the pre-fix version went straight from a falsy first result to
    `_retry_delay()`/asyncio.sleep(120) with no over_limit check in between.

    KEPT (not retired): test_bot_ross_commands.py has no test that isolates
    this specific property. DailyImageCommandTest.test_failure_posts_failure_
    message_once_without_marking_fired stubs `_retry_delay` itself to a no-op
    returning False, which also makes the second attempt not happen -- so it
    can't distinguish "over_limit() short-circuited the retry" from "the
    stubbed _retry_delay just declined to retry". A real replacement needs the
    first failed attempt to write request_data.json up to API_LIMIT as a
    side effect (not before the call), then assert call_count stayed at 1
    with no asyncio.sleep. No such test exists yet."""

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

    Since C1, all four calls live inside main() rather than at module scope
    (bot_ross.py no longer does filesystem writes at import time), so the
    real assertions below walk main()'s body instead of the module's.

    Regression: bot_ross.py originally called the three _seed_* functions
    first, and only then os.makedirs(DAILY_IMAGES_DIR, exist_ok=True) at the
    very bottom of the module. Since os.makedirs creates every missing
    intermediate directory, that call is also what creates data/ itself, so
    it has to run first."""

    def _module_level_makedirs_calls(self, body):
        # os.makedirs(...) as a bare top-level statement of the given
        # statement list (an ast.Expr whose value is the Call) --
        # deliberately restricted to a flat statement list (not ast.walk,
        # which would also match a makedirs call nested inside some
        # unrelated function) since ordering only means something among
        # statements that actually run in source order within that scope.
        #
        # Further restricted to the call that actually creates the data
        # directory -- os.makedirs(DAILY_IMAGES_DIR, ...) (or a literal
        # "data/..." string) -- so an unrelated makedirs(...) added above
        # the seed calls in the future can't make this test pass while the
        # real DAILY_IMAGES_DIR call still regresses below them.
        calls = []
        for stmt in body:
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

    def _module_level_seed_call_linenos(self, body):
        seed_names = {"_seed_magic_library", "_seed_macro_library", "_seed_daily_schedule"}
        linenos = []
        for stmt in body:
            if not isinstance(stmt, ast.Expr):
                continue
            call = stmt.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in seed_names:
                linenos.append(call.lineno)
        return linenos

    def test_makedirs_precedes_first_seed_call(self):
        # Since C1 all four calls live inside main(), not at module scope --
        # see main()'s body directly rather than the whole module's tree.
        main_body = _load_function("main").body
        makedirs_calls = self._module_level_makedirs_calls(main_body)
        self.assertTrue(
            makedirs_calls,
            "expected an os.makedirs(...) call in main() that creates data/ "
            "at startup",
        )
        seed_linenos = self._module_level_seed_call_linenos(main_body)
        self.assertEqual(
            len(seed_linenos), 3,
            "expected all three of _seed_magic_library()/_seed_macro_library()/"
            "_seed_daily_schedule() to be called inside main()",
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

        makedirs_calls = self._module_level_makedirs_calls(tree.body)
        self.assertEqual(
            len(makedirs_calls), 1,
            "the unrelated os.makedirs('logs', ...) call must be filtered "
            "out -- only the DAILY_IMAGES_DIR call creates the data "
            "directory",
        )

        seed_linenos = self._module_level_seed_call_linenos(tree.body)
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
    but a `ctx.send` partway through would). Checked via `ast` because
    these are source-ordering invariants, which AST inspection expresses
    directly.

    NOT renamed/trimmed as originally planned: three of this class's five
    methods (test_daily_commands_exist, test_validate_slot_precedes_save_in_
    add_update_toggle, test_scheduler_tick_reloads_the_schedule_fresh) were
    slated for retirement, but none has a landed behavioral replacement --
    see each method's own KEPT note below for the specific gap. Only once all
    three are actually superseded does the class's namesake property go away
    and the rename to DailyCommandsSourceInvariantsTest become accurate."""

    MUTATING_COMMANDS = ("daily_add", "daily_update", "daily_remove", "daily_toggle")
    VALIDATE_GATED_COMMANDS = ("daily_add", "daily_update", "daily_toggle")

    @classmethod
    def setUpClass(cls):
        cls.functions = {name: _load_function(name) for name in cls.MUTATING_COMMANDS}

    def test_daily_commands_exist(self):
        # KEPT: test_bot_ross_commands.py never drives &daily_show at all (the
        # other five &daily_* commands are all exercised via cmd("daily_...")).
        # A missing command would make bot.get_command(...) return None and
        # error a behavioral test with equal clarity, but only once daily_show
        # is actually driven somewhere.
        for name in ("daily_list", "daily_show") + self.MUTATING_COMMANDS:
            with self.subTest(name=name):
                func = _load_function(name)
                self.assertIsInstance(func, ast.AsyncFunctionDef, f"{name} must be an async command")

    def test_validate_slot_precedes_save_in_add_update_toggle(self):
        # KEPT: test_bot_ross_commands.py has a byte-identical-file-on-bad-input
        # replacement for &daily_update (DailyScheduleCommandsTest.test_update_
        # with_bad_time_replies_error_and_leaves_file_byte_identical) but none
        # for &daily_add -- its two &daily_add tests
        # (test_add_edit_slot_persists_with_provenance,
        # test_add_duplicate_id_refused_without_write) are a successful add and
        # a duplicate-id refusal; neither passes an invalid field value, so
        # validate_slot's failure path inside &daily_add is never reached.
        # &daily_toggle legitimately has no reachable validate failure (flipping
        # `enabled` can't invalidate an entry), so its coverage would ride on
        # add/update once both exist.
        #
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
        # KEPT: no test in test_bot_ross_commands.py drives _run_due_daily_slots
        # at all. A replacement would edit the schedule file after a first tick
        # and assert the new slot fires on a second tick with no restart/reload
        # call -- losing this silently would let a future "optimization" cache
        # the schedule in a module global, making every &daily_* edit require a
        # restart with nothing noticing.
        #
        # Guards against caching the schedule in a module global, which would
        # silently make every &daily_* edit require a restart to take effect.
        func = _load_function("_run_due_daily_slots")
        calls = _calls_attr(func, "daily_schedule", "load_schedule")
        self.assertTrue(calls, "_run_due_daily_slots must call daily_schedule.load_schedule(...) every tick")


class DailyImageRepostRollsNoMagicTest(unittest.TestCase):
    """&daily_image's repost path (today's image already painted -> repost,
    don't repaint) must not re-roll magic paint: the retained PNG already
    baked in whatever the original roll decided, so a second roll would both
    mis-count the persisted `magic` stat and imply, via the appended 🖌️
    tell, that this particular retained image got a mixin when it may not
    have. daily_image_cmd's own docstring calls this one of its two
    deliberate repost guarantees (the other being "make no generation call").

    KEPT (not retired): the other three methods that used to live in
    DailyImageRepostDoesNotRegenerateTest (no-generation-call, early-return,
    generate-path-still-generates) ARE superseded by
    DailyImageCommandTest.test_repost_makes_no_api_call_and_spends_nothing /
    test_generate_path_retains_prunes_and_marks_fired in
    test_bot_ross_commands.py. This one is not: that fixture's SCHEDULE entry
    carries no `"magic": true` key, and BotTestCase.setUp pins
    MAGIC_PAINT_RATE to 0.0 for every test unless a test re-patches it -- so
    with daily_image_cmd only rolling magic under
    `if entry and entry.get("magic")`, the roll no-ops on both counts before
    it could ever reach _bump_magic_counter, and the repost test's
    `self.assertEqual(self.read_data(), {})` can't observe a re-roll landing
    there. (A *stray* magic call inserted into the repost branch would still
    be caught -- mutation-verified: it writes request_data.json and breaks
    that same assertion. What the fixture can't do is prove the absence is
    for the *right* reason rather than an accident of a magic-less entry and
    a zeroed rate.) A real replacement needs a magic:true generate entry,
    MAGIC_PAINT_RATE patched to 1.0, a pre-seeded retained PNG, and
    assertions that the repost reply carries no 🖌️ tell AND read_data() has
    no `magic` key afterward. Until that lands, this AST check is the only
    thing that would catch a magic roll hoisted into (or left inside) the
    repost branch."""

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


class DailyUpdateDoesNotEchoUnboundedTextTest(unittest.TestCase):
    """&daily_update's success reply must not echo a raw message/edit_prompt value
    verbatim through a bare ctx.send: those fields are free text of arbitrary
    length, and Discord's 2000-char message cap means a long enough value makes
    the CONFIRMATION fail (discord.HTTPException) even though the write already
    succeeded -- the user sees no reply and has no reason to believe the edit
    landed. Regression: `&daily_update lunch message <1972 x's>` (1972 is exactly
    what fits after the ~28-char reply prefix in 2000 chars) produced a
    2006-character reply that raised on send.

    KEPT (not retired): no test in test_bot_ross_commands.py drives a long
    message/edit_prompt value through &daily_update at all. A replacement
    needs to assert BOTH halves -- every sent message stays <= 2000 chars AND
    the schedule file afterward holds the full, untruncated value (so a naive
    "fix" can't just truncate what's stored)."""

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

    KEPT (not retired): test_bot_ross_commands.py exercises the warning for
    &daily_toggle and &daily_remove (DailyScheduleCommandsTest.test_removing_
    last_generate_slot_warns / test_toggle_disables_and_persists) but never
    for &daily_update -- neither the documented `enabled off` spelling nor
    the `type edit` case (test_warning_check_is_not_gated_to_only_the_enabled_
    field's whole point: the check must fire on ANY write that empties the
    last enabled generate slot, not just the `enabled` field) has a
    behavioral test. A replacement needs all four call sites -- &daily_update
    `enabled off`, &daily_update `type edit`, &daily_toggle, &daily_remove --
    to produce the identical warning text.
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

    KEPT (not retired): no test in test_bot_ross_commands.py drives any
    &daily_* command with an oversized bogus or on-disk slot id. A
    replacement needs two scenarios: (a) a ~2500-char bogus id sent to
    &daily_show/&daily_update/&daily_remove/&daily_toggle each sends a
    non-empty, <=2000-char reply; (b) a hand-written schedule file with a
    ~2500-char real id has the mutating commands' SUCCESS replies also stay
    <=2000 chars.
    """

    COMMANDS = ("daily_show", "daily_update", "daily_remove", "daily_toggle")

    # The SUCCESS path echoes the id too ("Removed daily slot `x`.", "Updated `x` ...",
    # "Daily slot `x` is now **enabled**."), sourced from slot_entry_id(entry) rather
    # than the typed slot_id. &daily_add caps a NEW id at 32 chars via is_valid_slot_id,
    # but a hand-edited data/daily_schedule.json can hold an arbitrarily long one, so
    # that path was unbounded while the not-found path above was already hardened. An
    # over-cap reply there is worse than the not-found case: the write has already
    # landed, so the user reads the HTTPException as "it failed" and may redo it.
    SUCCESS_ECHO_COMMANDS = ("daily_update", "daily_remove", "daily_toggle")

    def test_success_path_slot_entry_id_is_wrapped_in_truncate_text(self):
        for name in self.SUCCESS_ECHO_COMMANDS:
            with self.subTest(command=name):
                func = _load_function(name)
                bare = [
                    node for node in _assigns_to(func, "sid")
                    if isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "slot_entry_id"
                ]
                self.assertEqual(
                    bare, [],
                    f"{name} assigns sid = daily_schedule.slot_entry_id(entry) unwrapped; "
                    "sid is echoed into the success reply, so it must go through "
                    "daily_schedule.truncate_text(...) like the not-found replies do",
                )
                wrapped = [
                    call for call in _calls_attr(func, "daily_schedule", "truncate_text")
                    if any(
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "slot_entry_id"
                        for arg in call.args
                    )
                ]
                self.assertTrue(
                    wrapped,
                    f"{name} must wrap daily_schedule.slot_entry_id(entry) in "
                    "daily_schedule.truncate_text(...) before echoing it",
                )

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

    PARTIALLY retired: test_bot_ross_commands.py's DailyScheduleCommandsTest.
    test_list_refuses_to_treat_corrupt_file_as_empty behaviorally replaces the
    &daily_list half (asserts the "could not be parsed as JSON" reply and that
    it does NOT say "schedule is empty"), so test_daily_list_checks_schedule_
    file_is_corrupt below is genuinely redundant with it now. It stays anyway:
    the spec is explicit that both members of an (a)/(b) pair must have
    replacements before either AST method is dropped, since &daily_add's half
    -- driving &daily_add against a corrupt file and asserting it refuses
    with the file left byte-identical -- has no test at all yet. Dropping
    only the &daily_list method here would split one class's coverage across
    two files for no reader benefit.
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
        # KEPT: no test drives &daily_add against a corrupt schedule file.
        func = _load_function("daily_add")
        calls = _calls_attr(func, "daily_schedule", "schedule_file_is_corrupt")
        self.assertTrue(
            calls,
            "daily_add must call daily_schedule.schedule_file_is_corrupt(...) before "
            "treating an empty-looking schedule as safe to add the first entry to",
        )


class DockerfilePythonVersionTest(unittest.TestCase):
    """The Docker base image must not be older than the Python this code is
    developed and tested on.

    Version skew here is uniquely nasty: it produces a failure that passes every
    local check -- the full test suite, ast.parse, a manual read -- and then
    SyntaxErrors at container start, where the only symptom is a crash-looping
    container. It has already happened once, with a PEP 701 nested f-string
    (f"...{f'{e['id']}'}...") that 3.12 accepts and 3.10 rejects.

    ast.parse(..., feature_version=(3, 10)) does NOT catch that class of bug --
    verified: feature_version does not downgrade the f-string tokenizer -- and there
    is no CI running the older interpreter, so nothing else here would notice.
    Keeping the image at or above the development version removes the whole category
    rather than policing individual syntax features.
    """

    # Bump only alongside the development environment (.venv), never below it.
    MINIMUM = (3, 14)
    DOCKERFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Dockerfile")

    def _base_version(self):
        with open(self.DOCKERFILE, "r", encoding="utf-8") as f:
            for line in f:
                match = re.match(r"\s*FROM\s+python:(\d+)\.(\d+)", line)
                if match:
                    return int(match.group(1)), int(match.group(2))
        raise AssertionError("Dockerfile has no `FROM python:X.Y` line to check")

    def test_base_image_is_not_older_than_the_dev_interpreter(self):
        version = self._base_version()
        self.assertGreaterEqual(
            version, self.MINIMUM,
            f"Dockerfile pins python:{version[0]}.{version[1]} but this code is "
            f"developed and tested on {self.MINIMUM[0]}.{self.MINIMUM[1]}. Newer "
            "syntax would pass every local check and then SyntaxError at container "
            "start. Audit the source for newer-than-target syntax before lowering "
            "this, and lower MINIMUM here deliberately.",
        )

    def test_minimum_is_not_ahead_of_the_running_interpreter(self):
        # The other direction: if MINIMUM were bumped past what anyone actually runs
        # the tests on, this file would be asserting a guarantee nothing verifies.
        self.assertLessEqual(
            self.MINIMUM, sys.version_info[:2],
            f"MINIMUM is {self.MINIMUM} but the tests are running on "
            f"{sys.version_info[0]}.{sys.version_info[1]}; the version claim is "
            "unverified by this suite.",
        )


class NoImportTimeSideEffectsTest(unittest.TestCase):
    """Complement of DataDirCreatedBeforeSeedingTest's C1 retarget: those tests
    prove the four calls are correctly ORDERED inside main(), but say nothing
    about whether a regression duplicated them back onto the module body while
    leaving main() untouched. A behavioral import-with-no-environment test
    (test_bot_ross_config.py's ImportSafetyTest, M1) would catch most such a
    regression too, but only as long as the developer's own environment
    doesn't happen to satisfy whatever got duplicated -- this test names each
    offending call precisely in its failure message instead, unconditionally."""

    @classmethod
    def setUpClass(cls):
        with open(BOT_ROSS_PATH, "r", encoding="utf-8") as f:
            source = f.read()
        cls.tree = ast.parse(source)

    def _module_level_expr_calls(self):
        # Bare top-level statements only (ast.Expr wrapping a Call) -- a call
        # nested inside main() or any other function is fine and expected;
        # only module-SCOPE calls run at import time.
        calls = []
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                calls.append(stmt.value)
        return calls

    def test_no_bot_run_at_module_scope(self):
        offenders = [
            c for c in self._module_level_expr_calls()
            if isinstance(c.func, ast.Attribute) and c.func.attr == "run"
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "bot"
        ]
        self.assertEqual(
            offenders, [],
            "bot.run(...) must only be called from inside main(), gated by "
            "the `if __name__ == \"__main__\":` guard -- a module-scope call "
            "starts the bot on every `import bot_ross`, which is the whole "
            "thing C1 exists to prevent",
        )

    def test_no_makedirs_at_module_scope(self):
        offenders = [
            c for c in self._module_level_expr_calls()
            if isinstance(c.func, ast.Attribute) and c.func.attr == "makedirs"
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "os"
        ]
        self.assertEqual(
            offenders, [],
            "os.makedirs(...) must only run from inside main() -- a "
            "module-scope call writes to the filesystem on every "
            "`import bot_ross`, which ImportSafetyTest's tmpdir-listdir "
            "check (M1) exists to catch, but this names the call directly",
        )

    def test_no_seed_calls_at_module_scope(self):
        seed_names = {"_seed_magic_library", "_seed_macro_library", "_seed_daily_schedule"}
        offenders = [
            c for c in self._module_level_expr_calls()
            if isinstance(c.func, ast.Name) and c.func.id in seed_names
        ]
        self.assertEqual(
            offenders, [],
            "the three _seed_*() calls must only run from inside main() -- "
            "a module-scope call writes the working library/schedule copies "
            "to data/... on every `import bot_ross`",
        )


class MainGuardTest(unittest.TestCase):
    """Without `if __name__ == \"__main__\": main()`, `CMD ["python",
    "bot_ross.py"]` imports the module, does nothing, and exits 0 --
    and run.sh's `--restart=unless-stopped` then spins the container
    silently forever, with no error to grep for."""

    @classmethod
    def setUpClass(cls):
        with open(BOT_ROSS_PATH, "r", encoding="utf-8") as f:
            source = f.read()
        cls.tree = ast.parse(source)

    def test_main_guard_calls_main(self):
        guards = [
            stmt for stmt in self.tree.body
            if isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Compare)
            and isinstance(stmt.test.left, ast.Name) and stmt.test.left.id == "__name__"
            and any(isinstance(op, ast.Eq) for op in stmt.test.ops)
            and any(
                isinstance(c, ast.Constant) and c.value == "__main__"
                for c in stmt.test.comparators
            )
        ]
        self.assertTrue(guards, "expected an `if __name__ == \"__main__\":` block")
        calls_main = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "main"
            for guard in guards
            for n in ast.walk(guard)
            if n is not guard  # only inspect the guard's body, not itself
        )
        self.assertTrue(
            calls_main,
            "the `if __name__ == \"__main__\":` block must call main() -- "
            "without it the container imports, does nothing, and exits 0",
        )


class MainBodyOrderingTest(unittest.TestCase):
    """Statement ordering inside main() -- each comparison's failure mode is
    named individually, since these are exactly the three orderings C1's
    spec calls out as load-bearing (vanished warnings, writes before secret
    validation, bot serving traffic before its libraries are seeded)."""

    @classmethod
    def setUpClass(cls):
        cls.main = _load_function("main")

    def _first_call_lineno(self, name):
        for stmt in self.main.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if isinstance(call.func, ast.Name) and call.func.id == name:
                    return call.lineno
        raise AssertionError(f"main() has no top-level call to {name}()")

    def test_setup_logging_precedes_load_config(self):
        self.assertLess(
            self._first_call_lineno("setup_logging"),
            self._first_call_lineno("load_config"),
            "setup_logging() must run before load_config() -- otherwise the "
            "BOT_TIMEZONE warning and the nine inline-comment warnings log "
            "against a handler-less logger and vanish",
        )

    def test_load_config_precedes_makedirs(self):
        makedirs_calls = [
            stmt.value for stmt in self.main.body
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == "makedirs"
            and isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == "os"
        ]
        self.assertTrue(makedirs_calls, "expected an os.makedirs(...) call in main()")
        self.assertLess(
            self._first_call_lineno("load_config"),
            min(c.lineno for c in makedirs_calls),
            "load_config() must run before the filesystem writes -- a "
            "mis-started container (missing secret) should exit before "
            "touching the data/ volume",
        )

    def test_bot_run_follows_all_seed_calls(self):
        seed_names = ("_seed_magic_library", "_seed_macro_library", "_seed_daily_schedule")
        seed_linenos = [self._first_call_lineno(n) for n in seed_names]
        run_calls = [
            stmt.value for stmt in self.main.body
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == "run"
            and isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == "bot"
        ]
        self.assertTrue(run_calls, "expected a bot.run(...) call in main()")
        self.assertGreater(
            min(c.lineno for c in run_calls), max(seed_linenos),
            "bot.run(...) must follow all three _seed_*() calls -- otherwise "
            "the bot can start serving commands against unseeded libraries",
        )


class LoadConfigGlobalListTest(unittest.TestCase):
    """A name omitted from load_config's `global` statement whose test value
    happens to equal the module default passes every behavioral check --
    reassignment silently becomes a function-local instead of updating the
    module global. Enumerating the `global` statement's names directly
    catches the omission unconditionally, regardless of what any test
    happens to assert it to."""

    REQUIRED_GLOBALS = {
        "OPENAI_API_KEY", "DISCORD_BOT_TOKEN", "LIMIT", "IMAGE_MODEL", "IMAGE_MODERATION",
        "MEME_MODEL", "MAGIC_PAINT_RATE", "DRAIN_TIMEOUT", "BOT_TIMEZONE", "BOT_ZONE",
        "DAILY_IMAGE_ENABLED", "_raw_daily_channel", "DAILY_IMAGE_CHANNEL_ID",
        "DAILY_CHANNEL_MISCONFIGURED",
    }

    @classmethod
    def setUpClass(cls):
        cls.load_config = _load_function("load_config")

    def _globaled_names(self):
        names = set()
        for node in ast.walk(self.load_config):
            if isinstance(node, ast.Global):
                names.update(node.names)
        return names

    def test_global_statement_covers_every_required_name(self):
        globaled = self._globaled_names()
        missing = self.REQUIRED_GLOBALS - globaled
        self.assertEqual(
            missing, set(),
            f"load_config() must declare {sorted(missing)} in its `global` "
            "statement, or reassigning them inside the function creates a "
            "function-local shadow instead of updating the module config",
        )

    def test_raw_daily_channel_specifically_is_globaled(self):
        # Dedicated assertion, called out by name: omitting _raw_daily_channel
        # from the `global` list makes the assignment a function local, so
        # on_ready reads the module default (None) instead, silently
        # regressing the "set but unparseable vs. unset" distinction --
        # the exact bug that already cost a day of daily images in
        # production (see CLAUDE.md's Daily Image of the Day notes).
        self.assertIn(
            "_raw_daily_channel", self._globaled_names(),
            "_raw_daily_channel missing from load_config()'s `global` "
            "statement -- this is the production day-of-lost-daily-images "
            "regression, not a cosmetic omission",
        )


class FetchFunctionsUseOpenAIKeyConstantTest(unittest.TestCase):
    """fetch_image and fetch_image_edit authenticate via the module-level
    OPENAI_API_KEY constant, not the legacy `openai.api_key` SDK global --
    C4 deleted `import openai` and that global along with it, so this is now
    a permanent regression guard against reintroducing it, not a transitional
    sync check. If a header referenced openai.api_key again, it would be an
    outright NameError at &paint/&remix time (there is no `openai` module
    imported to hold that attribute anymore)."""

    def _asserts_no_openai_api_key_attr(self, func_node, label):
        offenders = [
            n for n in ast.walk(func_node)
            if isinstance(n, ast.Attribute) and n.attr == "api_key"
            and isinstance(n.value, ast.Name) and n.value.id == "openai"
        ]
        self.assertEqual(
            offenders, [],
            f"{label} must not reference openai.api_key -- C4 deleted the "
            "openai SDK entirely, so a header still reading it would raise "
            "NameError at request time, not merely drift from a live sync",
        )

    def _assert_references_openai_api_key_name(self, func_node, label):
        self.assertTrue(
            _references(func_node, "OPENAI_API_KEY"),
            f"{label} must authenticate via the module-level OPENAI_API_KEY "
            "constant set by load_config()",
        )

    def test_fetch_image(self):
        node = _load_function("fetch_image")
        self._asserts_no_openai_api_key_attr(node, "fetch_image")
        self._assert_references_openai_api_key_name(node, "fetch_image")

    def test_fetch_image_edit(self):
        node = _load_function("fetch_image_edit")
        self._asserts_no_openai_api_key_attr(node, "fetch_image_edit")
        self._assert_references_openai_api_key_name(node, "fetch_image_edit")

    def test_fetch_meme_prompt(self):
        # _fetch_meme_prompt (C4) is the third function POSTing to
        # api.openai.com with a Bearer header, and the only one whose body
        # is never exercised behaviorally (test_bot_ross_commands.py patches
        # it out wholesale as a fake, so it never runs for real there) --
        # this AST check is the only thing that would catch a future edit
        # that reached for openai.api_key instead of the OPENAI_API_KEY
        # constant.
        node = _load_function("_fetch_meme_prompt")
        self._asserts_no_openai_api_key_attr(node, "_fetch_meme_prompt")
        self._assert_references_openai_api_key_name(node, "_fetch_meme_prompt")


class RequirementsNoLongerListOpenAITest(unittest.TestCase):
    """T10 (C4): guards reintroduction via a future merge resolution. C4 dropped
    the openai SDK entirely (get_meme_prompt now POSTs through _fetch_meme_prompt,
    the same raw-aiohttp style as the image endpoints) -- requirements.txt must
    never list it again."""

    REQUIREMENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")

    def test_no_openai_requirement_line(self):
        with open(self.REQUIREMENTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        offenders = [line for line in lines if re.match(r"^openai\b", line)]
        self.assertEqual(
            offenders, [],
            f"requirements.txt still lists openai: {offenders!r} -- C4 dropped the SDK "
            "entirely in favor of a raw aiohttp POST (_fetch_meme_prompt)",
        )


class RequirementsNoLongerListAsyncioBackportTest(unittest.TestCase):
    """T11 (C4b): the PyPI package `asyncio` is a 2015 backport of the (then-new)
    stdlib module of the same name. `pip install -r requirements.txt` genuinely
    places it in site-packages, but it has been inert on every Python this repo
    has ever run on: the stdlib `asyncio` always wins on `sys.path` first, so the
    installed backport is never actually imported. The line pinned nothing,
    protected nothing, and only misled a reader into thinking asyncio was a
    third-party dependency here -- nothing may ever depend on that accident
    again, so this guards the line's reintroduction the same way T10 guards
    openai's."""

    REQUIREMENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")

    def test_no_asyncio_requirement_line(self):
        with open(self.REQUIREMENTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        offenders = [line for line in lines if re.match(r"^asyncio\b", line)]
        self.assertEqual(
            offenders, [],
            f"requirements.txt still lists asyncio: {offenders!r} -- it is the inert "
            "2015 PyPI backport, shadowed by stdlib on sys.path; C4b removed it",
        )


class NoStaleC6PreconditionClaimsTest(unittest.TestCase):
    """Guards against three stale claims a Python-3.14 rework (r_c6_python314)
    fixed in review. Each of these comments/docstrings asserted, in present
    tense, a fact that was only true *before* C6 rebuilt the .venv/image on
    3.14: that openai or coloredlogs was still installed pending a "future"
    C6 rebuild, or that discord.py 2.3.2 (rather than the current 2.7.1 pin)
    was "the installed discord.py source" for the Thread.permissions_for
    security claim. C6 has since landed -- the .venv is 3.14, neither package
    is installed, and the pinned discord.py is 2.7.1 -- so a maintainer
    reading any of these phrases verbatim would draw a false conclusion (e.g.
    "this test/mitigation is now vacuous, delete it") about code that in fact
    still has teeth via a different mechanism. Each phrase below must never
    reappear verbatim; a similar claim reintroduced later must be phrased in
    the past tense against the current pin, not as present-tense fact about a
    still-pending rebuild."""

    REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

    # (relative path, forbidden phrase, why it's now false)
    CASES = [
        (
            "test_bot_ross_commands.py",
            "the 3.12 .venv keeps",
            "OpenAISDKNotImportedTest's docstring claimed openai was still "
            "installed pending C6; C6 has landed and openai is gone",
        ),
        (
            "test_bot_ross_config.py",
            "the 3.12 .venv still",
            "test_coloredlogs_import_is_gone's comment claimed coloredlogs was "
            "still installed pending C6; C6 has landed and coloredlogs is gone",
        ),
        (
            "message_links.py",
            "discord.py 2.3.2's Thread.permissions_for",
            "needs_thread_membership_check's docstring pinned the security "
            "claim to the pre-bump discord.py version instead of the pin",
        ),
        (
            "test_message_links.py",
            "discord.py 2.3.2's Thread.permissions_for",
            "NeedsThreadMembershipCheckTest's docstring pinned the security "
            "claim to the pre-bump discord.py version instead of the pin",
        ),
    ]

    def test_stale_phrases_do_not_reappear(self):
        for relpath, phrase, why in self.CASES:
            path = os.path.join(self.REPO_ROOT, relpath)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn(
                phrase, content,
                f"{relpath} still contains the stale phrase {phrase!r} -- {why}",
            )


class NoStaleAtImportClaimsTest(unittest.TestCase):
    """Guards comments/docs that described env-var parsing (BOT_TIMEZONE,
    DAILY_IMAGE_ENABLED/DAILY_IMAGE_CHANNEL_ID) or library seeding
    (_seed_daily_schedule) as happening "at import". Since C1, none of
    load_config()'s body -- and none of the three _seed_*() calls -- runs at
    import time; they only run when main() calls them, from the
    `if __name__ == "__main__":` guard. "At import" is therefore a literally
    false timing claim even though the protective behavior itself (never
    raise on a bad env var; seed the working copy before it's read) still
    holds at startup. Each phrase below must never reappear verbatim; a
    similar comment reintroduced later must say "at startup"/"from main()",
    not "at import"/"at module bottom"."""

    REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

    # (relative path, forbidden phrase, why it's now false)
    CASES = [
        (
            "bot_ross.py",
            "falls back to UTC rather than crashing at import",
            "BOT_TIMEZONE is parsed inside load_config(), called from main() -- "
            "not at import time",
        ),
        (
            "bot_ross.py",
            "typo'd env var can't crash the bot at import",
            "DAILY_IMAGE_ENABLED/DAILY_IMAGE_CHANNEL_ID are parsed inside "
            "load_config(), called from main() -- not at import time",
        ),
        (
            "daily_schedule.py",
            "not crash the bot at import/startup",
            "parse_bool/parse_channel_id/get_zone are pure helpers called from "
            "load_config() at startup, never at import time",
        ),
        (
            "daily_schedule.py",
            "pasted mention can never crash the bot at import",
            "parse_channel_id is called from load_config() at startup, never "
            "at import time",
        ),
        (
            "CLAUDE.md",
            "seeded via `_seed_daily_schedule()` at module bottom",
            "_seed_daily_schedule() is called from main(), reachable only via "
            "the `if __name__ == \"__main__\":` guard -- nothing at module "
            "bottom seeds anything since C1",
        ),
        (
            "CLAUDE.md",
            "rather than crashing at import",
            "the BOT_TIMEZONE env-var table row paraphrases the same claim as "
            "the bot_ross.py CASE above with different wording ('at startup' "
            "vs. 'at import') -- this guard was written to catch it but only "
            "covered bot_ross.py, letting the sibling doc occurrence sail "
            "past; load_config() (which parses BOT_TIMEZONE) runs from "
            "main(), never at import time",
        ),
    ]

    def test_stale_phrases_do_not_reappear(self):
        for relpath, phrase, why in self.CASES:
            path = os.path.join(self.REPO_ROOT, relpath)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn(
                phrase, content,
                f"{relpath} still contains the stale phrase {phrase!r} -- {why}",
            )


class NoStaleDailySchedulerCoverageClaimsTest(unittest.TestCase):
    """Guards two CLAUDE.md claims about DailyCommandsValidateBeforeSaveTest
    and the daily-scheduler wiring's behavioral coverage, each banned for a
    different reason -- neither ban is a claim that CLAUDE.md ever said this
    verbatim in the past; it's a forward-looking guard against the phrase
    being (re)introduced:

    1. The class's ordering invariant is validate-BEFORE-save (see its
       docstring and test_validate_slot_precedes_save_in_add_update_toggle).
       "write-before-validate" names the OPPOSITE, invalid ordering, so it is
       banned outright -- a future rewrite of the CLAUDE.md description must
       never invert the invariant this way, even though (checked via `git log
       -S` against this repo's history) that exact phrase never actually
       shipped in a committed CLAUDE.md.
    2. `_run_due_daily_slots` (the scheduler's per-tick channel-resolve /
       due-slot / mark-fired query) has no behavioral test either -- only an
       AST call-presence check -- so a claim that `_daily_scheduler_loop` is
       the ONLY untested piece of the daily-scheduler wiring is false. This
       one WAS found overclaiming in a draft of the C8 docs pass (spec E5's
       proposed wording) and rejected before it ever reached a commit."""

    REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

    # (relative path, forbidden phrase, why it's now false)
    CASES = [
        (
            "CLAUDE.md",
            "the write-before-validate ordering",
            "DailyCommandsValidateBeforeSaveTest's invariant is "
            "validate-before-save (validate_slot must precede the save "
            "call) -- 'write-before-validate' names the opposite ordering",
        ),
        (
            "CLAUDE.md",
            "only the `_daily_scheduler_loop` heartbeat itself runs untested",
            "_run_due_daily_slots has no behavioral coverage either (only "
            "an AST reload-fresh check) -- 'only' overclaims that "
            "_daily_scheduler_loop is the sole untested piece",
        ),
    ]

    def test_stale_phrases_do_not_reappear(self):
        for relpath, phrase, why in self.CASES:
            path = os.path.join(self.REPO_ROOT, relpath)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn(
                phrase, content,
                f"{relpath} still contains the stale phrase {phrase!r} -- {why}",
            )


class DocClassFileAttributionTest(unittest.TestCase):
    """Guards a mis-attribution found in review of the C8 docs pass: CLAUDE.md's
    'Startup and Testability' section named LoadConfigGlobalListTest right after
    citing test_bot_ross_config.py's `_CONFIG_GLOBALS` (implying, wrongly, that
    the class lives in that file too) and cited MainBodyOrderingTest with no
    file at all -- both classes actually live in test_bot_ross_source.py.
    DocsTruthTest.test_test_classes_named_in_docs_exist only checks that a
    mentioned class exists SOMEWHERE in the repo, never which file the doc
    claims it lives in, so a mis-attribution like this sails past that check
    silently -- worse, ImportSafetyTest is genuinely defined in BOTH
    test_bot_ross_commands.py and test_bot_ross_config.py, so "just check the
    name exists" would not even catch an ImportSafetyTest mis-citation. A
    maintainer told the wrong file greps that file, finds nothing, and
    concludes the guard was deleted. This test checks two things for each
    (class, file) pair: the doc actually attributes the class to that file
    (the exact phrasing CLAUDE.md uses), and the class really is defined
    there -- so both a doc regression and a future code move away from that
    file are caught."""

    REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

    # (class name, file CLAUDE.md must attribute it to, via the phrasing
    # "`ClassName` in `file.py`"). Add an entry whenever CLAUDE.md names a
    # test class alongside the specific file it lives in.
    ATTRIBUTIONS = [
        ("LoadConfigGlobalListTest", "test_bot_ross_source.py"),
        ("MainBodyOrderingTest", "test_bot_ross_source.py"),
    ]

    def test_class_attributions_match_definitions(self):
        claude_md_path = os.path.join(self.REPO_ROOT, "CLAUDE.md")
        with open(claude_md_path, "r", encoding="utf-8") as f:
            claude_md = f.read()
        for class_name, expected_file in self.ATTRIBUTIONS:
            with self.subTest(class_name=class_name, expected_file=expected_file):
                # The doc must actually say the class lives in this file --
                # not merely mention the class name somewhere in the doc.
                attribution = f"`{class_name}` in `{expected_file}`"
                self.assertIn(
                    attribution, claude_md,
                    f"CLAUDE.md does not attribute {class_name} to {expected_file} "
                    f"via the phrase {attribution!r} -- either the attribution was "
                    "dropped/reworded, or it points at the wrong file",
                )
                # And the attribution must actually be true: the class must be
                # defined in the file the doc says it's in, not merely exist
                # somewhere in the repo (some class names -- e.g. ImportSafetyTest
                # -- are defined in more than one file, so "exists somewhere" is
                # not strong enough to back a specific-file claim).
                target_path = os.path.join(self.REPO_ROOT, expected_file)
                with open(target_path, "r", encoding="utf-8") as f:
                    target_source = f.read()
                self.assertRegex(
                    target_source, rf"(?m)^class {re.escape(class_name)}\b",
                    f"CLAUDE.md attributes {class_name} to {expected_file}, but no "
                    f"such class is defined there -- the class moved, was renamed, "
                    "or the attribution was never true",
                )


class NoStaleDocPointerDirectionTest(unittest.TestCase):
    """Guards a mis-pointed cross-reference found during the C8 docs pass:
    a sentence inside 'Daily Image of the Day' pointed a reader at "the
    canonical gate line in Startup and Testability below". `## Startup and
    Testability` appears EARLIER in CLAUDE.md than `## Daily Image of the
    Day`, so the section being pointed at is above the pointer, not below
    it -- a reader following "below" would scroll to the end of the file,
    find no gate line there (Key Dependencies/Notes are the only sections
    after Daily Image of the Day), and could wrongly conclude the canonical
    gate line was deleted."""

    REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

    def test_startup_and_testability_pointer_points_the_right_direction(self):
        path = os.path.join(self.REPO_ROOT, "CLAUDE.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn(
            "Startup and Testability below", content,
            "CLAUDE.md still points a reader at 'Startup and Testability "
            "below', but that section appears earlier in the file than "
            "this pointer -- it should say 'above'",
        )


class DocsTruthTest(unittest.TestCase):
    """Docs-consistency guards. CLAUDE.md is a build artifact like the Dockerfile:
    this file's charter (source/artifact-shape properties) is exactly where checks
    on it belong. Each method pins one documentation-drift failure mode that has
    already happened at least once in this repo's history."""

    LIVE_API_SCRIPTS = frozenset({"test_image.py", "test_remix.py"})
    DOC_FILES = ("CLAUDE.md", "README.md")
    REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def _read(path):
        # A missing/unreadable doc file is itself a defect -- let
        # FileNotFoundError/OSError propagate loudly rather than skipping past it.
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @classmethod
    def _repo_test_modules(cls):
        # Repo root only, no recursion -- never descends into .venv/data/__pycache__.
        # The live-API scripts are real test_*.py files but are deliberately never
        # part of the local gate (they hit the live OpenAI API and spend real money).
        live_stems = {os.path.splitext(n)[0] for n in cls.LIVE_API_SCRIPTS}
        return {
            os.path.splitext(n)[0]
            for n in os.listdir(cls.REPO_ROOT)
            if n.startswith("test_") and n.endswith(".py")
        } - live_stems

    @staticmethod
    def _gate_module_lists(text):
        # [ \t] NOT \s: \s would glue tokens across a newline and turn two
        # adjacent single-module run lines into a phantom "gate line" spanning
        # both. Returns every invocation found, including single-module ones --
        # callers filter on len(...) >= 2 to isolate actual multi-module gates.
        return [
            match.split()
            for match in re.findall(r"python -m unittest((?:[ \t]+test_\w+)+)", text)
        ]

    @classmethod
    def _defined_test_classes(cls):
        # Raw-text regex, not import: importing test_image.py/test_remix.py must
        # never happen from inside the gate. Includes the live-API scripts --
        # classes defined there still legitimately exist and may legitimately be
        # named in the docs.
        classes = set()
        for name in os.listdir(cls.REPO_ROOT):
            if name.startswith("test_") and name.endswith(".py"):
                source = cls._read(os.path.join(cls.REPO_ROOT, name))
                classes.update(re.findall(r"^class (\w+)\b", source, re.MULTILINE))
        return classes

    def test_documented_gate_lines_list_every_test_module(self):
        # Failure mode prevented: a doc's gate line silently drifting out of
        # sync with the repo's actual test_*.py files -- this already happened
        # once (test_bot_ross_config.py was added by C1 but omitted from the
        # documented gate, so C4 broke that module undetected).
        expected = self._repo_test_modules()
        gate_lines = []
        for name in self.DOC_FILES:
            text = self._read(os.path.join(self.REPO_ROOT, name))
            for module_list in self._gate_module_lists(text):
                if len(module_list) >= 2:
                    gate_lines.append((name, module_list))
        self.assertTrue(
            gate_lines,
            f"no multi-module `python -m unittest ...` gate line found in {self.DOC_FILES!r} "
            "-- if the documented gate was deleted outright, the check below would "
            "vacuously pass, so its mere existence is asserted here first",
        )
        for name, module_list in gate_lines:
            got = set(module_list)
            with self.subTest(doc=name, line=module_list):
                missing = sorted(expected - got)
                extra = sorted(got - expected)
                self.assertEqual(
                    got, expected,
                    f"{name}'s gate line is out of sync with the repo's test_*.py files -- "
                    f"missing from the doc: {missing}, stale/extra in the doc: {extra} "
                    "-- a doc naming a deleted module is as wrong as one omitting a new one",
                )

    def test_test_classes_named_in_docs_exist(self):
        # Failure mode prevented: a doc crediting a class that was renamed or
        # retired -- both already happened (a renamed-in-doc-only class, and a
        # retired class whose name lingered in prose after its test was deleted).
        defined = self._defined_test_classes()
        mentioned = set()
        for name in self.DOC_FILES:
            text = self._read(os.path.join(self.REPO_ROOT, name))
            mentioned.update(re.findall(r"\b([A-Z][A-Za-z0-9]*Test)\b", text))
        self.assertTrue(
            mentioned,
            f"no *Test class name found mentioned across {self.DOC_FILES!r} -- the "
            "scanning regex likely broke rather than the docs genuinely naming none",
        )
        phantom = sorted(mentioned - defined)
        self.assertLessEqual(
            mentioned, defined,
            f"{self.DOC_FILES!r} mention test class name(s) that don't exist in any "
            f"repo test_*.py file: {phantom} -- renamed or retired without updating the docs",
        )

    def test_no_stale_unimportability_claims(self):
        # Failure mode prevented: the next copy-pasted module docstring
        # reintroducing the false "importing bot_ross always starts the bot"
        # rationale -- it already propagated to seven files once. Each phrase
        # below is assembled by adjacent-string concatenation so the contiguous
        # phrase never appears literally in THIS file's own source -- otherwise
        # this very check would trip on itself the moment it scans its own file.
        # For the same reason, no assertion message anywhere in this method may
        # spell out the trigger phrases verbatim either.
        stale_phrases = [
            "cannot be imported" " under test",
            "can't be imported" " under test",
            "can never be imported" " under test",
            "isn't possible" " under test",
            "unimportable" " under test",
        ]
        # Catches both the plain and backtick-quoted markdown form of the claim
        # that a module's source concludes with a call that starts the bot.
        # Deliberately does NOT match bot_ross.py's true statement that
        # bot.run() returns once _graceful_shutdown closes the bot, since that
        # sentence never reads "ends in" immediately before it.
        stale_run_call_re = re.compile(r"ends in `?bot\.run\(\)")

        names = sorted(
            n for n in os.listdir(self.REPO_ROOT)
            if (n.endswith(".py") or n.endswith(".md"))
            and os.path.isfile(os.path.join(self.REPO_ROOT, n))
        )
        for name in names:
            text = self._read(os.path.join(self.REPO_ROOT, name))
            for phrase in stale_phrases:
                with self.subTest(file=name, phrase=phrase):
                    self.assertNotIn(
                        phrase, text,
                        f"{name} still contains a stale claim (phrase {phrase!r}) that "
                        "bot_ross.py can't be exercised without starting the bot -- false "
                        "since load_config()/main() landed; keep the module's "
                        "dependency-free POINT, fix the reasoning",
                    )
            with self.subTest(file=name, phrase="module concludes with a bot-starting call"):
                self.assertNotRegex(
                    text, stale_run_call_re,
                    f"{name} still claims a module's source concludes with a call that "
                    "starts the bot at import time -- false since load_config()/main() "
                    "landed; keep the module's dependency-free POINT, fix the reasoning",
                )

    def test_dockerfile_test_stage_copies_files_this_class_reads(self):
        # Failure mode prevented: this class opens DOC_FILES by name via _read(),
        # which lets FileNotFoundError propagate rather than skipping past a
        # missing doc -- but the Docker test stage's COPY list is maintained by
        # hand and can drift out of sync with what the tests actually read. That
        # already happened: DocsTruthTest was added without adding CLAUDE.md to
        # the test stage's COPY line, so `docker build --target test .` ERRORed
        # on both doc-reading methods while the host gate stayed green and never
        # noticed. This pins the two file sets together so the next doc file a
        # test starts reading can't silently miss the same COPY line.
        dockerfile_path = os.path.join(self.REPO_ROOT, "Dockerfile")
        text = self._read(dockerfile_path)
        stage_marker = re.search(r"^FROM\s+\S+\s+AS\s+test\s*$", text, re.MULTILINE)
        self.assertIsNotNone(
            stage_marker, "Dockerfile has no `FROM ... AS test` stage to inspect",
        )
        test_stage_text = text[stage_marker.end():]
        # Dockerfile line-continuations (trailing `\`) join a COPY's source list
        # across multiple physical lines -- collapse them before scanning so a
        # wrapped COPY instruction isn't mistaken for several short ones.
        joined = re.sub(r"\\\n[ \t]*", " ", test_stage_text)
        copied = set()
        for args in re.findall(r"^COPY[ \t]+(.+)$", joined, re.MULTILINE):
            copied.update(args.split())
        copied.discard("./")
        missing = [name for name in self.DOC_FILES if name not in copied]
        self.assertFalse(
            missing,
            f"Dockerfile's test stage never COPYs {missing} into the image, but "
            f"this class's DOC_FILES {self.DOC_FILES!r} are read by name via "
            "_read() -- the container gate would FileNotFoundError even though "
            "the host gate (which reads straight off the checked-out repo) stays "
            "green",
        )


    def test_dockerfile_gate_line_lists_every_test_module(self):
        # Failure mode prevented: the CONTAINER's gate drifting out of sync with
        # the repo. test_documented_gate_lines_list_every_test_module above scans
        # only DOC_FILES, and the COPY guard below checks only which files reach
        # the image -- neither looks at the `RUN python -m unittest ...` line,
        # which is the gate that actually decides whether `docker build --target
        # test .` fails. A new test_foo.py could be added, both docs updated, and
        # COPY updated, while the RUN line silently never runs it: the build would
        # stay green while the container gate quietly stopped being a gate.
        #
        # This is the same class of drift that already bit the documented gate
        # (test_bot_ross_config.py was added by C1, omitted from the doc gate,
        # and C4 then broke that module undetected) -- the Dockerfile's RUN line
        # is simply the copy of that list nothing was checking yet.
        dockerfile_path = os.path.join(self.REPO_ROOT, "Dockerfile")
        text = self._read(dockerfile_path)
        # Same trailing-backslash normalization the COPY guard uses: the RUN gate
        # wraps across four physical lines, and without collapsing them the regex
        # would see only the first line's modules and pass while the rest went
        # unchecked -- a false green on the exact thing being verified.
        joined = re.sub(r"\\\n[ \t]*", " ", text)
        gate_lines = [m for m in self._gate_module_lists(joined) if len(m) >= 2]
        self.assertTrue(
            gate_lines,
            "Dockerfile has no multi-module `python -m unittest ...` line -- if the "
            "container gate were deleted outright the check below would vacuously "
            "pass, so its existence is asserted first",
        )
        expected = self._repo_test_modules()
        for module_list in gate_lines:
            got = set(module_list)
            with self.subTest(line=module_list):
                self.assertEqual(
                    got, expected,
                    "the Dockerfile's `RUN python -m unittest` gate is out of sync "
                    f"with the repo's test_*.py files -- missing: {sorted(expected - got)}, "
                    f"stale/extra: {sorted(got - expected)}. This line is the container's "
                    "real gate; a module missing here is a module `docker build "
                    "--target test .` will never run.",
                )


if __name__ == "__main__":
    unittest.main()
