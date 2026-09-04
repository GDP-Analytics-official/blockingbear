# Security Policy

BlockingBear exists to keep sensitive data away from a model provider. A security bug
here is not an abstraction: it is somebody's personal data leaving a building it was
supposed to stay in. We would much rather hear about it from you than from the people
whose data it was.

## Reporting a vulnerability

**Do not open a public issue.** Write to **info@gdpanalytics.com** with `SECURITY` in
the subject line, and give us a chance to ship a fix before the details are public.

If you would rather report it through GitHub, use **Security → Report a vulnerability**
on this repository (private vulnerability reporting). Either way it reaches the same
people.

Please include, as far as you can:

- what the impact is — in particular, whether unredacted content can reach a model
  provider, or one user's data can reach another user;
- the steps to reproduce it, and the commit or version you tested;
- whether the installation was SQLite or Postgres, and which optional pieces were
  running (code interpreter sandbox, web tools, OCR);
- **no real personal data.** If you need a document to demonstrate the bug, build a
  synthetic one — `backend/tests/assets/` shows the kind of thing we mean. A report is
  not a good reason to send us somebody's payslip.

We will acknowledge your report within **5 working days** and tell you what we think of
it — including if we think it is not a vulnerability, and why. There is no bug bounty:
this is a small team on a free-software project, and the only thing we can offer is
credit in the release notes, if you want it.

## What is in scope

The things we care most about, roughly in order:

- **Egress of unredacted content.** Anything that lets a real value reach OpenRouter, a
  provider, a search engine or a fetched page in a conversation marked as anonymized —
  in a prompt, an attachment, a tool call, a briefing, a filename or an error message.
- **Cross-tenant leaks.** One user reading another user's conversations, projects,
  files or entity registry; a project registry bleeding into a chat it does not belong
  to; a placeholder resolving to a value from somebody else's conversation.
- **Authentication and authorization.** Bypassing login, forging or replaying a token,
  reaching an admin-only endpoint as a standard user, escaping the forced password
  change on first login.
- **Secrets.** The OpenRouter Management API Key, a user's personal inference key or
  the JWT secret becoming readable by a user or returned to a browser.
- **The setup wizard after it has been completed.** Once completed, the public setup
  endpoints must refuse to set the Management API Key, create another administrator or
  change the anonymization defaults.
- **Sandbox escape.** Code executed by the model in the interpreter container reaching
  the host, the network, or files outside its workspace.
- **Server-side request forgery and injection through the web tools.** Getting the
  browser container to reach the host network or an internal address.
- **Restoration bugs that destroy data**, where a document comes back from an
  anonymization round corrupted or with content silently missing.

## What is out of scope

- **The setup window on a fresh install.** Until the setup wizard is completed, anyone
  who reaches the sign-in page can complete it and become the administrator. This is
  deliberate: there is no default password to leak, and the installation documentation
  says to complete the wizard immediately after the first start.
- **Anything that requires the administrator to have already made the choice.** Running
  a non-Zero-Data-Retention model after enabling the per-conversation exemption, or
  exposing the server on `0.0.0.0` without a reverse proxy, is a documented decision,
  not a vulnerability.
- **Missed detections.** The PII engine is a statistical model plus regex, and it will
  miss things — a name spelled in an unusual way, a format we do not know. Those are
  ordinary bugs: open a public issue with a *synthetic* example. What is a vulnerability
  is a value the engine **did** find and the app leaked anyway.
- **Denial of service** against your own installation, and vulnerabilities in
  third-party dependencies that we merely install — report those upstream, though we
  are glad to know if we are pinning a version with a known CVE.
## Supported versions

There are no releases yet. Fixes land on the default branch, and that is the only thing
we can support. If you are running BlockingBear in production, track that branch — and
tell us, so we can warn you directly when something serious is fixed.
