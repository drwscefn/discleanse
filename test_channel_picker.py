import unittest
import queue


class Screen:
    """Terminal boundary double; exhausts input if the picker fails to exit."""
    def __init__(self, keys, size=(12, 80)):
        self.keys = iter(keys)
        self.size = size

    def keypad(self, enabled): pass
    def erase(self): pass
    def refresh(self): pass
    def getmaxyx(self): return self.size
    def getch(self): return next(self.keys)
    def addnstr(self, *args): pass
    def timeout(self, milliseconds): pass


class PickerTests(unittest.TestCase):
    def test_live_counts_update_without_reordering_or_losing_selection(self):
        from channel_picker import _pick_channels
        updates = queue.Queue()
        writes = []

        class UpdatingScreen(Screen):
            def getch(self):
                key = super().getch()
                if key == ord(' '):
                    updates.put(('b', 1234))
                    updates.put(('a', 0))
                return key

            def addnstr(self, row, column, text, width, style):
                writes.append((row, text))

        selected = _pick_channels(UpdatingScreen([ord(' '), ord('j'), ord(' '), 10]),
                                  [('a', 'Alpha (a)'), ('b', 'Beta (b)')],
                                  'DELETE', servers=True, count_updates=updates)
        self.assertEqual(selected, ['a', 'b'])
        self.assertTrue(any(row == 4 and '~0 msgs' in text for row, text in writes))
        self.assertTrue(any(row == 5 and '~1,234 msgs' in text for row, text in writes))

    def test_server_picker_returns_multiple_ticked_servers(self):
        from channel_picker import _pick_channels
        self.assertEqual(_pick_channels(Screen([ord(' '), ord('j'), ord(' '), 10]),
                                       [('a', 'Alpha'), ('b', 'Beta')],
                                       'DELETE', servers=True), ['a', 'b'])

    def test_server_picker_cancellation_returns_none(self):
        from channel_picker import _pick_channels
        self.assertIsNone(_pick_channels(Screen([ord('q')]), [('a', 'Alpha')],
                                         'DRY RUN', servers=True))

    def test_empty_server_selection_cannot_start_deletion(self):
        from channel_picker import _pick_channels
        self.assertIsNone(_pick_channels(Screen([10, ord('q')]), [('a', 'Alpha')],
                                         'DELETE', servers=True))

    def pick(self, keys, choices=None, size=(12, 80)):
        from channel_picker import _pick_channels
        return _pick_channels(Screen(keys, size), choices or [('a', 'Alpha'), ('b', 'Beta')],
                              'DRY RUN')

    def test_space_selects_and_enter_returns_only_ticked_channels(self):
        self.assertEqual(self.pick([ord('j'), ord(' '), 10]), ['b'])

    def test_empty_enter_does_not_mean_all_channels(self):
        self.assertEqual(self.pick([10, ord(' '), 10]), ['a'])

    def test_quit_returns_cancel_even_after_selection(self):
        self.assertIsNone(self.pick([ord(' '), ord('q')]))

    def test_toggle_all_then_clear_and_reselect(self):
        self.assertEqual(self.pick([ord('a'), ord('n'), ord('j'), ord(' '), 10]), ['b'])

    def test_long_list_scrolls_to_last_item(self):
        import curses
        self.assertEqual(self.pick([curses.KEY_END, ord(' '), 10],
                                   [(str(i), str(i)) for i in range(100)]), ['99'])

    def test_tiny_terminal_can_still_cancel(self):
        self.assertIsNone(self.pick([ord('q')], size=(2, 5)))

    def test_tiny_terminal_cannot_confirm_hidden_selections(self):
        self.assertIsNone(self.pick([ord('a'), 10, ord('q')], size=(2, 5)))


if __name__ == '__main__':
    unittest.main()
