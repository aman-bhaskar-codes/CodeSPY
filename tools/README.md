# External analyzer inventory
Python libraries are locked by uv; JS libraries by npm. External binaries require separate installation and license checks.

| Tool | Purpose | Setup status |
| --- | --- | --- |
| Ghidra + compatible JDK | Native decompilation | 12.1.4 downloaded, checksum verified, headless usage checked with JDK 27 |
| Rizin | Binary analysis | Installed; see docs/TOOLCHAIN.md |
| JADX + compatible JDK | Android DEX | Installed; see docs/TOOLCHAIN.md |
| WABT | WebAssembly tools | Installed; see docs/TOOLCHAIN.md |
| libmagic | python-magic native library | Installed; python-magic smoke check passed |
| Semgrep CE | Static rules | 1.181.0, separate isolated uv tool environment |
| Docker Desktop | Local services | Engine running; three CodeSPY dependency services verified |
| OpenTofu | Terraform-compatible future AWS provisioning | 1.13.1; provider only, no resources declared |
| gitleaks | Secret scanning | Installed; see docs/TOOLCHAIN.md |
| Firecracker | Runtime microVMs | Linux/KVM environment required |
| QLoRA/Unsloth | Future specialist fine-tuning | Separate Linux GPU environment, deferred |

Paid/proprietary REA integrations (Hopper, IDA, JEB) are not mandatory CodeSPY dependencies. ADB/Apktool/Frida/Binwalk/TShark are optional future adapters; install when their target formats are in scope.
