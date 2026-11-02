"""Tests for the private on-demand FFmpeg relay."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import pytest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "motorola_nursery"
    / "transcoder.py"
)
SPEC = importlib.util.spec_from_file_location("motorola_transcoder", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
transcoder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transcoder
SPEC.loader.exec_module(transcoder)


@pytest.mark.usefixtures("socket_enabled")
class FfmpegRelayTest(unittest.TestCase):
    """Exercise the private HTTP endpoint and process lifecycle."""

    def test_relay_is_private_on_demand_and_cleans_up(self) -> None:
        """Start FFmpeg only for the secret URL and stop it cleanly."""
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(self._run_relay_test(Path(directory)))

    async def _run_relay_test(self, tmp_path: Path) -> None:
        fake_ffmpeg = tmp_path / "fake-ffmpeg"
        fake_ffmpeg.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "while True:\n"
            "    sys.stdout.buffer.write(b'fake-mpeg-ts-payload')\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(0.02)\n"
        )
        fake_ffmpeg.chmod(0o755)
        errors: list[int] = []
        relay = transcoder.FfmpegRelay(
            str(fake_ffmpeg), "rtsp://private", errors.append
        )
        await relay.start()
        try:
            parsed = urlsplit(relay.url)
            self.assertEqual(parsed.hostname, "127.0.0.1")
            self.assertEqual(relay.active_processes, 0)

            bad_reader, bad_writer = await asyncio.open_connection(
                parsed.hostname, parsed.port
            )
            bad_writer.write(b"GET /wrong HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await bad_writer.drain()
            self.assertTrue((await bad_reader.read()).startswith(b"HTTP/1.1 404"))
            bad_writer.close()
            await bad_writer.wait_closed()
            self.assertEqual(relay.active_processes, 0)

            reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            writer.write(
                f"GET {parsed.path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
            )
            await writer.drain()
            response = await reader.readuntil(b"fake-mpeg-ts-payload")
            self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
            self.assertIn(b"Content-Type: video/MP2T", response)
            self.assertEqual(relay.active_processes, 1)
            await asyncio.wait_for(relay.stop(), timeout=3)
            self.assertEqual(relay.active_processes, 0)
            writer.close()
            await writer.wait_closed()
        finally:
            await relay.stop()

        self.assertEqual(relay.active_processes, 0)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
