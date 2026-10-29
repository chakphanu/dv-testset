# SPDX-License-Identifier: MIT
"""IAX2 wire tests against a loopback fake peer."""
import struct
import socket
import hashlib
import threading
import time
import unittest
from dvtest.proto.iax2 import Client, full_frame, parse_full, information, parse_information, swap_pcm


class WireTests(unittest.TestCase):
    def test_full_header_and_retransmit_bit(self):
        data = full_frame(123, 456, 0x12345678, 255, 9, 6, 8, b"abc", retransmit=True)
        self.assertEqual(data[:8], bytes.fromhex("807b81c812345678"))
        frame = parse_full(data)
        self.assertEqual((frame.source, frame.destination, frame.timestamp), (123, 456, 0x12345678))
        self.assertEqual((frame.out_sequence, frame.in_sequence, frame.kind, frame.subclass), (255, 9, 6, 8))
        self.assertEqual(frame.payload, b"abc")
        with self.assertRaises(ValueError):
            parse_full(data[:11])
        with self.assertRaises(ValueError):
            parse_full(b"\x00" + data[1:])
        stop_tone = data[:11] + b"\xff" + data[12:]
        self.assertEqual(parse_full(stop_tone).subclass, -1)
        self.assertEqual(full_frame(1, 2, 3, 4, 5, 4, -1)[11], 255)

    def test_information_lengths(self):
        data = information(6, b"rig") + information(11, struct.pack("!H", 2))
        self.assertEqual(parse_information(data), {6: b"rig", 11: b"\x00\x02"})
        with self.assertRaises(ValueError):
            parse_information(data[:-1])
        with self.assertRaises(ValueError):
            information(6, b"x" * 256)

    def test_signed_network_pcm_samples(self):
        samples = [-32768, -1, 0, 1, 32767] * 32
        little = struct.pack("<160h", *samples)
        big = struct.pack(">160h", *samples)
        self.assertEqual(swap_pcm(little), big)
        self.assertEqual(swap_pcm(big), little)
        with self.assertRaises(ValueError):
            swap_pcm(little[:-1])

    def test_allstar_redundant_radio_handshake(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            server.settimeout(0.5)
            caller = Client("127.0.0.1", "test-only", port=server.getsockname()[1])
            try:
                caller.remote = 321
                packet = full_frame(321, caller.local, 0, 0, 0, 7, 0, b"!NEWKEY!\0")
                caller.handle(packet, server.getsockname())
                self.assertTrue(caller.link_ready)
                parse_full(server.recvfrom(512)[0])
                caller.key()
                key = parse_full(server.recvfrom(512)[0])
                self.assertEqual((key.kind, key.subclass), (4, 12))
                caller.unkey()
                unkey = parse_full(server.recvfrom(512)[0])
                self.assertEqual((unkey.kind, unkey.subclass), (4, 13))
            finally:
                caller.socket.close()

    def test_authentication_after_lost_new_and_duplicate_challenge(self):
        errors = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            server.settimeout(2)
            caller = Client("127.0.0.1", "test-only", port=server.getsockname()[1])

            def peer():
                try:
                    first, address = server.recvfrom(512)
                    self.assertFalse(first[2] & 0x80)
                    second, address = server.recvfrom(512)
                    self.assertTrue(second[2] & 0x80)
                    new = parse_full(second)
                    self.assertEqual((new.kind, new.subclass), (6, 1))
                    challenge = information(14, b"\0\2") + information(15, b"rig-challenge")
                    auth = full_frame(321, new.source, 0, 0, 1, 6, 8, challenge)
                    server.sendto(auth, address)
                    while True:
                        frame = parse_full(server.recvfrom(1024)[0])
                        if (frame.kind, frame.subclass) == (6, 9):
                            break
                    elements = parse_information(frame.payload)
                    self.assertEqual(elements[16], hashlib.md5(b"rig-challengetest-only").hexdigest().encode())
                    server.sendto(auth, address)
                    server.sendto(full_frame(321, new.source, 1, 1, 2, 6, 7,
                        information(9, struct.pack("!I", 64))), address)
                    server.sendto(full_frame(321, new.source, 2, 2, 2, 4, 4), address)
                    while True:
                        frame = parse_full(server.recvfrom(1024)[0])
                        self.assertNotEqual((frame.kind, frame.subclass), (6, 9))
                        if frame.kind == 7:
                            break
                    self.assertEqual(frame.payload.rstrip(b"\0"), b"!NEWKEY!")
                    server.sendto(full_frame(321, new.source, 3, 3, 3, 7, 0, b"!NEWKEY!\0"), address)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=peer)
            thread.start()
            try:
                caller.call("1901", timeout=3)
                self.assertTrue(caller.accepted and caller.answered and caller.link_ready)
                self.assertGreaterEqual(caller.retransmissions, 1)
            finally:
                caller.socket.close()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)

    def test_unresponsive_peer_has_bounded_retransmission(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            caller = Client("127.0.0.1", "test-only", port=server.getsockname()[1])
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    caller.call("1901", timeout=10)
                self.assertLess(time.monotonic() - started, 6)
                self.assertEqual(caller.retransmissions, 5)
                server.setblocking(False)
                packets = []
                while True:
                    try:
                        packets.append(server.recvfrom(512)[0])
                    except BlockingIOError:
                        break
                self.assertEqual(len(packets), 6)
            finally:
                caller.socket.close()

