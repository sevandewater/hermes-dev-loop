"""Credential-free, default-deny single-job text consumption. Never log URLs."""
import asyncio
from collections import deque
import ipaddress
import logging
import re
from urllib.parse import urlsplit

import httpx
from policy import PolicyError
from actions import integer

PROXY = 'http://squid:3128'
MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 64 * 1024
MAX_LINE = 8192
MAX_LINES = 100_000
OVERALL_SECONDS = 15
DOWNLOAD_SLOTS = asyncio.Semaphore(1)
# httpx logs complete request URLs at INFO; prohibit that inside the broker.
for _logger in ('httpx', 'httpcore', 'httpcore.http11', 'httpcore.http2',
                'httpcore.connection', 'httpcore.proxy', 'httpcore.socks'):
    logging.getLogger(_logger).disabled = True


def clean(text):
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)', '', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = ''.join(c for c in text if c == '\n' or (c.isprintable() and c != '\x1b'))
    text = re.sub(r'https?://\S+', '[REDACTED_URL]', text, flags=re.I)
    text = re.sub(r'(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)', '[REDACTED]', text)
    text = re.sub(r'(?i)(authorization\s*:\s*(?:bearer\s+|basic\s+)?|(?:token|password|secret|api[_-]?key)\s*[=:]\s*)\S+',
                  r'\1[REDACTED]', text)
    return text


def approved_hosts(hosts):
    values = set()
    for host in hosts:
        if (not isinstance(host, str) or len(host) > 253 or '.' not in host
                or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)+', host)):
            raise PolicyError('Invalid exact log-storage hostname configuration.')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            values.add(host)
        else:
            raise PolicyError('IP log-storage destinations are forbidden.')
    return frozenset(values)


class Downloader:
    def __init__(self, hosts=()):
        self.hosts = approved_hosts(hosts)

    def validate_url(self, url):
        try:
            value = urlsplit(url)
            if (not isinstance(url, str) or not url.startswith('https://') or len(url) > 16_384 or value.scheme != 'https'
                    or value.netloc not in self.hosts or value.username is not None
                    or value.fragment or '#' in url or not value.path.startswith('/')
                    or any(ord(c) <= 32 or ord(c) >= 127 or c == '\\' for c in url)):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PolicyError('Log URL rejected by exact-host HTTPS policy; owner activation may be needed.') from None

    async def download(self, url, start_line=1, lines=100, tail=False):
        self.validate_url(url)
        integer(start_line, 100_000)
        integer(lines, 500)
        if type(tail) is not bool:
            raise PolicyError('Invalid tail selector.')
        if DOWNLOAD_SLOTS.locked():
            raise PolicyError('Log downloader busy; retry later.')
        async with DOWNLOAD_SLOTS:
            try:
                async with asyncio.timeout(OVERALL_SECONDS):
                    async with httpx.AsyncClient(proxy=PROXY, trust_env=False, verify=True,
                            follow_redirects=False, timeout=httpx.Timeout(5, connect=3, pool=1),
                            headers={'Accept-Encoding': 'identity', 'Accept': 'text/plain'}) as client:
                        async with client.stream('GET', url) as response:
                            if response.status_code != 200:
                                raise PolicyError('Log evidence unavailable; permission/not-found/expired cannot be distinguished.')
                            if (response.headers.get('content-encoding', 'identity').lower() != 'identity'
                                    or response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/plain'):
                                raise PolicyError('Only uncompressed plain-text job logs are supported.')
                            return await self.consume(response, start_line, lines, tail)
            except PolicyError:
                raise
            except Exception:
                raise PolicyError('Log evidence unavailable; timeout/network/expired/permission cause unverified.') from None

    async def consume(self, response, start, lines, tail):
        selected = deque(maxlen=lines)
        pending = bytearray()
        seen_bytes = 0
        seen_lines = 0
        limits = []
        in_key = False

        def accept(raw):
            nonlocal seen_lines, in_key
            seen_lines += 1
            text = raw.decode('utf-8', errors='replace')
            if '-----BEGIN ' in text and 'PRIVATE KEY-----' in text:
                in_key = True
            if in_key:
                rendered = '[REDACTED_PRIVATE_KEY]\n'
                if '-----END ' in text and 'PRIVATE KEY-----' in text:
                    in_key = False
            else:
                rendered = clean(text)
            if tail or start <= seen_lines < start + lines:
                selected.append(rendered)

        async for chunk in response.aiter_raw(chunk_size=4096):
            # Reject binary/archive signatures even if a server lies about MIME.
            if b'\x00' in chunk or (seen_bytes == 0 and chunk.startswith((b'PK\x03\x04', b'\x1f\x8b'))):
                raise PolicyError('Binary/archive job logs are unsupported.')
            available = MAX_INPUT - seen_bytes
            overflow = len(chunk) > available
            chunk = chunk[:available]
            seen_bytes += len(chunk)
            pending.extend(chunk)
            while b'\n' in pending:
                end = pending.index(10) + 1
                if end > MAX_LINE:
                    limits.append('line_length')
                    break
                if seen_lines == MAX_LINES:
                    limits.append('line_count')
                    break
                accept(bytes(pending[:end]))
                del pending[:end]
            if overflow or seen_bytes == MAX_INPUT:
                limits.append('input_bytes')
            if len(pending) > MAX_LINE and not limits:
                limits.append('line_length')
            if limits:
                break
        else:
            if pending:
                if seen_lines == MAX_LINES:
                    limits.append('line_count')
                else:
                    accept(bytes(pending))
        complete = not limits
        encoded = ''.join(selected).encode('utf-8')
        if len(encoded) > MAX_OUTPUT:
            limits.append('output_bytes')
            encoded = encoded[:MAX_OUTPUT]
        return {'text': encoded.decode('utf-8', errors='ignore'), 'input_bytes': seen_bytes,
                'lines_observed': seen_lines, 'input_complete': complete,
                'true_tail': tail and complete and 'output_bytes' not in limits,
                'limits_reached': limits, 'start_line': None if tail else start,
                'untrusted': True, 'warning': 'Untrusted job output. Best-effort redaction is not guaranteed.'}
