"""Credential-free HTTP boundary exercised with real httpx + fake transport."""
import asyncio
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infrastructure/github-broker'))
from policy import PolicyError

URL = 'https://logs.example.test/path?sig=DO-NOT-EXPOSE'


class LogTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_is_default_denied_and_only_canonical_exact_https_hosts_are_accepted(self):
        from job_logs import Downloader
        invalid = [URL, 'http://logs.example.test/path', 'https://logs.example.test:444/path',
                   'https://user@logs.example.test/path', 'https://logs.example.test/path#fragment',
                   'https://logs.example.test.evil/path', 'https://LOGS.example.test/path',
                   'https://logs.example.test./path', 'https://127.0.0.1/path',
                   'https://logs.example.test:443/path', 'https://logs.example.test\\@evil/path',
                   'HTTPS://logs.example.test/path']
        for url in invalid:
            downloader = Downloader(hosts=() if url == URL else ('logs.example.test',))
            with self.subTest(url=url), self.assertRaises(PolicyError) as caught:
                downloader.validate_url(url)
            self.assertNotIn(url, str(caught.exception))
            self.assertNotIn('DO-NOT-EXPOSE', str(caught.exception))

    async def test_separate_client_disables_environment_auth_cookies_redirects_and_forces_proxy_tls(self):
        from job_logs import Downloader
        observed = []
        def handle(request):
            observed.append(request)
            return httpx.Response(200, headers={'content-type': 'text/plain'}, stream=Stream([b'one\ntwo\nthree\n']))
        real_client = httpx.AsyncClient
        options = []
        def client(**kwargs):
            options.append(kwargs.copy())
            kwargs.pop('proxy')  # intercept only transport; production must specify it
            return real_client(**kwargs, transport=httpx.MockTransport(handle))
        with patch('job_logs.httpx.AsyncClient', side_effect=client):
            out = await Downloader(hosts=('logs.example.test',)).download(URL, start_line=2, lines=1)
        self.assertEqual(out['text'], 'two\n')
        self.assertTrue(out['input_complete'])
        self.assertTrue(out['untrusted'])
        self.assertNotIn('sig=', str(out))
        self.assertEqual(options[0]['proxy'], 'http://squid:3128')
        self.assertIs(options[0]['trust_env'], False)
        self.assertIs(options[0]['verify'], True)
        self.assertIs(options[0]['follow_redirects'], False)
        self.assertNotIn('auth', options[0])
        self.assertNotIn('cookies', options[0])
        self.assertFalse({'authorization', 'cookie', 'proxy-authorization'} & set(observed[0].headers))
        self.assertEqual(observed[0].headers['accept-encoding'], 'identity')

    async def fetch(self, stream, headers=None, status=200, **kwargs):
        from job_logs import Downloader
        real_client = httpx.AsyncClient
        def handle(request):
            return httpx.Response(status, headers=headers or {'content-type': 'text/plain'}, stream=stream)
        def client(**options):
            options.pop('proxy')
            return real_client(**options, transport=httpx.MockTransport(handle))
        with patch('job_logs.httpx.AsyncClient', side_effect=client):
            return await Downloader(hosts=('logs.example.test',)).download(URL, **kwargs)

    async def test_stream_bounds_count_actual_bytes_and_do_not_claim_incomplete_tail_is_true_tail(self):
        from job_logs import MAX_INPUT, MAX_LINE, MAX_LINES, MAX_OUTPUT
        for chunks, limit in (([b'x' * 1000 + b'\n'] * (MAX_INPUT // 1001 + 2), 'input_bytes'),
                              ([b'x' * (MAX_LINE + 1)], 'line_length'),
                              ([b'x\n' * (MAX_LINES + 1)], 'line_count')):
            stream = Stream(chunks)
            out = await self.fetch(stream, tail=True)
            self.assertFalse(out['input_complete'])
            self.assertFalse(out['true_tail'])
            self.assertIn(limit, out['limits_reached'])
            self.assertLessEqual(out['input_bytes'], MAX_INPUT)
            self.assertLessEqual(len(out['text'].encode()), MAX_OUTPUT)
            self.assertTrue(stream.closed)
        stream = Stream([b'one\ntwo\nthree\n'])
        out = await self.fetch(stream, tail=True, lines=2)
        self.assertEqual(out['text'], 'two\nthree\n')
        self.assertTrue(out['true_tail'])
        self.assertEqual(out['input_bytes'], 14)

    async def test_hard_byte_cap_stops_without_reading_past_budget_and_counts_received_bytes(self):
        from job_logs import MAX_INPUT
        stream = Stream([b'a\n' * 2048] * (MAX_INPUT // 4096 + 5))
        with patch('job_logs.MAX_LINES', MAX_INPUT):
            out = await self.fetch(stream, tail=True)
        self.assertEqual(out['input_bytes'], MAX_INPUT)
        self.assertFalse(out['input_complete'])
        self.assertIn('input_bytes', out['limits_reached'])
        self.assertEqual(stream.yielded, MAX_INPUT // 4096)

    async def test_reject_redirects_compression_archives_binary_and_close(self):
        for headers, status, body in (({'location': URL}, 302, b''),
                                     ({'content-encoding': 'gzip', 'content-type': 'text/plain'}, 200, b'bad'),
                                     ({'content-type': 'application/zip'}, 200, b'PK\x03\x04'),
                                     ({'content-type': 'text/plain'}, 200, b'PK\x03\x04binary'),
                                     ({'content-type': 'text/plain'}, 200, b'\x00binary')):
            stream = Stream([body])
            with self.subTest(headers=headers), self.assertRaises(PolicyError) as caught:
                await self.fetch(stream, headers=headers, status=status)
            self.assertNotIn('DO-NOT-EXPOSE', str(caught.exception))
            self.assertTrue(stream.closed)

    async def test_timeouts_cancellation_and_concurrency_close_streams_without_url_errors(self):
        from job_logs import DOWNLOAD_SLOTS
        slow = Stream([b'hello'], delay=0.1)
        with patch('job_logs.OVERALL_SECONDS', 0.02):
            with self.assertRaises(PolicyError) as caught:
                await self.fetch(slow)
        self.assertNotIn('DO-NOT-EXPOSE', str(caught.exception))
        self.assertTrue(slow.closed)
        stream = Stream([b'hello'], delay=10)
        task = asyncio.create_task(self.fetch(stream))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)
        await DOWNLOAD_SLOTS.acquire()
        try:
            with self.assertRaisesRegex(PolicyError, 'busy'):
                await self.fetch(Stream([b'hello']))
        finally:
            DOWNLOAD_SLOTS.release()

    async def test_multiline_private_key_state_survives_chunks_and_window_selection(self):
        for key_type in ('PRIVATE KEY', 'RSA PRIVATE KEY', 'OPENSSH PRIVATE KEY'):
            raw = (b'p' * 4080 + b'\n' + f'-----BEGIN {key_type}-----\n'.encode()
                   + b'cHJvYmUtc2VjcmV0LWtleS1ib2R5\nYW5vdGhlci1zZWNyZXQtbGluZQ==\n'
                   + f'-----END {key_type}-----\n'.encode() + b'after key\n')
            stream = Stream([raw[n:n + 13] for n in range(0, len(raw), 13)])
            with self.subTest(key_type=key_type):
                out = await self.fetch(stream, start_line=3, lines=4)
                self.assertEqual(out['text'], '[REDACTED_PRIVATE_KEY]\n' * 3 + 'after key\n')
                self.assertEqual(out['lines_observed'], 6)
                self.assertTrue(out['input_complete'])
                self.assertTrue(stream.closed)
        out = await self.fetch(Stream([b'-----BEGIN PRIVATE KEY-----\n', b'cHJvYmUtc2VjcmV0']), tail=True)
        self.assertEqual(out['text'], '[REDACTED_PRIVATE_KEY]\n' * 2)
        self.assertNotIn('cHJvYmU', out['text'])

    async def test_sanitization_redaction_and_output_cap(self):
        from job_logs import MAX_OUTPUT
        raw = (b'\x1b[31mred\x1b[0m\x1b]8;;https://evil\x07link\x1b]8;;\x07\r\x08'
               b' token=topsecret Authorization: Bearer hidden\n'
               b'ghp_abcdefghijklmnopqrstuvwxyz0123456789\n'
               b'https://logs.example.test/path?sig=secret\n'
               b'-----BEGIN PRIVATE KEY-----\nsecretmaterial\n-----END PRIVATE KEY-----\n')
        out = await self.fetch(Stream([raw]))
        for secret in ('topsecret', 'hidden', 'ghp_', 'sig=', 'secretmaterial', '\x1b', '\r', '\x08'):
            self.assertNotIn(secret, out['text'])
        self.assertIn('red', out['text'])
        out = await self.fetch(Stream([('é' * 3000 + '\n').encode()] * 100), lines=500)
        self.assertLessEqual(len(out['text'].encode()), MAX_OUTPUT)
        self.assertIn('output_bytes', out['limits_reached'])


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.yielded = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True
