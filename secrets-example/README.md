Create a sibling directory named `secrets/` (gitignored) with:

- `admin_password.txt`: one strong administration password.
- `session_secret.txt`: at least 32 random characters (`openssl rand -base64 48`).
- `google_oauth_client.json`: downloaded Google OAuth Desktop client JSON.

Never commit these files. This image runs as UID/GID `10001:10001`. With local Docker Compose, file-backed secrets are bind-mounted with their host ownership, so set the production copies to `chown 10001:10001 secrets/*` and `chmod 400 secrets/*`; otherwise the non-root process cannot read them. The OAuth refresh token is created later as `/data/google-token.json` in the persistent volume; it is also a secret and must not be printed or committed.
