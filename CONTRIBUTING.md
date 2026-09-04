# Contributing to BlockingBear

Contributions are welcome — bug reports, fixes, new document formats, better PII rules,
translations. Please open an issue before starting anything large, so we can agree on the
approach before you spend your evenings on it.

Taking part means following our [Code of Conduct](CODE_OF_CONDUCT.md).

## Sign your commits (DCO)

Every commit must carry a `Signed-off-by` line. This is the
[Developer Certificate of Origin](DCO), the same lightweight mechanism used by the Linux
kernel, Docker and Nextcloud. It is **not** a copyright assignment: you keep the
copyright on what you write. You are certifying one thing — that you have the right to
contribute that code under the AGPL-3.0.

That matters more than it sounds. If you write code on your employer's time or hardware,
in many jurisdictions the copyright belongs to your employer, not to you, and it is not
yours to give away. The same goes for code lifted from a project under an incompatible
licence. The sign-off is you telling us you have checked.

Git adds the line for you:

```bash
git commit -s -m "your message"
```

It appends `Signed-off-by: Your Name <your@email.com>`, taken from your `user.name` and
`user.email`. Use a real name and a real address. Forgot it on the last commit?

```bash
git commit --amend -s --no-edit          # last commit
git rebase --signoff main                # a whole branch
```

## What your contribution is licensed under

BlockingBear is **AGPL-3.0** and will stay that way. Your contribution is licensed to the
project under the AGPL-3.0 too, and nobody — including GDP Analytics — can relicense it
into a closed-source product. There is no contributor agreement to sign beyond the
sign-off above.

## Before you open a pull request

- **Run the test suites** that touch what you changed. They are plain scripts, not
  pytest: `python backend/tests/<name>_test.py`. The
  [development guide](docs/DEVELOPMENT.md) names the main ones and explains
  `BLOCKINGBEAR_TEST_DATABASE_URL`; `backend/tests/` holds the rest, one file per area.
- **Never point the tests at a real database.** They create and drop data. Use a
  throwaway one, and recreate it between suites.
- **Write repository documentation, technical explanations and new code comments in
  English.** User-visible strings must still be localized as described below.
- **User-visible strings go through i18n**, both `it` and `en`. Never hardcode a string
  in a component, and never hardcode a PII tag name — those come from the model config.
- **Do not commit** anything from `backend/data/`, `backend/models/`, any `.env`, or any
  real document. Test assets belong in `backend/tests/assets/` and must be synthetic.
- If you touch the anonymization path, say in the pull request **what you tested it on**.
  This is a privacy tool: a regression here leaks somebody's data.

## Reporting a security issue

Please do **not** open a public issue for a vulnerability, in particular anything that
could leak unredacted content to a model provider. Write to **info@gdpanalytics.com**
instead and give us a chance to ship a fix first. [SECURITY.md](SECURITY.md) says what
to include, what is in scope, and how quickly you can expect an answer.
