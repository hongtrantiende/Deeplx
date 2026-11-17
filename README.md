# instabox-watchdog

Watchdog giữ máy InstaBox "novela" sống lại sau khi chạm trần runtime của gói free,
chạy trên GitHub Actions cron mỗi 5 phút.

Cấu trúc: daemon `instabox-keepawake` trong máy giữ 4 tín hiệu hoạt động (không bị
pause vì idle). Workflow này ngoài máy chỉ làm một việc: thấy máy đang pause vì
chạm trần (hoặc cooldown hết) thì resume lại.

File: `instabox-watchdog.py` (từ instabox-keepawake-kit, linux tier).
Secrets: `INSTABOX_MACHINE_ID`, `INSTABOX_API_KEY` (scope machines:read machines:write).

Lưu ý: GitHub tự tắt schedule sau 60 ngày repo không có hoạt động — mở tab Actions
bấm re-enable khi nhận email cảnh báo.
