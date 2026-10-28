"""Small curses checklist; no network or deletion takes place in this screen."""
import queue


def pick_channels(choices, mode):
    """Return selected channel IDs, or None on cancellation."""
    return _run_picker(choices, mode)


def pick_guild(choices, mode, count_updates=None):
    """Return ticked server IDs, or None on cancellation."""
    return _run_picker(choices, mode, servers=True, count_updates=count_updates)


def _run_picker(choices, mode, servers=False, count_updates=None):
    try:
        import curses
    except ImportError:
        raise SystemExit('Terminal picker unavailable; use --guild ID with --channel ID or --all-channels.')

    def run(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        palette = {}
        if curses.has_colors():
            try:
                background = curses.COLOR_BLACK
                try:
                    curses.use_default_colors()
                    background = -1
                except curses.error:
                    pass
                for pair, (name, color) in enumerate((
                    ('accent', curses.COLOR_CYAN), ('selected', curses.COLOR_GREEN),
                    ('notice', curses.COLOR_YELLOW), ('delete', curses.COLOR_RED),
                ), 1):
                    curses.init_pair(pair, color, background)
                    palette[name] = curses.color_pair(pair)
            except curses.error:
                palette = {}  # Keep the checklist usable on limited terminals.
        return _pick_channels(screen, choices, mode, servers=servers, palette=palette,
                              count_updates=count_updates)

    try:
        return curses.wrapper(run)
    except curses.error:
        raise SystemExit('Cannot open terminal picker; use --guild ID with --channel ID or --all-channels.')


def _pick_channels(screen, choices, mode, servers=False, palette=None, count_updates=None):
    import curses

    if not choices:
        return None
    palette = palette or {}
    screen.keypad(True)
    selected = set()
    cursor = top = 0
    notice = ''
    base_labels = dict(choices)
    counts = {}
    if count_updates is not None:
        screen.timeout(100)  # Redraw incoming counts even while no key is pressed.

    while True:
        if count_updates is not None:
            while True:
                try:
                    guild_id, count = count_updates.get_nowait()
                except queue.Empty:
                    break
                if guild_id is None:
                    raise SystemExit(count)
                counts[guild_id] = count
            updated = []
            for guild_id, _ in choices:
                if guild_id not in counts:
                    summary = 'count loading...'
                elif counts[guild_id] is None:
                    summary = 'count unavailable'
                else:
                    summary = f'~{counts[guild_id]:,} msgs'
                updated.append((guild_id, f'{summary} | {base_labels[guild_id]}'))
            choices = updated
        screen.erase()
        height, width = screen.getmaxyx()

        def line(row, text, style=0, column=0):
            if 0 <= row < height and width - column > 1:
                # Channel names are external input; never render control characters.
                text = ''.join(c if c.isprintable() else ' ' for c in text)
                try:
                    screen.addnstr(row, column, text, width - column - 1, style)
                except curses.error:
                    pass  # Unicode display width or a resize can clip a line.

        rows = max(1, height - 6)
        top = max(0, min(top, cursor))
        if cursor >= top + rows:
            top = cursor - rows + 1
        if height < 8 or width < 35:
            line(0, 'Resize terminal (q cancels)')
        else:
            noun = 'servers' if servers else 'channels'
            summary = f'{len(selected)} {noun} selected'
            mode_color = palette.get('delete' if mode.startswith('DELETE') else 'accent', 0)
            line(0, f'Discleanse | {mode} | {summary}', curses.A_BOLD | mode_color)
            line(1, 'Up/Down: move  Space: select  a: all  n: none', palette.get('accent', 0))
            action = 'run cleanup' if mode.startswith('DELETE') else 'run dry run'
            line(2, f'Enter: {action} on ticked {noun}  q/Esc: cancel')
            for row, index in enumerate(range(top, min(top + rows, len(choices))), 4):
                channel_id, label = choices[index]
                checked = channel_id in selected
                mark = 'X' if checked else ' '
                pointer = '>' if index == cursor else ' '
                prefix = f'{pointer} [{mark}] '
                style = palette.get('selected' if checked else 'accent', 0)
                if index == cursor:
                    style |= curses.A_REVERSE
                suffix = f' ({channel_id})'
                name = label[:-len(suffix)] if label.endswith(suffix) else label
                # Put IDs first so long names cannot push them off-screen. The
                # following column uses only the ASCII prefix and snowflake width.
                line(row, prefix, style | (curses.A_BOLD if checked else 0))
                line(row, channel_id, style | curses.A_BOLD, column=len(prefix))
                line(row, f'  {name}', style, column=len(prefix) + len(channel_id))
            hint = (f'Counts {len(counts)}/{len(choices)}: ~your searchable messages.'
                    if count_updates is not None else 'Space: tick servers; Enter: run on selected servers.') if servers else 'Threads selected separately; archived threads: --channel ID.'
            line(height - 2, notice or hint, palette.get('notice', 0) if notice else curses.A_DIM)
            line(height - 1, f'{cursor + 1}/{len(choices)} | Only your own messages are cleansed.', curses.A_DIM)
        screen.refresh()
        key = screen.getch()
        if key != -1:
            notice = ''
        if key in (ord('q'), ord('Q'), 27):
            return None
        if height < 8 or width < 35:
            continue  # Never confirm a selection the user cannot see.
        if key in (curses.KEY_UP, ord('k')):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            cursor = min(len(choices) - 1, cursor + 1)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(choices) - 1
        elif key == curses.KEY_NPAGE:
            cursor = min(len(choices) - 1, cursor + rows)
        elif key == curses.KEY_PPAGE:
            cursor = max(0, cursor - rows)
        elif key == ord(' '):
            channel_id = choices[cursor][0]
            selected.symmetric_difference_update({channel_id})
        elif key == ord('a'):
            selected = {channel_id for channel_id, _ in choices}
        elif key == ord('n'):
            selected.clear()
        elif key in (10, 13, curses.KEY_ENTER):
            if not selected:
                notice = f'Tick at least one {"server" if servers else "channel"}, or press q to cancel.'
            elif not servers and len(selected) > 500:
                notice = 'Select at most 500 channels per run (Discord search limit).'
            else:
                return [channel_id for channel_id, _ in choices if channel_id in selected]
