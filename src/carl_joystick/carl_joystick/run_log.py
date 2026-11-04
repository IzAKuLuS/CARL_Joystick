"""Bounded, asynchronous JSONL flight recorder. Never writes on a control callback."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid


def clean(value, depth=0):
    """Bound diagnostic input and exclude secrets, including nested browser data."""
    if depth > 6:
        return '<depth limit>'
    if isinstance(value, dict):
        return {str(k)[:80]: clean(v, depth + 1) for k, v in list(value.items())[:80]
                if not any(word in str(k).lower() for word in ('token', 'password', 'secret', 'authorization'))}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        return value[:8192]
    if isinstance(value, float):
        import math
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (int, bool)):
        return value
    return str(value)[:2048]


class RunLog:
    def __init__(self, directory, *, max_bytes=32 * 1024 * 1024, max_files=32,
                 queue_size=8192, on_error=lambda message: None):
        self.run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12]
        self.directory = str(Path(directory).expanduser() / self.run_id)
        self.max_bytes, self.max_files = max_bytes, max_files
        if max_bytes < 1024 or max_files < 2 or queue_size < 1:
            raise ValueError('Invalid run-log capacity')
        self.queue = queue.Queue(queue_size)
        self.lock = threading.Lock()
        self.on_error = on_error
        self.sequence = self.dropped = self.written = 0
        self.error = None
        self.closed = False
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._write, name='carl-run-log', daemon=True)
        self.thread.start()

    def emit(self, event, **fields):
        # Ordering, event timestamp and queue insertion are one operation across threads.
        with self.lock:
            if self.closed:
                return
            self.sequence += 1
            record = dict(schema=1, run_id=self.run_id, seq=self.sequence,
                          utc=datetime.now(timezone.utc).isoformat(),
                          wall_ns=time.time_ns(), mono_ns=time.monotonic_ns(),
                          event=event, data=clean(fields), dropped_total=self.dropped)
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                self.dropped += 1

    def status(self):
        return dict(run_id=self.run_id, directory=self.directory, queued=self.queue.qsize(),
                    written=self.written, dropped=self.dropped, error=self.error,
                    writer_alive=self.thread.is_alive(), closed=self.closed)

    def close(self, timeout=2):
        with self.lock:
            self.closed = True
        self.done.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.error = 'writer shutdown timed out; tail may be missing'
            self.on_error(self.error)

    def _write(self):
        stream = None
        try:
            Path(self.directory).mkdir(parents=True, exist_ok=False)
            part, size, last_sync = 0, 0, time.monotonic()
            while not self.done.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=0.2)
                except queue.Empty:
                    if stream:
                        stream.flush()
                    continue
                record['write_mono_ns'] = time.monotonic_ns()
                record['write_lag_ms'] = (record['write_mono_ns'] - record['mono_ns']) / 1_000_000
                line = json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n'
                length = len(line.encode())
                if stream is None or size + length > self.max_bytes:
                    if stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                        stream.close()
                    part += 1
                    # Keep the first segment (manifest) plus a bounded recent tail.
                    if part > self.max_files:
                        old = Path(self.directory, f'events-{part - self.max_files + 1:06}.jsonl')
                        old.unlink(missing_ok=True)
                    stream = Path(self.directory, f'events-{part:06}.jsonl').open('w', encoding='utf-8')
                    size = 0
                stream.write(line)
                size += length
                self.written += 1
                if time.monotonic() - last_sync >= 1:
                    stream.flush()
                    os.fsync(stream.fileno())
                    last_sync = time.monotonic()
        except Exception as error:
            self.error = f'{type(error).__name__}: {error}'
            self.on_error('CARL run logging failed: ' + self.error)
        finally:
            if stream:
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                except OSError as error:
                    self.error = str(error)
                    self.on_error('CARL run logging failed: ' + self.error)
