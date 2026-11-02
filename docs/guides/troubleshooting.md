# Troubleshooting account setup

[Documentation index](../README.md)

## What an email-code request error means

Account setup contacts `9.moto.5gencare.com` over certificate-verified TLS on
TCP port 3388. There is no hardcoded country, country code, language or region
parameter in the login request. The vendor can return another server within
`*.moto.5gencare.com`; the integration follows that route when submitting the
code. The `-6 <vendor-host>` code-request response also requires a reconnect
before requesting the code; the integration follows up to two redirects and
rejects loops. Other routing domains are currently rejected. Other regions have not yet
been validated.

A code-request error can mean a vendor rejection or an unexpected response,
including an unsupported routing host. It does not by itself prove rate limiting
or an incorrect email address. This happens before camera discovery, so it also
does not establish whether a particular camera model is compatible.

## Collect logs when the first setup cannot finish

Update the integration in HACS, or replace the files in
`custom_components/motorola_nursery` if you installed it manually. Restart
Home Assistant to load the new files.
Then enable logging **before** submitting the email form. If setup has never
completed, the integration's debug button or diagnostics download may not be
available. Use this method instead; it does not require another restart:

1. Open **Developer tools > Actions** and switch to YAML mode.
2. Run:

   ```yaml
   action: logger.set_level
   data:
     custom_components.motorola_nursery: debug
   ```

3. Open **Settings > Devices & services > Add integration > Motorola Nursery**
   and reproduce the failure once. Note the approximate time. Do not keep
   requesting codes repeatedly. If a code arrives, do not share it.
4. Open **Settings > System > Logs**, select **Home Assistant Core** if a source
   selector is present, and use **Download full logs**. Filter the downloaded
   file to lines containing `custom_components.motorola_nursery` around the
   attempt. Account messages start with `Account`; local connection messages
   start with `Local tunnel` or `Local camera probe`.
5. Restore the log level with the same action, changing `debug` to `warning`.
   If you previously configured another level, restore that level instead.
6. Review the selected lines and attach them as a `.txt` file to the issue.
   Do not post the complete HA log: other integrations can log private data.

If `logger.set_level` is unavailable, add the following to the existing
`logger:` configuration (do not create a duplicate section), then restart HA:

```yaml
logger:
  default: warning
  logs:
    custom_components.motorola_nursery: debug
```

Reproduce once, download and review the logs as above, then remove this debug
setting and restart HA when convenient.

## Existing installations

For an integration that already has an entry, use **Settings > Devices &
services > Motorola Nursery > three-dot menu > Enable debug logging**. Reproduce
the problem, then choose **Disable debug logging** to download the log.
**Download diagnostics**, when available, also includes integration and HA
versions and connection/capability counters. Diagnostics are supplementary:
they do not contain the failed first-setup exchange.

See Home Assistant's [debug logging and diagnostics guide](https://www.home-assistant.io/docs/configuration/troubleshooting/#debug-logs-and-diagnostics).

## Code accepted, but camera listing fails

If logs show a completed `v3_loginset` followed by a `v3_dlist` validation
failure, the OTP authentication has succeeded and the server has responded.
An unsupported camera-list response is different from a network outage.
The setup error distinguishes these cases.

With integration debug enabled, share the reviewed `Account device list framing`,
`Account device list rejected`, `Account device record` and
`Account device record rejected` lines from one attempt, along with the setup
version line. They identify the count/shape or credential rule that failed using
only types, lengths and validation flags. No raw response is needed. Use the
same log collection procedure above, then restore the log level.

For example, `declared_count=1 received_fields=8 expected_fields=8` confirms the
expected seven-field layout for one camera; `reasons=device_id_length` then
isolates the unsupported field. This does not establish why its value differs
or prove that the whole camera protocol is incompatible. The current account parser accepts SID device values up to 30 bytes; update
and restart HA before reporting a rejection from an older installation.

If camera selection now succeeds, enter the camera's LAN address from a network
that Home Assistant can reach. Report whether setup accepts that address, then
whether opening the camera produces live video. A local connection failure is
a separate stage from account login or device listing. Include the camera
firmware if the app displays it, but do not send credentials or serial numbers.

## Camera listed, but local connection fails

Enable integration debug logging as described above, then submit the camera
address once. If the setup form is still open, retry that step without requesting
another email code.
Share the reviewed `Local camera probe` and `Local tunnel` lines from that
attempt, along with the integration version and camera firmware version.

The existing network error can represent several different stages:

| Log stage/category | What it establishes | Next check |
| --- | --- | --- |
| `tcp_connect` / `dns` | Hostname resolution failed. | Verify the hostname from the HA environment. |
| `tcp_connect` / `connection_refused`, `unreachable` or `timeout` | The local TCP service was not reached. | Check camera TCP port 77 from the HA container/host, network isolation and service availability. A successful ping does not prove TCP access. |
| `handshake_send` | TCP opened, but sending the request failed. | Use the error category to investigate a reset, timeout or write failure. |
| `handshake_receive` / `incomplete_response` or `timeout` | The request was sent but no complete acknowledgement arrived. | Inspect the safe response length and shape summary. |
| `handshake_validate` / `unexpected_response` | The ten-byte reply differed from `ok 0 dconn`. | Compare the safe prefix/shape flags; this is not proof that the account password is wrong. |
| `first_response` / `timeout` | The ACK succeeded, but no decoded stream data arrived within eight seconds of the first receive. | A stalled optional SID echo or cipher prefix can cause this; the tunnel closes and releases the bridge slot. |
| `Local tunnel authenticated` | The v1 tunnel acknowledgement matched. | Confirm probe cleanup and setup completion, then test live video separately. |

`Local tunnel response` reports received/expected byte counts, whitespace field
count and prefix shape (`expected_prefix`, `negative_status`, `other`, `empty`).
The expected acknowledgement is ten bytes. After stream data arrives,
`Local tunnel acknowledgement suffix` reports `sid_echo` for the extended reply
or `none` for the short reply. Unknown reply contents and numeric statuses
are not printed. A short error reply may remain pending until EOF or the
connection timeout; its summary still identifies a short reply.
If authentication succeeds but `Local camera probe closing` is followed by a
probe failure, the error occurred during teardown rather than authentication.

These logs narrow the investigation; they do not prove another transport is
required. No packet capture or raw handshake is needed for the first report.

## Testing a beta release

Use the latest stable release for normal installation. Install a beta only
when testing a requested candidate; note its exact version in the report.

In HACS, find Motorola Nursery, open the three-dot menu and choose **Redownload**.
Under **Need a different version?**, select the requested beta tag. If it is
missing, use **Update information** and enable beta/prerelease visibility if
your HACS version offers that option. Confirm the exact beta version before
downloading, then restart Home Assistant. See the official
[HACS version-selection guide](https://www.hacs.xyz/docs/use/repositories/dashboard/#downloading-a-specific-version-of-a-repository).

Enable debug logging, reproduce the requested step once, review/share only the
integration lines, then restore your previous log level. To return to stable,
use the same version selector to choose the stable release and restart HA.

## What to include in an issue

- Integration and Home Assistant versions.
- Camera model and firmware, if known.
- Your country/region and the exact mobile app name and version. Region is
  supplied by you; the integration does not infer or collect it.
- Whether a **fresh sign-in code** can be requested and received using the
  official app. An existing signed-in session does not test code delivery.
  Avoid signing out of your only working app session just for this test.
- Approximate attempt time and the reviewed integration log excerpt.

Debug logs report connection stage, DNS/TLS/timeout categories, operation names,
response field counts, first-field category, short negative numeric vendor
statuses, elapsed time, and whether the vendor redirects login. Numeric status
meanings are not assumed. They omit emails, codes, client/account/device IDs,
tokens, credentials, camera names/addresses, raw responses and routing hostnames.
No logs or diagnostics are sent automatically to the maintainer.

For example, `operation=v3_otp fields=1 first=negative_status status=-10`
followed by `reason=server_rejected` distinguishes a vendor refusal from
`reason=unexpected_response` or `reason=unsupported_routing_host`. It does not
explain an undocumented vendor status on its own. These logs narrow the next
investigation; additional evidence may still be necessary.

## Incoming LAN mode has unavailable readings

Check that local mode is enabled, that HA can listen on TCP 2278, and that only
the camera's outgoing TCP 2278 is redirected to that listener. The camera does
not expose an equivalent getter service at its own port 2278. Check return
routing/NAT as well as firewall reachability. Video on TCP 77 is a separate path.

Debug logs report initialization stages and error categories without credentials
or raw frames. A missing camera connection never triggers account renewal or
cloud telemetry. Disable local mode to restore the standard transport. After a
camera reset or relink, repeat account setup manually to refresh saved credentials.
Local mode supports allowlisted settings and lullaby commands when the camera
advertises usable capabilities. PTZ requires writable axes; actual motor behavior
on compatible models remains unverified. See [local readings](local-readings.md).
