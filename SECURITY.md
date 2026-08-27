# Security Policy

## Supported version

Security fixes are made on the current `main` branch. This project does not
maintain security support for unpublished snapshots, forks, or old commits.

## Report a vulnerability

Use GitHub's private vulnerability-reporting form for this repository. Do not
open a public issue containing exploit details, credentials, student data, or
other sensitive information.

Include the affected component, reproduction conditions, expected impact, and
the smallest safe proof of concept. Remove tokens and personal information from
logs or screenshots before attaching them.

If private vulnerability reporting is temporarily unavailable, wait for a
private channel rather than disclosing an unpatched vulnerability publicly.

## Operational secrets

Real credentials belong only in ignored environment files or the deployment's
secret manager. The tracked `.env.example` contains names and placeholders, not
working values.
