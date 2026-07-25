# Windows Deployment

## Quick Start

```cmd
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -r requirements.txt
copy deploy\wechatbridge.env.example .env
python -m wechatbridge
```

## Autostart via Task Scheduler

1. Open Task Scheduler -> Create Task
2. Action: Start a program -> `pythonw.exe` with arguments `-m wechatbridge`
3. Set Working Directory to the repo root
4. Trigger: At log on
5. Settings: Restart on failure, every 1 minute, up to 3 times

## Notes

- Default session directory: `%USERPROFILE%\.local\share\wechatbridge\sessions`
- `HOME` and `USERPROFILE` are both set to the session dir for agy subprocess
- `os.chmod` is a no-op on Windows; token file permissions rely on NTFS ACLs
- `os.setsid` (Unix process group) is not used on Windows; agy subprocess inherits the parent's process group
