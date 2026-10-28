import argparse
import io
import threading
import unittest
from concurrent.futures import CancelledError
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import discleanse
from test_discleanse import response


class WorkerTests(unittest.TestCase):
    def test_count_cooldown_survives_enter_and_applies_to_deletion_worker(self):
        args = argparse.Namespace(workers=1, guild=None, channel=None, before=None,
                                  delete_owned_threads=False, yes=False, execute=True)
        clock = [100.0]
        waits = []
        parent = discleanse.RunControl()
        discleanse._worker_state.control = parent

        def count(*unused):
            discleanse._worker_state.control.defer_global(60)
            return 42

        def picker(guilds, mode, count_updates=None):
            self.assertEqual(count_updates.get(timeout=2), ('one', 42))
            return ['one']

        def cleanse(session, job, me_id):
            session.request = Mock(return_value=response())

            def wait(seconds):
                session.request.assert_not_called()
                waits.append(seconds)
                clock[0] += seconds
                return False

            with patch.object(discleanse._worker_state.control.stop, 'wait', side_effect=wait):
                discleanse.request(session, 'GET', 'https://example.test')

        try:
            with patch('discleanse.time.monotonic', side_effect=lambda: clock[0]), \
                    patch('discleanse.message_count', side_effect=count), \
                    patch('discleanse.pick_guild', side_effect=picker), \
                    patch('discleanse.cleanse_guild', side_effect=cleanse), \
                    redirect_stdout(io.StringIO()):
                selected = discleanse.pick_guild_with_counts('token', [('one', 'One')], 'me', args, 'DELETE')
                discleanse.run_servers('token', args, selected, {'one': 'One'}, 'me')
            self.assertEqual(waits, [60])
        finally:
            discleanse._worker_state.__dict__.clear()

    def test_two_servers_overlap_with_separate_sessions_and_arguments(self):
        barrier = threading.Barrier(2, timeout=3)
        seen = []
        args = argparse.Namespace(workers=2, guild=None, channel=None,
                                  delete_owned_threads=False, yes=False, execute=True)

        def cleanse(session, job, me_id):
            seen.append((session, job, me_id, threading.get_ident()))
            barrier.wait()
            self.assertEqual(session.headers['Authorization'], 'test-token')

        with patch('discleanse.cleanse_guild', side_effect=cleanse), redirect_stdout(io.StringIO()):
            discleanse.run_servers('test-token', args, ['one', 'two'],
                                   {'one': 'One', 'two': 'Two'}, 'me')
        self.assertEqual({entry[1].guild for entry in seen}, {'one', 'two'})
        self.assertEqual(len({id(entry[0]) for entry in seen}), 2)
        self.assertEqual(len({id(entry[1]) for entry in seen}), 2)
        self.assertEqual(len({entry[3] for entry in seen}), 2)
        self.assertIsNone(args.guild)

    def test_global_rate_limit_delays_other_worker_before_request(self):
        control = discleanse.RunControl()
        clock = [100.0]
        session = Mock()
        session.request.return_value = response()
        waits = []

        def wait(seconds):
            self.assertFalse(session.request.called)
            waits.append(seconds)
            clock[0] += seconds
            return False

        with patch('discleanse.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(control.stop, 'wait', side_effect=wait):
            control.defer_global(5)
            discleanse._worker_state.control = control
            try:
                discleanse.request(session, 'GET', 'https://example.test/guilds/two')
            finally:
                del discleanse._worker_state.control
        self.assertEqual(waits, [5])
        self.assertEqual(session.request.call_count, 1)

    def test_global_429_publishes_cooldown(self):
        for headers, data in [({}, {'global': True, 'retry_after': 7}),
                              ({'X-RateLimit-Global': 'true'}, {'retry_after': 7})]:
            with self.subTest(headers=headers):
                control = discleanse.RunControl()
                session = Mock()
                session.request.side_effect = [response(429, data, headers), response()]
                discleanse._worker_state.control = control
                try:
                    with patch.object(control, 'wait_global'), \
                            patch.object(control, 'defer_global') as defer, \
                            redirect_stdout(io.StringIO()):
                        discleanse.request(session, 'GET', 'https://example.test')
                    self.assertGreaterEqual(defer.call_args.args[0], 7)
                finally:
                    del discleanse._worker_state.control

    def test_cancellation_interrupts_long_wait_and_blocks_next_request(self):
        control = discleanse.RunControl()
        control.stop.set()
        discleanse._worker_state.control = control
        try:
            with self.assertRaises(CancelledError):
                discleanse.pause(60)
            session = Mock()
            with self.assertRaises(CancelledError):
                discleanse.request(session, 'DELETE', 'https://example.test')
            session.request.assert_not_called()
        finally:
            del discleanse._worker_state.control

    def test_auth_block_cancels_every_worker(self):
        control = discleanse.RunControl()
        session = Mock()
        session.request.return_value = response(401, {'code': 40001})
        discleanse._worker_state.control = control
        try:
            with self.assertRaises(SystemExit):
                discleanse.request(session, 'GET', 'https://example.test')
            self.assertTrue(control.stop.is_set())
        finally:
            del discleanse._worker_state.control

    def test_non_json_unauthorized_response_cancels_every_worker(self):
        control = discleanse.RunControl()
        session = Mock()
        denied = response(401)
        denied._content = b'Unauthorized'
        session.request.return_value = denied
        discleanse._worker_state.control = control
        try:
            with self.assertRaises(SystemExit):
                discleanse.request(session, 'GET', 'https://example.test')
            self.assertTrue(control.stop.is_set())
        finally:
            del discleanse._worker_state.control

    def test_server_failure_is_reported_but_other_server_can_finish(self):
        args = argparse.Namespace(workers=2, guild=None, channel=None,
                                  delete_owned_threads=False, yes=False, execute=True)
        finished = threading.Event()

        def cleanse(session, job, me_id):
            if job.guild == 'one':
                raise SystemExit('Search forbidden')
            finished.set()

        with patch('discleanse.cleanse_guild', side_effect=cleanse), \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                discleanse.run_servers('test-token', args, ['one', 'two'],
                                       {'one': 'One', 'two': 'Two'}, 'me')
        self.assertTrue(finished.is_set())
        self.assertIn('[one] Stopped: Search forbidden', output.getvalue())

    def test_prompted_thread_deletion_runs_on_main_thread(self):
        args = argparse.Namespace(workers=2, guild=None, channel=None,
                                  delete_owned_threads=True, yes=False, execute=True)
        thread_ids = []
        with patch('discleanse.cleanse_guild', side_effect=lambda *a: thread_ids.append(threading.get_ident())), \
                redirect_stdout(io.StringIO()):
            discleanse.run_servers('test-token', args, ['one', 'two'],
                                   {'one': 'One', 'two': 'Two'}, 'me')
        self.assertEqual(thread_ids, [threading.get_ident()] * 2)


class CountTests(unittest.TestCase):
    def test_picker_opens_while_counts_are_running(self):
        picker_open = threading.Event()
        args = argparse.Namespace(before=None, channel=None)

        def count(session, guild_id, me_id, options):
            self.assertTrue(picker_open.wait(2), 'counting blocked opening the picker')
            return 42

        def picker(guilds, mode, count_updates=None):
            self.assertEqual(guilds, [('one', 'One (one)')])
            picker_open.set()
            self.assertEqual(count_updates.get(timeout=2), ('one', 42))
            return ['one']

        with patch('discleanse.message_count', side_effect=count), \
                patch('discleanse.pick_guild', side_effect=picker):
            result = discleanse.pick_guild_with_counts('test-token', [('one', 'One (one)')],
                                                     'me', args, 'DELETE')
        self.assertEqual(result, ['one'])

    def test_count_is_filtered_to_author_date_and_channels(self):
        session = Mock()
        session.request.return_value = response(data={'total_results': 1234})
        args = argparse.Namespace(before='2026-01-01', channel=['one'])
        count = discleanse.message_count(session, 'guild', 'me', args)
        self.assertEqual(count, 1234)
        params = session.request.call_args.kwargs['params']
        self.assertEqual(params['author_id'], 'me')
        self.assertEqual(params['channel_id'], ['one'])
        self.assertIn('max_id', params)
        self.assertEqual(params['limit'], 1)

    def test_indexing_count_is_retried(self):
        session = Mock()
        session.request.side_effect = [response(202, {'retry_after': 2}),
                                       response(data={'total_results': 0})]
        with patch('discleanse.pause') as pause:
            count = discleanse.message_count(session, 'guild', 'me',
                                            argparse.Namespace(before=None, channel=None))
        self.assertEqual(count, 0)
        self.assertGreaterEqual(pause.call_args.args[0], 2)

    def test_unavailable_count_is_not_zero(self):
        for replies in ([response(403, {'code': 50001})],
                        [response(202, {'retry_after': 1})] * 3,
                        [response(data={})]):
            session = Mock()
            session.request.side_effect = replies
            with patch('discleanse.pause'):
                count = discleanse.message_count(session, 'guild', 'me',
                                                argparse.Namespace(before=None, channel=None))
            self.assertIsNone(count)


if __name__ == '__main__':
    unittest.main()
