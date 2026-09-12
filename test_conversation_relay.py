"""Offline relay tests: no app endpoints or provider requests are invoked."""
import asyncio
import copy
import importlib
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

# Import route definitions without loading local secrets or constructing clients.
with patch('dotenv.load_dotenv'), patch.dict('os.environ', {}, clear=True):
    app_module = importlib.import_module('app')

from fastapi import WebSocketDisconnect
from scenarios import load_scenario


class FakeStream:
    def __init__(self, chunks=(), stop_reason='end_turn'):
        self.queue = asyncio.Queue()
        for chunk in chunks:
            self.queue.put_nowait(chunk)
        self.stop_reason = stop_reason
        self.closed = False
        self.started = asyncio.Event()
        self.text_stream = self.iter_text()

    async def __aenter__(self):
        self.started.set()
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_text(self):
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self.stop_reason)


class FakeClient:
    def __init__(self, streams):
        self.streams = iter(streams)
        self.requests = []
        self.messages = self

    def stream(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return next(self.streams)


class FakeWebSocket:
    def __init__(self, client):
        self.app = SimpleNamespace(state=SimpleNamespace(anthropic_client=client))
        self.incoming = asyncio.Queue()
        self.sent = []
        self.accepted = False
        self.close_code = None
        self.block_send = None
        self.send_entered = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def close(self, code, reason):
        self.close_code = code

    async def receive_json(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def send_json(self, message):
        self.send_entered.set()
        if self.block_send is not None:
            await self.block_send.wait()
        self.sent.append(message)

    def prompt(self, text, last=True):
        self.incoming.put_nowait({'type': 'prompt', 'voicePrompt': text, 'last': last})


async def eventually(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(poll(), timeout=2)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.running = []
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.scenario_loader = patch.object(app_module, 'load_relay_scenario', return_value=load_scenario())
        self.scenario_loader.start()
        self.addCleanup(self.scenario_loader.stop)

    async def asyncTearDown(self):
        for ws, task in self.running:
            if not task.done():
                ws.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(asyncio.gather(*(task for _, task in self.running)), 2)

    def start(self, client):
        ws = FakeWebSocket(client)
        ws.incoming.put_nowait({'type': 'setup', 'callControlId': 'offline-call'})
        task = asyncio.create_task(app_module.conversation_relay(ws))
        self.running.append((ws, task))
        return ws

    async def test_streaming_and_multiturn_history(self):
        first = FakeStream(['Hello ', 'there.', None])
        second = FakeStream(['Again.', None])
        client = FakeClient([first, second])
        ws = self.start(client)
        ws.prompt('interim', last=False)
        ws.prompt('How may I help you?')
        await eventually(lambda: first.closed)
        self.assertEqual(ws.sent, [
            {'type': 'text', 'token': 'Hello ', 'last': False},
            {'type': 'text', 'token': 'there.', 'last': True},
        ])
        ws.prompt('Next')
        await eventually(lambda: second.closed)
        self.assertEqual(client.requests[1]['messages'], [
            {'role': 'user', 'content': 'How may I help you?'},
            {'role': 'assistant', 'content': 'Hello there.'},
            {'role': 'user', 'content': 'Next'},
        ])
        self.assertEqual(client.requests[0]['model'], 'claude-haiku-4-5-20251001')

    async def test_unexpected_interrupt_does_not_cancel_noninterruptible_playback(self):
        first = FakeStream(['Still ', 'speaking.', None])
        client = FakeClient([first])
        ws = self.start(client)
        ws.block_send = asyncio.Event()
        ws.prompt('How may I help you?')
        await asyncio.wait_for(ws.send_entered.wait(), 2)
        ws.incoming.put_nowait({'type': 'interrupt', 'utteranceUntilInterrupt': ''})
        await asyncio.sleep(0)
        self.assertFalse(first.closed)
        ws.block_send.set()
        await eventually(lambda: first.closed)
        self.assertEqual(ws.sent, [
            {'type': 'text', 'token': 'Still ', 'last': False},
            {'type': 'text', 'token': 'speaking.', 'last': True},
        ])

    async def test_unexpected_interrupt_preserves_completed_assistant_history(self):
        first = FakeStream(['Finished generating.', None])
        second = FakeStream(['Okay.', None])
        client = FakeClient([first, second])
        ws = self.start(client)
        ws.prompt('How may I help you?')
        await eventually(lambda: first.closed)
        ws.incoming.put_nowait({'type': 'interrupt', 'utteranceUntilInterrupt': 'Finished'})
        ws.prompt('Stop')
        await eventually(lambda: second.closed)
        self.assertEqual(
            [m['role'] for m in client.requests[1]['messages']],
            ['user', 'assistant', 'user'],
        )

    async def test_new_final_prompt_supersedes_active_generation(self):
        first = FakeStream(['Already sent ', 'stale buffered text'])
        second = FakeStream(['New response.', None])
        client = FakeClient([first, second])
        ws = self.start(client)
        ws.prompt('Old question')
        await eventually(lambda: len(ws.sent) >= 1)
        ws.prompt('New question')
        await eventually(lambda: first.closed and second.closed)
        self.assertEqual(
            [frame['token'] for frame in ws.sent],
            ['Already sent ', 'New response.'],
        )
        self.assertTrue(all(m['role'] == 'user' for m in client.requests[1]['messages']))

    async def test_calls_are_isolated_and_disconnect_closes_stream(self):
        first, second = FakeStream(), FakeStream(['Other reply.', None])
        client = FakeClient([first, second])
        ws1, ws2 = self.start(client), self.start(client)
        ws1.prompt('Call one')
        await first.started.wait()
        ws2.prompt('Call two')
        await eventually(lambda: second.closed)
        self.assertEqual(client.requests[1]['messages'], [{'role': 'user', 'content': 'Call two'}])
        ws1.incoming.put_nowait(WebSocketDisconnect())
        await eventually(lambda: first.closed)
        self.assertEqual(ws1.sent, [])
        self.assertEqual(len(ws2.sent), 1)

    async def test_partial_prompt_does_not_start_generation(self):
        stream = FakeStream(['Should not run.', None])
        client = FakeClient([stream])
        ws = self.start(client)
        ws.prompt('How can I help you', last=False)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(client.requests, [])
        self.assertEqual(ws.sent, [])

    async def test_preamble_prompts_do_not_start_generation(self):
        stream = FakeStream(['Should not run.', None])
        client = FakeClient([stream])
        ws = self.start(client)
        for text in (
            'This call may be recorded for quality assurance.',
            'For Spanish, oprima dos.',
            'Thank you for calling PGAI Clinic.',
        ):
            ws.prompt(text)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(client.requests, [])
        self.assertEqual(ws.sent, [])

    async def test_opt_in_conversation_logging_redacts_secrets_and_ignores_interim_text(self):
        stream = FakeStream(['I can help with terminal-secret.', None])
        client = FakeClient([stream])
        output = io.StringIO()
        with patch.object(app_module, 'LOG_CONVERSATION', True), \
             patch.dict('os.environ', {'TEST_API_KEY': 'terminal-secret'}), \
             redirect_stdout(output):
            ws = self.start(client)
            ws.prompt('interim terminal-secret', last=False)
            ws.prompt('This call may be recorded for quality assurance.')
            ws.prompt('What information do you need? terminal-secret')
            await eventually(lambda: stream.closed)
        logged = output.getvalue()
        self.assertNotIn('interim terminal-secret', logged)
        self.assertIn(
            'CONVERSATION PGAI: [ignored: recording_disclosure] '
            'This call may be recorded for quality assurance.',
            logged,
        )
        self.assertIn('CONVERSATION PGAI: What information do you need? [REDACTED]', logged)
        self.assertIn('CONVERSATION PATIENT: I can help with [REDACTED]', logged)
        self.assertNotIn('terminal-secret', logged)

    async def test_conversation_logging_is_silent_when_disabled(self):
        stream = FakeStream(['A complete reply.', None])
        client = FakeClient([stream])
        output = io.StringIO()
        with patch.object(app_module, 'LOG_CONVERSATION', False), redirect_stdout(output):
            ws = self.start(client)
            ws.prompt('What appointment would you like?')
            await eventually(lambda: stream.closed)
        self.assertNotIn('CONVERSATION PGAI:', output.getvalue())
        self.assertNotIn('CONVERSATION PATIENT:', output.getvalue())

    async def test_actionable_prompt_ends_on_one_nonempty_final_frame(self):
        stream = FakeStream(['I would like ', 'to schedule a physical.', None])
        client = FakeClient([stream])
        ws = self.start(client)
        ws.prompt('How may I help you today?')
        await eventually(lambda: stream.closed)
        self.assertEqual(ws.sent, [
            {'type': 'text', 'token': 'I would like ', 'last': False},
            {'type': 'text', 'token': 'to schedule a physical.', 'last': True},
        ])
        self.assertEqual(sum(frame['last'] is True for frame in ws.sent), 1)
        self.assertTrue(ws.sent[-1]['token'])
        self.assertFalse(any(frame['token'] == '' for frame in ws.sent))
        self.assertEqual(client.requests[0]['messages'], [
            {'role': 'user', 'content': 'How may I help you today?'},
        ])

    async def test_single_chunk_generation_is_delivered_not_discarded(self):
        stream = FakeStream(['Yes, that works for me.', None])
        client = FakeClient([stream])
        ws = self.start(client)
        ws.prompt('Would Tuesday at 3:30 PM work?')
        await eventually(lambda: stream.closed)
        self.assertEqual(ws.sent, [
            {'type': 'text', 'token': 'Yes, that works for me.', 'last': True},
        ])

    async def test_empty_generation_sends_no_frames(self):
        stream = FakeStream([None])
        client = FakeClient([stream])
        ws = self.start(client)
        ws.prompt('What appointment would you like?')
        await eventually(lambda: stream.closed)
        self.assertEqual(ws.sent, [])

    async def test_error_and_truncation_do_not_commit_assistant(self):
        failed = FakeStream(['Partial', RuntimeError('fake provider failure')])
        truncated = FakeStream(['Truncated', None], stop_reason='max_tokens')
        final = FakeStream(['Complete', None])
        client = FakeClient([failed, truncated, final])
        ws = self.start(client)
        for index, stream in enumerate([failed, truncated, final]):
            ws.prompt(str(index))
            await eventually(lambda: stream.closed)
        self.assertTrue(all(m['role'] == 'user' for m in client.requests[2]['messages']))

    async def test_invalid_input_and_missing_key(self):
        ws = self.start(None)
        await eventually(lambda: ws.close_code is not None)
        self.assertEqual(ws.close_code, 1011)
        stream = FakeStream(['OK', None])
        client = FakeClient([stream])
        ws = self.start(client)
        for message in [ValueError(), [], {'type': 'prompt', 'voicePrompt': 123, 'last': True},
                        {'type': 'prompt', 'voicePrompt': '', 'last': True}, {'type': 'dtmf'}]:
            ws.incoming.put_nowait(message)
        ws.prompt('Valid')
        await eventually(lambda: stream.closed)
        self.assertEqual(len(client.requests), 1)

    async def test_lifespan_reuses_and_closes_client(self):
        from unittest.mock import AsyncMock
        fake = SimpleNamespace(close=AsyncMock())
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'offline-placeholder'}), \
             patch.object(app_module, 'AsyncAnthropic', return_value=fake) as constructor:
            async with app_module.lifespan(app_module.app):
                self.assertIs(app_module.app.state.anthropic_client, fake)
                constructor.assert_called_once()
            fake.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
