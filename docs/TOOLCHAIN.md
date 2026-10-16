# Verified local toolchain

| Component                   | Version     |
| --------------------------- | ----------- |
| Python                      | 3.12.12     |
| Project Node                | 22.23.3     |
| JDK                         | 21.0.12.1   |
| FastAPI                     | 0.143.3     |
| SQLAlchemy                  | 2.0.54      |
| Arq                         | 0.28.0      |
| Ghidra                      | 12.1.4      |
| Binwalk                     | 3.1.0       |
| Unblob container            | 26.6.4      |
| ASAR                        | 4.3.1       |
| Semgrep                     | 1.181.0     |
| Rizin                       | 0.9.1       |
| JADX                        | 1.5.6       |
| WABT                        | 1.0.42      |
| Gitleaks                    | 8.30.1      |
| Terraform                   | 1.16.6      |
| OpenTofu                    | 1.13.1      |
| LocalStack                  | 3.8.1       |
| PostgreSQL vector / pg_trgm | 0.8.7 / 1.6 |

Source `scripts/env.sh` to select project Node 22, JDK 21 and ignored `.tools` installations. The machine's global Node default is preserved. Root `uv.lock` and both npm lockfiles capture package versions; `backend/uv.lock` links to the workspace lock. Local executables and environments are not committed. Fresh machines must install the external tools separately; `make tools` handles isolated Semgrep only.

Official Ghidra and Terraform downloads were checksum verified. Unblob uses a pinned container with networking disabled. SDK installation does not configure or validate provider access. API keys and AWS account actions are owner-managed and prohibited for the assistant.
