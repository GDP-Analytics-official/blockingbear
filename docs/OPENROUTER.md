# OpenRouter configuration

BlockingBear reaches every model through OpenRouter. The installation owns one
OpenRouter credential, a **Management API Key**; every application user then gets a
personal inference key created from it.

| Credential | Who creates it | Where it is stored | Purpose |
| --- | --- | --- | --- |
| Management API Key | The administrator, in their own browser at <https://openrouter.ai/settings/management-keys> | `data/openrouter_management.key` | Creates, rotates and manages the per-user inference keys; reads usage, balance and analytics. Cannot perform chat completions. |
| Personal inference keys (`blockingbear-<username>`) | BlockingBear, through the Management API Key | PostgreSQL user records | Chat inference for their respective users, with optional individual spending limits |

There is no shared inference key: a user without a personal key cannot use the chat.

## Setup wizard

Until it has been completed, the sign-in page shows a setup wizard. It asks, in order:

1. The interface language.
2. The Management API Key. Create it in your own browser: sign in at
   <https://openrouter.ai/settings/management-keys>, choose **+ New key**,
   give it a recognizable name such as `blockingbear` and copy the value. BlockingBear
   asks OpenRouter what kind of key it is and rejects normal inference keys.
3. The password of the `admin` account (at least 8 characters). BlockingBear creates the
   account and, through the Management API Key, its personal inference key
   `blockingbear-admin`. If OpenRouter refuses, the account is not created and the
   wizard shows the error.
4. The categories of personal data to anonymize for every user.
5. Optionally, the first terms to always anonymize for every user.
6. Whether chat anonymization is required or optional, and whether models without Zero
   Data Retention providers may be used.
7. Optionally, the default chat model for every user, with its initial options.

Steps 4–7 can be changed later from **Settings**. The wizard does not sign in: once
completed, the sign-in page appears and the administrator logs in with the chosen
password.

Complete the wizard immediately after the first start: until it is completed, anyone who
reaches the sign-in page can complete it and become the administrator. Once completed,
the public setup endpoints refuse further changes.

Never paste the key into `backend/.env`, a terminal, an issue, the repository or an agent
conversation. The wizard is the only place where it is entered; afterwards it can be
replaced from the **API keys** admin page.

## Per-user keys

- Creating an application user creates their inference key, with the spending limit and
  reset period chosen on the **Users** page. If OpenRouter is unreachable at that
  moment, the user is created anyway and the key is created at backend startup or at the
  user's first chat message.
- OpenRouter returns the plaintext of a new key only once; BlockingBear stores it
  immediately on the user record. A key whose plaintext was lost is rotated (revoked and
  recreated) from the **Users** page.
- Deleting a user revokes their key.
- The **API keys** page lists the keys with their usage and limits; **Costs** shows the
  account balance and analytics. All generated keys draw from the same OpenRouter account
  balance; a per-key limit caps consumption but does not reserve part of that balance.

## Verification

Sign in as the administrator. **API keys** must show the Management API Key in use
(masked) and the account balance, and the **Users** page must show a key for the
administrator. Send a chat message to confirm inference.

## Backups and credential rotation

- Back up `data/` for the Management API Key, the JWT secret and application data.
- Back up PostgreSQL for users and their personal inference keys.
- If the Management API Key is exposed, revoke it in OpenRouter, create a new one and
  paste it into **API keys**. Existing personal keys keep working: they belong to the
  OpenRouter account, not to the Management API Key that created them.
- If a personal key is exposed, rotate that user's key from the **Users** page.

OpenRouter reference: [Management API Keys](https://openrouter.ai/settings/management-keys).
