# Windows release artifacts

## Included

- `windows-x64/GcpVpnManager-windows-x64.exe`
- `windows-x86/GcpVpnManager-windows-x86.exe`
- `SHA256SUMS.txt`: SHA-256 hashes relative to this directory
- Per-architecture dependency inventories and third-party license notices

Build toolchain: official signed CPython 3.13.17 Windows x64 / x86 distributions,
PyInstaller 6.16.0. Both builds are native to their respective Python architecture;
the x86 build is not a renamed x64 executable. The apps themselves are unsigned.

## Verification scope

- Automated tests run in both x64 and x86 Python: first-run UI, one/many servers,
  persistent DPAPI storage, config validation, cloud response handling, tunnel
  sequencing, retained service configs, and log retention.
- Tk UI exercised on the Windows 11 x64 build host, including 32-bit Python under
  WOW64. This is not testing on a separate physical 32-bit Windows computer.
- EXE PE architecture, GUI subsystem, administrator manifest and required bundled
  modules verified. No deployed key/profile/log bundled; the public CA certificate
  bundle from certifi is expected.
- Ubuntu server scripts passed Bash syntax and ShellCheck warning checks.
- Cloud operations are covered by simulated API responses, not a live GCP
  deployment. Verify WireGuard handshakes and network connectivity after
  configuring your own server.

## Check a download

```powershell
Get-FileHash .\windows-x64\GcpVpnManager-windows-x64.exe -Algorithm SHA256
```

Compare with `SHA256SUMS.txt` from a trusted copy of the source/release. A checksum
detects changed bytes; it does not authenticate an unsigned publisher by itself.

Run the EXE for your Windows architecture and complete the first-run setup.
The app stores settings and logs in AppData. Keep credentials and imported
WireGuard profiles in a private location.

Retain each architecture's `licenses/` directory when redistributing its EXE.
GitHub Actions build artifacts have a limited retention period.
