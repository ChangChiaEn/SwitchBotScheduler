"""
SwitchBot 最小可用版
用法:
  python quick.py                         -> 掃描附近設備, 列出 MAC
  python quick.py <MAC> on                -> 開啟
  python quick.py <MAC> off               -> 關閉
  python quick.py <MAC> press             -> 單次按壓 (Press Mode 用)
"""
import asyncio
import logging
import sys

from bleak import BleakScanner
from switchbot import GetSwitchbotDevices, Switchbot

logging.getLogger("switchbot").setLevel(logging.ERROR)


async def scan() -> None:
    devs = await GetSwitchbotDevices().discover(scan_timeout=10)
    if not devs:
        print("沒掃到任何 SwitchBot, 檢查藍牙 / 距離 / Bot 電量")
        return
    print(f"{'MAC':<20} {'Model':<20} RSSI")
    print("-" * 50)
    for mac, adv in devs.items():
        print(f"{mac:<20} {str(adv.data.get('modelName')):<20} {adv.data.get('rssi')}")


async def _find_device(mac: str):
    for attempt in range(1, 4):
        ble = await BleakScanner.find_device_by_address(mac, timeout=20)
        if ble is not None:
            return ble
        print(f"  掃不到, 重試 {attempt}/3 ...")
        await asyncio.sleep(3)
    return None


async def control(mac: str, action: str) -> None:
    mac = mac.upper()
    ble = await _find_device(mac)
    if ble is None:
        print(f"找不到 {mac} (試過 3 次)")
        return
    bot = Switchbot(device=ble, retry_count=3)
    ok = await {"on": bot.turn_on, "off": bot.turn_off, "press": bot.press}[action]()
    print(f"{action} -> {ok}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        asyncio.run(scan())
    elif len(sys.argv) == 3 and sys.argv[2] in ("on", "off", "press"):
        asyncio.run(control(sys.argv[1], sys.argv[2]))
    else:
        print(__doc__)
