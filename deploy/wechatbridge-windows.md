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

- Default instance data root: `%USERPROFILE%\.local\share\wechatbridge\<instance>\`  
  (`WECHATBRIDGE_INSTANCE` defaults to `default`)
- Default session directory: `%USERPROFILE%\.local\share\wechatbridge\<instance>\sessions`
- State / QR paths also live under that instance root unless overridden by env
- Multi-instance: run separate processes with different `WECHATBRIDGE_INSTANCE` values
- Subprocess env sets `HOME` and `USERPROFILE` to the **per-user session dir** (agy and grok)
- `os.chmod` is a no-op on Windows; token file permissions rely on NTFS ACLs
- `os.setsid` (Unix process group) is not used on Windows; CLI subprocesses inherit the parent's process group
