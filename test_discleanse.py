import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests

import discleanse


def response(status=200, data=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({} if data is None else data).encode()
    result.headers.update(headers or {})
    return result


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.sleep = self.enterContext(patch('discleanse.time.sleep'))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))

    def test_requests_have_finite_connect_and_read_timeouts(self):
        session = Mock()
        session.request.return_value = response()
        discleanse.request(session, 'GET', 'https://example.test')
        timeout = session.request.call_args.kwargs.get('timeout')
        self.assertIsNotNone(timeout)
        self.assertTrue(all(0 < value <= 60 for value in timeout))

    def test_network_failures_retry_the_same_request_until_recovery(self):
        for error in (requests.Timeout, requests.ConnectionError,
                      requests.exceptions.ChunkedEncodingError):
            with self.subTest(error=error):
                session = Mock()
                success = response(204)
                session.request.side_effect = [error('private detail'), success]
                result = discleanse.request(session, 'DELETE', 'https://example.test',
                                           params={'id': '123'})
                self.assertIs(result, success)
                self.assertEqual(session.request.call_count, 2)
                self.assertEqual(session.request.call_args_list[0],
                                 session.request.call_args_list[1])
        self.assertNotIn('private detail', self.output.getvalue())
        self.assertIn('retry', self.output.getvalue())

    def test_server_failures_back_off_with_a_cap_and_reset_after_success(self):
        session = Mock()
        success = response()
        session.request.side_effect = [response(503) for _ in range(9)] + [success]
        self.assertIs(discleanse.request(session, 'GET', 'https://example.test'), success)
        delays = [call.args[0] for call in self.sleep.call_args_list]
        self.assertGreater(delays[1], delays[0])
        self.assertLessEqual(max(delays), 60)
        self.assertEqual(delays[-1], delays[-2])
        self.sleep.reset_mock()
        session.request.side_effect = [response(502), success]
        discleanse.request(session, 'GET', 'https://example.test')
        self.assertEqual(self.sleep.call_args.args[0], delays[0])

    def test_rate_limit_delay_is_respected(self):
        session = Mock()
        session.request.side_effect = [response(429, {'retry_after': 80}), response()]
        discleanse.request(session, 'GET', 'https://example.test')
        self.assertGreaterEqual(self.sleep.call_args.args[0], 80)

    def test_auth_failure_still_stops(self):
        session = Mock()
        session.request.return_value = response(401, {'code': 40001})
        with self.assertRaises(SystemExit):
            discleanse.request(session, 'GET', 'https://example.test')
        self.sleep.assert_not_called()

    def test_ctrl_c_is_not_retried(self):
        session = Mock()
        session.request.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            discleanse.request(session, 'GET', 'https://example.test')
        self.sleep.assert_not_called()

    def test_delete_timeout_followed_by_missing_message_is_handled(self):
        session = Mock()
        session.request.side_effect = [requests.ReadTimeout(),
                                       response(404, {'code': 10008})]
        ok, note = discleanse.delete_message(session, 'channel', 'message')
        self.assertIsNone(ok)
        self.assertIn('already gone', note)


class ChannelTests(unittest.TestCase):
    def test_channel_list_labels_categories_and_threads_without_containers(self):
        session = Mock()
        session.request.side_effect = [response(data=[
            {'id': 'cat', 'type': 4, 'name': 'Community'},
            {'id': 'chat', 'type': 0, 'name': 'general', 'parent_id': 'cat'},
            {'id': 'forum', 'type': 15, 'name': 'questions'},
            {'id': 'voice', 'type': 2, 'name': 'Lounge'},
        ]), response(data={'threads': [
            {'id': 'thread', 'type': 11, 'name': 'Help', 'parent_id': 'forum'},
        ]})]
        channels = dict(discleanse.load_channel_choices(session, 'guild'))
        self.assertEqual(set(channels), {'chat', 'voice', 'thread'})
        self.assertIn('Community', channels['chat'])
        self.assertIn('general', channels['chat'])
        self.assertIn('questions', channels['thread'])
        self.assertIn('Help', channels['thread'])

    def test_channel_listing_failure_cannot_expand_to_whole_guild(self):
        session = Mock()
        session.request.return_value = response(403, {'code': 50001})
        with self.assertRaises(SystemExit):
            discleanse.load_channel_choices(session, 'guild')

    def test_forbidden_active_threads_preserves_available_channels(self):
        for code in (50001, 50013, 20001):
            with self.subTest(code=code):
                session = Mock()
                session.request.side_effect = [
                    response(data=[{'id': 'chat', 'type': 0, 'name': 'general'}]),
                    response(403, {'code': code}),
                ]
                with redirect_stdout(io.StringIO()) as output:
                    choices = discleanse.load_channel_choices(session, 'guild')
                self.assertEqual(choices, [('chat', 'general (chat)')])
                self.assertIn('403', output.getvalue())
                self.assertIn('--channel ID', output.getvalue())

    def test_thread_listing_auth_block_still_stops(self):
        session = Mock()
        session.request.side_effect = [
            response(data=[{'id': 'chat', 'type': 0, 'name': 'general'}]),
            response(403, {'code': 40001}),
        ]
        with self.assertRaises(SystemExit):
            discleanse.load_channel_choices(session, 'guild')

    def run_main(self, argv, replies, selection=None, guild='guild', guild_selection=None):
        if isinstance(guild_selection, str):
            guild_selection = [guild_selection]
        session = Mock()
        session.headers = {}
        session.request.side_effect = replies
        with redirect_stdout(io.StringIO()), \
                patch.dict('os.environ', {'DISCORD_TOKEN': 'test-token'}), \
                patch('sys.argv', ['discleanse.py', '--workers', '1', '--skip-counts',
                                   *(['--guild', guild] if guild else []), *argv]), \
                patch('discleanse.requests.Session', return_value=session), \
                patch('discleanse.sys.stdin.isatty', return_value=True), \
                patch('discleanse.sys.stdout.isatty', return_value=True), \
                patch('discleanse.pick_channels', return_value=selection), \
                patch('discleanse.pick_guild', return_value=guild_selection), \
                patch('discleanse.pause'):
            discleanse.main()
        return session

    def test_guild_list_contains_names_and_ids_in_alphabetical_order(self):
        session = Mock()
        session.request.return_value = response(data=[
            {'id': '2', 'name': 'Zulu'}, {'id': '1', 'name': 'Alpha'},
        ])
        self.assertEqual(discleanse.load_guild_choices(session),
                         [('1', 'Alpha (1)'), ('2', 'Zulu (2)')])
        self.assertTrue(session.request.call_args.args[1].endswith('/users/@me/guilds'))

    def test_selected_guild_is_used_for_channel_listing_and_search(self):
        session = self.run_main(['--pick-channels', '--dry-run'], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data=[{'id': 'chosen', 'name': 'Chosen server'}]),
            response(data=[{'id': 'a', 'type': 0, 'name': 'general'}]),
            response(data={'threads': []}),
            response(data={'messages': [], 'total_results': 0}),
        ], selection=['a'], guild=None, guild_selection='chosen')
        urls = [c.args[1] for c in session.request.call_args_list]
        self.assertIn(discleanse.API + '/guilds/chosen/channels', urls)
        self.assertIn(discleanse.API + '/guilds/chosen/messages/search', urls)

    def test_server_selection_defaults_to_deleting_across_that_server(self):
        session = self.run_main([], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data=[{'id': 'chosen', 'name': 'Chosen server'}]),
            response(data={'messages': [[{
                'id': 'msg', 'channel_id': 'a', 'hit': True,
                'author': {'id': 'me'}, 'type': 0, 'content': 'hi',
            }]], 'total_results': 1}),
            response(204),
            response(data={'messages': [], 'total_results': 0}),
        ], guild=None, guild_selection='chosen')
        searches = [c for c in session.request.call_args_list if c.args[1].endswith('/search')]
        self.assertEqual(len(searches), 2)
        self.assertTrue(all('/guilds/chosen/' in c.args[1] for c in searches))
        self.assertNotIn('channel_id', searches[0].kwargs['params'])
        deletes = [c for c in session.request.call_args_list if c.args[0] == 'DELETE']
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0].args[1].endswith('/channels/a/messages/msg'))

    def test_explicit_dry_run_overrides_server_selection_deletion_default(self):
        session = self.run_main(['--dry-run'], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data=[{'id': 'chosen', 'name': 'Chosen server'}]),
            response(data={'messages': [[{
                'id': 'msg', 'channel_id': 'a', 'hit': True,
                'author': {'id': 'me'}, 'type': 0, 'content': 'hi',
            }]], 'total_results': 1}),
            response(data={'messages': [], 'total_results': 0}),
        ], guild=None, guild_selection='chosen')
        self.assertTrue(all(c.args[0] == 'GET' for c in session.request.call_args_list))

    def test_multiple_servers_are_searched_separately(self):
        session = self.run_main([], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data=[{'id': 'one', 'name': 'One'}, {'id': 'two', 'name': 'Two'}]),
            response(data={'messages': [], 'total_results': 0}),
            response(data={'messages': [], 'total_results': 0}),
        ], guild=None, guild_selection=['one', 'two'])
        searches = [c for c in session.request.call_args_list if c.args[1].endswith('/search')]
        self.assertEqual([c.args[1] for c in searches], [
            discleanse.API + '/guilds/one/messages/search',
            discleanse.API + '/guilds/two/messages/search',
        ])
        self.assertTrue(all(c.kwargs['params']['offset'] == 0 for c in searches))

    def test_cancel_server_picker_stops_before_channel_listing(self):
        session = self.run_main([], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data=[{'id': 'g', 'name': 'Server'}]),
        ], guild=None)
        self.assertEqual(session.request.call_count, 2)

    def test_no_guilds_stops_without_searching(self):
        session = self.run_main([], [
            response(data={'id': 'me', 'username': 'me'}), response(data=[]),
        ], guild=None)
        self.assertEqual(session.request.call_count, 2)

    def test_cancelling_picker_never_searches_or_deletes(self):
        session = self.run_main([], [response(data={'id': 'me', 'username': 'me'}),
                                   response(data=[{'id': 'a', 'type': 0, 'name': 'a'}]),
                                   response(data={'threads': []})])
        self.assertEqual(session.request.call_count, 3)
        self.assertTrue(all(c.args[0] == 'GET' for c in session.request.call_args_list))

    def test_picker_selection_scopes_search_and_dry_run_never_deletes(self):
        session = self.run_main([], [response(data={'id': 'me', 'username': 'me'}),
                                   response(data=[{'id': 'a', 'type': 0, 'name': 'a'}]),
                                   response(data={'threads': []}),
                                   response(data={'messages': [[{
                                       'id': 'msg', 'channel_id': 'a', 'hit': True,
                                       'author': {'id': 'me'}, 'type': 0, 'content': 'hi',
                                   }]], 'total_results': 1}),
                                   response(data={'messages': [], 'total_results': 0})],
                                selection=['a'])
        searches = [c for c in session.request.call_args_list if c.args[1].endswith('/search')]
        self.assertEqual(searches[0].kwargs['params']['channel_id'], ['a'])
        self.assertTrue(all(c.args[0] == 'GET' for c in session.request.call_args_list))

    def test_execute_does_not_delete_outside_selected_channel(self):
        session = self.run_main(['--channel', 'a', '--execute'], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data={'messages': [[{
                'id': 'msg', 'channel_id': 'other', 'hit': True,
                'author': {'id': 'me'}, 'type': 0, 'content': 'hi',
            }]], 'total_results': 1}),
            response(data={'messages': [], 'total_results': 0}),
        ])
        self.assertTrue(all(c.args[0] == 'GET' for c in session.request.call_args_list))

    def test_execute_deletes_selected_message(self):
        session = self.run_main(['--channel', 'a', '--execute'], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data={'messages': [[{
                'id': 'msg', 'channel_id': 'a', 'hit': True,
                'author': {'id': 'me'}, 'type': 0, 'content': 'hi',
            }]], 'total_results': 1}),
            response(204),
            response(data={'messages': [], 'total_results': 0}),
        ])
        deletes = [c for c in session.request.call_args_list if c.args[0] == 'DELETE']
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0].args[1].endswith('/channels/a/messages/msg'))

    def test_all_channels_bypasses_picker_and_searches_without_channel_filter(self):
        session = self.run_main(['--all-channels'], [
            response(data={'id': 'me', 'username': 'me'}),
            response(data={'messages': [], 'total_results': 0}),
        ])
        self.assertEqual(session.request.call_count, 2)
        self.assertNotIn('channel_id', session.request.call_args.kwargs['params'])

    def test_noninteractive_launch_requires_explicit_scope_before_network(self):
        with patch('sys.argv', ['discleanse.py', '--guild', 'guild']), \
                patch('discleanse.sys.stdin.isatty', return_value=False), \
                patch('discleanse.requests.Session') as session, \
                patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                discleanse.main()
            self.assertEqual(caught.exception.code, 2)
            session.assert_not_called()


if __name__ == '__main__':
    unittest.main()
