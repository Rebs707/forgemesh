# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately through
GitHub: on this repository's **Security** tab, choose **Report a vulnerability**. That
opens a private advisory visible only to you and the maintainers.

Include what you found, how to reproduce it, and the impact you expect. You should
get an acknowledgement within a week.

## Supported versions

Fixes land on `main`. There are no maintained release branches yet.

## Scope and deployment notes

ForgeMesh is built to run locally against your own databases:

- The web app listens on `127.0.0.1` by default and has no authentication. Don't
  expose it on a network you don't trust.
- The Docker services use development credentials (`root`/`password` for MySQL,
  `admin`/`admin` for Fuseki). Change them before running anywhere shared.
- API keys belong in `.env`, which is gitignored. Never commit them.
- Natural-language queries send schema context and your question to the configured
  LLM endpoint. Use a local model (see the README's "Running fully local" section)
  if the schema itself is sensitive.
