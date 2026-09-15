"""Small immutable evidence files and resource boundaries for the F0 pilot."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

GIB = 1024 ** 3


def utcnow():
    return datetime.now(timezone.utc)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def write_json(path, value, *, immutable=False):
    path = Path(path)
    text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    if immutable and path.exists():
        if path.read_text(encoding='utf-8-sig') != text:
            raise ValueError('IMMUTABLE_EVIDENCE_ALREADY_EXISTS')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


def resource_snapshot():
    if os.name == 'nt':
        class Mem(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong) for n in ('total', 'available', 'page_total',
                'page_available', 'virtual_total', 'virtual_available', 'extended')]
        class Proc(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong)] + [
                (n, ctypes.c_size_t) for n in ('peak_rss', 'rss', 'peak_pool', 'pool',
                'peak_nonpaged', 'nonpaged', 'page', 'peak_page')]
        memory, process = Mem(), Proc()
        memory.length, process.cb = ctypes.sizeof(memory), ctypes.sizeof(process)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise OSError('MEMORY_STATUS_UNAVAILABLE')
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(process), process.cb):
            raise OSError('PROCESS_MEMORY_UNAVAILABLE')
        available, rss, peak = memory.available, process.rss, process.peak_rss
    else:
        info = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        available = int(info['MemAvailable'].split()[0]) * 1024
        info = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        rss, peak = (int(info[n].split()[0]) * 1024 for n in ('VmRSS', 'VmHWM'))
    return dict(at=utcnow().isoformat(), pid=os.getpid(), rss_bytes=rss,
                peak_rss_bytes=peak, system_available_bytes=available)


class ResourceBoundary(RuntimeError):
    pass


class Guard:
    def __init__(self, deadline, output, *, snapshot=resource_snapshot):
        self.deadline = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
        if self.deadline.tzinfo is None:
            raise ValueError('DEADLINE_MUST_HAVE_TIMEZONE')
        self.output, self.snapshot = Path(output), snapshot
        self.peak_rss, self.min_available = 0, None

    def check(self, recovery=None):
        reading = self.snapshot()
        self.peak_rss = max(self.peak_rss, reading['peak_rss_bytes'])
        free = reading['system_available_bytes']
        self.min_available = free if self.min_available is None else min(free, self.min_available)
        reason = ('FOUR_HOUR_AUTHORIZATION_EXPIRED' if utcnow() >= self.deadline else
                  'RSS_LIMIT_1_5_GIB' if max(reading['rss_bytes'], self.peak_rss) > 1.5 * GIB else
                  'SYSTEM_AVAILABLE_BELOW_2_GIB' if free < 2 * GIB else None)
        if reason:
            write_json(self.output / 'RESOURCE_STOP.json', dict(reason=reason, reading=reading,
                       recovery=recovery, stopped_other_services=False, auto_retry=False))
            raise ResourceBoundary(reason)
        return reading


def safe_child(root, relative):
    root = Path(root).resolve()
    p = (root / relative).resolve()
    if p == root or not p.is_relative_to(root):
        raise ValueError('PATH_ESCAPES_F0_ROOT')
    return p
