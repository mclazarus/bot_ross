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


if __name__ == "__main__":
    unittest.main()
