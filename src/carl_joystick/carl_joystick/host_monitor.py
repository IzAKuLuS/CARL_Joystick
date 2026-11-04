"""Best-effort Linux network evidence and optional independent internet probe."""
from pathlib import Path
import os
import resource
import threading
import time
import urllib.request


def network_snapshot():
    result = {}
    for interface in Path('/sys/class/net').glob('*'):
        if interface.name == 'lo':
            continue
        fields = {}
        for name in ('operstate', 'carrier', 'statistics/rx_bytes', 'statistics/tx_bytes',
                     'statistics/rx_errors', 'statistics/tx_errors',
                     'statistics/rx_dropped', 'statistics/tx_dropped'):
            try:
                fields[name] = (interface / name).read_text().strip()
            except OSError:
                pass
        result[interface.name] = fields
    for name in ('route', 'wireless'):
        try:
            result[name] = Path('/proc/net', name).read_text()[:8192]
        except OSError:
            pass
    return result


class HostMonitor:
    def __init__(self, recorder, internet_probe_url=''):
        self.recorder, self.url = recorder, internet_probe_url
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, name='carl-network-monitor', daemon=True)
        self.thread.start()

    def _run(self):
        last_probe = -float('inf')
        while not self.done.is_set():
            started = time.monotonic()
            try:
                self.recorder.emit('host_network', interfaces=network_snapshot())
                usage = resource.getrusage(resource.RUSAGE_SELF)
                disk = os.statvfs(Path(self.recorder.directory).parent)
                self.recorder.emit('host_resources', load_average=os.getloadavg(),
                                   process_cpu_user_s=usage.ru_utime, process_cpu_system_s=usage.ru_stime,
                                   process_max_rss_kib=usage.ru_maxrss,
                                   disk_available_bytes=disk.f_bavail * disk.f_frsize,
                                   memory_info=Path('/proc/meminfo').read_text()[:4096])
                if self.url and started - last_probe >= 5:
                    last_probe = started
                    try:
                        # This observes only the server's path to this target. No command dependency.
                        with urllib.request.urlopen(self.url, timeout=1) as response:
                            self.recorder.emit('internet_probe', ok=True, status=response.status,
                                               elapsed_ms=(time.monotonic() - started) * 1000)
                    except Exception as error:
                        self.recorder.emit('internet_probe', ok=False, error_type=type(error).__name__,
                                           detail=str(error), elapsed_ms=(time.monotonic() - started) * 1000)
            except Exception as error:
                self.recorder.emit('host_monitor_error', detail=str(error))
            self.done.wait(max(0.1, 1 - (time.monotonic() - started)))

    def close(self):
        self.done.set()
        self.thread.join(timeout=1.5)
