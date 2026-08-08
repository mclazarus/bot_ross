"""Pure logic for detecting and classifying Discord message links pasted into a
&remix prompt (e.g. "https://discord.com/channels/111/222/333").

Kept separate from bot_ross.py (which ends in bot.run() at import) so it can be
imported and unit tested without Discord/OpenAI secrets, mirroring release_image.py/
image_size.py/macros.py/pipe_chain.py. No I/O, no Discord import -- everything here is
string parsing and id comparison.

SECURITY CONTRACT -- read this before touching _resolve_linked_images in bot_ross.py:
`classify_link` compares ids found in attacker-controlled text; it is only a cheap
pre-filter. The guild id embedded in a pasted URL is whatever the person who pasted it
typed -- nothing stops them from putting the CURRENT server's guild id in a link that
actually points at a channel in a DIFFERENT server (Discord routes messages by channel
id, not by the guild id in the URL). The authoritative checks live in bot_ross.py
against the RESOLVED channel object and the INVOKING USER's own permissions: (a) the
resolved channel's `guild.id` equals the invoking `ctx.guild.id`, and (b) the invoking
user has `view_channel` + `read_message_history` on that resolved channel. A future
maintainer who trusts the URL's guild id as authorization turns &remix into an
image-exfiltration tool for private channels the requester cannot actually see.
"""

import re
from typing import List, NamedTuple, Optional, Tuple

# The cap on how many linked messages (post-dedupe) a single &remix invocation will
# resolve. Pinned as a constant -- the S5/S6 notice strings and the README both quote
# this number, so a drive-by change here must be deliberate.
MAX_LINKS = 4


class MessageLink(NamedTuple):
    """One parsed Discord message link. `guild_id` is None for an '@me' (DM) link.
    `raw` is the exact matched substring (including any '<>' wrapper and query
    string) so strip_message_links can remove precisely what was matched, nothing
    more, nothing less. Field order/names are part of the contract -- bot_ross.py
    destructures by attribute name, not position."""
    guild_id: Optional[int]
    channel_id: int
    message_id: int
    raw: str


# Regex design decisions, and the failure mode each one prevents:
#   - The host alternation sits IMMEDIATELY after "https?://", so "https://
#     notdiscord.com/..." and "https://discord.com.evil.com/channels/..." cannot
#     match -- a lookalike/suffixed host must never be treated (or silently
#     stripped from the prompt) as a real Discord link.
#   - The subdomain prefix is a closed list (ptb|canary|www), not a wildcard --
#     prevents an "evil-ptb.discord.com"-style host from slipping through.
#   - Every id is \d+, so trailing prose punctuation (".", ",", ")") is never
#     swallowed into the message id -- prevents fetching a nonexistent snowflake
#     and leaving a mangled cleaned prompt.
#   - The optional '<' / '>' Discord no-embed wrapper is INSIDE the match, so
#     stripping never leaves an orphan bracket in the prompt.
#   - The tolerated query string's character class excludes '>', so a wrapped link
#     with a query ("<...?jump=1>") still consumes its closing wrapper.
#   - re.IGNORECASE because URL hosts are case-insensitive by spec; the
#     authoritative server-side checks in bot_ross.py stay in force regardless, so
#     this can't be exploited by casing tricks -- it would just fail to match at
#     all, which is the safe direction.
# Documented quirk, tested but tolerated: "/channels/1/2/3/4" matches ids (1, 2, 3)
# and leaves "4" behind as ordinary prose. Real Discord message links never carry a
# fourth path segment, so this is a non-issue in practice; we document it rather
# than contort the regex to reject it.
MESSAGE_LINK_RE = re.compile(
    r"<?"                            # optional Discord no-embed wrapper, opening
    r"https?://"                     # scheme (http tolerated; Discord itself emits https)
    r"(?:(?:ptb|canary|www)\.)?"     # optional official client subdomain
    r"(?:discord|discordapp)\.com"   # host, anchored IMMEDIATELY after //
    r"/channels/"
    r"(@me|\d+)"                     # guild id, or the literal '@me' for DM links
    r"/(\d+)"                        # channel id
    r"/(\d+)"                        # message id
    r"/?"                            # tolerated trailing slash
    r"(?:\?[^\s<>]*)?"               # tolerated query string (never crosses whitespace or '>')
    r">?",                           # optional wrapper, closing
    re.IGNORECASE,
)


def find_message_links(text):
    """Return every MESSAGE_LINK_RE match in `text`, in textual (left-to-right)
    order, as a list[MessageLink]. Duplicates are preserved -- strip_message_links
    needs every occurrence to remove them all, deduping is limit_links' job, not
    this function's.

    `text` may be None or "" (fails open to [] rather than raising) so a caller
    invoking &remix with prompt=None never needs its own guard.

    '@me' parses to guild_id=None. Numeric ids parse via int(), so 19-20 digit
    Discord snowflakes come through exact -- Python ints have no size limit,
    unlike a float-based parse, which would silently corrupt them past 2**53.
    """
    if not text:
        return []
    links = []
    for m in MESSAGE_LINK_RE.finditer(text):
        guild_raw, channel_raw, message_raw = m.group(1), m.group(2), m.group(3)
        guild_id = None if guild_raw.lower() == "@me" else int(guild_raw)
        links.append(MessageLink(guild_id, int(channel_raw), int(message_raw), m.group(0)))
    return links


def strip_message_links(text):
    """Remove every message link from `text`, returning (cleaned, links) where
    `links == find_message_links(text)`.

    `text` may be None -> ("", []).

    No links found -> (text, []) with `text` returned VERBATIM, byte-for-byte --
    this feature must not perturb the overwhelming majority of &remix prompts that
    contain no link, the same untouched-passthrough guarantee pipe_chain.split_pipeline
    makes for a promptless '|'.

    Otherwise, every matched substring is removed, then EACH LINE independently has
    runs of spaces/tabs ([ \\t]+) collapsed to a single space and leading/trailing
    space stripped. Newlines are preserved exactly -- a link that occupied its own
    line becomes an empty line, not a merged one, so a multi-line prompt keeps its
    line count. Documented side effect: once any link was found (and thus this
    per-line normalization runs at all), unrelated double spaces elsewhere in the
    prompt also collapse -- acceptable, since the text is about to feed an image
    model, not be rendered verbatim.

    A prompt that is ONLY a link yields cleaned == "" -- callers map that to None,
    landing on the existing image-without-prompt remix path.
    """
    if text is None:
        return "", []
    links = find_message_links(text)
    if not links:
        return text, []
    without_links = text
    for link in links:
        without_links = without_links.replace(link.raw, "", 1)
    cleaned_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in without_links.split("\n")]
    return "\n".join(cleaned_lines), links


def classify_link(link, current_guild_id):
    """Classify a parsed link against the invoking context's guild id. Returns one
    of "ok" / "dm" / "no_guild" / "cross_guild", checked in exactly this order:

      1. link.guild_id is None            -> "dm"          (an '@me' link)
      2. current_guild_id is None         -> "no_guild"     (invoked outside a guild)
      3. link.guild_id == current_guild_id -> "ok"
      4. otherwise                        -> "cross_guild"

    The "dm" check runs FIRST so a DM link pasted inside a DM with the bot is
    diagnosed as "dm" (the more specific, more informative case), not "no_guild".

    Pure id comparison only -- never raises for any int/None combination. This is
    ONLY a cheap pre-filter over attacker-controlled URL text; see the module
    docstring for why it is not, and cannot be, the security boundary.
    """
    if link.guild_id is None:
        return "dm"
    if current_guild_id is None:
        return "no_guild"
    if link.guild_id == current_guild_id:
        return "ok"
    return "cross_guild"


def format_skip_notes(
    *,
    outside_guild=0,
    unfetchable=0,
    no_image=0,
    kept=0,
    over_cap=0,
    truncated=False,
    max_links=MAX_LINKS,
):
    """Assemble the S2-S6 &remix notice lines in the fixed order the spec mandates
    (S2 -> S3 -> S4 -> S5 -> S6), independent of the order the underlying counts
    happen to be computed in over in bot_ross.py's _resolve_linked_images.

    This lives here rather than as inline appends in bot_ross.py specifically so
    the ordering contract is unit-testable: bot_ross.py cannot be imported under
    test (it ends in bot.run() at module scope), so any logic that stays inline
    there is untestable by construction. Each of the five buckets is independent
    -- a bucket contributes its line iff its count is truthy -- so callers can pass
    zero and skip a line without needing to filter first.

    S1 ("Message links only work in a server channel...") is deliberately NOT
    built here: it is the sole note on the outside-a-guild early-return path in
    _resolve_linked_images and never coexists with any of S2-S6.
    """
    notes = []
    if outside_guild:
        notes.append(f"Skipping {outside_guild} message link(s) from outside this server.")
    if unfetchable:
        notes.append(
            f"Skipping {unfetchable} message link(s) I couldn't fetch — the message may be "
            f"gone, or you may not have access to that channel."
        )
    if no_image:
        notes.append(f"Skipping {no_image} linked message(s) with no image attached.")
    if over_cap:
        notes.append(f"That's a lot of links! Using the first {kept} and skipping {over_cap}.")
    if truncated:
        notes.append(
            f"Linked messages had more than {max_links} images — using the first {max_links}."
        )
    return notes


def needs_thread_membership_check(is_private_thread, has_manage_threads):
    """True when _resolve_linked_images must run an explicit
    `channel.fetch_member(ctx.author.id)` check before trusting
    `channel.permissions_for(ctx.author)` for a linked message's channel.

    discord.py's Thread.permissions_for(obj) (re-verified on 2.7.1, the pinned
    version) delegates straight to `self.parent.permissions_for(obj)` -- it does
    NOT factor in private-thread membership at all (verified against the
    installed discord.py source: it reads
    `parent = self.parent; base = GuildChannel.permissions_for(parent, obj)` and
    never touches thread membership). That means authoritative check (b) in
    _resolve_linked_images (view_channel + read_message_history) can come back
    True for a requester who is NOT a member of a private thread and whom
    Discord's own client would refuse to even show that thread to, as long as
    they can see the thread's PARENT channel. Left uncaught, a pasted link to a
    private thread the bot happens to be in (or has Manage Threads on) becomes
    exactly the image-exfiltration path this feature exists to prevent -- just
    through the one channel type where permissions_for is not authoritative.

    `has_manage_threads` is an escape hatch matching Discord's real semantics: a
    member with Manage Threads can act on a private thread without being an
    explicit member of it, so the extra fetch_member round-trip is skipped for
    them, same as Discord's own UI would allow.
    """
    return bool(is_private_thread) and not has_manage_threads


def limit_links(links, max_links=MAX_LINKS):
    """Dedupe `links` on the (guild_id, channel_id, message_id) triple -- keeping
    the FIRST occurrence, `raw` and all -- then split into (kept, dropped) at
    `max_links`. Duplicates collapse silently and are never counted as dropped:
    pasting the same link twice is one link, and counting it as a drop would burn
    the cap on nothing and produce a confusing "skipping 1" notice for a link that
    was never actually skipped.

    `max_links <= 0` drops everything (kept == []); never raises and never wraps
    around via negative-slice semantics. Never mutates the input list.
    """
    seen = set()
    unique = []
    for link in links:
        key = (link.guild_id, link.channel_id, link.message_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)

    if max_links <= 0:
        return [], unique
    return unique[:max_links], unique[max_links:]
