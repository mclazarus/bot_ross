"""Unit tests for Discord message-link parsing/classification/capping logic.

These exercise message_links.py in isolation (no Discord/OpenAI, no I/O -- the
module itself performs none): host-matching edge cases (including the two
lookalike-host SECURITY tests, 16 and 17), stripping's whitespace/newline
handling, guild classification (including the cross-guild/DM SECURITY tests,
40 and 42), and the dedupe+cap logic. Mirrors test_macros.py's structure and
comment density; module docstring on message_links.py itself carries the
pre-filter-vs-authoritative-checks security contract this file's tests probe.

Also covers two pure helpers that exist specifically so logic that would
otherwise be inline (and therefore untestable) in bot_ross.py's
_resolve_linked_images can be unit tested instead: format_skip_notes (the
S2-S6 notice-line ORDER contract) and needs_thread_membership_check (the
decision of when a private-thread membership check must run, closing the gap
where discord.py's Thread.permissions_for ignores private-thread membership).

Run from the repo root:  python -m unittest test_message_links -v
"""

import unittest

from message_links import (
    MAX_LINKS,
    MessageLink,
    classify_link,
    find_message_links,
    format_skip_notes,
    limit_links,
    needs_thread_membership_check,
    strip_message_links,
)

# Terse constructor for test cases: builds a MessageLink with a `raw` that matches
# what find_message_links would actually produce for a plain (unwrapped, no query)
# link with these ids, so tests that only care about the id triple don't have to
# spell out a URL by hand every time.
L = lambda g, c, m: MessageLink(g, c, m, raw=f"https://discord.com/channels/{g if g is not None else '@me'}/{c}/{m}")

# Realistic 19-digit Discord snowflakes -- chosen specifically because they exceed
# 2**53 (~9.007e15), the largest integer a double-precision float can represent
# exactly. A float-based id parse (e.g. accidentally routing through json or some
# other float-producing path) would silently corrupt one of these; int() never
# does. See FindMessageLinksTest.test_snowflake_ids_parse_exactly and
# ClassifyLinkTest's snowflake case.
SF_GUILD = 1109237972199157841
SF_CHANNEL = 1109238151119926322
SF_MESSAGE = 1204476170907750441


class FindMessageLinksTest(unittest.TestCase):
    def test_basic_link_mid_sentence(self):
        # Case 1: ids are ints, raw is the exact matched substring.
        links = find_message_links("see https://discord.com/channels/111/222/333 there")
        self.assertEqual(
            links,
            [MessageLink(111, 222, 333, "https://discord.com/channels/111/222/333")],
        )

    def test_legacy_discordapp_host(self):
        # Case 2.
        links = find_message_links("https://discordapp.com/channels/111/222/333")
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0].guild_id, links[0].channel_id, links[0].message_id), (111, 222, 333))

    def test_ptb_subdomain(self):
        # Case 3: PTB (Public Test Build) client host.
        links = find_message_links("https://ptb.discord.com/channels/111/222/333")
        self.assertEqual(len(links), 1)

    def test_canary_subdomain(self):
        # Case 4: Canary client host.
        links = find_message_links("https://canary.discord.com/channels/111/222/333")
        self.assertEqual(len(links), 1)

    def test_www_subdomain(self):
        # Case 5.
        links = find_message_links("https://www.discord.com/channels/111/222/333")
        self.assertEqual(len(links), 1)

    def test_ptb_and_legacy_host_compose(self):
        # Case 6: the subdomain prefix and the legacy discordapp.com host both apply
        # at once.
        links = find_message_links("https://ptb.discordapp.com/channels/111/222/333")
        self.assertEqual(len(links), 1)

    def test_http_scheme_tolerated(self):
        # Case 7: http (not just https) is tolerated -- Discord itself always emits
        # https, but a manually-typed or old-copied link might not be.
        links = find_message_links("http://discord.com/channels/111/222/333")
        self.assertEqual(len(links), 1)

    def test_case_insensitive_host_and_path(self):
        # Case 8: hosts are case-insensitive per URL semantics; the server-side
        # authoritative checks in bot_ross.py stay in force regardless of casing.
        links = find_message_links("HTTPS://DISCORD.COM/CHANNELS/111/222/333")
        self.assertEqual(len(links), 1)

    def test_no_embed_wrapper(self):
        # Case 9: the '<>' wrapper is INSIDE the match, so raw includes it exactly.
        links = find_message_links("<https://discord.com/channels/111/222/333>")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].raw, "<https://discord.com/channels/111/222/333>")

    def test_query_string_consumed(self):
        # Case 10: the query is part of the match, so stripping leaves nothing of
        # it behind in the cleaned prompt.
        links = find_message_links("https://discord.com/channels/111/222/333?jump=1")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].message_id, 333)
        self.assertTrue(links[0].raw.endswith("?jump=1"))

    def test_trailing_slash_tolerated(self):
        # Case 11.
        links = find_message_links("https://discord.com/channels/111/222/333/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].message_id, 333)

    def test_trailing_slash_then_query(self):
        # Case 12.
        links = find_message_links("https://discord.com/channels/111/222/333/?jump=1")
        self.assertEqual(len(links), 1)

    def test_wrapped_with_query_still_consumes_closing_bracket(self):
        # Case 13: the query character class excludes '>', so the wrapper's closing
        # '>' is still consumed as part of the match rather than left dangling.
        links = find_message_links("<https://discord.com/channels/111/222/333?jump=1>")
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0].raw.endswith(">"))

    def test_trailing_period_not_swallowed(self):
        # Case 14: sentence-ending punctuation must never become part of the id.
        links = find_message_links("see https://discord.com/channels/111/222/333.")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].message_id, 333)
        self.assertFalse(links[0].raw.endswith("."))

    def test_trailing_parenthesis_not_swallowed(self):
        # Case 15.
        links = find_message_links("(see https://discord.com/channels/111/222/333)")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].message_id, 333)
        self.assertFalse(links[0].raw.endswith(")"))

    def test_lookalike_host_does_not_match(self):
        # Case 16 -- SECURITY: the host is anchored immediately after "https?://",
        # so a domain that merely CONTAINS "discord.com" as a substring, or shares
        # its name under a different TLD, must never be treated (or, worse,
        # silently stripped from the prompt) as a real Discord link.
        self.assertEqual(find_message_links("https://notdiscord.com/channels/1/2/3"), [])

    def test_suffixed_domain_does_not_match(self):
        # Case 17 -- SECURITY: "discord.com.evil.com" contains "discord.com" as a
        # PREFIX of a longer domain the attacker actually controls. The character
        # immediately after "discord.com" must be "/channels/", not ".", so this
        # suffix attack cannot match.
        self.assertEqual(find_message_links("https://discord.com.evil.com/channels/1/2/3"), [])

    def test_invite_domain_does_not_match(self):
        # Case 18: discord.gg is the invite-link domain, a different app entirely.
        self.assertEqual(find_message_links("https://discord.gg/channels/1/2/3"), [])

    def test_two_segment_channel_link_does_not_match(self):
        # Case 19: a channel link (guild/channel, no message id) must not match
        # with some trailing garbage mistaken for the message id.
        self.assertEqual(find_message_links("https://discord.com/channels/111/222"), [])

    def test_non_numeric_non_at_me_guild_segment_does_not_match(self):
        # Case 20.
        self.assertEqual(find_message_links("https://discord.com/channels/abc/222/333"), [])

    def test_at_me_dm_link(self):
        # Case 21: '@me' is the DM-link form; guild_id parses to None.
        links = find_message_links("https://discord.com/channels/@me/222/333")
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0].guild_id)
        self.assertEqual(links[0].channel_id, 222)
        self.assertEqual(links[0].message_id, 333)

    def test_two_links_in_order(self):
        # Case 22: textual order is preserved, wrapped and unwrapped alike.
        text = "A https://discord.com/channels/1/2/3 and <https://discord.com/channels/4/5/6>"
        links = find_message_links(text)
        self.assertEqual(len(links), 2)
        self.assertEqual((links[0].guild_id, links[0].channel_id, links[0].message_id), (1, 2, 3))
        self.assertEqual((links[1].guild_id, links[1].channel_id, links[1].message_id), (4, 5, 6))

    def test_duplicate_link_both_occurrences_preserved(self):
        # Case 23: find_message_links must NOT dedupe -- strip_message_links needs
        # every occurrence removed; deduping is limit_links' job, done later.
        text = "https://discord.com/channels/1/2/3 and again https://discord.com/channels/1/2/3"
        links = find_message_links(text)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0], links[1])

    def test_empty_string(self):
        # Case 24.
        self.assertEqual(find_message_links(""), [])

    def test_none_fails_open(self):
        # Case 25: &remix can be invoked with prompt=None; this must never raise.
        self.assertEqual(find_message_links(None), [])

    def test_snowflake_ids_parse_exactly(self):
        # Case 26: 19-digit real-world-shaped snowflakes come through as exact
        # Python ints (see the SF_* module constants' docstring for why this
        # matters -- they exceed 2**53).
        text = f"https://discord.com/channels/{SF_GUILD}/{SF_CHANNEL}/{SF_MESSAGE}"
        links = find_message_links(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].guild_id, int(str(SF_GUILD)))
        self.assertEqual(links[0].channel_id, int(str(SF_CHANNEL)))
        self.assertEqual(links[0].message_id, int(str(SF_MESSAGE)))

    def test_documented_fourth_segment_quirk(self):
        # Case 27: a fourth numeric path segment is NOT part of the regex's model
        # of a message link (real Discord links never have one) -- it matches the
        # first three ids and leaves the fourth behind as ordinary prose. We
        # document this rather than contort the regex to reject a case that never
        # occurs with genuine Discord-generated links.
        links = find_message_links("https://discord.com/channels/1/2/3/4")
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0].guild_id, links[0].channel_id, links[0].message_id), (1, 2, 3))
        cleaned, _ = strip_message_links("https://discord.com/channels/1/2/3/4")
        self.assertIn("4", cleaned)


class StripMessageLinksTest(unittest.TestCase):
    def test_no_links_verbatim_passthrough(self):
        # Case 28: the SAME string, internal double spaces and all -- this is the
        # same untouched-passthrough guarantee pipes.split_pipeline makes for a
        # promptless '|'; whitespace normalization is only earned by an actual
        # removal.
        text = "make it  neon   please"
        cleaned, links = strip_message_links(text)
        self.assertEqual(cleaned, text)
        self.assertIs(cleaned, text)  # byte-for-byte the same object, not just equal
        self.assertEqual(links, [])

    def test_none_input(self):
        # Case 29.
        self.assertEqual(strip_message_links(None), ("", []))

    def test_mid_sentence_single_space_left_behind(self):
        # Case 30: exactly one space where the link was, no double space.
        cleaned, links = strip_message_links("make https://discord.com/channels/1/2/3 neon")
        self.assertEqual(cleaned, "make neon")
        self.assertEqual(len(links), 1)

    def test_prompt_that_is_only_a_link(self):
        # Case 31: empty cleaned text -- the caller (bot_ross.py) maps this to
        # None, landing on the existing image-only remix path.
        cleaned, links = strip_message_links("https://discord.com/channels/1/2/3")
        self.assertEqual(cleaned, "")
        self.assertEqual(len(links), 1)

    def test_only_a_wrapped_link(self):
        # Case 32: no orphan '<' or '>' left over.
        cleaned, links = strip_message_links("<https://discord.com/channels/1/2/3>")
        self.assertEqual(cleaned, "")
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)
        self.assertEqual(len(links), 1)

    def test_newlines_preserved(self):
        # Case 33: the link's own line becomes an empty line -- the total line
        # count must not change (a multi-line prompt must not collapse to one
        # line).
        text = "look at this\nhttps://discord.com/channels/1/2/3\nmake it neon"
        cleaned, links = strip_message_links(text)
        self.assertEqual(cleaned, "look at this\n\nmake it neon")
        self.assertEqual(cleaned.count("\n"), text.count("\n"))
        self.assertEqual(len(links), 1)

    def test_two_links_one_line(self):
        # Case 34.
        text = "A <https://discord.com/channels/1/2/3> B https://discord.com/channels/4/5/6 C"
        cleaned, links = strip_message_links(text)
        self.assertEqual(cleaned, "A B C")
        self.assertEqual(len(links), 2)

    def test_leading_link(self):
        # Case 35: the line's leading space (left behind by the removed link) is
        # stripped.
        cleaned, links = strip_message_links("https://discord.com/channels/1/2/3 make it art")
        self.assertEqual(cleaned, "make it art")

    def test_tabs_collapse_to_single_space(self):
        # Case 36: '[ \t]+' runs (here, tabs on both sides of the wrapped link)
        # collapse to one space each.
        cleaned, links = strip_message_links("a\t<https://discord.com/channels/1/2/3>\tb")
        self.assertEqual(cleaned, "a b")

    def test_consistency_with_find_message_links(self):
        # Case 37: for a mixed multi-link input, the second element of
        # strip_message_links' return is exactly what find_message_links would
        # produce on its own -- same list, same order, duplicates included.
        text = (
            "A https://discord.com/channels/1/2/3 and "
            "<https://discord.com/channels/4/5/6> and again "
            "https://discord.com/channels/1/2/3"
        )
        self.assertEqual(strip_message_links(text)[1], find_message_links(text))

    def test_trailing_punctuation_survives_stripping(self):
        # Case 38: the comma is user prose, not part of the link match, so it's
        # deliberately preserved -- imperfect (a slightly odd "see , ok"), but the
        # rule is simple and predictable: never silently eat characters that
        # weren't part of the matched link.
        cleaned, links = strip_message_links("see https://discord.com/channels/1/2/3, ok")
        self.assertEqual(cleaned, "see , ok")
        self.assertEqual(len(links), 1)


class ClassifyLinkTest(unittest.TestCase):
    def test_same_guild_is_ok(self):
        # Case 39.
        self.assertEqual(classify_link(L(111, 2, 3), 111), "ok")

    def test_cross_guild(self):
        # Case 40 -- SECURITY (pre-filter layer): a link whose guild id doesn't
        # match the invoking guild is classified "cross_guild" here so it's kept
        # from ever reaching a network fetch. This function is NOT the security
        # boundary itself -- the real authorization lives in bot_ross.py's
        # _resolve_linked_images, checked against the RESOLVED channel object.
        self.assertEqual(classify_link(L(999, 2, 3), 111), "cross_guild")

    def test_dm_link_in_a_guild(self):
        # Case 41: an '@me' link pasted while invoking from a guild.
        link = MessageLink(None, 2, 3, "https://discord.com/channels/@me/2/3")
        self.assertEqual(classify_link(link, 111), "dm")

    def test_guild_link_with_no_current_guild(self):
        # Case 42 -- SECURITY: a guild-shaped link pasted in a DM with the bot.
        # The authoritative guild checks in bot_ross.py cannot run outside a
        # guild at all, so this must be refused rather than silently let through.
        self.assertEqual(classify_link(L(111, 2, 3), None), "no_guild")

    def test_dm_precedence_over_no_guild(self):
        # Case 43: when BOTH conditions apply (an '@me' link, invoked outside a
        # guild), the more specific diagnosis ("dm") wins -- the dm check runs
        # strictly before the no_guild check.
        link = MessageLink(None, 2, 3, "https://discord.com/channels/@me/2/3")
        self.assertEqual(classify_link(link, None), "dm")

    def test_snowflake_exact_integer_comparison(self):
        # Case 44: exact match on realistic 19-digit ids is "ok"; a same-length
        # id that's off by one is "cross_guild" -- proves this is exact integer
        # comparison, not float rounding (which could silently equate two
        # different 19-digit ids that differ only in low-order digits).
        self.assertEqual(classify_link(L(SF_GUILD, 2, 3), SF_GUILD), "ok")
        self.assertEqual(classify_link(L(SF_GUILD, 2, 3), SF_GUILD + 1), "cross_guild")


class LimitLinksTest(unittest.TestCase):
    def test_under_the_cap(self):
        # Case 45.
        a, b, c = L(1, 1, 1), L(1, 1, 2), L(1, 1, 3)
        kept, dropped = limit_links([a, b, c])
        self.assertEqual(kept, [a, b, c])
        self.assertEqual(dropped, [])

    def test_exactly_at_the_cap_is_inclusive(self):
        # Case 46: MAX_LINKS unique links, all kept -- the boundary is inclusive.
        links = [L(1, 1, i) for i in range(MAX_LINKS)]
        kept, dropped = limit_links(links)
        self.assertEqual(kept, links)
        self.assertEqual(dropped, [])

    def test_over_the_cap_keeps_first_four_in_order(self):
        # Case 47: 6 unique links -> first 4 kept, last 2 dropped, input order
        # preserved in both lists.
        links = [L(1, 1, i) for i in range(6)]
        kept, dropped = limit_links(links)
        self.assertEqual(kept, links[:4])
        self.assertEqual(dropped, links[4:])

    def test_duplicates_collapse_without_counting_as_dropped(self):
        # Case 48: pasting the same link twice is one link -- it must not burn the
        # cap, and must not produce a bogus "skipping 1" notice for a link that
        # was never actually skipped.
        a, b = L(1, 1, 1), L(1, 1, 2)
        kept, dropped = limit_links([a, a, b])
        self.assertEqual(kept, [a, b])
        self.assertEqual(dropped, [])

    def test_dedupe_key_is_the_full_id_triple(self):
        # Case 49: two links sharing a message_id but differing in channel_id are
        # DIFFERENT messages and must both be kept -- the dedupe key is the full
        # (guild_id, channel_id, message_id) triple, never message_id alone.
        a, b = L(1, 10, 99), L(1, 20, 99)
        kept, dropped = limit_links([a, b])
        self.assertEqual(kept, [a, b])
        self.assertEqual(dropped, [])

    def test_first_occurrence_wins_on_dedupe(self):
        # Case 50: two links sharing an id triple but differing in `raw` (one
        # wrapped in '<>') -- the FIRST one's raw survives in the kept result.
        a1 = MessageLink(1, 2, 3, "https://discord.com/channels/1/2/3")
        a2 = MessageLink(1, 2, 3, "<https://discord.com/channels/1/2/3>")
        kept, dropped = limit_links([a1, a2])
        self.assertEqual(kept, [a1])
        self.assertEqual(kept[0].raw, a1.raw)
        self.assertEqual(dropped, [])

    def test_max_links_zero_drops_everything(self):
        # Case 51.
        links = [L(1, 1, i) for i in range(3)]
        kept, dropped = limit_links(links, max_links=0)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, links)

    def test_max_links_negative_treated_as_zero(self):
        # Case 52: never raises, and never wraps around via negative-slice
        # semantics (links[:-1] would silently drop only the LAST element instead
        # of everything).
        links = [L(1, 1, i) for i in range(3)]
        kept, dropped = limit_links(links, max_links=-1)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, links)

    def test_empty_input(self):
        # Case 53.
        self.assertEqual(limit_links([]), ([], []))

    def test_purity_input_not_mutated(self):
        # Case 54.
        links = [L(1, 1, i) for i in range(6)]
        before = list(links)
        limit_links(links)
        self.assertEqual(links, before)


class FormatSkipNotesTest(unittest.TestCase):
    """format_skip_notes assembles the &remix S2-S6 notice lines. The single most
    important property is ORDER: the lines must come out S2 -> S3 -> S4 -> S5 -> S6
    regardless of which counts are nonzero or what order the caller happens to
    compute them in -- bot_ross.py's _resolve_linked_images resolves unfetchable/
    no_image/truncated only after the cap (S5) has already been applied, so if the
    ordering were left to inline append order the S5 line would land before S3/S4.
    """

    def test_all_buckets_come_out_in_S2_through_S6_order(self):
        # Every bucket nonzero at once -- this is the exact scenario the validator
        # flagged: same-guild links, one over the cap, one unfetchable. All five
        # lines must appear, in fixed order, regardless of kwarg order below.
        notes = format_skip_notes(
            truncated=True,
            over_cap=2,
            no_image=1,
            unfetchable=1,
            outside_guild=3,
            kept=4,
        )
        self.assertEqual(
            notes,
            [
                "Skipping 3 message link(s) from outside this server.",
                "Skipping 1 message link(s) I couldn't fetch — the message may be "
                "gone, or you may not have access to that channel.",
                "Skipping 1 linked message(s) with no image attached.",
                "That's a lot of links! Using the first 4 and skipping 2.",
                "Linked messages had more than 4 images — using the first 4.",
            ],
        )

    def test_only_S3_and_S5_present_still_S3_before_S5(self):
        # This is the validator's exact failing scenario: 6 same-guild links, one
        # of which points at a deleted message -- S3 (unfetchable) must precede S5
        # (over-cap), even though over-cap (dropped=2) was computed before the
        # resolution loop that produces unfetchable=1.
        notes = format_skip_notes(unfetchable=1, over_cap=2, kept=4)
        self.assertEqual(
            notes,
            [
                "Skipping 1 message link(s) I couldn't fetch — the message may be "
                "gone, or you may not have access to that channel.",
                "That's a lot of links! Using the first 4 and skipping 2.",
            ],
        )

    def test_no_buckets_nonzero_yields_empty_list(self):
        self.assertEqual(format_skip_notes(), [])

    def test_single_bucket_outside_guild_only(self):
        self.assertEqual(
            format_skip_notes(outside_guild=1),
            ["Skipping 1 message link(s) from outside this server."],
        )

    def test_single_bucket_no_image_only(self):
        self.assertEqual(
            format_skip_notes(no_image=2),
            ["Skipping 2 linked message(s) with no image attached."],
        )

    def test_single_bucket_truncated_only_uses_max_links(self):
        # max_links is threaded through to the S6 text, not hardcoded, so a
        # non-default cap (as tests elsewhere pass to limit_links) still renders
        # correctly.
        self.assertEqual(
            format_skip_notes(truncated=True, max_links=4),
            ["Linked messages had more than 4 images — using the first 4."],
        )

    def test_zero_counts_are_falsy_and_produce_no_line(self):
        # over_cap=0 with kept=4 must NOT produce an S5 line -- only a truthy
        # count triggers its bucket's line, regardless of what `kept` is.
        notes = format_skip_notes(kept=4, over_cap=0)
        self.assertEqual(notes, [])


class NeedsThreadMembershipCheckTest(unittest.TestCase):
    """needs_thread_membership_check decides whether _resolve_linked_images must
    run an extra channel.fetch_member(ctx.author.id) round-trip before trusting
    channel.permissions_for(ctx.author) for a linked message's channel -- because
    discord.py 2.3.2's Thread.permissions_for delegates straight to the PARENT
    channel and ignores private-thread membership entirely.
    """

    def test_private_thread_without_manage_threads_needs_check(self):
        # The exfiltration scenario: a private thread under a channel the
        # requester can see, but they aren't a member of the thread itself and
        # lack Manage Threads -- permissions_for alone would wrongly say yes.
        self.assertTrue(needs_thread_membership_check(True, False))

    def test_private_thread_with_manage_threads_skips_check(self):
        # Escape hatch matching Discord's real semantics: Manage Threads grants
        # access to a private thread without requiring explicit membership, so
        # the extra fetch_member round-trip would be redundant.
        self.assertFalse(needs_thread_membership_check(True, True))

    def test_public_channel_never_needs_check_regardless_of_manage_threads(self):
        # A non-private channel/thread is fully covered by permissions_for
        # already -- the extra check must never fire for it.
        self.assertFalse(needs_thread_membership_check(False, False))
        self.assertFalse(needs_thread_membership_check(False, True))


class ModuleConstantsTest(unittest.TestCase):
    def test_max_links_is_four(self):
        # Case 55: the cap the wiring, the S5/S6 notice strings, and the README
        # all quote -- a drive-by change here must trip this test.
        self.assertEqual(MAX_LINKS, 4)

    def test_message_link_fields(self):
        # Case 56: bot_ross.py's wiring destructures MessageLink by attribute
        # name (link.guild_id, link.channel_id, ...) -- a field rename must be a
        # deliberate, test-visible change.
        self.assertEqual(MessageLink._fields, ("guild_id", "channel_id", "message_id", "raw"))


if __name__ == "__main__":
    unittest.main()
