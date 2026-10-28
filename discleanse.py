#!/usr/bin/env python3
"""
discleanse.py - delete YOUR OWN messages (and optionally your own
threads) from a Discord server.

NOTE: This authenticates with a user token, which Discord's Terms of Service
prohibit automating ("self-botting"). Use on your own account at your own risk;
Discord may restrict or terminate the account.

Usage:
  export DISCORD_TOKEN="your user token"
  python discleanse.py                                     # tick servers, Enter deletes
  python discleanse.py --dry-run                           # preview across ticked servers
  python discleanse.py --pick-channels                     # also pick channels per server
  python discleanse.py --workers 2                         # opt into concurrent cleanup
  python discleanse.py --guild 123456789012345678            # pick channels, dry run
  python discleanse.py --guild 123456789012345678 --execute  # pick channels, delete

Server picker: arrows to move, Space to tick servers, Enter to run, q to cancel.
Interactive server selection defaults to DELETE across each selected server.
Use --dry-run to preview, or --pick-channels to narrow the scope per server.
Servers run one at a time by default. Use --workers 2 to opt into concurrent
cleanup with separate sessions. Global rate-limit waits pause all workers.
The server list opens immediately after discovery; approximate counts of YOUR
searchable messages update in the background. Counts respect --before and
--channel, but include message types that may not be deletable. Unavailable
counts are not zero. --skip-counts disables count lookups.
Stopping counting or cleanup may wait for an in-flight request to time out.
Owned-thread deletion with confirmation prompts runs sequentially.
Supplying --guild keeps the existing dry-run default unless --execute is given.
Channels and active threads are separate choices; archived threads can be
targeted with --channel ID. Nothing is selected initially.
For non-interactive runs, specify --guild ID with --channel ID or --all-channels.
Network failures retry automatically with waits capped at 60 seconds;
Discord's rate-limit waits may be longer. Ctrl+C stops the run.
Deletion completes only after three zero-result searches from the beginning,
eight seconds apart. Empty or incomplete pages retry; undeletable messages
can keep the run open until stopped. Dry runs finish after listing results.

Options:
  --guild ID               skip server picker
  --channel ID             limit to one channel/thread (repeatable)
  --all-channels           skip picker and search the whole server
  --pick-channels          open channel checklist for each selected server
  --workers N              concurrent servers (1-8, default 1)
  --skip-counts            skip background message counts
  --before DATE            only delete messages older than this date (e.g. 2026-01-01)
  --execute                actually delete (explicit --guild defaults to dry run)
  --dry-run                only list messages, never delete
  --delete-owned-threads   after deleting messages, also delete threads YOU created.
                           WARNING: this removes other people's messages in those
                           threads too. Asks for confirmation unless --yes is given.
  --yes                    skip the confirmation prompt for thread deletion
"""
import argparse
import copy
import os
import queue
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from channel_picker import pick_channels, pick_guild

API = "https://discord.com/api/v9"
# Only these message types are user-deletable normal messages (default, reply).
DELETABLE_TYPES = {0, 19}
# Channel types: 10 = announcement thread, 11 = public thread, 12 = private thread.
THREAD_TYPES = {10, 11, 12}
ERR_THREAD_ARCHIVED = 50083
REQUEST_TIMEOUT = (10, 30)  # connect timeout, read inactivity timeout (seconds)
MAX_RETRY_DELAY = 60
ZERO_CONFIRMATIONS = 3
SEARCH_RECHECK_DELAY = 8
_worker_state = threading.local()
_output_lock = threading.Lock()
BANNER = r"""
       .####.              .###.        #
     .#########################  #          .
    #########################   ##    #
   ######    ########    #####      #      .
  #######    ########    ####  #  #     .
  #########################    #     .       .
   #####################  ##      .
     ######        ####  #    .        .
       ###          ##     .

                  d i s c l e a n s e
                 leave less behind.
"""


class RateLimitState:
    """Account-wide cooldown retained across counting, selection and deletion."""

    def __init__(self):
        self.lock = threading.Lock()
        self.global_until = 0.0


class RunControl:
    """Cancellation for one phase, plus the shared account cooldown."""

    def __init__(self, rate_limits=None):
        self.stop = threading.Event()
        self.rate_limits = rate_limits if rate_limits is not None else RateLimitState()

    def check(self):
        if self.stop.is_set():
            raise CancelledError()

    def defer_global(self, seconds):
        with self.rate_limits.lock:
            self.rate_limits.global_until = max(self.rate_limits.global_until,
                                               time.monotonic() + seconds)

    def wait_global(self):
        while True:
            self.check()
            with self.rate_limits.lock:
                remaining = self.rate_limits.global_until - time.monotonic()
            if remaining <= 0:
                return
            if self.stop.wait(remaining):
                raise CancelledError()


def pause(seconds):
    control = getattr(_worker_state, "control", None)
    if control is None:
        time.sleep(seconds)
    elif control.stop.wait(seconds):
        raise CancelledError()


def log(*values, **kwargs):
    if getattr(_worker_state, "quiet", False):
        return  # The count worker must not write over the curses screen.
    prefix = getattr(_worker_state, "prefix", None)
    with _output_lock:
        if prefix:
            values = (f"[{prefix}]", *values)
        print(*values, **kwargs)


def message_count(session, guild_id, me_id, args):
    """Approximate own searchable messages, or None if unavailable/indexing."""
    params = {"author_id": me_id, "limit": 1}
    if args.before:
        params["max_id"] = snowflake_from_date(args.before)
    if args.channel:
        params["channel_id"] = args.channel
    for attempt in range(3):
        result = request(session, "GET", f"{API}/guilds/{guild_id}/messages/search", params=params)
        if result.status_code == 200:
            count = result.json().get("total_results")
            return count if isinstance(count, int) and count >= 0 else None
        if result.status_code != 202:
            return None
        if attempt < 2:
            pause(float(result.json().get("retry_after", 5)) + 1)
    return None


def pick_guild_with_counts(token, guilds, me_id, args, mode):
    """Open the list immediately and feed counts to it from one background worker."""
    updates = queue.Queue()
    parent = getattr(_worker_state, "control", None)
    control = RunControl(parent.rate_limits if parent else None)
    fatal = []

    def count_servers():
        _worker_state.control = control
        _worker_state.quiet = True
        session = requests.Session()
        session.headers["Authorization"] = token
        try:
            for guild_id, _ in guilds:
                control.check()
                try:
                    count = message_count(session, guild_id, me_id, args)
                except (ValueError, TypeError, requests.RequestException):
                    count = None
                updates.put((guild_id, count))
        except CancelledError:
            pass
        except SystemExit as exc:
            fatal.append(str(exc))
            updates.put((None, str(exc)))
        finally:
            session.close()
            del _worker_state.control
            del _worker_state.quiet

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="message-counts")
    future = pool.submit(count_servers)
    try:
        selected = pick_guild(guilds, mode, count_updates=updates)
    finally:
        control.stop.set()
        future.cancel()
        if future.running():
            log("Finishing the in-flight count request...", flush=True)
        pool.shutdown(wait=True, cancel_futures=True)
    if fatal:
        sys.exit(fatal[0])
    if not future.cancelled():
        future.result()
    return selected


def run_servers(token, args, selected_guilds, labels, me_id, channels_by_guild=None):
    """Run independent server jobs, sharing only global cooldown/cancellation."""
    parent = getattr(_worker_state, "control", None)
    control = RunControl(parent.rate_limits if parent else None)
    workers = min(args.workers, len(selected_guilds))
    if args.delete_owned_threads and args.execute and not args.yes:
        workers = 1  # Never interleave interactive thread-deletion confirmations.
        log("Owned-thread confirmations requested; running servers sequentially.")

    def run_one(guild_id):
        previous_state = _worker_state.__dict__.copy()
        _worker_state.control = control
        _worker_state.prefix = guild_id
        session = None
        try:
            control.check()
            job = copy.copy(args)
            job.guild = guild_id
            job.channel = (channels_by_guild or {}).get(guild_id, args.channel)
            if job.channel:
                job.channel = list(job.channel)
            session = requests.Session()
            session.headers["Authorization"] = token
            log(f"Server: {labels[guild_id]}", flush=True)
            cleanse_guild(session, job, me_id)
            return True
        except CancelledError:
            return False
        except SystemExit as exc:
            log(f"Stopped: {exc}", flush=True)
            return False
        except Exception as exc:
            log(f"Failed: {type(exc).__name__}", flush=True)
            return False
        finally:
            if session is not None:
                session.close()
            _worker_state.__dict__.clear()
            _worker_state.__dict__.update(previous_state)

    log(f"Running {len(selected_guilds)} server(s) with {workers} worker(s).", flush=True)
    if workers == 1:
        # Keep any confirmation prompt on the main thread.
        results = [run_one(guild_id) for guild_id in selected_guilds]
    else:
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="discleanse")
        futures = []
        try:
            futures = [pool.submit(run_one, guild_id) for guild_id in selected_guilds]
            results = [future.result() for future in as_completed(futures)]
        except BaseException:
            control.stop.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    if not all(results):
        sys.exit("One or more servers could not finish; see the server logs above.")


def load_guild_choices(session):
    """List the account's servers (a user can belong to at most 200)."""
    result = request(session, "GET", f"{API}/users/@me/guilds", params={"limit": 200})
    if result.status_code != 200:
        sys.exit(f"Could not list servers ({result.status_code}); nothing selected.")
    choices = [(guild["id"], f"{guild['name']} ({guild['id']})") for guild in result.json()]
    return sorted(choices, key=lambda choice: choice[1].casefold())


def load_channel_choices(session, guild_id):
    """List message-bearing channels and active threads; containers supply labels."""
    result = request(session, "GET", f"{API}/guilds/{guild_id}/channels")
    if result.status_code != 200:
        sys.exit(f"Could not list channels ({result.status_code}); nothing selected.")
    channels = result.json()
    result = request(session, "GET", f"{API}/guilds/{guild_id}/threads/active")
    if result.status_code == 403:
        log("Warning: Discord denied active-thread listing (403). Showing available "
              "channels only; use --channel ID to target a thread directly.", flush=True)
    elif result.status_code != 200:
        sys.exit(f"Could not list active threads ({result.status_code}); nothing selected.")
    else:
        channels += result.json().get("threads", [])
    by_id = {channel["id"]: channel for channel in channels}
    choices = []
    for channel in by_id.values():
        # Categories, forums and media channels are containers, not message targets.
        if channel.get("type") not in {0, 2, 5, 10, 11, 12, 13}:
            continue
        names = [channel.get("name", channel["id"])]
        parent_id = channel.get("parent_id")
        seen = {channel["id"]}
        while parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id[parent_id]
            names.insert(0, parent.get("name", parent_id))
            parent_id = parent.get("parent_id")
        label = " / ".join(names)
        if channel.get("type") in THREAD_TYPES:
            label += " [thread]"
        choices.append((channel["id"], f"{label} ({channel['id']})"))
    return sorted(choices, key=lambda choice: choice[1].casefold())


def request(session, method, url, **kwargs):
    """Retry transient failures until recovery; Ctrl+C still interrupts waits."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    retry_delay = 3
    while True:
        control = getattr(_worker_state, "control", None)
        if control is not None:
            control.wait_global()
        try:
            resp = session.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as exc:
            # Exception text can include URLs or request data; log only its type.
            log(f"  {type(exc).__name__} during {method}; retrying in {retry_delay}s",
                  flush=True)
            pause(retry_delay)
            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
            continue
        if resp.status_code == 429:
            data = {}
            try:
                data = resp.json()
                wait = float(data.get("retry_after", 2))
            except (ValueError, TypeError):
                wait = float(resp.headers.get("Retry-After", 5))  # non-JSON (e.g. Cloudflare)
            wait += 0.5
            log(f"  rate limited, sleeping {wait:.1f}s", flush=True)
            global_limit = (data.get("global") is True
                            or resp.headers.get("X-RateLimit-Global", "").lower() == "true"
                            or resp.headers.get("X-RateLimit-Scope") == "global")
            if control is not None and global_limit:
                control.defer_global(wait)
            else:
                pause(wait)
            continue
        if resp.status_code == 408 or resp.status_code >= 500:
            log(f"  HTTP {resp.status_code} during {method}; retrying in {retry_delay}s",
                  flush=True)
            pause(retry_delay)
            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
            continue
        if resp.status_code == 401 or (resp.status_code == 403
                                      and error_code(resp) in (40002, 40003, 40001, 0)):
            if control is not None:
                control.stop.set()
            sys.exit(f"Discord blocked the request ({resp.status_code}): {resp.text[:200]}")
        # Bucket exhausted: wait exactly as long as Discord says, no longer.
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            try:
                pause(float(resp.headers.get("X-RateLimit-Reset-After", 1)) + 0.1)
            except ValueError:
                pause(1)
        return resp


def snowflake_from_date(date_str):
    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000) - 1420070400000
    return str(ms << 22)


def error_code(resp):
    try:
        return resp.json().get("code")
    except ValueError:
        return None


def delete_message(session, channel_id, message_id):
    """Delete a message. If its thread is archived, unarchive, delete, re-archive.
    Returns (success, status_note)."""
    url = f"{API}/channels/{channel_id}/messages/{message_id}"
    r = request(session, "DELETE", url)
    if r.status_code == 204:
        return True, ""
    if r.status_code == 404 and error_code(r) == 10008:
        return None, "already gone (stale search result)"

    if error_code(r) == ERR_THREAD_ARCHIVED:
        u = request(session, "PATCH", f"{API}/channels/{channel_id}", json={"archived": False})
        if u.status_code != 200:
            return False, f"thread archived, unarchive failed ({u.status_code})"
        r = request(session, "DELETE", url)
        # Put the thread back the way it was.
        request(session, "PATCH", f"{API}/channels/{channel_id}", json={"archived": True})
        if r.status_code == 204:
            return True, "(unarchived temporarily)"
    return False, f"{r.status_code} {r.text[:150]}"


def main():
    previous_state = _worker_state.__dict__.copy()
    _worker_state.control = RunControl()
    try:
        return _main()
    finally:
        _worker_state.__dict__.clear()
        _worker_state.__dict__.update(previous_state)


def _main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--guild", help="server (guild) ID; bypass server picker")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--channel", action="append", help="channel/thread ID (repeatable); bypass picker")
    scope.add_argument("--all-channels", action="store_true", help="bypass picker; search whole server")
    scope.add_argument("--pick-channels", action="store_true", help="pick channels per selected server")
    p.add_argument("--before", help="only messages before this date, e.g. 2026-01-01")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="actually delete")
    mode.add_argument("--dry-run", action="store_true", help="preview only; never delete")
    p.add_argument("--delete-owned-threads", action="store_true",
                   help="also delete threads you created (removes others' messages in them)")
    p.add_argument("--yes", action="store_true", help="skip thread-deletion confirmation")
    p.add_argument("--workers", type=int, default=1, help="concurrent servers (1-8, default 1)")
    p.add_argument("--skip-counts", action="store_true", help="open server picker without message counts")
    p.add_argument("--delay", type=float, default=1.2,
                   help="seconds to pause between deletes (default 1.2; lower is faster\n"
                        "but more likely to hit rate limits or get the account flagged)")
    args = p.parse_args()

    if not 1 <= args.workers <= 8:
        p.error("--workers must be between 1 and 8")
    if args.delay < 0:
        p.error("--delay must not be negative")

    if not args.guild:
        args.execute = not args.dry_run
        if not args.channel and not args.pick_channels:
            args.all_channels = True

    if args.channel and len(args.channel) > 500:
        p.error("select at most 500 channels per run")
    if not args.guild or (not args.channel and not args.all_channels):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            p.error("pickers need a terminal; use --guild ID with --channel ID or --all-channels")

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("Set the DISCORD_TOKEN environment variable.")

    if sys.stdout.isatty():
        log(BANNER, flush=True)
    session = requests.Session()
    session.headers["Authorization"] = token

    me = request(session, "GET", f"{API}/users/@me")
    if me.status_code != 200:
        sys.exit(f"Auth failed ({me.status_code}). Check your token.")
    me_id = me.json()["id"]
    log(f"Logged in as {me.json()['username']} ({me_id})")
    log("Mode:", "DELETE" if args.execute else "DRY RUN (use --execute to delete)")

    if not args.guild:
        log("Loading your servers...", flush=True)
        guilds = load_guild_choices(session)
        if not guilds:
            log("No servers found for this account.")
            return
        mode_label = "DELETE" if args.execute else "DRY RUN"
        if args.skip_counts:
            selected_guilds = pick_guild(guilds, mode_label)
        else:
            selected_guilds = pick_guild_with_counts(token, guilds, me_id, args, mode_label)
        if not selected_guilds:
            log("Cancelled; nothing cleansed.")
            return
        labels = dict(guilds)
    else:
        selected_guilds = [args.guild]
        labels = {args.guild: args.guild}

    channels_by_guild = {}
    ready_guilds = []
    for guild_id in selected_guilds:
        job = copy.copy(args)
        job.guild = guild_id
        if not job.channel and not job.all_channels:
            log(f"\nSelect channels for: {labels[guild_id]}")
            if not choose_channels(session, job):
                continue
        channels_by_guild[guild_id] = job.channel
        ready_guilds.append(guild_id)
    session.close()
    if ready_guilds:
        run_servers(token, args, ready_guilds, labels, me_id, channels_by_guild)


def choose_channels(session, args):
    """Prepare channel choices on the main thread before workers start."""
    if not args.channel and not args.all_channels:
        log("Loading channels and active threads...", flush=True)
        choices = load_channel_choices(session, args.guild)
        if not choices:
            log("No message channels or active threads found.")
            return
        mode = "DELETE" if args.execute else "DRY RUN"
        if args.delete_owned_threads:
            mode += " + owned-thread deletion requested"
        args.channel = pick_channels(choices, mode)
        if not args.channel:
            log("Cancelled; nothing cleansed.")
            return
        labels = dict(choices)
        log("Selected channels:")
        for channel_id in args.channel:
            log(f"  {labels[channel_id]}")
    return True


def cleanse_guild(session, args, me_id):
    """Clean one server, keeping pagination and thread state local to that server."""
    selected_channels = set(args.channel or [])

    max_id = snowflake_from_date(args.before) if args.before else None
    offset = 0  # counts messages we skipped, since deleted ones drop out of results
    deleted = skipped = 0
    zero_streak = 0
    channel_cache = {}   # channel_id -> channel object (or None)
    owned_threads = {}   # thread_id -> name
    thread_pass_done = False

    def note_channel(channel_id):
        """Remember threads that I created, for the optional thread-deletion pass."""
        if channel_id in channel_cache:
            return
        r = request(session, "GET", f"{API}/channels/{channel_id}")
        ch = r.json() if r.status_code == 200 else None
        channel_cache[channel_id] = ch
        if ch and ch.get("type") in THREAD_TYPES and ch.get("owner_id") == me_id:
            owned_threads[channel_id] = ch.get("name", "?")

    while True:
        if args.execute and offset > 9975:
            log("  Search pagination limit reached with skipped results; "
                "waiting and restarting from the beginning.", flush=True)
            offset = 0
            zero_streak = 0
            pause(SEARCH_RECHECK_DELAY)
        params = {"author_id": me_id, "sort_by": "timestamp", "sort_order": "desc", "offset": offset}
        if args.channel:
            params["channel_id"] = args.channel
        if max_id:
            params["max_id"] = max_id

        resp = request(session, "GET", f"{API}/guilds/{args.guild}/messages/search", params=params)
        if resp.status_code == 202:  # search index not ready yet
            zero_streak = 0
            try:
                wait = max(float(resp.json().get("retry_after", 5)), 0) + 1
            except (ValueError, TypeError, AttributeError):
                wait = SEARCH_RECHECK_DELAY
            log(f"  Search index not ready; retrying in {wait:.1f}s.", flush=True)
            pause(wait)
            continue
        if resp.status_code != 200:
            sys.exit(f"Search failed: {resp.status_code} {resp.text}")

        try:
            data = resp.json()
            total = data.get("total_results")
            groups = data.get("messages")
            valid = (type(total) is int and total >= 0 and isinstance(groups, list)
                     and all(isinstance(group, list)
                             and all(isinstance(msg, dict) for msg in group)
                             for group in groups))
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            zero_streak = 0
            log("  Search response incomplete or invalid; waiting and retrying.", flush=True)
            pause(SEARCH_RECHECK_DELAY)
            continue
        if data.get("doing_deep_historical_index"):
            zero_streak = 0
            log("  Search is still indexing history; waiting and retrying.", flush=True)
            pause(SEARCH_RECHECK_DELAY)
            continue
        # Each result is a context group; the matching message has "hit": true.
        hits = [m for group in groups for m in group if m.get("hit")]
        if not hits:
            if args.execute and args.delete_owned_threads and owned_threads and not thread_pass_done:
                # An undeletable thread message must not prevent the explicitly
                # requested thread-deletion pass from ever being reached.
                delete_owned_threads(session, args, owned_threads)
                thread_pass_done = True
                offset = 0
                zero_streak = 0
                pause(SEARCH_RECHECK_DELAY)
                continue
            if not args.execute and offset >= total:
                # A preview does not remove messages, so it ends at the last page.
                break
            if args.execute and offset == 0 and total == 0 and not groups:
                zero_streak += 1
                log(f"  Search reports zero messages ({zero_streak}/{ZERO_CONFIRMATIONS}).",
                    flush=True)
                if zero_streak >= ZERO_CONFIRMATIONS:
                    log("Confirmed zero searchable messages in the selected scope.", flush=True)
                    break
            else:
                zero_streak = 0
                log(f"  Empty search page (total_results={total}, offset={offset}); "
                    "completion is not confirmed. Waiting and rechecking.", flush=True)
            if args.execute:
                # Deleted/stale results can shift offsets past messages that remain.
                # Every completion check must cover the original full selection.
                offset = 0
            pause(SEARCH_RECHECK_DELAY)
            continue
        zero_streak = 0

        for msg in hits:
            control = getattr(_worker_state, "control", None)
            if control is not None:
                control.check()
            # Enforce the selection locally, even if search includes other channels.
            if selected_channels and msg["channel_id"] not in selected_channels:
                offset += 1
                skipped += 1
                continue
            if args.delete_owned_threads:
                note_channel(msg["channel_id"])
            if msg["author"]["id"] != me_id or msg["type"] not in DELETABLE_TYPES:
                offset += 1
                skipped += 1
                continue
            preview = (msg["content"] or "[attachment/embed]")[:60].replace("\n", " ")
            if not args.execute:
                log(f"[dry-run] #{msg['channel_id']} {msg['id']}: {preview}")
                offset += 1  # nothing is removed, so advance
                deleted += 1
                continue
            ok, note = delete_message(session, msg["channel_id"], msg["id"])
            if ok is None:  # stale search result; step past it
                offset += 1
                log(f"skip {msg['id']} ({note})")
                continue
            if ok:
                deleted += 1
                log(f"deleted {msg['id']}: {preview} {note}")
            else:
                offset += 1
                skipped += 1
                log(f"skip {msg['id']} ({note})")
            pause(args.delay)

    verb = "deleted" if args.execute else "would delete"
    log(f"\nMessages: {verb} {deleted}, skipped {skipped}.")

    if args.delete_owned_threads and not thread_pass_done:
        delete_owned_threads(session, args, owned_threads)


def delete_owned_threads(session, args, owned_threads):
    """Run the explicitly requested owned-thread pass, retaining confirmation."""
    # Optional pass: delete threads I created.
    if args.delete_owned_threads:
        if not owned_threads:
            log("No threads created by you were found among channels with your messages.")
            return
        log(f"\nThreads you created ({len(owned_threads)}):")
        for tid, name in owned_threads.items():
            log(f"  {tid}  {name}")
        log("Deleting a thread removes ALL messages in it, including other people's.")

        if not args.execute:
            log("[dry-run] no threads deleted.")
            return
        if not args.yes and input("Type 'delete threads' to confirm: ").strip() != "delete threads":
            log("Aborted; threads left in place.")
            return
        t_deleted = 0
        for tid, name in owned_threads.items():
            r = request(session, "DELETE", f"{API}/channels/{tid}")
            if r.status_code == 200:
                t_deleted += 1
                log(f"deleted thread {tid}: {name}")
            else:
                log(f"could not delete thread {tid} ({r.status_code})")
            pause(1.2)
        log(f"Threads: deleted {t_deleted} of {len(owned_threads)}.")


def cli():
    """Installed command entry point, including clean Ctrl+C handling."""
    try:
        main()
    except KeyboardInterrupt:
        log("\nStopped.")
        sys.exit(130)


if __name__ == "__main__":
    cli()
