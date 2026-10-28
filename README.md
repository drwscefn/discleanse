# discleanse

```text
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
```

Clean up your own messages across selected Discord servers from a terminal.
Tick multiple servers, see your message counts load in the background, and run
cleanup with rate-limit handling and automatic retries.

**The interactive server picker deletes by default.** Pressing Enter starts
deletion in the servers you have ticked. Use `discleanse --dry-run` to preview.
Deletion cannot be undone by this tool.

This authenticates with a user token. Discord
[prohibits automating normal user accounts](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots)
and may terminate accounts for doing so. This is an independent tool, not an
official Discord application.

## Install

Requires **Python 3.11+**, an interactive terminal, and Python's `curses` module
for the pickers. Linux and macOS are the intended platforms. Windows terminal
support has not been verified.

Clone the repository and install into a virtual environment:

```bash
git clone https://github.com/drwscefn/discleanse.git
cd discleanse
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

This installs the `discleanse` command and its `requests` dependency.
You can also install into a dedicated environment with `pipx install .` or
`uv tool install .` if you already use either tool.

## Start

Supply your token using the `DISCORD_TOKEN` environment variable. In Bash, this
reads it without displaying it or putting its value in shell history:

```bash
read -rsp 'Discord token: ' DISCORD_TOKEN; printf '\n'
export DISCORD_TOKEN
discleanse --dry-run
```

Keep the token private. The program does not need it in a source file, command
argument, or checked-in configuration file. An `.env` file is not loaded
automatically.

To delete instead of previewing:

```bash
discleanse
```

The server list appears after login and discovery. Counts then fill in without
moving rows or clearing selections.

| Key | Action |
| --- | --- |
| Up / Down, or `k` / `j` | Move the cursor |
| Space | Tick or untick a server |
| `a` | Tick everything in the list |
| `n` | Clear all ticks |
| Enter | Run on the ticked servers |
| `q` or Escape | Cancel the picker |
| Home / End, Page Up / Page Down | Navigate long lists |
| Ctrl+C | Stop the current run |

Server and channel IDs are bold, ticked rows are green, and deletion mode is
shown in red when the terminal supports colours. Nothing starts with an empty
selection.

Once you are finished, remove the token from the current shell with
`unset DISCORD_TOKEN`.

## Choose the scope

```bash
# Preview your messages across ticked servers.
discleanse --dry-run

# Tick servers, then choose channels within each one. This deletes by default.
discleanse --pick-channels

# Restrict cleanup to messages older than a date.
discleanse --before 2026-01-01

# Skip the server picker. Explicit --guild defaults to a dry run.
discleanse --guild SERVER_ID --all-channels

# Delete from a specific server and channel without either picker.
discleanse --guild SERVER_ID --channel CHANNEL_ID --execute

# Repeat --channel to target several channels or threads.
discleanse --guild SERVER_ID --channel CHANNEL_A --channel CHANNEL_B --execute
```

`--guild` deliberately preserves the original script's dry-run default. When
you supply it, add `--execute` to delete. Without `--guild`, interactive server
selection defaults to deletion unless you pass `--dry-run`.

Channel selection is exact: selecting a parent channel does not select its
threads. The channel picker lists message channels and active threads, with
categories and forum names used as labels. If active-thread listing is denied,
it still shows the available channels. Archived threads can be targeted using
`--channel THREAD_ID`. Whole-server cleanup searches across the server without
needing the channel or thread enumeration endpoints.

For non-interactive use, supply `--guild` and either `--all-channels` or one or
more `--channel` arguments.

## Message counts, retries and concurrency

- **Counts are approximate.** `~1,234 msgs` means Discord search reports that
  many messages by your account. Counts respect `--before` and explicit
  `--channel` filters; they do not anticipate later channel-picker selections.
  Some message types may not be deletable. A failed lookup shows
  `count unavailable`, not zero.
- **Counting runs in the background.** Select servers while it runs, or use
  `--skip-counts` to disable lookups. Enter or cancellation stops further count
  lookups before cleanup starts.
- **One server runs at a time by default.** Use `--workers 2` to try concurrent
  cleanup; accepted values are 1–8. Each worker has its own connection session
  and server state. More workers may just encounter more rate limits.
- **Global cooldowns are shared.** A global rate-limit response pauses future
  requests from every worker. The cooldown survives leaving the picker.
- **Transient failures retry automatically.** Connection and read inactivity
  timeouts are 10 and 30 seconds. Network failures and server errors use
  increasing waits up to 60 seconds; Discord's rate-limit waits may be longer.
  `--delay` sets the pause between deletions, defaulting to 1.2 seconds.
- **Completion requires confirmation.** During deletion, three consecutive
  valid searches from offset zero must report zero messages, eight seconds
  apart. Empty pages with a positive or missing count, incomplete responses,
  and pending indexing wait and retry. Skipped results trigger another pass
  from the beginning. HTTP 408 timeouts also retry.
- **Remaining messages keep the run open.** If messages cannot be deleted
  because of their type or permissions, cleanup keeps rechecking rather than
  declaring completion. Use Ctrl+C to stop. Authentication and other permanent
  request failures still report an error. Dry runs finish after listing results;
  they do not wait for messages to be deleted.
- **Stopping is cooperative.** Ctrl+C interrupts waits; an in-flight HTTP
  request may need to finish or time out before the process exits.

Worker output is tagged with the server ID. The logs include short message
previews, so treat saved logs as private.

## Optional thread deletion

Normal cleanup targets your messages. `--delete-owned-threads` additionally
offers to delete threads you created that were discovered among the search
results. **Deleting a thread also removes other people's messages inside it.**

This option asks you to type `delete threads` before deleting those threads.
`--yes` skips that prompt. Runs requiring thread-deletion prompts use one worker
so prompts cannot overlap. `--dry-run` never deletes messages or threads.

## Limitations

- Server messages only; direct messages are not supported.
- Cleanup depends on Discord's search index and the account's access. Missing
  history, permission failures, stale search results, and search pagination
  limits can make a pass incomplete. The tool does not certify complete erasure.
- Only normal messages and replies are targeted. Other message types are
  skipped. Removed copies, exports, screenshots, or third-party archives are
  outside this tool's scope.
- There is no persistent resume checkpoint. A later run searches again for
  messages that remain.
- Archived threads may need to be temporarily unarchived to delete a message.
  An interrupted run can leave a thread unarchived.

Run `discleanse --help` for all options. The source entry point remains
available as `python discleanse.py`.

## Packaging

```bash
python -m pip install -e '.[dev]'
python -m build
python -m twine check dist/*
```

Builds produce a wheel and source archive under `dist/`. Install the wheel with
`python -m pip install dist/discleanse-0.1.1-py3-none-any.whl`. Building does not
publish anything to PyPI or GitHub.
