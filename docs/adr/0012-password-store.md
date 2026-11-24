# ADR 0012. Passwords in the secure storage of the system

Status: accepted, 2026-11-24. Moves on from the credentials row of ADR 0006.

## Context

The server took the password of its login from the libpq password file, `~/.pgpass`, as `psql` does,
and `install.py import` copied every password DBeaver had saved into that file. The file is clear
text: whoever reads it, a backup of it or a dotfiles repository that swallowed it, has the passwords.
On 2026-11-24 the requirement was set: the passwords the server itself keeps are never stored in
clear. Whether `psql`, DBeaver or another tool can read them is not a goal.

What bounds the decision:

- ADR 0001: the standard library only, Python 3.9+, on macOS, Windows and Linux; the Linux users are
  on desktops.
- The agent starts the server without a terminal: nothing may ask the user anything at that moment.
- Some users have hundreds of logins (dozens of stands, a login per schema), and one call may ask a
  hundred stands at once.
- Those who have a `~/.pgpass` already must keep working after an update.

## Decision

| Question | Choice | Why |
|---|---|---|
| Where the passwords are | one record in the secure storage of the system: a generic password in the login keychain of macOS, a file sealed by DPAPI under `%APPDATA%\pg-explorer-mcp` on Windows, an item of the Secret Service (the keyring of the desktop) on Linux | the system encrypts it with a key of its own, bound to the user's login; the server has no cryptography of its own |
| What the record holds | the lines of the libpq password file, `host:port:database:role:password`, matched as libpq matches them | the one format `psql` users know, with `*` and the escapes; the lookup is the one that read the file |
| How the system is reached | macOS: `/usr/bin/security`; Windows: `ctypes` into `crypt32`; Linux: the `secret-tool` utility of libsecret | each is on the machine already or in the standard library (ADR 0001) |
| Where a server takes the password from | the first place that has one: `PGPASSWORD`, the record, the libpq password file, the `password=` line of a registry section | nothing that worked stops working, and the record wins over the places in clear text |
| How often the record is read | once in 300 seconds per process, at the first lookup after that | a call over a hundred stands pays one reading, not a hundred |
| Who writes the record | `install.py password`, typed in a terminal, and `install.py import`; never the server | the server is started by the agent and has no terminal; a tool call must not change a password |
| The libpq password file | still read; `status` warns that the password comes from it in clear text; `install.py password --move` carries a login over | an update breaks nothing; the warning and the command move each user over |
| `password=` in a registry section | still works, last in the order; `status` warns about the clear text | the same |
| No secure storage on the machine | the server keeps no password and says so in `status`; `PGPASSWORD` and the password file are what is left | anything to encrypt with would be clear text there as well |
| `PG_EXPLORER_MCP_PASSWORD_STORE=off` | the record is neither read nor written | the tests, CI, and a machine where the storage of the system must not be touched |

## Alternatives considered

- **Keep `~/.pgpass`.** Clear text: the requirement rules it out.
- **`PGPASSWORD` in the environment.** It would sit in the settings file of the agent, in clear.
- **A passphrase asked when the server starts.** The agent starts the server without a terminal.
- **One record per login in the storage of the system.** Nothing can be stored where there is no storage; on macOS every password passes through the argument of `security` when it is written; and a call over a hundred stands pays a hundred readings.
- **A file of our own, encrypted, with its key in the storage of the system.** AES-CTR and HMAC-SHA256 on the vendored AES, the composition SSH uses: the modes matched OpenSSL in a spike, and the AES block took 0.69 ms in pure Python, 0.7 s for 16 KB, seconds to open the file of a user with hundreds of logins. A keystream built from HMAC would be fast and would be a construction of our own. When the storage of the system holds the record itself, there is nothing of ours to get wrong.
- **A file encrypted with a passphrase, the key next to it.** Two files in clear instead of one.
- **The keychain through `ctypes` into Security.framework.** macOS asks the user for each application, and Python is not the application to vouch for a record. The `security` tool is trusted for what it created.
- **Kerberos (GSSAPI), OAuth, a client certificate: no stored password at all.** The only way to have no secret on the disk. pg8000 speaks none of them, ADR 0001 keeps it as the driver, and each of them needs the DBA to change how the server authenticates.

## Consequences

- What this protects against: the file, its backups, a dotfiles repository, another user's account.
  What it does not protect against: a program running as the same user. On macOS that program calls
  `security`, on Windows it calls DPAPI, on Linux it talks to the keyring over D-Bus, and none of them
  asks.
- On macOS the record goes through the argument of `security` when it is written, base64 on one line:
  whoever can read that argument can call `security` and read the record anyway. A command line holds
  at most 1 MiB there: about 700 KB of lines, thousands of logins.
- Linux needs `secret-tool`, which is not on every desktop by default (`libsecret-tools` on Debian and
  Ubuntu, `libsecret` on Fedora and RHEL); `install.py password` names it. A server, a container or
  WSL has no keyring: there the server keeps no password.
- The store of each system is small and its own; they arrive one system at a time.
- A password put into the record is used by the next connection, at most 300 seconds later: the
  settings are read again before a session is opened.
- `install.py export` cannot hand a password to another tool through the password file for those who
  keep none: such a tool asks for it once, as the PostgreSQL extension of VS Code always did.
- The tests never come near the storage of the system. A server in the test process gets a store in
  memory, a server started as a process gets `PG_EXPLORER_MCP_PASSWORD_STORE=off`. One test tries the
  real keychain of a Mac, with `PG_EXPLORER_MCP_TEST_KEYCHAIN=1`, on a record of its own that it deletes.
