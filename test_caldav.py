"""
test_caldav.py — 測試 Synology CalDAV 連線
Usage: python test_caldav.py
"""

import caldav
import getpass

CALDAV_URL = "http://100.70.14.11:5000/caldav/bill86854238/"

password = getpass.getpass("Synology 密碼: ")

client = caldav.DAVClient(
    url=CALDAV_URL,
    username="bill86854238",
    password=password,
)

print(f"連線到 {CALDAV_URL} ...")

principal = client.principal()
calendars = principal.calendars()

print(f"\n找到 {len(calendars)} 個行事曆：")
for cal in calendars:
    print(f"  - {cal.name}  ({cal.url})")
