# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in TurboDRF, please
report it privately so it can be patched before disclosure. **Do not file
a public issue or PR with exploitation details.**

**How to report:**

- **Preferred:** [GitHub Private Vulnerability Reporting](https://github.com/AlexanderCollins/turbodrf/security/advisories/new) — open a draft advisory directly. Only the maintainer can see it until disclosure.
- **Email:** `alexcollins010+turbodrf-security@gmail.com`

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce (or a minimal proof of concept)
- The TurboDRF version affected
- Your assessment of severity
- Any thoughts on remediation

## What to expect

TurboDRF is maintained on a best-effort basis by a small team — there is
no guaranteed response SLA. Reports are reviewed when the maintainer is
next online, which may be days or weeks. Please be patient and don't
disclose publicly while a fix is in flight.

The general flow:

- **Acknowledgement** when the maintainer next gets online — typically
  days, occasionally weeks for solo periods. If you haven't heard back
  in 60 days, feel free to send a follow-up.
- **Triage**: confirmation, severity rating, and a rough patch
  timeline if the report is accepted.
- **Coordinated disclosure**: a fix is developed privately, a patch
  release is published, and a security advisory (CVE if warranted) is
  filed against the GitHub Advisory Database. Reporters are credited
  unless they prefer anonymity.
- **Embargo**: please give the project reasonable time to ship a fix
  before public disclosure. We don't enforce a fixed window; common
  practice is 90 days for medium-severity issues, longer for complex
  ones, sooner if a fix is already published.

This project follows
[coordinated vulnerability disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure)
as the default. If a vulnerability is being actively exploited or
affects users in immediate danger, please mark the report **URGENT** in
the subject so it surfaces ahead of routine triage.

## Supported versions

Security fixes go into the latest minor release line only (currently
0.8.x). Older lines don't get patches: upgrade, following the release notes
in [`CHANGELOG.md`](CHANGELOG.md).

| Version | Supported |
|---------|-----------|
| Latest minor (0.8.x today) | ✅ |
| Older minors | ❌ — upgrade |

## Threat model

See [`docs/security.md`](docs/security.md) for the documented threat
model: what TurboDRF protects against, where consumer code retains
responsibility, the framework's defense-in-depth options, and its known
limitations.
