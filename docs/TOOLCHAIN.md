# Verified local toolchain

| Component                   | Verified version  |
| --------------------------- | ----------------- |
| Python                      | 3.12.12           |
| Node / npm                  | 24.18.0 / 11.16.0 |
| FastAPI                     | 0.143.3           |
| SQLAlchemy                  | 2.0.54            |
| React                       | 19.3.1            |
| TypeScript                  | 5.9.3             |
| Tailwind                    | 4.3.3             |
| Semgrep (isolated)          | 1.181.0           |
| Rizin                       | 0.9.1             |
| JADX                        | 1.5.6             |
| WABT                        | 1.0.42            |
| gitleaks                    | 8.30.1            |
| OpenTofu                    | 1.13.1            |
| OpenJDK                     | 27                |
| PostgreSQL vector / pg_trgm | 0.8.7 / 1.6       |
| Docker Engine               | 29.0.1            |

Python and npm packages are locked in uv.lock and package-lock.json. External tools are installed via Homebrew except Semgrep and Ghidra, which live in ignored .tools/. Ghidra release 12.1.4 archive checksum matched the official GitHub release SHA-256: ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db. The archive is not committed.

The local ignored .env records JAVA_HOME, GHIDRA_INSTALL_DIR and PLAYWRIGHT_BROWSERS_PATH. CLI tools do not automatically source .env; export those settings in a terminal when needed. The installed browser runtime was smoke-tested through both Python and Node Playwright. Semgrep may need SSL_CERT_FILE=/etc/ssl/cert.pem on this Mac; TLS verification remains enabled.

AWS CLI v2.38.1 is installed separately through Homebrew for modern SSO workflows; CLI v1 was removed from the application environment. The connected AWS plugin supplies authenticated account access. No long-lived local AWS access keys or local profile were created.

Use `make install`, `make services`, `make checks`, `make stop`; `make tools` recreates the isolated Semgrep installation. Ghidra is an official external distribution: https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.4_build . Source binaries are not executed during setup.

AWS OpenTofu provider 6.69.1 was downloaded with signature verification and locked in infra/terraform/.terraform.lock.hcl. Provider configuration passed fmt and validate. No resource plan or apply was run.
