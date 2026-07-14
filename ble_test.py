"""列出所有附近的 BLE 廣播, 不限 SwitchBot"""
import asyncio
from bleak import BleakScanner


async def main():
    print("掃描 15 秒...")
    devs = await BleakScanner.discover(timeout=15, return_adv=True)
    if not devs:
        print("完全沒有 BLE 設備, 藍牙權限或驅動問題")
        return
    print(f"共 {len(devs)} 個設備:\n")
    for addr, (dev, adv) in devs.items():
        name = dev.name or adv.local_name or "(無名稱)"
        print(f"  {addr}  RSSI={adv.rssi:>4}  {name}")


asyncio.run(main())
