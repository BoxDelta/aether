# Security policy

AETHER is early-stage software built by one person for a single user on
their own machine. It is not production-ready and has no security
guarantees. Reports are still welcome and taken seriously.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the **Security** tab of
this repository and choose **Report a vulnerability**. This keeps the
report private between you and the maintainer.

Please include what you found, how to reproduce it, and what you think the
impact is. You will get a reply on a best-effort basis; this is a
one-person project, so there is no fixed response time.

## In scope

-   Core (the API), the model router, the identity layer and the memory
    code in this repository.
-   Authentication and access control to Core, privacy-level routing (what
    may be sent to a cloud model), event logging, and database migrations.
-   Secrets handling: anything that could leak a secret into a log, an
    event, a prompt or the repository.
-   The CI and local guardrails in this repository.

Out of scope: vulnerabilities in third-party software (Ollama, Postgres,
Docker, the Claude API, Python packages) that are not caused by how this
project uses them (report those upstream), and the security of your own
machine or network.

## Please do not

-   Open public issues, pull requests or discussions for a vulnerability.
-   Test against anyone else's systems, accounts or data. Test only on
    your own copy running on your own machine.
-   Include real secrets, tokens or personal data in a report.
