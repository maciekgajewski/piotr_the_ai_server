from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from ai_server.domain_agents.system_status.store import SystemStatusStore
from ai_server.home_assistant.interfaces import HomeAssistantInventory


@dataclass(frozen=True)
class SystemStatusOptions:
    collection_interval_seconds: float = 30.0
    baseline_alpha: float = 0.01
    max_short_report_issues: int = 3
    disk_paths: tuple[str, ...] = ("/",)
    home_assistant_entities: tuple[str, ...] = ()
    disk_free_warning_percent: float = 15.0
    disk_free_critical_percent: float = 5.0
    inode_free_warning_percent: float = 10.0
    inode_free_critical_percent: float = 3.0
    memory_available_warning_percent: float = 10.0
    memory_available_critical_percent: float = 5.0
    swap_used_warning_percent: float = 50.0
    swap_used_critical_percent: float = 80.0
    load_per_cpu_warning: float = 1.0
    load_per_cpu_critical: float = 2.0
    temperature_warning_c: float = 75.0
    temperature_critical_c: float = 85.0
    stale_snapshot_seconds: float = 120.0
    ha_entity_stale_seconds: float = 300.0
    baseline_min_samples: int = 10
    baseline_deviation_ratio: float = 1.0


class HomeAssistantStateProvider(Protocol):
    @property
    def inventory(self) -> HomeAssistantInventory | None:
        raise NotImplementedError

    def cached_state(self, entity_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


class SystemStatusCollector:
    def __init__(
        self,
        *,
        store: SystemStatusStore,
        options: SystemStatusOptions = SystemStatusOptions(),
        home_assistant: HomeAssistantStateProvider | None = None,
        now_factory: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._options = options
        self._home_assistant = home_assistant
        self._now_factory = now_factory
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._logger = logging.getLogger(f"{__name__}.SystemStatusCollector")

    async def start(self) -> None:
        self._closed = False
        if self._task is None or self._task.done():
            await self.collect_once()
            self._task = asyncio.create_task(self._collection_loop())

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def latest_snapshot(self) -> dict[str, Any]:
        return self._store.load_snapshot()

    def snapshot_is_stale(self, snapshot: dict[str, Any] | None = None) -> bool:
        snapshot = snapshot if snapshot is not None else self.latest_snapshot()
        collected_at = _number(snapshot.get("collected_at_epoch")) if isinstance(snapshot, dict) else None
        if collected_at is None:
            return True
        return self._now_factory() - collected_at > self._options.stale_snapshot_seconds

    async def collect_once(self) -> dict[str, Any]:
        now = self._now_factory()
        baselines = self._store.load_baselines()
        metrics: dict[str, Any] = {}
        issues: list[dict[str, Any]] = []
        baseline_values: dict[str, tuple[float, str]] = {}

        self._collect_disk(metrics, issues, baseline_values)
        self._collect_memory(metrics, issues, baseline_values)
        self._collect_load(metrics, issues, baseline_values)
        self._collect_temperature(metrics, issues, baseline_values)
        self._collect_uptime(metrics)
        self._collect_logs(metrics, issues)
        self._collect_home_assistant(metrics, issues)
        self._apply_baselines(baselines, baseline_values, issues, now)

        health_status = _overall_status(issues)
        snapshot = {
            "status": health_status,
            "collected_at_epoch": now,
            "collected_at": datetime.fromtimestamp(now).astimezone().isoformat(),
            "metrics": metrics,
            "issues": sorted(issues, key=_issue_sort_key),
        }
        self._store.store_baselines(baselines)
        self._store.store_snapshot(snapshot)
        return snapshot

    async def _collection_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._options.collection_interval_seconds)
            try:
                await self.collect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("system status collection failed")

    def _collect_disk(
        self,
        metrics: dict[str, Any],
        issues: list[dict[str, Any]],
        baseline_values: dict[str, tuple[float, str]],
    ) -> None:
        disks = []
        for path in self._options.disk_paths:
            try:
                usage = shutil.disk_usage(path)
                stat = os.statvfs(path)
            except OSError as exc:
                issues.append(_issue("warning", "disk_unavailable", f"Nie mogę odczytać dysku {path}.", {"path": path, "error": str(exc)}))
                continue
            free_percent = usage.free / usage.total * 100 if usage.total else 0.0
            inode_total = stat.f_files
            inode_free_percent = stat.f_ffree / inode_total * 100 if inode_total else 100.0
            disk = {"path": path, "free_percent": free_percent, "inode_free_percent": inode_free_percent}
            disks.append(disk)
            safe_path = _metric_safe_path(path)
            baseline_values[f"disk.{safe_path}.free_percent"] = (free_percent, "lower_bad")
            self._threshold_issue(
                issues,
                value=free_percent,
                warning=self._options.disk_free_warning_percent,
                critical=self._options.disk_free_critical_percent,
                lower_is_bad=True,
                code="disk_space_low",
                message=f"Kończy się miejsce na dysku {path}.",
                details=disk,
            )
            self._threshold_issue(
                issues,
                value=inode_free_percent,
                warning=self._options.inode_free_warning_percent,
                critical=self._options.inode_free_critical_percent,
                lower_is_bad=True,
                code="disk_inodes_low",
                message=f"Kończą się wolne inody na dysku {path}.",
                details=disk,
            )
        metrics["disks"] = disks

    def _collect_memory(
        self,
        metrics: dict[str, Any],
        issues: list[dict[str, Any]],
        baseline_values: dict[str, tuple[float, str]],
    ) -> None:
        meminfo = _read_meminfo()
        if not meminfo:
            metrics["memory"] = {"status": "unknown"}
            return
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable")
        swap_total = meminfo.get("SwapTotal", 0)
        swap_free = meminfo.get("SwapFree", 0)
        memory = {
            "total_kb": total,
            "available_kb": available,
            "swap_total_kb": swap_total,
            "swap_free_kb": swap_free,
        }
        if total and available is not None:
            available_percent = available / total * 100
            memory["available_percent"] = available_percent
            baseline_values["memory.available_percent"] = (available_percent, "lower_bad")
            self._threshold_issue(
                issues,
                value=available_percent,
                warning=self._options.memory_available_warning_percent,
                critical=self._options.memory_available_critical_percent,
                lower_is_bad=True,
                code="memory_low",
                message="Kończy się wolna pamięć RAM.",
                details=memory,
            )
        if swap_total:
            swap_used_percent = (swap_total - swap_free) / swap_total * 100
            memory["swap_used_percent"] = swap_used_percent
            baseline_values["memory.swap_used_percent"] = (swap_used_percent, "higher_bad")
            self._threshold_issue(
                issues,
                value=swap_used_percent,
                warning=self._options.swap_used_warning_percent,
                critical=self._options.swap_used_critical_percent,
                lower_is_bad=False,
                code="swap_high",
                message="System mocno używa swapu.",
                details=memory,
            )
        metrics["memory"] = memory

    def _collect_load(
        self,
        metrics: dict[str, Any],
        issues: list[dict[str, Any]],
        baseline_values: dict[str, tuple[float, str]],
    ) -> None:
        try:
            load_1m, load_5m, load_15m = os.getloadavg()
        except OSError:
            metrics["load"] = {"status": "unknown"}
            return
        cpu_count = os.cpu_count() or 1
        per_cpu = load_1m / cpu_count
        load = {"load_1m": load_1m, "load_5m": load_5m, "load_15m": load_15m, "cpu_count": cpu_count, "load_1m_per_cpu": per_cpu}
        metrics["load"] = load
        baseline_values["load.load_1m_per_cpu"] = (per_cpu, "higher_bad")
        self._threshold_issue(
            issues,
            value=per_cpu,
            warning=self._options.load_per_cpu_warning,
            critical=self._options.load_per_cpu_critical,
            lower_is_bad=False,
            code="load_high",
            message="Obciążenie procesora jest wysokie.",
            details=load,
        )

    def _collect_temperature(
        self,
        metrics: dict[str, Any],
        issues: list[dict[str, Any]],
        baseline_values: dict[str, tuple[float, str]],
    ) -> None:
        temperatures = []
        for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
            try:
                raw_value = path.read_text(encoding="utf-8").strip()
                value_c = float(raw_value) / 1000.0
            except (OSError, ValueError):
                continue
            zone = path.parent.name
            temperatures.append({"zone": zone, "temperature_c": value_c})
        metrics["temperatures"] = temperatures
        if not temperatures:
            return
        max_temperature = max(item["temperature_c"] for item in temperatures)
        baseline_values["temperature.max_c"] = (max_temperature, "higher_bad")
        self._threshold_issue(
            issues,
            value=max_temperature,
            warning=self._options.temperature_warning_c,
            critical=self._options.temperature_critical_c,
            lower_is_bad=False,
            code="temperature_high",
            message="Temperatura systemu jest wysoka.",
            details={"max_temperature_c": max_temperature, "zones": temperatures},
        )

    def _collect_uptime(self, metrics: dict[str, Any]) -> None:
        try:
            raw_uptime = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
            metrics["uptime"] = {"seconds": float(raw_uptime)}
        except (OSError, ValueError, IndexError):
            metrics["uptime"] = {"status": "unknown"}

    def _collect_logs(self, metrics: dict[str, Any], issues: list[dict[str, Any]]) -> None:
        patterns = re.compile(r"(I/O error|EXT4-fs error|Out of memory|oom-killer|segfault)", re.IGNORECASE)
        matches = []
        readable_files = []
        for path in (Path("/var/log/syslog"), Path("/var/log/kern.log")):
            try:
                with path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    stream.seek(max(0, size - 200_000))
                    text = stream.read().decode("utf-8", errors="ignore")
            except OSError:
                continue
            readable_files.append(str(path))
            matches.extend(match.group(0) for match in patterns.finditer(text))
        metrics["logs"] = {"checked_files": readable_files, "matched_error_count": len(matches)}
        if matches:
            issues.append(
                _issue(
                    "warning",
                    "recent_system_log_errors",
                    "W dostępnych logach systemowych są niedawne błędy.",
                    {"matches": matches[-10:], "checked_files": readable_files},
                )
            )

    def _collect_home_assistant(self, metrics: dict[str, Any], issues: list[dict[str, Any]]) -> None:
        entities = []
        if not self._options.home_assistant_entities:
            metrics["home_assistant"] = {"configured_entities": []}
            return
        if self._home_assistant is None:
            metrics["home_assistant"] = {"status": "not_configured", "configured_entities": list(self._options.home_assistant_entities)}
            issues.append(_issue("warning", "ha_not_configured", "Źródła zdrowia z Home Assistant są skonfigurowane, ale HA nie jest podłączony.", {}))
            return
        for entity_id in self._options.home_assistant_entities:
            state = self._home_assistant.cached_state(entity_id)
            if state is None:
                entities.append({"entity_id": entity_id, "status": "missing"})
                issues.append(_issue("warning", "ha_entity_missing", f"Brakuje encji Home Assistant {entity_id}.", {"entity_id": entity_id}))
                continue
            entity = {
                "entity_id": entity_id,
                "state": state.get("state"),
                "attributes": state.get("attributes") if isinstance(state.get("attributes"), dict) else {},
                "last_updated": state.get("last_updated"),
            }
            entities.append(entity)
            if state.get("state") in {"unavailable", "unknown"}:
                issues.append(_issue("warning", "ha_entity_unavailable", f"Encja Home Assistant {entity_id} jest niedostępna.", entity))
            last_updated_epoch = _parse_datetime_epoch(state.get("last_updated"))
            if last_updated_epoch is not None and self._now_factory() - last_updated_epoch > self._options.ha_entity_stale_seconds:
                issues.append(_issue("warning", "ha_entity_stale", f"Encja Home Assistant {entity_id} ma stare dane.", entity))
        metrics["home_assistant"] = {"configured_entities": list(self._options.home_assistant_entities), "entities": entities}

    def _apply_baselines(
        self,
        baselines: dict[str, Any],
        values: dict[str, tuple[float, str]],
        issues: list[dict[str, Any]],
        now: float,
    ) -> None:
        for metric_id, (value, direction) in values.items():
            raw_record = baselines.get(metric_id, {})
            record = raw_record if isinstance(raw_record, dict) else {}
            previous_ema = _number(record.get("ema"))
            samples = int(record.get("samples")) if isinstance(record.get("samples"), int) else 0
            if previous_ema is not None and samples >= self._options.baseline_min_samples:
                ratio = _deviation_ratio(value, previous_ema, direction)
                if ratio >= self._options.baseline_deviation_ratio:
                    issues.append(
                        _issue(
                            "warning",
                            "baseline_deviation",
                            "Metryka odbiega od długiego trendu.",
                            {"metric": metric_id, "value": value, "ema": previous_ema, "direction": direction, "ratio": ratio},
                        )
                    )
            ema = value if previous_ema is None else self._options.baseline_alpha * value + (1.0 - self._options.baseline_alpha) * previous_ema
            baselines[metric_id] = {"ema": ema, "samples": samples + 1, "last_updated_epoch": now, "direction": direction}

    def _threshold_issue(
        self,
        issues: list[dict[str, Any]],
        *,
        value: float,
        warning: float,
        critical: float,
        lower_is_bad: bool,
        code: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        if lower_is_bad:
            severity = "critical" if value <= critical else "warning" if value <= warning else None
        else:
            severity = "critical" if value >= critical else "warning" if value >= warning else None
        if severity is not None:
            issues.append(_issue(severity, code, message, details))


def _read_meminfo() -> dict[str, int]:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        name, _, value = line.partition(":")
        parts = value.strip().split()
        if not parts:
            continue
        try:
            values[name] = int(parts[0])
        except ValueError:
            continue
    return values


def _issue(severity: str, code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "details": details}


def _overall_status(issues: list[dict[str, Any]]) -> str:
    if any(issue.get("severity") == "critical" for issue in issues):
        return "critical"
    if issues:
        return "warning"
    return "ok"


def _issue_sort_key(issue: dict[str, Any]) -> tuple[int, str]:
    severity_order = {"critical": 0, "warning": 1}
    return (severity_order.get(str(issue.get("severity")), 2), str(issue.get("code", "")))


def _metric_safe_path(path: str) -> str:
    normalized = path.strip("/") or "root"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _deviation_ratio(value: float, ema: float, direction: str) -> float:
    denominator = max(abs(ema), 0.000001)
    if direction == "lower_bad" and value < ema:
        return (ema - value) / denominator
    if direction == "higher_bad" and value > ema:
        return (value - ema) / denominator
    return 0.0


def _parse_datetime_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return None
