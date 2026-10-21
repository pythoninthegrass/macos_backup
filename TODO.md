# TODO

- [ ] Run one-time setup (password in keychain + 1Password, `init`, Full Disk Access grant, `install`)
- [ ] Export the 3 Apple signing identities to a passphrase-protected .p12 as a portable backup
- [ ] Run first full backup by hand on LAN (with a direct Tailscale peer connection) and time it
- [ ] Verify restore of `Library/Keychains` unlocks correctly
- [ ] Consider `restic/rest-server` on the Synology (Docker) if SFTP throughput over Tailscale disappoints
