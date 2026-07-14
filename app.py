"""
SwitchBot 排程控制 (GUI 版) - 多裝置版
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Callable, Iterable

import schedule
from bleak import BleakScanner
from switchbot import GetSwitchbotDevices, Switchbot

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

logging.disable(logging.CRITICAL)

BLE_START_TIMEOUT = 8
BLE_STOP_TIMEOUT = 8
BLE_DISCOVER_TIMEOUT = 14
BLE_COMMAND_TIMEOUT = 18
BLE_CONTROL_TIMEOUT = 75

APP_NAME = "SwitchBotScheduler"
WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
ACTION_NAMES = {"on": "開啟", "off": "關閉", "press": "按壓"}


def get_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) / APP_NAME if appdata else Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Device:
    mac: str
    name: str = ""


@dataclass
class ScheduleItem:
    id: int
    time: str
    action: str
    days: list[int]
    device_mac: str = ""
    enabled: bool = True


@dataclass
class AppConfig:
    devices: list[Device] = field(default_factory=list)
    schedules: list[ScheduleItem] = field(default_factory=list)
    autostart: bool = False

    @classmethod
    def load(cls) -> "AppConfig":
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
            if "devices" in data:
                devices = [Device(**d) for d in data.get("devices", [])]
            else:
                # 舊格式 (單一裝置) 遷移
                devices = []
                old_mac = data.get("device_mac", "")
                if old_mac:
                    devices.append(Device(
                        mac=old_mac,
                        name=data.get("device_name", "") or "WoHand",
                    ))
            schedules = []
            default_mac = devices[0].mac if devices else ""
            for s in data.get("schedules", []):
                if "device_mac" not in s:
                    s = {**s, "device_mac": default_mac}
                schedules.append(ScheduleItem(**s))
            return cls(
                devices=devices,
                schedules=schedules,
                autostart=data.get("autostart", False),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        data = {
            "devices": [asdict(d) for d in self.devices],
            "schedules": [asdict(s) for s in self.schedules],
            "autostart": self.autostart,
        }
        CONFIG_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def find_device(self, mac: str) -> Device | None:
        return next((d for d in self.devices if d.mac == mac), None)


class BleWorker:
    def __init__(self, log_cb: Callable[[str], None]) -> None:
        self._log = log_cb
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._watch_macs: set[str] = set()
        self._cache: dict = {}
        self._scanner = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def submit_scan(self, cb: Callable[[list[dict]], None]) -> None:
        self._queue.put(("scan", cb))

    def submit_control(self, mac: str, action: str,
                       cb: Callable[[bool, str], None]) -> None:
        self._queue.put(("control", mac, action, cb))

    def watch_all(self, macs: Iterable[str]) -> None:
        self._queue.put(("watch", list(macs)))

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main(loop))
        finally:
            loop.close()

    async def _main(self, loop: asyncio.AbstractEventLoop) -> None:
        logging.getLogger("switchbot").setLevel(logging.ERROR)
        check_interval = 60
        stale_threshold = 180
        last_check = time.time()

        while not self._stop.is_set():
            try:
                job = await loop.run_in_executor(
                    None, lambda: self._queue.get(timeout=check_interval)
                )
            except queue.Empty:
                job = "__tick__"

            if job == "__tick__":
                now = time.time()
                if now - last_check >= check_interval:
                    try:
                        await self._health_check(stale_threshold)
                    except Exception as e:
                        self._log(f"健康檢查錯誤: {e}")
                    last_check = now
                continue

            if job is None:
                break
            if job[0] == "scan":
                _, cb = job
                try:
                    cb(await self._scan())
                except Exception as e:
                    self._log(f"掃描錯誤: {e}")
                    cb([])
            elif job[0] == "control":
                _, mac, action, cb = job
                try:
                    ok, msg = await asyncio.wait_for(
                        self._control(mac, action),
                        timeout=BLE_CONTROL_TIMEOUT,
                    )
                    cb(ok, msg)
                except asyncio.TimeoutError:
                    self._cache.pop(mac.upper(), None)
                    self._log("控制逾時, 已重置藍牙掃描狀態")
                    await self._stop_bg_scanner()
                    await self._start_bg_scanner()
                    cb(False, "控制逾時, 請稍後再試")
                except Exception as e:
                    self._log(f"控制錯誤: {e}")
                    cb(False, str(e))
            elif job[0] == "watch":
                _, macs = job
                try:
                    await self._set_watch_all(macs)
                except Exception as e:
                    self._log(f"背景掃描錯誤: {e}")
            last_check = time.time()
        await self._stop_bg_scanner()

    async def _health_check(self, stale_threshold: float) -> None:
        if not self._watch_macs:
            return
        if self._scanner is None:
            self._log("健康檢查: 背景掃描未啟動, 重新啟動")
            await self._start_bg_scanner()
            return
        # 只要任何一台監聽中的裝置近期有廣播, 就視為 scanner 健康
        # (個別裝置長時間無廣播可能是裝置本身關閉, 不該因此重啟 scanner)
        now = time.time()
        newest_age: float | None = None
        for m in self._watch_macs:
            cached = self._cache.get(m)
            if cached:
                age = now - cached["time"]
                if newest_age is None or age < newest_age:
                    newest_age = age
        if newest_age is None or newest_age > stale_threshold:
            msg = (f"{newest_age:.0f}s 未收到任何廣播"
                   if newest_age is not None else "尚未收到廣播")
            self._log(f"健康檢查: {msg}, 重啟背景掃描")
            await self._stop_bg_scanner()
            await asyncio.sleep(1.0)
            await self._start_bg_scanner()

    async def _set_watch_all(self, macs: list[str]) -> None:
        new_macs = {m.upper() for m in macs if m}
        if self._watch_macs == new_macs:
            return
        await self._stop_bg_scanner()
        for m in list(self._cache.keys()):
            if m not in new_macs:
                self._cache.pop(m, None)
        self._watch_macs = new_macs
        if new_macs:
            await self._start_bg_scanner()

    async def _start_bg_scanner(self) -> None:
        if self._scanner is not None or not self._watch_macs:
            return

        def _cb(device, adv):
            addr = device.address.upper()
            if addr in self._watch_macs:
                self._cache[addr] = {
                    "device": device,
                    "rssi": adv.rssi,
                    "time": time.time(),
                }

        try:
            self._scanner = BleakScanner(detection_callback=_cb)
            await asyncio.wait_for(
                self._scanner.start(),
                timeout=BLE_START_TIMEOUT,
            )
            self._log(f"背景監聽啟動 ({len(self._watch_macs)} 個裝置)")
        except asyncio.TimeoutError:
            self._scanner = None
            self._log("背景監聽啟動逾時")
        except Exception as e:
            self._scanner = None
            self._log(f"背景監聽啟動失敗: {e}")

    async def _stop_bg_scanner(self) -> None:
        if self._scanner is None:
            return
        scanner = self._scanner
        self._scanner = None
        try:
            await asyncio.wait_for(scanner.stop(), timeout=BLE_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            self._log("背景監聽停止逾時, 將重建掃描器")
        except Exception:
            pass

    async def _scan(self) -> list[dict]:
        self._log("掃描設備中 (10 秒)...")
        await self._stop_bg_scanner()
        try:
            devs = await asyncio.wait_for(
                GetSwitchbotDevices().discover(scan_timeout=10),
                timeout=BLE_DISCOVER_TIMEOUT,
            )
            out = []
            for mac, adv in devs.items():
                out.append({
                    "mac": mac,
                    "model": adv.data.get("modelName", ""),
                    "rssi": adv.data.get("rssi"),
                })
            self._log(f"找到 {len(out)} 個 SwitchBot 設備")
            return out
        finally:
            await self._start_bg_scanner()

    async def _get_fresh_device(self, mac: str, use_cache: bool) -> object | None:
        if use_cache:
            cached = self._cache.get(mac)
            if (cached and time.time() - cached["time"] < 5
                    and (cached["rssi"] or -127) > -100):
                self._log(f"  命中快取 (RSSI={cached['rssi']})")
                return cached["device"]
            if self._scanner is not None:
                self._log("  等待 Bot 廣播...")
                deadline = time.time() + 10
                while time.time() < deadline:
                    await asyncio.sleep(0.5)
                    cached = self._cache.get(mac)
                    if (cached and time.time() - cached["time"] < 3
                            and (cached["rssi"] or -127) > -100):
                        self._log(f"  收到廣播 (RSSI={cached['rssi']})")
                        return cached["device"]
        await self._stop_bg_scanner()
        self._log("  主動掃描...")
        for attempt in range(1, 4):
            try:
                devs = await asyncio.wait_for(
                    GetSwitchbotDevices().discover(scan_timeout=8),
                    timeout=BLE_DISCOVER_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._log("  掃描逾時, 重試...")
                continue
            hit = devs.get(mac)
            if hit is not None:
                rssi = hit.data.get("rssi", -127) if hit.data else -127
                if (rssi or -127) > -100:
                    self._log(f"  掃到 (RSSI={rssi})")
                    return hit.device
            await asyncio.sleep(1.5)
        return None

    async def _control(self, mac: str, action: str) -> tuple[bool, str]:
        mac = mac.upper()
        last_err = None
        try:
            for outer in range(1, 4):
                device = await self._get_fresh_device(mac, use_cache=(outer == 1))
                if device is None:
                    last_err = "找不到設備"
                    self._log(f"  第 {outer}/3 輪: 找不到設備")
                    await asyncio.sleep(2)
                    continue

                await self._stop_bg_scanner()
                await asyncio.sleep(0.3)

                try:
                    bot = Switchbot(device=device, retry_count=2)
                    func = {"on": bot.turn_on, "off": bot.turn_off,
                            "press": bot.press}[action]
                    ok = await asyncio.wait_for(
                        func(),
                        timeout=BLE_COMMAND_TIMEOUT,
                    )
                    if ok:
                        return True, f"{ACTION_NAMES[action]}成功"
                    last_err = "指令未成功"
                except asyncio.TimeoutError:
                    last_err = "Timeout"
                    self._cache.pop(mac, None)
                except Exception as e:
                    last_err = f"{type(e).__name__}"
                    self._cache.pop(mac, None)
                self._log(f"  第 {outer}/3 輪連線失敗 ({last_err}), 等 3 秒重試...")
                await asyncio.sleep(3)

            return False, (
                f"連線失敗 ({last_err}). Bot 可能在深度睡眠, "
                "請按一下 Bot 按鈕喚醒, 並確認手機 App 完全關閉或飛航模式."
            )
        finally:
            await self._start_bg_scanner()


class SchedulerThread:
    def __init__(self, get_cfg: Callable[[], AppConfig],
                 submit: Callable[[str, str, Callable], None],
                 log_cb: Callable[[str], None]) -> None:
        self._get_cfg = get_cfg
        self._submit = submit
        self._log = log_cb
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def refresh(self) -> None:
        self._dirty.set()

    def _run(self) -> None:
        self._rebuild()
        last_tick = time.time()
        last_tick_dt = datetime.now()
        while not self._stop.is_set():
            try:
                now = time.time()
                now_dt = datetime.now()
                if now - last_tick > 30:
                    self._log(
                        f"偵測到時間跳躍 {now - last_tick:.0f}s "
                        f"(可能剛從睡眠恢復), 重建排程"
                    )
                    self._run_missed_between(last_tick_dt, now_dt)
                    self._rebuild()
                last_tick = now
                last_tick_dt = now_dt

                if self._dirty.is_set():
                    self._rebuild()
                    self._dirty.clear()
                schedule.run_pending()
            except Exception as e:
                self._log(f"排程迴圈錯誤: {type(e).__name__}: {e}")
            time.sleep(1)

    def _device_label(self, cfg: AppConfig, mac: str) -> str:
        d = cfg.find_device(mac)
        return d.name if (d and d.name) else (mac or "?")

    def _rebuild(self) -> None:
        schedule.clear()
        cfg = self._get_cfg()
        if not cfg.devices:
            self._log("排程: 尚未新增設備, 暫停執行")
            return
        active = [s for s in cfg.schedules if s.enabled]
        if not active:
            self._log("排程: 無啟用中項目")
            return
        loaded = 0
        for s in active:
            if not cfg.find_device(s.device_mac):
                self._log(f"  跳過 {s.time}: 對應設備已不存在 ({s.device_mac})")
                continue
            schedule.every().day.at(s.time).do(
                self._trigger_if_day,
                s.device_mac, s.action, tuple(s.days), s.time,
            )
            loaded += 1
        self._log(f"排程已載入 {loaded} 筆, 下次觸發:")
        short_days = ["一", "二", "三", "四", "五", "六", "日"]
        for j in schedule.get_jobs():
            if j.next_run:
                w = short_days[j.next_run.weekday()]
                self._log(f"  {j.next_run.strftime('%m/%d')} (週{w}) "
                          f"{j.next_run.strftime('%H:%M')}")

    def _run_missed_between(self, start: datetime, end: datetime) -> None:
        cfg = self._get_cfg()
        if not cfg.devices or not cfg.schedules:
            return

        window_start = start - timedelta(seconds=2)
        window_end = end
        for s in cfg.schedules:
            if not s.enabled:
                continue
            if not cfg.find_device(s.device_mac):
                continue
            try:
                hh, mm = (int(part) for part in s.time.split(":", 1))
            except ValueError:
                continue

            day = window_start.date()
            while day <= window_end.date():
                run_at = datetime.combine(day, datetime.min.time()).replace(
                    hour=hh, minute=mm
                )
                if window_start < run_at <= window_end and run_at.weekday() in s.days:
                    label = self._device_label(cfg, s.device_mac)
                    self._log(
                        f">>> 補跑錯過排程 [{label}] {s.time} "
                        f"{ACTION_NAMES[s.action]}"
                    )
                    self._submit(
                        s.device_mac, s.action,
                        lambda ok, msg, lbl=label: self._log(f"    [{lbl}] {msg}")
                    )
                day += timedelta(days=1)

    def _trigger_if_day(self, mac: str, action: str,
                        days: tuple, time_str: str) -> None:
        today = datetime.now().weekday()
        if today not in days:
            return
        cfg = self._get_cfg()
        label = self._device_label(cfg, mac)
        self._log(f">>> 觸發 [{label}] {time_str} {ACTION_NAMES[action]}")
        self._submit(
            mac, action,
            lambda ok, msg, lbl=label: self._log(f"    [{lbl}] {msg}")
        )


def set_autostart(enable: bool) -> None:
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        cmd = f'"{sys.executable}" "{Path(__file__).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                        winreg.KEY_WRITE | winreg.KEY_READ) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


class ScanDialog(tk.Toplevel):
    def __init__(self, parent: "App", existing_macs: set[str]) -> None:
        super().__init__(parent)
        self.title("掃描 SwitchBot 設備")
        self.geometry("440x340")
        self.transient(parent)
        self.grab_set()
        self.selected: dict | None = None
        self._parent = parent
        self._existing = {m.upper() for m in existing_macs}

        ttk.Label(self,
                  text="提示: 先把手機 SwitchBot App 完全關閉,\n"
                       "再按一下 Bot 按鈕喚醒它."
                  ).pack(padx=10, pady=8)
        self.lst = tk.Listbox(self)
        self.lst.pack(fill="both", expand=True, padx=10, pady=5)

        frm = ttk.Frame(self)
        frm.pack(fill="x", padx=10, pady=8)
        self.btn_scan = ttk.Button(frm, text="重新掃描", command=self._scan)
        self.btn_scan.pack(side="left")
        ttk.Button(frm, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(frm, text="選擇", command=self._pick).pack(side="right")
        self._devices: list[dict] = []
        self.after(100, self._scan)

    def _scan(self) -> None:
        self.lst.delete(0, "end")
        self.lst.insert("end", "掃描中, 請稍候...")
        self.btn_scan.config(state="disabled")
        self._parent.ble.submit_scan(self._on_result)

    def _on_result(self, devices: list[dict]) -> None:
        try:
            if self.winfo_exists():
                self.after(0, lambda: self._display(devices))
        except tk.TclError:
            pass

    def _display(self, devices: list[dict]) -> None:
        try:
            if not self.winfo_exists():
                return
            self.btn_scan.config(state="normal")
            self.lst.delete(0, "end")
            self._devices = devices
            if not devices:
                self.lst.insert("end", "沒有找到任何 SwitchBot 設備")
                return
            for d in devices:
                model = d["model"] or "(未知型號)"
                marker = " (已加入)" if d["mac"].upper() in self._existing else ""
                self.lst.insert(
                    "end",
                    f"{model}  {d['mac']}  RSSI={d.get('rssi')}{marker}"
                )
        except tk.TclError:
            pass

    def _pick(self) -> None:
        sel = self.lst.curselection()
        if not sel or not self._devices:
            return
        d = self._devices[sel[0]]
        if d["mac"].upper() in self._existing:
            messagebox.showinfo("已存在", "此設備已加入清單.")
            return
        self.selected = d
        self.destroy()


class ScheduleDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, devices: list[Device],
                 item: ScheduleItem | None = None) -> None:
        super().__init__(parent)
        self.title("排程設定")
        self.geometry("420x340")
        self.transient(parent)
        self.grab_set()
        self.result: ScheduleItem | None = None
        self._devices = devices

        d = item or ScheduleItem(
            id=0, time="08:00", action="on",
            days=[0, 1, 2, 3, 4],
            device_mac=devices[0].mac if devices else "",
            enabled=True,
        )

        frm_t = ttk.Frame(self)
        frm_t.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(frm_t, text="時間:").pack(side="left")
        hh, mm = d.time.split(":")
        self.var_hh = tk.StringVar(value=hh)
        self.var_mm = tk.StringVar(value=mm)
        ttk.Spinbox(frm_t, from_=0, to=23, width=4, textvariable=self.var_hh,
                    format="%02.0f").pack(side="left", padx=4)
        ttk.Label(frm_t, text=":").pack(side="left")
        ttk.Spinbox(frm_t, from_=0, to=59, width=4, textvariable=self.var_mm,
                    format="%02.0f").pack(side="left", padx=4)

        frm_dev = ttk.Frame(self)
        frm_dev.pack(fill="x", padx=12, pady=4)
        ttk.Label(frm_dev, text="設備:").pack(side="left")
        labels = [f"{dev.name or 'WoHand'}  {dev.mac}" for dev in devices]
        self.cmb_dev = ttk.Combobox(
            frm_dev, values=labels, state="readonly", width=38
        )
        self.cmb_dev.pack(side="left", padx=6)
        if labels:
            idx = next(
                (i for i, dev in enumerate(devices) if dev.mac == d.device_mac),
                0,
            )
            self.cmb_dev.current(idx)

        frm_a = ttk.LabelFrame(self, text="動作")
        frm_a.pack(fill="x", padx=12, pady=4)
        self.var_action = tk.StringVar(value=d.action)
        for v, label in [("on", "開啟"), ("off", "關閉"), ("press", "按壓")]:
            ttk.Radiobutton(frm_a, text=label, variable=self.var_action,
                            value=v).pack(side="left", padx=8, pady=4)

        frm_d = ttk.LabelFrame(self, text="星期 (至少選一個)")
        frm_d.pack(fill="x", padx=12, pady=4)
        self.vars_days: list[tk.BooleanVar] = []
        for i, name in enumerate(WEEKDAY_NAMES):
            v = tk.BooleanVar(value=(i in d.days))
            self.vars_days.append(v)
            ttk.Checkbutton(frm_d, text=name, variable=v).pack(
                side="left", padx=2, pady=4
            )

        self.var_enabled = tk.BooleanVar(value=d.enabled)
        ttk.Checkbutton(self, text="啟用此排程",
                        variable=self.var_enabled).pack(anchor="w", padx=16, pady=4)

        frm_btn = ttk.Frame(self)
        frm_btn.pack(fill="x", padx=12, pady=12)
        ttk.Button(frm_btn, text="確定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(frm_btn, text="取消", command=self.destroy).pack(side="right")

    def _ok(self) -> None:
        if not self._devices:
            messagebox.showerror("錯誤", "請先新增至少一個設備")
            return
        try:
            h = int(self.var_hh.get())
            m = int(self.var_mm.get())
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            messagebox.showerror("錯誤", "時間格式錯誤 (HH 0-23, MM 0-59)")
            return
        days = [i for i, v in enumerate(self.vars_days) if v.get()]
        if not days:
            messagebox.showerror("錯誤", "請至少選擇一個星期")
            return
        idx = self.cmb_dev.current()
        if idx < 0:
            messagebox.showerror("錯誤", "請選擇目標設備")
            return
        self.result = ScheduleItem(
            id=0,
            time=f"{h:02d}:{m:02d}",
            action=self.var_action.get(),
            days=days,
            device_mac=self._devices[idx].mac,
            enabled=self.var_enabled.get(),
        )
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SwitchBot 排程控制")
        self.geometry("680x800")
        self.minsize(600, 720)

        self.cfg = AppConfig.load()
        self._next_id = max((s.id for s in self.cfg.schedules), default=0) + 1
        self._log_q: queue.Queue = queue.Queue()
        self._ui_q: queue.Queue = queue.Queue()
        self._tray = None
        self._tray_notified = False

        self.ble = BleWorker(self._log)
        self.ble.start()
        self.scheduler = SchedulerThread(
            get_cfg=lambda: self.cfg,
            submit=self.ble.submit_control,
            log_cb=self._log,
        )
        self.scheduler.start()

        self._build_ui()
        self._refresh_devices()
        self._refresh_list()
        self._drain()

        if TRAY_AVAILABLE:
            self._start_tray()
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.ble.watch_all([d.mac for d in self.cfg.devices])
        if not self.cfg.devices:
            self.after(400, self._add_device)

    def _build_ui(self) -> None:
        # 設備清單
        frm_d = ttk.LabelFrame(self, text="設備")
        frm_d.pack(fill="x", padx=10, pady=6)
        cols_d = ("name", "mac")
        self.tree_dev = ttk.Treeview(
            frm_d, columns=cols_d, show="headings", height=4,
            selectmode="browse",
        )
        for c, t, w, a in [
            ("name", "名稱", 240, "w"),
            ("mac", "MAC", 220, "w"),
        ]:
            self.tree_dev.heading(c, text=t)
            self.tree_dev.column(c, width=w, anchor=a)
        self.tree_dev.pack(fill="x", padx=6, pady=(6, 0))

        frm_db = ttk.Frame(frm_d)
        frm_db.pack(fill="x", pady=6)
        for txt, cmd in [
            ("新增 (掃描)", self._add_device),
            ("重新命名", self._rename_device),
            ("刪除", self._delete_device),
        ]:
            ttk.Button(frm_db, text=txt, command=cmd).pack(side="left", padx=4)

        # 手動測試
        frm_m = ttk.LabelFrame(self, text="手動測試 (對選取的設備)")
        frm_m.pack(fill="x", padx=10, pady=6)
        for act, label in [("on", "開啟"), ("off", "關閉"), ("press", "按壓")]:
            ttk.Button(frm_m, text=label,
                       command=lambda a=act: self._manual(a)).pack(
                side="left", padx=6, pady=8
            )

        # 排程
        frm_s = ttk.LabelFrame(self, text="排程")
        frm_s.pack(fill="both", expand=True, padx=10, pady=6)
        cols = ("en", "device", "time", "action", "days")
        self.tree = ttk.Treeview(frm_s, columns=cols, show="headings", height=8)
        for c, t, w, a in [
            ("en", "啟用", 50, "center"),
            ("device", "設備", 140, "w"),
            ("time", "時間", 70, "center"),
            ("action", "動作", 70, "center"),
            ("days", "星期", 240, "w"),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.tree.bind("<Double-1>", lambda e: self._edit())

        frm_b = ttk.Frame(frm_s)
        frm_b.pack(fill="x", pady=6)
        for txt, cmd in [
            ("新增", self._add),
            ("編輯", self._edit),
            ("刪除", self._delete),
            ("啟用/停用", self._toggle),
        ]:
            ttk.Button(frm_b, text=txt, command=cmd).pack(side="left", padx=4)

        # 日誌
        frm_l = ttk.LabelFrame(self, text="日誌")
        frm_l.pack(fill="both", padx=10, pady=6)
        self.txt = tk.Text(frm_l, height=8, state="disabled", wrap="word")
        self.txt.pack(fill="both", expand=True, padx=6, pady=6)

        # 底部
        frm_bottom = ttk.Frame(self)
        frm_bottom.pack(fill="x", padx=10, pady=(0, 10))
        self.var_auto = tk.BooleanVar(value=self.cfg.autostart)
        ttk.Checkbutton(frm_bottom, text="開機自動啟動", variable=self.var_auto,
                        command=self._toggle_autostart).pack(side="left")
        if TRAY_AVAILABLE:
            tip = "  (按 X 會縮到右下角系統匣, 排程持續在背景執行)"
        else:
            tip = "  (視窗關閉後排程停止, 請最小化而非關閉)"
        ttk.Label(frm_bottom, text=tip, foreground="#666").pack(side="left")

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_q.put(f"[{ts}] {msg}")

    def _drain(self) -> None:
        try:
            while True:
                line = self._log_q.get_nowait()
                self.txt.config(state="normal")
                self.txt.insert("end", line + "\n")
                self.txt.see("end")
                lines = int(self.txt.index("end-1c").split(".")[0])
                if lines > 300:
                    self.txt.delete("1.0", f"{lines-300}.0")
                self.txt.config(state="disabled")
        except queue.Empty:
            pass
        # 系統匣選單在另一條執行緒觸發, 透過佇列轉回主執行緒處理
        try:
            while True:
                cmd = self._ui_q.get_nowait()
                if cmd == "show":
                    self._show_window()
                elif cmd == "quit":
                    self._quit()
                    return
        except queue.Empty:
            pass
        self.after(200, self._drain)

    def _refresh_devices(self) -> None:
        prev_sel = self.tree_dev.selection()
        for i in self.tree_dev.get_children():
            self.tree_dev.delete(i)
        for d in self.cfg.devices:
            self.tree_dev.insert(
                "", "end", iid=d.mac,
                values=(d.name or "WoHand", d.mac),
            )
        # 維持原選取或預選第一個
        target = None
        if prev_sel and self.cfg.find_device(prev_sel[0]):
            target = prev_sel[0]
        elif self.cfg.devices:
            target = self.cfg.devices[0].mac
        if target:
            self.tree_dev.selection_set(target)
            self.tree_dev.focus(target)

    def _refresh_list(self) -> None:
        for i in self.tree.get_children():
            self.tree.delete(i)
        for s in sorted(self.cfg.schedules,
                        key=lambda x: (x.device_mac, x.time)):
            days = "每天" if len(s.days) == 7 else "、".join(
                WEEKDAY_NAMES[d] for d in sorted(s.days)
            )
            dev = self.cfg.find_device(s.device_mac)
            dev_label = (dev.name or "WoHand") if dev else f"(已移除) {s.device_mac}"
            self.tree.insert(
                "", "end", iid=str(s.id),
                values=(
                    "V" if s.enabled else "",
                    dev_label,
                    s.time,
                    ACTION_NAMES[s.action],
                    days,
                ),
            )

    def _selected_device(self) -> Device | None:
        sel = self.tree_dev.selection()
        if not sel:
            return None
        return self.cfg.find_device(sel[0])

    @staticmethod
    def _default_name(model: str, mac: str) -> str:
        suffix = mac.replace(":", "")[-4:]
        return f"{model or 'Bot'}-{suffix}"

    def _add_device(self) -> None:
        existing = {d.mac for d in self.cfg.devices}
        dlg = ScanDialog(self, existing)
        self.wait_window(dlg)
        if not dlg.selected:
            return
        info = dlg.selected
        name = self._default_name(info.get("model") or "", info["mac"])
        self.cfg.devices.append(Device(mac=info["mac"], name=name))
        self.cfg.save()
        self._refresh_devices()
        self._refresh_list()
        self.scheduler.refresh()
        self.ble.watch_all([d.mac for d in self.cfg.devices])
        self._log(f"已新增設備: {name} ({info['mac']})")

    def _rename_device(self) -> None:
        d = self._selected_device()
        if not d:
            messagebox.showwarning("無選取", "請先在設備清單中選取一個設備")
            return
        new_name = simpledialog.askstring(
            "重新命名", "請輸入設備名稱:", initialvalue=d.name, parent=self
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name:
            messagebox.showerror("錯誤", "名稱不可為空")
            return
        d.name = new_name
        self.cfg.save()
        self._refresh_devices()
        self._refresh_list()
        self._log(f"已重新命名設備: {new_name} ({d.mac})")

    def _delete_device(self) -> None:
        d = self._selected_device()
        if not d:
            messagebox.showwarning("無選取", "請先在設備清單中選取一個設備")
            return
        affected = [s for s in self.cfg.schedules if s.device_mac == d.mac]
        if affected:
            ok = messagebox.askyesno(
                "確認刪除",
                f"確定刪除設備「{d.name or d.mac}」?\n"
                f"將同時刪除 {len(affected)} 筆相關排程."
            )
        else:
            ok = messagebox.askyesno(
                "確認刪除", f"確定刪除設備「{d.name or d.mac}」?"
            )
        if not ok:
            return
        self.cfg.devices = [x for x in self.cfg.devices if x.mac != d.mac]
        self.cfg.schedules = [s for s in self.cfg.schedules if s.device_mac != d.mac]
        self.cfg.save()
        self._refresh_devices()
        self._refresh_list()
        self.scheduler.refresh()
        self.ble.watch_all([x.mac for x in self.cfg.devices])
        self._log(f"已刪除設備: {d.mac}")

    def _manual(self, action: str) -> None:
        d = self._selected_device()
        if not d:
            messagebox.showwarning("無選取", "請先在設備清單中選取一個設備")
            return
        label = d.name or d.mac
        self._log(f"手動 [{label}] {ACTION_NAMES[action]} ...")
        self.ble.submit_control(
            d.mac, action,
            lambda ok, msg, lbl=label: self._log(f"  [{lbl}] {msg}")
        )

    def _selected(self) -> ScheduleItem | None:
        sel = self.tree.selection()
        if not sel:
            return None
        sid = int(sel[0])
        return next((s for s in self.cfg.schedules if s.id == sid), None)

    def _add(self) -> None:
        if not self.cfg.devices:
            messagebox.showwarning("無設備", "請先新增至少一個設備")
            return
        dlg = ScheduleDialog(self, self.cfg.devices)
        self.wait_window(dlg)
        if dlg.result:
            dlg.result.id = self._next_id
            self._next_id += 1
            self.cfg.schedules.append(dlg.result)
            self.cfg.save()
            self._refresh_list()
            self.scheduler.refresh()

    def _edit(self) -> None:
        s = self._selected()
        if not s:
            return
        if not self.cfg.devices:
            messagebox.showwarning("無設備", "請先新增至少一個設備")
            return
        dlg = ScheduleDialog(self, self.cfg.devices, s)
        self.wait_window(dlg)
        if dlg.result:
            s.time = dlg.result.time
            s.action = dlg.result.action
            s.days = dlg.result.days
            s.enabled = dlg.result.enabled
            s.device_mac = dlg.result.device_mac
            self.cfg.save()
            self._refresh_list()
            self.scheduler.refresh()

    def _delete(self) -> None:
        s = self._selected()
        if not s:
            return
        if not messagebox.askyesno(
            "確認", f"刪除排程: {s.time} {ACTION_NAMES[s.action]} ?"
        ):
            return
        self.cfg.schedules = [x for x in self.cfg.schedules if x.id != s.id]
        self.cfg.save()
        self._refresh_list()
        self.scheduler.refresh()

    def _toggle(self) -> None:
        s = self._selected()
        if not s:
            return
        s.enabled = not s.enabled
        self.cfg.save()
        self._refresh_list()
        self.scheduler.refresh()

    def _toggle_autostart(self) -> None:
        self.cfg.autostart = self.var_auto.get()
        self.cfg.save()
        try:
            set_autostart(self.cfg.autostart)
            self._log(f"開機啟動: {'開啟' if self.cfg.autostart else '關閉'}")
        except Exception as e:
            messagebox.showerror("錯誤", f"設定開機啟動失敗: {e}")
            self.var_auto.set(not self.cfg.autostart)
            self.cfg.autostart = self.var_auto.get()
            self.cfg.save()

    @staticmethod
    def _tray_image() -> "Image.Image":
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(228, 0, 43, 255))
        d.rounded_rectangle((26, 14, 38, 50), radius=6, fill=(255, 255, 255, 255))
        return img

    def _start_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem(
                "開啟主視窗",
                lambda *args: self._ui_q.put("show"),
                default=True,
            ),
            pystray.MenuItem(
                "結束程式",
                lambda *args: self._ui_q.put("quit"),
            ),
        )
        try:
            self._tray = pystray.Icon(
                APP_NAME, self._tray_image(), "SwitchBot 排程控制", menu
            )
            self._tray.run_detached()
        except Exception as e:
            self._tray = None
            self._log(f"系統匣圖示啟動失敗: {e}")

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _close(self) -> None:
        if self._tray is not None:
            # 縮到系統匣背景執行, 排程不中斷
            self.withdraw()
            self._log("視窗已隱藏, 排程持續在背景執行")
            if not self._tray_notified:
                self._tray_notified = True
                try:
                    self._tray.notify(
                        "程式仍在背景執行, 排程不會中斷.\n"
                        "雙擊此圖示可重新開啟視窗; "
                        "右鍵選「結束程式」才會真正關閉.",
                        "SwitchBot 排程控制",
                    )
                except Exception:
                    pass
            return
        # 沒有系統匣支援時, 以確認對話框避免誤觸關閉
        if not messagebox.askyesno(
            "結束程式", "結束後所有排程將停止執行!\n確定要結束嗎?"
        ):
            return
        self._quit()

    def _quit(self) -> None:
        if self._tray is not None:
            tray = self._tray
            self._tray = None
            try:
                tray.stop()
            except Exception:
                pass
        try:
            self.scheduler.stop()
            self.ble.stop()
        finally:
            self.destroy()


if __name__ == "__main__":
    App().mainloop()
