# Chrome DevTools MCP (development only)

`.cursor/mcp.json` starts `chrome-devtools-mcp@1.13.0` for local UI work. It is not part of the Wardmuse runtime, the VM image, or any release.

Use it only against the fake home:

```bash
cd app
MUSE_FAKE_STATE=cards pnpm dev
```

`WARDMUSE_FAKE_STATE` is the same switch and wins when both are set. `pnpm dev` serves made-up state on localhost. Do not point the browser at a real home, a logged-in Chrome profile, or anyone's keys, records, or mail. The server is started with `--isolated` and `--headless`, and it does not send usage statistics.

The pinned package is a dev tool. Nothing in `wardmuse/`, the proxy, or the systemd units launches it.
