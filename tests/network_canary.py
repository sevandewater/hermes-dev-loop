"""Disposable, credential-free TCP/UDP targets on the proxy's external network."""
import asyncio
import json
import struct


async def tcp(reader, writer):
    try:
        data = await asyncio.wait_for(reader.read(4096), 5)
        if data:
            print(json.dumps({"event": "tcp", "port": writer.get_extra_info("sockname")[1]}), flush=True)
            if writer.get_extra_info("sockname")[1] == 53:
                query = data[2:]
                reply = query[:2] + b"\x81\x80" + query[4:6] + b"\0\0\0\0\0\0" + query[12:]
                writer.write(struct.pack("!H", len(reply)) + reply)
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\ncanary")
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


class DNS(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        print(json.dumps({"event": "udp", "port": 53}), flush=True)
        self.transport.sendto(data[:2] + b"\x81\x80" + data[4:6] + b"\0\0\0\0\0\0" + data[12:], address)


async def main():
    servers = [await asyncio.start_server(tcp, "0.0.0.0", port) for port in (53, 80, 443)]
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(DNS, local_addr=("0.0.0.0", 53))
    print("READY", flush=True)
    try:
        await asyncio.sleep(300)
    finally:
        transport.close()
        for server in servers:
            server.close()


asyncio.run(main())
