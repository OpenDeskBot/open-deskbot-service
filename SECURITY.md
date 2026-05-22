# Security Policy

## Supported versions

Security fixes are applied to the default branch (`main`). Tagged releases, if any, are listed in GitHub Releases.

## Reporting a vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Instead, email the maintainers with:

- Description of the issue and potential impact
- Steps to reproduce
- Affected component (`deskbot-server`, `paddlespeech-server`, etc.)
- Suggested fix (if any)

We aim to acknowledge reports within a few business days.

## Scope notes

- **Secrets**: Never commit `.env`, API keys, or SSH keys. Use environment variables at runtime.
- **Network exposure**: Default configs bind `0.0.0.0`. Do not expose deskbot-server directly to the public internet without authentication and TLS termination.
- **Dependencies**: Report supply-chain concerns via the same private channel or GitHub Dependabot alerts on the repository.

## Safe defaults for self-hosting

- Run behind a firewall or reverse proxy
- Set `LLM_API_KEY` via environment, not config files in images
- Keep model directories read-only inside containers where possible
